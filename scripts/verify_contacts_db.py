"""拿真库验 contacts_db 那九个函数和 0011 那四张表。默认整跑回滚，一行不留。

    python -m scripts.verify_contacts_db            跑一遍然后回滚
    python -m scripts.verify_contacts_db --commit   留下数据（基本不需要）

前提：0011 已经上了库。没上的话第一句 insert 就会报 relation does not exist。

为什么这一段必须在真库上跑：tests/test_contacts_db.py 用假游标，只验得出「参数按列名
落对了位置」。下面这几件全是库的行为，假游标验不了：

    一、jsonb 和 text[] 写进去再读出来是不是原样（dict 回来还是 dict、tuple 进 text[] 不报错）
    二、on conflict (lead_id, channel, lower(value), source) 能不能认出那个表达式唯一索引，
        大小写不同的同一个地址是不是真被挡住
    三、两条 CHECK 挡不挡得住：个人主页来的邮箱没带 person_lookup_id、地址是空串
    四、numeric(10, 5) 加总之后是不是精确的 Decimal
    五、leads.domain 那个唯一索引真撞上时，set_domain_if_empty 是不是先一步认出来「被占了」
    六、删一家公司时四张表的外键动作是不是都对（cascade 删干净、set null 不撞 CHECK）

造的 lead 用 verify-contacts- 前缀，万一 --commit 了也认得出来。
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal

import psycopg

from models import (
    CompanyProfile,
    ContactChannel,
    ContactSource,
    FoundContact,
    PersonLookup,
    SpendPurpose,
)
from persistence import contacts_db, db

TEST_PREFIX = "verify-contacts-"
NOW = dt.datetime.now(dt.UTC)


def seed(cur) -> tuple[int, list[int]]:
    """插一行 runs 和三行最简单的 lead，返回 (run_id, [lead_id, ...])。

    输入：游标
    输出：如 (812, [2001, 2002, 2003])

    lead 只填四个必填列，身份按名字建，不经过 lead_db 的身份归并：这里验的是联系方式那几张表
    """
    cur.execute(
        "insert into runs (run_type, status, run_date) values ('scheduled', 'running', %s) returning id",
        (dt.date.today().isoformat(),),
    )
    run_id = int(cur.fetchone()[0])

    leads = []
    for i in range(3):
        name = f"{TEST_PREFIX}{i}"
        cur.execute(
            """
            insert into leads (identity_kind, identity_value, normalized_name, display_name)
            values ('name', %s, %s, %s)
            returning id
            """,
            (name, name, f"测试公司 {i}"),
        )
        leads.append(int(cur.fetchone()[0]))
    return run_id, leads


def expect_error(cur, what: str, fn) -> None:
    """跑 fn，要求它被库拒掉；拒掉之后事务还能接着用。

    输入：游标；这一条在验什么（印出来用）；一个不带参数的函数
    输出：无。没被拒就 assert 炸

    用 savepoint 包起来：Postgres 里一句报错会让整个事务进入「只能回滚」的状态，
    不回到 savepoint 的话后面几段全跑不了
    """
    cur.execute("savepoint expect_error")
    try:
        fn()
    except psycopg.errors.CheckViolation as err:
        cur.execute("rollback to savepoint expect_error")
        print(f"  [OK] {what}：被 CHECK 挡住（{err.diag.constraint_name}）")
        return
    raise AssertionError(f"{what}：应该被库拒掉，却写进去了")


def verify_profiles(cur, *, lead: int, run_id: int) -> None:
    """概况写两次，读回来是最新那一次，jsonb 和 text[] 原样回来。"""
    old = CompanyProfile(
        website="http://www.hushly.example",
        employee_count=9,
        employee_count_range="2-10",
        founded_year=2022,
        location="United States",
        industries=("Music",),
        phone=None,
        raw={"name": "Hushly", "employeeCount": 9},
    )
    new = CompanyProfile(
        **{**old.__dict__, "employee_count": 11, "raw": {"name": "Hushly", "employeeCount": 11}}
    )

    contacts_db.insert_company_profile(cur, lead, old, run_id=run_id, fetched_at=NOW - dt.timedelta(days=30))
    contacts_db.insert_company_profile(cur, lead, new, run_id=run_id, fetched_at=NOW)

    got = contacts_db.latest_profiles(cur, [lead])
    assert got[lead] == new, got[lead]
    print("  [OK] 同一家两份概况，读回来是最新那份；industries 回来是 tuple、raw 回来是 dict")

    cur.execute("select count(*) from company_profiles where lead_id = %s", (lead,))
    assert cur.fetchone()[0] == 2
    print("  [OK] 旧那份还在（只往里加，不覆盖）")


def verify_person_lookups(cur, *, lead: int, run_id: int) -> int:
    """同一个人查两次：第二次撞唯一约束，拿回同一个 id。返回那个 id 给后面写联系方式用。"""
    lookup = PersonLookup(
        person_linkedin_url="https://www.linkedin.com/in/verify-contacts-sam/",
        person_name="sam hadid",
        headline="ceo @ Hushly - a sanctuary for human sound",
        current_company_name="Hushly",
        current_company_linkedin_url="https://www.linkedin.com/company/hushly/",
        matches_lead=True,
        raw={"firstName": "sam"},
    )

    first = contacts_db.insert_person_lookup(cur, lead, lookup, signal_id=None, run_id=run_id, fetched_at=NOW)
    again = contacts_db.insert_person_lookup(cur, lead, lookup, signal_id=None, run_id=run_id, fetched_at=NOW)
    assert first[1] is True and again == (first[0], False), (first, again)
    print(f"  [OK] 同一个人第二次写：撞唯一约束，返回旧行 id={first[0]} 和 False")

    got = contacts_db.person_lookups_for(cur, [lead])
    assert [saved.lookup for saved in got[lead]] == [lookup]
    print("  [OK] 读回来只有一行，字段原样")
    return first[0]


def verify_contacts(cur, *, lead: int, run_id: int, person_lookup_id: int) -> None:
    """地址按小写去重；两个来源的同一个地址各算一行；两条 CHECK 挡得住。"""
    site = FoundContact(
        channel=ContactChannel.EMAIL,
        value="Contact@Hushly.example",
        source=ContactSource.WEBSITE,
        source_url="https://home.hushly.example/",
        email_status=None,
        catch_all=None,
        found_in_google=None,
        quality_score=None,
    )
    assert contacts_db.insert_contact(cur, lead, site, person_lookup_id=None, run_id=run_id, found_at=NOW)

    lower = FoundContact(**{**site.__dict__, "value": "contact@hushly.example"})
    added = contacts_db.insert_contact(cur, lead, lower, person_lookup_id=None, run_id=run_id, found_at=NOW)
    assert added is False
    print("  [OK] 同一个地址只差大小写：on conflict 认出了表达式唯一索引，没写第二行")

    searched = FoundContact(
        channel=ContactChannel.EMAIL,
        value="contact@hushly.example",
        source=ContactSource.LINKEDIN_EMAIL_SEARCH,
        source_url="https://www.linkedin.com/in/verify-contacts-sam/",
        email_status="risky",
        catch_all=True,
        found_in_google=True,
        quality_score=75,
    )
    later = NOW + dt.timedelta(seconds=1)
    assert contacts_db.insert_contact(
        cur, lead, searched, person_lookup_id=person_lookup_id, run_id=run_id, found_at=later
    )
    got = contacts_db.contacts_for(cur, [lead])[lead]
    assert [c.contact.source for c in got] == [ContactSource.LINKEDIN_EMAIL_SEARCH, ContactSource.WEBSITE]
    assert got[1].contact.value == "Contact@Hushly.example"
    print("  [OK] 同一个地址两个来源各一行，最新的在前；地址按原样存（Contact@Hushly.example）")

    def write_without_person(contact: FoundContact) -> bool:
        return contacts_db.insert_contact(
            cur, lead, contact, person_lookup_id=None, run_id=run_id, found_at=NOW
        )

    no_person = FoundContact(**{**searched.__dict__, "value": "other@hushly.example"})
    expect_error(cur, "个人主页来的邮箱没带 person_lookup_id", lambda: write_without_person(no_person))
    empty = FoundContact(**{**site.__dict__, "value": ""})
    expect_error(cur, "地址是空串", lambda: write_without_person(empty))


def verify_spend(cur, *, lead: int, run_id: int) -> None:
    """三笔小钱加起来一分不差。

    只看这几笔：库里可能已经有真实的花费行，所以拿「加之前」和「加之后」的差来比
    """
    since = NOW - dt.timedelta(seconds=5)
    before = contacts_db.spend_since(cur, since=since)
    for cost in ("0.00405", "0.01", "0.00005"):
        contacts_db.record_spend(
            cur,
            actor="harvestapi/linkedin-company",
            purpose=SpendPurpose.COMPANY_PROFILE,
            item_count=1,
            cost_usd=Decimal(cost),
            run_id=run_id,
            lead_id=lead,
            called_at=NOW,
        )
    after = contacts_db.spend_since(cur, since=since)
    assert isinstance(after, Decimal) and after - before == Decimal("0.01410"), (before, after)
    print(f"  [OK] 0.00405 + 0.01 + 0.00005 = {after - before}，Decimal 精确，没有浮点尾巴")


def verify_domain(cur, *, free: int, owner: int) -> None:
    """三种结果都走一遍，最后一种要在撞唯一索引之前认出来。"""
    domain = f"{TEST_PREFIX}hushly.example"

    got = contacts_db.set_domain_if_empty(cur, owner, domain)
    assert got.outcome == "written", got
    print(f"  [OK] 空着的那一格写上了：lead {owner} -> {domain}")

    got = contacts_db.set_domain_if_empty(cur, owner, f"{TEST_PREFIX}orboaudio.example")
    assert got.outcome == "kept", got
    cur.execute("select domain from leads where id = %s", (owner,))
    assert cur.fetchone()[0] == domain
    print("  [OK] 已经有值的不覆盖")

    got = contacts_db.set_domain_if_empty(cur, free, domain)
    assert (got.outcome, got.other_lead_id) == ("taken", owner), got
    print(f"  [OK] 这个域名已经是 lead {owner} 的：没写，没撞唯一索引，把 {owner} 交回来")


def verify_delete(cur, *, lead: int) -> None:
    """删掉一家公司：概况、查人、联系方式跟着删干净，花的钱那几行留着、lead_id 变空。

    这一段焊的是 0011 里 lead_contacts.person_lookup_id 用 cascade 不用 set null 那一条：
    set null 的话，删公司时连带删查人那一行，会去把联系方式的 person_lookup_id 改成空，
    撞上「个人主页来的邮箱必须带 person_lookup_id」那条 CHECK，整句删除失败。

    在 savepoint 里删，删完退回去：后面还要数测试造的 lead 还剩几行
    """
    cur.execute("savepoint verify_delete")
    cur.execute("delete from leads where id = %s", (lead,))
    for table in ("company_profiles", "person_lookups", "lead_contacts"):
        cur.execute(f"select count(*) from {table} where lead_id = %s", (lead,))
        assert cur.fetchone()[0] == 0, table
    cur.execute(
        "select count(*) from actor_spend where lead_id is null and actor = 'harvestapi/linkedin-company'"
        " and called_at = %s",
        (NOW,),
    )
    assert cur.fetchone()[0] >= 3
    cur.execute("rollback to savepoint verify_delete")
    print("  [OK] 删公司：概况、查人、联系方式跟着删了，没撞 CHECK；花的钱留着、lead_id 变空")


def main() -> int:
    """造数据、跑六段验证、回滚。

    输入：命令行参数（只有 --commit）
    输出：进程退出码，全过就是 0
    """
    commit = "--commit" in sys.argv

    with db.connect() as conn, conn.cursor() as cur:
        run_id, leads = seed(cur)
        print(f"造了 run={run_id} 和 {len(leads)} 行 lead：{leads}\n")

        print("── company_profiles ─────────────────────────────────────")
        verify_profiles(cur, lead=leads[0], run_id=run_id)

        print("\n── person_lookups ───────────────────────────────────────")
        person_lookup_id = verify_person_lookups(cur, lead=leads[0], run_id=run_id)

        print("\n── lead_contacts ────────────────────────────────────────")
        verify_contacts(cur, lead=leads[0], run_id=run_id, person_lookup_id=person_lookup_id)

        print("\n── actor_spend ──────────────────────────────────────────")
        verify_spend(cur, lead=leads[0], run_id=run_id)

        print("\n── leads.domain ─────────────────────────────────────────")
        verify_domain(cur, free=leads[1], owner=leads[2])

        print("\n── 删公司 ───────────────────────────────────────────────")
        verify_delete(cur, lead=leads[0])

        if commit:
            conn.commit()
            print("\n已提交。那几条假公司留在库里了，记得清掉。")
        else:
            conn.rollback()
            cur.execute("select count(*) from leads where identity_value like %s", (f"{TEST_PREFIX}%",))
            print(f"\n已回滚，测试造的 lead 现在 {cur.fetchone()[0]} 行")

    return 0


if __name__ == "__main__":
    sys.exit(main())
