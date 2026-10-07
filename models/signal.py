"""信号与候选主体 —— pipeline 的三段基数。

    raw item  1:1   Signal          证据单位
    Signal    1:0..N Candidate      这条信号指向哪些公司
    Candidate N:1   CompanyCluster  按身份归并后的一家公司

中间那两跳是整个设计的关键。把它压成 1:1（一条结果 = 一个公司 = 发帖人）
就会丢掉「帖子提到的公司」这条通道 —— 那条通道虽然只占约 15% 的量，
却是唯一身份精确的来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from typing import Any

from models.company import CompanyRef


class SourceType(StrEnum):
    """一条信号是从哪种东西上采来的。对应 queries.query_type。"""

    LINKEDIN_POST = "linkedin_post"
    LINKEDIN_COMPANY = "linkedin_company"


class ActorType(StrEnum):
    """发帖人是个人还是公司主页。

    实测不变式（两跑各 100% 成立）：
        raw["author"]["companyId"] 有值  等价于  author.type == "company"

    也就是说 profile 作者拿不到公司 id，它们的公司身份只能从 author.info
    那串职位文本里解析，不可靠。这是 mentioned 候选（身份精确）
    存在价值的根本原因。
    """

    PROFILE = "profile"
    COMPANY = "company"


class LeadType(StrEnum):
    """这个候选是通过哪条通道发现的。

    六个值对应六条通道，刻意分这么细而不是只分「发帖人」和「被提到的公司」，
    是因为每条通道的产出要能单独统计。其中两条我们最不确定值不值得留：
    headline 兜底是靠文本猜的，转发那三条是后来才接上的。粗到只有两个值的话，
    报表上看不出这两条各贡献了多少，也就没法判断该不该继续维护它们。

    这六个值不是平的，按「这话是谁说的」分成两组，写话术时用这个分组：

        第一人称，可以直接引用他的原文
            AUTHOR / HEADLINE / REPOST_AUTHOR / REPOST_HEADLINE
        从第三方口中得知的
            MENTIONED / REPOST_MENTIONED

    这两组的开场句根本不能共用 —— 从第三方口中知道的那类，装作偶然看到很假。
    所以通道必须在数据模型里就记下来，不能等到写话术的时候在 prompt 里
    临时判断。

    这个分组现在做成了 is_first_person（2026-08-29 加的）。原先这里写着
    「还没有消费者，写 draft 那一期真要分话术时再加」—— 那个前提已经不成立：
    analyzer 喂 prompt 时必须逐条证据说明「说这段话的人是不是这家公司的人」，
    否则被 @ 出来的公司会拿发帖人的职位去判 seniority（实测一跑 133 家里
    96 家只有第三方通道，这是主流情况不是边角情况）。
    放成属性而不是让 analyzer 自己列一个集合，是因为 draft 那一期要用同一个
    分组，两处各列一份的话哪天漂了就是两层对同一条证据做出不同判断，而且不报错。

    另外前后两组各自内部的身份强度也不同：AUTHOR 和 REPOST_AUTHOR 带
    LinkedIn 公司 id，HEADLINE 和 REPOST_HEADLINE 只有名字。那个区别在
    CompanyRef.is_weak_identity 上已经有了，这里不重复表达。
    """

    AUTHOR = "author"  # 发帖人所属公司，数据源直接给了公司 id
    HEADLINE = "headline"  # 从发帖人的职位串里兜底解析出来的，身份最弱
    MENTIONED = "mentioned"  # 正文里被 @ 链接的公司
    REPOST_AUTHOR = "repost_author"  # 被转发原帖的作者所属公司，数据源给了公司 id
    REPOST_HEADLINE = "repost_headline"  # 从原帖作者的职位串里解析出来的
    REPOST_MENTIONED = "repost_mentioned"  # 原帖正文里被 @ 链接的公司

    @property
    def is_first_person(self) -> bool:
        """这条通道拿到的证据，是不是这家公司自己的人说的。

        输出：True 表示第一人称，False 表示从第三方口中得知

            LeadType.AUTHOR.is_first_person            -> True
            LeadType.REPOST_AUTHOR.is_first_person     -> True
            LeadType.MENTIONED.is_first_person         -> False
            LeadType.REPOST_MENTIONED.is_first_person  -> False

        两个消费者，问的是同一件事的两面：analyzer 拿它判 seniority
        该看谁的职位串（全 False 就是没有人代表这家公司说过话，那一维是 0），
        draft 那一期拿它决定开场句 —— 从第三方口中知道的那类，装作偶然看到很假。
        """
        return self in {
            LeadType.AUTHOR,
            LeadType.HEADLINE,
            LeadType.REPOST_AUTHOR,
            LeadType.REPOST_HEADLINE,
        }

    @property
    def is_from_original_post(self) -> bool:
        """这条通道说的是不是转发里那条原帖。

        输出：True 表示这条候选是从原帖那一段里抽出来的

            LeadType.REPOST_AUTHOR.is_from_original_post  -> True
            LeadType.AUTHOR.is_from_original_post         -> False

        转发的信号有两段正文（转发者自己写的那句 + 原帖），两段的作者不是
        同一个人。要把通道配到正确的那一段上，就得先能问出这个问题 ——
        问不出来的话，原帖里「我们刚做完众筹」会被记在转发者头上。
        """
        return self in {
            LeadType.REPOST_AUTHOR,
            LeadType.REPOST_HEADLINE,
            LeadType.REPOST_MENTIONED,
        }


RawItem = dict[str, Any]  # 供应商返回的原始 JSON，一个字都不动地留着


@dataclass(frozen=True)
class RepostedFrom:
    """被转发的那条原帖。转发（点转发按钮）时才有，实测 10/226。

    为什么需要它：转发这个动作会让「说话的人」和「我们记下的人」错位。
    外层 author 是转发的人，正文是转发者自己加的那句话；真正有内容的原帖
    连作者带正文都在这个对象里。只读外层的话：

        转发者 João Aguiar  profile  <- 我们记成了这个
        原帖   ZENTRAai    company  <- 事实在这里

    
    反方向同样要紧：转发者可能只写「So cool to see this」，而原帖是
    一家深圳音频厂的产线宣传。不看原帖的 headline，supplier_headline
    那条规则拦不住它，供应商会被当成线索送进 LLM。

    刻意不做成 RawSignal 的子类，也刻意不展开成第二条 RawSignal：

      · 原帖不是一条检索结果，它没有 query 字段。塞一个 query_search 进去
        等于伪造归因（「这条是那个词搜出来的」是假的）
      · 展开成第二条会让同一个 external_id 出现在两条 signal 上，而它是
        跑内去重的键；raw_result_count 和计费条数的对账也会跟着糊掉

    原帖的完整 JSON 不在这里重复存一份，它已经在外层 RawSignal.raw["repost"] 里。
    """

    external_id: str  # 原帖的 activity id。实测等于外层的 repostId，那个字段不用取
    url: str
    text: str  # 原帖正文。转发者那句话在外层 RawSignal.text，两者是两段不同的话

    # 下面五个的含义跟 RawSignal 的同名字段完全一致，只是描述的是原帖作者。
    # 作者是个人还是公司主页那套区别在这里同样成立（实测原帖 5 个 company、
    # 5 个 profile，作者不变式 10/10 也成立）。
    actor_name: str
    actor_headline: str
    actor_url: str
    actor_type: ActorType
    actor_company: CompanyRef | None

    # 原帖里被 @ 链接的公司。跟外层的是两套，不是同一套
    mentioned_companies: tuple[CompanyRef, ...]

    observed_at: datetime  # 原帖发布时刻。一定早于外层，见 RawSignal.event_at


@dataclass(frozen=True)
class RawSignal:
    """adapter 的产出：把供应商字段直译成我们的词汇，不算任何派生字段。

    「翻译供应商格式」这件事只有 adapter 知道怎么做，所以它留在 adapters/ 里。
    normalize 只处理跟供应商无关的事，这样换数据源时 discovery/ 一行不动。
    """

    source: str  # "harvestapi_posts"
    source_type: SourceType
    external_id: str  # HarvestAPI 用 `id` 列，不是 `postId`（后者实测全空）
    url: str
    text: str  # 唯一可引用的原文，防幻觉闸门 assert 的对象
    query_search: str  # 归因：这条是哪个 query 搜出来的
    actor_name: str
    actor_headline: str  # HarvestAPI 的 author.info，供应商 headline 规则看它
    actor_url: str
    actor_type: ActorType

    # 发帖人所属的公司，adapter 能结构化拿到时才填。
    # HarvestAPI 的 post search 只在 actor_type == COMPANY 时给得出
    # （profile 作者的 author.id 是个人 URN，不是公司 id，别往这里塞）。
    # 填不了就 None，由 normalize 退回去解析 actor_headline —— 那是判断不是翻译，
    # 所以不放 adapter 里。将来换一个能给 profile 作者公司的供应商，
    # 这个字段自然就填上了，normalize 那条解析路径会自动让位。
    actor_company: CompanyRef | None

    # 正文里被 @ 链接的公司，来自 contentAttributes 的 COMPANY_NAME 条目。
    # 注意它的含义比「提到」窄：只抓被 @ 链接的实体，纯文字写出来但没链接的
    # 公司抓不到。所以这条通道是高精度、低召回 —— 拿到的一定对，但漏不少。
    # 要补召回得上 NER 或 LLM 抽取，那是另一笔钱，现在不做。
    mentioned_companies: tuple[CompanyRef, ...]

    # 这条是不是转发，以及被转发的原帖是什么。不是转发就 None。
    # 这是第四个「重复」维度，跟前三个抓的是不同现象，互相取代不了：
    #     同一 post 被多条 query 返回       external_id 跑内去重        
    #     同一 post 跨天再来                external_id 跨天 seen      
    #     同一篇正文被不同账号各自发一遍     content_hash              
    #     点了转发按钮                      这个字段                   
    # 最后一个是供应商直接给的结构化标记，比按正文算 hash 更准也更早。
    reposted_from: RepostedFrom | None

    observed_at: datetime
    raw: RawItem

    @property
    def quotable_texts(self) -> tuple[str, ...]:
        """这条信号里所有可以被引用的原文。防幻觉闸门的取材范围。

        输出：一到两段文本 —— 自己的正文，加上原帖正文（如果这条是转发）

        必须包含原帖正文，否则闸门会误判：转发的时候真正有内容的话在原帖里
        （转发者常常只写一句「So cool to see this」），LLM 引用原帖是对的，
        而只拿 self.text 去比会把它判成幻觉，把真线索一起丢掉。
        """
        if self.reposted_from is None:
            return (self.text,)
        return (self.text, self.reposted_from.text)

    @property
    def quotable_headlines(self) -> tuple[str, ...]:
        """这条信号里那几个人的职位串，也算可以被引用的原文。

        输出：零到两段文本 —— 发帖人的职位串，加上原帖作者的职位串
              （如果这条是转发）。空的那些不返回

        为什么它也算可引用：判据是「这句话是不是这个人自己公开写下的、
        而且我们手上有原件」，不是「它在哪个字段里」。LinkedIn 的职位串
        就是本人写的自我介绍，跟正文一样有原件可对。

        为什么必须算：给模型看的那条证据里同时有正文和职位串
        （analyzer._evidence_item 的 speaker_headline 字段），而闸门原先
        只认正文 —— 我们给它看两种文本，却只承认一种。实测这条洞会
        误杀真买方：一条创始人自己发的帖，模型引了他职位串里那句
        "Seratone is a plug-and-play karaoke that turns any speaker into
        a studio"（正文里没有这句），整家公司被判成幻觉丢掉，
        丢之前还多花了一次重问。

        它跟正文的地位不一样，只是「够不够格被引用」这一关上相同：
        职位串是常年不变的自我介绍，不是「现在正在发生的事」，
        所以 signal_strength 那一维不该从它身上读出高分 ——
        那条要求写在 prompt 里（引用优先取正文），不在这里。
        这里只回答「这句有没有原件」，接不接受是政策，在调用方。
        """
        headlines = [self.actor_headline]
        if self.reposted_from is not None:
            headlines.append(self.reposted_from.actor_headline)
        # 供应商经常不给职位串（空串），空的不返回：空串会让
        # `"" in text` 恒为真那类洞多一个入口。
        return tuple(headline for headline in headlines if headline)

    @property
    def event_at(self) -> datetime:
        """这件事本身发生的时刻。recency 这一维只能用它，不许用 observed_at。

        输出：转发就取原帖的发布时刻，不是转发就等于 observed_at

        两个时刻问的是两个不同的问题：
            observed_at   什么时候有人在谈这件事
            event_at      这件事什么时候发生

        recency 想量的是后者。用 observed_at 会让一条老消息每被转发一次就
        重新变「新」—— 而转发恰恰是老消息才有的待遇，等于给陈旧信号加分。
        实测转发最多只晚 2 天，量上无所谓，要紧的是别让口径反着。

        写成 property 而不是在打分层里判断，是因为「用哪个时刻」是全项目
        统一的一条口径，不能让 analyzer 和 scheduled 各判一次然后判出两样。
        """
        if self.reposted_from is None:
            return self.observed_at
        return self.reposted_from.observed_at


@dataclass(frozen=True)
class Signal(RawSignal):
    """normalize 的产出：RawSignal 加三个派生字段。

    分成两个类型是为了让「半填状态」不可能存在 —— 一个 Signal 拿在手上
    就保证 content_hash、matched_terms 和 fact_hash 都算过了。
    """

    # 这条信号带的全部正文归一化后的摘要。转发的话「全部正文」包括
    # 转发者自己那句话和原帖正文两段，理由见 normalize.to_signal。
    #
    # 它回答的问题很窄：这两段正文是不是同一段。别指望它回答「这两条信号
    # 是不是同一件事」—— 那是 Signal.fact_hash，转发时两者会分岔。
    #
    # 它不是主力去重手段。同一条 post 被多条 query 重复返回、以及跨天再来，
    # 靠 external_id 就够了，那是精确键。摘要补的是 external_id 覆盖不到的
    # 一种情况：同一篇正文被不同账号各自重新发一遍，那时候作者、URL、
    # external_id 全都不同，只有正文本身一样。
    #
    # 保留它的主要理由不在去重，而在 CompanyCluster.corroboration 的分母 ——
    # 一份新闻稿被几个账号各自发一遍，这几条都带命中词、都指向同一家公司，
    # 不按内容去重就会把「一个事实」数成好几个。
    content_hash: str

    # 命中词在 text 里的实际出现。空 = 砍，零成本、100% 准确、没有假阳性。
    # 第二个用途：query 通常有 5 个 OR 分支，记下具体命中哪个，
    # 才知道该删哪个分支。
    matched_terms: tuple[str, ...]

    # 这条信号代表的「独立事实」的摘要。CompanyCluster.corroboration 按它去重。
    #
    # 跟 content_hash 问的不是一件事：
    #     content_hash   这两段正文是不是同一段
    #     fact_hash      这两条信号说的是不是同一件事
    #
    # 非转发时两者相等。转发时取哪一段由 normalize 决定，判据是转发者
    # 自己写的那段还有没有相关性（命中词，或者他自己正文里被 @ 的公司）：
    #     有    取他自己那段 —— 他说的是自己的事，那是一件独立的事
    #     没有  取原帖正文 —— 他只是把别人的话又传了一遍
    #
    # 取原帖正文那一支顺带对上了另一种情形：原帖本身也被单独抓到时，
    # 它自己的 fact_hash 就是同一段正文的摘要，两边天然相等，
    # 一件事不会被数成两件。
    #
    # 为什么不再带类别标签（旧写法是 ("repost", 原帖id) 这样的二元组）：
    # 标签一旦参与比较，「转发指向 A」和「A 本身」就永远不相等，
    # 而它们说的正是同一件事。
    fact_hash: str

    @classmethod
    def from_raw(
        cls,
        raw: RawSignal,
        *,
        content_hash: str,
        matched_terms: tuple[str, ...],
        fact_hash: str,
    ) -> Signal:
        base = {f.name: getattr(raw, f.name) for f in fields(RawSignal)}
        return cls(
            **base,
            content_hash=content_hash,
            matched_terms=matched_terms,
            fact_hash=fact_hash,
        )

    @property
    def row_key(self) -> tuple[str, str]:
        """这条信号在一跑之内的行标识。同一行不该被当成两份证据。

        输出：帖子在外部系统里的 id，加上把它搜出来的那条 query

        为什么不只用 external_id：同一个帖子被两条不同的 query 搜到时，
        会作为两条独立结果返回，它们是两行、各自带着不同的归因。
        只按帖子 id 认的话会抹掉其中一条的归因，而归因决定哪条 query
        值不值得继续跑。

        它跟 fact_hash 问的不是一件事，两层都要有：
            row_key    这是不是同一行数据
            fact_hash  这两行说的是不是同一件事
        """
        return (self.external_id, self.query_search)


@dataclass(frozen=True)
class Candidate:
    """一条信号指向的一个候选主体。

    同一条 raw item 可以产出两个候选，而且命运相反 ——
    供应商发帖吹「我们帮 Lumen 做了 5000 只耳机」：
        author 候选     = Shenzhen XX Audio  供应商 headline 命中，砍
        mentioned 候选  = Lumen              真买方，留
    这就是丢弃规则必须挂在 candidate 上、而不是挂在 signal 上的直接原因。
    """

    company: CompanyRef
    # 这个候选是从哪条通道来的。六个值都可能出现，to_candidates 按固定顺序展开：
    #     AUTHOR            发帖人所属公司，数据源直接给了公司 id
    #     HEADLINE          同上，但结构化字段没有，是从发帖人职位串兜底解析的
    #     MENTIONED         正文里被 @ 链接的公司
    #     REPOST_AUTHOR     被转发原帖的作者所属公司，数据源给了公司 id
    #     REPOST_HEADLINE   同上，从原帖作者的职位串解析
    #     REPOST_MENTIONED  原帖正文里被 @ 链接的公司
    # 后三个只在这条信号是转发时才可能出现。
    # AUTHOR 和 HEADLINE 互斥（同一条通道，有结构化就不走兜底解析），
    # REPOST_AUTHOR 和 REPOST_HEADLINE 同理。
    #
    # prefilter 按通道分两侧看：前后各三个里，AUTHOR / HEADLINE /
    # REPOST_AUTHOR / REPOST_HEADLINE 是「发帖人自己那家公司」，
    # MENTIONED / REPOST_MENTIONED 是「他提到的别家公司」。
    # 跟发帖人身份有关的规则只能砍前一侧，砍到后一侧就会杀掉真线索。
    lead_type: LeadType
    signal: Signal


@dataclass
class CompanyCluster:
    """按身份键归并后的一家公司，带上所有支持它的证据。

    unique_company_count 就是归并出来的簇数。

    装的是 Candidate 而不是拆开的「信号列表加通道集合」。这一点是必须的：
    Candidate 本身就是「公司、通道、信号」的三元组，配对信息它自己带着。
    拆成两个字段存的话，「这条证据是从哪条通道来的」当场就丢了，
    而那正是 LeadType 存在的全部理由 —— 第一人称的证据和从第三方口中
    得知的证据，开场句根本不能共用。存了通道却对不上证据，等于没存。

    同一条信号可以在这里出现两次，那不是重复：公司主页发帖又 @ 了自己的话，
    同一个帖子会经由两条通道各产出一个候选，两个都是真的。要「有哪些证据行」
    的时候读 signals，它按行去重。
    """

    ref: CompanyRef
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def lead_types(self) -> set[LeadType]:
        """这家公司是通过哪几条通道被发现的。

        输出：通道的集合

        只回答「有没有走过这条通道」这一个问题，比如「这家公司有没有
        任何一条第一人称的证据」。要知道某一条具体证据走的是哪条通道，
        看 candidates 里对应那一项，别从这个集合反推。
        """
        return {candidate.lead_type for candidate in self.candidates}

    @property
    def signals(self) -> list[Signal]:
        """支持这家公司的证据行，按行去重，保持首次出现的顺序。

        输出：信号列表，同一行只出现一次

        去重按 Signal.row_key。要去这一次重是因为同一条信号可能经由两条
        通道进到同一个簇里，那时候它是一行数据、两条通道，作为证据只算一份。
        candidates 里那两项都保留着，因为通道信息是真的。
        """
        out: list[Signal] = []
        seen: set[tuple[str, str]] = set()
        for candidate in self.candidates:
            if candidate.signal.row_key in seen:
                continue
            seen.add(candidate.signal.row_key)
            out.append(candidate.signal)
        return out

    @property
    def corroboration(self) -> int:
        """几件互相独立的事指向同一家公司。

        输出：独立事实的个数，最小是 1

        纯计数，不用 LLM，免费，而且是最强的确定性来源：两个互不相干的作者
        各提到同一家公司一次，这个确定性任何单条信号自己都给不出来。

        数的单位是「事」不是「信号」，所以按 Signal.fact_hash 去重再数。
        不去重的话，一份新闻稿被几个账号联播就把这一维刷成高分，
        而这一维就变成了「谁的新闻稿转发多谁分高」。而发布公告恰恰是最容易
        被联播、又最容易带被 @ 公司的一类内容。

        这是这个类里的第三层去重，三层各防一件事，谁也替代不了谁：
            row_key    同一行不被记成两份证据
            fact_hash  不同的行说的是同一件事（联播、转发同一个原帖）
            身份键      不同的引用指的是同一家公司（在归并那一步）
        """
        return len({signal.fact_hash for signal in self.signals})
