"""contacts_db 的测试：0011 那四张表的读写，加上往 leads.domain 补域名。

不连真库。真库那一半（SQL 在 Postgres 里跑不跑得通、唯一索引和 CHECK 挡不挡得住）
在 scripts/verify_contacts_db.py，要 0011 已经上了库才能跑。

这里焊住的是四种不报错但写错的：

    列和值错位          insert 的列名顺序跟参数顺序对不上，数对上了、值写进了别的列。
                        假游标把列名从 SQL 里拆出来跟参数一一配对，按列名断言
    撞了唯一约束当成新写  重跑时查过的人被当成第一次查，调用方会以为又花了一次钱
    花钱传了 float      加总之后跟上限比大小差一点点判错
    域名覆盖            已经有值的 leads.domain 被这次抓到的跳转域名盖掉
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest

from models import (
    CompanyProfile,
    ContactChannel,
    ContactSource,
    FoundContact,
    PersonLookup,
    SpendPurpose,
)
from persistence import contacts_db

NOW = dt.datetime(2026, 9, 27, 14, tzinfo=dt.UTC)


class FakeCursor:
    """记下每条 SQL，insert 按列名拆成 dict。查询的结果由测试预先排好。

    输入：results，每次 execute 之后 fetchone/fetchall 依次拿到的结果，如
          [[(7,)], []] 表示第一条 SQL 返回一行 (7,)、第二条一行都没有
    记下：
        inserts   [(表名, {列名: 值})]，列名是从 SQL 里拆出来的，所以错位一定看得出来
        sqls      每条 SQL 压成一行，给断言「发了什么」用
    """

    def __init__(self, results: list[list[tuple]] | None = None, rowcounts: list[int] | None = None):
        self._results = list(results or [])
        self._rowcounts = list(rowcounts or [])
        self._current: list[tuple] = []
        self.rowcount = 0
        self.inserts: list[tuple[str, dict]] = []
        self.sqls: list[str] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(sql.split())
        self.sqls.append(text)

        # 输入   "insert into lead_contacts (lead_id, run_id, ...) values (%s, %s, ...)"
        # 输出   inserts 多一条 ("lead_contacts", {"lead_id": 1388, "run_id": 45, ...})
        match = re.match(r"insert into (\w+) \(([^)]*)\) values \(([^)]*)\)", text)
        if match:
            columns = [c.strip() for c in match.group(2).split(",")]
            placeholders = match.group(3).count("%s")
            assert len(columns) == placeholders == len(params), (
                f"{match.group(1)}：列 {len(columns)} 个、占位符 {placeholders} 个、参数 {len(params)} 个"
            )
            self.inserts.append((match.group(1), dict(zip(columns, params, strict=True))))

        self._current = self._results.pop(0) if self._results else []
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else len(self._current)

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)


def _profile() -> CompanyProfile:
    return CompanyProfile(
        website="http://www.hushly.example",
        employee_count=11,
        employee_count_range="2-10",
        founded_year=2022,
        location="United States",
        industries=("Music",),
        phone=None,
        raw={"name": "Hushly"},
    )


def _lookup(matches: bool = True) -> PersonLookup:
    return PersonLookup(
        person_linkedin_url="https://www.linkedin.com/in/samhadid/",
        person_name="sam hadid",
        headline="ceo @ Hushly - a sanctuary for human sound",
        current_company_name="Hushly",
        current_company_linkedin_url="https://www.linkedin.com/company/hushly/",
        matches_lead=matches,
        raw={"firstName": "sam"},
    )


def _email_search_contact() -> FoundContact:
    return FoundContact(
        channel=ContactChannel.EMAIL,
        value="sam@hushly.example",
        source=ContactSource.LINKEDIN_EMAIL_SEARCH,
        source_url="https://www.linkedin.com/in/samhadid/",
        email_status="risky",
        catch_all=True,
        found_in_google=True,
        quality_score=75,
    )


# ── 写 ────────────────────────────────────────────────────────────────────────


def test_a_profile_lands_in_the_right_columns():
    """概况六格、raw、行业数组按列名落位；行业转成 list，raw 转成 json 字符串。"""
    cur = FakeCursor(results=[[(3,)]])

    row_id = contacts_db.insert_company_profile(cur, 1388, _profile(), run_id=45, fetched_at=NOW)

    assert row_id == 3
    table, row = cur.inserts[0]
    assert table == "company_profiles"
    assert row["lead_id"] == 1388 and row["run_id"] == 45
    assert row["website"] == "http://www.hushly.example"
    assert (row["employee_count"], row["employee_count_range"]) == (11, "2-10")
    assert (row["founded_year"], row["location"], row["phone"]) == (2022, "United States", None)
    # tuple 进 text[] 会被 psycopg 当成一条记录发出去报错，必须是 list
    assert row["industries"] == ["Music"]
    assert row["raw"] == '{"name": "Hushly"}'
    assert row["fetched_at"] == NOW


def test_a_new_person_lookup_is_written_and_says_so():
    """第一次查这个人：写进去，返回 (id, True)，不再发第二条 SQL。"""
    cur = FakeCursor(results=[[(7,)]])

    got = contacts_db.insert_person_lookup(cur, 1388, _lookup(), signal_id=5120, run_id=45, fetched_at=NOW)

    assert got == (7, True)
    assert len(cur.sqls) == 1
    _, row = cur.inserts[0]
    assert row["person_linkedin_url"] == "https://www.linkedin.com/in/samhadid/"
    assert row["headline"] == "ceo @ Hushly - a sanctuary for human sound"
    assert row["current_company_linkedin_url"] == "https://www.linkedin.com/company/hushly/"
    assert (row["matches_lead"], row["signal_id"]) == (True, 5120)


def test_a_repeated_person_lookup_returns_the_old_row_and_says_it_was_not_new():
    """同一家、同一个人第二次写：撞唯一约束，返回旧行的 id 和 False。

    这条红了的症状：崩溃之后重跑，调用方以为这个人是第一次查，账上看着像又花了 $0.01
    """
    # 第一条 insert 撞了、do nothing 不返回行；第二条 select 查回旧行 id=7
    cur = FakeCursor(results=[[], [(7,)]])

    got = contacts_db.insert_person_lookup(cur, 1388, _lookup(), signal_id=5120, run_id=46, fetched_at=NOW)

    assert got == (7, False)
    assert cur.sqls[1].startswith("select id from person_lookups")


def test_a_contact_from_email_search_keeps_all_four_confidence_columns():
    """个人主页找来的邮箱：四样可信度一样不少地落位，枚举写成库里的字符串。"""
    cur = FakeCursor(rowcounts=[1])

    added = contacts_db.insert_contact(
        cur, 1388, _email_search_contact(), person_lookup_id=7, run_id=45, found_at=NOW
    )

    assert added is True
    _, row = cur.inserts[0]
    assert (row["channel"], row["source"]) == ("email", "linkedin_email_search")
    assert row["person_lookup_id"] == 7
    assert (row["email_status"], row["catch_all"], row["found_in_google"], row["quality_score"]) == (
        "risky",
        True,
        True,
        75,
    )


def test_a_contact_already_there_is_reported_as_not_added():
    """同一个地址再写一次：on conflict 跳过，rowcount 是 0，返回 False。"""
    cur = FakeCursor(rowcounts=[0])

    added = contacts_db.insert_contact(
        cur, 1388, _email_search_contact(), person_lookup_id=7, run_id=46, found_at=NOW
    )

    assert added is False
    assert "on conflict (lead_id, channel, lower(value), source) do nothing" in cur.sqls[0]


def test_spend_goes_in_as_decimal():
    """花的钱原样以 Decimal 进库，purpose 写成库里的字符串。"""
    cur = FakeCursor()

    contacts_db.record_spend(
        cur,
        actor="harvestapi/linkedin-company",
        purpose=SpendPurpose.COMPANY_PROFILE,
        item_count=1,
        cost_usd=Decimal("0.00405"),
        run_id=45,
        lead_id=1388,
        called_at=NOW,
    )

    _, row = cur.inserts[0]
    assert row["cost_usd"] == Decimal("0.00405") and isinstance(row["cost_usd"], Decimal)
    assert (row["actor"], row["purpose"], row["item_count"]) == (
        "harvestapi/linkedin-company",
        "company_profile",
        1,
    )


def test_spend_passed_as_float_is_refused():
    """传 float 当场炸，一条 SQL 都不发。"""
    cur = FakeCursor()

    with pytest.raises(TypeError, match="Decimal"):
        contacts_db.record_spend(
            cur,
            actor="harvestapi/linkedin-company",
            purpose=SpendPurpose.COMPANY_PROFILE,
            item_count=1,
            cost_usd=0.004,
            run_id=45,
            lead_id=1388,
            called_at=NOW,
        )
    assert cur.sqls == []


def test_spend_since_with_nothing_spent_is_zero_decimal():
    """这个账期一次都没调过：拿到 Decimal 0，不是 None 也不是 int。"""
    cur = FakeCursor(results=[[(0,)]])

    total = contacts_db.spend_since(cur, since=NOW)

    assert total == Decimal("0") and isinstance(total, Decimal)
    # 两条路都算：SQL 里不按 run_type 筛、不 join runs
    assert "runs" not in cur.sqls[0]


# ── 读 ────────────────────────────────────────────────────────────────────────


def test_reading_nothing_sends_no_sql():
    """一批空的 lead_id：三个读函数都直接给 {}，不发 SQL（any('{}') 虽然能跑，但白查一次）。"""
    cur = FakeCursor()

    assert contacts_db.latest_profiles(cur, []) == {}
    assert contacts_db.person_lookups_for(cur, []) == {}
    assert contacts_db.contacts_for(cur, []) == {}
    assert cur.sqls == []


def test_latest_profiles_turns_rows_back_into_profiles():
    """查回来的一行变回 CompanyProfile：行业 list 转 tuple，库里 null 的行业给 ()。"""
    cur = FakeCursor(
        results=[
            [
                (
                    1388,
                    "http://www.hushly.example",
                    11,
                    "2-10",
                    2022,
                    "United States",
                    ["Music"],
                    None,
                    {"name": "Hushly"},
                ),
                (1355, None, None, None, None, None, None, None, {}),
            ]
        ]
    )

    got = contacts_db.latest_profiles(cur, [1355, 1388, 1388])

    assert got[1388] == _profile()
    assert got[1355].industries == ()
    # 每家只取一行、最新的那一行，是 SQL 自己做的
    assert "distinct on (lead_id)" in cur.sqls[0]
    assert "order by lead_id, fetched_at desc, id desc" in cur.sqls[0]


def test_contacts_for_keeps_the_order_the_database_gives_and_turns_enums_back():
    """联系方式按库给的顺序（最新的在前）分到每家，channel / source 转回枚举。"""
    cur = FakeCursor(
        results=[
            [
                (
                    13,
                    1388,
                    7,
                    "email",
                    "sam@hushly.example",
                    "linkedin_email_search",
                    "https://www.linkedin.com/in/samhadid/",
                    "risky",
                    True,
                    True,
                    75,
                    NOW,
                ),
                (
                    12,
                    1388,
                    None,
                    "email",
                    "contact@hushly.example",
                    "website",
                    "https://home.hushly.example/",
                    None,
                    None,
                    None,
                    None,
                    NOW,
                ),
            ]
        ]
    )

    got = contacts_db.contacts_for(cur, [1388])

    assert [c.id for c in got[1388]] == [13, 12]
    assert got[1388][0].contact == _email_search_contact()
    assert got[1388][1].contact.source is ContactSource.WEBSITE
    assert got[1388][1].person_lookup_id is None
    assert "order by lead_id, found_at desc, id desc" in cur.sqls[0]


def test_a_source_the_code_does_not_know_blows_up_on_read():
    """库里的来源值不认识：读的时候当场 ValueError，不带着一个拼错的来源走到卡片上。"""
    cur = FakeCursor(
        results=[[(12, 1388, None, "email", "a@b.com", "webiste", None, None, None, None, None, NOW)]]
    )

    with pytest.raises(ValueError):
        contacts_db.contacts_for(cur, [1388])


# ── 补 leads.domain ──────────────────────────────────────────────────────────


def test_domain_is_written_when_empty_and_free():
    """那一格空着、没人占：写进去。update 里带着 domain is null 再挡一次。"""
    cur = FakeCursor(results=[[(None,)], [], []], rowcounts=[1, 0, 1])

    got = contacts_db.set_domain_if_empty(cur, 1388, "hushly.example")

    assert (got.outcome, got.other_lead_id) == ("written", None)
    assert cur.sqls[2].startswith("update leads set domain")
    assert "domain is null" in cur.sqls[2]


def test_a_domain_already_set_is_never_overwritten():
    """那一格已经有值：不管跟这次的一不一样都不动，只发一条 select。"""
    cur = FakeCursor(results=[[("orbobrand.example",)]])

    got = contacts_db.set_domain_if_empty(cur, 1388, "orboaudio.example")

    assert got.outcome == "kept"
    assert len(cur.sqls) == 1


def test_a_domain_owned_by_another_lead_is_reported_not_written():
    """域名已经在另一家那一行上：不写，把那一家的 id 交回去等人工判是不是同一家。"""
    cur = FakeCursor(results=[[(None,)], [(1288,)]])

    got = contacts_db.set_domain_if_empty(cur, 1388, "hushly.example")

    assert (got.outcome, got.other_lead_id) == ("taken", 1288)
    assert not any(sql.startswith("update") for sql in cur.sqls)


@pytest.mark.parametrize("raw", ["Hushly.example", "https://hushly.example", "www.hushly.example", "hushly.example/about"])
def test_a_domain_that_is_not_normalized_is_refused(raw):
    """没归一化的域名当场炸、一条 SQL 都不发：Hushly.example 和 hushly.example 唯一索引分不出来。"""
    cur = FakeCursor()

    with pytest.raises(ValueError, match="归一化"):
        contacts_db.set_domain_if_empty(cur, 1388, raw)
    assert cur.sqls == []
