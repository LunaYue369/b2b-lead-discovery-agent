"""拿真库验 lead_feedback 那四个函数。默认整跑回滚，一行不留。

    python -m scripts.verify_feedback            跑一遍然后回滚
    python -m scripts.verify_feedback --commit   留下数据（基本不需要）

为什么这一段必须在真库上跑、不写成单元测试：这四个函数的全部内容就是
SQL 语义 —— append-only 允许一 lead 多行、insert ... select 里那句
not exists、pushed_at 为空的不许被扫、update 改到 0 行时怎么分辨两种
原因。假游标只验得出「SQL 字符串长什么样」，验不了这些。
这跟 test_lead_db 用假游标不冲突：那边测的是身份归并的判断逻辑
（哪一档命中、要不要升档），判断可以脱离库；这里测的是库的行为。

跟 verify_persistence.py 分开是因为那个脚本要求 leads 表是空的
（它要对账整条漏斗的数字，库里有别的行就解释不清）。而第一次真跑之后
leads 表永远不空了。这个脚本自己造几行 lead、验完回滚，所以什么时候
都能跑。
"""

from __future__ import annotations

import datetime as dt
import sys

from models import FeedbackType
from persistence import db, feedback_db

# 造出来的 lead 都用这个前缀，万一 --commit 了也认得出来是谁留下的。
TEST_PREFIX = "verify-feedback-"


def seed_run(cur) -> int:
    """插一行 runs，返回 run_id。

    输入：游标
    输出：新那行的 id

    不用 discovery_db.insert_run：那个函数带着自己的语义（先记 running
    再收尾），而这里只需要一个能被外键指向的 run。
    """
    cur.execute(
        """
        insert into runs (run_type, status, run_date)
        values ('scheduled', 'running', %s)
        returning id
        """,
        (dt.date.today().isoformat(),),
    )
    return int(cur.fetchone()[0])


def seed_leads(cur, count: int) -> list[int]:
    """插 count 行最简单的 lead，返回它们的 id。

    输入：游标；要几行
    输出：id 列表，顺序跟插入顺序一致

    只填四个必填列，别的走默认值（status 默认 new、score 空）。
    这些行不经过 lead_db 的身份归并 —— 这个脚本验的是 feedback，
    公司是怎么进库的不在范围内。
    """
    ids = []
    for i in range(count):
        name = f"{TEST_PREFIX}{i}"
        cur.execute(
            """
            insert into leads (identity_kind, identity_value, normalized_name, display_name)
            values ('name', %s, %s, %s)
            returning id
            """,
            (name, name, f"测试公司 {i}"),
        )
        ids.append(int(cur.fetchone()[0]))
    return ids


def verify_append_only(cur, *, lead: int, run_id: int) -> None:
    """同一条 lead 写两行，第一行不许消失。

    输入：游标；哪条 lead；哪一跑
    输出：无。对不上就 assert 炸

    这是这张表最重要的一条性质：Riley approve 之后改稿改到一半反悔，
    要能写第二行。设计成 1 lead 1 行的话，「她当时是 approve 的」
    这个信号就没了，而那正是 analyzer 最该学的一类样本。
    """
    first = feedback_db.record_feedback(
        cur, lead, feedback_type=FeedbackType.APPROVE, author="U_TEST", run_id=run_id
    )
    second = feedback_db.record_feedback(
        cur,
        lead,
        feedback_type=FeedbackType.REJECT,
        author="U_TEST",
        run_id=run_id,
        note="改稿改到一半发现众筹失败了",
    )
    assert second > first, "第二行的 id 该更大"

    cur.execute("select count(*) from lead_feedback where lead_id = %s", (lead,))
    assert cur.fetchone()[0] == 2, "append-only：两行都该在"
    assert feedback_db.current_feedback(cur, lead) is FeedbackType.REJECT
    print(f"  [OK] append-only：lead={lead} 两行都在，当前态度取最新那行 reject")


def verify_no_feedback_is_not_ignored(cur, *, lead: int) -> None:
    """一行反馈都没有要给 None，不能给 IGNORED。

    输入：游标；哪条 lead
    输出：无

    两者不是一回事：None 是「还在等她」，IGNORED 是「等超时了，
    已经记成弱负信号」。sweep_ignored 靠这个区别决定扫谁。
    """
    assert feedback_db.current_feedback(cur, lead) is None
    print(f"  [OK] lead={lead} 没有反馈行 -> None，跟 ignored 不是一回事")


def verify_advance_to_draft(cur, *, lead: int, excluded: int) -> None:
    """推进 in_draft 的四种情况。

    输入：游标；一条可以推进的 lead；一条终态的 lead
    输出：无

    四种：正常推进 / 重复点击不炸 / lead 不存在要炸 / 终态不许复活。
    """
    # 造一个「已推送、冷却期在 30 天后」的状态，那就是真跑里推给她时的样子
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
    cur.execute(
        "update leads set status = 'pushed', cooldown_until = %s where id = %s", (later, lead)
    )
    feedback_db.advance_to_draft(cur, lead)

    cur.execute("select status, cooldown_until from leads where id = %s", (lead,))
    status, cooldown = cur.fetchone()
    assert status == "in_draft", status
    # 冷却期不许被清掉。清掉的话 lead_states 会判它「没在冷却」，
    # 明天那一跑就花一次 LLM 的钱重新分析它 —— 跟想要的效果正好相反。
    assert cooldown is not None, "cooldown_until 被清空了，这条 lead 明天就会被重新分析"
    assert abs((cooldown - later).total_seconds()) < 1, "cooldown_until 该原样不动"

    # 再点一次：Slack 的按钮回调本身就可能重投，重复点击是正常操作
    feedback_db.advance_to_draft(cur, lead)
    print(f"  [OK] lead={lead} pushed -> in_draft，冷却期原样保留，重复点击不炸")

    try:
        feedback_db.advance_to_draft(cur, 999999999)
    except RuntimeError as err:
        print(f"  [OK] 不存在的 lead 被拒：{err}")
    else:
        raise AssertionError("不存在的 lead 该抛 RuntimeError")

    cur.execute("update leads set status = 'excluded' where id = %s", (excluded,))
    try:
        feedback_db.advance_to_draft(cur, excluded)
    except RuntimeError as err:
        print(f"  [OK] excluded 的 lead 被拒：{err}")
    else:
        raise AssertionError("excluded 该抛 RuntimeError，一次点击不该让终态复活")


def verify_sweep(cur, *, run_id: int, old: int, fresh: int, never: int, answered: int) -> None:
    """ignored 扫描的四种情况，加一次幂等。

    输入：游标；哪一跑；四条各代表一种情况的 lead
    输出：无

    四条分别是：推出去很久没人动（该扫）/ 刚推出去（不该）/
    从来没推给她过（不该，这条最容易写错）/ 已经有反馈了（不该）。
    """
    now = dt.datetime.now(dt.UTC)
    cur.execute(
        "update leads set pushed_at = %s, pushed_run_id = %s where id = %s",
        (now - dt.timedelta(days=30), run_id, old),
    )
    cur.execute(
        "update leads set pushed_at = %s, pushed_run_id = %s where id = %s",
        (now - dt.timedelta(days=1), run_id, fresh),
    )
    cur.execute("update leads set pushed_at = null, pushed_run_id = null where id = %s", (never,))
    cur.execute(
        "update leads set pushed_at = %s, pushed_run_id = %s where id = %s",
        (now - dt.timedelta(days=30), run_id, answered),
    )

    swept = feedback_db.sweep_ignored(cur, now=now, after_days=7)
    assert old in swept, f"推出去 30 天没人动的该被扫，swept={swept}"
    assert fresh not in swept, "刚推出去的不该被扫"
    assert never not in swept, "从来没推给她过的不该被扫，那会污染 yield 的分母"
    assert answered not in swept, "已经有反馈行的不该被扫"
    print(f"  [OK] sweep 扫了 {len(swept)} 条：超时的进，新推的 / 没推过的 / 已答的都不进")

    cur.execute(
        "select feedback_type, author, run_id, note from lead_feedback where lead_id = %s",
        (old,),
    )
    assert cur.fetchone() == ("ignored", "system", run_id, None)
    print("  [OK] ignored 那行 author=system、run_id 取的是推那张卡的那一跑、note 为空")

    again = feedback_db.sweep_ignored(cur, now=now, after_days=7)
    assert old not in again, "第二次扫不该给同一条再写一行"
    cur.execute("select count(*) from lead_feedback where lead_id = %s", (old,))
    assert cur.fetchone()[0] == 1
    print("  [OK] 再扫一次幂等：同一条只有一行 ignored")

    # 第二轮：这条 lead 三个月前被推、被扫成 ignored，冷却期过了重新分析
    # 又过线，九天前再推了一次，她又没动。这一轮的沉默必须也能扫出来 ——
    # 扫不出来的话第二轮那几条 query 的分母就少算一条，而报表上完全看不出来。
    #
    # 时间线必须摆对，不然测的不是这件事：第一行 ignored 要早于第二次推送。
    # 上面那几步是在「现在」写的行，所以这里要把它往前挪 —— 三个月前推过、
    # 三个月前扫的，然后九天前才第二次推。
    cur.execute(
        "update lead_feedback set created_at = %s where lead_id = %s",
        (now - dt.timedelta(days=90), old),
    )
    cur.execute(
        "update leads set pushed_at = %s, pushed_run_id = %s where id = %s",
        (now - dt.timedelta(days=9), run_id, old),
    )
    second_round = feedback_db.sweep_ignored(cur, now=now, after_days=7)
    assert old in second_round, "第二轮推送之后的沉默也该被扫出来"
    cur.execute("select count(*) from lead_feedback where lead_id = %s", (old,))
    assert cur.fetchone()[0] == 2
    print("  [OK] 第二轮又被推又被忽略：再写一行 ignored（按 pushed_at 之后判，不按一生判）")


def main() -> int:
    """造数据、跑五段验证、回滚。

    输入：命令行参数（只有 --commit）
    输出：进程退出码，全过就是 0

    默认回滚。跟 verify_persistence 同一条口径：这些行的价值是「验证过
    SQL 的行为」，不是「保存数据」—— 留在库里的话，那几条假公司会进
    以后的 seen 和 approval yield，而它们看起来跟真数据一模一样。
    """
    commit = "--commit" in sys.argv

    with db.connect() as conn, conn.cursor() as cur:
        run_id = seed_run(cur)
        leads = seed_leads(cur, 6)
        print(f"造了 run={run_id} 和 {len(leads)} 行 lead：{leads}\n")

        print("── append-only ──────────────────────────────────────────")
        verify_append_only(cur, lead=leads[0], run_id=run_id)
        verify_no_feedback_is_not_ignored(cur, lead=leads[1])

        print("\n── 推进 in_draft ────────────────────────────────────────")
        verify_advance_to_draft(cur, lead=leads[1], excluded=leads[2])

        print("\n── ignored 扫描 ─────────────────────────────────────────")
        verify_sweep(
            cur,
            run_id=run_id,
            old=leads[3],
            fresh=leads[4],
            never=leads[5],
            answered=leads[0],
        )

        if commit:
            conn.commit()
            print("\n已提交。那几条假公司留在库里了，记得清掉。")
        else:
            conn.rollback()
            cur.execute(
                "select count(*) from leads where identity_value like %s", (f"{TEST_PREFIX}%",)
            )
            print(f"\n已回滚，测试造的 lead 现在 {cur.fetchone()[0]} 行")

    return 0


if __name__ == "__main__":
    sys.exit(main())
