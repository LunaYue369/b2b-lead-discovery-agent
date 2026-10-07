"""lead 的状态，以及 Riley 对 lead 的态度。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from models.contact import CardContacts
from models.signal import LeadType


class LeadStatus(StrEnum):
    """公司在 leads 表里的状态。

    leads 表装的是所有分析过的公司，不是只有合格的。不合格的公司也必须
    留下记录，否则明天同一条 query 又搜到它、又送一次 LLM ——
    而按实测分布，80% 到 90% 属于这一类。
    """

    NEW = "new"  # 分析过了，还没推给 Riley（分数没到线）
    PUSHED = "pushed"  # 已经以 info card 形式推给 Riley 了
    IN_DRAFT = "in_draft"  # Riley 说要写稿，交给 draft agent，discovery 不再捞它
    EXCLUDED = "excluded"  # 终态：确认是供应商、同行、词义撞车，永不复查


class FeedbackType(StrEnum):
    """Riley 对一条 lead 的态度。

    IGNORED 是必须有的：Riley 大部分卡片是不回应，沉默是弱负信号。
    只记 approve 和 reject 会让 human approval yield 的分母是错的，
    而那个指标决定每天 60% 的预算怎么分。
    由一个定时任务把超过 N 天没动作的卡片扫成 IGNORED。
    """

    APPROVE = "approve"  # 要写稿，同时把 lead 推进 IN_DRAFT
    SKIP = "skip"  # 这次先不联系，但不否定
    REJECT = "reject"  # 不该联系
    IGNORED = "ignored"  # 超时未处理，系统写的，不是 Riley 写的


class TriageVerdict(StrEnum):
    """这一跑要不要花钱分析这家公司。

    三个取值，逐家给。不做成三个合计数，是因为按 query 归因需要知道
    是哪几家：一家公司可能被今天两条 query 同时命中，那两条 query 的账
    里都要记上它这一档，而合计数分不回去。三个数随时可以数出来。

    EXCLUDED 单独一档不跟 COOLING 合并：那一档连第一次 LLM 都省，
    而 COOLING 只是这次先不看。混成一个数，「已知垃圾占多大比例」
    就没法回答，而那正是判断 query 该不该降级的依据之一。
    """

    RESEARCHABLE = "researchable"  # 这次要送 LLM
    COOLING = "cooling"  # 分析过了，冷却期还没到
    EXCLUDED = "excluded"  # 终态，永不复查


@dataclass(frozen=True)
class SavedLead:
    """一个簇落库之后拿回来的东西。save_clusters 按传进去的簇的顺序返回。

    novel 的含义是「这一行是这次头一回建的」，它就是 novel_lead_count 的
    来源 —— 那一次查库在 upsert 里已经做过了，不该再查第二遍。
    """

    lead_id: int
    novel: bool
    evidence_rows: int  # 这个簇实际写进去几行证据，跟候选数不等（同正文的会被合并）


@dataclass(frozen=True)
class LeadState:
    """一家公司在库里的现状：这次要不要花钱分析，以及上一次判成什么样。

    三个字段来自同一行 leads，所以一次查库一起拿回来。拆成「先问要不要
    分析、再问上次多少分」两次查询读的是同一行，还多带一条时序约束
    （上次的分必须在写新分之前问到），而那条约束靠注释守不如靠结构守。

    verdict 是库判的不是我们判的：「还在冷却期吗」要拿库里的 now() 去比。
    客户端自己凑一个「现在」的话，两台机器的时钟和时区不保证一致，
    同一批数据会在两台机器上分流出不同结果，而这种错查起来极痛苦。

    previous_score 允许 None，而它跟 0 分含义完全不同 —— 0 分是判过的
    结论，None 是还没判过（一家公司可以先被发现、落库，还没轮到分析）。
    退避那一步对两者的处理相反。
    """

    verdict: TriageVerdict
    previous_score: int | None
    cooldown_strikes: int


@dataclass(frozen=True)
class CardQuote:
    """一句原文引用，和模型对它的那一句解读。

    一个实际值：

        CardQuote(text="In the Vault is on it's way to becoming one of music's household series",
                  meaning="发言者提到Hushly正在制作音频节目，显示出他们在音频领域的活跃。")

    meaning 是 None：模型给的解读句数跟引用句数对不上，没法一句配一句（见 LeadCard.unpaired_means）
    """

    text: str
    meaning: str | None


@dataclass(frozen=True)
class CardEvidence:
    """卡片上「帖子证据」那一区里的一组：一条帖子，和出自这条帖子的原文引用。

    一个实际值：

        CardEvidence(author="sam hadid",
                     role="ceo @ Hushly - a sanctuary for human sound",
                     when="2026-09-21",
                     url="https://www.linkedin.com/posts/sam-hadid-a1b2c3d4_in-the-vault-is-on-its-way-"
                         "to-becoming-one-activity-7507890528395587584-pmIJ",
                     quotes=(CardQuote(text="In the Vault is on it's way to becoming one of music's ...",
                                       meaning="发言者提到Hushly正在制作音频节目……"),))

    quotes 是 build_cards 按「这句引用出自哪条帖子的原文」分进来的；lead_db.card_sources
    查出来时是 ()。这条帖子没有被引用的话一直是 ()，照样出一组（它是这家的证据之一）。

    没有「帖子标题」这一项，因为 LinkedIn 的帖子没有标题 —— 它只有正文。
    所以这一区显示的是「谁、什么身份、什么时候说的」，那三样才是 Riley
    点开链接之前需要判断的东西。硬造一个标题（比如截正文前十个字）会
    看起来像标题而实际是正文碎片，比不给更坏。
    """

    author: str
    role: str
    when: str  # 已经格式化好的日期串，卡片这一层不做时间计算
    url: str
    quotes: tuple[CardQuote, ...]


@dataclass(frozen=True)
class CardSource:
    """一条线索拼卡片要的全部素材，全部从库里查出来。lead_db.card_sources 返回它。

    一个实际值：

        CardSource(display_name="Chatterly Inc.", status="new", score=67,
                   icp_fit=8, signal_strength=8, seniority=8, moq_fit=8,
                   recency=6, reachability=2, corroboration=3,
                   analysis="Chatterly just closed a Kickstarter ...",
                   quotes_mean="创始人本人说众筹刚结束，正在找量产",
                   quotes=("We just wrapped our Kickstarter",),
                   evidence=(CardEvidence(author="Shah", role="Founder",
                                          when="2026-08-19",
                                          url="https://www.linkedin.com/posts/..."),))

    每个字段在库里的来处：

        display_name / status        leads.display_name / leads.status
        score 和七维 / analysis      leads.score、llm_ 四维、det_ 三维、llm_analysis
        quotes_mean / quotes         leads.llm_evidence_means / leads.llm_evidence_quotes
        evidence                     lead_evidence 串到 signals，取作者、身份、时间、链接
        evidence_texts               同一条 signals 的正文、原帖正文、两个职位串，拿来认引用出自哪一条

    中间那两行都是 save_assessment 在同一条 UPDATE 里写的，所以只要这家公司
    被某一跑打过分，它们就是齐的。

    跟 LeadCard 的差别：多一个 status（build_cards 拿它挡 in_draft），没有 lead_id
    （它是 card_sources 返回的那个字典的键），quotes 没截断（卡片放几条是 build_cards
    的事，不是库的事）。
    """

    display_name: str

    # 这一行现在的状态。带出来是为了让调用方能挡掉 in_draft 那一档。
    #
    # 为什么必须挡：一条线索被 Riley 点了「要联系」之后进 in_draft，
    # 但那个状态挡不住「重新分析」—— lead_states 的分流只看是不是
    # excluded、冷却期过没过，in_draft 不在里面。所以冷却期一过它会被
    # 再分析一次，分数还在线上的话就又被拼成一张卡推出去，
    # 而她正在给那家写稿。这是重复推送，不是新线索。
    #
    # 挡的位置在拼卡片那一步，不在这里：这个类型只装库里有什么，推哪几张
    # 由 build_cards 决定。scheduled 和 interactive 两条路都经过那一步，
    # 都不推 in_draft 的。
    status: str

    score: int

    # LLM 判的四维，0-10
    icp_fit: int
    signal_strength: int
    seniority: int
    moq_fit: int

    # 代码算的三维，0-10
    recency: int
    reachability: int
    corroboration: int

    analysis: str

    # 例 '创始人本人说众筹刚结束，正在找量产'。含义见 LeadCard 上同名那一格
    quotes_mean: str

    # 例 ("We just wrapped our Kickstarter", "we need a factory for 500 units")。
    # 库里是 text[]，查出来是 list，card_sources 转成 tuple 放进来（这个类是 frozen 的，
    # 装 list 的话外面还能往里 append）
    quotes: tuple[str, ...]

    evidence: tuple[CardEvidence, ...]

    # 跟 evidence 一一对应：第 i 条证据的原文，几段。build_cards 拿它认每句引用出自哪一条帖子。
    # 例 (("In the Vault is on it's way to becoming ...", "ceo @ Hushly - a sanctuary for human sound"),)
    # 段的顺序跟打分时核对引用的一样：正文、原帖正文（转发的才有）、职位串、原帖作者的职位串
    evidence_texts: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class DraftEvidence:
    """写稿时要看的一条证据：一条帖子的全文，加上「说这话的人是不是这家公司的人」。

    一个例子（编的）：Acme Audio 的创始人自己发帖说众筹刚结束

        DraftEvidence(signal_id=2720, lead_type=LeadType.AUTHOR,
                      author="Dana Lee", role="Founder @ Acme Audio", when="2026-09-20",
                      url="https://www.linkedin.com/posts/danalee_...",
                      text="We just wrapped our Kickstarter and are looking for a factory for 800 units.",
                      reposted_author=None, reposted_text=None)

    对照一条从第三方口中得知的（编的）：一家媒体发帖报道了 Acme Audio

        DraftEvidence(signal_id=2733, lead_type=LeadType.MENTIONED,
                      author="Audio Weekly", role="", when="2026-09-18", url="https://...",
                      text="Congrats to @Acme Audio on a successful campaign!",
                      reposted_author=None, reposted_text=None)

    跟 CardEvidence 的区别：卡片只要谁、什么时候、链接，给人点开看；写稿要正文本身（开场白要引它），
    还要 lead_type（从第三方口中知道的，开场白不能装作偶然看到本人的帖子，见 LeadType 的说明）。
    signal_id 留着，是给以后 plan_drafts 挑「开场引哪一条」用的：挑编号，代码认得出挑没挑错
    """

    signal_id: int
    # 这条证据是从哪个通道挂到这家公司上的。lead_type.is_first_person 就是「是不是本人说的」
    lead_type: LeadType
    author: str
    # 发帖人名字下面那一行（本人手写的职位串），没有是 ""
    role: str
    # 日期串，例 "2026-09-20"；库里没有时间是 ""
    when: str
    url: str
    text: str
    # 转发的帖子才有这两格：原帖作者和原帖正文。不是转发是 None
    reposted_author: str | None
    reposted_text: str | None


@dataclass(frozen=True)
class DraftSource:
    """写稿要的这家公司的素材：最近一次打分的结论，加几条最新的证据。lead_db.draft_source 返回它。

    一个例子（编的）：

        DraftSource(score=72,
                    analysis="Acme Audio just finished a Kickstarter for a clip-on mic and needs 800 units.",
                    quotes_mean="创始人本人说众筹刚结束，在找工厂做 800 台",
                    quotes=("We just wrapped our Kickstarter",),
                    evidence=(DraftEvidence(signal_id=2720, ...), DraftEvidence(signal_id=2733, ...)))

    每个字段在库里的来处（跟 CardSource 同一批列）：

        score / analysis             leads.score / leads.llm_analysis
        quotes_mean / quotes         leads.llm_evidence_means / leads.llm_evidence_quotes
        evidence                     lead_evidence 串到 signals

    分数和分析都是「最近一次打分」的：leads 那几列每打一次分就整体改写一次，所以按 lead_id 查
    拿到的就是最新那次，不用知道是哪一跑。没被打过分的公司（不该出现在卡片上）这几格是 None / 空
    """

    score: int | None
    analysis: str | None
    quotes_mean: str | None
    quotes: tuple[str, ...]
    # 最新的在前
    evidence: tuple[DraftEvidence, ...]


@dataclass(frozen=True)
class LeadCard:
    """一条线索在卡片上要显示的全部东西。纯数据，不认识 Slack。

    delivery 那一层只收这个，不收 DiscoveryResult、也不查库。这样
    「卡片长什么样」可以脱离一整跑单独测，而且换掉 Slack 的时候
    这个类一个字不用改。

    一个实际值：

        LeadCard(lead_id=1281,
                 display_name="Chatterly Inc.", score=67,
                 icp_fit=8, signal_strength=8, seniority=8, moq_fit=8,
                 recency=6, reachability=2, corroboration=3,
                 analysis="Chatterly just closed a Kickstarter ...",
                 evidence=(CardEvidence(author="Shah", role="Founder", when="2026-08-19",
                                        url="https://www.linkedin.com/posts/...",
                                        quotes=(CardQuote("We just wrapped our Kickstarter",
                                                          "众筹刚结束"),)),),
                 unplaced_quotes=(), unpaired_means="", contacts=NO_CONTACTS)

    为什么七维都摊在这里、不只给总分：Riley 要能校准。只看到 67 分
    她没法回答「这条哪里不对」，而她的反馈是唯一能反推阈值和权重的东西。

    引用是信号原文的逐字片段，已经过防幻觉闸门（不是原文子串的整家
    都被丢弃了，不会走到这里）。它是卡片上最重要的一块 —— 分数是判断，
    原文是事实，Riley 真正在读的是后者。

    每句引用放进它出自的那条帖子那一组（evidence[i].quotes）。认法不是猜：拿打分时核对引用的
    同一个函数（models.scoring.find_quote），在每条帖子的原文里逐字找，找到哪条就归哪条。
    哪条都找不到的（出自没显示出来的那几条证据）放进 unplaced_quotes，不硬塞给某一条。
    """

    lead_id: int
    display_name: str
    score: int

    # LLM 判的四维，0-10
    icp_fit: int
    signal_strength: int
    seniority: int
    moq_fit: int

    # 代码算的三维，0-10
    recency: int
    reachability: int
    corroboration: int

    analysis: str

    evidence: tuple[CardEvidence, ...]

    # 找不到出自哪条帖子的引用，例 (CardQuote("...", None),)；一般是 ()
    unplaced_quotes: tuple[CardQuote, ...]

    # 模型给的解读句数跟引用句数对不上时，那一整段解读原样放这里，卡片上在这一区开头印一次；
    # 对得上时每句解读已经配进各自的引用（CardQuote.meaning），这里是 ""
    unpaired_means: str

    # 「怎么联系」和「公司概况」两区要的东西。还没找过联系方式的那家是 NO_CONTACTS，
    # 例子见 CardContacts
    contacts: CardContacts


@dataclass(frozen=True)
class LeadHit:
    """lead_discovery_hits 里的一次命中，连带那家公司现在什么样。

    一行的意思是「这条 query 在某一跑里命中过这家公司」。同一条 query 对同一家公司
    每跑一次就有一行（那张表是 append-only 的原始账本），所以拿它数公司之前要先按
    lead_id 去重。

    一个实际值：

        LeadHit(query_id=30, lead_id=1301, display_name='Bonewave',
                status=LeadStatus.EXCLUDED, score=34, in_cooldown=False)

    score 允许为 None：那是「发现过、落过库，但还没轮到分析」，跟「判过，0 分」
    是两件事，退避和举例两处对它们的处理都不一样。

    in_cooldown 是库里当场算出来的布尔值，不是取回 cooldown_until 再自己比 ——
    自己比就要在客户端凑一个「现在」，而那台机器的时钟不保证跟库一致。
    跟 LeadState 那边「还在冷却期吗交给库去判」是同一条。
    """

    query_id: int
    lead_id: int
    display_name: str
    status: LeadStatus
    score: int | None
    in_cooldown: bool
