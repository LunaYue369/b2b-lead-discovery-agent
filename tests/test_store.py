"""PostgresDiscoveryStore：它跟协议对不对得上，以及事务的边界。

这个类不写 SQL，所以这里不验 SQL。验的是三件靠肉眼看不出来的事：
签名跟 DiscoveryStore 逐字一致、run_id 真的被带进了每一次写入、
以及事务只有 finish 一个出口。

签名那条尤其值得焊住：Protocol 的 isinstance 只查方法名在不在，不查参数。
pipeline 那边照协议调，这边少一个关键字参数的话，要等真跑起来才炸，
而那是每天七点定时任务里的事。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from models import DiscoveryHit, ExecutionCounters, ExecutionStatus, LeadStatus
from persistence.base import DiscoveryStore
from persistence.store import PostgresDiscoveryStore

NOW = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeConn:
    """只提供 store 用到的那三件：给游标、提交、回滚。"""

    def __init__(self, *, autocommit: bool = False) -> None:
        self.autocommit = autocommit
        self.cursors: list[FakeCursor] = []
        self.calls: list[str] = []

    def cursor(self) -> FakeCursor:
        cur = FakeCursor()
        self.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.calls.append("commit")

    def rollback(self) -> None:
        self.calls.append("rollback")


def _store() -> tuple[PostgresDiscoveryStore, FakeConn]:
    conn = FakeConn()
    return PostgresDiscoveryStore(conn, run_id=77), conn


# ─────────────────────────────────────────────── 跟协议对得上


def _protocol_methods() -> list[str]:
    return [
        name
        for name, value in vars(DiscoveryStore).items()
        if not name.startswith("_") and inspect.isfunction(value)
    ]


def test_the_store_implements_every_method_with_the_same_signature():
    """协议里每个方法都要有，而且签名逐字一样。

    isinstance 查不出签名差异：少一个关键字参数、把 keyword-only 写成
    位置参数，它照样说「实现了」。而 pipeline 是照协议调的。
    """
    names = _protocol_methods()
    assert len(names) == 10, f"协议方法数变了：{names}"

    for name in names:
        assert hasattr(PostgresDiscoveryStore, name), f"store 少了 {name}"
        expected = inspect.signature(getattr(DiscoveryStore, name))
        actual = inspect.signature(getattr(PostgresDiscoveryStore, name))
        assert str(actual) == str(expected), f"{name} 的签名跟协议对不上"


def test_run_id_is_an_attribute_not_a_parameter():
    """run_id 挂在实例上。每个方法都收一遍的话，传错另一次 run 的 id
    就成了一种可能，而那种错落库之后完全看不出来。"""
    store, _ = _store()
    assert store.run_id == 77
    assert "run_id" not in inspect.signature(PostgresDiscoveryStore.insert_signals).parameters


# ─────────────────────────────────────────────── 事务只有一个出口


def test_autocommit_connection_is_refused_at_construction():
    """autocommit 下每条语句自己提交，正是这一层要避免的提交一半。

    构造时就炸，不留到 finish —— 那时候数据已经一条条提交完了，
    回滚没有任何东西可回。
    """
    with pytest.raises(ValueError, match="autocommit"):
        PostgresDiscoveryStore(FakeConn(autocommit=True), run_id=77)


def test_finish_commits_or_rolls_back_and_closes_the_cursor():
    store, conn = _store()
    store.finish(commit=True)
    assert conn.calls == ["commit"]
    assert conn.cursors[0].closed

    store2, conn2 = _store()
    store2.finish(commit=False)
    assert conn2.calls == ["rollback"]
    assert conn2.cursors[0].closed


def test_writing_after_finish_raises():
    """收尾之后再写要炸。

    psycopg 在提交之后遇到下一条语句会安静地开一个新事务，那几行写入
    变成一次没人管的孤儿事务 —— 库里少了几行，而代码路径看起来走完了。
    """
    store, _ = _store()
    store.finish(commit=True)

    with pytest.raises(RuntimeError, match="已经收尾"):
        store.insert_signals([])
    with pytest.raises(RuntimeError, match="已经收尾"):
        store.record_discovery_hits([])
    with pytest.raises(RuntimeError, match="已经收尾"):
        store.finish(commit=True)


def test_a_failing_commit_still_leaves_the_store_closed():
    """提交自己抛的时候，异常照样往外走，而这个 store 不能再接着写。"""
    conn = FakeConn()

    def boom() -> None:
        raise RuntimeError("connection lost")

    conn.commit = boom  # type: ignore[method-assign]
    store = PostgresDiscoveryStore(conn, run_id=77)

    with pytest.raises(RuntimeError, match="connection lost"):
        store.finish(commit=True)

    assert conn.cursors[0].closed
    with pytest.raises(RuntimeError, match="已经收尾"):
        store.mark_ran(30, ran_at=NOW)


# ─────────────────────────────────────────────── run_id 真的带进去了


def test_every_write_carries_the_bound_run_id(monkeypatch):
    """三个要 run_id 的写入都必须带上实例上那个，不是别的数。

    转发这种代码最容易出的错是复制粘贴之后忘了改一个参数，而那种错的
    症状是一批信号挂在别人的跑批下面 —— 两跑的报表看起来都还正常。
    """
    seen: dict[str, int] = {}

    def fake_start_execution(cur, *, run_id, query_id):
        seen["start_execution"] = run_id
        return 1

    def fake_insert_signals(cur, signals, *, run_id):
        seen["insert_signals"] = run_id
        return {}

    def fake_save_clusters(cur, clusters, *, run_id, signal_ids):
        seen["save_clusters"] = run_id
        return ()

    def fake_record_hits(cur, hits, *, run_id):
        seen["record_discovery_hits"] = run_id

    monkeypatch.setattr("persistence.store.discovery_db.start_execution", fake_start_execution)
    monkeypatch.setattr("persistence.store.discovery_db.insert_signals", fake_insert_signals)
    monkeypatch.setattr("persistence.store.discovery_db.record_discovery_hits", fake_record_hits)
    monkeypatch.setattr("persistence.store.lead_db.save_clusters", fake_save_clusters)

    store, _ = _store()
    store.start_execution(query_id=30)
    store.insert_signals([])
    store.save_clusters([], signal_ids={})
    store.record_discovery_hits([DiscoveryHit(query_id=30, lead_id=501, novel=True)])

    assert seen == {
        "start_execution": 77,
        "insert_signals": 77,
        "save_clusters": 77,
        "record_discovery_hits": 77,
    }


def test_reads_and_per_lead_writes_do_not_take_a_run_id(monkeypatch):
    """读和按公司写的那几个不带 run_id：它们问的是库的现状，跟哪一跑无关。

    lead_states 连 _guard 都不走 —— 它是只读的，收尾之后再读虽然没有用，
    但也不会写出孤儿事务，没有理由为它多一条限制。
    """
    calls: list[str] = []

    def fake_seen(cur, *, days):
        calls.append(f"seen:{days}")
        return frozenset()

    def fake_states(cur, lead_ids):
        calls.append(f"states:{list(lead_ids)}")
        return {}

    def fake_mark_ran(cur, query_id, *, ran_at):
        calls.append(f"mark_ran:{query_id}")

    def fake_finish_execution(cur, execution_id, *, status, counters, error=None):
        calls.append(f"finish_execution:{execution_id}:{status}")

    monkeypatch.setattr("persistence.store.discovery_db.seen_content_hashes", fake_seen)
    monkeypatch.setattr("persistence.store.lead_db.lead_states", fake_states)
    monkeypatch.setattr("persistence.store.query_db.mark_ran", fake_mark_ran)
    monkeypatch.setattr("persistence.store.discovery_db.finish_execution", fake_finish_execution)

    store, _ = _store()
    assert store.seen_content_hashes(days=10) == frozenset()
    assert store.lead_states([501, 502]) == {}
    store.mark_ran(30, ran_at=NOW)
    store.finish_execution(
        1, status=ExecutionStatus.COMPLETED, counters=ExecutionCounters(), error=None
    )

    assert calls == [
        "seen:10",
        "states:[501, 502]",
        "mark_ran:30",
        "finish_execution:1:completed",
    ]


def test_save_assessment_forwards_every_field(monkeypatch):
    """七维分之外那四个参数（状态、冷却时刻、退避次数、分析时刻）一个都不能漏，
    run_id 由 store 自己带上（这个 store 绑的是 run 77）。"""
    got: dict = {}

    def fake_save(cur, lead_id, **kwargs):
        got["lead_id"] = lead_id
        got.update(kwargs)

    monkeypatch.setattr("persistence.store.lead_db.save_assessment", fake_save)

    store, _ = _store()
    store.save_assessment(
        501,
        assessment="placeholder",  # type: ignore[arg-type]
        status=LeadStatus.NEW,
        cooldown_until=NOW,
        strikes=2,
        analyzed_at=NOW,
    )

    assert got == {
        "lead_id": 501,
        "assessment": "placeholder",
        "status": LeadStatus.NEW,
        "cooldown_until": NOW,
        "strikes": 2,
        "analyzed_at": NOW,
        "run_id": 77,
    }
