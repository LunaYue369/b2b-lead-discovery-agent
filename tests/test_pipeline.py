"""pipeline 编排的测试。假的采集、假的库、假的模型客户端，不连网不连库。

这个文件焊的是编排层，而编排层的价值几乎全在异常路径上：

    一条 query 采集挂了，另外两条的成果还在不在
    第二段炸了，有没有把「见过但没变成公司」的信号留在库里
    库层面的错让事务废掉之后，还写不写得进那行 failed 的记录
    失败那条 query 的 last_run_at 有没有被误更新

这四种拿真库和真供应商都造不出来 —— 没法让 Apify 在第三条 query 上超时，
也没法让模型在第二批炸。所以它们只能用假实现来测，这也是 pipeline 走
DiscoveryStore 协议而不是直接拿游标的决定性理由（见 persistence/base.py）。

分工线要记住：真 SQL、身份归并四级、库里的 CHECK 约束由
tests/test_lead_db.py 和 scripts/verify_persistence.py 各自焊着，
下面这个假 store 不负责证明那些东西对。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from discovery.analyzer import load_facts, load_scoring
from discovery.cooldown import load_cooldown_config
from discovery.pipeline import run_discovery
from discovery.prefilter import load_rules
from models import (
    ActorType,
    CompanyRef,
    ExecutionStatus,
    LeadState,
    LeadStatus,
    QueryPool,
    QuerySpec,
    RawSignal,
    RunStatus,
    SavedLead,
    SourceType,
    TriageVerdict,
)

NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)

# 每条测试用的帖子正文里都有这一句，模型的引用就引它 —— 防幻觉那道闸门
# 要求引用是原文的子串，假客户端也得照这条规矩来，不然测的就不是同一件事。
QUOTE = "we need a manufacturing partner"


# ────────────────────────────────────────────────────────────── 假实现


class FakeStore:
    """内存版 store。

    三条语义必须照真的做，否则测的不是同一件事：

        insert_signals    撞 external_id 要返回原来那一行的 id，不新建
        save_clusters     按身份键 upsert，头一回见到才 novel=True
        finish_execution  落库前跑一遍计数自查（库里那三条 CHECK 的 Python 侧）

    identity_index 可以预置，用来造「库里早就有这家公司」和「两个身份档
    撞进同一行」这两种局面。explode_in 让指定的方法抛异常，用来造库层面
    的错 —— 那是唯一能测「事务废掉之后怎么收场」的办法。
    """

    def __init__(
        self,
        *,
        prior_seen: frozenset[str] = frozenset(),
        states: dict[int, LeadState] | None = None,
        identity_index: dict[tuple[str, str], int] | None = None,
        explode_in: set[str] | None = None,
    ) -> None:
        self.run_id = 77
        self.prior_seen = prior_seen
        self.states_by_lead = dict(states or {})
        self.identity_index = dict(identity_index or {})
        self.explode_in = set(explode_in or ())

        self.seen_days: int | None = None
        self.signals: dict[str, int] = {}  # external_id -> signals.id
        self.evidence: list[tuple[int, str]] = []  # (lead_id, content_hash)
        self.assessments: list[SimpleNamespace] = []
        self.hits: list = []
        self.marked: list[int] = []
        self.executions: list[dict] = []
        self.committed: bool | None = None

        self._next_signal_id = 300
        self._next_lead_id = 500

    def _explode(self, name: str) -> None:
        if name in self.explode_in:
            raise RuntimeError(f"{name} 挂了")

    def seen_content_hashes(self, *, days: int) -> frozenset[str]:
        self.seen_days = days
        return self.prior_seen

    def start_execution(self, *, query_id: int) -> int:
        self._explode("start_execution")
        execution_id = 140 + len(self.executions)
        self.executions.append(
            {"id": execution_id, "query_id": query_id, "status": ExecutionStatus.RUNNING}
        )
        return execution_id

    def finish_execution(self, execution_id, *, status, counters, error=None) -> None:
        self._explode("finish_execution")
        # 库里那三条 CHECK 在 Python 侧的同一份自查。放在这里而不是让测试
        # 各自断言，是为了每条测试都自动过一遍：账对不上的时候，出错的地方
        # 是造成它的那次调用，不是三十行之后的某个断言。
        counters.check_always()
        if status is ExecutionStatus.COMPLETED:
            counters.check()
        row = next(r for r in self.executions if r["id"] == execution_id)
        row.update(status=status, counters=counters, error=error)

    def insert_signals(self, signals) -> dict[str, int]:
        self._explode("insert_signals")
        out: dict[str, int] = {}
        for signal in signals:
            if signal.external_id not in self.signals:
                self.signals[signal.external_id] = self._next_signal_id
                self._next_signal_id += 1
            out[signal.external_id] = self.signals[signal.external_id]
        return out

    def save_clusters(self, clusters, *, signal_ids) -> tuple[SavedLead, ...]:
        self._explode("save_clusters")
        saved: list[SavedLead] = []
        for cluster in clusters:
            key = cluster.ref.identity_key
            novel = key not in self.identity_index
            if novel:
                self.identity_index[key] = self._next_lead_id
                self._next_lead_id += 1
            lead_id = self.identity_index[key]

            rows = 0
            for candidate in cluster.candidates:
                pair = (lead_id, candidate.signal.content_hash)
                if pair not in self.evidence:
                    self.evidence.append(pair)
                    rows += 1
            saved.append(SavedLead(lead_id=lead_id, novel=novel, evidence_rows=rows))
        return tuple(saved)

    def lead_states(self, lead_ids) -> dict[int, LeadState]:
        self._explode("lead_states")
        default = LeadState(TriageVerdict.RESEARCHABLE, None, 0)
        return {lead_id: self.states_by_lead.get(lead_id, default) for lead_id in lead_ids}

    def save_assessment(
        self, lead_id, *, assessment, status, cooldown_until, strikes, analyzed_at
    ) -> None:
        self.assessments.append(
            SimpleNamespace(
                lead_id=lead_id,
                score=assessment.score,
                status=status,
                cooldown_until=cooldown_until,
                strikes=strikes,
            )
        )

    def record_discovery_hits(self, hits) -> None:
        self.hits.extend(hits)

    def mark_ran(self, query_id, *, ran_at) -> None:
        self.marked.append(query_id)

    def finish(self, *, commit: bool) -> None:
        assert self.committed is None, "finish 是一跑一次的，被调了第二次"
        self.committed = commit

    # ── 测试读起来方便的几个视图 ─────────────────────────────────

    def execution(self, query_id: int) -> dict:
        return next(r for r in self.executions if r["query_id"] == query_id)

    def counters(self, query_id: int):
        return self.execution(query_id)["counters"]


class FakeAdapter:
    """按 query_id 给预置结果的假采集。值是异常时就抛出来，用来造采集失败。"""

    name = "fake"

    def __init__(self, plan: dict[int, list[RawSignal] | Exception]) -> None:
        self._plan = plan
        self._billed = 0

    def fetch(self, spec: QuerySpec, *, max_items: int) -> list[RawSignal]:
        planned = self._plan[spec.query_id]
        if isinstance(planned, Exception):
            raise planned
        self._billed = len(planned)
        return list(planned)

    def billed_items(self) -> int:
        return self._billed


class EchoClient:
    """把问到的每一家都照答一遍的假客户端。

    它从发出去的那条 user 消息里把 company_key 抠出来，逐个答一行 ——
    这样「问了谁就答谁」自动成立，不用测试自己去猜 analyzer 分了几批、
    每批装了谁。scores 可以按 key 指定分数，没指定的走默认。

    这跟「不要 mock 一个假响应再断言它等于那个假响应」不冲突：那条针对
    打分结果（只能真跑一遍看分辨力），这里断言的是编排 —— 谁被送进了
    模型、账怎么记、崩了怎么收尾。
    """

    def __init__(self, *, dims: dict[str, dict] | None = None, explode: bool = False) -> None:
        self._dims = dims or {}
        self._explode = explode
        self.asked: list[list[str]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        if self._explode:
            raise RuntimeError("rate limited")
        user = kwargs["messages"][1]["content"]
        keys = re.findall(r'"company_key": "([^"]+)"', user)
        self.asked.append(keys)
        rows = []
        for key in keys:
            row = {
                "company_key": key,
                "icp_fit": 8,
                "signal_strength": 7,
                "seniority": 6,
                "moq_fit": 7,
                "analysis": "looks like a real buyer",
                "evidence_means": "the quoted line is this company saying it",
                "evidence_quotes": [QUOTE],
            }
            row.update(self._dims.get(key, {}))
            rows.append(row)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps({"assessments": rows}), refusal=None
                    ),
                )
            ]
        )


# ──────────────────────────────────────────────────────── 造数据的小工具


def spec(query_id: int, text: str = "Kickstarter AND (microphone OR audio)") -> QuerySpec:
    return QuerySpec(
        query_id=query_id,
        text=text,
        source_type=SourceType.LINKEDIN_POST,
        pool=QueryPool.CANDIDATE,
        cadence_days=1,
        filters={},
    )


def post(
    external_id: str,
    *,
    mentions: tuple[CompanyRef, ...],
    query: str = "Kickstarter AND (microphone OR audio)",
    text: str | None = None,
) -> RawSignal:
    """一条被 @ 了公司的帖子。

    作者是个人、职位串是 Founder（解析不出公司），所以每条帖只产出
    被 @ 的那几个候选，数目可数。正文里带 QUOTE 那一句，供模型引用。

    默认正文里嵌了 external_id，这样两条帖的 content_hash 天然不同。
    不嵌的话两条正文一字不差，第二条会被「见过了」那条规则挡掉 ——
    想测那个行为的测试自己传同一个 RawSignal 对象进来。
    """
    return RawSignal(
        source="fake",
        source_type=SourceType.LINKEDIN_POST,
        external_id=external_id,
        url=f"https://www.linkedin.com/posts/{external_id}",
        text=text or f"Update from {external_id}: our campaign wrapped and {QUOTE}.",
        query_search=query,
        actor_name="Someone",
        actor_headline="Founder",
        actor_url="https://www.linkedin.com/in/someone/",
        actor_type=ActorType.PROFILE,
        actor_company=None,
        mentioned_companies=mentions,
        reposted_from=None,
        observed_at=datetime(2026, 8, 30, tzinfo=UTC),
        raw={},
    )


LUMEN = CompanyRef("Lumen", "111", None, None)
SONORA = CompanyRef("Sonora", "222", None, None)


def run(queries, adapter, store, *, client=None, **overrides):
    kwargs = dict(
        adapter=adapter,
        store=store,
        llm_client=client if client is not None else EchoClient(),
        rules=load_rules(),
        facts=load_facts(),
        scoring=load_scoring(),
        cooldown=load_cooldown_config(),
        max_posts_per_query=40,
        seen_days=10,
        now=NOW,
    )
    kwargs.update(overrides)
    return run_discovery(queries, **kwargs)


# ─────────────────────────────────────────────────── 正常一跑，跨 query 归并


def test_two_queries_finding_one_company_each_get_the_credit():
    """两条 query 撞上同一家公司：只分析一次，但两条账里各记一次。

    这是跨 query 归并的全部意义。逐 query 各归各的话，这家公司会被打两次
    分（花两份钱），而 corroboration 会被拆成两个 1。
    """
    store = FakeStore()
    adapter = FakeAdapter({30: [post("p-1", mentions=(LUMEN,))], 31: [post("p-2", mentions=(LUMEN,))]})

    result = run([spec(30), spec(31)], adapter, store)

    assert result.status is RunStatus.COMPLETED
    # 一家公司，两条证据（两条 query 各带一条），只送了一次模型。
    assert result.company_count == 1
    assert len(store.assessments) == 1
    assert len(store.evidence) == 2

    # 两条账各记一次：per-query 的 unique 之和是 2，而实际只有 1 家。
    for query_id in (30, 31):
        counters = store.counters(query_id)
        assert counters.unique_company_count == 1
        assert counters.researchable_lead_count == 1
        assert counters.novel_lead_count == 1
        assert counters.qualified_lead_count == 1

    # 命中流水两行，同一家公司、两条 query。
    assert [(hit.query_id, hit.novel) for hit in store.hits] == [(30, True), (31, True)]
    assert store.marked == [30, 31]
    assert store.committed is True


def test_a_second_query_hitting_the_same_post_is_attributed_to_this_run():
    """今天两条 query 采到同一篇帖：第二条记「跑内」不是「跨天」。

    合成一个 seen 集合的话，跨 query 重叠会伪装成正常损耗，而那两个数的
    运营含义相反 —— 跑内说明这几条 query 在互相重叠地采同一批帖。
    """
    same = post("p-1", mentions=(LUMEN,))
    store = FakeStore()
    result = run([spec(30), spec(31)], FakeAdapter({30: [same], 31: [same]}), store)

    assert result.status is RunStatus.COMPLETED
    assert store.counters(30).drop_reasons == {}
    assert store.counters(31).drop_reasons == {"seen_hash_in_run": 1}
    # 第二条 query 一条信号都没留下，所以它一家公司都没命中。
    assert store.counters(31).unique_company_count == 0


def test_a_hash_seen_in_an_earlier_run_is_attributed_to_that_run():
    """库里那批摘要撞上了，记的是「跨天」。跟上一条是同一个闸门、不同的账。"""
    only = post("p-1", mentions=(LUMEN,))
    from discovery.normalize import to_signal

    store = FakeStore(prior_seen=frozenset({to_signal(only).content_hash}))
    result = run([spec(30)], FakeAdapter({30: [only]}), store)

    assert store.seen_days == 10
    assert store.counters(30).drop_reasons == {"seen_hash_prior_run": 1}
    assert result.company_count == 0


def test_a_company_already_in_the_database_is_not_novel():
    """库里早有的公司不算「头一回见到」，novel 那一列决定 proven 排名。"""
    store = FakeStore(identity_index={("li_id", "111"): 501})
    result = run([spec(30)], FakeAdapter({30: [post("p-1", mentions=(LUMEN,))]}), store)

    assert result.lead_ids == {"li_id:111": 501}
    assert store.counters(30).unique_company_count == 1
    assert store.counters(30).novel_lead_count == 0
    assert [hit.novel for hit in store.hits] == [False]


def test_a_low_score_is_analyzed_but_does_not_count_as_qualified():
    """分数没过线的照样算 researchable，只是不算 qualified。

    库里那条 CHECK 要的「合格的不多过要分析的」就是这个关系。
    """
    store = FakeStore()
    client = EchoClient(
        dims={"li_id:111": {"icp_fit": 6, "signal_strength": 1, "seniority": 0, "moq_fit": 2}}
    )
    run([spec(30)], FakeAdapter({30: [post("p-1", mentions=(LUMEN,))]}), store, client=client)

    counters = store.counters(30)
    assert counters.researchable_lead_count == 1
    assert counters.qualified_lead_count == 0
    assert store.assessments[0].score < load_cooldown_config().qualified_score
    # 品类对但分数没过线 -> in_range 那一档，状态仍是 new 不是 pushed。
    assert store.assessments[0].status is LeadStatus.NEW
    assert store.assessments[0].cooldown_until == NOW + timedelta(days=14)


# ──────────────────────────────────────────────────────────── 分流那三档


def test_a_cooling_company_is_counted_but_not_analyzed():
    store = FakeStore(
        identity_index={("li_id", "111"): 501},
        states={501: LeadState(TriageVerdict.COOLING, 44, 1)},
    )
    run([spec(30)], FakeAdapter({30: [post("p-1", mentions=(LUMEN,))]}), store)

    counters = store.counters(30)
    assert (counters.unique_company_count, counters.cooldown_skipped_count) == (1, 0 + 1)
    assert counters.researchable_lead_count == 0
    assert store.assessments == []


def test_skip_cooldown_lets_a_terminal_company_through():
    """人明确点名要看某一家时，冷却中的和终态的一起放行。

    代价写在 pipeline 的注释里：一家终态公司这样被重新分析、而这次
    icp_fit 不为 0 的话，它的 status 会从 excluded 变回 new。
    """
    store = FakeStore(
        identity_index={("li_id", "111"): 501},
        states={501: LeadState(TriageVerdict.EXCLUDED, 12, 3)},
    )
    run(
        [spec(30)],
        FakeAdapter({30: [post("p-1", mentions=(LUMEN,))]}),
        store,
        skip_cooldown=True,
    )

    assert store.counters(30).researchable_lead_count == 1
    assert store.counters(30).excluded_skipped_count == 0
    assert store.assessments[0].status is LeadStatus.NEW


def test_two_identity_channels_landing_on_one_row_are_analyzed_once():
    """两个身份档撞进库里同一行时只分析一次，让掉的那个进 warnings。

    两个都分析等于同一家公司花两次钱，而且两次的分都写同一行、后写的
    盖掉先写的 —— 跟「同一家答两次」是同一类静默错。
    """
    # 强簇的公司名归一化成 "lumen technology"，跟弱簇那个键 "lumen" 不相等
    # —— 所以跑内那一步并不到一起（它比的是本跑强簇自己的名字）。
    # 而库里那一行是更早某跑用短名字写下的，两个键都指向它。
    strong = CompanyRef("Lumen Technology", "111", None, None)
    weak = CompanyRef("Lumen", None, None, None)
    store = FakeStore(
        identity_index={("li_id", "111"): 505, ("name", "lumen"): 505},
    )
    result = run(
        [spec(30)],
        FakeAdapter({30: [post("p-1", mentions=(strong,)), post("p-2", mentions=(weak,))]}),
        store,
    )

    assert len(store.assessments) == 1
    assert result.warnings and "撞进了库里同一行" in result.warnings[0]
    # 两个键、一家公司 —— company_count 数的是去重后的 lead_id。
    assert len(result.lead_ids) == 2
    assert result.company_count == 1


# ─────────────────────────────────────────────────────────── 四种收场


def test_one_failed_query_does_not_stop_the_run():
    """一条 query 采集挂了：另外那条照跑，失败那条不更新 last_run_at。

    写上去的话那条 query 会白吃一整个 cadence 冷却期，而报表上只显示
    「今天没轮到它」。
    """
    store = FakeStore()
    adapter = FakeAdapter(
        {30: RuntimeError("Apify run failed: 502"), 31: [post("p-2", mentions=(SONORA,))]}
    )

    result = run([spec(30), spec(31)], adapter, store)

    assert result.status is RunStatus.PARTIAL
    assert [(o.query_id, o.status) for o in result.per_query] == [
        (30, ExecutionStatus.FAILED),
        (31, ExecutionStatus.COMPLETED),
    ]
    assert result.per_query[0].error == "RuntimeError: Apify run failed: 502"
    assert store.marked == [31]
    # 失败那条在库里只留一行 failed 的执行记录，不留任何信号。
    assert store.execution(30)["status"] is ExecutionStatus.FAILED
    assert set(store.signals) == {"p-2"}
    assert store.committed is True


def test_per_query_order_follows_the_input_not_the_outcome():
    """per_query 的顺序跟传进来的一致，失败那条不排到最后。

    报表要按「今天跑了哪几条」来读，不是按「哪几条成了」。
    """
    store = FakeStore()
    adapter = FakeAdapter(
        {
            30: [post("p-1", mentions=(LUMEN,))],
            31: TimeoutError("read timeout"),
            29: [post("p-3", mentions=(SONORA,))],
        }
    )
    result = run([spec(30), spec(31), spec(29)], adapter, store)

    assert [o.query_id for o in result.per_query] == [30, 31, 29]
    assert result.status is RunStatus.PARTIAL


def test_no_query_succeeded_still_commits_the_failure_records():
    """一条都没采成也提交：库里只有几行 failed 的执行记录，那是有价值的痕迹。

    而且一条信号都没落，污染不到跨天 seen —— 这是它能提交的前提。
    """
    store = FakeStore()
    result = run([spec(30)], FakeAdapter({30: RuntimeError("Apify run failed: 502")}), store)

    assert result.status is RunStatus.FAILED
    assert result.company_count == 0
    assert store.committed is True
    assert store.marked == []
    assert store.hits == []
    assert result.warnings == ("query 30 采集失败：RuntimeError: Apify run failed: 502",)


def test_a_stage_two_failure_rolls_back_the_whole_run():
    """第二段炸了整跑回滚，而且异常原样抛出去、不返回结果。

    只留信号不留公司的话，那批 content_hash 会进跨天 seen，第二天同一批帖
    被当重复丢掉，它们指向的公司再也发现不了 —— 而报表上一切正常。
    """
    store = FakeStore(explode_in={"save_clusters"})
    with pytest.raises(RuntimeError, match="save_clusters"):
        run([spec(30)], FakeAdapter({30: [post("p-1", mentions=(LUMEN,))]}), store)

    assert store.committed is False
    assert store.assessments == []


def test_a_model_that_keeps_failing_rolls_back_too():
    """重试预算用完还是拿不到回答，也是整跑回滚。"""
    store = FakeStore()
    with pytest.raises(RuntimeError):
        run(
            [spec(30)],
            FakeAdapter({30: [post("p-1", mentions=(LUMEN,))]}),
            store,
            client=EchoClient(explode=True),
        )

    assert store.committed is False
    assert store.assessments == []


def test_a_dead_transaction_still_rolls_back():
    """库层面的错：写 failed 那一行本身也失败，照样要回滚。

    Postgres 里一条语句报错之后，同一个事务里后面每一句都会被拒。没有外层
    守卫的话，异常会带着一个开着的、已经废掉的事务跑出去，而调用方并不
    知道我们写到哪儿了。
    """
    store = FakeStore(explode_in={"insert_signals", "finish_execution"})
    with pytest.raises(RuntimeError):
        run([spec(30)], FakeAdapter({30: [post("p-1", mentions=(LUMEN,))]}), store)

    assert store.committed is False


def test_persist_false_walks_the_same_path_then_throws_it_away():
    """persist=False 不是「什么都不写」，是「全走一遍最后整个丢掉」。

    「这家公司是不是头一回见到」「它在不在冷却期」都要先当作真的写进去了
    才算得对，跳过写入会让这一跑的结论跟真跑不一样。
    """
    store = FakeStore()
    result = run(
        [spec(30)],
        FakeAdapter({30: [post("p-1", mentions=(LUMEN,))]}),
        store,
        persist=False,
    )

    assert result.status is RunStatus.COMPLETED
    # 写路径全走过了：分数算了、命中流水记了、执行记录收尾了。
    assert len(store.assessments) == 1
    assert len(store.hits) == 1
    assert store.execution(30)["status"] is ExecutionStatus.COMPLETED
    # 只是最后整个丢掉。
    assert store.committed is False


# ─────────────────────────────────────────────────────────── 计数对账


def test_counters_reconcile_across_the_funnel():
    """一条 query 的十个计数要自洽，这是 finish_execution 落库前那道自查。

    这条测试的价值在于它是端到端的：前四个数在第一段填、后六个在归因那步
    填，两段之间隔着跨 query 归并，而对账要求它们合得上。
    """
    store = FakeStore()
    # 三条帖：一条带被 @ 的公司、一条正文里有无关行业的词、一条什么通道都没有。
    posts = [
        post("p-1", mentions=(LUMEN,)),
        post("p-2", mentions=(), text="Great deal on furniture hardware and cabinet doors."),
        post("p-3", mentions=(), text="Nothing relevant here at all."),
    ]
    run([spec(30)], FakeAdapter({30: posts}), store)

    counters = store.counters(30)
    assert counters.raw_result_count == 3
    assert counters.signal_dropped_count == 2
    assert sum(counters.drop_reasons.values()) == (
        counters.signal_dropped_count + counters.candidate_dropped_count
    )
    assert (
        counters.researchable_lead_count
        + counters.cooldown_skipped_count
        + counters.excluded_skipped_count
        == counters.unique_company_count
        == 1
    )
