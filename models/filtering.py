"""prefilter 的丢弃理由和丢弃记录。

刻意分成两个枚举，不是一个。

血的教训：「公司主页帖」这条规则如果写成整条 raw item 丢弃，会把
「加速器从公司主页发帖恭喜 North Field Technologies 众筹成功」这种真线索
连帖子一起杀掉 —— 发帖人不是买方，但被提到的公司是。

分成两个枚举之后，drop_signal() 只收 SignalDropReason、
drop_candidate() 只收 CandidateDropReason，那个 bug 类型系统直接不让写。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from models.signal import LeadType


class SignalDropReason(StrEnum):
    """整条 raw item 丢弃。只有跟发帖人身份无关的理由才能放这里。"""

    # 一个相关性来源都没有：正文没有命中词，而且也没有别的通道能证明
    # 这条信号跟我们有关。名字不叫 no_term_in_content 是因为判据不止
    # 「有没有命中词」一条，叫那个名字会让报表看着像只查了正文。
    NO_RELEVANCE_SIGNAL = "no_relevance_signal"
    # 「见过了」刻意分成两条，不是一条带 detail 区分。
    # 漏斗报表是按 reason 聚合的：合成一条的话，要知道跨天砍了几条就得去
    # parse detail 字符串。而且两者的运营含义不同，趋势本来就该分开看 ——
    # 跨天是正常损耗（昨天已经处理过），跑内是联播、或者同一帖被多条 query
    # 搜到，后者是 query 有重叠的信号，归 query_intelligence 管。
    SEEN_HASH_IN_RUN = "seen_hash_in_run"  # 这一跑里已经出现过同一段正文
    SEEN_HASH_PRIOR_RUN = "seen_hash_prior_run"  # 更早的某一跑已经见过
    HARDWARE_STORE = "hardware_store"  # "五金" 词义撞车：防火门、家具五金、门窗
    SUPPLIER_CONTENT = "supplier_content"  # 制造语境 SEO 文
    OFF_TOPIC = "off_topic"  # 招聘 spam、配音、有声书之类无关话题
    # 上面三张词表同时命中了不止一张。单独立一条而不是归给排在前面那张，
    # 是为了让前三条的砍量各自只表示「只有这一类命中」——否则排序靠前的
    # 规则会吃掉后面规则的功劳，报表上看不出后面那些其实也拦得住。
    #
    # 值里带 signal 是必须的：candidate 级有一条同名的，而两级的砍量装在
    # RunStats.drop_reasons 同一个字典里、按字符串索引。值撞了的话，
    # 两级的数会悄悄加在一起，而那张表正是用来分辨「砍在花钱之前还是之后」的。
    MULTIPLE_RULE_HITS = "multiple_signal_rule_hits"


class CandidateDropReason(StrEnum):
    """只丢这一个候选，同一条 raw item 的其他候选照留。"""

    # 公司主页帖的 author 候选（这条信号是转发时，指原帖作者那一侧）。
    #
    # 砍的不是「这家公司」，是「这一次这条通道」。公司主页发的帖，作者就是
    # 品牌自己在广播：新品发布、招聘、加速器恭喜被投公司、供应商晒产线。
    # 这类帖子里没有一个具体的人在说「我们现在需要什么」——它讲的是这家
    # 公司本身，而我们要找的是正在发生的一件事。所以它不是「一家公司」
    # 这个好消息，是「一条没有事件、也没有说话人的线索」。
    #
    # 砍掉之后这家公司并不会永久出局：leads 表按公司归并，它从别的通道
    # 再出现时照样进得来（有人以个人身份发帖、或者被第三方提到），
    # 而那时候是有人在说话的，seniority 和 signal_strength 才有东西可判。
    #
    # 只砍 author 一侧，同一条信号里被 @ 的公司照留。加速器从公司主页发帖
    # 恭喜被投公司众筹成功时，发帖人不是买方，被提到的那家才是 —— 这就是
    # 这条规则必须待在候选级、不能写成整条丢弃的全部理由。
    #
    # 已知会误杀的一类：真买方从自己的公司主页发帖说在找代工厂。它跟品牌
    # 广播在数据上长得一模一样（都是公司主页作者、都带命中词），现有字段
    # 区分不了。样本太少，还不够支撑一条更细的规则，先记在账上。
    # 规则本身是 filters.yaml 里的一个开关，要量它的代价就关掉重跑一遍。
    COMPANY_PAGE_AUTHOR = "company_page_author"
    SUPPLIER_HEADLINE = "supplier_headline"  # 作者 headline 是供应商侧
    # 候选级的词表命中了不止一张。跟 SignalDropReason 那条同理：归给排在
    # 前面那张表的话，各表的砍量就变成「谁排前面谁大」，看不出后面那些
    # 其实也拦得住。现在候选级只有一张词表，所以这条走不到，它是为第二张
    # 表准备的 —— 加表的时候不必再回来改归因逻辑。
    # 值里带 candidate 的理由见 SignalDropReason 那条同名项。
    MULTIPLE_RULE_HITS = "multiple_candidate_rule_hits"
    NO_IDENTITY = "no_identity"  # 连公司名都解析不出来，无法建身份键

    # 这个实体不是「做产品的公司」：众筹平台、媒体、分析机构、投资方、展会。
    # 判据是「它做不做硬件产品」，不是「它多大」。Samsung / Garmin /
    # Google 真做硬件、只是太大，那是打分层 moq_fit 的判断，不在这里砍 ——
    # 把它们混进来会让「砍大厂的规则」在指标上显得完全正确，
    # 诱导把打分层的职责一路塞进规则层（labels 里 big_brand 单列同一理由）。
    # 也不碰语音 AI 软件公司：它们恰恰可能是买方（想做个带麦的硬件），
    # 那是 icp_fit 该判的。
    NOT_A_BUYER = "not_a_buyer"

    # 这一段正文 @ 了太多公司。@ 了一堆公司的帖子是清单、工具盘点、
    # 会议回顾 —— 它在谈一个集合，里面没有哪一家是「现在有需求的主角」。
    # 只砍被 @ 的那一侧候选。发帖人自己那一侧照留：写盘点文的人
    # 有没有价值是另一回事，跟他 @ 了几家没关系。
    MENTION_FANOUT = "mention_fanout"


@dataclass(frozen=True)
class SignalDrop:
    external_id: str
    reason: SignalDropReason
    # 命中了哪个词、撞了哪条 hash。出回归报告和查「误砍了真买方」
    # 全靠这个字段，别省。
    # 归因到 MULTIPLE_RULE_HITS 时，这里写各类命中了几个、分别是哪些词。
    detail: str


@dataclass(frozen=True)
class CandidateDrop:
    # 这个候选是从哪条帖子展开出来的。没有它就回不到原文：同一家公司
    # 可能在好几条帖子里各被砍一次，只靠公司名加通道分不开是哪一条，
    # 而查「这条为什么没了」总要落到具体那一篇。
    external_id: str
    company_name: str
    lead_type: LeadType
    reason: CandidateDropReason
    detail: str
