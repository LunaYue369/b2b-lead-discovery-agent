"""落库接口（ports/adapters 的 port 一侧，跟 adapters/base.py 同一个位置）。

pipeline 依赖这个接口，不依赖 psycopg。三个理由，第三个才是决定性的：

1. discovery/ 那一层不许连库（scripts/check_isolation.py 第四条）。
   规矩管的不是「不许写 SQL」，管的是「这一层能不能拿历史数据反复重放」。
   把游标透传进去在字面上过得了检查，但带着真游标的 pipeline 不管
   类型标注写什么都不是可重放的，那是钻规则的空子。

2. 一份固定的 JSON 加一个假 store 加一个假 LLM 客户端，就能把整条链路
   从头跑到尾，不连库不花钱。现在 experiment.py 和 score_once.py
   各自只跑得了半条。

3. pipeline 最值得测的是异常路径，而异常路径拿真库根本造不出来。
   「第三条 query 采集挂了，前两条的账还在不在、整跑还出不出结果」
   「analyzer 中途炸了，那些停在 running 的执行有没有被收尾」——
   没法让供应商在第三条 query 上超时，也没法让模型在第二批炸。
   编排层的价值几乎全在这些路径上，它们只能用假的来测。

这不排斥真库那条路：真 SQL、身份归并、库里的 CHECK 约束由
tests/test_lead_db.py 和 scripts/verify_persistence.py 各自焊着，
假 store 不负责证明那些东西对。分工线就划在这里 ——
假 store 只测编排，真库只测 SQL。

一个 store 实例只服务一次 run，run_id 绑在实例上而不是每个方法都传一遍：
它同时也是持有事务的那个对象（finish 是那个事务唯一的出口），生命周期本来就跟
一次 run 一样长。每个方法都收 run_id 的话，「传错了另一次 run 的 id」
就成了一种可能，而那种错落库之后完全看不出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class DiscoveryStore(Protocol):
    """一次 discovery run 要用到的全部落库动作。

    Protocol 是 Python 的「结构化接口」：一个类不需要继承它，只要方法名和
    签名对得上就算实现了它。加 runtime_checkable 之后 isinstance 也能查，
    但那只查方法名在不在，不查签名对不对。

    方法按 pipeline 里的调用顺序排，不按表分 —— 这个接口的读者是 pipeline，
    「按表分文件」那条口径管的是 persistence/ 内部怎么放代码。
    """

    run_id: int

    # ── 一跑开头取一次 ────────────────────────────────────────────

    def seen_content_hashes(self, *, days: int) -> frozenset[str]:
        """最近这些天见过的正文摘要，prefilter 的跨天记忆。

        输入：往回看几天
        输出：一批 content_hash

        窗口天数从参数进不写死：定这个数的理由是语义不是存储 ——
        超过窗口还一模一样的正文再出现，更可能是新事件，
        而不是同一轮联播的尾巴。
        """
        ...

    # ── 每条 query 一轮 ──────────────────────────────────────────

    def start_execution(self, *, query_id: int) -> int:
        """先插一行 running，返回 execution_id。

        输入：这次跑的是哪条 query
        输出：query_executions 那一行的 id

        开始和结束分两步走，是为了让「崩在半路」在库里留得下痕迹。
        一步式的写法在 query 跑完之后才插行，一条崩掉的执行库里一行不留。
        """
        ...

    def finish_execution(
        self,
        execution_id: int,
        *,
        status: ExecutionStatus,
        counters: ExecutionCounters,
        error: str | None = None,
    ) -> None:
        """给一条执行收尾，写最终状态和全部计数。

        输入：start_execution 给的 id；最终状态；累加好的计数；失败原因
        输出：无

        计数在这里一次写满，而不是边跑边更新：三堆分流之和要等于公司总数
        这类自查，只有在这一跑走完之后才成立。
        """
        ...

    def insert_signals(self, signals: Sequence[Signal]) -> dict[str, int]:
        """把过完闸门留下的信号写进库。

        输入：这条 query 留下来的信号
        输出：external_id 到 signals.id 的映射，写证据那一步要用

        撞了 external_id 说明这条帖之前存过，那是正常的（同一条帖过几天
        又被搜到，作为证据只算一份），返回的仍然是它原来那一行的 id。
        """
        ...

    # ── 跨 query 合并之后，整跑一轮 ────────────────────────────────

    def save_clusters(
        self, clusters: Sequence[CompanyCluster], *, signal_ids: dict[str, int]
    ) -> tuple[SavedLead, ...]:
        """把归并好的公司连同证据一起落库。

        输入：跨 query 合并归并出来的簇；external_id 到 signals.id 的映射
        输出：跟 clusters 一一对应、顺序相同的 SavedLead

        返回逐条而不是三个合计数，是因为 pipeline 后面要用每一家的
        lead_id 和 novel：lead_id 拿去分流和写分数，novel 拿去按 query
        归因（一家公司可能被多条 query 命中，合计数分不回去）。
        """
        ...

    def lead_states(self, lead_ids: Sequence[int]) -> dict[int, LeadState]:
        """问库这批公司现在什么样：这次要不要花钱分析，上一次判成什么样。

        输入：这一跑发现的全部 leads.id
        输出：lead_id 到 LeadState 的映射。传进来的每个 id 都必须有一项，
              不许缺；重复的 id 只占一项

        三件事一次查回来，因为读的是同一行 leads：冷却分流的判决、
        上一次的分数、已有的退避次数。分成两个方法的话不但多一次查库，
        还多一条「上次的分要在写新分之前问到」的时序约束。

        逐家返回而不是给三个合计数：按 query 归因要知道是哪几家。一家公司
        可能被今天两条 query 同时命中，那两条的账里都要记上它这一档，
        而合计数分不回去。三个数在调用方随时数得出来。

        查全部而不只查要分析的那一批：不分析的那些也要在每条 query 的账里
        分类计数，那是 query_executions 三堆分流对账的依据。
        """
        ...

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
        """把七维打分、打分用的引用和冷却决定写回 leads 那一行。

        输入：哪一家；完整打分；新状态；下次可复查的时刻（终态是 None）；
              新的退避次数；这次分析的时刻
        输出：无

        分数和冷却写在同一个动作里，是因为它们是同一件事的两半：
        「这次判成什么样」和「下次什么时候再判」。拆成两个方法的话，
        中间崩一下就会留下一行有新分数、却还带着旧冷却期的数据。

        这一跑的 run_id 不是参数，由实现自己带上（跟 insert_signals 一样），
        写进 leads.last_analyzed_run_id。推卡时靠它查出「这一跑打了分的是哪几家」。
        """
        ...

    def record_discovery_hits(self, hits: Sequence[DiscoveryHit]) -> None:
        """记下「这一跑里，哪条 query 命中了哪家公司」。

        输入：一批命中流水
        输出：无

        append-only，不去重也不更新。这张表是 query 表现的原始账本，
        改写它等于改写历史。
        """
        ...

    def mark_ran(self, query_id: int, *, ran_at: datetime) -> None:
        """记下这条 query 最近一次跑成功的时刻。

        输入：哪条 query；什么时候跑成功的
        输出：无

        语义是「跑成功」不是「尝试过」。采集挂了还调它的话，这条 query
        白吃一整个 cadence 冷却期，而报表上只显示「今天没轮到」。
        """
        ...

    # ── 事务 ─────────────────────────────────────────────────────

    def finish(self, *, commit: bool) -> None:
        """收尾：把这一跑留下，或者当它没发生过。

        输入：留不留
        输出：无

        一跑一个事务，中途不提交，所以这个方法是全部写入唯一的出口。
        刻意没有「每跑完一条 query 提交一次」那种做法，理由是提交一半会
        静默丢线索：信号先落了库，而公司要等跨 query 归并之后才写，
        中间崩一下就留下一批「见过但从没变成公司」的信号 —— 那些
        content_hash 进了跨天 seen 集合，第二天同一批帖会被当重复丢掉，
        它们指向的公司从此再也发现不了，而报表上一切正常。
        采集的钱是已经花掉的，回滚只多花一次明天的采集；提交一半的代价
        是永久丢线索，两边不对等。

        commit 由两件事一起决定：这一跑有没有整体失败，以及 run_discovery
        的 persist。单条 query 采集失败不算整体失败 —— 那一条记 failed、
        别的照跑，最后照样提交。

        commit=False 不是「什么都不写」，而是「同样的写路径全走一遍，最后
        整个丢掉」。这两者对 interactive 那条路差别很大：「这家公司是不是
        头一回见到」和「它在不在冷却期」都要先当作真的写进去了才算得对，
        跳过写入会让这一跑的结论跟真跑不一样。
        """
        ...
