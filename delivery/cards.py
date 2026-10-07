"""把一跑的结果做成 Slack 早报。这一层只拼数据结构，不发送。

这个文件里没有一行 Slack SDK 的调用，也不 import slack —— 它产出的是
chat.postMessage 的参数字典，发送在别处。分开的理由是「早报长什么样」
要能脱离 token 和网络单独测：样子会反复改（Riley 说少了什么就加），
而每改一次都要真发一条到频道里才能看，是不可接受的返工速度。

同理这里不查库、不收 DiscoveryResult，只收 LeadCard 列表。谁去把
「打分结果 + 库里的公司名 + 证据来源」凑成 LeadCard 是调用方的事。

一跑发多条消息，不是一条：

    第一条    汇总（灰蓝色条，没有按钮，账目两列）
    之后每条  一个线索（A 档绿色条、B 档橙色条，各带三个按钮）

为什么不塞进一条消息：Slack 消息之间本来就有间距和视觉分隔，而同一条
消息内部只能用 divider 画一根细线。要「一张卡是一张卡」的感觉，只能
靠消息本身。附带两个好处：一条发失败不影响别的（塞成一条是全有全无），
以及点完按钮可以只更新那一张卡。

C 档（没过线的）默认不进卡片，只落库。这不是省事，是「不合格的绝对不放上来」那条规矩：
每天倒一堆勉强的线索进频道，Riley 会停止读它，而那时候整个系统就白做了。
实验期例外：配置里 delivery.push_below_line 打开时 C 档也推，灰色条、卡片上写明没过线，
不截配额。理由见 discovery.yaml 那一项。

Slack 的四条硬限制，设计的时候绕不开（不是选择，是没得选）：

    没有背景色      也没有边框。唯一的上色办法是 attachment 的 color，
                    它画在左侧一条竖条上。用 attachment 的代价是 Slack
                    会加一行「Added by <应用名>」，那行删不掉。
                    （能删掉的是另一件事：降级文本在客户端里重复显示
                    一遍 —— 把 fallback 写进 attachment 内部、顶层不给
                    text 就没了。）
    分隔只有横线    卡片四周画不了框，也只有一种 divider、粗细颜色都不能
                    调。所以外框用重线字符自己画（深、粗），内部分区用
                    原生 divider（浅、细），靠两种线分出层级
    没有字号字色    只有加粗、header 块（最大字号）、和 context 块
                    （小号灰字）。所以分数一律不放 context ——
                    那是它以前显得又小又灰的原因
    fields 上限 10  两列布局靠 section 的 fields，最多 10 项即 5 行。
                    七个维度要 14 项，所以分数明细显示上是一个区、
                    实现上是两段 fields（小标题只挂第一段）。证据那边
                    一条四行正好八项，再加一项就要换排法。
                    塞第 11 项 Slack 直接拒收整条消息
"""

from __future__ import annotations

import json
import pathlib
import random
import re
from dataclasses import dataclass

import yaml

from models import (
    CardContacts,
    CardEmail,
    CardPerson,
    CardQuote,
    CardRelation,
    CompanyProfile,
    ContactSource,
    LeadCard,
)

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "discovery.yaml"

# 左侧竖色条的颜色。这是 Slack 唯一能给一条消息上色的机制
# （attachment 的 color），不是背景色 —— 背景色和边框都不给控制。
#
# 用 attachment 就必须接受它带来的东西：Slack 会在下面加一行
# 「Added by <应用名>」。之前为了去掉那行改成过顶层 blocks，代价是
# 没有色条。这一版把色条要回来，同时把「降级文本在客户端里重复显示
# 一遍」那个问题单独解决掉 —— 办法是 fallback 写在 attachment 内部、
# 顶层不给 text（见 lead_message 结尾）。
#
# C 档是灰色：只有 push_below_line 打开时才推，颜色要一眼看出「这张没过线」。
SUMMARY_COLOR = "#4a6785"
BAND_COLORS = {"A": "#2eb886", "B": "#e8912d", "C": "#9e9e9e"}

# 卡片上档位那一行的写法。C 档要把「没过线」写出来：只写一个「C 档」的话，
# 第一次看到的人不知道 C 是好是坏
BAND_LABELS = {"A": "A 档", "B": "B 档", "C": "C 档 · 没过线"}

# 公司名前面那个记号。以前这里放的是按档位分色的圆点，色条回来之后
# 颜色由色条负责，这里换成一个「这是一家公司」的记号。
# 没有按公司给不同 emoji 的办法 —— 我们手上没有对方的图标。
COMPANY_MARK = ":office:"
SUMMARY_MARK = ":clipboard:"

# 卡片最外面那两条线。用的是重线字符，不是 divider 块。
#
# 为什么不用 divider：Slack 只有一种 divider，粗细颜色都不能调。而卡片
# 内部的分区也要线，两处用同一种线就分不出「这是卡片的边界」和
# 「这是卡片里的一个分区」。所以外框用重线字符画（深、粗），
# 内部分区用原生 divider（浅、细），一眼能分出层级。
OUTER_RULE = "━" * 34

# 四个 LLM 维度在卡片上的显示名。
#
# 显示名刻意写成一句能读懂的话，不是字段名的直译：「品类」「事件」这种
# 两字词自己看不出在问什么，而这几个名字是 Riley 判断「该跟哪一维吵架」
# 的唯一入口。后面括号里的数字是权重，也就是这一维最多能贡献多少总分 ——
# 不写出来的话「品类 8 分」和「时效 8 分」看起来一样重，而实际差两倍半。
_LLM_LABELS = (
    ("icp_fit", "做的是不是音频设备"),
    ("signal_strength", "最近发生了什么事"),
    ("seniority", "说话的人能不能拍板"),
    ("moq_fit", "量够不够 500 起订"),
)

# 代码算的三维。卡片上跟上面那四维显示成同一个区（都在「分数明细」
# 底下），分成两段纯粹是 fields 上限 10 项的技术限制。
#
# 代码里仍然分两个常量，因为「这一维是判断还是事实」在排查时是有用的
# 区别：判断可能错，事实不会，出问题先怀疑哪一半取决于它。
#
# corroboration 显示成「几件事互相印证」不是「互证」：它数的是几件互相
# 独立的事指向同一家公司，而「互证」这个词不解释就看不懂。
_DET_LABELS = (
    ("recency", "这件事有多新"),
    ("reachability", "找不找得到人"),
    ("corroboration", "几件事互相印证"),
)

# 按钮的 action_id。buttons.py 那边按这几个字符串分发，所以它们是接口
# 不是文案 —— 改了要两处一起改。取值跟 models.FeedbackType 一一对应。
ACTION_APPROVE = "lead_approve"
ACTION_SKIP = "lead_skip"
ACTION_REJECT = "lead_reject"

# 线索卡上按钮那一块的 block_id 写成 "lead:1281:run:27"，这是它的开头。
# 它有两个用处：按钮回调靠它认出点的是哪条线索；对话那条路靠它认出一个
# thread 的开头是不是线索卡（delivery/conversation.lead_card_id）。所以点完
# 按钮、按钮那一块被换成回执的时候，block_id 要跟着留下（buttons.acknowledged_blocks）。
LEAD_BLOCK_PREFIX = "lead:"


@dataclass(frozen=True)
class CardConfig:
    """早报的三个分界、三个配额，和没过线的推不推。

    一个实际值（仓库里那份配置）：

        CardConfig(a_score=75, qualified_score=60, review_score=40,
                   a_quota=5, b_quota=10, review_sample_size=5, push_below_line=True)

    qualified_score 和 review_score 不在这一层自己定义，是从跟 cooldown
    共用的那份配置里读的：B 档的下界必须就是「过线」那条线本身。分开定义
    的话，改了过线分而没改这里，会出现「过线了但不推」或者「没过线却推了」，
    而两处各自看都是对的。
    """

    a_score: int
    qualified_score: int
    review_score: int
    a_quota: int
    b_quota: int
    review_sample_size: int

    # True：没过线的也推成 C 档卡片，不截配额，也不再抽基线样本（它们已经全在频道里了）
    push_below_line: bool

    # 七维各自的权重，读的是 scoring.yaml 里那一份，不是这一层自己定的。
    # 卡片上要把权重印在项目名旁边（不印的话「品类 8 分」和「时效 8 分」
    # 看起来一样重，而实际差两倍半），而权重的定义只有一处 ——
    # 抄一份到 delivery 这边，改了 scoring.yaml 卡片上就会印着旧数字，
    # 而那种错不报错。
    weights: dict[str, int]


def load_card_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> CardConfig:
    """读 discovery.yaml 里跟早报有关的那几个数。

    输入：配置文件路径，默认是仓库里那份
    输出：一个 CardConfig

    一个例子：

        文件   qualified_score: 60
               review_score: 40
               delivery: {a_score: 75, a_quota: 5, b_quota: 10,
                          review_sample_size: 5}
        输出   CardConfig(a_score=75, qualified_score=60, review_score=40,
                          a_quota=5, b_quota=10, review_sample_size=5)

    用下标不用 get，跟这个仓库里别的 load_* 同一条口径：键缺了当场
    KeyError。配额退化成默认值不报错，而症状是「今天推的条数跟配置说的
    不一样」，查的时候会先怀疑打分。

    三条大小关系当场校验。它们不是风格问题，每一条错了都会让早报安静地
    做错事，理由写在下面那几句 raise 里。
    """
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    block = raw["delivery"]

    # 权重在另一份配置里（scoring.yaml），因为它属于打分不属于早报。
    # 这一层只读不改，读它是为了把权重印在卡片上。
    scoring_path = pathlib.Path(path).parent / "scoring.yaml"
    scoring = yaml.safe_load(scoring_path.read_text(encoding="utf-8"))
    weights = {**scoring["weights"]["llm"], **scoring["weights"]["det"]}

    config = CardConfig(
        a_score=int(block["a_score"]),
        qualified_score=int(raw["qualified_score"]),
        review_score=int(raw["review_score"]),
        a_quota=int(block["a_quota"]),
        b_quota=int(block["b_quota"]),
        review_sample_size=int(block["review_sample_size"]),
        push_below_line=bool(block["push_below_line"]),
        weights=weights,
    )

    if config.a_score <= config.qualified_score:
        # A 档下界不高于过线分的话，B 档就是空的 —— 所有过线的都成了 A，
        # 而 A 的配额只有 B 的一半，等于悄悄砍掉一半推送量。
        raise ValueError(
            f"a_score={config.a_score} 不高于 qualified_score={config.qualified_score}，"
            f"那 B 档永远是空的"
        )
    if config.review_score >= config.qualified_score:
        # 边界档的上界就是过线分。反过来的话，过线的线索会同时出现在
        # 卡片区和「等 Riley 标注」的折叠区里，同一条线索两个身份。
        raise ValueError(
            f"review_score={config.review_score} 不低于 "
            f"qualified_score={config.qualified_score}，边界档会跟推送区重叠"
        )
    if config.a_quota < 1 or config.b_quota < 1:
        # 配额是 0 等于这一档关掉了，而报表上看起来是「今天这一档没有线索」。
        # 真要关掉某一档，该删掉那一档而不是把配额设成 0。
        raise ValueError(f"配额要至少 1：a_quota={config.a_quota} b_quota={config.b_quota}")

    return config


@dataclass(frozen=True)
class Digest:
    """一份早报里到底放什么。分档和截断都已经算完了。

    一个实际值（某跑打了 20 家的分，其中 3 家过线）：

        Digest(a=(), b=(XGIMI 66, Chatterly 62, ORBO 61), c=(),
               held_back_a=0, held_back_b=0,
               review_samples=(...5 条...), review_total=5)

    c 是没过线的那 17 家，只在 push_below_line 打开时才有东西；那时 review_samples
    是空的（抽样没意义了，它们全在 c 里），review_total 仍然照数。

    held_back_* 记的是「按分数排在配额之外」的条数，不是被丢掉的条数 ——
    它们照样在库里，只是今天没上卡片。这个数必须印出来：配额是天花板，
    而天花板挡住了什么如果不说，那就跟没有线索完全一样了。

    review_total 跟 len(review_samples) 分开存：折叠区只放抽样的几条，
    但「边界档一共有多少条」是 Riley 判断「今天这批值不值得花时间标」的
    依据，两个数不一样的时候差别很重要。
    """

    a: tuple[LeadCard, ...]
    b: tuple[LeadCard, ...]
    c: tuple[LeadCard, ...]
    held_back_a: int
    held_back_b: int
    review_samples: tuple[LeadCard, ...]
    review_total: int

    # 基线那一档的分数区间，已经格式化成 "40-59" 这样。
    # 存下来而不是让渲染那一层再去读配置：那两个数是 choose 分档时用的
    # 同一份，分头读会出现「卡片上写 40-59、实际按 45 分档」这种
    # 两处各自看都对的错。
    review_score_range: str

    @property
    def pushed_count(self) -> int:
        """今天真正上卡片的条数。

        输入：无
        输出：一个整数
        """
        return len(self.a) + len(self.b) + len(self.c)

    @property
    def qualified_count(self) -> int:
        """上了卡片的里面过线的条数（A 加 B）。

        输入：无
        输出：一个整数
        """
        return len(self.a) + len(self.b)

    @property
    def held_back_count(self) -> int:
        """被配额挡在外面的条数。

        输入：无
        输出：一个整数
        """
        return self.held_back_a + self.held_back_b


def choose(
    cards: list[LeadCard], *, config: CardConfig, rng: random.Random | None = None
) -> Digest:
    """把打完分的线索分档、按配额截断、抽出折叠区那几条。

    输入：这一跑打了分的全部线索（顺序无所谓）；分档和配额的配置；
          抽样用的随机数发生器（默认现造一个）
    输出：一个 Digest

    先按分数从高到低排一次，A、B、边界三档都从这个序上切 ——
    所以「配额截断」永远是砍掉分数最低的那几条，而不是碰巧排在后面的。

    一个例子：

        输入   20 家：3 家 >= 75、12 家 60-74、5 家 40-59
               config a_quota=5 b_quota=10 review_sample_size=5
               排序 -> A 档 3 家（没超配额）、B 档 12 家（超了 2 家）、
               边界 5 家
        输出   Digest(a=3 条, b=10 条, c=(), held_back_a=0, held_back_b=2,
                      review_samples=5 条, review_total=5)

        同样 20 家，config.push_below_line=True
        输出   Digest(a=3 条, b=10 条, c=5 条, held_back_a=0, held_back_b=2,
                      review_samples=(), review_total=5)
               没过线的全进 c，不截配额；A、B 的配额照旧

    rng 从参数进，跟这个仓库里 now 从参数进是同一条理由：折叠区是随机
    抽的，不从外面给种子的话，同一份数据两次跑不出同样的早报，
    而回归对照就没有立足点。

    分数相同的两条谁排前面：按 lead_id 兜底成全序。不兜的话排序不稳定，
    配额刚好卡在中间时今天留甲明天留乙，两次都不报错。
    """
    rng = rng or random.Random()
    ranked = sorted(cards, key=lambda card: (-card.score, card.lead_id))

    # 三档在同一个序上切。低于 review_score 的平时一条都不进早报，
    # 连折叠区也不进 —— 它们只落库。
    a_all = [c for c in ranked if c.score >= config.a_score]
    b_all = [c for c in ranked if config.qualified_score <= c.score < config.a_score]
    review_all = [c for c in ranked if config.review_score <= c.score < config.qualified_score]

    # 截断取前 quota 条（已经是从高到低），剩下的只数个数。
    a = tuple(a_all[: config.a_quota])
    b = tuple(b_all[: config.b_quota])

    # 输入   push_below_line=True，没过线的 5 家（52、45、40、18、9 分）
    # 输出   c=(52, 45, 40, 18, 9)，review_samples=()
    #        没过线的全推，所以不再从边界档里抽样：抽出来的几条本来就在 c 里，再列一遍是重复
    if config.push_below_line:
        return Digest(
            a=a,
            b=b,
            c=tuple(c for c in ranked if c.score < config.qualified_score),
            held_back_a=len(a_all) - len(a),
            held_back_b=len(b_all) - len(b),
            review_samples=(),
            review_total=len(review_all),
            review_score_range=f"{config.review_score}-{config.qualified_score - 1}",
        )

    # 折叠区随机抽，不取分数最高的几条：取最高的等于只给她看「差一点就
    # 过线」的那批，而这一档真正的用处是帮她画出那条线到底该在哪。
    sample_size = min(config.review_sample_size, len(review_all))
    samples = rng.sample(review_all, sample_size) if sample_size else []
    # 抽完再按分数排一遍：随机决定的是「抽谁」，不是「怎么摆」。
    samples.sort(key=lambda card: (-card.score, card.lead_id))

    return Digest(
        a=a,
        b=b,
        c=(),
        held_back_a=len(a_all) - len(a),
        held_back_b=len(b_all) - len(b),
        review_samples=tuple(samples),
        review_total=len(review_all),
        review_score_range=f"{config.review_score}-{config.qualified_score - 1}",
    )


def _score_table(card: LeadCard, weights: dict[str, int]) -> list[dict]:
    """七个维度排成两列一张表：左列是问题，右列是分数。

    输入：一条线索；每一维的权重
    输出：两个 field，第一个装全部项目名，第二个装全部分数

    输出长这样（两个 field 各自是七行）：

        做的是不是音频设备  (权重 25)        8 / 10
        最近发生了什么事    (权重 20)        8 / 10
        说话的人能不能拍板  (权重 10)        7 / 10
        量够不够 500 起订   (权重 15)        8 / 10
        这件事有多新        (权重 10)       10 / 10
        找不找得到人        (权重 10)        6 / 10
        几件事互相印证      (权重 10)        6 / 10

    为什么是「两个多行 field」而不是「十四个单行 field」：Slack 的 fields
    上限是 10 项，七维要 14 项，拆成两个 section 的话两段之间会多一道
    空行，把一张表切成两半。把七行塞进同一个 field 里换行，就是一整块。

    代价是对齐靠 Slack 自己排：某一行的项目名在窄窗口里折行的话，左右
    两列就会错开一行。项目名现在都在十来个字，正常宽度不会折 ——
    这是个已知的取舍，不是没想到。真出现了就得把项目名再缩短，
    而不是回去拆成两段。

    权重写在项目名后面，因为不写出来「品类 8 分」和「时效 8 分」看起来
    一样重，而实际差两倍半 —— 那会让人对着一张卡得出错的结论。
    权重从配置读不写死，否则改了 scoring.yaml 卡片上还印着旧数字。

    分数不加粗：一列里七个数全是粗体，等于一个都不突出，而且跟左边加粗
    的项目名抢。写成 "8 / 10" 不是光写 "8"：单看一个 8 认不出它是十分制。
    总分那一行同理写成 "80 / 100" —— 分项是 0-10、总分是七维加权后的
    0-100，两套刻度同时出现在一张卡上，不标出来一定会被读成同一套。

    用 section 的 fields，刻意不用 context 块：context 在 Slack 里渲染成
    小号灰字，而分数是这张卡上最该被看见的东西。
    """
    labels = []
    values = []
    for field, label in _LLM_LABELS + _DET_LABELS:
        labels.append(f"*{label}*  (权重 {weights[field]})")
        values.append(f"{getattr(card, field)} / 10")
    return [
        {"type": "mrkdwn", "text": "\n".join(labels)},
        {"type": "mrkdwn", "text": "\n".join(values)},
    ]


def _esc(text: str) -> str:
    """Slack mrkdwn 里的三个保留字符转义。

    输入：'Makers&Co'
    输出：'Makers&amp;Co'

    不转的话 Slack 会把 & 后面、< > 里面的东西当成它的语法（链接、@某人），显示出来就错了
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str) -> dict:
    """一个 mrkdwn 的 section 块。"""
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _link(url: str) -> str:
    """一个可点的链接，显示的就是网址本身。

    输入：'https://www.linkedin.com/in/sam-hadid-a1b2c3d4'
    输出：'<https://www.linkedin.com/in/sam-hadid-a1b2c3d4>'

    Slack 里 <网址> 不带 | 显示文字，就原样印出网址。不用「打开」这种字：看得到网址才知道点过去是哪里
    """
    return f"<{url}>"


def _quote_lines(quote: CardQuote, number: int | None) -> list[str]:
    """一句引用的两行：先证据分析（有的话），再原文引用。

    输入：CardQuote(text="In the Vault is on it's way ...", meaning="发言者提到Hushly正在制作音频节目……")；
          编号（一组里有几句时是 1、2、3，只有一句时是 None）
    输出：['证据分析：发言者提到Hushly正在制作音频节目……', '原文引用：', '> In the Vault is on it's way ...']
          有编号时标题带上编号：'证据分析 1：…'、'原文引用 1：'
    """
    tag = f" {number}" if number else ""
    lines = []
    if quote.meaning:
        lines.append(f"证据分析{tag}：{_esc(quote.meaning)}")
    lines.append(f"原文引用{tag}：")
    lines.append(f"> {_esc(quote.text)}")
    return lines


def _evidence_section(card: LeadCard) -> list[dict]:
    """「帖子证据」那一区：每条帖子一组，标 1. 2. 3.，每组纵向排。

    输入：LeadCard 的 evidence / unplaced_quotes / unpaired_means
    输出：Block Kit 的块列表，一个标题块加每组一块（每组单独一块，免得超过 Slack 一块 3000 字的上限）

    一组长这样（Hushly 的真数据）：

        *1.*
        发帖人：sam hadid
        发帖人身份：ceo @ Hushly - a sanctuary for human sound
        时间：2026-09-21
        帖子链接：<https://www.linkedin.com/posts/sam-hadid-a1b2c3d4_in-the-vault-...-pmIJ>
        证据分析：发言者提到Hushly正在制作音频节目，显示出他们在音频领域的活跃。
        原文引用：
        > In the Vault is on it's way to becoming one of music's household series

    证据分析和原文引用的先后是：先告诉人这几句说明了什么，再给原话。
    解读跟引用句数对不上、没法一句配一句时（unpaired_means 有值），那一整段放在标题下面，
    标「证据分析（整体）」，各组里只放原文引用。
    找不到出自哪条帖子的引用，放在最后一组「其他原文引用」
    """
    head = ":mag: *帖子证据*"
    if card.unpaired_means:
        head += f"\n证据分析（整体）：{_esc(card.unpaired_means)}"
    blocks: list[dict] = [{"type": "divider"}, _section(head)]

    for i, item in enumerate(card.evidence, start=1):
        # 输出   ['*1.*', '发帖人：sam hadid', '发帖人身份：ceo @ Hushly ...',
        #         '时间：2026-09-21', '帖子链接：<https://www.linkedin.com/posts/...>', ...]
        lines = [
            f"*{i}.*",
            f"发帖人：{_esc(item.author) or '-'}",
            f"发帖人身份：{_esc(item.role) or '-'}",
            f"时间：{item.when or '-'}",
            f"帖子链接：{_link(item.url)}",
        ]
        numbered = len(item.quotes) > 1
        for n, quote in enumerate(item.quotes, start=1):
            lines.extend(_quote_lines(quote, n if numbered else None))
        blocks.append(_section("\n".join(lines)))

    if card.unplaced_quotes:
        lines = ["*其他原文引用*（没对上是上面哪条帖子的）"]
        numbered = len(card.unplaced_quotes) > 1
        for n, quote in enumerate(card.unplaced_quotes, start=1):
            lines.extend(_quote_lines(quote, n if numbered else None))
        blocks.append(_section("\n".join(lines)))
    return blocks


# 每种关系在卡片上那一句话。措辞跟 triggers/contacts.py 开头每种情况的「卡片」那一格一致
# 起草对话里列「这家卡片上的人」时也用这张表（drafting/planner.py），两处说法一样
RELATION_TEXT = {
    CardRelation.OWN_PAGE: "这家公司自己的主页发的帖",
    CardRelation.OTHER_PAGE: "发帖的是别的机构，不是这家公司的，但仍可联络",
    CardRelation.MEMBER: "这家公司的人",
    CardRelation.NOT_LOOKED_UP: "职位串里写着这家公司，还没核对他现在是不是在这家",
    CardRelation.LEFT: "他名字下面写着这家公司，但现在已不在这家",
    CardRelation.NOT_MEMBER: "发帖的人不是这家公司的，只是在帖子里提到了它，仍可联络",
    CardRelation.UNCLEAR: "看不出发帖人是不是这家公司的，仍可联络",
}

# 找邮箱的服务商给的状态，换成人话。只列见过的值；没见过的原样印出来，不猜它是什么意思。
# 起草对话第一问列邮箱时也用这张表（drafting/nodes.request_question），两处说法一样
EMAIL_STATUS_TEXT = {
    "risky": "不确定能不能收到",
    "valid": "能收到，服务商验证过这个地址",
}


def _email_lines(email: CardEmail, number: int) -> list[str]:
    """「联系方式 / 邮箱」里的一个邮箱，每项一行、带小标题。

    两个例子：

        *1.*
        地址：sam@hushly.example
        来源：sam hadid 的 LinkedIn 主页（<https://www.linkedin.com/in/sam-hadid-a1b2c3d4>）
        能不能收到：不确定能不能收到（服务商原话 risky）
        能不能事先验证：不能。对方邮箱什么地址都收，填错了也不会退信，所以没法事先确认这个地址真的有人在用
        公开出现过：在谷歌搜索结果里公开出现过
        质量分：75 / 100（找邮箱的服务商打的分）

        *2.*
        地址：contact@hushly.example
        来源：官网首页（<https://home.hushly.example>）
        可靠程度：官网上公开写着的，没有另外验证过

    可信度不够的也印，但必须配着可信度；没有的那一项不印
    """
    lines = [f"*{number}.*", f"地址：{_esc(email.value)}"]

    if email.source is ContactSource.WEBSITE:
        page = f"（{_link(email.source_url)}）" if email.source_url else ""
        lines.append(f"来源：官网首页{page}")
        lines.append("可靠程度：官网上公开写着的，没有另外验证过")
        return lines

    whose = f"{_esc(email.person_name)} 的 " if email.person_name else ""
    link = f"（{_link(email.source_url)}）" if email.source_url else ""
    lines.append(f"来源：{whose}LinkedIn 主页{link}")
    # 输入   other_company='Rawcut'（Evan Park 替 Kestrel 查出来的 evan.park@rawcutstudio.example）
    # 输出   '注意：这是他在 Rawcut 的邮箱，不是这家的'
    if email.other_company:
        lines.append(f"注意：这是他在 {_esc(email.other_company)} 的邮箱，不是这家的")
    if email.email_status:
        # 输入   'risky'        输出   '不确定能不能收到（服务商原话 risky）'
        # 输入   'valid'        输出   '能收到，服务商验证过这个地址（服务商原话 valid）'
        # 输入   没见过的值     输出   '服务商标的是「xxx」'
        known = EMAIL_STATUS_TEXT.get(email.email_status)
        raw = _esc(email.email_status)
        status = f"{known}（服务商原话 {raw}）" if known else f"服务商标的是「{raw}」"
        lines.append(f"能不能收到：{status}")
    if email.catch_all:
        lines.append(
            "能不能事先验证：不能。对方邮箱什么地址都收，填错了也不会退信，所以没法事先确认这个地址真的有人在用"
        )
    if email.found_in_google:
        lines.append("公开出现过：在谷歌搜索结果里公开出现过")
    if email.quality_score is not None:
        lines.append(f"质量分：{email.quality_score} / 100（找邮箱的服务商打的分）")
    return lines


def _person_lines(person: CardPerson, number: int) -> list[str]:
    """「联系方式 / 联系人」里的一个作者，每项一行、带小标题。

    两个例子：

        *1.*
        名字：sam hadid
        跟这家的关系：这家公司的人
        职位：Founder
        LinkedIn 主页：<https://www.linkedin.com/in/sam-hadid-a1b2c3d4>
        相关帖子：<https://www.linkedin.com/posts/sam-hadid-a1b2c3d4_in-the-vault-...-pmIJ>

        *2.*
        名字：audioXpress
        跟这家的关系：发帖的是别的机构，不是这家公司的，但仍可联络
        LinkedIn 公司页：<https://www.linkedin.com/company/audioxpress/>
        相关帖子：<那条帖子的链接>

    职位只有「这家公司的人」有（查回来的现职）；「现在在」只有「已不在这家」有。
    发帖的是公司主页（这家自己的，或别的机构的）时，链接那一项叫「LinkedIn 公司页」，个人叫「LinkedIn 主页」
    """
    lines = [
        f"*{number}.*",
        f"名字：{_esc(person.name)}",
        f"跟这家的关系：{RELATION_TEXT[person.relation]}",
    ]
    if person.relation is CardRelation.MEMBER and person.position:
        lines.append(f"职位：{_esc(person.position)}")
    if person.relation is CardRelation.LEFT and person.current_company:
        lines.append(f"现在在：{_esc(person.current_company)}")
    is_page = person.relation in (CardRelation.OWN_PAGE, CardRelation.OTHER_PAGE)
    lines.append(f"{'LinkedIn 公司页' if is_page else 'LinkedIn 主页'}：{_link(person.profile_url)}")
    if person.post_url:
        lines.append(f"相关帖子：{_link(person.post_url)}")
    return lines


def _contact_blocks(contacts: CardContacts) -> list[dict]:
    """「联系方式」那一区：邮箱、网站、联系人三组，每一项纵向排、带小标题。

    输入：CardContacts，例子见那个类
    输出：Block Kit 的块列表：一个标题块，加每组一块

    三组，组不标号，组里的每一个标 1. 2. 3.：

        邮箱     一定有这一组：找到几个列几个，一个都没有写「还没找到」
        网站     官网和 LinkedIn 公司页，有哪个列哪个；都没有这一组不出现
        联系人   每个作者一个，同一个人只一个；一个都没有这一组不出现
    """
    blocks: list[dict] = [{"type": "divider"}, _section(":telephone_receiver: *联系方式*")]

    # 邮箱
    lines = ["*邮箱*"]
    if contacts.emails:
        for n, email in enumerate(contacts.emails, start=1):
            lines.extend(_email_lines(email, n))
    else:
        lines.append("还没找到")
    blocks.append(_section("\n".join(lines)))

    # 网站
    # 输出   '官网：<https://hushly.example>'、'LinkedIn 公司页：<https://www.linkedin.com/company/hushly/>'
    sites = []
    if contacts.website_url:
        sites.append(f"官网：{_link(contacts.website_url)}")
    if contacts.company_page_url:
        sites.append(f"LinkedIn 公司页：{_link(contacts.company_page_url)}")
    if sites:
        blocks.append(_section("\n".join(["*网站*", *sites])))

    # 联系人
    if contacts.people:
        lines = ["*联系人*"]
        for n, person in enumerate(contacts.people, start=1):
            lines.extend(_person_lines(person, n))
        blocks.append(_section("\n".join(lines)))
    return blocks


def _profile_block(profile: CompanyProfile | None) -> list[dict]:
    """「公司概况」那一区。没抓过公司详情（C2，或者还没找过）就整区不出现。

    输入：CompanyProfile(employee_count=11, employee_count_range="2-10", founded_year=2022,
                         location="United States", industries=("Music",), ...)
    输出：[divider, section]，section 的文字每项一行：

        :information_source: *公司概况*
        人数：11 人（公司自己选的规模档 2-10）
        成立：2022 年
        总部：United States
        行业：Music

    缺哪样就不印哪样；一样都没有也不出现
    """
    if profile is None:
        return []
    lines = []
    # 输出   '人数：11 人（公司自己选的规模档 2-10）'；只有人数档 '人数：规模档 2-10'；只有人数 '人数：11 人'
    if profile.employee_count is not None and profile.employee_count_range:
        band = _esc(profile.employee_count_range)
        lines.append(f"人数：{profile.employee_count} 人（公司自己选的规模档 {band}）")
    elif profile.employee_count is not None:
        lines.append(f"人数：{profile.employee_count} 人")
    elif profile.employee_count_range:
        lines.append(f"人数：规模档 {_esc(profile.employee_count_range)}")
    if profile.founded_year:
        lines.append(f"成立：{profile.founded_year} 年")
    if profile.location:
        lines.append(f"总部：{_esc(profile.location)}")
    if profile.industries:
        lines.append(f"行业：{_esc('、'.join(profile.industries))}")
    if not lines:
        return []
    return [{"type": "divider"}, _section("\n".join([":information_source: *公司概况*", *lines]))]


def lead_message(card: LeadCard, *, band: str, run_id: int, weights: dict[str, int]) -> dict:
    """一条线索一条消息。返回 chat.postMessage 的参数。

    输入：一条线索；它在哪一档（"A"、"B"，push_below_line 打开时还有 "C"）；这一跑的 run_id；七维的权重
    输出：一个字典，可以直接展开传给 chat.postMessage

    一条卡片的分区顺序是「是谁 -> 多少分 -> 什么样的公司 -> 凭什么 -> 帖子证据 -> 找谁 -> 动作」：

        section   重线，卡片上边界
        header    公司记号加公司名，Slack 里最大的字号
        section   档位和总分
        section   分数明细（小标题 + 七维两列，实现上是两段 fields）
        divider   细线，区之间
        section   公司概况（抓过公司详情才有）
        divider
        section   分数分析
        divider
        section   帖子证据（标题），再每条帖子一块：1. 2. 3.
        divider
        section   联系方式（标题），再每组一块：邮箱、网站、联系人
        divider
        actions   三个按钮
        section   重线，卡片下边界

    两种线是刻意分开的：外面两条用重线字符画（深、粗），里面用原生
    divider（浅、细）。Slack 只有一种 divider、粗细颜色都不能调，
    两处用同一种线就分不出「这是卡片的边界」和「这是卡片里的一个分区」。

    七个维度显示成一个区，实现上是两段 fields：Slack 的 fields 上限是
    10 项，七维要 14 项。小标题只挂在第一段上，第二段不带标题，
    看起来就是接着往下排的一张表。

    「证据分析」跟「原文引用」在同一组里但各有小标题：前者是模型读完之后的结论，后者是对方写下的字。
    标清楚才分得开哪句是判断、哪句是事实，而判断可能错、原话不会。结论那一句还兼一个闸门查不出来的
    作用 —— 引用逐字抄对了，不代表那句话说的是这家公司（同一篇帖子里可能提到好几家），
    而让模型把结论写出来，张冠李戴就会自己露出来。

    色条走 attachment，那是 Slack 唯一能给消息上色的机制。用它就得接受
    Slack 加的那行「Added by <应用名>」。但「降级文本在客户端里重复显示
    一遍」这个问题可以单独解掉：fallback 写在 attachment 内部、顶层不给
    text。顶层给 text 的话它会显示在色条上方，等于把通知栏那句又印一遍。

    四种情况：

    一、完整的一条

        输入   card=LeadCard(display_name="Chatterly Inc.", score=80, ...)
               band="A"  run_id=27  weights={"icp_fit": 25, ...}
        输出   {"attachments": [{"color": "#2eb886",
                                 "fallback": "A 档 80 分 Chatterly Inc.",
                                 "blocks": [十几块]}]}

    二、模型没给分析或者没给证据结论

        输出   少对应那一区，别的照旧
        后果   两者为空本来会被 analyzer 当成不合格重问，所以正常走不到
               这里；但块的拼装不该依赖那个保证 —— 上游改一次闸门就
               崩掉一层 UI 是不可接受的耦合

    三、没有原话

        输出   各组里没有「原文引用」那几行
        后果   同上。防幻觉闸门要求至少一条有效引用，走不到这里

    四、没有证据来源（拿不到链接）

        输出   帖子证据那一区不出现（没有找不到出处的引用、也没有整体解读时）；按钮照旧在
        后果   按钮永远在，因为 Riley 的判断不能因为少一个链接就没法表达
    """
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": OUTER_RULE}},
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{COMPANY_MARK} {card.display_name}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{BAND_LABELS[band]}*        *{card.score} / 100*",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":bar_chart: *分数明细*"},
            "fields": _score_table(card, weights),
        },
    ]

    # 第二块：公司概况（抓过公司详情才有）。放在分数后面：先知道这是一家什么样的公司
    blocks.extend(_profile_block(card.contacts.profile))

    if card.analysis:
        blocks.append({"type": "divider"})
        analysis = "\n".join(_analysis_lines(card.analysis))
        blocks.append(_section(f":brain: *分数分析*\n{analysis}"))

    # 帖子证据：每条帖子一组，发帖人、身份、时间、链接，再证据分析、原文引用
    if card.evidence or card.unplaced_quotes or card.unpaired_means:
        blocks.extend(_evidence_section(card))

    # 联系方式：邮箱、网站、联系人三组
    blocks.extend(_contact_blocks(card.contacts))

    # 按钮回调要靠它认出「点的是哪条线索、哪一跑推的那张卡」。
    # 键名是接口不是文案：buttons.py 那边按它解析。
    payload = json.dumps({"lead_id": card.lead_id, "run_id": run_id}, separators=(",", ":"))
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "actions",
            # block_id 和 value 里各带一份 lead_id 和 run_id：Slack 两种回调
            # 载荷（block_actions 和 view_submission）带回来的字段不完全一样。
            "block_id": f"{LEAD_BLOCK_PREFIX}{card.lead_id}:run:{run_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_APPROVE,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "要联系"},
                    "value": payload,
                },
                {
                    "type": "button",
                    "action_id": ACTION_SKIP,
                    # 中间这个按钮没有第三种颜色可选：Slack 只给 primary（绿）
                    # 和 danger（红）两种样式，剩下的一律是默认白底。
                    "text": {"type": "plain_text", "text": "这次先不"},
                    "value": payload,
                },
                {
                    "type": "button",
                    "action_id": ACTION_REJECT,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "不该联系"},
                    "value": payload,
                },
            ],
        }
    )
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": OUTER_RULE}})

    # fallback 放在 attachment 里面，顶层不给 text：顶层的 text 会显示在
    # 色条上方，等于把通知栏那句在客户端里再印一遍。
    return {
        "attachments": [
            {
                "color": BAND_COLORS[band],
                "fallback": f"{BAND_LABELS[band]} {card.score} 分 {card.display_name}",
                "blocks": blocks,
            }
        ]
    }


def _verdict_line(analysis: str) -> str:
    """从整段分析里只取「结论」那一行。

    输入：analyzer 写的那段分析，格式是固定的五行
          （品类：/ 事件：/ 层级：/ 起订量：/ 结论：）
    输出：结论那一行；找不到就退回第一行；整段是空就是空串

    三种情况：

        输入 "品类：做耳机。\n事件：众筹刚结束。\n结论：值得现在联系。"
        输出 "结论：值得现在联系。"

        输入 "这家公司做耳机，值得联系。"     没有固定格式
        输出 "这家公司做耳机，值得联系。"     退回第一行

        输入 ""
        输出 ""

    为什么基线那一区只放这一行：整段五行贴进去的话，Slack 会自己把长
    文本折起来、加一个「Show more」—— 那个折叠不是我们加的，是客户端
    对超长 section 的处理，控制不了。而基线区的用途是让她快速扫一遍
    决定标什么，每条都要点一次「Show more」就白设计了。
    真要细看，点链接看原帖比读摘要准。

    退回第一行而不是返回空：格式是 prompt 定的，模型偶尔不照做，
    那时候给她第一句仍然有用 —— 而给空串会让这一条只剩分数和公司名，
    正好回到「只给公司名她判断不了」那个问题上。
    """
    lines = _analysis_lines(analysis)
    if not lines:
        return ""
    for line in lines:
        if line.startswith("结论"):
            return line
    return lines[0]


# 分析那五行的小标题前面该断行的地方：前面是空白（空白换成换行），或者紧跟在句号后面（插一个换行）。
# 只认这两种位置，句子中间碰巧出现「事件：」不会被拆开
_ANALYSIS_BREAK = re.compile(r"(?:\s+|(?<=[。！？.]))(?=(?:品类|事件|层级|起订量|结论)：)")


def _analysis_lines(analysis: str) -> list[str]:
    """analyzer 写的那段分析拆成一项一行（品类：/ 事件：/ 层级：/ 起订量：/ 结论：）。

    prompt 要求五项之间用换行隔开，模型有时候不照做，五项用空格连成一行，卡片上就成了一大段：

        输入   "品类：Draftex Legal并不涉及音频设备的制造。 事件：没有任何证据。 结论：不值得联系。"
        输出   ["品类：Draftex Legal并不涉及音频设备的制造。", "事件：没有任何证据。", "结论：不值得联系。"]

        输入   "品类：做耳机。\n事件：众筹刚结束。"            本来就换了行的，不变
        输出   ["品类：做耳机。", "事件：众筹刚结束。"]

    每行首尾的空白去掉（模型有时候在行尾留两个空格），空行去掉
    """
    text = _ANALYSIS_BREAK.sub("\n", analysis)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _baseline_blocks(digest: Digest) -> list[dict]:
    """打分基线参考那一区：40 到 59 分那一档的抽样。

    输入：分档算完的 Digest
    输出：Block Kit 的块列表，没有抽样就是空列表

    这一档不是「快过线了」。它是攒给 Riley 标注的：推给她的要严，
    给她标的要有量，否则「多少分算值得联系」这条线永远定不下来 ——
    A/B/C 三个阈值现在全是猜的，只有她的标注能反推。

    每条给的是分数、公司名、一句分析、一个链接。只给公司名她判断不了，
    而判断不了的样本标不出东西来 —— 那这一区就白占地方。

    一句分析截到一百二十字：这一区是让她快速过一遍，不是让她读完整份
    分析。真要细看，点链接看原帖比读摘要准。
    """
    if not digest.review_samples:
        return []

    blocks: list[dict] = [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":triangular_ruler: *打分基线参考*\n"
                    f"{digest.review_score_range} 分这一档共 {digest.review_total} 条，"
                    f"抽 {len(digest.review_samples)} 条。"
                    "这些不是让你联系的，是用来定「多少分算值得联系」那条线的。"
                ),
            },
        },
    ]

    for card in digest.review_samples:
        line = f"*{card.score} / 100*    *{card.display_name}*"
        verdict = _verdict_line(card.analysis)
        if verdict:
            line += f"\n{verdict}"
        if card.evidence:
            line += f"\n{card.evidence[0].url}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})

    return blocks


def summary_message(
    digest: Digest, *, run_id: int, company_count: int, origin: str | None, failed: bool = False
) -> dict:
    """汇总那一条。样式跟线索卡刻意不一样。

    输入：分档截断都算完的 Digest；这一跑的 run_id；这一跑分析了几家公司
          （口径是「真花了 LLM 钱打分的家数」，不是拼出几张卡）；
          这一跑是哪条对话让它跑的，那条对话开头那句话的链接，如
          'https://acme-team.slack.com/archives/C0000000001/p1790126072000100'，
          定时那一跑传 None；这一跑的采集是不是全失败了
    输出：一个字典，可以直接展开传给 chat.postMessage

    跟线索卡有四处故意的区别：另一个色条颜色、没有按钮、不出现任何
    分项分数、账目用两列。一屏扫下来要能立刻分出「这条是总账」和
    「这条是一个线索」。

    run 那个数字是 runs 表的主键 —— 每跑一次加一行，所以它同时是
    「第几次跑」和「去库里翻这一跑」的钥匙（runs / query_executions /
    lead_discovery_hits 全按它串）。它放第二行不放标题里：标题该是
    这条消息是什么，编号是细节。

    四种情况：

    一、有线索

        输入   digest 里 A 4 条 B 1 条，company_count=22
        输出   标题「线索报告」，第二行 run 27，然后两列账：
                   分析了公司    22 家
                   推送          5 条（A 4 · B 1）
                   配额挡住      0 条
                   基线参考      2 条

    二、一条都没过线

        输入   digest.pushed_count == 0
        输出   账目照印，多一段说明这是合格结果不是故障
        后果   刻意仍然发这一条：不发的话，「系统挂了」和「今天真没有」
               在频道里长得一模一样

    三、被配额挡住了几条

        输入   digest.held_back_b=2
        输出   「配额挡住 2 条」那一行
        后果   配额是天花板不是地板，而天花板挡住了什么如果不说，
               就跟本来没有线索完全一样

    四、有基线参考

        输入   digest.review_samples 非空
        输出   末尾一区，每条给分数、公司名、一句分析、一个链接

    五、这一跑的采集全失败了

        输入   failed=True（run_discovery 返回的 status 是 failed），
               这时 digest 必然是空的、company_count 是 0
        输出   账目照印（全是 0），情况二那段说明换成「采集全失败了」
        后果   这一条跟情况二必须分开：采集失败时也是一条线索都没有，
               共用情况二那段话的话，卡片上写着「这是合格的结果，不是
               故障」而实际是故障。两种情况长得一模一样，是这套设计里
               最不能出的那种错。
               仍然发这一条而不是沉默：沉默的话 Riley 那天在频道里什么都
               看不到，而「系统挂了」和「今天没线索」又混成一件事了
    """
    pushed = f"{digest.pushed_count} 条"
    if digest.pushed_count:
        # 用中文顿号分隔，不用中点：中点在这张卡上已经被别处用掉了，
        # 同一个符号在一屏里表示两种关系会读起来含糊。
        pushed += f"（A {len(digest.a)}、B {len(digest.b)}"
        # 输出   "（A 0、B 1、C 没过线 14）"。C 只在 push_below_line 打开时有，关着不印
        if digest.c:
            pushed += f"、C 没过线 {len(digest.c)}"
        pushed += "）"

    # 四项八格，在 fields 上限 10 以内。再加一项就要换排法，不能悄悄超。
    tally = [
        {"type": "mrkdwn", "text": "*分析了公司*"},
        {"type": "mrkdwn", "text": f"{company_count} 家"},
        {"type": "mrkdwn", "text": "*推送*"},
        {"type": "mrkdwn", "text": pushed},
        {"type": "mrkdwn", "text": "*配额挡住*"},
        {"type": "mrkdwn", "text": f"{digest.held_back_count} 条"},
        {"type": "mrkdwn", "text": "*基线参考*"},
        {"type": "mrkdwn", "text": f"{digest.review_total} 条"},
    ]

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{SUMMARY_MARK} 线索报告"}},
        {
            "type": "section",
            # 写「采集编号」不写「第 N 次采集」。
            #
            # 这个数是 runs 表的主键（bigserial），从建库起一直往上加、
            # 不重置也不复用 —— 但它不等于「跑过几次」：事务回滚掉的时候
            # 那个号已经被吃掉了，不会还回来。实测第一次真跑拿到的是 27，
            # 而那时候 runs 表里只有一行 —— 前面二十六个号全被「整跑回滚」
            # 的验证跑吃掉了。写成「第 27 次采集」就是一句假话。
            #
            # 它真正的用处是对账的钥匙：runs / query_executions /
            # lead_discovery_hits / lead_feedback 全按它串。
            "text": {"type": "mrkdwn", "text": f"采集编号 {run_id}"},
        },
    ]

    # 输入   origin='https://acme-team.slack.com/archives/C0000000001/p1790126072000100'
    # 输出   多一行小字「来自这条对话」，点开跳到用户 @ bot 的那一句
    #        interactive 推出来的卡片跟定时那一跑混在同一个频道里，不写来处的话，
    #        Riley 分不清这批是每天自动来的，还是有人专门问出来的。
    #        定时那一跑是 None，这一行不出现
    if origin:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"来自<{origin}|这条对话>"}]}
        )
    blocks.append({"type": "section", "fields": tally})

    if failed:
        # 采集全失败的时候也是一条线索都没有，但那跟「今天真的没有」是
        # 两件完全不同的事。共用同一段话的话，卡片上写着「这是合格的结果，
        # 不是故障」而实际是故障 —— 那正是这套设计里最不能出的那种错：
        # 两种情况长得一模一样。
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*这一跑的采集全失败了，所以一条线索都没有。*\n"
                        "这不是「今天没有合格线索」，是采不到数据 —— "
                        "去 journal 里看那几条 query 各自的错码。"
                    ),
                },
            }
        )
    elif not digest.qualified_count and digest.c:
        # push_below_line 打开、一家都没过线：下面的卡片全是灰色的 C 档，
        # 要先说清楚它们没过线，免得被当成推荐联系的
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*这一跑没有过线的线索。* 下面的卡片全是没过线的（灰色 C 档），"
                        "推出来是为了让你看到这一跑抓到了什么。"
                    ),
                },
            }
        )
    elif not digest.pushed_count:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*这一跑没有过线的线索。* 这是合格的结果，不是故障 —— "
                        "配额是天花板不是地板，不够就不凑。\n"
                        "连着几跑都这样的话，是关键词或者 ICP 该调了。"
                    ),
                },
            }
        )

    blocks.extend(_baseline_blocks(digest))

    return {
        "attachments": [
            {
                "color": SUMMARY_COLOR,
                "fallback": f"线索报告 run {run_id} 推送 {digest.pushed_count} 条",
                "blocks": blocks,
            }
        ]
    }
