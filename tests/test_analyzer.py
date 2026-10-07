"""analyzer 里纯计算那几段的测试。

不调模型。要焊的是「同样的输入永远给同样的分」和「模型乱答时当场炸」，
两件都不需要真请求；而真请求会让这套测试依赖密钥和网络，跑不成回归。
模型那一段的验证方式是拿真数据真跑一遍看分辨力，不是 mock 一个假响应
再断言它等于那个假响应。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from discovery.analyzer import (
    RESPONSE_SCHEMA,
    BatchIdentityMismatch,
    Complaint,
    LlmCallFailed,
    ScoringConfig,
    _brief,
    _check_content,
    _identity_note,
    _retry_note,
    _table_lookup,
    assess_batch,
    build_messages,
    company_key,
    deterministic_scores,
    load_facts,
    load_scoring,
    parse_assessments,
    weighted_score,
)
from models import (
    ActorType,
    AssessmentProblem,
    Candidate,
    CompanyCluster,
    CompanyRef,
    LeadType,
    LlmAssessment,
    RawSignal,
    RepostedFrom,
    Signal,
    SourceType,
)

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _signal(text: str, *, external_id: str, days_ago: int) -> Signal:
    raw = RawSignal(
        source="harvestapi_posts",
        source_type=SourceType.LINKEDIN_POST,
        external_id=external_id,
        url="https://www.linkedin.com/posts/x",
        text=text,
        query_search="Kickstarter AND audio",
        actor_name="Someone",
        actor_headline="Founder",
        actor_url="https://www.linkedin.com/in/someone/",
        actor_type=ActorType.PROFILE,
        actor_company=None,
        mentioned_companies=(),
        reposted_from=None,
        observed_at=NOW - timedelta(days=days_ago),
        raw={},
    )
    return Signal.from_raw(
        raw,
        content_hash=f"H-{external_id}",
        matched_terms=("audio",),
        fact_hash=f"F-{external_id}",
    )


def _repost_signal(*, outer: str, inner: str, external_id: str, days_ago: int) -> Signal:
    """一条转发信号。外层是转发者写的那句，里面是原帖，两边作者不是同一个人。"""
    raw = RawSignal(
        source="harvestapi_posts",
        source_type=SourceType.LINKEDIN_POST,
        external_id=external_id,
        url="https://www.linkedin.com/posts/x",
        text=outer,
        query_search="Kickstarter AND audio",
        actor_name="Someone",
        actor_headline="Founder",
        actor_url="https://www.linkedin.com/in/someone/",
        actor_type=ActorType.PROFILE,
        actor_company=None,
        mentioned_companies=(),
        reposted_from=RepostedFrom(
            external_id=f"{external_id}-orig",
            url="https://www.linkedin.com/posts/orig",
            text=inner,
            actor_name="Jane Doe",
            actor_headline="Founder at Chatterly",
            actor_url="https://www.linkedin.com/in/jane/",
            actor_type=ActorType.PROFILE,
            actor_company=None,
            mentioned_companies=(),
            observed_at=NOW - timedelta(days=days_ago + 1),
        ),
        observed_at=NOW - timedelta(days=days_ago),
        raw={},
    )
    return Signal.from_raw(
        raw,
        content_hash=f"H-{external_id}",
        matched_terms=("audio",),
        fact_hash=f"F-{external_id}",
    )


class _ScriptedClient:
    """按剧本答话的假客户端。测的是重问的控制流，不是模型的判断。

    模块开头那条「不 mock 一个假响应再断言它等于那个假响应」针对的是打分
    结果 —— 那个只能拿真数据真跑一遍看分辨力。这里断言的是「第二轮只问了
    不合格的那一家」这类控制流，它没有别的办法能测，而它出错的方式是静默的：
    多问几家只是多花钱，少问几家就是漏线索，两种都不报错。

    users 记下每一轮实际发出去的 user 消息，测试拿它来断言问了谁。
    """

    def __init__(self, rounds: list[list[dict]], *, finish_reason: str = "stop") -> None:
        self._rounds = list(rounds)
        self._finish_reason = finish_reason
        self.users: list[str] = []
        self.kwargs: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs) -> SimpleNamespace:
        self.users.append(kwargs["messages"][1]["content"])
        self.kwargs.append(kwargs)
        rows = self._rounds.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self._finish_reason,
                    message=SimpleNamespace(
                        content=json.dumps({"assessments": rows}), refusal=None
                    ),
                )
            ]
        )


def _cluster(ref: CompanyRef, *signals: Signal) -> CompanyCluster:
    return CompanyCluster(
        ref=ref,
        candidates=[
            Candidate(company=ref, lead_type=LeadType.MENTIONED, signal=s) for s in signals
        ],
    )


def _llm(**kw) -> LlmAssessment:
    base = dict(
        icp_fit=10,
        signal_strength=10,
        seniority=10,
        moq_fit=10,
        analysis="",
        evidence_quotes=(),
    )
    base.update(kw)
    return LlmAssessment(**base)


# ─────────────────────────────────────────────── 映射表


def test_table_lookup_takes_the_first_matching_band():
    """两个方向的表合用一个函数，取的都是「第一个满足的档」。

    升序表问的是「不超过多少天」，降序表问的是「至少几件事」。
    """
    days = ((3, 10), (7, 8), (30, 4))
    assert _table_lookup(days, 1, ascending=True) == 10
    assert _table_lookup(days, 5, ascending=True) == 8
    assert _table_lookup(days, 30, ascending=True) == 4

    counts = ((4, 10), (2, 6), (1, 3))
    assert _table_lookup(counts, 9, ascending=False) == 10
    assert _table_lookup(counts, 2, ascending=False) == 6
    assert _table_lookup(counts, 1, ascending=False) == 3


def test_table_lookup_blows_up_instead_of_returning_zero():
    """表兜不住就炸，不退化成 0 分。

    返回 0 的话，一个配错的表会表现成「这一维所有线索都是 0 分」，
    而那看起来跟「确实都不新」一模一样，几周都发现不了。
    """
    with pytest.raises(ValueError, match="兜不住"):
        _table_lookup(((3, 10), (7, 8)), 99, ascending=True)


# ─────────────────────────────────────────────── 确定性那三维


def test_deterministic_scores_reward_fresh_reachable_corroborated():
    """身份全、事情刚发生、好几条独立证据 —— 三维都该拿高分。"""
    config = load_scoring()
    ref = CompanyRef("Lumen", "100000001", "https://www.linkedin.com/company/lumen/", "lumen.example")
    cluster = _cluster(
        ref,
        _signal("甲事", external_id="a", days_ago=1),
        _signal("乙事", external_id="b", days_ago=9),
    )

    det = deterministic_scores(cluster, config=config, now=NOW)

    assert det.recency == 10  # 按最近那条算，不是按平均
    assert det.reachability == config.reachability["has_domain"]
    assert det.corroboration == 6  # 两件独立的事


def test_deterministic_scores_punish_weak_identity_and_old_events():
    """只有名字、事情是两个月前、只有一条证据 —— 三维都该低。"""
    config = load_scoring()
    cluster = _cluster(
        CompanyRef("某音频厂", None, None, None),
        _signal("旧事", external_id="c", days_ago=60),
    )

    det = deterministic_scores(cluster, config=config, now=NOW)

    assert det.recency == 2
    assert det.reachability == config.reachability["name_only"]
    assert det.corroboration == 3


def test_recency_uses_the_newest_event_not_the_average():
    """一家公司上个月发过、昨天又发一条，时效按昨天算。

    取平均会让老信号把新信号拖下去，而我们找的正是「现在正在发生的事」。
    """
    config = load_scoring()
    ref = CompanyRef("Lumen", "1", None, None)
    old_only = _cluster(ref, _signal("旧", external_id="a", days_ago=60))
    old_and_new = _cluster(
        ref,
        _signal("旧", external_id="a", days_ago=60),
        _signal("新", external_id="b", days_ago=1),
    )

    assert deterministic_scores(old_only, config=config, now=NOW).recency == 2
    assert deterministic_scores(old_and_new, config=config, now=NOW).recency == 10


def test_a_future_timestamp_does_not_sneak_past_the_table():
    """数据源给了未来时刻是脏数据，不是「特别新」，钳到 0 天照样走表。"""
    config = load_scoring()
    cluster = _cluster(CompanyRef("Lumen", "1", None, None), _signal("怪", external_id="a", days_ago=-5))
    assert deterministic_scores(cluster, config=config, now=NOW).recency == 10


# ─────────────────────────────────────────────── 加权


def test_full_marks_is_exactly_one_hundred():
    """七维全满正好 100，说明权重之和确实是 100。"""
    config = load_scoring()
    det = deterministic_scores(
        _cluster(
            CompanyRef("Lumen", "1", "https://www.linkedin.com/company/lumen/", "lumen.example"),
            _signal("甲", external_id="a", days_ago=0),
            _signal("乙", external_id="b", days_ago=0),
            _signal("丙", external_id="c", days_ago=0),
            _signal("丁", external_id="d", days_ago=0),
        ),
        config=config,
        now=NOW,
    )
    assert weighted_score(_llm(), det, config) == 100


def test_a_category_mismatch_cannot_be_rescued_by_the_deterministic_half():
    """品类不对的一律低分，不管证据多新多全。

    icp_fit 是 0 的时候，就算确定性那三维全满，总分也只到 30 ——
    低于任何一档推送阈值。这是「不合格的绝对不放上来」在打分层的落点。
    """
    config = load_scoring()
    det = deterministic_scores(
        _cluster(
            CompanyRef("Google", "1", "https://www.linkedin.com/company/google/", "google.com"),
            *[_signal(f"第 {i} 条", external_id=str(i), days_ago=0) for i in range(4)],
        ),
        config=config,
        now=NOW,
    )
    nothing = _llm(icp_fit=0, signal_strength=0, seniority=0, moq_fit=0)
    assert weighted_score(nothing, det, config) == 30


def test_a_category_mismatch_is_capped_even_when_every_other_dimension_is_strong():
    """品类不对但别的维度很硬的那一档，必须被封顶。

    上面那条测试只证明了「四维全 0」，而 icp_fit 的权重是它自己那一维的
    上限、不是别人的天花板：icp_fit 拿 0 分时剩下六维仍然能挣到过线以上。
    事件很硬、说话人是老板、MOQ 也合得上，只是做的东西跟我们无关的公司
    到处都有 —— 这条测试焊住的就是它们不许靠别的维度过线。
    """
    config = load_scoring()
    det = deterministic_scores(
        _cluster(
            CompanyRef("BVF", "1", "https://www.linkedin.com/company/bvf/", None),
            *[_signal(f"第 {i} 条", external_id=str(i), days_ago=10) for i in range(3)],
        ),
        config=config,
        now=NOW,
    )
    strong_elsewhere = _llm(icp_fit=0, signal_strength=8, seniority=8, moq_fit=8)

    # 不封顶的话这几维加权出来是 40 分以上，会落进给 Riley 标注的边界档
    assert weighted_score(strong_elsewhere, det, config) == config.off_category_cap


def test_the_cap_stays_below_the_annotation_band():
    """封顶值必须低于 review_score，否则品类不对的会挤进 Riley 的标注池。

    两个数在两份配置里（封顶在 scoring.yaml，review_score 在
    discovery.yaml），所以这条约束没有单一的读入口能查。trigger 读完
    全部配置那一步会查，这里焊住仓库里那两份配置本身是对的。
    """
    from triggers.harvest import load_run_config

    assert load_scoring().off_category_cap < load_run_config().review_score


def test_weights_that_do_not_add_to_a_hundred_blow_up():
    """权重之和不是 100 是配置写错，按比例缩放会让人以为分数还是那个分数。"""
    config = load_scoring()
    broken = ScoringConfig(**{**config.__dict__, "llm_weights": {**config.llm_weights, "icp_fit": 24}})
    with pytest.raises(ValueError, match="权重之和"):
        weighted_score(_llm(), deterministic_scores(
            _cluster(CompanyRef("A", "1", None, None), _signal("x", external_id="a", days_ago=0)),
            config=config, now=NOW,
        ), broken)


# ─────────────────────────────────────────────── 解析模型的回答


def _row(key: str, **kw) -> dict:
    base = dict(
        company_key=key,
        icp_fit=5,
        signal_strength=5,
        seniority=5,
        moq_fit=5,
        analysis="ok",
        evidence_means="ok",
        evidence_quotes=[],
    )
    base.update(kw)
    return base


def test_parse_maps_answers_back_by_key_not_by_position():
    """按 company_key 对回去，不按下标。

    模型偶尔会打乱顺序，按下标会把甲公司的判断安到乙公司头上，而且不报错。
    """
    a = _cluster(CompanyRef("Lumen", "111", None, None), _signal("x", external_id="a", days_ago=0))
    b = _cluster(CompanyRef("Sonora", "222", None, None), _signal("y", external_id="b", days_ago=0))
    payload = {"assessments": [_row(company_key(b), icp_fit=9), _row(company_key(a), icp_fit=2)]}

    out, complaints = parse_assessments(payload, [a, b])

    assert out[company_key(a)].icp_fit == 2
    assert out[company_key(b)].icp_fit == 9
    assert complaints == {}


def test_a_missing_company_blows_up_instead_of_disappearing():
    """模型漏答一家就整批作废。漏掉的那家不会出现在报表里，而漏斗数字看起来正常。"""
    a = _cluster(CompanyRef("Lumen", "111", None, None), _signal("x", external_id="a", days_ago=0))
    b = _cluster(CompanyRef("Sonora", "222", None, None), _signal("y", external_id="b", days_ago=0))
    with pytest.raises(BatchIdentityMismatch) as err:
        parse_assessments({"assessments": [_row(company_key(a))]}, [a, b])

    assert err.value.missing == [company_key(b)]
    assert err.value.extra == []


def test_an_invented_company_blows_up():
    """模型多答一家多半是它自己编的，那条会带着编的引用一路走下去。"""
    a = _cluster(CompanyRef("Lumen", "111", None, None), _signal("x", external_id="a", days_ago=0))
    payload = {"assessments": [_row(company_key(a)), _row("li_id:999999")]}
    with pytest.raises(BatchIdentityMismatch) as err:
        parse_assessments(payload, [a])

    assert err.value.extra == ["li_id:999999"]


def test_a_score_out_of_range_is_caught_here_not_at_the_database():
    """0-10 是库里 CHECK 的范围，放过去要到落库那一步才炸，离出错的地方太远。

    但它不像「答错了公司集合」那样整批作废：能定位到是哪一家的哪一维，
    所以只把那一家挑出来重问，别的公司的判断照用。
    """
    a = _cluster(CompanyRef("Lumen", "111", None, None), _signal("x", external_id="a", days_ago=0))
    b = _cluster(CompanyRef("Sonora", "222", None, None), _signal("y", external_id="b", days_ago=0))
    payload = {"assessments": [_row(company_key(a), icp_fit=11, seniority=-1), _row(company_key(b))]}

    out, complaints = parse_assessments(payload, [a, b])

    assert list(out) == [company_key(b)]
    assert [c.detail for c in complaints[company_key(a)]] == ["icp_fit=11", "seniority=-1"]
    assert {c.kind for c in complaints[company_key(a)]} == {AssessmentProblem.SCORE_OUT_OF_RANGE}


def test_the_same_company_answered_twice_blows_up():
    """同一家答两次要炸。

    判重只比集合的话，[A, A, B] 对 {A, B} 是相等的，然后解析时后一行覆盖
    前一行 —— 两个不同的分数，静默取最后一个，报表上完全看不出来。
    """
    a = _cluster(CompanyRef("Lumen", "111", None, None), _signal("x", external_id="a", days_ago=0))
    b = _cluster(CompanyRef("Sonora", "222", None, None), _signal("y", external_id="b", days_ago=0))
    payload = {
        "assessments": [
            _row(company_key(a), icp_fit=9),
            _row(company_key(a), icp_fit=1),
            _row(company_key(b)),
        ]
    }

    with pytest.raises(BatchIdentityMismatch) as err:
        parse_assessments(payload, [a, b])

    assert err.value.duplicated == [company_key(a)]
    assert err.value.missing == [] and err.value.extra == []


def test_an_empty_analysis_is_not_accepted():
    """没有理由的分数 Riley 没法校准，而分数本身看不出这条缺了理由。"""
    a = _cluster(CompanyRef("Lumen", "111", None, None), _signal("x", external_id="a", days_ago=0))

    out, complaints = parse_assessments({"assessments": [_row(company_key(a), analysis="  ")]}, [a])

    assert out == {}
    assert [c.kind for c in complaints[company_key(a)]] == [AssessmentProblem.EMPTY_ANALYSIS]


def test_the_identity_note_lists_the_three_kinds_separately():
    """漏了要补、多了要删、重了要合 —— 三件事不一样，合成一句模型只改一种。"""
    note = _identity_note(
        BatchIdentityMismatch(missing=["li_id:1"], extra=["li_id:2"], duplicated=["li_id:3"])
    )

    assert "Missing" in note and "'li_id:1'" in note
    assert "do not invent" in note and "'li_id:2'" in note
    assert "more than once" in note and "'li_id:3'" in note


def test_a_transport_failure_says_which_batch_it_was():
    """SDK 抛的异常里没有「这是哪一批的哪几家」，而排查就是从那儿开始。

    我们不在这里重试：网络那类 SDK 自己已经退避重试过并放弃了（默认
    max_retries=2），再叠一层只会让重试次数相乘、让定时任务拖更久。
    这一层的活是补上下文。
    """

    class _RateLimited:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._boom))

        def _boom(self, **kwargs):
            err = RuntimeError("Rate limit reached")
            err.status_code = 429
            raise err

    a = _cluster(CompanyRef("Lumen", "111", None, None), _signal("x", external_id="a", days_ago=0))

    with pytest.raises(LlmCallFailed) as err:
        assess_batch([a], facts={}, config=load_scoring(), client=_RateLimited())

    assert err.value.status_code == 429
    assert err.value.company_keys == [company_key(a)]


def test_every_request_carries_our_own_timeout():
    """超时是我们的，重试是 SDK 的。

    SDK 默认 read timeout 是 600 秒，14 批最坏能挂两个多小时，而这是个
    每天跑一次的定时任务 —— 挂在那儿比失败更难发现。
    """
    a = _cluster(
        CompanyRef("Lumen", "111", None, None), _signal("real text", external_id="a", days_ago=0)
    )
    client = _ScriptedClient([[_row(company_key(a), evidence_quotes=["real text"])]])

    assess_batch([a], facts={}, config=load_scoring(), client=client)

    assert client.kwargs[0]["timeout"] == load_scoring().request_timeout_seconds


# ─────────────────────────────────────────────── 喂给模型的那份材料


def test_a_third_party_mention_is_not_marked_as_this_company_speaking():
    """被 @ 出来的公司，发帖人不是它的人 —— seniority 不能照着那个人的职位判。

    实测这一跑 133 家里 96 家只有第三方通道，所以这是主流情况不是边角情况。
    只给一个簇级的 found_via 集合的话，模型看得见「有 mentioned 这条通道」，
    却看不出「这条证据的作者不是这家公司的人」。
    """
    signal = _signal("Samsung's new watches score your heart", external_id="a", days_ago=0)
    cluster = _cluster(CompanyRef("Samsung", "111", None, None), signal)

    item = _brief(cluster)["evidence"][0]

    assert item["speaker"] == "Someone"
    assert item["speaker_is_this_company"] is False


def test_the_author_channel_is_marked_as_this_company_speaking():
    """发帖人自己就是这家公司的人时，那条证据是第一人称的。"""
    signal = _signal("We just wrapped our Kickstarter", external_id="a", days_ago=0)
    ref = CompanyRef("Chatterly", "111", None, None)
    cluster = CompanyCluster(
        ref=ref, candidates=[Candidate(company=ref, lead_type=LeadType.AUTHOR, signal=signal)]
    )

    assert _brief(cluster)["evidence"][0]["speaker_is_this_company"] is True


def test_a_repost_credits_the_original_author_not_the_reposter():
    """转发的两段正文各记各的说话人。

    两段共用转发者的名字是错的：加速器转发被投公司的帖子时，原帖里
    「我们刚做完众筹」会被记在加速器头上，seniority 判到了另一家公司的人身上。
    """
    signal = _repost_signal(
        outer="So cool to see this",
        inner="We just wrapped our Kickstarter",
        external_id="a",
        days_ago=0,
    )
    ref = CompanyRef("Chatterly", "111", None, None)
    cluster = CompanyCluster(
        ref=ref,
        candidates=[Candidate(company=ref, lead_type=LeadType.REPOST_AUTHOR, signal=signal)],
    )

    outer, inner = _brief(cluster)["evidence"]

    assert outer["speaker"] == "Someone"
    assert outer["speaker_is_this_company"] is False
    assert inner["speaker"] == "Jane Doe"
    assert inner["speaker_headline"] == "Founder at Chatterly"
    assert inner["speaker_is_this_company"] is True


# ─────────────────────────────────────────────── 防幻觉那道闸门


def test_an_answer_with_no_quotes_does_not_pass_the_gate():
    """空数组不算过关。

    原先它能合法穿过：unverified_quotes 对空元组返回空列表，看起来跟
    「引用全对」一模一样。于是一条高分、零条原文引用的线索照样能推给 Riley，
    而分项加原文引用正是她校准判断的全部依据。
    """
    cluster = _cluster(
        CompanyRef("Lumen", "111", None, None), _signal("real text", external_id="a", days_ago=0)
    )

    _, problems = _check_content(_llm(evidence_quotes=()), cluster)

    assert [p.kind for p in problems] == ["no_quote"]


def test_a_blank_quote_does_not_count_as_evidence():
    """空串是 `"" in text` 恒为真的漏洞：它过得了子串比对，但它不是证据。

    只用空串一个，不掺空格串 —— 空格串在这条正文里找不到，会先被判成编的，
    那样测到的是另一条支路，恰好绕开要焊的这一条。
    """
    cluster = _cluster(
        CompanyRef("Lumen", "111", None, None), _signal("real text", external_id="a", days_ago=0)
    )

    _, problems = _check_content(_llm(evidence_quotes=("",)), cluster)

    assert [p.kind for p in problems] == ["no_quote"]


def test_a_fabricated_quote_is_reported_verbatim():
    """编的那句原样带回去，模型才看得见自己编的是哪一句。"""
    cluster = _cluster(
        CompanyRef("Lumen", "111", None, None), _signal("real text", external_id="a", days_ago=0)
    )

    _, problems = _check_content(_llm(evidence_quotes=("they raised a Series A",)), cluster)

    assert [(p.kind, p.detail) for p in problems] == [
        ("fabricated_quote", "they raised a Series A")
    ]



def test_a_quote_the_model_tidied_up_is_recovered_not_discarded():
    """模型顺手改了大小写、加了句号、把换行写成空格 —— 那不是编造。

    实测一跑 20 家有 4 家因为这个被判成幻觉丢掉，四句跟原文的相似度都在
    0.989 以上。按幻觉丢掉它们，等于为了防编造扔掉五分之一的真线索。
    四个例子都是那一跑里真出现过的形态。
    """
    # 原文里那个换行是故意的：实测模型常把它写成一个空格。
    text = "Linkwave delivers end-to-end ODM solutions,\nfor enterprise partners."
    cluster = _cluster(
        CompanyRef("Linkwave", "111", None, None), _signal(text, external_id="a", days_ago=0)
    )

    for tidied in (
        "Linkwave delivers end-to-end ODM solutions",          # 逐字
        "Linkwave delivers end-to-end ODM solutions",          # 只差大小写
        "Linkwave delivers end-to-end ODM solutions,",         # 句尾多个逗号
        "ODM solutions, for enterprise partners",              # 换行写成了空格
    ):
        checked, problems = _check_content(_llm(evidence_quotes=(tidied,)), cluster)
        assert problems == [], f"{tidied!r} 被误判成幻觉了"
        # 存下来的是原文那一段，不是模型写的那一段 —— 这一句以后要抄进
        # 给客户的邮件里，模型顺手补的句号不该跟着走
        assert checked.evidence_quotes[0] in text


def test_a_real_fabrication_still_does_not_get_through():
    """放宽的只是大小写、空白和两端标点。换词、多一个词，照样是编造。"""
    text = "Linkwave delivers end-to-end ODM solutions for enterprise partners."
    cluster = _cluster(
        CompanyRef("Linkwave", "111", None, None), _signal(text, external_id="a", days_ago=0)
    )

    for made_up in (
        "Linkwave delivers end-to-end OEM solutions",           # 换了一个词
        "Linkwave delivers ODM solutions",                      # 少了几个词
        "Linkwave is raising a Series A",                       # 整句是编的
        "for enterprise partners Linkwave delivers",            # 调了词序
    ):
        _, problems = _check_content(_llm(evidence_quotes=(made_up,)), cluster)
        assert [p.kind for p in problems] == [AssessmentProblem.FABRICATED_QUOTE], made_up


def test_a_blank_quote_next_to_a_real_one_does_not_trigger_a_retry():
    """空串既不是编造也不是证据，跳过它就行，不该为它多花一次请求。"""
    cluster = _cluster(
        CompanyRef("Lumen", "111", None, None), _signal("real text", external_id="a", days_ago=0)
    )

    checked, problems = _check_content(_llm(evidence_quotes=("", "real text")), cluster)

    assert problems == []
    assert checked.evidence_quotes == ("real text",)


def test_a_quote_from_another_company_is_still_a_fabrication():
    """只跟这一家自己的原文比。拿甲公司的话当乙公司的证据要抓出来 ——
    一批十家里到处都是别家的原文，比对范围松了这道闸门基本就没了。"""
    a = _cluster(
        CompanyRef("Lumen", "111", None, None),
        _signal("Lumen wrapped its Kickstarter", external_id="a", days_ago=0),
    )
    b = _cluster(
        CompanyRef("Sonora", "222", None, None),
        _signal("Sonora shipped its first batch", external_id="b", days_ago=0),
    )

    _, problems = _check_content(_llm(evidence_quotes=("Sonora shipped its first batch",)), a)

    assert [p.kind for p in problems] == [AssessmentProblem.FABRICATED_QUOTE]
    # 同一句放回 b 自己那边是合法的
    assert _check_content(_llm(evidence_quotes=("Sonora shipped its first batch",)), b)[1] == []


def test_the_retry_note_groups_by_kind_and_sorts():
    """按 kind 分节、组内排序 —— 同样的输入永远生成同样的文本。

    temperature 是 0，一模一样的 prompt 会拿到一模一样的坏回答，
    所以重问必须改 prompt 才有意义，而这段就是改动的那部分。
    """
    note = _retry_note(
        {
            "li_id:222": [Complaint("no_quote", "no verbatim quote survived the check")],
            "li_id:111": [
                Complaint("fabricated_quote", "shipping in Q4"),
                Complaint("fabricated_quote", "raising a Series A"),
            ],
        }
    )

    assert note == _retry_note(
        {
            "li_id:111": [
                Complaint("fabricated_quote", "raising a Series A"),
                Complaint("fabricated_quote", "shipping in Q4"),
            ],
            "li_id:222": [Complaint("no_quote", "no verbatim quote survived the check")],
        }
    )
    assert note.index("li_id:111: 'raising a Series A'") < note.index("li_id:111: 'shipping in Q4'")
    assert "NOT verbatim substrings" in note
    assert "without a single usable quote" in note


# ─────────────────────────────────────────────── 重问的控制流


def _two_per_batch() -> ScoringConfig:
    """仓库那份配置，只把 batch_size 换成 2。

    下面几条测的是一批里有两家时的机制：只重问答错的那一家、漏答了整批重问、两种重问各算
    各的预算。线上配置是一次一家（config/scoring.yaml 那一段写了为什么），但这套批内机制
    还在代码里，哪天批大小改回来照样要对，所以测试自己给一个装得下两家的批，不跟着线上配置走
    """
    return ScoringConfig(**{**load_scoring().__dict__, "batch_size": 2})


def test_the_second_round_only_asks_about_the_companies_that_failed():
    """只重问不合格的那几家，已经过关的不再动它。

    不只是省钱，更是止损：已经过关的再问一遍等于重新掷一次骰子，
    一家本来引用也对的公司可能第二轮就编了一句。重问只可能让它变差。
    """
    good = _cluster(
        CompanyRef("Chatterly", "111", None, None), _signal("we wrapped it", external_id="a", days_ago=0)
    )
    bad = _cluster(
        CompanyRef("Sonora", "222", None, None), _signal("they shipped", external_id="b", days_ago=0)
    )
    client = _ScriptedClient(
        [
            [
                _row(company_key(good), evidence_quotes=["we wrapped it"]),
                _row(company_key(bad), evidence_quotes=["raising a Series A"]),
            ],
            [_row(company_key(bad), evidence_quotes=["they shipped"])],
        ]
    )

    settled, discarded = assess_batch(
        [good, bad], facts={}, config=_two_per_batch(), client=client
    )

    assert set(settled) == {company_key(good), company_key(bad)}
    assert discarded == {}
    assert company_key(good) in client.users[0]
    assert company_key(good) not in client.users[1]
    assert company_key(bad) in client.users[1]


def test_a_company_that_keeps_making_things_up_is_handed_back_not_scored():
    """重问完还在编就丢弃，但要说出是哪一家、编了什么。

    不静默丢：丢的是「模型对这家反复答不好」，那本身是关于模型和这条线索的
    信息，数量涨起来说明 prompt 或者证据材料出了问题。
    """
    good = _cluster(
        CompanyRef("Chatterly", "111", None, None), _signal("we wrapped it", external_id="a", days_ago=0)
    )
    bad = _cluster(
        CompanyRef("Sonora", "222", None, None), _signal("they shipped", external_id="b", days_ago=0)
    )
    client = _ScriptedClient(
        [
            [
                _row(company_key(good), evidence_quotes=["we wrapped it"]),
                _row(company_key(bad), evidence_quotes=["raising a Series A"]),
            ],
            [_row(company_key(bad), evidence_quotes=["still made up"])],
        ]
    )

    settled, discarded = assess_batch(
        [good, bad], facts={}, config=_two_per_batch(), client=client
    )

    assert set(settled) == {company_key(good)}
    assert discarded == {company_key(bad): ["still made up"]}


def test_a_wrong_company_set_re_asks_the_whole_batch():
    """答错了公司集合就整批重问 —— 「哪句话是关于谁的」整体存疑。

    跟内容问题相反：内容问题能定位到某一家，身份问题不能，
    剩下那些看起来正常的也不能挑着用。
    """
    a = _cluster(
        CompanyRef("Chatterly", "111", None, None), _signal("we wrapped it", external_id="a", days_ago=0)
    )
    b = _cluster(
        CompanyRef("Sonora", "222", None, None), _signal("they shipped", external_id="b", days_ago=0)
    )
    client = _ScriptedClient(
        [
            [_row(company_key(a), evidence_quotes=["we wrapped it"])],  # 漏答了 b
            [
                _row(company_key(a), evidence_quotes=["we wrapped it"]),
                _row(company_key(b), evidence_quotes=["they shipped"]),
            ],
        ]
    )

    settled, discarded = assess_batch([a, b], facts={}, config=_two_per_batch(), client=client)

    assert set(settled) == {company_key(a), company_key(b)}
    assert discarded == {}
    # 第二轮两家都在，不是只补漏掉的那一家
    assert company_key(a) in client.users[1]
    assert company_key(b) in client.users[1]
    assert "did not cover exactly" in client.users[1]


def test_an_identity_retry_does_not_eat_the_evidence_budget():
    """身份错和引用错各花各的预算，这是这个循环唯一容易写错的地方。

    共用一个数的话会出现这条路：第一轮答错了公司集合、整批重问，第二轮公司
    对上了但引用是编的 —— 这时候预算已经被身份那次吃光，引用那一关一次机会
    都没得到。而这两件事根本没关系。

    这条测试焊的就是「第三次调用还发生了」。
    """
    a = _cluster(
        CompanyRef("Chatterly", "111", None, None),
        _signal("we wrapped it", external_id="a", days_ago=0),
    )
    b = _cluster(
        CompanyRef("Sonora", "222", None, None),
        _signal("they shipped", external_id="b", days_ago=0),
    )
    client = _ScriptedClient(
        [
            # 第 1 次：漏答了 b -> 花掉 identity 的预算，整批重问
            [_row(company_key(a), evidence_quotes=["we wrapped it"])],
            # 第 2 次：公司对上了，但 b 的引用是编的 -> 花掉 content 的预算
            [
                _row(company_key(a), evidence_quotes=["we wrapped it"]),
                _row(company_key(b), evidence_quotes=["raising a Series A"]),
            ],
            # 第 3 次：只问 b，这次它引对了
            [_row(company_key(b), evidence_quotes=["they shipped"])],
        ]
    )

    settled, discarded = assess_batch([a, b], facts={}, config=_two_per_batch(), client=client)

    assert set(settled) == {company_key(a), company_key(b)}
    assert discarded == {}
    assert len(client.users) == 3
    # 第三次只问了 b：a 在第二次就过关了，不该再被掷一次骰子
    assert company_key(a) not in client.users[2]


def test_the_two_budgets_add_up_they_do_not_multiply():
    """最多 1 + identity_retries + content_retries 次调用。

    会相乘的是「每进一个新阶段就把预算重置一遍」那种写法。这里两个计数器
    各自单调递减、都不重置，所以上界是相加的 —— 一次定时任务最坏要花多少
    钱、跑多久，是能算出来的。
    """
    config = load_scoring()
    a = _cluster(
        CompanyRef("Chatterly", "111", None, None),
        _signal("we wrapped it", external_id="a", days_ago=0),
    )
    # 每一轮都编，而且中间还夹一次答错公司集合
    client = _ScriptedClient(
        [
            [_row("li_id:999999", evidence_quotes=["nope"])],  # 身份错
            [_row(company_key(a), evidence_quotes=["made up"])],  # 内容错
            [_row(company_key(a), evidence_quotes=["still made up"])],  # 还是内容错
        ]
    )

    settled, discarded = assess_batch([a], facts={}, config=config, client=client)

    assert settled == {}
    assert discarded == {company_key(a): ["still made up"]}
    assert len(client.users) == 1 + config.identity_retries + config.content_retries


def test_a_company_set_that_stays_wrong_blows_up_after_the_identity_budget():
    """身份错连着两次说明是系统性问题（prompt 太长、批太大、模型太弱），
    再重问也没用，直接抛出去让这一跑失败。"""
    a = _cluster(
        CompanyRef("Chatterly", "111", None, None),
        _signal("we wrapped it", external_id="a", days_ago=0),
    )
    client = _ScriptedClient(
        [
            [_row("li_id:999999", evidence_quotes=["nope"])],
            [_row("li_id:888888", evidence_quotes=["nope"])],
        ]
    )

    with pytest.raises(BatchIdentityMismatch):
        assess_batch([a], facts={}, config=load_scoring(), client=client)

    assert len(client.users) == 1 + load_scoring().identity_retries


def test_a_batch_bigger_than_the_config_blows_up():
    """一次请求判几家是配置说了算。这个前提破了，症状是输出被截断、整批白跑，
    而那看起来像模型抽风，不像配置写错。"""
    clusters = [
        _cluster(CompanyRef(f"C{i}", str(i), None, None), _signal("x", external_id=str(i), days_ago=0))
        for i in range(4)
    ]
    config = ScoringConfig(**{**load_scoring().__dict__, "batch_size": 3})

    with pytest.raises(ValueError, match="超过 batch_size"):
        assess_batch(clusters, facts={}, config=config, client=_ScriptedClient([]))


def test_a_truncated_answer_names_batch_size_instead_of_a_json_error():
    """输出被 max_tokens 截断时，报的是真正的原因。

    直接 json.loads 半个 JSON 报的是「第 8 行少个花括号」，看着像模型抽风，
    实际是 batch_size 配大了 —— 而这两个数是绑在一起的。
    """
    a = _cluster(CompanyRef("Chatterly", "111", None, None), _signal("x", external_id="a", days_ago=0))
    client = _ScriptedClient([[_row(company_key(a))]], finish_reason="length")

    with pytest.raises(LlmCallFailed, match="batch_size") as err:
        assess_batch([a], facts={}, config=load_scoring(), client=client)

    # 出错信息里要能看出是哪一批，排查「今天早报为什么少了 20 家」从这儿开始
    assert err.value.company_keys == [company_key(a)]


# ─────────────────────────────────────────────── prompt


def test_the_prompt_carries_the_facts_whitelist():
    """facts.json 整块进 prompt。硬数字只能引这里的，不给它就只能靠编。"""
    a = _cluster(CompanyRef("Lumen", "1", None, None), _signal("x", external_id="a", days_ago=0))
    user = build_messages([a], load_facts())[1]["content"]

    assert "COMPANY FACTS" in user
    assert "minimum order quantity" in user
    assert company_key(a) in user


def test_a_retry_hands_back_the_quotes_that_were_invented():
    """重写时把编的那几句原样带回去，比笼统说一句「请引用原文」有用得多。"""
    a = _cluster(CompanyRef("Lumen", "1", None, None), _signal("x", external_id="a", days_ago=0))
    note = "NOT verbatim: they are raising a Series A"
    user = build_messages([a], load_facts(), retry_note=note)[1]["content"]

    assert user.endswith(note)


def test_the_schema_requires_every_field():
    """strict 模式要求每个属性都在 required 里，漏一个 OpenAI 会拒。

    焊这一条是因为加字段时最容易忘的就是同步 required，而症状是
    一次请求直接报错，看着像网络问题。
    """
    item = RESPONSE_SCHEMA["schema"]["properties"]["assessments"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False
