"""Shared Discovery Core 的字面实现 —— 一个函数，不是一个概念。

scheduled 和 interactive 调的都是这里，这就是「共用」的全部含义。

成本级联，闸门放在花钱之前：

    harvest    花钱      量由自己定
    normalize  不花钱
    prefilter  不花钱    闸门在这
    dedup      不花钱    第二道：终态那一档连第一次 LLM 都省
    analyzer   贵，而且会犯错  ← 最后才到

顺序不能调。prefilter 挪到 analyzer 后面，省下的钱和挡住的错都没了。

外部世界的三样东西全从参数进（采集、库、模型客户端），跟 analyzer 的
client 和 now 是同一条纪律。好处是这个函数在一台没有密钥、没有网络、
没有数据库的机器上也能整条跑完，用一份固定的 JSON 加两个假实现。

两段式，不是一条直线
────────────────────

    第一段 逐条 query                   第二段 整跑一次
    start_execution(running)            to_clusters(全部候选)
      adapter.fetch                     save_clusters
      to_signal                         lead_states(分流 + 上次的分)
      drop_signals                      analyze
      to_candidates                     next_cooldown + save_assessment
      drop_candidates                   record_discovery_hits
      insert_signals                    按 query 回填计数
                                        finish_execution 逐条
                                        finish(一跑一个事务，这里才提交)

归并放在跨 query 那一段，有两个理由。一是两条不同的 query 命中同一家，
正是 corroboration 的分子，那是最强的免费信号，逐条 query 各归各的
就把它拆散了。二是同一家公司只该花一次 LLM 的钱。

顺带还白拿一个好处：分流是整跑做一次的，所以第二条 query 不会把
第一条这一跑刚发现的公司记成「还在冷却」——逐 query 独立跑的话，
那条 query 的 cooldown rate 会被冤枉，而那个数是判断 query 该不该
降级的依据之一。

执行记录因此要跨过第一段停在 running：researchable / cooldown_skipped /
novel / qualified 这几个数只有第二段才算得出来。start 和 finish
分两步走那个改动，收益就落在这里。

按 query 归因怎么落
────────────────────

pipeline 手上有「哪条 query 采到了哪些帖 -> 哪些候选 -> 哪些公司」
这条内存链，per-query 的计数全能算出来，不用回查 signals.query_search
那一列（它只记第一个搜到这条帖的 query，第二条永远记不上）。

一家公司被两条 query 命中时，两条账里各记一次。所以各 query 的
unique_company_count 和 novel_lead_count 之和会大于这一跑的实际家数 ——
这是 per-query 指标的正确口径，不是 bug。跑级的那个数看
DiscoveryResult.company_count，别去加 executions 那一列。

跨 query 的重叠本身是个要盯的东西：两条高度重叠的 query 每天发现
同一批公司时，两边的指标都很好看，而真相是留一条就够。明细落在
lead_discovery_hits 上就是为了以后能把这件事算出来，见 DiscoveryHit。

错误隔离三档
────────────

    一条 query 采集挂了   这条记 failed，继续下一条，整跑照样出结果
                          这条不调 mark_ran（那一列的语义是「最近一次跑成功」）
    analyzer 整段炸       整跑回滚，一个字都不留。不能只留信号 ——
                          那批 content_hash 会进跨天 seen 集合，第二天同一批帖
                          被当重复丢掉，它们指向的公司再也发现不了
    一条 query 全砍光      记 completed，计数全 0。这是正常结果不是错误

一条都没跑成 -> failed；有成有败 -> partial；全成 -> completed。

重跑当天是安全且便宜的：信号撞 external_id 会跳过、公司会被认下来、
novel 归零、冷却期挡住二次 LLM，只重复花采集的钱。所以「今天挂了明天
怎么办」的答案是直接再跑一次。

不归这里管的两件事
──────────────────

数量不够要不要补跑一轮，是 trigger 的事。配额是天花板不是地板，
而且「同样的库状态选出同样的 queries」这条可重放性要守住 ——
pipeline 只跑给它的那几条，不自己决定再跑。

推卡片也不归这里。这个函数返回一个纯数据的 DiscoveryResult，
不认识 Slack。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from adapters.base import SourceAdapter
from discovery.analyzer import ScoringConfig, analyze, company_key
from discovery.cooldown import CooldownConfig, next_cooldown
from discovery.normalize import to_candidates, to_clusters, to_signal
from discovery.prefilter import FilterRules, drop_candidates, drop_signals
from models import (
    Assessment,
    Candidate,
    CompanyCluster,
    DiscoveryHit,
    DiscoveryResult,
    ExecutionCounters,
    ExecutionStatus,
    LeadState,
    LeadType,
    QueryOutcome,
    QuerySpec,
    RunStatus,
    SavedLead,
    Signal,
    TriageVerdict,
)
from persistence.base import DiscoveryStore


@dataclass(frozen=True)
class _QueryHarvest:
    """一条 query 走完第一段之后手上的东西。

    只在这个模块内部传递，所以是私有的 —— 第二段要的是全部 query 的
    候选合在一起，这个对象本身不会出现在返回值里。

    counters 此时只填到 candidate_dropped 那一层：公司数、分流、novel、
    qualified 六个数要等跨 query 归并之后才算得出来，那是第二段的事。
    """

    kept_signals: tuple[Signal, ...]  # 过完 signal 级闸门留下来的
    candidates: tuple[Candidate, ...]  # 再过完 candidate 级闸门留下来的
    signal_ids: dict[str, int]  # external_id 到 signals.id，写证据要用
    counters: ExecutionCounters


@dataclass
class _QueryRun:
    """一条 query 在这一跑里的全部记录，成没成都在这里。

    harvest 和 error 互斥：采集加闸门那一段跑通了就有 harvest、error 是 None，
    抛了异常就反过来。不做成两个列表，是为了保住「跟传进来的 queries 同序」
    这条性质 —— 报表要按「今天跑了哪几条」来读，不是按「哪几条成了」，
    而分成两个列表之后原来的顺序就拼不回来了。

    counters 跟 harvest 里那个是同一个对象。放在这里是因为失败那一路
    没有 harvest，而半截的计数照样要写进执行记录。
    """

    spec: QuerySpec
    execution_id: int
    counters: ExecutionCounters
    harvest: _QueryHarvest | None = None
    error: str | None = None

    def outcome(self) -> QueryOutcome:
        """转成对外的那份记录。

        输入：无
        输出：一个 QueryOutcome

        状态由 harvest 在不在决定，不另存一个 status 字段：两处各存一份
        早晚会漂，而「有产物却记着 failed」这种数据没有人看得出来是错的。
        """
        return QueryOutcome(
            query_id=self.spec.query_id,
            execution_id=self.execution_id,
            status=ExecutionStatus.FAILED if self.harvest is None else ExecutionStatus.COMPLETED,
            counters=self.counters,
            error=self.error,
        )


def _collect_one_query(
    spec: QuerySpec,
    *,
    adapter: SourceAdapter,
    store: DiscoveryStore,
    rules: FilterRules,
    prior_seen: frozenset[str],
    run_seen: frozenset[str],
    max_posts_per_query: int,
    counters: ExecutionCounters,
) -> _QueryHarvest:
    """第一段：一条 query 从采集走到信号落库。

    输入：跑哪条 query；采集实现；落库接口；闸门规则；更早那些跑见过的
          正文摘要；这一跑前面几条 query 已经留下的正文摘要；
          这条 query 最多采几条；一个空的计数对象
    输出：一个 _QueryHarvest。同时把前四个数填进传进来那个计数对象

    计数对象从外面传进来而不是在这里造，是为了让「崩在半路」也留得下数。
    采集成功、归一化那步炸了的时候，run_discovery 手上仍然有
    raw_result_count 和 billed_item_count，能写进那行 failed 的执行记录；
    造在里面的话异常一抛就什么都没了，报表上「采集回来几条」是 0，
    看着像一条都没采到。

    这里不碰 start_execution 和 finish_execution。执行记录由 run_discovery
    在外面开合，因为「采集挂了要把这条执行记成 failed」需要在这个函数
    抛异常之后还能拿到 execution_id，而 id 是 start_execution 给的 ——
    开在里面的话，异常一抛出去就没人知道该收尾哪一行了。

    下面用一个例子从头走到尾，每一步手上有什么都写出来。

    输入长这样
    ──────────

        spec   QuerySpec(query_id=30,
                         text='Kickstarter AND (microphone OR audio)',
                         source_type=SourceType.LINKEDIN_POST,
                         pool=QueryPool.CANDIDATE,
                         cadence_days=1)

        prior_seen  frozenset({"9f86d081...", "2c26b46b..."})
                    库里查出来的，只有最近 seen_days 天那个窗口内的摘要，
                    不是全部历史。窗口的理由是语义不是存储：超过十天还
                    一字不差地再出现，更可能是一件新事，不是同一轮联播
                    的尾巴

        run_seen    frozenset()
                    这一跑前面几条 query 已经留下的摘要。今天这条是第一条，
                    所以是空的。它跟 prior_seen 分开传，因为两者挡掉的
                    东西一样、记的账相反 —— 见下面第 3 步

        两个集合都是 run_discovery 传进来的，这一层不查库

        max_posts_per_query  40
        rules                config/filters.yaml 读出来的一整套闸门规则
        adapter / store      采集和落库，两个都是接口，实现从外面注入

    第 1 步 采集
    ────────────

        adapter.fetch(spec, max_items=40) 拿回 5 条 RawSignal
        （真跑一条 query 是几十条，这里写 5 条是为了下面每一步都数得清）：

            p-1  创始人发帖：我们的众筹结束了，正在找代工厂
            p-2  某加速器从公司主页发帖，恭喜被投的 Solara Audio 上线众筹
            p-3  某供应商从个人号发帖：我们帮 Lumen 做了 5000 只耳机
            p-4  理财顾问发的联播稿，正文跟音频硬件一点关系都没有
            p-5  一条公告，正文跟更早某一跑见过的那条一字不差

        counters.raw_result_count = 5
        counters.billed_item_count = 5

    第 2 步 归一
    ────────────

        to_signal 逐条翻译成 Signal：算 content_hash、算 matched_terms、
        转发的两段正文拼在一起算。这一步只算不砍，5 条还是 5 条。

            p-1  matched_terms=("Kickstarter", "audio")
            p-2  matched_terms=()  但 mentioned_companies 里有 Solara Audio
            p-3  matched_terms=("audio",)
            p-4  matched_terms=()  mentioned_companies 也是空的
            p-5  matched_terms=("Kickstarter",)  content_hash 是 "9f86d081..."

    第 3 步 signal 级闸门
    ─────────────────────

        drop_signals 整条丢弃。这里砍掉两条：

            p-4  两条相关性通道都不成立            NO_RELEVANCE_SIGNAL
            p-5  content_hash 在 prior_seen 里     SEEN_HASH_PRIOR_RUN

        p-2 没有命中词却活下来，是因为相关性有两条通道：被 @ 的公司是
        身份精确的通道，它跟我们相不相关，不该由发帖人用没用我们的词决定。

        p-5 要是撞在 run_seen 上（今天前面某条 query 已经采到过同一篇），
        记的就是 SEEN_HASH_IN_RUN 而不是 PRIOR_RUN。两个数的运营含义相反：
        跨天是正常损耗，跑内说明今天这几条 query 在互相重叠地采同一批帖，
        那是 query 组队要管的事。所以两个集合分开传，不合成一个。

        kept_signals = (p-1, p-2, p-3)
        counters.signal_dropped_count = 2
        counters.drop_reasons = {"no_relevance_signal": 1,
                                 "seen_hash_prior_run": 1}

    第 4 步 展开候选 + candidate 级闸门
    ───────────────────────────────────

        to_candidates 把每条信号指向的公司摊开，3 条信号摊出 5 个候选：

            p-1 -> Chatterly（author，从 headline 解析出来的）
            p-2 -> 那家加速器（author，公司主页）
                   Solara Audio（mentioned）
            p-3 -> 深圳某音频厂（author，headline 是供应商侧）
                   Lumen（mentioned）

        drop_candidates 只丢候选不丢信号，这里砍掉两个：

            那家加速器      COMPANY_PAGE_AUTHOR
            深圳某音频厂     SUPPLIER_HEADLINE

        p-3 那条最能说明为什么闸门要分两级：同一条信号摊出来的两个候选
        命运相反。写成整条丢弃的话，Lumen 这个真买方会跟着供应商一起没。

        candidates = (Chatterly, Solara Audio, Lumen)
        counters.candidate_dropped_count = 2
        counters.drop_reasons = {"no_relevance_signal": 1,
                                 "seen_hash_prior_run": 1,
                                 "company_page_author": 1,
                                 "supplier_headline": 1}

        四项之和 4 = 2 + 2，ExecutionCounters.check() 焊的就是这一条，
        所以每条丢弃记录都要 bump 一次，一条都不能漏。

    第 5 步 落库
    ────────────

        store.insert_signals 写 kept_signals 三条。p-3 上周就存过（同一条帖
        被再搜到是正常的），撞唯一键跳过，拿回来的是它原来那一行的 id：

            signal_ids = {"p-1": 318, "p-2": 319, "p-3": 205}

    输出长这样
    ──────────

        _QueryHarvest(
            kept_signals=(p-1, p-2, p-3),
            candidates=(Chatterly, Solara Audio, Lumen),
            signal_ids={"p-1": 318, "p-2": 319, "p-3": 205},
            counters=ExecutionCounters(
                raw_result_count=5, billed_item_count=5,
                signal_dropped_count=2, candidate_dropped_count=2,
                drop_reasons={"no_relevance_signal": 1,
                              "seen_hash_prior_run": 1,
                              "company_page_author": 1,
                              "supplier_headline": 1}))

        counters 里另外六个数（公司数、分流三堆、novel、qualified）此刻
        全是 0，它们要等跨 query 归并之后才算得出来，那是第二段的事。
        这三个候选也还没归并成公司 —— Solara Audio 可能同时被今天另一条
        query 命中，那种情况下它们要合成一家，而不是各算各的。
    """
    # 计数对象是 run_discovery 造的、每条 query 一个新的，不累加也不从库里读
    # ——「这条 query 历史上表现如何」是 stats 从 query_executions 聚合出来的，
    # 跟这一次跑了什么无关。
    # 第 1 步。两个数不相等：fetch 返回的是我们能用的 post，而评论会作为
    # 独立 item 一起计费，它们变不成 RawSignal，所以从列表长度推不出来。
    # billed_items() 问的是 adapter 手上那次 fetch 的计费数（它把上一句
    # 那次调用的结果记着），所以要紧跟在 fetch 后面取。
    # 抓不到 adapter 会抛，不会返回空列表冒充成功 —— 静默 0 条会让这条
    # query 的指标记成「没线索」，实际是网络断了，而那组数字决定
    # proven 池 60% 的预算。
    raw_signals = adapter.fetch(spec, max_items=max_posts_per_query)
    counters.raw_result_count = len(raw_signals)
    counters.billed_item_count = adapter.billed_items()

    # 第 2 步和第 3 步。两批摘要都从参数进来 —— 闸门是纯函数，自己不查库。
    # 分开传是为了归因：撞在库里那批上是跨天的正常损耗，撞在这一跑前面
    # 几条 query 留下的那批上是 query 重叠，两个数要分开看。
    signals = [to_signal(raw) for raw in raw_signals]
    kept_signals, signal_drops = drop_signals(
        signals, rules=rules, seen_content_hashes=prior_seen, run_seen=run_seen
    )
    counters.signal_dropped_count = len(signal_drops)
    for drop in signal_drops:
        counters.bump_drop(drop.reason)

    # 第 4 步。候选从留下来的信号里展开，不从原始信号里展开：
    # 被整条丢掉的信号不该再贡献候选。
    candidates = [candidate for signal in kept_signals for candidate in to_candidates(signal)]
    kept_candidates, candidate_drops = drop_candidates(candidates, rules=rules)
    counters.candidate_dropped_count = len(candidate_drops)
    for drop in candidate_drops:
        counters.bump_drop(drop.reason)

    # 第 5 步。落的是 kept_signals 而不是 kept_candidates 那些信号：
    # 一条信号的候选可能被砍光，但它自己过了 signal 级闸门，
    # 仍然是这一跑看见过的事实，而且下一跑要靠它的 content_hash 去重。
    signal_ids = store.insert_signals(kept_signals)

    return _QueryHarvest(
        kept_signals=tuple(kept_signals),
        candidates=tuple(kept_candidates),
        signal_ids=signal_ids,
        counters=counters,
    )


@dataclass(frozen=True)
class _RunAnalysis:
    """第二段跑完之后，这一跑在公司这一侧的全部结果。

    clusters 和 saved 一一对应、顺序相同，两者要一起用（一个给身份和证据，
    一个给 lead_id 和 novel）。合成一个对象是可以的，但那要么改 to_clusters
    的返回值、要么在这里造第三种结构，两条都是为了省一个 zip 去动别处。
    """

    clusters: tuple[CompanyCluster, ...]
    saved: tuple[SavedLead, ...]
    states: dict[int, LeadState]  # lead_id 到库里的现状，含这次要不要分析
    lead_ids: dict[str, int]  # company_key 到 leads.id，这一跑发现的每一家都在
    scored: dict[str, Assessment]  # company_key 到打完的分，只有分析过且过关的
    discarded: dict[str, list[str]]  # 反复答不好被丢弃的，company_key 到它编的那几句
    # 两个簇撞进了库里同一行，被让掉的那几个 company_key。正常是空的，
    # 见 _merge_and_analyze 里去重那一段。不空就该进 warnings，别静默。
    merged_away: tuple[str, ...]


def _merge_and_analyze(
    harvests: Sequence[_QueryHarvest],
    *,
    store: DiscoveryStore,
    llm_client: Any,
    facts: dict[str, Any],
    scoring: ScoringConfig,
    cooldown: CooldownConfig,
    now: datetime,
    skip_cooldown: bool,
) -> _RunAnalysis:
    """第二段：把全部 query 的候选合起来归并、分流、打分、写回冷却。

    输入：全部 query 的第一段产物；落库接口；模型客户端；我们自己的事实；
          打分配置；冷却配置；当前时刻；要不要让冷却分流整个让路
    输出：一个 _RunAnalysis

    这一段整跑只做一次，不逐 query 做。两个理由：两条不同的 query 命中
    同一家正是 corroboration 的分子，逐条各归各的就把它拆散了；同一家公司
    只该花一次 LLM 的钱。代价是每条 query 的六个计数要事后按命中集合分回去，
    那是 run_discovery 那边的事。

    接着第一段那个例子往下走。今天跑了两条 query
    ────────────────────────────────────────────

        query 30  留下 Chatterly、Solara Audio、Lumen
        query 31  留下 Solara Audio（另一条帖）、Vantek Labs、Kessel Mics

    第 1 步 合并候选，归并成公司
    ────────────────────────────

        6 个候选合在一起交给 to_clusters，按身份键归并成 5 家：

            Chatterly        1 条证据
            Solara Audio   2 条证据，来自两条不同的 query
            Lumen          1 条
            Vantek Labs    1 条
            Kessel Mics    1 条

        Solara Audio 那两条就是跨 query 归并的全部意义：两条互相独立的
        query 各自撞上它，corroboration 因此是 2 而不是两个 1。
        逐 query 分开归并的话，这个信号不存在，而且它要被打两次分。

    第 2 步 落库，顺便知道哪几家是头一回见到
    ────────────────────────────────────────

        store.save_clusters 返回跟 clusters 一一对应的 SavedLead：

            Chatterly       lead_id=501  novel=True
            Solara Audio  lead_id=502  novel=True
            Lumen         lead_id=503  novel=False   库里早有
            Vantek Labs   lead_id=504  novel=False
            Kessel Mics   lead_id=505  novel=False

        novel 是 upsert 顺手带出来的，那一次查库在 upsert 里已经做过了。

    第 3 步 问库：这次花钱分析哪几家，它们上次判成什么样
    ────────────────────────────────────────────────────

        store.lead_states 一次把五家的现状都拿回来。三件事读的是同一行
        leads，所以一起问：

            501  RESEARCHABLE  上次分数 None  退避 0   新公司，没有冷却期
            502  RESEARCHABLE  上次分数 None  退避 0   同上
            503  RESEARCHABLE  上次分数 58    退避 0   冷却期已经到了
            504  EXCLUDED      -                      终态，上个月判过品类
                                                      不对，连 LLM 都省
            505  COOLING       -                      上周刚分析过

        verdict 是库判的：「还在冷却期吗」拿库里的 now() 比，不取回时刻
        自己比 —— 客户端凑一个「现在」的话，两台机器的时钟和时区不保证
        一致，同一批数据会分流出不同结果。

        skip_cooldown 为真时照样查库（上次的分数和退避次数还是要的），
        只是把每一家的 verdict 就地改成 RESEARCHABLE —— 冷却中的和终态的
        一起放行。它服务的是「我就要看这家」，人已经明确点名了，系统的
        记账不该拦她。代价要知道：一家终态公司这样被重新分析、而这次
        icp_fit 不为 0 的话，它的 status 会从 EXCLUDED 变回 NEW。
        那是「人点名要看」应有的效果，不是意外，但它确实翻了旧判决。

    第 4 步 打分
    ────────────

        analyze 只拿要分析的那三家的簇（分批、重问、防幻觉都在它里面）：

            scored     Chatterly 71 分、Lumen 52 分
            discarded  Solara Audio，引用反复对不上原文

    第 5 步 算冷却，跟分数一起写回去
    ────────────────────────────────

            Chatterly  71 >= 60         qualified  30 天   strikes 0
            Lumen    52，上次 58       in_range   本该 14 天，但分数没起色，
                                                  strikes 0 -> 1，翻成 28 天

        Solara Audio 被丢弃了，不写分数也不写冷却，明天可以再试。
        这是刻意的：引用对不上是模型跟这批证据之间的事，不是这家公司的
        属性，凭它给公司记一笔冷却是记错了对象。这一档的量很小，
        真变多了会在 discarded 里看得见。

    输出长这样
    ──────────

        _RunAnalysis(
            clusters=(Chatterly, Solara Audio, Lumen, Vantek Labs, Kessel Mics),
            saved=(501/新, 502/新, 503/旧, 504/旧, 505/旧),
            states={501: LeadState(RESEARCHABLE, None, 0), ...,
                    504: LeadState(EXCLUDED, 31, 2),
                    505: LeadState(COOLING, 44, 1)},
            lead_ids={"li_id:106...": 501, ...}，五家都在,
            scored={"li_id:106...": Assessment(score=71), "name:lumen": ...},
            discarded={"li_id:993...": ["We just closed our seed round"]},
            merged_away=())

        每条 query 的六个计数还没填 —— 那要拿这里的结果跟各 query 的命中
        集合对一遍，是下一个函数的事。

    两个簇撞进库里同一行（merged_away 那件事）
    ──────────────────────────────────────────

        跑内归并按身份键做，键取梯度最高那一档，所以「只有名字」的簇和
        「有 LinkedIn id」的簇是两个键。normalize 那边会把同名的弱簇并进
        强簇，但它比的是本跑强簇自己的公司名 —— 如果库里那一行的名字是
        更早某一跑用另一种写法写下的，本跑并不到一起，而查库时两个簇会
        撞上同一行。

        这时候不能两个都分析：同一家公司花两次钱，而且两次的分数都写进
        同一行，后写的盖掉先写的，退避还会拿同一个旧分算两遍。这正是
        「同一家答两次、后一行覆盖前一行」那类静默错。

        所以按 lead_id 去重，只分析第一个撞上的簇，让掉的记进 merged_away
        让它可见。让掉的那个簇在按 query 归因时照样算 —— 那条 query 确实
        命中了这家公司，只是这次的证据由另一个簇代表。

        真正的解法是 enrich 补域名之后按域名对，以及 find_lead 四级都查完
        再合并，两笔都还没做。这里只保证它不静默、不重复花钱。
    """
    # 把两条 query 留下的候选和 signal_ids 各自合成一份。
    # signal_ids 合并不会撞：键是 external_id，同一条帖被两条 query 采到时
    # insert_signals 两次拿回的是同一行 id。
    candidates = [candidate for harvest in harvests for candidate in harvest.candidates]
    signal_ids = {
        external_id: signal_id
        for harvest in harvests
        for external_id, signal_id in harvest.signal_ids.items()
    }

    clusters = to_clusters(candidates)
    saved = store.save_clusters(clusters, signal_ids=signal_ids)

    # strict=True 让「两个序列长度不一样」当场炸 —— 它们是靠位置对应起来的，
    # 长度一旦不等，后面每一家的 lead_id 都安在了别人头上，
    # 而那种错落库之后完全看不出来。
    lead_ids = {
        company_key(cluster): entry.lead_id
        for cluster, entry in zip(clusters, saved, strict=True)
    }
    # company_key 由簇的身份键拼出来，而簇本来就是按身份键归并的，所以
    # 一簇一个键。撞了说明 to_clusters 的归并键变了，而上面那个字典会
    # 静默少一项、company_count 跟着少一家。同类的坑掉过一次（用 set 查
    # 「模型答了哪几家」，同一家答两次照样相等），所以这里当场炸。
    if len(lead_ids) != len(clusters):
        raise ValueError(f"company_key 撞了：{len(clusters)} 个簇只拼出 {len(lead_ids)} 个键")

    # 第 3 步。skip_cooldown 也照样查库 —— 上次的分数和退避次数还是要用，
    # 让路的只是 verdict 这一项。
    states = store.lead_states([entry.lead_id for entry in saved])
    if skip_cooldown:
        states = {
            lead_id: replace(state, verdict=TriageVerdict.RESEARCHABLE)
            for lead_id, state in states.items()
        }

    #        researchable：要分析的簇，按 lead_id 去重。
    # 去重是必须的，不是保险：两个簇撞进库里同一行时，两个都分析等于同一家
    # 公司花两次钱，而且两次的分都写同一行、后写的盖掉先写的。理由和真正的
    # 解法见 docstring 最后一节。
    researchable: list[tuple[CompanyCluster, SavedLead]] = []
    merged_away: list[str] = []
    taken: set[int] = set()
    for cluster, entry in zip(clusters, saved, strict=True):
        if states[entry.lead_id].verdict is not TriageVerdict.RESEARCHABLE:
            continue
        if entry.lead_id in taken:
            merged_away.append(company_key(cluster))
            continue
        taken.add(entry.lead_id)
        researchable.append((cluster, entry))

    # 第 4 步。没有一家要分析时直接收工：analyze 空跑一趟也不会出错，
    # 但那是一次白花的请求开销，而「今天全在冷却里」是很正常的一天。
    if not researchable:
        return _RunAnalysis(
            clusters=clusters,
            saved=saved,
            states=states,
            lead_ids=lead_ids,
            scored={},
            discarded={},
            merged_away=tuple(merged_away),
        )

    scored, discarded = analyze(
        [cluster for cluster, _ in researchable],
        facts=facts,
        config=scoring,
        client=llm_client,
        now=now,
    )

    # 第 5 步。遍历的是 researchable 而不是 scored，因为要按 lead_id 写库，
    # 而 scored 的键是 company_key —— 从这边走一遍两个都在手上。
    for cluster, entry in researchable:
        assessment = scored.get(company_key(cluster))
        if assessment is None:
            # 反复答不好被丢弃的那几家。不写分数也不写冷却，理由见 docstring。
            continue
        state = states[entry.lead_id]
        decision = next_cooldown(
            assessment,
            previous_score=state.previous_score,
            strikes=state.cooldown_strikes,
            config=cooldown,
            now=now,
        )
        store.save_assessment(
            entry.lead_id,
            assessment=assessment,
            status=decision.status,
            cooldown_until=decision.cooldown_until,
            strikes=decision.strikes,
            analyzed_at=now,
        )

    return _RunAnalysis(
        clusters=clusters,
        saved=saved,
        states=states,
        lead_ids=lead_ids,
        scored=scored,
        discarded=discarded,
        merged_away=tuple(merged_away),
    )


def _candidate_key(candidate: Candidate) -> tuple[str, LeadType, tuple[str, str]]:
    """给一个候选拼一个可以当字典键的标识。

    输入：一个候选
    输出：(哪条帖, 哪条通道, 公司身份键)

    要这个东西是因为 Candidate 自己不能当字典键：它里面挂着整条 Signal，
    而 Signal 又原样带着供应商返回的那个 dict，字典是不可哈希的。

    三段合起来才够用：同一条帖可以摊出好几个候选（发帖人一个、被 @ 的
    每家一个），同一家公司也可以在同一条帖里从两条通道各出一次。

    撞键是可能的（同一条帖、同一条通道、同一个身份键出现两次，被 @ 的
    实体列表里有重复项时就会），但撞了没有害处：这个键只用来查「这个
    候选落进了哪个簇」，而撞键的两个候选身份键相同，必定落进同一个簇。
    """
    return (candidate.signal.external_id, candidate.lead_type, candidate.company.identity_key)


def _attribute(
    pairs: Sequence[tuple[QuerySpec, _QueryHarvest]],
    analysis: _RunAnalysis,
    *,
    qualified_score: int,
) -> tuple[DiscoveryHit, ...]:
    """把整跑的公司这一侧的结果，按每条 query 的命中集合分回去。

    输入：(query, 它第一段的产物) 的配对，只含采集成功的那些；第二段的结果；
          过线分
    输出：这一跑的全部命中流水，按 query 顺序、每条 query 内部按 lead_id 排序

    这个函数不碰库也不碰模型，同样的输入永远给同样的输出。它唯一的副作用是
    就地把每条 query 的 counters 后六个数填上 —— counters 那个对象从第一段
    一路带过来，本来就是「每个节点往上面加数」的用法。

    为什么需要这一步：第二段是跨 query 做的，公司归并之后就分不清哪家是
    哪条 query 带来的了。而 query_executions 是 per-query 的账，proven 池
    60% 的预算按它排名。所以要拿「这条 query 留下了哪些候选」反查回去。

    反查靠的是簇里的候选：每个簇都记着自己是由哪些候选归并出来的，
    倒过来建一张「候选 -> lead_id」的索引就行。不能拿候选自己的身份键
    去算它属于哪个簇 —— 弱身份的候选会被并进强身份的簇，键不一样。

    输入长这样（接着前两段那个例子，今天跑了两条 query）
    ────────────────────────────────────────────────────

        pairs   两个配对，第一项是 query、第二项是它第一段的产物

            (QuerySpec(query_id=30,
                       text='Kickstarter AND (microphone OR audio)', ...),
             _QueryHarvest(
                 kept_signals=(p-1, p-2, p-3),
                 candidates=(Chatterly, Solara Audio, Lumen),
                 signal_ids={"p-1": 318, "p-2": 319, "p-3": 205},
                 counters=ExecutionCounters(
                     raw_result_count=5, billed_item_count=5,
                     signal_dropped_count=2, candidate_dropped_count=2,
                     drop_reasons={"no_relevance_signal": 1,
                                   "seen_hash_prior_run": 1,
                                   "company_page_author": 1,
                                   "supplier_headline": 1})))

            (QuerySpec(query_id=31,
                       text='wearable AND (microphone OR audio OR voice)', ...),
             _QueryHarvest(
                 kept_signals=(p-6, p-7, p-8),
                 candidates=(Solara Audio, Vantek Labs, Kessel Mics),
                 signal_ids={"p-6": 320, "p-7": 321, "p-8": 322},
                 counters=ExecutionCounters(
                     raw_result_count=6, billed_item_count=6,
                     signal_dropped_count=3, candidate_dropped_count=1,
                     drop_reasons={"no_relevance_signal": 2,
                                   "seen_hash_in_run": 1,
                                   "mention_fanout": 1})))

            两件事值得留意。一是 query 31 的 drop_reasons 里有一条
            seen_hash_in_run，那就是跨 query 重叠露头的地方：它采到了
            query 30 已经留下的一篇帖，而记的是「跑内」不是「跨天」。
            二是两条 query 都摊出了 Solara Audio，但来自不同的帖 ——
            同一篇帖不会出现两次，第二次会被上面那条规则挡掉。

            pairs 只含采集成功的 query。今天要是有第三条采集挂了，它压根
            不在这里面，它那条执行由 run_discovery 单独收尾（记 failed，
            只查两条无条件的不变式，因为它的分流三堆本来就凑不齐）。

        analysis   第二段的返回值，四样事实都在里面

            clusters  (Chatterly, Solara Audio, Lumen, Vantek Labs, Kessel Mics)
            saved     (SavedLead(lead_id=501, novel=True,  evidence_rows=1),
                       SavedLead(lead_id=502, novel=True,  evidence_rows=2),
                       SavedLead(lead_id=503, novel=False, evidence_rows=1),
                       SavedLead(lead_id=504, novel=False, evidence_rows=1),
                       SavedLead(lead_id=505, novel=False, evidence_rows=1))
                      顺序跟 clusters 一一对应
            states    {501: LeadState(RESEARCHABLE, None, 0),
                       502: LeadState(RESEARCHABLE, None, 0),
                       503: LeadState(RESEARCHABLE, 58,   0),
                       504: LeadState(EXCLUDED,     31,   2),
                       505: LeadState(COOLING,      44,   1)}
            scored    {"name:chatterly": Assessment(score=71),
                       "name:lumen":   Assessment(score=52)}
                      Solara Audio 不在里面，它被丢弃了
            discarded {"li_id:100000001": ["We just wrapped our campaign"]}
            merged_away  ()

        qualified_score  60

        注意 Solara Audio 的证据有两条（evidence_rows=2），因为两条 query
        各带来一条。那正是跨 query 归并要拿到的东西。

    query 30 的账：三家逐个交代
    ───────────────────────────

        每一家都要说清它算进了哪几个计数，一个都不能只活在数字里 ——
        「这个 3 是哪三家」答不上来的时候，账错了也看不出来。

            501 Chatterly       要分析  头一回见到  71 分
                              -> unique +1  researchable +1  novel +1
                                 qualified +1
            502 Solara Audio  要分析  头一回见到  被丢弃，没有分数
                              -> unique +1  researchable +1  novel +1
            503 Lumen         要分析  库里早有    52 分，没过 60 线
                              -> unique +1  researchable +1

        503 只进了前两个计数：它不是头一回见到（上次已经发现过），
        分数也没过线，所以 novel 和 qualified 都不算它。合起来是

            unique_company_count     3
            researchable_lead_count  3     501 502 503
            cooldown_skipped_count   0
            excluded_skipped_count   0
            novel_lead_count         2     501 502
            qualified_lead_count     1     501

    query 31 的账：另外三家
    ───────────────────────

            502 Solara Audio  要分析  头一回见到  被丢弃，没有分数
                              -> unique +1  researchable +1  novel +1
            504 Vantek Labs   终态    库里早有    没分析
                              -> unique +1  excluded +1
            505 Kessel Mics   冷却中  库里早有    没分析
                              -> unique +1  cooldown +1

            unique_company_count     3
            researchable_lead_count  1     502
            cooldown_skipped_count   1     505
            excluded_skipped_count   1     504
            novel_lead_count         1     502
            qualified_lead_count     0     502 被丢弃了，没有分数

        504 和 505 没被分析，所以它们不可能有分数，qualified 也就轮不到
        它们。库里那条 CHECK 要的「合格的不多过要分析的」就是这个道理。

    两条账合起来看
    ──────────────

        unique 加起来是 6，而这一跑只发现了 5 家 —— Solara Audio(502) 在
        两条账里各算了一次。这是 per-query 指标的正确口径不是重复计数：
        两条 query 确实各自独立地找到了它。跑级的家数看
        DiscoveryResult.company_count，别去加这一列。

        novel 同理，两条账都记 502 是头一回见到。归给「先跑的那条」是错的
        —— 那取决于 selector 今天把谁排在前面，同样的库状态会因为排序不同
        给出不同的归因，而这一列决定 proven 排名。

    输出：一个元组，六行命中流水
    ───────────────────────────

        (DiscoveryHit(query_id=30, lead_id=501, novel=True),
         DiscoveryHit(query_id=30, lead_id=502, novel=True),
         DiscoveryHit(query_id=30, lead_id=503, novel=False),
         DiscoveryHit(query_id=31, lead_id=502, novel=True),
         DiscoveryHit(query_id=31, lead_id=504, novel=False),
         DiscoveryHit(query_id=31, lead_id=505, novel=False))

        行序是「先按 pairs 里 query 的顺序，同一条 query 内按 lead_id 升序」。
        502 出现两次，两次的 novel 都是 True —— 它是这一跑头一回进库的，
        这件事跟哪条 query 在问它无关。

        三家没被分析的公司照样有行（504 505 没分析、503 分了但没过线）：
        这张表记的是「这条 query 撞到了哪家公司」，不是「找到了哪些好线索」。
        只记好的话，「这条 query 只会重复捞已知的垃圾」就永远看不出来。

        这张明细是为了回答 query_executions 那几个计数列答不了的问题，
        最要紧的一个是「这家公司是不是只有这条 query 找得到」：
        按 lead_id 分组、数 distinct query_id 等于 1 的那些就是。
        两条高度重叠的 query 每天发现同一批公司时，两边的 novel 都很好看，
        而真相是留一条就够 —— 这是唯一能查出那件事的地方。

    同时就地改掉的东西
    ──────────────────

        六个计数直接填进每条 query 自己那个 counters 对象（就是上面两张
        账），不另外返回。那个对象从第一段一路带过来，前四个数已经填好了，
        这一步补上后六个，接下来 run_discovery 拿它去 finish_execution。

    最后每条 query 的 counters 都跑一遍 check()，把三条不变式在这里就焊住
    （分流三堆之和等于公司数、合格的不多过要分析的、头一回见到的不多过
    公司数）。落库那一步还会再查一遍，但这里查能定位到是哪条 query 的账
    算错了，那边只能说「有一条对不上」。
    """
    #        lead_of：候选 -> 它落进的那个簇对应的 lead_id。
    #        novel_by_lead / qualified：按 lead_id 记的两个事实。
    # novel 用 or 累积，是因为两个簇可能撞进同一行（见 _merge_and_analyze
    # 里去重那一段），那时候只有先 upsert 的那个簇拿到 novel=True。
    lead_of: dict[tuple[str, LeadType, tuple[str, str]], int] = {}
    novel_by_lead: dict[int, bool] = {}
    qualified: set[int] = set()
    for cluster, entry in zip(analysis.clusters, analysis.saved, strict=True):
        for candidate in cluster.candidates:
            lead_of[_candidate_key(candidate)] = entry.lead_id
        novel_by_lead[entry.lead_id] = novel_by_lead.get(entry.lead_id, False) or entry.novel
        assessment = analysis.scored.get(company_key(cluster))
        if assessment is not None and assessment.score >= qualified_score:
            qualified.add(entry.lead_id)

    hits: list[DiscoveryHit] = []
    for spec, harvest in pairs:
        # 这条 query 命中了哪几家，按 lead_id 去重。去重是必须的：同一家公司
        # 可以在这条 query 里从好几条帖、好几条通道各出一次候选，
        # 而 unique_company_count 数的是公司不是候选。
        # 这里查不到键说明 to_clusters 漏了一个候选，那是它不允许发生的事。
        hit_leads = sorted({lead_of[_candidate_key(c)] for c in harvest.candidates})

        tally = Counter(analysis.states[lead_id].verdict for lead_id in hit_leads)
        counters = harvest.counters
        counters.unique_company_count = len(hit_leads)
        counters.researchable_lead_count = tally[TriageVerdict.RESEARCHABLE]
        counters.cooldown_skipped_count = tally[TriageVerdict.COOLING]
        counters.excluded_skipped_count = tally[TriageVerdict.EXCLUDED]
        counters.novel_lead_count = sum(1 for lead_id in hit_leads if novel_by_lead[lead_id])
        counters.qualified_lead_count = sum(1 for lead_id in hit_leads if lead_id in qualified)
        counters.check()

        # 排过序才 extend：集合的迭代顺序不保证，不排的话同样的输入两次跑
        # 会写出行序不同的流水，回归对照就没有立足点了。
        hits += [
            DiscoveryHit(query_id=spec.query_id, lead_id=lead_id, novel=novel_by_lead[lead_id])
            for lead_id in hit_leads
        ]

    return tuple(hits)


def run_discovery(
    queries: Sequence[QuerySpec],
    *,
    adapter: SourceAdapter,
    store: DiscoveryStore,
    llm_client: Any,
    rules: FilterRules,
    facts: dict[str, Any],
    scoring: ScoringConfig,
    cooldown: CooldownConfig,
    max_posts_per_query: int,
    seen_days: int,
    now: datetime,
    skip_cooldown: bool = False,
    persist: bool = True,
) -> DiscoveryResult:
    """跑一轮 discovery：采集、过闸门、归并、分析、落库，返回一份纯数据结果。

    输入：
        queries              今天跑哪几条。scheduled 由 selector 选出来，
                             interactive 可能是 Riley 临时给的一条
        adapter              采集实现。这一层不认识任何供应商字段
        store                落库接口，同时持有这一跑的事务和 run_id
        llm_client           打分用的模型客户端。这一层不读密钥 ——
                             否则纯计算那几段在没有密钥的机器上就测不了
        rules                两级闸门的全套规则。跟三个配置一样从参数进，
                             这样「换一套词表重跑同一批数据」不用改代码
        facts                我们自己的可核实事实，analyzer 判 moq_fit 要用
        scoring              权重、映射表、批大小、重试预算
        cooldown             四档冷却的天数和分档的线
        max_posts_per_query  每条 query 最多采几条。天花板不是地板
        seen_days            跨天 seen 窗口往回看几天
        now                  当前时刻，必须带时区
        skip_cooldown        见下
        persist              见下
    输出：一个 DiscoveryResult

    星号后面的参数只能按名字传。这个函数有一串同类型的参数
    （两个 int、两个配置对象），允许按位置传的话调换两个不会报错，
    只会静默跑出不对的结果。

    now 从参数进不用 datetime.now()：拿历史数据回归时，写死的当前时刻
    才能让两次跑给出同样的 recency 和同样的冷却期。

    skip_cooldown 让「这次要不要花钱分析」这道分流整个让路，冷却中的和
    终态的一起放行。它服务的是 interactive 那条路上「我就要看这家」——
    人已经明确点名了一家公司，系统的记账不该拦她，而一次一家也不会
    造成成本问题。scheduled 永远传 False。
    这时 cooldown_skipped 和 excluded_skipped 都记 0，分流三堆之和
    仍然等于公司总数，库里那条 CHECK 照样成立。

    persist=False 不是「什么都不写」，而是「同样的写路径全走一遍，最后
    整个丢掉」。interactive 探索性地跑一轮时用它。这个参数在函数体里
    不产生任何分支，直接交给 store.finish(commit=persist) ——
    「这里不写、那里也不写」的判断散在五六个地方，总会漏掉一处，
    而且「这家公司是不是头一回见到」「它在不在冷却期」都要先当作真的
    写进去了才算得对。

    这个函数被 scheduled 和 interactive 调用的是同一份实现，签名里
    queries / skip_cooldown / persist 这三个位置就是为此留的：
    按「只服务 scheduled」写（自己读配置、自己决定冷却、自己落库）的话，
    interactive 那一步要把它推倒重写。

    输入长这样（今天 selector 选了三条 query）
    ─────────────────────────────────────────

        queries  (QuerySpec(query_id=30, text='Kickstarter AND (...)', ...),
                  QuerySpec(query_id=31, text='wearable AND (...)', ...),
                  QuerySpec(query_id=29, text='looking for a hardware ...', ...))

        adapter     一个 HarvestApiPostsAdapter
        store       一个绑在这一跑上的 store，store.run_id == 77
        llm_client   一个 OpenAI 客户端
        rules        config/filters.yaml
        facts        config/facts.json
        scoring      config/scoring.yaml
        cooldown     config/discovery.yaml 的分数线和 cooldown 那一段
        max_posts_per_query  40
        seen_days    10
        now          datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
        skip_cooldown / persist  都是默认值（scheduled 那条路）

    第 0 步 取跨天记忆
    ──────────────────

        prior_seen = store.seen_content_hashes(days=10)，比如 2000 个摘要。
        整跑只取一次，之后逐条 query 传下去；run_seen 从空集合开始，
        每跑完一条 query 把它留下的信号的摘要并进去。

        只并留下来的那些：被闸门砍掉的信号不该占住它那条摘要，否则同一段
        正文的第二条会因为「见过了」被丢，而第一条其实也没留下。

    第 1 步 一条一条 query 走第一段
    ──────────────────────────────

        每条 query 三件事：先插一行 running 的执行记录，跑第一段，
        然后按成没成分两条路。这个例子里前两条成了、第三条采集挂了：

            query 30  start_execution -> 执行记录 id=140
                      _collect_one_query 正常返回
                      run_seen 并进它留下的 3 条信号的摘要
            query 31  start_execution -> 141
                      正常返回。它的 drop_reasons 里有一条 seen_hash_in_run，
                      那就是撞上 query 30 刚留下的摘要
            query 29  start_execution -> 142
                      adapter 抛 RuntimeError('Apify run failed: 502')
                      -> 当场 finish_execution(142, FAILED, 半截计数, 错误原文)
                      -> 不调 mark_ran
                      -> 不 raise，继续往下走

        为什么单条 query 失败不掀桌子：采集超时和限流是常事，而另外两条
        已经花钱采到的数据没有理由跟着一起丢。为什么它不调 mark_ran：
        那一列的语义是「最近一次跑成功」，写上去这条 query 会白吃一整个
        cadence 冷却期，而报表上只显示「今天没轮到它」。

        半截计数是有用的：这条 query 的执行记录里 raw_result_count 可能是
        40（采集成功了、归一化那步才炸），也可能是 0（采集本身挂了）。
        两者查起来完全不同，所以计数对象由这里造、传进去填。

    第 2 步 跨 query 归并、分析、归因
    ─────────────────────────────────

        拿成功的那两条走 _merge_and_analyze 和 _attribute，然后把命中流水
        写进库。这三步包在一个 try 里，任何一处抛异常就整跑回滚。

        _attribute 里的 counters.check() 也在这个 try 里：账算错了宁可
        回滚，不要把一份对不上的账提交进去 —— 那批数字要拿去决定
        proven 池 60% 的预算。

    第 3 步 收尾
    ────────────

            finish_execution(140, COMPLETED, 填满的计数)
            mark_ran(30, ran_at=now)
            finish_execution(141, COMPLETED, 填满的计数)
            mark_ran(31, ran_at=now)
            finish(commit=True)

        执行记录跨过整个第一段停在 running，到这里才收尾。因为
        researchable / cooldown_skipped / novel / qualified 这四个数只有
        跨 query 归并之后才算得出来。

    输出长这样
    ──────────

        DiscoveryResult(
            run_id=77,
            status=RunStatus.PARTIAL,          三条里两条成了
            per_query=(QueryOutcome(query_id=30, execution_id=140,
                                    status=COMPLETED, counters=..., error=None),
                       QueryOutcome(query_id=31, execution_id=141,
                                    status=COMPLETED, counters=..., error=None),
                       QueryOutcome(query_id=29, execution_id=142,
                                    status=FAILED, counters=半截的,
                                    error="RuntimeError: Apify run failed: 502")),
            scored={"name:chatterly": Assessment(score=71),
                    "name:lumen":   Assessment(score=52)},
            lead_ids={"name:chatterly": 501, "li_id:100000001": 502,
                      "name:lumen": 503, "name:vantek labs": 504,
                      "li_id:88123": 505},
            discarded={"li_id:100000001": ["We just wrapped our campaign"]},
            warnings=("query 29 采集失败：RuntimeError: Apify run failed: 502",))

        per_query 的顺序跟传进来的 queries 一致，成功失败混在一起 ——
        报表要按「今天跑了哪几条」来读，不是按「哪几条成了」。

        company_count 从 lead_ids 算，是 5。别去加 per_query 里那三个
        unique_company_count（那三个数加起来是 6，Solara Audio 在两条账里
        各算了一次，那是 per-query 指标的正确口径）。

    四种收场，以及库里每张表分别留下什么
    ────────────────────────────────────

    [A] 全都跑成了  ->  status=COMPLETED，提交

        例：两条 query 都跑通。哪怕一条信号都没采到，那也算跑通 ——
        「今天这条词没搜到东西」是结果，不是故障。

        query_executions  两行。status 从 'running' 改成 'completed'，
                          finished_at 写上时刻，error 留 null，
                          十个计数列一次写满
        queries           两行的 last_run_at 更新成 now。走的是
                          greatest(coalesce(last_run_at, now), now)，
                          只往前不往后 —— 两次 run 时间上重叠时，
                          后收尾的那个不会把它往回拨
        signals           这两条 query 留下来的信号，有多少写多少，
                          可能是 0 行
        leads             归并出来的公司，身份四列全写、status 默认 'new'
        lead_evidence     每家公司挂几条证据，(lead_id, content_hash) 唯一
        lead_discovery_hits  「哪条 query 撞到了哪家公司」的流水
        runs              这一行是 trigger 插的，也由 trigger 按返回的
                          status 收尾，不在这个函数里动

        返回  per_query 两条都是 completed，warnings 空

    [B] 一条都没采成  ->  status=FAILED，照样提交

        例：唯一那条 query 的 adapter 抛了 'Apify run failed: 502'。

        query_executions  一行，status='failed'，error 记
                          'RuntimeError: Apify run failed: 502'，
                          计数是崩之前那半截（采集本身就挂了的话全是 0；
                          采回来 40 条、归一化那步才炸的话
                          raw_result_count 是 40）
        queries           last_run_at 一动不动。那一列的语义是
                          「最近一次跑成功」，写上去这条 query 会白吃
                          一整个 cadence 冷却期，而报表上只显示
                          「今天没轮到它」
        signals 及以下     一行都没有

        照样提交，是因为库里这时只有那几行 failed 的执行记录：它们是
        有价值的痕迹（明天排查「昨天为什么一条线索都没有」就靠它），
        而且一条信号都没落，污染不到跨天 seen。

        返回  status=FAILED，warnings 里写明哪条 query 因为什么失败

    [C] 有成有败  ->  status=PARTIAL，提交

        例：三条 query，中间那条读超时。

        query_executions  三行：两行 completed、一行 failed
        queries           只有成功那两条的 last_run_at 更新
        signals 及以下     只有成功那两条采到的东西

        返回  per_query 的顺序仍然是传进来的 30 / 31 / 29，失败那条混在
              中间不排到最后 —— 报表要按「今天跑了哪几条」来读

        失败那条 query 在库里不会留半截数据，而且这一条是天然成立的、
        不靠额外清理：第一段只在最后一步才写库（insert_signals），
        在那之前挂掉等于什么都没写。而如果是 insert_signals 自己挂了，
        那是库层面的错 —— Postgres 里一条语句报错之后整个事务就废了，
        紧接着写 failed 那一行同样会失败，于是走 [D] 整跑回滚。
        所以「一条 query 失败了，它已经成功的那部分也不该进库」
        在两种情况下都成立。

    [D] 第二段抛异常  ->  回滚，异常原样抛出去，不返回结果

        例：analyzer 用完两个重试预算还是拿不到可用回答；或者
        save_clusters 撞了约束；或者上面说的事务已经废掉。

        库里什么都没有。连 trigger 插的那行 runs 也没了 —— 它跟这一跑的
        数据在同一个事务里。所以整跑失败时这一跑在库里不留任何痕迹，
        痕迹在进程日志里。这是「一跑一个事务」的必然结果，不是漏了什么。
        trigger 拿到异常之后不要再去 finish_run，那一行已经不存在了。

        这一档刻意不返回一个 status=FAILED 的结果：采集失败是预期内的事、
        有账可记，所以它是数据；第二段炸了是「不该发生」的事，而且回滚
        之后库里什么都没有、没有账可返回。让它是异常，调用方才会留下堆栈。

    为什么第二段失败要整跑回滚，而不是留下第一段的成果
    ──────────────────────────────────────────────────

    先说清楚「第二段失败」是什么。它不是「模型判错了」，也不是「某几家
    答不好」—— 那两类在 analyzer 内部就处理掉了（错误分三层、两个重试
    预算、反复答不好的进 discarded 只进日志和计数）。能抛到这一层的
    只剩「重试也没用」那一类：限流一直不退，或者库炸了。

    留下第一段的成果，直觉上像是省了钱。实际上省不到：采集的钱是按
    「发起一次采集」花的，明天那一跑照样要把同一批 query 重新采一遍，
    库里有没有昨天那几行 signals 不影响账单一分钱。省下的只是几个 INSERT。

    而留下来要付两笔代价。一笔是跨天 seen 被污染：那批 content_hash 进了
    窗口，第二天同一批帖被当重复丢掉，它们指向的公司再也发现不了，
    而报表上一切正常。另一笔是要修这个污染，就得把 seen 改成「只认跑完整
    的那些 run 的信号」—— 那是个更准确的口径，但要多一个 join，还要回答
    「partial 那一跑的信号算不算处理过」这个新问题。

    所以取舍是：付两笔复杂度，换一个零收益的东西。

    换个角度看，「允许第二段失败」这件事其实已经允许了，只是失败的粒度
    不是「保留一半」，而是「今天这一跑没有产出」。而今天没有产出本来就是
    可接受的答案 —— 配额是天花板不是地板，「今天只有 2 条」是正常回答，
    「今天 0 条，因为模型那边一直限流」也是。

    真要保住第一段的成果，正确做法不是提交一半，是做「只重跑第二段」的
    续跑：留着 signals，第二天用它们直接从归并开始、跳过采集，那才真省钱。
    它的前置正是上面那条 —— seen 必须先改成只认跑完整的 run，否则续跑
    之前那批信号已经把第二天的采集毒了。现在不做：一跑两条 query 大约两毛
    钱，为省两毛钱引入一个新入口和一套「哪些 run 可以续」的状态不划算。
    """
    # 一个外层守卫：任何逃逸出去的异常都先回滚，再把异常原样抛出去。
    #
    # 它不只是给第二段用的。第一段那个 except 里还要往库里写一行 failed 的
    # 执行记录，而如果异常本来就来自库（连接断了、撞了约束），那一句同样会
    # 失败 —— Postgres 里一条语句报错之后，同一个事务里后面每一句都会被拒。
    # 那种情况下这一跑什么都做不成，唯一正确的收场就是回滚。没有这个守卫的
    # 话，异常会带着一个开着的、已经废掉的事务跑出去，收拾它的责任落到
    # 调用方头上，而调用方并不知道我们写到哪儿了。
    finished = False
    try:
        # 第 0 步。整跑取一次跨天记忆，之后逐条 query 传下去。
        prior_seen = store.seen_content_hashes(days=seen_days)
        run_seen: set[str] = set()

        #        runs：每条 query 一条记录，顺序跟传进来的 queries 一致。
        # 成功的 harvest 有值，失败的是 None 而 error 有值，两者互斥。
        runs: list[_QueryRun] = []

        for spec in queries:
            # 计数对象在这里造，传进去填 —— 这样第一段崩在半路时，
            # 累加到一半的数仍然在我们手上，能写进那行 failed 的执行记录。
            counters = ExecutionCounters()
            execution_id = store.start_execution(query_id=spec.query_id)
            try:
                harvest = _collect_one_query(
                    spec,
                    adapter=adapter,
                    store=store,
                    rules=rules,
                    prior_seen=prior_seen,
                    run_seen=frozenset(run_seen),
                    max_posts_per_query=max_posts_per_query,
                    counters=counters,
                )
            except Exception as err:
                # 捕 Exception 而不是某几个具体类型：这一段调的是采集实现，
                # 而那是最脆也最会换的一层，它抛什么不该由这里假定。
                # 类型名一起写进 error，否则「502」和「解析失败」在库里
                # 长得一样。
                reason = f"{type(err).__name__}: {err}"
                store.finish_execution(
                    execution_id,
                    status=ExecutionStatus.FAILED,
                    counters=counters,
                    error=reason,
                )
                runs.append(
                    _QueryRun(
                        spec=spec,
                        execution_id=execution_id,
                        counters=counters,
                        error=reason,
                    )
                )
                continue

            # 只并留下来的信号的摘要，理由见 docstring 第 0 步。
            run_seen |= {signal.content_hash for signal in harvest.kept_signals}
            runs.append(
                _QueryRun(
                    spec=spec, execution_id=execution_id, counters=counters, harvest=harvest
                )
            )

        ok = [run for run in runs if run.harvest is not None]
        warnings = [
            f"query {run.spec.query_id} 采集失败：{run.error}"
            for run in runs
            if run.harvest is None
        ]

        if not ok:
            # 收场 B：一条都没采成。库里只有几行 failed 的执行记录，
            # 提交它们是有价值的痕迹，而且一条信号都没落，
            # 污染不到跨天 seen。
            #
            # finished 标在调用之前：finish 自己抛的话不该再被下面那个
            # except 调第二次，store.finish 是一跑一次的。
            finished = True
            store.finish(commit=persist)
            return DiscoveryResult(
                run_id=store.run_id,
                status=RunStatus.FAILED,
                per_query=tuple(run.outcome() for run in runs),
                scored={},
                lead_ids={},
                discarded={},
                warnings=tuple(warnings),
            )

        # 第 2 步。归并、打分、归因、写命中流水。任何一处抛异常都由外层
        # 守卫回滚，一个字都不留 —— 只留信号不留公司的话，那批 content_hash
        # 会进跨天 seen，第二天同一批帖被当重复丢掉，它们指向的公司再也
        # 发现不了。为什么不允许「留一半」，见 docstring 最后一节。
        analysis = _merge_and_analyze(
            [run.harvest for run in ok],
            store=store,
            llm_client=llm_client,
            facts=facts,
            scoring=scoring,
            cooldown=cooldown,
            now=now,
            skip_cooldown=skip_cooldown,
        )
        hits = _attribute(
            [(run.spec, run.harvest) for run in ok],
            analysis,
            qualified_score=cooldown.qualified_score,
        )
        store.record_discovery_hits(hits)

        # 第 3 步。mark_ran 只给成功的那些，那一列的语义是「最近一次跑成功」。
        for run in ok:
            store.finish_execution(
                run.execution_id, status=ExecutionStatus.COMPLETED, counters=run.counters
            )
            store.mark_ran(run.spec.query_id, ran_at=now)

        if analysis.merged_away:
            warnings.append(
                "这几家的两条身份通道撞进了库里同一行，只分析了一次："
                + "、".join(analysis.merged_away)
            )

        finished = True
        store.finish(commit=persist)
        return DiscoveryResult(
            run_id=store.run_id,
            status=RunStatus.COMPLETED if len(ok) == len(runs) else RunStatus.PARTIAL,
            per_query=tuple(run.outcome() for run in runs),
            scored=analysis.scored,
            lead_ids=analysis.lead_ids,
            discarded=analysis.discarded,
            warnings=tuple(warnings),
        )
    except Exception:
        if not finished:
            store.finish(commit=False)
        raise
