"""prefilter 的回归测试。

这里测的不是「函数跑不跑得起来」，是几条一旦破掉就会静默丢线索的性质。
每条测试上面写清楚破掉之后会发生什么 —— 没有那句话的话，后面的人
看到测试挡路只会顺手改断言。

真买方误砍的量化验证不在这里，那要靠 scripts/experiment.py 拿标注跑。
这里守的是规则本身的行为：通道是或不是且、层级有没有搞错、
归因是不是唯一、配置写错会不会当场炸。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from discovery.prefilter import FilterRules, drop_candidates, drop_signals, load_rules
from models import (
    ActorType,
    Candidate,
    CandidateDropReason,
    CompanyRef,
    LeadType,
    RawSignal,
    RepostedFrom,
    Signal,
    SignalDropReason,
    SourceType,
)

RULES = FilterRules(
    require_relevance_signal=True,
    relevance_channels=("matched_terms", "mentioned_companies"),
    drop_seen_content_hash=True,
    hardware_store=("fire door", "CNC machining"),
    supplier_content=("Buying Guide", "How to Choose a"),
    off_topic=("audiobook", "voice actor"),
    drop_company_page_author=True,
    candidate_terms=(("supplier_headline", ("OEM/ODM", "Export Sales")),),
    not_a_buyer=(("413999", "Kickstarter"), ("5597", "Forbes")),
    max_mentions_per_post=4,
)


def _company(name: str, *, company_id: str | None = None) -> CompanyRef:
    return CompanyRef(name=name, linkedin_company_id=company_id, linkedin_url=None, domain=None)


def _signal(
    text: str = "普通正文",
    *,
    external_id: str = "1",
    content_hash: str = "h1",
    matched_terms: tuple[str, ...] = ("audio",),
    mentioned: tuple[CompanyRef, ...] = (),
    headline: str = "Founder",
    actor_type: ActorType = ActorType.PROFILE,
    reposted_from: RepostedFrom | None = None,
) -> Signal:
    """造一条信号。默认是「能过全部规则」的样子，每个测试只改它关心的那一项。

    输入：想改的那几个字段
    输出：一条 Signal

    默认值刻意是干净的：这样测试里出现的每个参数都是这条测试要考的点，
    读的人不用去分辨哪些字段是噪音。
    """
    raw = RawSignal(
        source="harvestapi_posts",
        source_type=SourceType.LINKEDIN_POST,
        external_id=external_id,
        url="https://www.linkedin.com/posts/x",
        text=text,
        query_search="audio AND manufacturer",
        actor_name="Someone",
        actor_headline=headline,
        actor_url="https://www.linkedin.com/in/someone/",
        actor_type=actor_type,
        actor_company=None,
        mentioned_companies=mentioned,
        reposted_from=reposted_from,
        observed_at=datetime(2026, 8, 27, tzinfo=UTC),
        raw={},
    )
    # fact_hash 这里跟 content_hash 取同一个值：闸门的测试不关心「是不是
    # 同一件事」，那个判断在 corroboration 那一层，不在 prefilter。
    return Signal.from_raw(
        raw,
        content_hash=content_hash,
        matched_terms=matched_terms,
        fact_hash=content_hash,
    )


def _candidate(
    company: CompanyRef, lead_type: LeadType, signal: Signal | None = None
) -> Candidate:
    return Candidate(company=company, lead_type=lead_type, signal=signal or _signal())


# ───────────────────────────────────────────────────── 规则一：相关性通道


def test_a_mentioned_company_alone_is_enough_to_stay():
    """被 @ 的公司自己就够放行，不需要正文里也有命中词。

    这条破掉就退回只认字面匹配的老写法，实测那样会砍掉三分之一以上的
    真买方：供应商发帖吹自己帮某家做了耳机，他一个我们的词都不用，
    被点名的那家却正是买方。
    """
    signal = _signal(matched_terms=(), mentioned=(_company("Lumen"),))
    kept, drops = drop_signals([signal], rules=RULES)
    assert kept == (signal,)
    assert drops == ()


def test_matched_terms_alone_is_enough_to_stay():
    """反方向：正文有命中词、没有被 @ 公司，同样放行。

    两条通道是「或」不是「且」。写成且会比只认命中词还严，方向正好反了。
    """
    signal = _signal(matched_terms=("audio",), mentioned=())
    kept, _ = drop_signals([signal], rules=RULES)
    assert kept == (signal,)


def test_no_channel_at_all_gets_dropped():
    """两条通道都不成立才砍，理由是 NO_RELEVANCE_SIGNAL。"""
    signal = _signal(matched_terms=(), mentioned=())
    kept, drops = drop_signals([signal], rules=RULES)
    assert kept == ()
    assert [d.reason for d in drops] == [SignalDropReason.NO_RELEVANCE_SIGNAL]


def test_channels_can_be_narrowed_to_matched_terms_only():
    """通道表真的在起作用：砍到只剩命中词之后，光有被 @ 公司就不够了。

    这是上一条的对照组。没有它的话，「加了 mentioned 通道」和
    「这条规则根本没生效」两种情况在测试里长得一模一样。
    """
    import dataclasses

    narrow = dataclasses.replace(RULES, relevance_channels=("matched_terms",))
    signal = _signal(matched_terms=(), mentioned=(_company("Lumen"),))
    kept, drops = drop_signals([signal], rules=narrow)
    assert kept == ()
    assert [d.reason for d in drops] == [SignalDropReason.NO_RELEVANCE_SIGNAL]


def test_rule_one_can_be_switched_off_entirely():
    """整条关掉时，一个通道都没有的信号也照过。"""
    import dataclasses

    off = dataclasses.replace(RULES, require_relevance_signal=False)
    signal = _signal(matched_terms=(), mentioned=())
    kept, drops = drop_signals([signal], rules=off)
    assert kept == (signal,)
    assert drops == ()


def test_unknown_channel_name_blows_up(tmp_path):
    """配置里写了没有实现的通道名，读配置就炸。

    退化成「跳过这个通道」的话，把 mentioned_companies 拼错一个字母的
    后果是那条通道悄悄失效、规则一悄悄变严、真买方悄悄被多砍一批，
    而症状只是「今天线索少」，从报表上根本查不出来。
    """
    path = tmp_path / "filters.yaml"
    path.write_text(
        "signal_level:\n"
        "  require_relevance_signal: true\n"
        "  relevance_channels: [matched_terms, mentiond_companies]\n"
        "  drop_seen_content_hash: true\n"
        "  hardware_store: []\n"
        "  supplier_content: []\n"
        "  off_topic: []\n"
        "candidate_level:\n"
        "  drop_company_page_author: true\n"
        "  supplier_headline: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="没有对应实现"):
        load_rules(path)


def test_missing_config_key_blows_up(tmp_path):
    """配置少一个键当场 KeyError，不退化成「这条规则不生效」。"""
    path = tmp_path / "filters.yaml"
    path.write_text(
        "signal_level:\n"
        "  require_relevance_signal: true\n"
        "  relevance_channels: [matched_terms]\n"
        "  hardware_store: []\n"
        "  supplier_content: []\n"
        "  off_topic: []\n"
        "candidate_level:\n"
        "  drop_company_page_author: true\n"
        "  supplier_headline: []\n",
        encoding="utf-8",
    )
    with pytest.raises(KeyError):
        load_rules(path)


# ───────────────────────────────────────────────────── 规则二：见过的正文


def test_same_hash_twice_in_one_run_is_attributed_to_this_run():
    signal_a = _signal(external_id="1", content_hash="same")
    signal_b = _signal(external_id="2", content_hash="same")
    kept, drops = drop_signals([signal_a, signal_b], rules=RULES)
    assert kept == (signal_a,)
    assert [d.reason for d in drops] == [SignalDropReason.SEEN_HASH_IN_RUN]


def test_hash_seen_in_an_earlier_run_is_attributed_to_that_run():
    """跑内和跨天是两个 reason。

    漏斗表按 reason 聚合，而这两个数的含义完全不同：跨天是正常损耗，
    跑内是联播或者同一帖被多条 query 搜到 —— 后者是 query 有重叠的信号。
    合成一个 reason 的话，query 重叠度上升这件事在报表上就看不见了。
    """
    signal = _signal(content_hash="old")
    kept, drops = drop_signals([signal], rules=RULES, seen_content_hashes=frozenset({"old"}))
    assert kept == ()
    assert [d.reason for d in drops] == [SignalDropReason.SEEN_HASH_PRIOR_RUN]


def test_hash_left_by_an_earlier_query_in_this_run_is_attributed_to_this_run():
    """这一跑前面几条 query 留下的摘要，算跑内不算跨天。

    pipeline 一条 query 一次调用，所以「同一帖被今天两条 query 搜到」
    跨了两次调用，前一条的摘要只能从 run_seen 传进来。要是跟库里查出来
    那批合成一个集合，这种撞车会被记成「更早见过」—— 而它恰恰是
    query 组队重叠的信号，那两个数的运营含义相反。
    """
    signal = _signal(content_hash="from-query-one")
    kept, drops = drop_signals(
        [signal],
        rules=RULES,
        seen_content_hashes=frozenset({"old"}),
        run_seen=frozenset({"from-query-one"}),
    )
    assert kept == ()
    assert [d.reason for d in drops] == [SignalDropReason.SEEN_HASH_IN_RUN]


def test_both_seen_sets_block_but_are_attributed_apart():
    """两个集合挡掉的东西一样，记的账不一样。"""
    prior = _signal(external_id="1", content_hash="old")
    in_run = _signal(external_id="2", content_hash="from-query-one")
    kept, drops = drop_signals(
        [prior, in_run],
        rules=RULES,
        seen_content_hashes=frozenset({"old"}),
        run_seen=frozenset({"from-query-one"}),
    )
    assert kept == ()
    assert [d.reason for d in drops] == [
        SignalDropReason.SEEN_HASH_PRIOR_RUN,
        SignalDropReason.SEEN_HASH_IN_RUN,
    ]


def test_a_dropped_signal_does_not_occupy_its_hash():
    """被别的规则砍掉的信号，不该占住它那条摘要。

    占住的话，同一段正文的第二条会因为「见过了」被丢，而第一条其实
    也没留下 —— 两条都没了，报表上却显示是两种不同的原因。
    """
    dropped = _signal(external_id="1", content_hash="same", text="Buying Guide")
    survivor = _signal(external_id="2", content_hash="same")
    kept, drops = drop_signals([dropped, survivor], rules=RULES)
    assert kept == (survivor,)
    assert [d.reason for d in drops] == [SignalDropReason.SUPPLIER_CONTENT]


# ───────────────────────────────────────────────────── 规则三到五：噪音词表


def test_single_category_hit_is_attributed_to_that_category():
    signal = _signal(text="all about CNC machining today")
    _, drops = drop_signals([signal], rules=RULES)
    assert [d.reason for d in drops] == [SignalDropReason.HARDWARE_STORE]
    assert drops[0].detail == "CNC machining"


def test_multiple_terms_in_one_category_all_land_in_detail():
    """同一类命中好几个词，detail 要写全。

    只写第一个的话，改词表时不知道剩下几个词是不是也该动。
    """
    signal = _signal(text="a Buying Guide, and also How to Choose a mic")
    _, drops = drop_signals([signal], rules=RULES)
    assert drops[0].reason == SignalDropReason.SUPPLIER_CONTENT
    assert drops[0].detail == "Buying Guide, How to Choose a"


def test_hits_across_categories_get_their_own_reason():
    """命中不止一类时归到 MULTIPLE_RULE_HITS，不归给排在前面那类。

    归给前面那类的话，前三条规则的砍量就变成「谁排前面谁大」，
    看不出后面那些其实也拦得住 —— 而那正是判断一条规则该不该留的依据。
    """
    signal = _signal(text="CNC machining plus a Buying Guide for an audiobook")
    _, drops = drop_signals([signal], rules=RULES)
    assert drops[0].reason == SignalDropReason.MULTIPLE_RULE_HITS
    assert "hardware_store x1 (CNC machining)" in drops[0].detail
    assert "supplier_content x1 (Buying Guide)" in drops[0].detail
    assert "off_topic x1 (audiobook)" in drops[0].detail


def test_every_dropped_signal_carries_exactly_one_reason():
    """归因唯一：丢弃记录数必须等于被丢信号数。

    一条信号记两个理由的话，「各规则砍了多少」的合计会超过总丢弃数，
    漏斗表就对不上了。
    """
    signals = [
        _signal(external_id="1", matched_terms=(), mentioned=()),
        _signal(external_id="2", text="CNC machining and a Buying Guide"),
        _signal(external_id="3", content_hash="dup"),
        _signal(external_id="4", content_hash="dup"),
    ]
    kept, drops = drop_signals(signals, rules=RULES)
    assert len(kept) + len(drops) == len(signals)
    assert len({d.external_id for d in drops}) == len(drops)


# ───────────────────────────────────────────────────── candidate 级：层级


def test_company_page_post_drops_the_author_but_keeps_the_mentioned():
    """血的教训那一条：公司主页帖只砍 author 侧候选。

    写成整条丢弃的话，「加速器从公司主页发帖恭喜被投公司众筹成功」
    这类线索会连帖子一起没掉 —— 发帖人不是买方，被提到的公司才是。
    """
    signal = _signal(actor_type=ActorType.COMPANY)
    author = _candidate(_company("某加速器", company_id="1"), LeadType.AUTHOR, signal)
    mentioned = _candidate(_company("North Field Technologies", company_id="2"), LeadType.MENTIONED, signal)

    kept, drops = drop_candidates([author, mentioned], rules=RULES)
    assert kept == (mentioned,)
    assert [d.reason for d in drops] == [CandidateDropReason.COMPANY_PAGE_AUTHOR]
    # 丢弃记录要能回到原文那条帖子，不然查「这条为什么没了」落不了地
    assert drops[0].external_id == signal.external_id


def test_supplier_headline_never_touches_a_mentioned_candidate():
    """供应商 headline 只判 author 侧，被 @ 的公司不受它影响。

    发帖人的职位串描述的是他自己，跟他 @ 的公司毫无关系。拿它去判
    mentioned，就会把「供应商吹我们帮 Lumen 做了 5000 只耳机」里的
    真买方 Lumen 一起砍掉。
    """
    signal = _signal(headline="OEM/ODM Export Sales Manager")
    supplier = _candidate(_company("深圳某音频厂", company_id="1"), LeadType.HEADLINE, signal)
    buyer = _candidate(_company("Lumen", company_id="2"), LeadType.MENTIONED, signal)

    kept, drops = drop_candidates([supplier, buyer], rules=RULES)
    assert kept == (buyer,)
    assert [d.reason for d in drops] == [CandidateDropReason.SUPPLIER_HEADLINE]
    # 两个词都命中，两个都要写进 detail，不能只留第一个
    assert drops[0].detail == "OEM/ODM, Export Sales"


def test_repost_author_side_reads_the_original_authors_headline():
    """转发通道的 author 侧，看的是原帖作者的职位串，不是转发者的。

    看错人的话，转发者恰好是供应商时会把原帖作者那家公司误砍。
    """
    original = RepostedFrom(
        external_id="orig-1",
        url="https://www.linkedin.com/posts/orig",
        text="原帖正文",
        actor_name="Original",
        actor_headline="OEM/ODM Sourcing Partner",
        actor_url="https://www.linkedin.com/in/orig/",
        actor_type=ActorType.PROFILE,
        actor_company=None,
        mentioned_companies=(),
        observed_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
    signal = _signal(headline="Founder", reposted_from=original)
    candidate = _candidate(_company("某厂", company_id="1"), LeadType.REPOST_HEADLINE, signal)

    kept, drops = drop_candidates([candidate], rules=RULES)
    assert kept == ()
    assert [d.reason for d in drops] == [CandidateDropReason.SUPPLIER_HEADLINE]


def test_candidate_without_any_identity_is_dropped_not_raised():
    """建不出身份键的候选在这里丢掉，不留到归并那步炸掉整跑。

    放在这里丢是为了让它出现在漏斗报表里 —— 它是「解析失败了多少」
    这个指标的唯一来源。
    """
    nameless = _candidate(_company("   "), LeadType.MENTIONED)
    kept, drops = drop_candidates([nameless], rules=RULES)
    assert kept == ()
    assert [d.reason for d in drops] == [CandidateDropReason.NO_IDENTITY]


def test_a_second_candidate_word_list_gets_its_own_attribution(monkeypatch):
    """加第二张候选级词表时，归因逻辑不用改。

    这条测的是表驱动本身：临时往表里插一张查公司名的词表，两张表同时
    命中，归因应该落到 MULTIPLE_RULE_HITS 而不是排在前面那张。
    生产配置里现在只有一张表，所以那条分支平时走不到 —— 用 monkeypatch
    走一遍，免得它在第二张表真加进来之前一直是没测过的死代码。
    """
    import dataclasses

    from discovery import prefilter

    monkeypatch.setitem(
        prefilter._CANDIDATE_TEXT_RULES,
        "banned_company_name",
        (CandidateDropReason.NO_IDENTITY, lambda candidate: candidate.company.name),
    )
    rules = dataclasses.replace(
        RULES,
        candidate_terms=RULES.candidate_terms + (("banned_company_name", ("Trading",)),),
    )
    candidate = _candidate(
        _company("Shenzhen Trading Co", company_id="1"),
        LeadType.HEADLINE,
        _signal(headline="OEM/ODM Manager"),
    )

    kept, drops = drop_candidates([candidate], rules=rules)
    assert kept == ()
    assert drops[0].reason == CandidateDropReason.MULTIPLE_RULE_HITS
    assert "supplier_headline x1 (OEM/ODM)" in drops[0].detail
    assert "no_identity x1 (Trading)" in drops[0].detail


def test_unknown_candidate_word_list_blows_up(tmp_path):
    """配置里出现没有实现的候选级词表名，读配置就炸。

    静默忽略的话，把 supplier_headline 拼错一个字母 = 那张表整个不生效、
    供应商全放进来，而症状只是「今天垃圾变多」，报表上查不出来。
    """
    path = tmp_path / "filters.yaml"
    path.write_text(
        "signal_level:\n"
        "  require_relevance_signal: true\n"
        "  relevance_channels: [matched_terms]\n"
        "  drop_seen_content_hash: true\n"
        "  hardware_store: []\n"
        "  supplier_content: []\n"
        "  off_topic: []\n"
        "candidate_level:\n"
        "  drop_company_page_author: true\n"
        "  supplier_headline: []\n"
        "  suplier_headlines: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="没对应实现"):
        load_rules(path)


def test_shipped_config_still_loads():
    """仓库里那份 config/filters.yaml 本身能读通。

    上面所有测试用的都是手写的规则，配置文件写坏了它们一条都发现不了。
    """
    rules = load_rules()
    assert rules.relevance_channels
    assert dict(rules.candidate_terms)["supplier_headline"]




# ───────────────────────────────────────────── 规则九：@ 了一堆公司


def _many(n: int) -> tuple[CompanyRef, ...]:
    return tuple(_company(f"C{i}", company_id=str(1000 + i)) for i in range(n))


def test_a_post_mentioning_a_crowd_stops_being_a_lead_source():
    """@ 了一堆公司的帖子是清单，里面没有哪一家是「现在有需求的主角」。

    detail 记这一段 @ 了几家 —— 查「这条为什么没了」时，这个数直接告诉你
    该不该调阈值，比只记一个规则名有用。
    """
    crowd = _many(5)  # 阈值是 4，5 家就超了
    signal = _signal(mentioned=crowd)
    kept, drops = drop_candidates(
        [_candidate(c, LeadType.MENTIONED, signal) for c in crowd], rules=RULES
    )

    assert kept == ()
    assert {d.reason for d in drops} == {CandidateDropReason.MENTION_FANOUT}
    assert drops[0].detail == "5 mentions"


def test_a_handful_of_mentions_still_counts():
    """恰好到阈值不砍。「X 和 Y 达成合作」这类真有事发生的帖子就只 @ 一两三家，
    砍过头等于把最有价值的那类帖子一起丢掉。"""
    few = _many(4)
    signal = _signal(mentioned=few)
    kept, drops = drop_candidates(
        [_candidate(c, LeadType.MENTIONED, signal) for c in few], rules=RULES
    )

    assert len(kept) == 4
    assert drops == ()


def test_the_author_side_survives_a_crowded_post():
    """只砍被 @ 的那一侧，发帖人自己那一侧照留。

    跟规则⑦（供应商 headline 只砍 author 一侧）是同一条层级纪律：这条规则
    说的是「这些被 @ 的公司都只是清单里的一项」，它对发帖人自己一个字都没说。
    砍到 author 一侧的话，写盘点文的人如果职位串里正好有家真公司会被无辜砍掉。
    """
    crowd = _many(9)
    signal = _signal(mentioned=crowd)
    candidates = [_candidate(c, LeadType.MENTIONED, signal) for c in crowd]
    candidates.append(_candidate(_company("Chatterly"), LeadType.HEADLINE, signal))

    kept, drops = drop_candidates(candidates, rules=RULES)

    assert [c.company.name for c in kept] == ["Chatterly"]
    assert len(drops) == 9


def test_a_repost_counts_each_segment_on_its_own():
    """转发按段数分开数。

    转发者写一句、@ 一家，而原帖是个十家清单 —— 两段的扇出完全不同。
    混着数会让其中一段被另一段的行为连累：要么外层那一家被原帖的清单
    牵连砍掉，要么原帖那十家因为外层只有一家而全部放行。
    """
    inner_crowd = _many(9)
    original = RepostedFrom(
        external_id="orig",
        url="https://www.linkedin.com/posts/orig",
        text="十家清单",
        actor_name="Someone Else",
        actor_headline="Editor",
        actor_url="https://www.linkedin.com/in/else/",
        actor_type=ActorType.PROFILE,
        actor_company=None,
        mentioned_companies=inner_crowd,
        observed_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
    outer = _company("Chatterly", company_id="100000001")
    signal = _signal(mentioned=(outer,), reposted_from=original)

    candidates = [_candidate(outer, LeadType.MENTIONED, signal)]
    candidates += [_candidate(c, LeadType.REPOST_MENTIONED, signal) for c in inner_crowd]

    kept, drops = drop_candidates(candidates, rules=RULES)

    # 外层只 @ 了一家，留下；原帖那十家全砍
    assert [c.company.name for c in kept] == ["Chatterly"]
    assert {d.reason for d in drops} == {CandidateDropReason.MENTION_FANOUT}
    assert drops[0].detail == "9 mentions"


# ───────────────────────────────────────────── 规则八：不是做产品的公司


def test_a_crowdfunding_platform_mention_is_not_a_lead():
    """被 @ 的 Kickstarter 不是线索，它是众筹平台。

    实测这一批 133 家里混着众筹平台、媒体、投资方，每一个都在花 LLM 的钱，
    而它们永远不可能是买方。detail 记的是表里登记的那个名字，
    不是候选自己的名字 —— 查「这条为什么没了」要回到表里那一行。
    """
    kickstarter = _company("Kickstarter", company_id="413999")
    kept, drops = drop_candidates([_candidate(kickstarter, LeadType.MENTIONED)], rules=RULES)

    assert kept == ()
    assert drops[0].reason == CandidateDropReason.NOT_A_BUYER
    assert drops[0].detail == "Kickstarter"


def test_a_big_brand_is_not_cut_here():
    """这条规则不许滑成大厂黑名单。

    判据是「它做不做硬件产品」，不是「它多大」。Samsung 真做硬件、只是
    太大，那是打分层 moq_fit 的判断。混进来的话「砍大厂的规则」在指标上
    会显得完全正确，诱导把打分层的职责一路塞进规则层 ——
    labels 里 big_brand 单列成一类是同一条理由。
    """
    samsung = _company("Samsung Electronics", company_id="1753")
    kept, drops = drop_candidates([_candidate(samsung, LeadType.MENTIONED)], rules=RULES)

    assert len(kept) == 1
    assert drops == ()


def test_a_candidate_without_an_id_is_left_alone():
    """只有名字的候选这条规则碰不到。这是已知的覆盖缺口，不是 bug。

    按名字砍会撞：这批数据里就有叫 Gamma / Notion / Work 的实体，
    按名字砍会连带杀掉真叫这个名字的公司。漏掉一个投资方的代价是一次
    LLM 调用，按名字砍的代价可能是一条真买方。
    """
    no_id = _company("Kickstarter")  # 同名，但没有 id
    kept, drops = drop_candidates([_candidate(no_id, LeadType.HEADLINE)], rules=RULES)

    assert len(kept) == 1
    assert drops == ()


def test_blocking_one_mention_does_not_kill_the_others_from_that_post():
    """层级：只丢这一个候选，同一条帖子里别的候选照留。

    一篇「Chatterly 的众筹刚在 Kickstarter 结束」的帖子会展开出两个
    mentioned 候选：Kickstarter 那个该砍，Chatterly 那个正是我们要的。
    写成整条丢弃会把真买方一起杀掉。
    """
    mentioned = (
        _company("Kickstarter", company_id="413999"),
        _company("Chatterly", company_id="100000001"),
    )
    signal = _signal(mentioned=mentioned)
    candidates = [_candidate(c, LeadType.MENTIONED, signal) for c in mentioned]

    kept, drops = drop_candidates(candidates, rules=RULES)

    assert [c.company.name for c in kept] == ["Chatterly"]
    assert [d.reason for d in drops] == [CandidateDropReason.NOT_A_BUYER]


def test_the_config_ids_survive_being_written_without_quotes():
    """YAML 里 id 写成带引号的字符串，但有人去掉引号就会被解析成整数。

    那种错的症状是「这条规则悄悄不生效」—— 垃圾变多，报表上查不出来。
    load_rules 里 str() 一次就是为了这个，这条测试焊住它。
    """
    rules = load_rules()

    assert all(isinstance(company_id, str) for company_id, _ in rules.not_a_buyer)
    assert any(name == "Kickstarter" for _, name in rules.not_a_buyer)


def test_rules_stay_hashable():
    """整套规则必须能求指纹。

    frozen dataclass 会自动生成指纹函数，而字段里只要有一个字典，
    求指纹就当场报错。守这条是为了以后能给 load_rules 加缓存、
    或者把几套规则放进集合去重 —— 那时候才发现字段类型选错了，
    改起来要动配置读取和判断逻辑两处。
    """
    assert hash(RULES) == hash(RULES)
    assert len({RULES, RULES}) == 1
