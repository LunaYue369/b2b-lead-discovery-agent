"""DiscoveryStore 的真实现：把协议里那十个动作转发到 persistence 各模块。

这个类自己不写 SQL，一行都没有。SQL 在 discovery_db / lead_db / query_db
里，按表分文件；这里按「一次 run 要做的事」重新组织一遍，因为它的读者是
pipeline，而 pipeline 关心的是顺序不是表。

一个实例只服务一次 run：run_id 是实例属性，不是每个方法的参数。它同时
持有那次 run 的事务，生命周期本来就跟一次 run 一样长。每个方法都收
run_id 的话，「传错了另一次 run 的 id」就成了一种可能，而那种错落库之后
完全看不出来 —— 一批信号挂在别人的跑批下面，报表上两跑都还是正常的。

一跑一个事务，中途不提交，finish 是全部写入唯一的出口。理由在
persistence/base.py 的 finish 那段写全了，简短版：提交一半会静默丢线索，
因为信号先落库、公司要等跨 query 归并之后才写，中间崩一下就留下一批
「见过但从没变成公司」的信号，它们的 content_hash 进了跨天 seen 集合，
第二天同一批帖被当重复丢掉，指向的公司从此再也发现不了。

事务的边界比这个类大一点：runs 那一行是 trigger 在建 store 之前插的，
用的是同一个连接，所以也在同一个事务里。finish(commit=False) 会把它
一起回滚掉 —— 那是有意的，见 pipeline 里那张四种收场的表。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import psycopg

from models import (
    Assessment,
    CompanyCluster,
    DiscoveryHit,
    ExecutionCounters,
    ExecutionStatus,
    LeadState,
    LeadStatus,
    SavedLead,
    Signal,
)
from persistence import discovery_db, lead_db, query_db


class PostgresDiscoveryStore:
    """DiscoveryStore 的 Postgres 实现。

    输入：一个不带 autocommit 的连接；这一跑的 run_id
    输出：一个绑在那一跑上的 store

    不继承 DiscoveryStore：那是个 Protocol，靠方法名和签名对得上就算实现。
    不继承的好处是「协议和实现对不上」在 pipeline 那边当场就是类型错误，
    而不是在这里被一个装样子的基类吞掉。

    要求连接不是 autocommit：autocommit 下每条语句自己提交，那正是这一层
    要避免的「提交一半」。构造时当场查，不留到 finish 才发现 —— 那时候
    数据已经一条条提交完了，回滚没有任何东西可回。

    每个方法为什么都没有 docstring：它们的语义逐字写在 persistence/base.py
    的协议里，而那份才是 pipeline 的读者会去看的。在这里再抄一遍，改一处
    忘一处就会两边打架，而文档打架比没有文档更坏 —— 读的人不知道该信哪份。
    只有实现比协议多做一件事的地方才写注释：构造时拒 autocommit、
    写入前的 _guard、finish 里关游标。

    一次 run 里这十个动作按什么顺序被调，看 pipeline.run_discovery 的
    docstring，那里是按步骤列的。
    """

    def __init__(self, conn: psycopg.Connection, *, run_id: int) -> None:
        if conn.autocommit:
            raise ValueError(
                "store 要一跑一个事务，连接不能是 autocommit —— "
                "那样每条语句自己提交，中途崩掉就是提交了一半"
            )
        self._conn = conn
        self._cur = conn.cursor()
        self._finished = False
        self.run_id = run_id

    # ── 一跑开头取一次 ────────────────────────────────────────────

    def seen_content_hashes(self, *, days: int) -> frozenset[str]:
        return discovery_db.seen_content_hashes(self._cur, days=days)

    # ── 每条 query 一轮 ──────────────────────────────────────────

    def start_execution(self, *, query_id: int) -> int:
        self._guard()
        return discovery_db.start_execution(self._cur, run_id=self.run_id, query_id=query_id)

    def finish_execution(
        self,
        execution_id: int,
        *,
        status: ExecutionStatus,
        counters: ExecutionCounters,
        error: str | None = None,
    ) -> None:
        self._guard()
        discovery_db.finish_execution(
            self._cur, execution_id, status=status, counters=counters, error=error
        )

    def insert_signals(self, signals: Sequence[Signal]) -> dict[str, int]:
        self._guard()
        return discovery_db.insert_signals(self._cur, signals, run_id=self.run_id)

    # ── 跨 query 合并之后，整跑一轮 ────────────────────────────────

    def save_clusters(
        self, clusters: Sequence[CompanyCluster], *, signal_ids: dict[str, int]
    ) -> tuple[SavedLead, ...]:
        self._guard()
        return lead_db.save_clusters(
            self._cur, clusters, run_id=self.run_id, signal_ids=signal_ids
        )

    def lead_states(self, lead_ids: Sequence[int]) -> dict[int, LeadState]:
        return lead_db.lead_states(self._cur, lead_ids)

    def save_assessment(
        self,
        lead_id: int,
        *,
        assessment: Assessment,
        status: LeadStatus,
        cooldown_until: datetime | None,
        strikes: int,
        analyzed_at: datetime,
    ) -> None:
        self._guard()
        lead_db.save_assessment(
            self._cur,
            lead_id,
            assessment=assessment,
            status=status,
            cooldown_until=cooldown_until,
            strikes=strikes,
            analyzed_at=analyzed_at,
            run_id=self.run_id,
        )

    def record_discovery_hits(self, hits: Sequence[DiscoveryHit]) -> None:
        self._guard()
        discovery_db.record_discovery_hits(self._cur, hits, run_id=self.run_id)

    def mark_ran(self, query_id: int, *, ran_at: datetime) -> None:
        self._guard()
        query_db.mark_ran(self._cur, query_id, ran_at=ran_at)

    # ── 事务 ─────────────────────────────────────────────────────

    def finish(self, *, commit: bool) -> None:
        """把这一跑留下，或者当它没发生过。之后这个 store 不能再用。

        输入：留不留
        输出：无

        游标在这里一起关掉，两个动作都不吞异常：提交失败要让调用方看见，
        它是「这一跑到底算不算数」的唯一答案。

        finish 之后再写就抛，不是洁癖：psycopg 在提交之后遇到下一条语句会
        安静地开一个新事务，于是那几行写入变成一次没人管的、谁也不会提交
        的孤儿事务（或者更糟，被后面某次提交顺手带进去）。让它当场炸。
        """
        self._guard()
        # 先标记再动手：commit 自己抛的时候，这个 store 也不该还能接着写。
        self._finished = True
        try:
            if commit:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._cur.close()

    def _guard(self) -> None:
        """收尾之后不许再写。

        输入：无
        输出：无。已经收尾就抛 RuntimeError

        只挡写不挡读（seen_content_hashes 和 lead_states 不调它）：收尾之后
        再读没有意义，但也写不出孤儿事务，没有理由为它多一条限制。真读了
        会撞上 psycopg 的「游标已关闭」，那个报错本身就说清楚了。
        """
        if self._finished:
            raise RuntimeError("这个 store 已经收尾了，一个实例只服务一次 run")
