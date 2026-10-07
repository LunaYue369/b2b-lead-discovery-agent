"""一次 discovery run、一次 query execution，以及执行过程中的全部计数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from models.filtering import CandidateDropReason, SignalDropReason
from models.scoring import Assessment


class RunType(StrEnum):
    SCHEDULED = "scheduled"
    INTERACTIVE = "interactive"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"  # 部分 query 跑成功了


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ExecutionCounters:
    """一次 query 执行的全部计数。字段跟 query_executions 表逐一对应。

    每个节点只管往这个对象上加数，最后由 lead_repo 一次落库。
    好处是「表里的数对不对」变成一个能写 assert 的不变式，见 check()。
    """

    raw_result_count: int = 0  # 供应商返回的原始条数

    # 实际计费条数，跟上面那个不相等：评论会作为独立 item 塞进同一个 dataset
    # （实测 120 item 不是 60）。最终要优化的是「每块钱几个合格线索」，
    # 不是「每次执行几个」。
    billed_item_count: int = 0

    signal_dropped_count: int = 0  # prefilter 在 signal 级砍掉的
    candidate_dropped_count: int = 0  # prefilter 在 candidate 级砍掉的

    # 各规则各砍了多少。不记的话规则层就是个黑箱，误砍永远发现不了。
    drop_reasons: dict[str, int] = field(default_factory=dict)

    unique_company_count: int = 0  # 归并后的公司数
    researchable_lead_count: int = 0  # 过了 cooldown，这次要分析的
    cooldown_skipped_count: int = 0  # 因 cooldown 未到而跳过的
    excluded_skipped_count: int = 0  # 因已是 EXCLUDED 终态而跳过的，连 LLM 都省了

    # 本次执行首次发现的公司数。没有这一列，一条「只会重复发现别人
    # 已发现的公司」的 query 看起来跟真正带来新线索的一样好 ——
    # 而 proven 占每天 60% 的预算。
    novel_lead_count: int = 0

    qualified_lead_count: int = 0  # 分数过线的

    def bump_drop(self, reason: SignalDropReason | CandidateDropReason) -> None:
        self.drop_reasons[str(reason)] = self.drop_reasons.get(str(reason), 0) + 1

    def check_always(self) -> None:
        """不管这次跑成没跑成都该成立的那两条。

        输入：无
        输出：无。对不上就抛 ValueError

        拆出来是为了跟库里的 CHECK 对齐。query_executions 上那三条约束里，
        triage 那条带 `status <> 'completed'` 的前提，另外两条是无条件的。
        全都只在 completed 时查的话，一条 failed 的执行会在这边放行、
        到库里被拒，而库那层抛出来只有一个约束名。

        这两条无条件成立是有道理的：合格的不可能多过这次分析过的，
        头一回见到的不可能多过公司总数 —— 跑到一半崩了也一样。
        """
        if self.qualified_lead_count > self.researchable_lead_count:
            raise ValueError(
                f"qualified({self.qualified_lead_count}) 不可能超过 "
                f"researchable({self.researchable_lead_count})"
            )
        if self.novel_lead_count > self.unique_company_count:
            raise ValueError(
                f"novel({self.novel_lead_count}) 不可能超过 "
                f"unique_company_count({self.unique_company_count})"
            )

    def check(self) -> None:
        """跑完整一趟之后的自查。对不上说明某个节点漏加数了。

        输入：无
        输出：无。对不上就抛 ValueError

        比 check_always 多两条，它们只在这一跑走完整个漏斗之后才成立：
        分流三堆之和等于公司总数；drop_reasons 各项之和等于两级砍掉的总数。
        崩在半路的时候这两条本来就会对不上（计数是分节点加的），
        所以 failed 那一路只查 check_always。
        """
        self.check_always()
        triage = (
            self.researchable_lead_count
            + self.cooldown_skipped_count
            + self.excluded_skipped_count
        )
        if triage != self.unique_company_count:
            raise ValueError(
                f"分流数对不上：researchable({self.researchable_lead_count}) + "
                f"cooldown({self.cooldown_skipped_count}) + "
                f"excluded({self.excluded_skipped_count}) = {triage} "
                f"不等于 unique_company_count({self.unique_company_count})"
            )
        total_drops = sum(self.drop_reasons.values())
        counted = self.signal_dropped_count + self.candidate_dropped_count
        if total_drops != counted:
            raise ValueError(
                f"drop_reasons 合计({total_drops}) 不等于 "
                f"signal_dropped + candidate_dropped({counted})"
            )


@dataclass(frozen=True)
class DiscoveryHit:
    """「这一跑里，这条 query 命中了这家公司」的一行流水，append-only。

    落进 lead_discovery_hits 表。存明细而不是只存计数，是为了以后能回答
    query_executions 那几个计数列答不了的问题 —— 最要紧的一个是「这家公司
    是不是只有这条 query 找得到」。

    两条高度重叠的 query 每天发现同一批公司时，它们各自的 novel_lead_count
    都很好看，指标说「两条都很棒」，而真相是留一条就够。有了明细，
    「独占发现」用一句 SQL 就能算出来，不必现在就定死一个计数列，
    也不必现在就在两种归因规则里选一个。

    这也是为什么归因落在这张表上，而不是给 signals 加一个 query_id 列：
    一条帖能被多条 query 搜到，压进一列必然丢，而且同一条帖第二次被
    别的 query 搜到时不会重新落行，那一列会永远记着第一次的归因。
    """

    query_id: int
    lead_id: int
    novel: bool  # 这家公司是不是这一跑头一回进库（重叠命中时每条 query 各记一次）


@dataclass(frozen=True)
class QueryOutcome:
    """一条 query 在这一跑里的结果。per-query 的账全在这里。"""

    query_id: int
    execution_id: int
    status: ExecutionStatus
    counters: ExecutionCounters
    error: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    """run_discovery 的返回值。纯数据，不认识 Slack。

    图的最后一步返回一个 dataclass 而不是「发了一条 Slack 消息」，
    这样测整条链路不需要任何 token。这条在别的项目上踩过。

    scored 和 discarded 两项不重叠：拿到 Assessment 的都是引用验过的，
    进 discarded 的一条 Assessment 都没有。调用方对两者的处理也不一样 ——
    前者按分数排序推给 Riley，后者只进日志和计数。
    """

    run_id: int
    status: RunStatus
    per_query: tuple[QueryOutcome, ...]
    scored: dict[str, Assessment]  # company_key -> 打完分的结果
    lead_ids: dict[str, int]  # company_key -> leads.id，company_count 和日志里的 lead= 要用
    discarded: dict[str, list[str]]  # 反复答不好被丢弃的，company_key -> 编的那几句
    warnings: tuple[str, ...]

    @property
    def company_count(self) -> int:
        """这一跑一共发现了多少家公司（去重后）。

        输入：无
        输出：一个整数

        这个 property 存在的唯一理由是挡住一个错误写法：把各条 query 的
        unique_company_count 加起来。一家公司被两条 query 命中时，两条账里
        各记一次（那是 per-query 指标的正确口径），所以那个和会大于实际家数。
        runs 表里没有跑级的计数列，那个和又看着完全合理，所以这个数必须
        在这里给出来。

        数的是去重后的 lead_id 不是键的个数：两个身份档不同的簇偶尔会撞进
        库里同一行（见 pipeline 里 merged_away 那一段），那时候键有两个、
        公司只有一家。
        """
        return len(set(self.lead_ids.values()))
