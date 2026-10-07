"""models 包的不变式测试。

这些不是「为了有测试而写的测试」—— 每一条对应一个已经想清楚、
而且一旦被改坏就会静默出错的地方（库里多一行公司、指标算错、幻觉放过去）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from models import (
    ActorType,
    Assessment,
    Candidate,
    CandidateDropReason,
    CompanyCluster,
    CompanyRef,
    DeterministicScores,
    ExecutionCounters,
    IdentityUnresolvable,
    LeadType,
    LlmAssessment,
    RawSignal,
    RepostedFrom,
    Signal,
    SignalDropReason,
    SourceType,
    linkedin_company_slug,
    locate_quote,
    normalize_company_name,
    unverified_quotes,
)

# ─────────────────────────────────────────────────────────── 公司身份


def test_identity_key_ladder_order():
    """梯度顺序：数字 id > slug > 域名 > 名字。

    顺序错了不会报错，只会让同一家公司在不同批次里落到不同的键上 ——
    症状是库里同一家公司两行，而且极难发现。
    """
    URL = "https://www.linkedin.com/company/northfield-inc/"

    full = CompanyRef("North Field Technologies", "100000001", URL, "northfield.example")
    assert full.identity_key == ("li_id", "100000001")

    no_id = CompanyRef("North Field Technologies", None, URL, "northfield.example")
    assert no_id.identity_key == ("li_slug", "northfield-inc")

    only_domain = CompanyRef("North Field Technologies", None, None, "https://WWW.NorthField.example/about")
    assert only_domain.identity_key == ("domain", "northfield.example")

    only_name = CompanyRef("North Field Technologies, Inc.", None, None, None)
    assert only_name.identity_key == ("name", "north field technologies")


def test_identity_unresolvable_raises_instead_of_guessing():
    """没有任何可用身份 → 显式炸掉。

    绝不允许在这里加「用名字凑一个」的兜底：编出来的键会在
    dedup 阶段把两家不相干的公司并成一家。
    """
    with pytest.raises(IdentityUnresolvable):
        _ = CompanyRef("   ", None, None, None).identity_key


def test_weak_identity_flag():
    assert CompanyRef("Lumen", None, None, None).is_weak_identity is True
    assert CompanyRef("Lumen", "123", None, None).is_weak_identity is False


def test_normalize_company_name_strips_legal_suffix_only():
    """只剥法律后缀。

    剥 "Technologies" 会把 North Field Technologies 和 North Field 错并成一家。
    """
    assert normalize_company_name("Lumen Inc.") == "lumen"
    assert normalize_company_name("HE Manufacturing (M) SDN BHD") == "he manufacturing m"
    assert normalize_company_name("North Field Technologies") == "north field technologies"
    assert normalize_company_name("North Field") == "north field"
    assert normalize_company_name("North Field Technologies") != normalize_company_name("North Field")


def test_linkedin_slug_extraction_does_not_guess():
    assert linkedin_company_slug("https://www.linkedin.com/company/northfield-inc/") == "northfield-inc"
    assert linkedin_company_slug("https://linkedin.com/company/Lumen?trk=x") == "lumen"
    # 个人页不是公司页 —— 抽不到就返回 None，不要退化成拿路径最后一段当 slug
    assert linkedin_company_slug("https://www.linkedin.com/in/some-person/") is None


def test_merge_fills_gaps_and_refuses_different_companies():
    """归并时一条候选可能只有 slug、另一条只有域名，合起来身份才完整。"""
    a = CompanyRef("Lumen", None, "https://www.linkedin.com/company/lumen/", None)
    b = CompanyRef("Lumen", None, "https://www.linkedin.com/company/lumen/", "lumen.example")
    assert a.merge(b).domain == "lumen.example"

    other = CompanyRef("NAVA", None, "https://www.linkedin.com/company/nava/", None)
    with pytest.raises(ValueError):
        a.merge(other)


# ─────────────────────────────────────────────────────────── 信号与归并


def _reposted(
    text: str,
    actor_type: ActorType = ActorType.COMPANY,
    external_id: str = "orig-1",
) -> RepostedFrom:
    """造一条「被转发的原帖」。默认作者是公司主页，转发通道的价值就在这里。"""
    return RepostedFrom(
        external_id=external_id,
        url="https://www.linkedin.com/posts/orig",
        text=text,
        actor_name="ZENTRAai",
        actor_headline="Industrial technology",
        actor_url="https://www.linkedin.com/company/altynexai/",
        actor_type=actor_type,
        actor_company=CompanyRef("ZENTRAai", "200000018", None, None),
        mentioned_companies=(),
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def _raw(
    text: str,
    external_id: str = "1",
    actor_type: ActorType = ActorType.PROFILE,
    reposted_from: RepostedFrom | None = None,
    query_search: str = "wearable AND (microphone OR audio OR voice)",
    actor_headline: str = "Founder",
) -> RawSignal:
    return RawSignal(
        source="harvestapi_posts",
        source_type=SourceType.LINKEDIN_POST,
        external_id=external_id,
        url="https://www.linkedin.com/posts/x",
        text=text,
        query_search=query_search,
        actor_name="Someone",
        actor_headline=actor_headline,
        actor_url="https://www.linkedin.com/in/someone/",
        actor_type=actor_type,
        actor_company=None,
        mentioned_companies=(),
        reposted_from=reposted_from,
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
        raw={},
    )


def test_signal_from_raw_copies_every_field():
    """加字段忘了传 → 这条会立刻炸，而不是留一个 None 到下游。"""
    raw = _raw("we are looking for a manufacturing partner")
    sig = Signal.from_raw(raw, content_hash="abc", matched_terms=("microphone",), fact_hash="abc")
    assert sig.external_id == raw.external_id
    assert sig.actor_headline == raw.actor_headline
    assert sig.observed_at == raw.observed_at
    assert sig.content_hash == "abc"
    assert sig.matched_terms == ("microphone",)


def test_quotable_texts_covers_both_halves_of_a_repost():
    """转发有两段原文，闸门的取材范围必须两段都算上。

    实测最典型的一条：转发者只写了 84 字「So cool to see what began as a
    dorm room project make it to a Time Square billboard」，而原帖是
    Songscription 那条 981 字的公司公告，被 @ 的公司也只在原帖里。
    只取 self.text 的话，这条线索的全部内容都不在可引用范围内。
    """
    plain = _raw("我自己发的")
    assert plain.quotable_texts == ("我自己发的",)

    shared = _raw("So cool to see this", reposted_from=_reposted("big news: Songscription"))
    assert shared.quotable_texts == ("So cool to see this", "big news: Songscription")


def _cluster(ref: CompanyRef, *signals: Signal, lead_type: LeadType = LeadType.MENTIONED):
    """按一批信号造一个簇。簇里装的是候选，所以每条证据都带着自己的通道。"""
    return CompanyCluster(
        ref=ref,
        candidates=[Candidate(company=ref, lead_type=lead_type, signal=s) for s in signals],
    )


def test_cluster_keeps_which_channel_each_piece_of_evidence_came_from():
    """簇必须能回答「这一条证据是从哪条通道来的」，不只是「有过哪些通道」。

    这是 LeadType 存在的全部理由：第一人称的证据和从第三方口中得知的证据，
    开场句根本不能共用。只存一个通道集合的话，写话术时拿着某条原文却
    不知道它是哪一类，开场句照样会写错，等于存了也白存。
    """
    ref = CompanyRef("Lumen", "1", None, None)
    mine = Signal.from_raw(
        _raw("我们在找代工厂", "a"),
        content_hash="H1",
        matched_terms=("x",),
        fact_hash="H1",
    )
    theirs = Signal.from_raw(
        _raw("恭喜 Lumen 众筹成功", "b"),
        content_hash="H2",
        matched_terms=("x",),
        fact_hash="H2",
    )
    cluster = CompanyCluster(
        ref=ref,
        candidates=[
            Candidate(company=ref, lead_type=LeadType.HEADLINE, signal=mine),
            Candidate(company=ref, lead_type=LeadType.MENTIONED, signal=theirs),
        ],
    )

    assert cluster.lead_types == {LeadType.HEADLINE, LeadType.MENTIONED}
    # 关键在这里：每条证据仍然找得到自己的通道
    by_row = {c.signal.row_key: c.lead_type for c in cluster.candidates}
    assert by_row[mine.row_key] == LeadType.HEADLINE
    assert by_row[theirs.row_key] == LeadType.MENTIONED


def test_cluster_counts_one_row_once_even_from_two_channels():
    """同一条信号经两条通道进同一个簇：候选留两条，证据只算一行。

    公司主页发帖又 @ 了自己就是这种情况。两个候选都是真的（一个说这家
    公司自己发了帖，另一个说帖子里 @ 了它），但作为证据它只是一行数据，
    不该让 corroboration 读成 2。
    """
    ref = CompanyRef("launchLAB", "1", None, None)
    one = Signal.from_raw(
        _raw("我们公司发的帖", "a"),
        content_hash="H1",
        matched_terms=("x",),
        fact_hash="H1",
    )
    cluster = CompanyCluster(
        ref=ref,
        candidates=[
            Candidate(company=ref, lead_type=LeadType.AUTHOR, signal=one),
            Candidate(company=ref, lead_type=LeadType.MENTIONED, signal=one),
        ],
    )

    assert len(cluster.candidates) == 2
    assert cluster.lead_types == {LeadType.AUTHOR, LeadType.MENTIONED}
    assert len(cluster.signals) == 1
    assert cluster.corroboration == 1


def test_row_key_keeps_the_same_post_from_two_queries_apart():
    """同一帖被两条 query 搜到是两行，归因不同，不能按一行算。

    归因决定哪条 query 值不值得继续跑，只按帖子 id 认会抹掉其中一条。
    """
    a = Signal.from_raw(
        _raw("同一个帖子", "post-1", query_search="wearable AND audio"),
        content_hash="H1",
        matched_terms=("x",),
        fact_hash="H1",
    )
    b = Signal.from_raw(
        _raw("同一个帖子", "post-1", query_search="Kickstarter AND audio"),
        content_hash="H1",
        matched_terms=("x",),
        fact_hash="H1",
    )

    assert a.external_id == b.external_id
    assert a.row_key != b.row_key
    # 但它们说的是同一件事，corroboration 仍然只算一次
    assert a.fact_hash == b.fact_hash


def test_event_at_uses_the_original_post_not_the_repost():
    """recency 必须量「事件何时发生」，不是「何时有人在谈」。

    用 observed_at 的话，一条老消息每被转发一次就重新变新 ——
    而转发恰恰是老消息才有的待遇，等于给陈旧信号加分。
    """
    plain = _raw("我自己发的")
    assert plain.event_at == plain.observed_at

    shared = _raw("So cool to see this", reposted_from=_reposted("原帖正文"))
    assert shared.observed_at == datetime(2026, 8, 21, tzinfo=UTC)
    assert shared.event_at == datetime(2026, 8, 20, tzinfo=UTC)


def test_unverified_quotes_accepts_quote_from_reposted_original():
    """引用原帖正文是合法的，不能被判成幻觉。

    这条防的是一个会静默丢线索的回归：转发时内容在原帖里，LLM 从原帖引用
    完全正确，而如果闸门只比对转发者那句话，合法引用会被判成幻觉，
    调用方按规矩把整条丢掉 —— 错得毫无痕迹。
    """
    cluster = _cluster(
        CompanyRef("Songscription", "200000012", None, None),
        Signal.from_raw(
            _raw(
                "So cool to see this",
                reposted_from=_reposted("big news: Songscription is becoming Play Anything!"),
            ),
            content_hash="H1",
            matched_terms=("Songscription",),
            fact_hash="H1",
        ),
    )
    real = LlmAssessment(
        icp_fit=8,
        signal_strength=7,
        seniority=6,
        moq_fit=7,
        analysis="原帖是公司自己发的改名公告",
        evidence_quotes=("Songscription is becoming Play Anything",),
    )
    assert unverified_quotes(real, cluster) == []

    # 两段都不含的照样要被抓出来，别为了放过原帖把闸门整个放松了
    fake = LlmAssessment(
        icp_fit=8,
        signal_strength=7,
        seniority=6,
        moq_fit=7,
        analysis="编的",
        evidence_quotes=("they are raising a Series A",),
    )
    assert unverified_quotes(fake, cluster) == ["they are raising a Series A"]


def test_corroboration_counts_distinct_content_not_signals():
    """按 content_hash 去重后再数，否则联播会把这一维刷高。

    同一篇被 9 个账号转发 → 9 条 signal，但只有 1 个独立事实。
    """
    cluster = _cluster(
        CompanyRef("Lumen", "1", None, None),
        Signal.from_raw(
            _raw("same story", "a"),
            content_hash="H1",
            matched_terms=("x",),
            fact_hash="H1",
        ),
        Signal.from_raw(
            _raw("same story", "b"),
            content_hash="H1",
            matched_terms=("x",),
            fact_hash="H1",
        ),
        Signal.from_raw(
            _raw("другой", "c"),
            content_hash="H2",
            matched_terms=("x",),
            fact_hash="H2",
        ),
    )
    assert len(cluster.signals) == 3
    assert cluster.corroboration == 2


# ─────────────────────────────────────────────────────────── 防幻觉


def test_unverified_quotes_catches_paraphrase():
    """引用必须是原文子串。改写得再像也算幻觉。

    这一条杀掉大部分幻觉，别放松成模糊匹配。
    """
    cluster = _cluster(
        CompanyRef("Lumen", "1", None, None),
        Signal.from_raw(
            _raw("Now the hard part: finding a manufacturing partner for a 500-unit run."),
            content_hash="H1",
            matched_terms=("manufacturing",),
            fact_hash="H1",
        ),
    )
    llm = LlmAssessment(
        icp_fit=8, signal_strength=9, seniority=7, moq_fit=9,
        analysis="刚众筹完，明说要找制造伙伴",
        evidence_quotes=(
            "finding a manufacturing partner",          # 真子串
            "they are searching for a factory partner",  # 改写 → 必须被抓出来
        ),
    )
    bad = unverified_quotes(llm, cluster)
    assert bad == ["they are searching for a factory partner"]

    ok = Assessment(llm=llm, det=DeterministicScores(recency=9, reachability=6, corroboration=2), score=81)
    assert ok.score == 81


# ─────────────────────────────────────────────────────────── 计数不变式


def _counters(**kw) -> ExecutionCounters:
    base = dict(
        raw_result_count=120, billed_item_count=240,
        unique_company_count=10,
        researchable_lead_count=6, cooldown_skipped_count=3, excluded_skipped_count=1,
        novel_lead_count=4, qualified_lead_count=2,
    )
    base.update(kw)
    return ExecutionCounters(**base)


def test_counters_ok():
    _counters().check()


def test_counters_catch_triage_mismatch():
    """三种去向加起来必须等于 unique_company_count。

    对不上说明某个节点漏加数了 —— 早炸比库里躺着错数据好，
    因为这些数字决定每天 60% 的预算怎么分。
    """
    with pytest.raises(ValueError, match="分流数对不上"):
        _counters(cooldown_skipped_count=2).check()


def test_counters_catch_impossible_qualified():
    with pytest.raises(ValueError, match="不可能超过"):
        _counters(qualified_lead_count=7).check()


def test_counters_catch_novel_overflow():
    with pytest.raises(ValueError, match="不可能超过"):
        _counters(novel_lead_count=11).check()


def test_check_always_skips_what_only_holds_after_a_full_run():
    """崩在半路的那一跑，分流和 drop_reasons 本来就会对不上，不该拿它们卡。

    计数是各节点分别加的，崩在中间就必然缺一截。库里那条 triage 约束也
    自带「不是 completed 就免检」的前提，两边这样才对得上。
    """
    half_done = _counters(cooldown_skipped_count=2)
    with pytest.raises(ValueError, match='分流数对不上'):
        half_done.check()
    half_done.check_always()


def test_check_always_still_catches_the_two_unconditional_ones():
    """这两条不管跑成没跑成都成立，所以 failed 那一路也要查。

    库里那两条 CHECK 是无条件的。这边放行、库里拒绝的话，抛出来只有一个
    约束名，而这个函数存在的理由就是给出「哪个数对不上」的中文说明。
    """
    with pytest.raises(ValueError, match='不可能超过'):
        _counters(qualified_lead_count=7).check_always()
    with pytest.raises(ValueError, match='不可能超过'):
        _counters(novel_lead_count=11).check_always()


def test_counters_drop_reasons_must_reconcile():
    c = _counters(signal_dropped_count=2, candidate_dropped_count=1)
    c.bump_drop(SignalDropReason.NO_RELEVANCE_SIGNAL)
    c.bump_drop(SignalDropReason.SEEN_HASH_IN_RUN)
    with pytest.raises(ValueError, match="drop_reasons 合计"):
        c.check()
    c.bump_drop(CandidateDropReason.SUPPLIER_HEADLINE)
    c.check()


def test_drop_reason_enums_stay_disjoint():
    """两个枚举刻意分开，别有人图省事合并成一个。

    合并之后「公司主页帖」就能被写成整条丢弃，而那会杀掉
    「加速器发帖提到真买方」这类线索 —— 发帖人不是买方，被提到的公司是。
    """
    assert not ({str(r) for r in SignalDropReason} & {str(r) for r in CandidateDropReason})


# ─────────────────────────────────── 职位串也算可引用的原文


def _headline_cluster(text: str, headline: str) -> CompanyCluster:
    """造一条「正文是一段、职位串是另一段」的簇。"""
    return _cluster(
        CompanyRef("North Field Technologies", "100000001", None, None),
        Signal.from_raw(
            _raw(text, actor_headline=headline),
            content_hash="H1",
            matched_terms=("microphone",),
            fact_hash="H1",
        ),
    )


def test_a_quote_from_the_speaker_headline_is_not_a_hallucination():
    """引职位串里的话不算编造。

    实测出来的真损失（2026-09-02 那次回放跑）：一条创始人自己发的帖，
    正文讲跟 SingAlong 合作，而模型引的是他职位串里那句产品自述。闸门原先
    只认正文，于是整家公司被判成幻觉丢掉 —— 而那是标准 ICP 买方
    （做带麦的卡拉OK硬件、还在做原型），丢之前还多花了一次重问。

    判据是「这句话是不是这个人自己公开写下的、而且我们手上有原件」，
    不是「它在哪个字段里」。而且 prompt 本来就把职位串给模型看了。
    """
    cluster = _headline_cluster(
        "Seeing this from the SingAlong team means a lot. The division of labour is the whole point.",
        "Founder & CEO, North Field Technologies | Seratone is a plug-and-play karaoke "
        "that turns any speaker into a studio | Patent",
    )
    quoted = LlmAssessment(
        icp_fit=8,
        signal_strength=6,
        seniority=9,
        moq_fit=8,
        analysis="创始人自己发帖，做带麦的卡拉OK硬件，还在做原型",
        evidence_quotes=("Seratone is a plug-and-play karaoke that turns any speaker into a studio",),
    )
    assert unverified_quotes(quoted, cluster) == []
    assert (
        locate_quote("plug-and-play karaoke that turns any speaker into a studio", cluster)
        == "plug-and-play karaoke that turns any speaker into a studio"
    )


def test_the_gate_did_not_get_loose_when_headlines_were_allowed():
    """正文和职位串都没有的那句照样是编造。

    放宽的是取材范围，不是判据。这一条焊住「多了一个来源」没有变成
    「什么都能过」——闸门是冷启动的生死线，它松掉之后没有任何报错。
    """
    cluster = _headline_cluster(
        "Seeing this from the SingAlong team means a lot.",
        "Founder & CEO, North Field Technologies | Seratone is a plug-and-play karaoke",
    )
    fake = LlmAssessment(
        icp_fit=8,
        signal_strength=7,
        seniority=6,
        moq_fit=7,
        analysis="编的",
        evidence_quotes=("they are raising a Series A",),
    )
    assert unverified_quotes(fake, cluster) == ["they are raising a Series A"]


def test_body_text_wins_when_the_same_sentence_sits_in_both():
    """同一句话同时在正文和职位串里时，还回来的是正文那一段。

    两处的写法可能不一样（职位串里常带 | 分隔符和大小写口号），而存进
    Assessment 的那一段以后要抄进给客户的邮件。取材顺序是正文优先，
    所以这件事是确定的，不取决于字典或集合的迭代顺序。
    """
    sentence = "Seratone turns any speaker into a studio"
    cluster = _headline_cluster(
        f"Big update. {sentence}. Shipping in Q4.",
        f"Founder & CEO | {sentence.upper()} | Patent",
    )
    assert locate_quote(sentence, cluster) == sentence


def test_an_empty_headline_is_not_a_source():
    """供应商不给职位串时，空串不许进取材范围。

    空串留在里面会给「`"" in text` 恒为真」那类洞多一个入口。
    """
    cluster = _headline_cluster("Some post body", "")
    signal = cluster.signals[0]
    assert signal.quotable_headlines == ()


def test_the_original_authors_headline_counts_too():
    """转发时原帖作者的职位串也算 —— prompt 里那一段证据也带着它。"""
    cluster = _cluster(
        CompanyRef("ZENTRAai", "1", None, None),
        Signal.from_raw(
            _raw("So cool to see this", reposted_from=_reposted("big news")),
            content_hash="H1",
            matched_terms=("x",),
            fact_hash="H1",
        ),
    )
    signal = cluster.signals[0]
    assert signal.quotable_headlines == ("Founder", "Industrial technology")
    assert locate_quote("Industrial technology", cluster) == "Industrial technology"


def test_off_category_is_only_a_hard_zero():
    """off_category 只认 icp_fit 恰好是 0，1 分不算。

    0 分和 1 分在语义上不一样：1 分是「沾一点边」，0 分是「完全不是
    这门生意」。判成 0 的后果重得多 —— 分数被封顶、公司进终态永不复查，
    所以这条边界不许滑动。
    """
    from models.scoring import LlmAssessment

    def llm(icp):
        return LlmAssessment(
            icp_fit=icp,
            signal_strength=8,
            seniority=8,
            moq_fit=8,
            analysis="x",
            evidence_quotes=("y",),
        )

    assert llm(0).off_category is True
    assert llm(1).off_category is False
    assert llm(10).off_category is False


def test_a_straight_apostrophe_still_finds_the_curly_one_in_the_original():
    """原文用的是弯撇号和短破折号，模型抄成了键盘上的直撇号和连字符：要找得回来，
    而且还回来的是原文那一段，带着原来的弯撇号。

    输入   原文   "Spotify\u2019s end\u2013to\u2013end audio push"
           引用   "Spotify's end-to-end audio push"
    输出   "Spotify\u2019s end\u2013to\u2013end audio push"

    不折这两种字符的话，这句会被判成编造，重问还是这样就整家丢掉。而它一个词都没改。
    """
    original = "Spotify\u2019s end\u2013to\u2013end audio push is driving demand"
    cluster = _cluster(
        CompanyRef("Lumen", "1", None, None),
        Signal.from_raw(_raw(original), content_hash="H1", matched_terms=("audio",), fact_hash="H1"),
    )

    expected = "Spotify\u2019s end\u2013to\u2013end audio push"
    assert locate_quote("Spotify's end-to-end audio push", cluster) == expected
    # 反过来也一样：原文是直撇号，模型写成了弯撇号
    plain = _cluster(
        CompanyRef("Lumen", "1", None, None),
        Signal.from_raw(
            _raw("we're shipping in May"), content_hash="H2", matched_terms=("x",), fact_hash="H2"
        ),
    )
    assert locate_quote("we\u2019re shipping in May", plain) == "we're shipping in May"


def test_folding_punctuation_does_not_let_a_rewritten_word_through():
    """只折符号的写法，不折词。原文是 we are，模型写成 we're，那是改了原话，照样拦下。"""
    cluster = _cluster(
        CompanyRef("XREAL", "1", None, None),
        Signal.from_raw(
            _raw("we are beyond excited to push the limits of this incredible hardware"),
            content_hash="H1",
            matched_terms=("hardware",),
            fact_hash="H1",
        ),
    )
    assert locate_quote("we're beyond excited to push the limits", cluster) is None
