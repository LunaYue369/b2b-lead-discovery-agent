"""规则层闸门。纯代码，零成本，放在 LLM 之前。

为什么不交给 LLM 判（这条比省钱重要）：
一篇耳机工厂的 SEO 文 "How to Choose a Bone Conduction Headphone Manufacturer"
措辞跟真买方的求助帖高度相似 —— prototype / tooling / mass production / MOQ 全中。
人工读都中招过三次，LLM 有相当概率给它打高分。
而规则是确定的：headline 带 OEM/ODM + Export Sales → 供应商；
正文带 Buying Guide / How to Choose a → 制造语境 SEO。一撞就砍，没有概率。
把能确定判的交给确定性层，别让 LLM 去赌。

第二个理由是可回归：每条丢弃都带 reason + detail，能出表、能拿历史数据回归、
能发现「这条规则误砍了真买方」。LLM 砍掉的只有一个分数，改 prompt 前后对不上。

规则分两级，层级搞错会杀真线索：
    signal 级（整条丢）    —— 只有跟发帖人身份无关的理由
        matched_terms 空 / content_hash 已见 / 五金词义撞车 /
        制造语境 SEO 文 / 招聘 spam
    candidate 级（只丢这个候选）
        公司主页帖 → 只砍 author 候选，mentioned 候选照留
        供应商 headline → 只砍 author 候选
        （供应商吹「我们帮 Lumen 做了 5000 只耳机」：
          author=深圳某音频厂 砍，mentioned=Lumen 是真买方 留）

规则从 config/filters.yaml 读。硬要求：以后挖出第四类噪音
必须是 YAML 加一行，不是改代码。

误砍一条真买方比多留十条垃圾严重得多。所以规则改一次就要拿回归集跑一次，
逐条确认真买方一条都没被砍。
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

import yaml

from models import (
    Candidate,
    CandidateDrop,
    CandidateDropReason,
    IdentityUnresolvable,
    LeadType,
    Signal,
    SignalDrop,
    SignalDropReason,
)

DEFAULT_RULES_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "filters.yaml"

# 相关性通道：一条信号凭什么算「跟我们有关」。
# 规则一放行只要求其中任意一条成立，全不成立才砍整条。
#
# 做成表是为了让「加一种通道」是 YAML 加一行加这里加一项，不是改判断逻辑。
# 目前两条的分工：命中词是「发帖人自己用了我们的话」，被 @ 的公司是
# 「结构化字段直接点名了这家公司」。后者不该由前者决定 —— 只认前者的
# 写法实测砍掉了三分之一以上的真买方。
_RELEVANCE_CHANNELS: dict[str, Callable[[Signal], bool]] = {
    "matched_terms": lambda signal: bool(signal.matched_terms),
    "mentioned_companies": lambda signal: bool(signal.mentioned_companies),
}

# 公司主页发的帖，它的 author 候选就是这家公司自己。
# 转发的原帖作者是公司主页时同理。
_COMPANY_PAGE_SIDE = frozenset({LeadType.AUTHOR, LeadType.REPOST_AUTHOR})


@dataclass(frozen=True)
class FilterRules:
    """从 config/filters.yaml 读出来的一整套规则。

    字段全是必填，读不到就炸。不给默认值是刻意的：配置里少一个键
    如果悄悄退化成「这条规则不生效」，闸门会静默漏水，而漏下去的东西
    要花钱过 LLM 才发现。
    """

    require_relevance_signal: bool
    relevance_channels: tuple[str, ...]
    drop_seen_content_hash: bool
    hardware_store: tuple[str, ...]
    supplier_content: tuple[str, ...]
    off_topic: tuple[str, ...]
    drop_company_page_author: bool
    # candidate 级的词表，一项是 (表名, 这张表的词)。不做成一个个具名字段，
    # 是为了让「加一张候选级词表」= YAML 加一段 + _CANDIDATE_TEXT_RULES
    # 加一行，不用动 FilterRules 也不用动 drop_candidates 的判断逻辑。
    # 表名必须在 _CANDIDATE_TEXT_RULES 里有对应实现，两边对不上就炸。
    #
    # 是成对的元组不是字典：这个类是 frozen 的，Python 会因此给它生成一个
    # 指纹函数，好让它能当缓存的键或者进集合去重，而指纹是把各字段的指纹
    # 拼出来的。字典自己没有指纹（它随时能改，指纹留不住），字段里有字典的话
    # 谁真去求指纹就当场报错。元组不许改、有指纹，整个类就一直是可求指纹的。
    candidate_terms: tuple[tuple[str, tuple[str, ...]], ...]

    # 不是「做产品的公司」的实体，按 LinkedIn 公司 id 认。一项是
    # (公司 id, 那家的名字)，名字只用来写进丢弃记录的 detail，
    # 判断只看 id。判据和为什么不按名字见 config/filters.yaml 的规则⑧。
    #
    # 跟 candidate_terms 一样是成对的元组不是字典，理由同上：
    # 这个类是 frozen 的，字段里有字典就求不出指纹了。
    not_a_buyer: tuple[tuple[str, str], ...]

    # 一段正文最多 @ 几家公司，超过就不把它 @ 的那些当线索。
    # 判据和阈值怎么定的见 config/filters.yaml 的规则⑨。
    max_mentions_per_post: int


def load_rules(path: pathlib.Path | str = DEFAULT_RULES_PATH) -> FilterRules:
    """读 config/filters.yaml，把它变成一个 FilterRules。

    输入：配置文件路径，默认是仓库里那份
    输出：一个 FilterRules

    raw 是解析出来的两层嵌套字典，signal_level 和 candidate_level
    各一层。这里把它摊平成一个对象，是为了调用处不用记哪条规则在哪一层 ——
    层级是丢弃动作的属性（丢整条还是丢一个候选），不是配置读取的属性。

    用下标取值不用 get：键缺了就当场 KeyError，而不是退化成空列表。
    一条规则悄悄失效，症状是垃圾变多而不是报错，最难发现。
    """
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    signal_level = raw["signal_level"]
    candidate_level = raw["candidate_level"]

    return FilterRules(
        require_relevance_signal=signal_level["require_relevance_signal"],
        relevance_channels=_checked_channels(signal_level["relevance_channels"]),
        drop_seen_content_hash=signal_level["drop_seen_content_hash"],
        hardware_store=tuple(signal_level["hardware_store"]),
        supplier_content=tuple(signal_level["supplier_content"]),
        off_topic=tuple(signal_level["off_topic"]),
        drop_company_page_author=candidate_level["drop_company_page_author"],
        candidate_terms=_checked_candidate_terms(candidate_level),
        # id 在 YAML 里写成带引号的字符串，这里再 str() 一次是为了
        # 哪天有人把引号去掉、YAML 解析成 int 时也能对上 ——
        # 那种错的症状是「这条规则悄悄不生效」，最难查。
        not_a_buyer=tuple(
            (str(company_id), str(name))
            for company_id, name in candidate_level["not_a_buyer"].items()
        ),
        max_mentions_per_post=candidate_level["max_mentions_per_post"],
    )


def _checked_candidate_terms(candidate_level: dict) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """把 candidate_level 里的词表读出来，两个方向都对一遍。

    输入：YAML 里 candidate_level 那一段
    输出：成对的元组，每项是 (表名, 这张表的词)

    顺序跟着 _CANDIDATE_TEXT_RULES 的登记顺序走，不跟着 YAML 的书写顺序，
    这样改配置里的段落次序不会让报表里的归因跟着变。

    正向：_CANDIDATE_TEXT_RULES 里登记的每张表，配置里必须有，
    少了当场 KeyError，不退化成「这张表是空的所以这条规则不生效」。

    反向：配置里出现了没有实现的表名也要炸。少了这一步的话，把
    supplier_headline 拼错一个字母的后果是那张表整个不生效、供应商
    全放进来，而症状只是「今天垃圾变多」，从报表上查不出来。
    这跟相关性通道那边是同一个理由。
    """
    known = set(_CANDIDATE_TEXT_RULES) | _CANDIDATE_FLAG_KEYS
    unknown = sorted(set(candidate_level) - known)
    if unknown:
        raise ValueError(
            f"config 的 candidate_level 里有没对应实现的键：{unknown}。"
            f"词表可用的是 {sorted(_CANDIDATE_TEXT_RULES)}"
        )
    return tuple((name, tuple(candidate_level[name])) for name in _CANDIDATE_TEXT_RULES)


def _checked_channels(names: Iterable[str]) -> tuple[str, ...]:
    """确认 YAML 里写的通道名都有对应实现，没有就当场炸。

    输入：配置里那串通道名
    输出：同样一串，原样返回

    不认识的名字必须报错而不是跳过。跳过的话，把 mentioned_companies
    拼错一个字母的后果是那条通道悄悄失效、规则一悄悄变严、真买方悄悄
    被多砍一批 —— 这类故障的症状是「今天线索少」，从报表上查不出原因。
    """
    checked = tuple(names)
    unknown = [name for name in checked if name not in _RELEVANCE_CHANNELS]
    if unknown:
        raise ValueError(
            f"config 里的相关性通道没有对应实现：{unknown}。"
            f"可用的是 {sorted(_RELEVANCE_CHANNELS)}"
        )
    return checked


def _has_relevance(signal: Signal, channels: Iterable[str]) -> bool:
    """这条信号有没有至少一个通道能证明它跟我们有关。

    输入：一条信号；启用的通道名
    输出：任意一条通道成立就是 True，全不成立才是 False

    是 any 不是 all：通道之间是「或」的关系。它们各自从不同角度回答
    同一个问题，一条成立就够，要求全都成立等于把闸门收得比只认命中词
    还紧，正好走反方向。
    """
    return any(_RELEVANCE_CHANNELS[name](signal) for name in channels)


def _mention_fanout(candidate: Candidate) -> int | None:
    """这个候选所在的那一段正文，一共 @ 了几家公司。

    输入：一个候选
    输出：那一段 @ 的公司数，或者 None（这个候选不是被 @ 出来的）

    三个例子：

        输入 MENTIONED 候选，外层正文 @ 了 9 家
        输出 9

        输入 REPOST_MENTIONED 候选，原帖正文 @ 了 3 家（外层 @ 了 1 家）
        输出 3   数的是原帖那一段，不是外层

        输入 AUTHOR / HEADLINE / REPOST_AUTHOR / REPOST_HEADLINE 候选
        输出 None

    只有被 @ 出来的候选才有答案，理由跟 _source_headline 那条一模一样：
    「这篇帖子 @ 了一堆公司」说的是这些被 @ 的公司都只是清单里的一项，
    它对发帖人自己那一侧一个字都没说。拿它去砍 author 候选的话，
    一个写盘点文的人如果职位串里正好有家真公司，会被无辜砍掉。

    转发要按段数：转发者写一句、@ 一家，而原帖是个十家清单 —— 两段的
    扇出完全不同，混着数会让其中一段被另一段的行为连累。
    """
    if candidate.lead_type is LeadType.MENTIONED:
        return len(candidate.signal.mentioned_companies)
    if candidate.lead_type is LeadType.REPOST_MENTIONED:
        original = candidate.signal.reposted_from
        return len(original.mentioned_companies) if original is not None else None
    return None


def _not_a_buyer_name(candidate: Candidate, rules: FilterRules) -> str | None:
    """这个候选指向的实体在不在「不是做产品的公司」那张表里。

    输入：一个候选；规则
    输出：命中就返回表里登记的那个名字，没命中返回 None

    两个例子：

        输入 候选指向 CompanyRef("Kickstarter", "413999", ...)
        输出 "Kickstarter"

        输入 候选指向 CompanyRef("Chatterly Inc.", "100000001", ...)
        输出 None

    返回名字而不是返回 True，是为了丢弃记录的 detail 能写出「是表里
    哪一行拦的」。候选自己的名字跟表里登记的可能不一样（同一个 id 在
    不同帖子里被写成 "Meta" 和 "Meta for Developers"），查「这条为什么
    没了」要回到表里那一行，不是回到候选那一行。

    没有 id 的候选直接放行。那是 headline 通道解析出来的引用，它只有名字，
    而这张表刻意不按名字认（名字会撞）。这是已知的覆盖缺口不是 bug ——
    漏掉一个的代价是一次 LLM 调用，按名字砍的代价是可能杀掉真买方。
    """
    company_id = candidate.company.linkedin_company_id
    if not company_id:
        return None

    # 表很短（十几行），线性扫一遍比建索引清楚。哪天长到几百行再说 ——
    # 而按 filters.yaml 里写的，它本来就不该长到那个程度。
    for blocked_id, name in rules.not_a_buyer:
        if blocked_id == company_id:
            return name
    return None


def _terms_in(text: str, terms: Iterable[str]) -> tuple[str, ...]:
    """找出全部出现在文本里的词，用来生成丢弃记录的 detail。

    输入：要检查的文本，和一组词
    输出：命中的全部词，按词表书写顺序；一个都没有就是空元组

    不在第一个命中就停，跟 signal 级那三张词表的做法一致：只记第一个的话，
    改词表时不知道剩下几个词是不是也该动，而一条职位串同时命中
    "OEM/ODM" 和 "Export Sales" 跟只命中一个，说明的强度并不一样。

    大小写不敏感，子串匹配不看单词边界 —— 这里的词表都是多词短语或者
    行业术语（"Buying Guide"、"OEM/ODM"），不像命中词那样会出现
    audio 撞 audiobook 那类问题，而放宽一点能抓到 OEM/ODMs 这种变体。
    """
    lowered = text.lower()
    return tuple(term for term in terms if term.lower() in lowered)


def _all_hits_in(text: str, rules: FilterRules) -> dict[SignalDropReason, tuple[str, ...]]:
    """把三张噪音词表在正文里命中的词全部找出来，不短路。

    输入：正文，和一整套规则
    输出：命中了的那几类 → 这一类命中的全部词。一类都没中就是空字典

    一个例子：

        输入   一篇写 "CNC machining" 又写 "Factory Price" 和 "Wholesale" 的工厂 SEO 文
        输出   {HARDWARE_STORE:  ("CNC machining",),
                SUPPLIER_CONTENT: ("Factory Price", "Wholesale")}

    没命中的类**不出现在字典里**，不是给一个空元组。这样调用处 len(hits)
    直接就是「命中了几类」，不用再过滤空值。

    值存的是词本身不是个数：个数用 len() 就有，反过来从个数推不出词，
    而改词表的时候要知道的恰恰是「该动哪一个词」。

    不短路是这个函数存在的全部意义。短路的话，一条正文只要先撞上五金词，
    它同时也是供应商 SEO 文这件事就永远不会被记下来，而报表上
    supplier_content 的砍量因此长期偏低，看起来像那条规则不该留。

    顺序是焊死的：外层按规则优先级、内层按词表在 YAML 里的书写顺序，
    这样同一批数据两次跑出来的报表能逐字对照。
    """
    hits: dict[SignalDropReason, tuple[str, ...]] = {}
    lowered = text.lower()
    for reason, terms in (
        (SignalDropReason.HARDWARE_STORE, rules.hardware_store),
        (SignalDropReason.SUPPLIER_CONTENT, rules.supplier_content),
        (SignalDropReason.OFF_TOPIC, rules.off_topic),
    ):
        matched = tuple(term for term in terms if term.lower() in lowered)
        if matched:
            hits[reason] = matched
    return hits


def _format_hits(hits: Mapping[StrEnum, tuple[str, ...]]) -> str:
    """把多类命中拼成一行 detail，给报表和人工抽查看。

    输入：命中表，signal 级和 candidate 级的都收
    输出：形如 "hardware_store x1 (CNC machining), supplier_content x2
          (Factory Price, Wholesale)" 的一行

    两级共用一个函数，是为了两边的 detail 长得一样 —— 报表要把两级的
    丢弃并在一张表里看，格式漂了就得写两套解析。

    个数和词都写：个数是拿来比大小的，词是拿来改词表的，少哪个都得回头
    再翻一次原文。
    """
    return ", ".join(
        f"{reason.value} x{len(terms)} ({', '.join(terms)})" for reason, terms in hits.items()
    )


def drop_signals(
    signals: Iterable[Signal],
    *,
    rules: FilterRules,
    seen_content_hashes: frozenset[str] = frozenset(),
    run_seen: frozenset[str] = frozenset(),
) -> tuple[tuple[Signal, ...], tuple[SignalDrop, ...]]:
    """signal 级闸门：整条丢弃。只放跟发帖人身份无关的理由。

    输入：这一跑的全部信号；规则；更早那些跑见过的内容摘要；
          以及这一跑前面几条 query 已经留下的内容摘要
    输出：两个元组，留下来的信号，和每条被丢信号的丢弃记录

    一个例子：

        输入   三条信号，第一条既没有命中词也没有被 @ 的公司，第二条正文带
               "How to Choose a"，第三条正常
        输出   留下 (第三条,)，
               丢弃记录 (SignalDrop(reason=NO_RELEVANCE_SIGNAL, ...),
                        SignalDrop(reason=SUPPLIER_CONTENT, detail="How to Choose a"))

    两个 seen 集合分开传，不合成一个。它们挡掉的东西一样，记的账不一样：

        seen_content_hashes  更早那些跑见过的，从库里查出来传进来
                             -> SEEN_HASH_PRIOR_RUN，正常损耗
        run_seen             这一跑前面几条 query 已经留下的
                             -> SEEN_HASH_IN_RUN，跟这一次调用里
                                自己撞上的算同一类

    合成一个的话，跨 query 撞车会被记成「更早见过」。而这两个数的运营
    含义相反：前者是昨天已经处理过，后者说明今天这几条 query 在互相
    重叠地采同一批帖，那是 query 组队要管的事。合并之后重叠会伪装成
    正常损耗，报表上完全看不出来。

    这个函数自己不碰数据库 —— 它是纯函数，同样的输入永远给同样的输出，
    这样才能拿历史数据反复回归。两个默认空集合是为了不带库也能跑，
    那是开发时的用法。

    每条信号最多记一个丢弃理由。归因只能有一条，否则「各规则砍了多少」
    的合计会超过总丢弃数，报表对不上。规则顺序因此就是归因顺序，
    排在前面的规则会吃掉后面规则的功劳。

    噪音词表那三条（规则三到五）之间是个例外：它们不分先后，而是先把
    三张表全查完，只中一类就归给那一类，中了不止一类归给
    MULTIPLE_RULE_HITS。所以前三条的砍量各自表示「只有这一类命中」，
    互相不抢功劳。规则一二仍然在它们之前短路，那两条跟词表问的不是
    同一件事，谁先谁后的影响要靠改 YAML 重跑来量，不靠这里埋数据。

    留下来的顺序保持输入顺序，丢弃记录按丢弃发生的顺序。两边都稳定，
    回归对照才有立足点。
    """
    kept: list[Signal] = []
    drops: list[SignalDrop] = []

    #        seen 是两批的并集，每留下一条信号就把它的摘要加进去。
    # 所以它同时承担跑内去重和跨天去重，两者用的是同一个判断，
    # 分开的只有归因（见下面挑 reason 那几行）。
    # 只有留下来的才加：被别的规则砍掉的那条不该占住这个摘要，
    # 否则同一篇正文的第二条会因为「见过了」被丢，而第一条其实也没留下。
    seen = set(seen_content_hashes) | set(run_seen)

    for signal in signals:
        # 规则一：一个相关性来源都没有。砍量最大，所以排第一。
        # 它量的是 LinkedIn 语义相关匹配和我们的判据之间的落差。
        #
        # 判据是 relevance_channels 里任意一条成立，不是只看命中词。
        # 只看命中词的旧写法实测砍掉三分之一以上的真买方，因为那样是把
        # 两类东西混在一个布尔量里砍：语义搜索硬拉进来的完全不相关内容，
        # 和语义相关、只是换了说法的真买方。加一条身份精确通道就能把
        # 后一类捞回来大半。各方案的实测对照跑 scripts/experiment.py。
        if rules.require_relevance_signal and not _has_relevance(signal, rules.relevance_channels):
            drops.append(
                SignalDrop(
                    external_id=signal.external_id,
                    reason=SignalDropReason.NO_RELEVANCE_SIGNAL,
                    detail=f"query={signal.query_search}",
                )
            )
            continue

        # 规则二：这段正文见过了。跑内和跨天是两个 reason 不是一个，
        # 因为漏斗报表按 reason 聚合，而这两个数的含义完全不同：
        # 跨天是正常损耗（昨天已经处理过），跑内是联播、或者同一帖被多条
        # query 搜到 —— 后者是 query 有重叠的信号，归 query_intelligence 管。
        if rules.drop_seen_content_hash and signal.content_hash in seen:
            #        seen_reason：只有在 seen_content_hashes 里的才算更早
            # 那些跑见过的。剩下两种来源都算这一跑内 —— run_seen 里的
            # （这一跑前面几条 query 留下的）和这个循环中途加进去的
            # （同一批信号里前面那条），它们是同一件事，只是隔了一次调用。
            if signal.content_hash in seen_content_hashes:
                seen_reason = SignalDropReason.SEEN_HASH_PRIOR_RUN
            else:
                seen_reason = SignalDropReason.SEEN_HASH_IN_RUN
            drops.append(
                SignalDrop(
                    external_id=signal.external_id,
                    reason=seen_reason,
                    detail=f"hash={signal.content_hash}",
                )
            )
            continue

        # 规则三到五：正文里出现了明确属于另一个行业或另一类内容的词。
        #        hits 是三张词表各自命中了哪些词，三张全查完不短路。
        # 只中一类就归给那一类，detail 写命中的词（改词表时要知道动哪个）；
        # 中了不止一类说明这条正文同时踩了几种噪音，单独归一类，
        # 免得排序靠前的词表把后面那些的砍量吃掉。
        hits = _all_hits_in(signal.text, rules)
        if len(hits) == 1:
            reason, terms = next(iter(hits.items()))
            drops.append(
                SignalDrop(
                    external_id=signal.external_id,
                    reason=reason,
                    detail=", ".join(terms),
                )
            )
            continue
        if len(hits) > 1:
            drops.append(
                SignalDrop(
                    external_id=signal.external_id,
                    reason=SignalDropReason.MULTIPLE_RULE_HITS,
                    detail=_format_hits(hits),
                )
            )
            continue

        kept.append(signal)
        seen.add(signal.content_hash)

    return tuple(kept), tuple(drops)


def _source_headline(candidate: Candidate) -> str | None:
    """这个候选的公司身份是从谁的职位串来的。

    输入：一个候选
    输出：那个人的职位串，或者 None（这个候选跟任何人的职位串都无关）

    只有 author 一侧的候选才有答案：它们的公司就是发帖那个人所属的公司，
    所以那个人的职位串描述的正是这家公司。被 @ 的公司返回 None ——
    发帖人的职位串描述的是发帖人自己，跟他提到的那家公司毫无关系。
    拿发帖人的职位串去判被 @ 的公司，会把供应商发帖提到的真买方一起砍掉，
    而那正是三段基数拆成一对多要救的那种线索。
    """
    if candidate.lead_type in {LeadType.AUTHOR, LeadType.HEADLINE}:
        return candidate.signal.actor_headline
    if candidate.lead_type in {LeadType.REPOST_AUTHOR, LeadType.REPOST_HEADLINE}:
        original = candidate.signal.reposted_from
        return original.actor_headline if original is not None else None
    return None


# candidate 级的词表规则：表名 → （丢弃理由，这张表查候选身上的哪段文本）。
#
# 比 signal 级那三张表多带一个取文本的函数，因为两级的情况不一样：
# signal 级三张表查的都是同一段正文，candidate 级每张表查的文本可能不同 ——
# 这张查发帖人的职位串，以后的可能查公司名、查官网。
#
# 把「查哪段文本」写进表里而不是散在判断逻辑里，是因为这正是当初出层级
# bug 的地方：拿发帖人的职位串去判他 @ 的那家公司。取文本的函数对不该
# 适用的通道返回 None，规则就天然碰不到那些候选，不用再写一遍条件。
#
# 加一张表 = YAML 加一段 + 这里加一行 + 枚举加一个值，判断逻辑不动。
_CANDIDATE_TEXT_RULES: dict[str, tuple[CandidateDropReason, Callable[[Candidate], str | None]]] = {
    "supplier_headline": (CandidateDropReason.SUPPLIER_HEADLINE, _source_headline),
}

# candidate_level 里不是词表的键。列出来是为了让 _checked_candidate_terms
# 能分辨「这个键是开关」和「这个键是拼错的表名」。
_CANDIDATE_FLAG_KEYS = frozenset(
    {"drop_company_page_author", "not_a_buyer", "max_mentions_per_post"}
)


def _candidate_hits(
    candidate: Candidate, rules: FilterRules
) -> dict[CandidateDropReason, tuple[str, ...]]:
    """把 candidate 级的全部词表在这个候选身上查一遍，不短路。

    输入：一个候选；一整套规则
    输出：命中了的那几类 → 这一类命中的全部词。一类都没中就是空字典

    一个例子：

        输入   一个 HEADLINE 候选，发帖人职位串是
               "OEM/ODM Export Sales Manager"
        输出   {SUPPLIER_HEADLINE: ("OEM/ODM", "Export Sales")}

    跟 signal 级的 _all_hits_in 同构：不短路、没命中的类不出现在字典里、
    值存词不存个数。理由那边写了，这里不重复。

    取文本返回 None 的表直接跳过 —— 那表示这张表不适用于这个通道，
    不是「这段文本是空的」。两者的区别很要紧：supplier_headline 对
    mentioned 候选返回 None，是因为发帖人的职位串跟他 @ 的公司无关，
    而不是因为那家公司没有职位串。
    """
    hits: dict[CandidateDropReason, tuple[str, ...]] = {}
    for name, terms in rules.candidate_terms:
        reason, get_text = _CANDIDATE_TEXT_RULES[name]
        text = get_text(candidate)
        if text is None:
            continue
        matched = _terms_in(text, terms)
        if matched:
            hits[reason] = matched
    return hits


def drop_candidates(
    candidates: Iterable[Candidate],
    *,
    rules: FilterRules,
) -> tuple[tuple[Candidate, ...], tuple[CandidateDrop, ...]]:
    """candidate 级闸门：只丢这一个候选，同一条信号的其他候选照留。

    输入：展开出来的全部候选；规则
    输出：两个元组，留下来的候选，和每条被丢候选的丢弃记录

    一个例子。供应商从个人号发帖吹「我们帮 Lumen 做了 5000 只耳机」，
    正文里 @ 了 Lumen，这条信号展开出两个候选：

        输入   (Candidate(深圳某音频厂, HEADLINE, 信号),
                Candidate(Lumen, MENTIONED, 信号))
        输出   留下 (Candidate(Lumen, MENTIONED, 信号),)
               丢弃记录 (CandidateDrop(reason=SUPPLIER_HEADLINE, detail="OEM/ODM"),)

    这两个候选来自同一条信号却命运相反，这就是丢弃规则必须挂在候选上、
    不能挂在信号上的全部理由。写成整条丢弃的话，Lumen 这个真买方
    会跟着供应商一起被杀掉。

    跟 signal 级一样，每个候选最多记一个理由，规则顺序就是归因顺序。
    """
    kept: list[Candidate] = []
    drops: list[CandidateDrop] = []

    for candidate in candidates:
        # 规则六：公司主页发的帖，它的 author 候选就是这家公司自己。
        # 只砍 author 一侧，同一条信号里被 @ 的公司照留 —— 加速器从公司
        # 主页发帖恭喜被投公司众筹成功时，发帖人不是买方，被提到的才是。
        if rules.drop_company_page_author and candidate.lead_type in _COMPANY_PAGE_SIDE:
            drops.append(
                CandidateDrop(
                    external_id=candidate.signal.external_id,
                    company_name=candidate.company.name,
                    lead_type=candidate.lead_type,
                    reason=CandidateDropReason.COMPANY_PAGE_AUTHOR,
                    detail=candidate.lead_type.value,
                )
            )
            continue

        # 规则九：这一段正文 @ 了太多公司。排在规则八之前只是归因顺序 ——
        # 「这篇帖子根本不是在谈某一家的处境」比「这家实体不做产品」更靠外，
        # 一个候选最多记一个理由，先归外面那层。
        fanout = _mention_fanout(candidate)
        if fanout is not None and fanout > rules.max_mentions_per_post:
            drops.append(
                CandidateDrop(
                    external_id=candidate.signal.external_id,
                    company_name=candidate.company.name,
                    lead_type=candidate.lead_type,
                    reason=CandidateDropReason.MENTION_FANOUT,
                    # detail 记这一段 @ 了几家。查「这条为什么没了」时，
                    # 这个数直接告诉你该不该调阈值，比只记一个规则名有用。
                    detail=f"{fanout} mentions",
                )
            )
            continue

        # 规则八：这个实体不是「做产品的公司」（众筹平台、媒体、投资方等）。
        # 排在词表之前只是归因顺序：一个候选最多记一个理由，而「它根本不是
        # 一家做产品的公司」比「它的职位串像供应商」更根本，先归这一类。
        #
        # 只看 id 不看名字。名字会撞（这批数据里就有叫 Gamma / Notion / Work
        # 的实体），而 mentioned 通道实测 120/120 都带 id。
        # 只有名字的候选（headline 通道）这条规则天然碰不到，那是已知的
        # 覆盖缺口，不是 bug —— 理由见 config/filters.yaml 的规则⑧。
        blocked = _not_a_buyer_name(candidate, rules)
        if blocked is not None:
            drops.append(
                CandidateDrop(
                    external_id=candidate.signal.external_id,
                    company_name=candidate.company.name,
                    lead_type=candidate.lead_type,
                    reason=CandidateDropReason.NOT_A_BUYER,
                    # detail 记表里登记的那个名字，不记候选自己的名字：
                    # 两者可能不一样（"Meta" vs "Meta for Developers"），
                    # 而查「这条为什么没了」要回到表里那一行。
                    detail=blocked,
                )
            )
            continue

        # 规则七：候选级的词表。现在只有一张（职位串一看就是供应商侧的人），
        # 但走的是跟 signal 级同构的表驱动写法：全查完不短路，只中一类归
        # 那一类，中了不止一类归 MULTIPLE_RULE_HITS。
        # 哪张表查哪段文本写在 _CANDIDATE_TEXT_RULES 里，不适用的通道
        # 取文本返回 None，规则天然碰不到它们。
        hits = _candidate_hits(candidate, rules)
        if len(hits) == 1:
            reason, terms = next(iter(hits.items()))
            drops.append(
                CandidateDrop(
                    external_id=candidate.signal.external_id,
                    company_name=candidate.company.name,
                    lead_type=candidate.lead_type,
                    reason=reason,
                    detail=", ".join(terms),
                )
            )
            continue
        if len(hits) > 1:
            drops.append(
                CandidateDrop(
                    external_id=candidate.signal.external_id,
                    company_name=candidate.company.name,
                    lead_type=candidate.lead_type,
                    reason=CandidateDropReason.MULTIPLE_RULE_HITS,
                    detail=_format_hits(hits),
                )
            )
            continue

        # 规则八：建不出身份键。这一条不在配置里，因为它不是可调的策略，
        # 是数据本身的硬约束 —— 连公司名都没有的候选，后面归并和查库
        # 都无从下手。放在这里丢是为了让它出现在漏斗报表里，
        # 而不是在归并那一步炸掉整跑。
        try:
            # 只是要触发它，看会不会抛。赋给下划线是因为返回值这里用不上，
            # 而直接写一句表达式会被 lint 当成写错了。
            _ = candidate.company.identity_key
        except IdentityUnresolvable as exc:
            drops.append(
                CandidateDrop(
                    external_id=candidate.signal.external_id,
                    company_name=candidate.company.name,
                    lead_type=candidate.lead_type,
                    reason=CandidateDropReason.NO_IDENTITY,
                    detail=str(exc),
                )
            )
            continue

        kept.append(candidate)

    return tuple(kept), tuple(drops)
