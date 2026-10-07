"""query_executions 两步走的测试：先插 running，跑完再收尾。

焊住的是拆分之后新出现的三种「不报错但账是错的」：

    拿 running 去收尾        执行永远停在空壳状态，报表上看着像「跑了很久」
    收尾一个不存在的 id      update 影响 0 行，计数悄悄丢了，函数照样返回
    状态拼错成 'complete'    静默跳过 check() 那两条自查

不连真库。这两个函数发出的 SQL 只有一插一改，假游标把 query_executions
那几列的语义照着实现一遍就够了。
"""

from __future__ import annotations

import pytest

from models import ExecutionCounters, ExecutionStatus
from persistence.discovery_db import finish_execution, start_execution


class FakeCursor:
    """够这两个函数用的最小游标。认不出来的 SQL 直接炸，不静默吞掉。"""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self._result: list[tuple] = []
        self._next_id = 31
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(sql.split())

        if text.startswith("insert into query_executions"):
            run_id, query_id = params
            row_id = self._next_id
            self._next_id += 1
            self.rows[row_id] = {
                "run_id": run_id,
                "query_id": query_id,
                "status": "running",
                "finished_at": None,
                "error": None,
                "novel_lead_count": 0,
            }
            self._result = [(row_id,)]
            self.rowcount = 1
            return

        if text.startswith("update query_executions"):
            # 逐个拆开而不是按下标取，这样哪天 update 的列变了，
            # 这里会当场炸而不是把某一列的值悄悄记到另一列上。
            (
                status,
                error,
                raw_result_count,
                billed_item_count,
                signal_dropped_count,
                candidate_dropped_count,
                drop_reasons,
                unique_company_count,
                researchable_lead_count,
                cooldown_skipped_count,
                excluded_skipped_count,
                novel_lead_count,
                qualified_lead_count,
                row_id,
            ) = params
            row = self.rows.get(row_id)
            self.rowcount = 0 if row is None else 1
            if row is not None:
                row["status"] = str(status)
                row["error"] = error
                row["finished_at"] = "now()"
                row["novel_lead_count"] = novel_lead_count
                row["qualified_lead_count"] = qualified_lead_count
                row["drop_reasons"] = drop_reasons
            return

        raise AssertionError(f"假游标不认识这条 SQL：{text[:60]}")

    def fetchone(self):
        return self._result[0] if self._result else None


def counters(**overrides) -> ExecutionCounters:
    """一份自洽的计数：三堆分流加起来正好等于公司数，drop_reasons 也对得上。"""
    base = ExecutionCounters(
        raw_result_count=191,
        billed_item_count=191,
        signal_dropped_count=65,
        candidate_dropped_count=28,
        drop_reasons={"no_relevance_signal": 65, "mention_fanout": 28},
        unique_company_count=86,
        researchable_lead_count=80,
        cooldown_skipped_count=4,
        excluded_skipped_count=2,
        novel_lead_count=70,
        qualified_lead_count=9,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_starts_as_running_with_no_counters_yet():
    """开跑时只记「这条 query 开始了」，计数一个都还不知道。

    started_at 由库里的 default now() 填，而这一刻才是这条 query 真正
    开始跑的时刻 —— 拆开两步之前它等于插入时刻，于是耗时永远是 0。
    """
    cur = FakeCursor()
    execution_id = start_execution(cur, run_id=7, query_id=2)
    assert execution_id == 31
    assert cur.rows[31]["status"] == "running"
    assert cur.rows[31]["finished_at"] is None


def test_finish_writes_status_and_counters():
    cur = FakeCursor()
    execution_id = start_execution(cur, run_id=7, query_id=2)
    finish_execution(cur, execution_id, status=ExecutionStatus.COMPLETED, counters=counters())
    assert cur.rows[31]["status"] == "completed"
    assert cur.rows[31]["finished_at"] == "now()"
    assert cur.rows[31]["novel_lead_count"] == 70


def test_failed_execution_keeps_half_counted_numbers():
    """崩在半路时分流三堆本来就凑不齐，只跑无条件那两条自查。

    拿 check() 去查它会把「采集失败」报成「计数对不上」——
    那是两件事，而报错说的是错的那件。
    """
    cur = FakeCursor()
    execution_id = start_execution(cur, run_id=7, query_id=2)
    # 采集就挂了，后面几段一步都没走到：三堆分流是 0，合格的自然也是 0。
    # 剩下的 raw / billed / 砍量是崩之前已经数出来的，照样要留在库里。
    half = counters(
        unique_company_count=0,
        researchable_lead_count=0,
        cooldown_skipped_count=0,
        excluded_skipped_count=0,
        novel_lead_count=0,
        qualified_lead_count=0,
    )
    finish_execution(
        cur, execution_id, status=ExecutionStatus.FAILED, counters=half, error="Apify 502"
    )
    assert cur.rows[31]["status"] == "failed"
    assert cur.rows[31]["error"] == "Apify 502"


def test_completed_still_runs_the_full_self_check():
    """completed 那一路四条全查：分流三堆对不上就当场炸，不进库。"""
    cur = FakeCursor()
    execution_id = start_execution(cur, run_id=7, query_id=2)
    with pytest.raises(ValueError):
        finish_execution(
            cur,
            execution_id,
            status=ExecutionStatus.COMPLETED,
            counters=counters(cooldown_skipped_count=99),
        )
    assert cur.rows[31]["status"] == "running"


def test_running_is_not_a_way_to_finish():
    """传 running 说明调用方把开始和结束搞混了。库里那条 CHECK 允许 running
    （start_execution 要用），所以只能在这一层挡。"""
    cur = FakeCursor()
    execution_id = start_execution(cur, run_id=7, query_id=2)
    with pytest.raises(ValueError):
        finish_execution(cur, execution_id, status=ExecutionStatus.RUNNING, counters=counters())


def test_typo_in_status_fails_at_the_call():
    """'complete' 少个 d：不挡的话会静默走 else 分支、少查两条自查，
    再到库里撞 CHECK，而那时的报错只有一个约束名。"""
    cur = FakeCursor()
    execution_id = start_execution(cur, run_id=7, query_id=2)
    with pytest.raises(ValueError):
        finish_execution(cur, execution_id, status="complete", counters=counters())


def test_finishing_an_unknown_id_is_an_error():
    """update 影响 0 行 = 这个 id 不存在。放过去的话这条执行的账永远是那行
    running 空壳，而报表上它看起来只是「跑了很久还没结束」。"""
    cur = FakeCursor()
    with pytest.raises(RuntimeError):
        finish_execution(cur, 999, status=ExecutionStatus.COMPLETED, counters=counters())
