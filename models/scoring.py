"""打分。

刻意拆成两半：LLM 只填需要判断的维度，能算的一律代码算。
同一条原则的第三次应用（第一次是 prefilter，第二次是 corroboration）：
把能确定判的交给确定性层，别让 LLM 去赌。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from models.signal import CompanyCluster


class AssessmentProblem(StrEnum):
    """模型对某一家答得不合格，是哪一种不合格。

    四个值分两组，分组的判据是「哪个函数查得出来」，而那又取决于查它需要什么：

        只看回答本身就能判      SCORE_OUT_OF_RANGE / EMPTY_ANALYSIS
        要拿原文来比才能判      FABRICATED_QUOTE / NO_QUOTE

    前一组在 analyzer.parse_assessments 里查（那时候还没有 LlmAssessment 对象，
    分数越界的话根本拼不出来），后一组在 analyzer._content_problems 里查
    （要 cluster 的 quotable_texts）。

    做成枚举不是为了好看。这四个值要在三处对上：产生它的地方、
    prompts.PROBLEM_HEADINGS 里的抬头、以及以后落库时的计数。
    写成裸字符串的话，拼错一个字母那条问题会从重试说明里**静默消失** ——
    公司照样被重问，但模型看不到自己错在哪，而 temperature 是 0，
    那就是一次注定拿回同样坏回答的请求。跟 SignalDropReason /
    CandidateDropReason 是同一类东西：一组封闭的原因，要被计数、要进报表。

    这里装的是「我们判得出的不合格」。判不出的那种 —— 分数都在范围内、
    引用也都是原文，但把供应商判成了买方 —— 不在这个枚举里，也不该在：
    重问解决不了它，只能靠 Riley 的标注和 golden set。
    """

    # 四维里有任意一维不在 0-10。库里 CHECK 就是这个范围，放过去要到落库
    # 那一步才炸，离出错的地方太远。
    SCORE_OUT_OF_RANGE = "score_out_of_range"

    # analysis 是空的。Riley 要的是分项加理由，一条没有理由的线索她没法校准，
    # 而分数本身看不出这条缺了理由。
    EMPTY_ANALYSIS = "empty_analysis"

    # 引用不是这家公司任何一条原文的子串，见 unverified_quotes。
    FABRICATED_QUOTE = "fabricated_quote"

    # 一条有效引用都没给。空数组和空白串都算，理由见 analyzer._content_problems。
    NO_QUOTE = "no_quote"


@dataclass(frozen=True)
class LlmAssessment:
    """LLM 能填的，只有这些。每维 0-10。

    一个实际值（Chatterly，众筹刚结束、创始人自己发的帖）：

        LlmAssessment(icp_fit=10, signal_strength=8, seniority=10, moq_fit=8,
                      analysis="Chatterly just closed a Kickstarter for "
                               "offset-driver headphones and the founder "
                               "is the one posting.",
                      evidence_quotes=("We just wrapped our Kickstarter",))

    对照一个不合格的（被 @ 到的大厂，品类不对）：

        LlmAssessment(icp_fit=0, signal_strength=2, seniority=0, moq_fit=0,
                      analysis="Consumer electronics giant mentioned by a newsletter account.",
                      evidence_quotes=("new watches score your heart",))
    """

    icp_fit: int  # 品类是否落在 麦克风、耳机、带麦可穿戴、会议音响、录音笔
    signal_strength: int  # 事件硬度：众筹刚结束 > 在招 NPI > 讲愿景
    seniority: int  # 决策层级：创始人和 CTO > PM > 工程师

    # 反向维度：MOQ 500 对刚众筹完只卖 800 台的团队正好，
    # 而大厂完美命中品类词但永远聊不成，该低分。
    moq_fit: int

    analysis: str  # 为什么值得 outreach
    evidence_quotes: tuple[str, ...]  # 必须是某条 signal.text 的子串，见 unverified_quotes

    # 那几句原话证明了这家公司的什么。一两句中文，给 Riley 看的。
    #
    # 为什么要单独有这一项，而不是让她自己从原话推：引用是逐字抄的，
    # 抄得对不代表指的是这家公司。实测见过一次 —— 一篇帖子提到了甲和乙
    # 两家，模型抄了一句讲乙家产品的话，挂在甲家的卡片上。引用逐字正确、
    # 闸门也放行，因为闸门只查「这句话在不在这家公司的证据里」，
    # 查不了「这句话说的是不是这家公司」。逼模型写出「这句话证明了什么」，
    # 那种张冠李戴就会自己露出来 —— 它得写「这句话说的是乙家的产品」，
    # 而那句话一读就知道不对。
    #
    # 为什么不做成「每条引用配一句结论」：那要把 evidence_quotes 从
    # 一串字符串改成一串对象，而全仓库有七十多处在用它（闸门、重问说明、
    # 落库、一堆测试）。而两个平行的等长数组更糟 —— 长度一错位，
    # 结论就会安静地挂到另一句引用上。整体一句话拿到的收益是一样的。
    #
    # 默认空串是刻意让测试和脚本少写一个参数，生产那条路不许空：
    # parse_assessments 把空的当成「没有理由的分数」当场退回重问，
    # 跟 analysis 为空同一档。所以这个默认值不是「忘了填也没事」。
    evidence_means: str = ""

    @property
    def off_category(self) -> bool:
        """这家公司做的东西跟我们的品类完全无关。

        输入：无
        输出：True 就是品类完全不对

        一个例子：

            LlmAssessment(icp_fit=0, signal_strength=8, ...)  -> True
            LlmAssessment(icp_fit=1, signal_strength=0, ...)  -> False

        icp_fit == 0 是模型给的最重的一句判断，语义跟 1 分不一样：
        1 分是「沾一点边」，0 分是「完全不是这门生意」。所以它单独成一个
        判据，而不是跟别的分数一起参与加权。

        写成 property 而不是让三个地方各写一次 `icp_fit == 0`：
        用它的有三处（打分封顶、cooldown 判终态、报告分档），三处必须
        永远是同一个判据 —— 分家之后会出现「打分层不封顶、cooldown 判终态」
        这种自相矛盾的组合，而两边各自看都是对的。
        """
        return self.icp_fit == 0


@dataclass(frozen=True)
class DeterministicScores:
    """代码算的，LLM 碰不到。每维 0-10。

    两个实际值：

        昨天的事、有公司页、两条独立证据
            DeterministicScores(recency=10, reachability=6, corroboration=6)
        两个月前、只解析出名字、一条证据
            DeterministicScores(recency=2, reachability=2, corroboration=3)

    映射表在 config/scoring.yaml，取档的函数是 analyzer._table_lookup。
    """

    # 由 Signal.event_at 算，不是 observed_at。转发的信号两者不同，
    # 而 recency 量的是「这件事什么时候发生」，理由见那个 property。
    recency: int
    reachability: int  # 有没有 LinkedIn URL 或域名
    corroboration: int  # 由 CompanyCluster.corroboration 映射


@dataclass(frozen=True)
class Assessment:
    """一家公司的完整判断：LLM 那四维、代码那三维、加权后的总分。

    analyzer.analyze 的产物，一家一个。一个实际值：

        Assessment(
            llm=LlmAssessment(icp_fit=10, signal_strength=8, seniority=10,
                              moq_fit=8, analysis="Chatterly just closed ...",
                              evidence_quotes=("We just wrapped our Kickstarter",)),
            det=DeterministicScores(recency=10, reachability=6, corroboration=3),
            score=82)

    两半分开存不合成七个字段，是因为「这一维是判断还是事实」要能一眼看出来 ——
    判断可能错，事实不会，出问题时先怀疑哪一半取决于这个区别。
    """

    llm: LlmAssessment
    det: DeterministicScores
    score: int  # 0-100，加权后。权重放 config，不放这里。


# 排版字符到 ASCII 的对照，_fold 第一步用。每一项都是一个字符换一个字符，
# 所以位置表不受影响。只收「同一个符号的不同写法」：换掉之后那句话说的还是同一件事。
#
#     弯撇号、弯单引号、角分号           -> '    例 Spotify’s 和 Spotify's 算同一句
#     弯双引号、双角分号                 -> "
#     连字符、不断行连字符、数字线、
#     短破折号、长破折号、横线、减号     -> -    例 end–to–end 和 end-to-end 算同一句
#
# 模型抄原文的时候经常把这几种排版字符顺手写成键盘上那个 ASCII 字符。
# NFKC 管不到它们（它不认为弯撇号和直撇号是同一个字符），所以要单独折。
# 省略号不在这里：NFKC 已经把它折成三个点了。
_TYPOGRAPHIC = str.maketrans(
    {
        "\u2018": "'",  # 左弯单引号
        "\u2019": "'",  # 右弯单引号，也是最常见的弯撇号
        "\u201a": "'",  # 低位单引号
        "\u201b": "'",  # 反向单引号
        "\u2032": "'",  # 角分号，常被当撇号用
        "\u201c": '"',  # 左弯双引号
        "\u201d": '"',  # 右弯双引号
        "\u201e": '"',  # 低位双引号
        "\u201f": '"',  # 反向双引号
        "\u2033": '"',  # 双角分号
        "\u2010": "-",  # 连字符
        "\u2011": "-",  # 不断行连字符
        "\u2012": "-",  # 数字线
        "\u2013": "-",  # 短破折号
        "\u2014": "-",  # 长破折号
        "\u2015": "-",  # 横线
        "\u2212": "-",  # 减号
    }
)


def _fold(text: str) -> tuple[str, list[int]]:
    """把一段文本折成「宽松比对用」的样子，同时记下每个字符来自原文哪一位。

    输入：原文
    输出：(折过的文本, 位置表)。位置表跟折过的文本一样长，
          第 i 项是那个字符在原文里的下标

    一个例子：

        输入 "Linkwave  delivers\nODM"
        输出 ("linkwave delivers odm",
              [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, ...])
             连着两个空格折成一个，换行也折成空格，大写折成小写

    四步折叠，每一步都对应一种模型抄引用时出过的差异：

        排版字符    弯撇号、弯引号、各种破折号换成 ASCII 的 ' " -，对照表见 _TYPOGRAPHIC。
                    模型常把 Spotify’s 抄成 Spotify's
        NFKC        全角、数学粗体那类兼容字符还原成普通字母
        casefold    大小写。实测模型把 "Linkwave" 写成了 "Linkwave"
        折叠空白    连续空白（含换行）算一个空格。实测模型把换行写成了空格

    逐字符折而不是整段折，是为了位置表能一一对上：casefold 和 NFKC
    都可能把一个字符变成好几个（'ß' -> 'ss'），那时候这几个新字符共用
    同一个原文下标。整段折的话长度一变，就没法从匹配位置回推原文了，
    而回推原文正是这个函数存在的理由 —— 见 locate_quote。
    """
    # out 装折过的字符，index 装它们各自来自原文的哪一位。
    # 循环结束时两者一样长。
    out: list[str] = []
    index: list[int] = []

    # prev_space 记上一个折出来的字符是不是空格，用来把连续空白压成一个。
    prev_space = False

    for i, char in enumerate(text):
        if char.isspace():
            if prev_space:
                continue
            out.append(" ")
            index.append(i)
            prev_space = True
            continue

        prev_space = False
        # 输入   '’'（弯撇号）
        # 输出   "'"。先换排版字符再做 NFKC：NFKC 不认为这两个是同一个字符。
        #        一个原文字符可能折出好几个（'ß' -> 'ss'），那几个共用同一个下标 i
        for folded in unicodedata.normalize("NFKC", char.translate(_TYPOGRAPHIC)).casefold():
            out.append(folded)
            index.append(i)

    return "".join(out), index


# 引用两端可以被模型顺手加上或者删掉的字符。只管两端，中间一个不动 ——
# 中间的标点变了就是改了原话，那正是要拦的。
# 实测模型最爱干的就是给引用补一个句号，而原文那个位置是空格或者换行。
_QUOTE_EDGE = " \t\r\n.,;:!?\"'“”‘’（）()[]【】《》…—-"


def locate_quote(quote: str, cluster: CompanyCluster) -> str | None:
    """在这一簇的原文里找回这句引用，返回逐字原文那一段；找不到返回 None。

    输入：模型给的那句引用；这一家的簇
    输出：原文里对应的那一段（逐字，包含原本的大小写和空白），
          或者 None 表示这句在原文里根本没有

    三个例子（原文是 "Linkwave delivers end-to-end ODM solutions, ..."）：

        输入 "Linkwave delivers end-to-end ODM solutions"     逐字命中
        输出 "Linkwave delivers end-to-end ODM solutions"

        输入 "Linkwave delivers end-to-end ODM solutions."    大小写差 + 多个句号
        输出 "Linkwave delivers end-to-end ODM solutions"     还的是原文那一段

        输入 "Linkwave is raising a Series A"                 原文里没有
        输出 None

    这个函数解决的是一个实测出来的真问题：一跑 20 家里有 4 家（20%）被判成
    幻觉丢掉，而那四句跟原文的相似度都在 0.989 以上 —— 差的是一个模型顺手
    加上的句号、或者一个大小写。那不是编造，那是抄的时候顺手抹平了。
    按幻觉丢掉它们，等于为了防编造而扔掉五分之一的真线索。

    放宽的是「怎么找」，不是「找到算什么」：

        找   忽略大小写、把连续空白算一个、两端的标点不计较
        算   返回的是原文那一段，不是模型写的那一段

    所以宽松只用来认人，认出来之后存进 Assessment 的仍然是逐字原文 ——
    这比原先还严一点：原先存的是模型写的版本，它可能带着那个多出来的句号，
    而那一句以后会被抄进给客户的邮件里。

    残留的风险很小：换词、调词序、多一个词少一个词，折叠之后照样对不上。
    折叠动的只有大小写、空白多少、引号撇号破折号的写法、和两端的标点，
    这几样都不改变那句话说了什么。

    第四个例子（引的是职位串，正文里没有这句）：

        原文 正文     "Seeing this from the SingAlong team means a lot. ..."
             职位串   "Founder & CEO, North Field Technologies | Seratone is a
                       plug-and-play karaoke that turns any speaker into a studio"
        输入 "plug-and-play karaoke that turns any speaker into a studio"
        输出 "plug-and-play karaoke that turns any speaker into a studio"

    职位串也在取材范围里，因为 prompt 给模型看的那条证据里就有它
    （speaker_headline）。只认正文的话，模型抄了职位串会被判成编造 ——
    实测因此丢掉过一个创始人自己发帖的真买方，见 Signal.quotable_headlines。

    sources 是这一簇给模型看过的全部原文（每条信号的正文，
    转发的两段都算，再加每个说话人的职位串）；真正去找的是 find_quote。
    """
    # 取材范围 = 给模型看过的全部原文：每条信号的正文（转发的两段都算），
    # 加上每个说话人的职位串。顺序是正文优先 —— 同一句话同时出现在两处时
    # （职位串里那句口号也被写进了正文），还回来的是正文那一段。
    #
    # 职位串必须在这个范围里，理由见 Signal.quotable_headlines：prompt 里
    # 那条证据同时有正文和职位串，闸门只认正文的话，模型抄了职位串就被
    # 判成编造 —— 实测这样丢掉过一个创始人自己发帖的真买方。
    sources = [t for s in cluster.signals for t in s.quotable_texts]
    sources += [h for s in cluster.signals for h in s.quotable_headlines]
    found = find_quote(quote, sources)
    return found[1] if found else None


def find_quote(quote: str, texts: list[str]) -> tuple[int, str] | None:
    """在几段原文里找这句引用，返回第一个找到的那一段是第几段、原文里对应的是哪几个字。

    输入：引用，如 "Linkwave delivers end-to-end ODM solutions."；
          几段原文，如 ["hello", "Linkwave delivers end-to-end ODM solutions, for ..."]
    输出：(1, "Linkwave delivers end-to-end ODM solutions")；哪一段都没有是 None

    比法跟 locate_quote 一样宽松（忽略大小写、连续空白算一个、两端标点不计较、弯撇号当直撇号），
    因为就是它的核心：locate_quote 拿一簇帖子的全部原文调它，拼卡片时拿每条证据的原文调它、
    认出这句引用出自哪一条帖子。两边用同一个函数，才不会出现「打分时认得、卡片上认不出」
    """
    # trimmed 去掉两端可能被模型改动的字符，例如
    #     "Groove Labs is ... catalyst program." -> "Groove Labs is ... catalyst program"
    # 去完是空的说明这句里除了标点空白什么都没有，那不是证据。
    trimmed = quote.strip(_QUOTE_EDGE)
    if not trimmed:
        return None

    # needle 是折过的引用，例如 "linkwave delivers end-to-end odm solutions"。
    needle, _ = _fold(trimmed)
    if not needle:
        return None

    for i, text in enumerate(texts):
        # 先试逐字。命中就直接还原文那一段，一次折叠都不用做。
        if trimmed in text:
            return i, trimmed

        # haystack 是折过的原文，index 是它每个字符来自原文哪一位。
        haystack, index = _fold(text)
        at = haystack.find(needle)
        if at == -1:
            continue

        # 把折过的位置换回原文位置。末尾用 index[at + len - 1] + 1，
        # 因为最后那个折出来的字符可能是某个原文字符展开的其中一个。
        start = index[at]
        end = index[at + len(needle) - 1] + 1
        return i, text[start:end]

    return None


def unverified_quotes(assessment: LlmAssessment, cluster: CompanyCluster) -> list[str]:
    """返回在原文里找不回来的引用。非空就是幻觉。

    输入：模型对某一家的判断；这一家的簇（拿它的原文来比）
    输出：那几句找不回来的引用，按 evidence_quotes 里的原顺序；
          空列表表示每一句都能在原文里找到

    两个例子（这家公司的原文是 "We just wrapped our Kickstarter"）：

        输入 evidence_quotes=("We just wrapped our Kickstarter",)
        输出 []

        输入 evidence_quotes=("We just wrapped our Kickstarter",
                             "they are raising a Series A")
        输出 ["they are raising a Series A"]

    这里只回答事实：哪几句在原文里没有。**接不接受这份判断是政策**，
    在调用方 —— analyzer._check_content 还要求「至少有一条」，
    draft 那一期的政策会是「两次不过直接丢弃」。事实放在这里，是因为
    它就是 LlmAssessment.evidence_quotes 这个字段「合法」的定义本身，
    定义在别的层的话，字段和它的规则会各自漂。

    怎么算「找得到」见 locate_quote：忽略大小写、空白多少和两端标点，
    但换词、调词序、多一个词少一个词照样算编造。
    空串是这条规则的一个洞：`"" in text` 恒为真，所以空串不会出现在
    返回值里。堵它的是调用方（那属于政策：空串不是证据）。

    要拿回「原文到底是哪一段」用 locate_quote，别用这个函数的反面 ——
    这个只回答有没有，不回答是哪一段。
    """
    return [q for q in assessment.evidence_quotes if locate_quote(q, cluster) is None]
