"""normalize 的口径测试。

每一条对应一个已经想清楚、而且一旦被改坏就会静默出错的取舍。
静默出错在这个模块里有两种形态，两种都不会报错：
    真线索被当成重复或不相关丢掉
    假公司名被抽出来，归并时把两家不相干的公司并成一家
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from discovery.normalize import (
    _query_terms,
    company_from_headline,
    content_hash,
    matched_terms,
    to_clusters,
    to_signal,
)
from models import (
    ActorType,
    Candidate,
    CompanyRef,
    IdentityUnresolvable,
    LeadType,
    RawSignal,
    RepostedFrom,
    Signal,
    SourceType,
)

# ─────────────────────────────────────────────────────────── content_hash


def test_content_hash_ignores_what_changes_when_text_is_reposted():
    """同一段话被重新发一遍时会变、但不承载意义的东西，摘要要无视掉。

    这四类是保险，不是主力 —— 复制粘贴出来的联播本来就逐字相同。
    但它们防的情况（同一段话经过不同客户端、编辑器、复制路径）
    只有它们能挡，而代价只有几微秒。
    """
    base = "We are looking for a manufacturing partner."
    assert content_hash(base) == content_hash("We  are looking\n\nfor a  manufacturing partner. ")
    assert content_hash(base) == content_hash(base.upper())
    assert content_hash("audio​wearable") == content_hash("audiowearable")
    assert content_hash("MOQ（500）") == content_hash("MOQ(500)")


def test_content_hash_keeps_links_and_hashtags_apart():
    """链接、话题标签、标点、emoji 一律不抹。

    这一条锁的是撞车方向。剥掉链接的话，下面两段会变成同一句，
    两家不同公司的独立线索被并成一条 —— 而带众筹链接的帖子
    恰好是最有价值的那一类。撞车是静默丢线索，报表上看不出来。
    """
    assert content_hash("Our campaign is live! https://kickstarter.com/lumen") != content_hash(
        "Our campaign is live! https://kickstarter.com/boomcrate"
    )
    assert content_hash("Shipping now #audio") != content_hash("Shipping now #wearables")
    assert content_hash("Excited! 🎉") != content_hash("Excited! 🚀")


# ─────────────────────────────────────────────────────────── query 解析


def test_query_terms_keeps_an_operator_free_query_as_one_term():
    """不带布尔符的 query 整句算一个词，不能按单词拆。

    这一条是整个命中词闸门的地基。按单词拆会拆出 for、a 这种虚词，
    它们命中每一篇帖子，命中词那条规则就永远为真，闸门等于被拆掉。
    """
    assert _query_terms("looking for a hardware manufacturing partner") == (
        ("looking for a hardware manufacturing partner",),
        (),
    )


def test_query_terms_splits_on_operators_and_keeps_quoted_phrases_whole():
    """布尔符之间的一整段是一个词，引号里的短语不被布尔符切开。

    引号短语要当成一个整体先认出来，否则里面含 OR 的短语会被切坏。
    顺序就是它们在 query 里出现的先后。
    """
    assert _query_terms('(prototype OR "mass production") AND (audio OR microphone)') == (
        ("prototype", "mass production", "audio", "microphone"),
        (),
    )


def test_query_terms_puts_a_not_branch_in_the_second_group():
    """NOT 后面的词单独一组。

    混进第一组的话，一条写着 hiring 的帖子会被算成「命中了 hiring，所以相关」——
    方向正好反了，而且不报错。
    """
    assert _query_terms("audio NOT hiring") == (("audio",), ("hiring",))


def test_query_terms_takes_a_parenthesised_not_group():
    """NOT 后面跟一个括号组时，组里每个词都是排除词。

    这是要排除多个词时唯一认的写法，因为不带括号的那种（NOT 甲 OR 乙）
    说不清乙算哪一边。
    """
    assert _query_terms('audio AND wearable NOT (hiring OR "job opening")') == (
        ("audio", "wearable"),
        ("hiring", "job opening"),
    )


def test_query_terms_refuses_what_it_cannot_parse():
    """拆不出词、或者形状没见过，一律炸，不返回空元组。

    「不知道该找什么词」和「找了但一个都没有」是两件事。混成同一个
    返回值的话，一次采集故障会被记成这批 query 全都没线索，
    而那组数字决定预算怎么分。
    """
    with pytest.raises(ValueError):
        _query_terms("")
    with pytest.raises(ValueError):
        _query_terms("AND OR ()")
    with pytest.raises(ValueError):
        # 整句都是排除，没有一个要找的词
        _query_terms("NOT audio")
    with pytest.raises(ValueError):
        # remote 算排除还是算要找的，写的人和读的人多半理解不一样
        _query_terms("audio NOT hiring OR remote")
    with pytest.raises(ValueError):
        _query_terms("audio NOT")
    with pytest.raises(ValueError):
        _query_terms("audio NOT AND hiring")
    with pytest.raises(ValueError):
        _query_terms("audio AND (microphone")


# ─────────────────────────────────────────────────────────── 命中词


def test_matched_terms_needs_a_real_word_boundary():
    """audiobook 不算命中 audio，但中英混排里的 audio 要算。

    边界用的是显式的 ASCII 判断，不是 \\b。\\b 依赖 \\w，而 Python 的
    \\w 把中日韩文字算成单词字符，一旦正文中英混排，\\b 就认为那里
    没有边界，词直接匹配不上。
    """
    q = "Kickstarter AND (audio OR microphone)"
    assert matched_terms("We produce audiobook narration at scale.", q) == ()
    assert matched_terms("我们的 audio耳机 已经量产", q) == ("audio",)


def test_matched_terms_tolerates_line_breaks_and_hyphens_inside_a_phrase():
    """词组里的空格允许对应正文里的换行或连字符，那是同一个意思。"""
    q = 'Kickstarter AND (microphone OR "voice recorder")'
    assert matched_terms("Our new voice\nrecorder ships in May.", q) == ("voice recorder",)
    assert matched_terms("The voice-recorder is done.", q) == ("voice recorder",)
    # 词组不完整就不算命中，不做部分匹配
    assert matched_terms("We are looking for a recorder.", q) == ()


def test_matched_terms_returns_query_order_for_attribution():
    """命中的词按它们在 query 里出现的先后返回，不按正文里出现的先后。

    它的第二个用途是归因：一条 query 挂着好几个 OR 分支，知道命中的
    是哪个分支，才知道该删哪个分支。所以词按 query 里的原样返回，
    不做归一化。
    """
    q = 'Kickstarter AND (microphone OR "voice recorder")'
    text = "A voice-recorder for meetings, launching on Kickstarter."
    # 正文里 voice-recorder 更靠前，但 query 里 Kickstarter 在前面
    assert matched_terms(text, q) == ("Kickstarter", "voice recorder")


def test_matched_terms_empty_is_a_normal_result():
    """一个词都不命中是正常且常见的结果，不是出错。

    LinkedIn 的搜索是语义相关匹配，它会把意思沾边的帖子也返回，
    而那些帖子的正文里可能一个搜索词都没有。砍掉它的动作在 prefilter。
    """
    assert matched_terms("Great launch, congrats to the whole team!", "Kickstarter AND audio") == ()


def test_matched_terms_does_not_count_an_excluded_word_as_a_hit():
    """排除词出现在正文里不是「相关」，是反过来的证据。

    这条焊住的是一个不报错的毛病：把 NOT 后面的词混进要找的词里，
    一条招聘帖会因为写了 hiring 而被判成命中，然后一路走到 analyzer 去花钱。
    """
    query = "audio NOT hiring"

    assert matched_terms("We are hiring an audio engineer", query) == ("audio",)
    assert matched_terms("We are hiring a backend engineer", query) == ()


# ─────────────────────────────────────────────────────────── 第一跳


def _raw(
    text: str,
    *,
    query: str,
    reposted_from: RepostedFrom | None = None,
    external_id: str = "1",
) -> RawSignal:
    return RawSignal(
        source="harvestapi_posts",
        source_type=SourceType.LINKEDIN_POST,
        external_id=external_id,
        url="https://www.linkedin.com/posts/x",
        text=text,
        query_search=query,
        actor_name="Someone",
        actor_headline="Founder",
        actor_url="https://www.linkedin.com/in/someone/",
        actor_type=ActorType.PROFILE,
        actor_company=None,
        mentioned_companies=(),
        reposted_from=reposted_from,
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
        raw={},
    )


def _reposted(text: str) -> RepostedFrom:
    return RepostedFrom(
        external_id="orig-1",
        url="https://www.linkedin.com/posts/orig",
        text=text,
        actor_name="Original Author",
        actor_headline="Founder",
        actor_url="https://www.linkedin.com/in/orig/",
        actor_type=ActorType.PROFILE,
        actor_company=None,
        mentioned_companies=(),
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_to_signal_counts_both_halves_of_a_repost():
    """转发的两段正文一起算，摘要和命中词都是。

    只取原帖正文的话，两个人转发同一原帖、其中一个自己加了
    「我们也在找代工厂」，摘要会跟另一个相同，这条真线索被丢掉。
    只取转发者那句话的话，一条讲众筹音频硬件的原帖会因为转发者
    没提关键词而被判成不相关。
    """
    q = "Kickstarter AND (audio OR microphone)"
    original = "Launching our audio wearable on Kickstarter next week."

    a = to_signal(_raw("So cool to see this", query=q, reposted_from=_reposted(original)))
    b = to_signal(_raw("We are looking for this too", query=q, reposted_from=_reposted(original)))

    # 原帖内容参与命中词，所以转发者没提关键词也能命中
    assert a.matched_terms == ("Kickstarter", "audio")
    # 转发者各写了不同的话，摘要就不同，谁都不会被当成对方的重复
    assert a.content_hash != b.content_hash
    # 但它们说的是同一件事，corroboration 靠 fact_hash 只算一次
    assert a.fact_hash == b.fact_hash


def test_thin_reposts_of_one_original_are_one_fact():
    """转发者只是传话时，几个人转同一个原帖算一件事。

    每个人都会加自己的一句话，所以正文摘要各不相同 —— 那是对的，
    他们发的确实不是同一段文字。但他们说的是同一件事，而 corroboration
    数的是事。只按正文摘要去重的话，一份公告被联播一轮就能把这一维刷高。
    """
    q = "Kickstarter AND (audio OR microphone)"
    original = "Launching our audio wearable on Kickstarter next week."

    a = to_signal(_raw("So cool to see this", query=q, reposted_from=_reposted(original)))
    b = to_signal(_raw("Congrats to the team", query=q, reposted_from=_reposted(original)))

    assert a.content_hash != b.content_hash
    assert a.fact_hash == b.fact_hash


def test_thin_reposts_of_different_originals_stay_apart():
    """转发不同的原帖是不同的事，不能因为都是传话就并成一件。

    这条防的是修上一条时修过头：把所有转发都当成同一件事。
    两条信号连转发者写的那句都一模一样，逼它必须靠原帖正文才分得开。
    """
    q = "Kickstarter AND (audio OR microphone)"

    a = to_signal(_raw("So cool", query=q, reposted_from=_reposted("甲原帖讲 audio")))
    b = to_signal(_raw("So cool", query=q, reposted_from=_reposted("乙原帖讲 audio")))

    assert a.fact_hash != b.fact_hash


def test_a_repost_that_says_something_of_its_own_is_its_own_fact():
    """转发者自己那段还有相关性时，那是一件独立的事，不算传话。

    判据是他自己那段里的命中词（或者他自己正文里被 @ 的公司），不是字数。
    实测转发的外层字数从 50 到 2778 连续分布、没有自然断点，阈值划在哪
    都是拍的；长度也不等于信息量。
    """
    q = "Kickstarter AND (audio OR microphone)"
    original = "Launching our audio wearable on Kickstarter next week."

    passing = to_signal(_raw("So cool to see this", query=q, reposted_from=_reposted(original)))
    speaking = to_signal(
        _raw("We are building our own microphone too", query=q,
             reposted_from=_reposted(original))
    )

    assert speaking.fact_hash != passing.fact_hash


def test_a_thin_repost_and_the_original_itself_are_one_fact():
    """原帖本身也被单独抓到时，它跟转发它的那条要认成同一件事。

    旧写法在这里有个洞：转发记的是原帖 id、原帖自己记的是正文摘要，
    两个键不相等，一件事被数成两件。改成一律按正文摘要之后天然对上。
    """
    q = "Kickstarter AND (audio OR microphone)"
    original = "Launching our audio wearable on Kickstarter next week."

    itself = to_signal(_raw(original, query=q, external_id="orig-1"))
    repost = to_signal(
        _raw("So cool to see this", query=q, external_id="re-1",
             reposted_from=_reposted(original))
    )

    assert itself.fact_hash == repost.fact_hash


def test_to_signal_does_not_drop_anything():
    """命中词算出来是空的也照样返回一个 Signal。

    这个模块只算不砍。把丢弃提前到这里，会让「这条为什么没了」
    要翻两个地方查。
    """
    sig = to_signal(_raw("Congrats to the team!", query="Kickstarter AND audio"))
    assert sig.matched_terms == ()
    assert sig.content_hash


# ─────────────────────────────────────────────────── headline 兜底解析


def test_company_from_headline_extracts_on_an_explicit_marker():
    """有明确任职标记时才抽，抽出来的身份只有名字。"""
    got = company_from_headline("Consultant | Low-Power Design at PULSAR Microelectronics | More")
    assert got is not None
    assert got.name == "PULSAR Microelectronics"
    # 三个强身份字段一律 None，这个弱是真实的，不在这里假装补齐
    assert got.linkedin_company_id is None
    assert got.linkedin_url is None
    assert got.domain is None
    assert got.is_weak_identity


def test_company_from_headline_ignores_an_email_address():
    """邮箱里的 @ 不是任职标记。

    职位串里常有人写邮箱招揽联系，而邮箱里 @ 后面跟的是域名。
    要求 @ 前面是空白就能分开两种用法：「任职于」的 @ 一定跟头衔隔着空格。
    """
    assert company_from_headline("AI Training Platform ... reach us info@stepwise.example now") is None
    # 前面有空格的 @ 照样要能抽，包括后面不带空格的写法
    got = company_from_headline("Software Engineer @Verint | Webmaster")
    assert got is not None and got.name == "Verint"


def test_company_from_headline_rejects_prose_after_at():
    """at 后面接的是一句话而不是公司名时要拒。

    靠小写虚词开头判断，而且刻意区分大小写：公司名里这些词会大写，
    写句子时是小写。代价是会连带拒掉少数把 the 写成小写的真公司名，
    那是可以接受的损失 —— 丢一条线索，而放过去会让一整句散文
    变成一个公司候选被送进 LLM。
    """
    assert company_from_headline("Building Tech at the Intersection of AI, Devices, Design") is None
    got = company_from_headline("Engineer at The Boring Company | Tunnels")
    assert got is not None and got.name == "The Boring Company"


def test_company_from_headline_rejects_placeholders_and_unusable_names():
    """占位说法和不能当身份键的名字一律拒，不修补。

    占位词放过去的话，所有隐形创业公司会被归并成同一家。
    单字符名字做身份键会跟所有同名的东西撞在一起，而名字本来就是
    梯度最低那一级，这种键宁可不要。
    """
    assert company_from_headline("Sales + Ops @ Stealth Startup | Business Development") is None
    assert company_from_headline("Consultant at Self Employed") is None
    assert company_from_headline("Director @ X, Moonshot Factory") is None
    assert company_from_headline("Senior Business Development Specialist") is None


def test_company_from_headline_does_not_read_commas_as_employers():
    """逗号形式刻意不认。

    "Chief Legal Officer, Corporate Secretary at Effectual" 里逗号分隔的
    是两个头衔，而这种写法跟真的逗号加公司名（"Owner, DLR Sales, Inc."）
    在文本上无法区分。认逗号会稳定地把头衔当成公司名。
    """
    # 没有任职标记、也没有法律后缀时，即使逗号后面真的是公司名也不抽
    assert company_from_headline("Co-founder & COO, SecureLabs | GRC Consulting Firm") is None
    # 有 at 的时候，抽的是 at 后面那个，不会被前面的逗号带偏
    got = company_from_headline("Chief Legal Officer, Corporate Secretary at Effectual")
    assert got is not None and got.name == "Effectual"


def test_company_from_headline_falls_back_to_a_legal_suffix():
    """没有任职标记但挂着法律后缀时，靠后缀抽。

    专门捡 "Founder/CEO Chatterly Inc." 这种写法。难点是职位和公司名
    都是大写开头，分不出哪个是哪个。判据不是职位词表而是位置：
    从后缀往前收大写词，但不收整段的第一个词 —— LinkedIn 的职位串
    几乎总是先写角色再写公司。

    不用职位词表是刻意的：那种表永远长不完，今天要英文明天要德语，
    没收录的会静默失败。法律后缀是公司的法律标识，数量固定。
    """
    got = company_from_headline("Founder/CEO Chatterly Inc. | Safe-listening headphones")
    assert got is not None and got.name == "Chatterly Inc."

    # 非英语的任职标记（这里是德语的 bei）本身不认，但后缀这条路照样能兜住
    got = company_from_headline("Co-CEO bei Trelo Group GmbH & Co. KG")
    assert got is not None and got.name == "Trelo Group GmbH"

    # 逗号在公司名里面时不能被当成段的结尾切掉
    got = company_from_headline("Owner, DLR Sales, Inc.")
    assert got is not None and got.name == "DLR Sales, Inc."

    # 整段以公司名开头的写法会漏掉，那是漏不是错：第一个词一律当职位看待
    assert company_from_headline("Chatterly Inc. | Safe-listening headphones") is None


def test_company_from_headline_takes_the_first_marker():
    """出现多个任职标记时取第一个，那是主职。"""
    got = company_from_headline("Engineer at Intel | Advisor at Foo Labs")
    assert got is not None and got.name == "Intel"


# ─────────────────────────────────────────────────────────── 第三跳


def _cand(company: CompanyRef, lead_type: LeadType, external_id: str) -> Candidate:
    """造一个候选。每条候选带一条不同的信号，这样证据行数和 corroboration 才有意义。"""
    signal = Signal.from_raw(
        _raw("some post about audio", query="Kickstarter AND audio", external_id=external_id),
        content_hash=f"H-{external_id}",
        matched_terms=("audio",),
        fact_hash=f"H-{external_id}",
    )
    return Candidate(company=company, lead_type=lead_type, signal=signal)


def test_to_clusters_groups_by_identity_key_and_keeps_order():
    """按身份键归并，输出顺序是每家公司第一次出现的顺序。

    顺序稳定是为了报表和回归能对照 —— 同样的输入每次跑出同样的顺序。
    """
    lumen = CompanyRef("Lumen", "1", None, None)
    nava = CompanyRef("NAVA", "2", None, None)
    clusters = to_clusters(
        [
            _cand(lumen, LeadType.MENTIONED, "a"),
            _cand(nava, LeadType.MENTIONED, "b"),
            _cand(lumen, LeadType.MENTIONED, "c"),
        ]
    )
    assert [c.ref.name for c in clusters] == ["Lumen", "NAVA"]
    assert clusters[0].corroboration == 2


def test_to_clusters_absorbs_a_name_only_cluster_into_the_matching_strong_one():
    """同一家公司停在不同档位时，只有名字的那个要并进身份更硬的那个。

    身份键只取梯度最高的一档，所以 ("name","pinecrest") 和
    ("li_id","100000006") 永远不相等，第一遍归并碰不到一起。不补的话
    后面查 leads 表时会被当成两家分别记 cooldown、分别送去分析。
    """
    strong = CompanyRef("Pinecrest", "100000006", None, None)
    weak = CompanyRef("Pinecrest", None, None, None)

    clusters = to_clusters([_cand(strong, LeadType.MENTIONED, "a"), _cand(weak, LeadType.HEADLINE, "b")])

    assert len(clusters) == 1
    # 留下的是身份更硬的那份引用，弱的那份只有名字，是它的子集
    assert clusters[0].ref.linkedin_company_id == "100000006"
    # 两条通道都保住了，配对没丢
    assert clusters[0].lead_types == {LeadType.MENTIONED, LeadType.HEADLINE}
    assert clusters[0].corroboration == 2


def test_to_clusters_refuses_to_absorb_when_the_name_is_ambiguous():
    """一个名字同时对上两个身份更硬的簇时，两边都不动。

    这是这一步唯一的判断，方向跟整个模块一致：并错一次会把两家不相干的
    公司永久并成一家、污染别人的证据，而不并只是少一次交叉印证。
    """
    one = CompanyRef("Daydream", "111", None, None)
    two = CompanyRef("Daydream", "222", None, None)
    weak = CompanyRef("Daydream", None, None, None)

    clusters = to_clusters(
        [
            _cand(one, LeadType.MENTIONED, "a"),
            _cand(two, LeadType.MENTIONED, "b"),
            _cand(weak, LeadType.HEADLINE, "c"),
        ]
    )

    assert len(clusters) == 3
    assert any(c.ref.is_weak_identity for c in clusters)


def test_to_clusters_lets_an_unresolvable_identity_blow_up():
    """走到第三跳还有身份不明的候选，说明 prefilter 没做它该做的事。

    身份不明是 prefilter 的一条丢弃理由。在这里默默跳过的话，候选数和
    簇数会对不上，而且查不出差在哪，所以让它炸。
    """
    nameless = CompanyRef("   ", None, None, None)
    with pytest.raises(IdentityUnresolvable):
        to_clusters([_cand(nameless, LeadType.MENTIONED, "a")])


def test_to_clusters_refuses_an_identity_tier_it_cannot_merge():
    """冒出 li_id / name 以外的身份档就炸，不静默漏并。

    _absorb_weak_into_strong 只处理「名字档」跟别的档之间的归并。今天只有
    li_id 和 name 两档在用，靠的是数据源的行为不是代码的拦截 —— adapter 里
    被 @ 的公司 id 是可选的，供应商漏一个就会冒出 slug 档。
    真冒出来的时候，同一家公司会一个簇停在 slug 档、一个停在 id 档，两边都
    不是名字档，那一步谁也碰不到谁：两行、两份 cooldown、两次 LLM，看不见。
    """
    slug_only = CompanyRef(
        name="Lumen",
        linkedin_company_id=None,
        linkedin_url="https://www.linkedin.com/company/lumen/",
        domain=None,
    )
    assert slug_only.identity_key == ("li_slug", "lumen")

    with pytest.raises(ValueError, match="身份档"):
        to_clusters([_cand(slug_only, LeadType.MENTIONED, "a")])


def test_company_from_headline_rejects_a_fixed_phrase_after_the_marker():
    """at large 这类固定短语里的 at 不是任职标记。

    真数据里出现过 "Writer and independent scholar at large."，按任职标记
    那条规则读出来的公司名会是 large —— 建一行公司、花一次 LLM 的钱，
    而这个人恰恰是在说他没有雇主。

    同时焊住这条规则没有伤及真名字：名字里带 Large 这个词的公司照样过。
    """
    assert company_from_headline("Writer and independent scholar at large.") is None
    assert company_from_headline("Engineer at heart | Building things") is None

    got = company_from_headline("Director of Product at Large Format Displays")
    assert got is not None
    assert got.name == "Large Format Displays"
