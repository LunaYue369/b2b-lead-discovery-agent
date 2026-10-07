"""plan_drafts 那一次 LLM 调用的全部东西，加上「这一轮要写哪几份、每份怎么写」那个纯函数。

一次可以要好几份稿（每轮最多 config 里的 max_drafts_per_round 份）。一次模型调用做两件事，
输出里分成两半，一格只属于一半：

    用户说的（*_user_requested / *_ai_understood / user_words）
                只抄用户的原话、把原话对到名单和邮箱的编号上。用户没说的留空，不猜
    AI 定的（*_ai_planned）
                写哪一种（用户没说时）、推我们哪几样卖点（facts.json 的键）、开场引哪条证据、
                为什么这样定。用户一份都没说、或者说的份数比说清的多，AI 再补几份

两半分开，是为了计划里「你说的」和「我定的」分得清：用户看到「卖点：起订量低」时，
知道这是 AI 挑的，不是他说过的。代码对两半各查一遍（parse_ai_plan）：
用户那一半的原话必须在对话里找得到，AI 那一半的键和证据编号必须真有。

这个文件被调两种情况，下面凡是两种情况不一样的地方，都单独写一行「要改计划时：」：

    第一次定计划   用户刚说完这一轮要写什么。例（North Field Technologies）：
                   「给 3 号写封邮件，强调 MOQ 500，再给 2 号写条私信」
                   -> 第 1 份 给 Marc-Andre 的邮件 / 第 2 份 给 Zach 的私信
    要改计划       用户看了上一版计划、回了一句修改意见，classify_plan_reply 判成 revise 绕回来。例：
                   「第 2 份改成加好友附言，再加一篇讲小批量的文章」
                   -> 第 1 份 原样 / 第 2 份 改成附言 / 第 3 份 新加的文章
                   这时模型多拿到两样（build_messages）：上一版原样（previous_drafts，每项标好是第几份）
                   和那句修改意见；交回来的仍是完整的一轮，不是只交改了的那几份

每份稿的几样：

    写给谁      统一编号：1 号 = 这家公司本身（称呼团队、发官网邮箱），2 号起是卡片上的人。
                只有用户能挑人；没说、或者说了名单上认不出，都写给 1 号，AI 不替他挑员工
    写哪一种    邮件 / LinkedIn 私信 / LinkedIn 加好友附言 / 文章。用户没说由 AI 定。
                前三种要有收件人；文章不写给具体的人，是把我们的卖点套到这家身上写的一篇
    发到哪      写邮件时。邮箱单独编号「主人的号-第几个」，例 1-2 = 这家公司的第二个邮箱、
                3-1 = 名单上 3 号那个人的邮箱。用户选了号就用那个，没说由代码按固定顺序挑
    要强调的    用户说的点，原话
    卖点        AI 从 facts.json 里挑的键，例 moq / product_families
    开场        AI 挑的一条证据（signal_id），没有合适的是 0

这里不追问用户。说了但认不出的人和邮箱、说的份数跟说清的对不上，一律按固定办法先定下来，
在计划里写一句说明，用户看计划时一眼看得到、要改直接说：

    说了写给谁，名单里对不上            写给这家公司，说明里点出他说的原话
    说了一个地址，卡片上没有            照他说的地址发
    说了一种说法（「sales 那个」），对不上   按固定顺序挑一个，说明里写挑的是哪个
    说了 3 份，只说清 2 份               AI 补的那几份接在后面
    说了 1 份，说清了 2 份               按说清的 2 份写

同一个对象、同一种、同一个地址写了两份（例：先说「给 Dana 写封邮件」，后面又说「Dana 那封邮件强调 MOQ」）
直接合成一份：多半是同一份说了两次。给这家公司的两个不同邮箱各写一封，地址不同，是两份；
文章一律不合并（都写给这家公司，靠讲的卖点区分）；写哪一种是 AI 定的也不合并（用户没说是同一种）。

跟 query/intent.py 同一个路数（为什么这样拼消息、为什么缺键就炸、为什么不重试），那边讲过的这里不重复。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import yaml

from delivery.cards import RELATION_TEXT
from models.contact import CardRelation, ContactSource
from triggers.interactive_discovery.drafting.prompts import AI_PLAN_SYSTEM

# 往上四层到仓库根：drafting/ -> interactive_discovery/ -> triggers/ -> 仓库根
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "drafting.yaml"

# 写哪一种的四个取值。取值 ASCII：代码要拿它做 if，它也会进 checkpoint。
# LinkedIn 的两种分开算：私信对方不是一度好友时可能收不到；加好友附言总能送到，但只有 300 字符
EMAIL = "email"
LINKEDIN_MESSAGE = "linkedin_message"
LINKEDIN_NOTE = "linkedin_note"
ARTICLE = "article"
FORMS = (EMAIL, LINKEDIN_MESSAGE, LINKEDIN_NOTE, ARTICLE)
# 要有收件人的那三种。文章不写给具体的人
NEEDS_RECIPIENT = (EMAIL, LINKEDIN_MESSAGE, LINKEDIN_NOTE)
# 模型可以填的：四种之一，或者空串（没说 / 不用定）
_FORM_VALUES = ("", *FORMS)

# 给人看的叫法。第一问、结束时列的计划都用这一张表
FORM_TEXT = {
    EMAIL: "邮件",
    LINKEDIN_MESSAGE: "LinkedIn 私信",
    LINKEDIN_NOTE: "LinkedIn 加好友附言",
    ARTICLE: "文章",
}

# 卖点（facts.json 的键）给人看的叫法。不写数字：数字只在 facts.json 里，这里写了就会跟它漂开。
# facts.json 加了新的键、这里没跟着加，计划里原样印那个键（tests 焊着每个键都有叫法）
SELLING_POINT_TEXT = {
    "moq": "起订量低（MOQ）",
    "categories": "我们专做的品类（音频和语音设备）",
    "services": "ODM / OEM / 贴牌服务",
    "certifications": "认证测试协调（FCC、CE 等）",
    "company": "公司实力（年限、做过的项目、年产量）",
    "product_families": "现成的产品平台可以改",
}

# ai_decides 里的取值：这一份写哪一种是 AI 定的
AI_FORM = "form"

# 统一编号里「这家公司本身」的号
COMPANY_NUMBER = 1

# 一份稿在账本（state["drafts"]）里的六种状态。怎么变见 langgraph学习/drafting/replan_rewrite_1a.txt 3.13
PLANNED = "planned"  # 要照计划从头写（还没写过，或者计划变了）
WRITTEN = "written"  # 写好了、发给用户看了，等他说话
REWRITE = "rewrite"  # 计划不变，按 feedback 改正文
REPLAN = "replan"  # 要改计划，只在 classify_feedback 到 plan_drafts 之间存在
APPROVED = "approved"  # 定稿
DROPPED = "dropped"  # 不要了
STATUSES = (PLANNED, WRITTEN, REWRITE, REPLAN, APPROVED, DROPPED)
# write_draft 要写的两种
TO_WRITE = (PLANNED, REWRITE)

# 发给卡片上哪几类人不用提醒：确认是这家的人、职位写着这家还没核对的人。
# 其余的（提到这家的人、别的机构、已经离开这家的人、看不出是谁的）照样能写，
# 但计划里要写一句，免得用户没注意到写给了外人
INSIDERS = (CardRelation.MEMBER.value, CardRelation.NOT_LOOKED_UP.value)


@dataclass(frozen=True)
class PlanConfig:
    """config/drafting.yaml 摊平之后的样子。

    实际值（今天那份配置）：

        PlanConfig(model='gpt-4.1', max_tokens=2000, temperature=0.0, request_timeout_seconds=30.0,
                   max_drafts_per_round=4, default_language='英文', default_tone='简短、专业，不用推销腔')
    """

    model: str
    max_tokens: int
    temperature: float
    request_timeout_seconds: float
    max_drafts_per_round: int
    default_language: str
    default_tone: str


def load_plan_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> PlanConfig:
    """读 config/drafting.yaml 的 plan 和 defaults 两段。

    输入：配置文件路径（不给就是仓库根的 config/drafting.yaml）
    输出：PlanConfig，值见那个类

    缺键当场 KeyError，不给默认值；不缓存。理由同 query/intent.load_intent_config
    """
    # 输入   整份 yaml 解析出来的字典
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    plan, defaults = raw["plan"], raw["defaults"]
    return PlanConfig(
        model=str(plan["model"]),
        max_tokens=int(plan["max_tokens"]),
        temperature=float(plan["temperature"]),
        request_timeout_seconds=float(plan["request_timeout_seconds"]),
        max_drafts_per_round=int(plan["max_drafts_per_round"]),
        default_language=str(defaults["language"]),
        default_tone=str(defaults["tone"]),
    )


@dataclass(frozen=True)
class AiPlanDraft:
    """模型给出的一份稿：前七格是用户对这一份说了什么，后四格是 AI 替这一份定的。

    一个例子（编的）。Acme Audio 的名单是 1. Acme Audio 这家公司本身 / 2. Dana Lee（Founder）/
    3. Sam Park（提到这家的人），证据有两条：[2720] Dana 本人说众筹刚结束要找工厂做 800 台、
    [2733] Audio Weekly 报道了他们。
        用户说「给 Dana 写封邮件，强调 MOQ 500，再写一篇文章」，拆出两份：
        第一份  AiPlanDraft(user_words="给 Dana 写封邮件，强调 MOQ 500",
                             recipient_user_requested="Dana", recipient_ai_understood=2,
                             email_user_requested="", email_ai_understood="",
                             form_user_requested="email", emphasis_user_requested=("MOQ 500",),
                             form_ai_planned="", selling_points_ai_planned=("moq",),
                             opening_ai_planned=2720,
                             reason_ai_planned="Dana 本人说要找工厂做 800 台，正好在我们起订量之上")
        第二份  AiPlanDraft(user_words="再写一篇文章", recipient_user_requested="",
                             recipient_ai_understood=0, email_user_requested="", email_ai_understood="",
                             form_user_requested="article", emphasis_user_requested=(),
                             form_ai_planned="", selling_points_ai_planned=("moq", "product_families"),
                             opening_ai_planned=2733, reason_ai_planned="…")

        用户说「用第二个官网邮箱给他们写一封」（这家官网上有 1-1、1-2 两个）：
                AiPlanDraft(user_words="用第二个官网邮箱给他们写一封", recipient_user_requested="他们",
                             recipient_ai_understood=1, email_user_requested="第二个官网邮箱",
                             email_ai_understood="1-2", form_user_requested="email", ...)

        用户只说了「写吧」：AI直接补一份，user_words 是 ""，用户那一半全空，
                AiPlanDraft(user_words="", recipient_user_requested="", recipient_ai_understood=0, ...,
                             form_ai_planned="linkedin_note", selling_points_ai_planned=("moq",),
                             opening_ai_planned=2720, reason_ai_planned="…")

        「强调 MOQ 500」只跟着第一份：用户是在说那封邮件时说的。说成「都强调 MOQ 500」才每份都带上

    要改计划时：交回来的是完整的一轮，每一份照样是一个 AiPlanDraft。例（North Field，上一版两份，
    用户回「第 2 份改成加好友附言，再加一篇讲小批量的文章」）：
        第一份  他没点名，每一格照抄上一版：
                AiPlanDraft(user_words="给 3 号写封邮件，强调 MOQ 500", recipient_ai_understood=3,
                            form_user_requested="email", emphasis_user_requested=("MOQ 500",), ...)
        第二份  他点名改了写法：user_words 照旧抄他最早说它的那句，写法填进用户那一半（是他说的）
                AiPlanDraft(user_words="再给 2 号写条私信", recipient_ai_understood=2,
                            form_user_requested="linkedin_note", form_ai_planned="", ...)
        第三份  他在修改里要加的：user_words 抄修改里那句，不是空串（空串是 AI 自己补的，会被丢掉）
                AiPlanDraft(user_words="再加一篇讲小批量的文章", form_user_requested="article",
                            emphasis_user_requested=("小批量",), ...)

    空值一律用空串 / 空元组 / 0，不用 None，理由同 query/intent.DiscoveryIntent
    """

    # 这一项接着计划里的第几份（PlanSlot.number）。0 = 新的一份，号由代码发。
    # 第一次定计划每项都是 0；改计划时沿用旧号，一份由几项合成的几项都填同一个号
    plan_number: int
    # 这一份出自用户哪句话，抄原话。代码拿它去对话里找（找不到说明这一份是模型编的，见 parse_ai_plan）。
    # "" = 这一份是 AI 补的
    # 要改计划时：上一版就有的那份，照旧抄他最早说它的那句（例「再给 2 号写条私信」），
    # 不换成修改意见那句；他在修改里新加的那份，抄修改里那句（例「再加一篇讲小批量的文章」）
    user_words: str
    # 用户说要写给谁的原话，例「Dana」「他们创始人」「他们团队」；没说是 ""
    recipient_user_requested: str
    # 模型把上面那句原话对到名单上的第几号（从 1 数，1 号是这家公司本身）。0 = 没说，-1 = 说了但名单里对不上。
    # 让模型填编号、不让它抄名字：名字可能抄错一个字母，编号填错了代码认得出（出了名单的范围）
    recipient_ai_understood: int
    # 用户说发到哪个邮箱的原话，例「dana@acme-audio.example」「1-2」「sales 那个」；没说是 ""
    email_user_requested: str
    # 模型把上面那句原话对到卡片上哪个邮箱，填邮箱的号，例 "1-2"。"" = 没说，"-1" = 说了但对不上。
    # 跟 recipient_ai_understood 一样填号不抄地址：号填错了代码认得出（卡片上没有这个号）
    #
    # 邮箱的编号规则（算号的是 email_numbers）：
    #   号 = 「组号-组里第几个」。组号就是名单编号（recipient_ai_understood 用的那一套），
    #   一个对象一组，组里只放邮箱，不放 LinkedIn 主页链接之类的，LinkedIn 链接不编号。
    #
    #   例（编的）。Acme Audio 官网上有三个邮箱，名单 2 号 Abby 找到三个邮箱，3 号 Cara 找到两个：
    #
    #       1 组  这家公司本身（官网上的邮箱）   1-1  hello@acme-audio.example
    #                                           1-2  sales@acme-audio.example
    #                                           1-3  support@acme-audio.example
    #       2 组  Abby                          2-1  abby@acme-audio.example
    #                                           2-2  abby.chen@acme-audio.example
    #                                           2-3  a.chen@acme-audio.example
    #       3 组  Cara                          3-1  cara@acme-audio.example
    #                                           3-2  cara.li@acme-audio.example
    #
    #   名单上没找到邮箱的人没有组，但名单编号不跟着挪：2 号没有邮箱时，3 号的邮箱还是 3-1，
    #   不会变成 2-1。这样组号永远等于名单编号，用户看到「3-1」就知道是 3 号那个人的。
    #   组里的顺序照 state["lead"]["emails"]（最新找到的在前）。
    #   个人邮箱的主人不在名单上的（按道理不会有），放进 1 组，列出来时带上是谁的
    email_ai_understood: str
    # 用户说的写法："email" / "linkedin_message" / "linkedin_note" / "article" / ""（没说）
    # 要改计划时：以他最后一次说的为准。先说「写条私信」、后来说「第 2 份改成加好友附言」，
    # 这里是 "linkedin_note"，不是 ""（不然计划会把它标成「写哪一种是我定的」）
    form_user_requested: str
    # 用户说这一份要强调的点，原话，例 ("MOQ 500",)
    # 要改计划时：计划里我们写的「卖点（我定的）：起订量低（MOQ）」不算他说的，
    # 模型抄进来的会被 _parse_draft 扔掉（在他说过的话里找不到）
    emphasis_user_requested: tuple[str, ...]
    # AI 定的写法，四种之一。用户说了写法、或者说了邮箱（那就是写邮件）时是 ""：不用它定
    form_ai_planned: str
    # AI 挑的卖点，facts.json 的键，至少一个，例 ("moq", "product_families")
    selling_points_ai_planned: tuple[str, ...]
    # AI 挑的开场引哪条证据，填证据的 signal_id；没有合适的（或者这家一条证据都没有）是 0
    opening_ai_planned: int
    # AI 为什么这样定，一句中文，给用户看的。例「Dana 本人说要找工厂做 800 台，正好在我们起订量之上」
    reason_ai_planned: str


@dataclass(frozen=True)
class AiPlan:
    """模型从整段对话里读出来、再替用户补齐的「这一轮写哪几份」。

    一个例子（编的，Acme Audio，用户说「写三份：给 Dana 写封邮件强调 MOQ 500，再给 Sam 发条加好友附言，
    都用中文」）：

        AiPlan(count_user_requested=3, count_user_words="写三份",
                     drafts=(AiPlanDraft(user_words="给 Dana 写封邮件强调 MOQ 500", ...),
                             AiPlanDraft(user_words="再给 Sam 发条加好友附言", ...),
                             AiPlanDraft(user_words="", ..., form_ai_planned="article", ...)),
                     language_user_requested="中文", tone_user_requested="", unsupported=())

        第三份 user_words 是 ""：用户说三份只说清两份，AI 补了一份。
        只有在用户明确说了写几份的情况下，count_user_requested 才有数值；
        但即使 count_user_requested=0，drafts 也可能有东西

    对照点了按钮之后只回了一句「写吧」或者「看你」的：用户那一半全空，drafts 里全是 AI 补的

        AiPlan(count_user_requested=0, count_user_words="",
                     drafts=(AiPlanDraft(user_words="", ..., form_ai_planned="email", ...),),
                     language_user_requested="", tone_user_requested="", unsupported=())
    """

    # 用户说了要几份，例「写三份」-> 3；没说数是 0
    count_user_requested: int
    # 说份数的那句原话，例「写三份」。代码拿它去对话里找：找不到说明这个数是模型编的，当作没说
    count_user_words: str
    # 一份一份。用户说的按他第一次提到的顺序，AI 补的接在后面
    # 要改计划时：完整的一轮（没点名的也在），他删掉的那份不在，他新加的接在后面
    drafts: tuple[AiPlanDraft, ...]
    # 语言、语气的原话，例「中文」「随意一点」；没说是 ""。所有份共用
    # 要改计划时：计划里我们写的「语言：英文」「语气：简短、专业…」不算他说的。模型抄进来的
    # 会被 parse_ai_plan 当成没说（不然计划上「你没说，按默认」那两句说明就没了）
    language_user_requested: str
    tone_user_requested: str
    # 这一步做不到的要求原话，例 ("帮我直接发出去",)
    unsupported: tuple[str, ...]


# 交给 OpenAI 的 json_schema。strict 模式两条硬要求（additionalProperties: false、required 列全），
# 加字段三处一起改（这里、prompts.AI_PLAN_SYSTEM、parse_ai_plan），理由同 query/intent.INTENT_SCHEMA
_DRAFT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "plan_number",
        "user_words",
        "recipient_user_requested",
        "recipient_ai_understood",
        "email_user_requested",
        "email_ai_understood",
        "form_user_requested",
        "emphasis_user_requested",
        "form_ai_planned",
        "selling_points_ai_planned",
        "opening_ai_planned",
        "reason_ai_planned",
    ],
    "properties": {
        "plan_number": {"type": "integer"},
        "user_words": {"type": "string"},
        "recipient_user_requested": {"type": "string"},
        "recipient_ai_understood": {"type": "integer"},
        "email_user_requested": {"type": "string"},
        "email_ai_understood": {"type": "string"},
        # 闭集：代码要拿它做 if
        "form_user_requested": {"type": "string", "enum": list(_FORM_VALUES)},
        "emphasis_user_requested": {"type": "array", "items": {"type": "string"}},
        "form_ai_planned": {"type": "string", "enum": list(_FORM_VALUES)},
        # 不在 schema 里写成 enum：键从 facts.json 现读，schema 是常量。是不是真有这个键由 parse_ai_plan 查
        "selling_points_ai_planned": {"type": "array", "items": {"type": "string"}},
        "opening_ai_planned": {"type": "integer"},
        "reason_ai_planned": {"type": "string"},
    },
}
# 外面一层，drafts 的每一项是 _DRAFT_ITEM_SCHEMA
AI_PLAN_SCHEMA: dict[str, Any] = {
    "name": "draft_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "count_user_requested",
            "count_user_words",
            "drafts",
            "language_user_requested",
            "tone_user_requested",
            "unsupported",
        ],
        "properties": {
            "count_user_requested": {"type": "integer"},
            "count_user_words": {"type": "string"},
            "drafts": {"type": "array", "items": _DRAFT_ITEM_SCHEMA},
            "language_user_requested": {"type": "string"},
            "tone_user_requested": {"type": "string"},
            "unsupported": {"type": "array", "items": {"type": "string"}},
        },
    },
}


class AiPlanCallFailed(RuntimeError):
    """这一次 plan_drafts 调用没拿到能用的回答。

    跟 query/intent.IntentCallFailed 同样的几种情况共用这一个类型（请求失败、被截断、拒答、没内容、
    不是 JSON、缺键、值不合法），这一层对它们做的事一样：补上上下文再抛，整次 invoke 失败，
    用户看到「出错了，回任意一句重试」（runner._recover 情况三）。不自己重试：temperature 是 0
    """


def listed_people(lead: Mapping[str, Any]) -> list[dict[str, Any]]:
    """统一编号里 2 号起的那些人：卡片上的人，去掉「这家公司自己的主页」。

    输入 -> 输出（编的例子）：
        people=[Acme Audio 公司主页（own_page）, Dana Lee, Sam Park] -> [Dana Lee, Sam Park]

    这家公司自己的 LinkedIn 主页就是 1 号（这家公司本身），不再单独占一个号
    """
    return [dict(p) for p in lead["people"] if p["relation"] != CardRelation.OWN_PAGE.value]


def person_line(person: Mapping[str, Any]) -> str:
    """名单里的一个人写成一句：名字、职位、跟这家什么关系。给模型看的名单和给人看的名单用同一句。

    输入 -> 输出（编的例子）：
        {"name": "Dana Lee", "relation": "member", "position": "Founder", ...}
            -> "Dana Lee（Founder，这家公司的人）"
        {"name": "Sam Park", "relation": "not_member", "position": None, ...}
            -> "Sam Park（发帖的人不是这家公司的，只是在帖子里提到了它，仍可联络）"

    关系那句跟卡片上「联系人 / 跟这家的关系」一字不差（cards.RELATION_TEXT）
    """
    relation = RELATION_TEXT[CardRelation(person["relation"])]
    about = f"{person['position']}，{relation}" if person["position"] else relation
    return f"{person['name']}（{about}）"


def company_line(lead: Mapping[str, Any]) -> str:
    """1 号那一行。例："Acme Audio 这家公司本身（称呼他们的团队）" """
    return f"{lead['display_name']} 这家公司本身（称呼他们的团队）"


def target_listing(lead: Mapping[str, Any]) -> str:
    """统一编号的名单，一行一个。编号就是 recipient_ai_understood 要填的那个数。

    输入 -> 输出（编的例子）：
        Acme Audio，卡片上 Dana Lee、Sam Park
            -> "1. Acme Audio 这家公司本身（称呼他们的团队）\\n2. Dana Lee（Founder，这家公司的人）\\n
                3. Sam Park（发帖的人不是这家公司的，…）"
        卡片上一个人都没有 -> "1. Chatterly Inc. 这家公司本身（称呼他们的团队）"
    """
    lines = [f"{COMPANY_NUMBER}. {company_line(lead)}"]
    lines += [f"{i}. {person_line(p)}" for i, p in enumerate(listed_people(lead), start=COMPANY_NUMBER + 1)]
    return "\n".join(lines)


def email_numbers(lead: Mapping[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    """卡片上每个邮箱的号：「主人的编号-在主人名下第几个」。email_ai_understood 要填的就是这个号。

    输入 -> 输出（编的例子。Acme Audio：官网上 hello@、sales@ 两个，名单 2 号 Dana Lee 有 dana@）：
        -> [("1-1", 1, {"value": "hello@acme-audio.example", ...}),
            ("1-2", 1, {"value": "sales@acme-audio.example", ...}),
            ("2-1", 2, {"value": "dana@acme-audio.example", "person_name": "Dana Lee", ...})]
        没有邮箱 -> []

    主人的编号跟统一编号是同一套：官网上的挂在 1 号（这家公司本身）下面，找到的个人邮箱挂在那个人下面。
    个人邮箱的主人不在名单上的（按道理不会有：个人邮箱都是替名单上的人查出来的），也挂在 1 号下面，
    列出来时带上是谁的。
    按主人的编号排，同一个主人名下保持 state["lead"]["emails"] 的顺序（最新找到的在前）
    """
    owner_of = {p["name"]: i for i, p in enumerate(listed_people(lead), start=COMPANY_NUMBER + 1)}
    grouped: dict[int, list[dict[str, Any]]] = {}
    for email in lead["emails"]:
        owner = owner_of.get(email["person_name"], COMPANY_NUMBER)
        grouped.setdefault(owner, []).append(dict(email))
    return [
        (f"{owner}-{k}", owner, email)
        for owner in sorted(grouped)
        for k, email in enumerate(grouped[owner], start=1)
    ]


def email_listing(lead: Mapping[str, Any]) -> str:
    """邮箱编号的清单，一行一个。给模型看的就是它。

    输入 -> 输出（同 email_numbers 的例子）：
        -> "1-1 hello@acme-audio.example\\n1-2 sales@acme-audio.example\\n2-1 dana@acme-audio.example（Dana Lee 的）"
        没有邮箱 -> "（没有）"
    """
    lines = [
        f"{number} {email['value']}" + (f"（{email['person_name']} 的）" if email["person_name"] else "")
        for number, _owner, email in email_numbers(lead)
    ]
    return "\n".join(lines) or "（没有）"


def selling_point_keys(knowledge: Mapping[str, Any]) -> tuple[str, ...]:
    """facts.json 里能当卖点挑的键：下划线开头的（注释、待办）不算。

    输入 -> 输出：knowledge["facts"] 是仓库里那份 facts.json
        -> ("moq", "categories", "services", "certifications", "company", "product_families")
    """
    return tuple(key for key in knowledge["facts"] if not key.startswith("_"))


def evidence_line(evidence: Mapping[str, Any], *, short: bool = False) -> str:
    """一条证据写成一行，给模型挑开场、给人看计划都用它（给模型的那份另外带全文）。

    输入 -> 输出（编的例子）：
        {"signal_id": 2720, "author": "Dana Lee", "role": "Founder @ Acme Audio", "when": "2026-09-20",
         "first_person": True, ...}
            -> "Dana Lee（Founder @ Acme Audio） 2026-09-20 的帖子，本人说的"
        {"signal_id": 2733, "author": "Audio Weekly", "role": "", "when": "2026-09-18",
         "first_person": False, ...}
            -> "Audio Weekly 2026-09-18 的帖子，第三方说的"
        short=True，role 是一整条 LinkedIn 简介（真例子，lead 1289 的 Zach T.）
            "Getting things done in Japan: Fintech | GTM | Strategy | Partnerships | ..."
            -> "Zach T.（Getting things done in Japan: Fintech） 2026-08-19 的帖子，第三方说的"

    short 给计划那一行用：简介只留第一个「|」前面那段，再长就截到 _ROLE_LIMIT 个字加「…」。
    给模型的那份不截：它要靠整条简介判断说话的是谁。
    本人说的还是第三方说的要写出来：第三方说的，开场不能装作偶然看到本人的帖子（drafting_knowledge.yaml）
    """
    role = evidence["role"]
    if short and role:
        role = role.split("|")[0].strip()
        if len(role) > _ROLE_LIMIT:
            role = role[:_ROLE_LIMIT].rstrip() + "…"
    who = f"{evidence['author']}（{role}）" if role else evidence["author"]
    whose = "本人说的" if evidence["first_person"] else "第三方说的"
    return f"{who} {evidence['when']} 的帖子，{whose}"


# 计划里发帖人简介最多留几个字。一行放得下、又认得出是谁
_ROLE_LIMIT = 40


def _knowledge_text(knowledge: Mapping[str, Any]) -> str:
    """knowledge 写成给模型看的一段：这家的情况、证据全文、我们能提供什么。

    输入：state["knowledge"]（retrieve_knowledge 取的那份，形状见 state.py）
    输出（编的例子，折了行）：
        "这家公司的概况：{"website": "https://acme-audio.example", ...}\\n
         为什么推这家（打分 72）：Acme Audio just finished a Kickstarter ...\\n
         打分时引的原话：We just wrapped our Kickstarter\\n
         \\n
         证据（开场要引就填前面的编号）：\\n
         [2720] Dana Lee（Founder @ Acme Audio） 2026-09-20 的帖子，本人说的：We just wrapped ...\\n
         \\n
         我们能提供的（卖点只能从这几个键里挑：moq, categories, ...）：\\n
         {"moq": {...}, ...}"

    没抓过概况的写「（没抓过）」、没有证据的写「（没有）」：让模型知道是没有，不是忘了给
    """
    profile = knowledge["profile"]
    assessment = knowledge["assessment"]
    lines = [
        "这家公司的概况：" + (json.dumps(profile, ensure_ascii=False) if profile else "（没抓过）"),
        f"为什么推这家（打分 {assessment['score']}）：{assessment['analysis']}",
    ]
    if assessment["quotes"]:
        lines.append("打分时引的原话：" + "；".join(assessment["quotes"]))
    lines += ["", "证据（开场要引就填前面的编号）："]
    for e in knowledge["evidence"]:
        line = f"[{e['signal_id']}] {evidence_line(e)}：{e['text']}"
        if e["reposted_text"]:
            line += f"（转发了 {e['reposted_author']} 的：{e['reposted_text']}）"
        lines.append(line)
    if not knowledge["evidence"]:
        lines.append("（没有）")
    keys = ", ".join(selling_point_keys(knowledge))
    lines += [
        "",
        f"我们能提供的（卖点只能从这几个键里挑：{keys}）：",
        json.dumps(knowledge["facts"], ensure_ascii=False),
    ]
    return "\n".join(lines)


def build_messages(
    lines: Sequence[str],
    lead: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    *,
    feedback: str,
    previous: Sequence[Mapping[str, Any]],
    drafts: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> list[dict[str, str]]:
    """把这家公司的名单和邮箱、这家的情况和我们能提供的、（要改时）上一版和他的修改意见、整段对话，
    拼成喂给模型的四到五条消息。

    输入：
        lines       这一轮的 messages，例 ["用户: 给 Dana 写封邮件，再给 Sam 发条加好友附言"]
        lead        state["lead"]，要用 display_name、people、emails
        knowledge   state["knowledge"]，要用 profile、assessment、evidence、facts
        feedback    他对着上一版说的那句修改意见（state["plan_feedback"]）；第一次定计划是 ""
        previous    上一版，previous_drafts 标好了每项是计划里的第几份；第一次定计划是 []
        drafts      账本（state["drafts"]）：改计划时每份标上已定稿 / 只改正文 / 要改计划，列出不要了的号
        plan        上一版计划（state["plan"]）：上一版 drafts 里没有原样的那几份，按它给模型看
    输出（第一次定计划，四条）：
        [{"role": "system", "content": AI_PLAN_SYSTEM},
         {"role": "user",
          "content": "这家公司：Acme Audio\\n名单：\\n1. Acme Audio 这家公司本身（称呼他们的团队）\\n"
                     "2. Dana Lee（…）\\n…\\n卡片上的邮箱：\\n1-1 hello@acme-audio.example\\n"
                     "2-1 dana@acme-audio.example（Dana Lee 的）"},
         {"role": "user",
          "content": "这家公司的概况：…\\n证据（…）：\\n[2720] …\\n我们能提供的（…）：\\n{…}"},
         {"role": "user", "content": "用户: 给 Dana 写封邮件，再给 Sam 发条加好友附言"}]

    如果是re-plan_drats，则要改时在对话那条前面多一条（_revision_text，整段长什么样见那个函数）：
         {"role": "user", "content": "这一轮是在改上一版计划。下面按他看到的计划，一份一份列出…\n\n"
                                     "他看到的第 1 份，是你上一版的 [0] 和 [2] 合成的…\n  [0] {…}\n…\n\n"
                                     "他看完这份计划说的：第 2 份改成私信\n\n请在上一版上改，…"}

    修改意见和上一版明写交进去，不让模型自己从对话里找：要改的是哪一句不靠「取最后一句」那条位置约定，
    上一版是结构化的原样，不用从计划的展示文字里反推（理由同 query/planner.draft_queries 的 feedback /
    previous_plan）。

    三段分成三条：卡片上有什么、这家的情况和我们的卖点、用户说了什么。混在一条里模型可能把名单
    或者证据当成用户的话，user_words 就会抄到不是用户说的东西。
    邮箱带号列出来：模型填号，是谁的邮箱由代码按号查
    """
    about = (
        f"这家公司：{lead['display_name']}\n"
        f"名单：\n{target_listing(lead)}\n"
        f"卡片上的邮箱：\n{email_listing(lead)}"
    )
    messages = [
        {"role": "system", "content": AI_PLAN_SYSTEM},
        {"role": "user", "content": about},
        {"role": "user", "content": _knowledge_text(knowledge)},
    ]
    if previous:
        messages.append({"role": "user", "content": _revision_text(feedback, previous, drafts, plan)})
    messages.append({"role": "user", "content": "\n".join(lines)})
    return messages


def previous_drafts(ai_plan: Mapping[str, Any], plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """上一版的每一项，标上它是用户看到的计划里的第几份。要改计划时交给模型的就是它。

    输入：
    上一轮的ai_plan：：
    ai_plan0,
    ai_plan1,
    ai_plan2,
    ai_plan3

    上一轮的正式plan，即用户真正看到的：
    plan1: source=[0,2]
    plan2: source=[1]

    replan时，用户说，修改第二份plan，指的其实是正式的plan2
    为了让ai可以修改好上一版本的正式的Plan2，ai需要知道，上一轮正式的plan2 source的ai_plan是谁
    因此，输出是要映射回上一轮的ai_plan的，顺序要按照ai_plan的槽位来
    idx=ai_plan0，是正式Plan1的source，plan number = 1
    idx=ai_plan1，是正式Plan2的source, plan number = 2
    idx=ai_plan2，是正式Plan1的source, plan number = 1
    idx=ai_plan3，None，不是任何正式Plan的source, plan number = None

    把这份输出传回ai的时候，ai收到修改plan2的指令，
    于是ai就可以loop previous_draft返回的list of dicts里，哪一个dict里的plan_number=2，
    那个dict里就包含了ai_plan1，于是，ai就可以修改这个原source了

    输入：
        ai_plan   state["ai_plan"]，上一版模型的原话
        plan      state["plan"]，上一版定下的计划，每份带 sources（出自 ai_plan["drafts"] 的哪几项）
    输出：ai_plan["drafts"] 每一项原样复制一份，最前面加一个键 plan_number，顺序跟 ai_plan["drafts"] 一样

    一个完整的例子（编的）。Acme Audio 的名单是 1. 这家公司本身（官网 1-1 hello@）/
    2. Dana Lee（Founder，2-1 dana@）/ 3. Sam Park（提到这家的人，没有邮箱），证据 [2720] Dana 本人的帖子、
    [2733] Audio Weekly 的报道。

    用户说「给 Dana 写封邮件强调 MOQ 500，再给 Sam 写条附言，
    Dana 那封再提一下固件」。他说清了要写哪几份、没说份数，prompt 规定这时不许补；
    这个例子里模型没守规矩，还是多补了一篇文章（[3]），为的是演示 plan_number 是 None 的那种：

        输入 ai_plan（上一版模型的原话，四项）：
            {"count_user_requested": 0, "count_user_words": "",
             "drafts": [
                 [0] {"user_words": "给 Dana 写封邮件强调 MOQ 500", "recipient_user_requested": "Dana",
                      "recipient_ai_understood": 2, "email_user_requested": "", "email_ai_understood": "",
                      "form_user_requested": "email", "emphasis_user_requested": ["MOQ 500"],
                      "form_ai_planned": "", "selling_points_ai_planned": ["moq"], "opening_ai_planned": 2720,
                      "reason_ai_planned": "Dana 本人说要找工厂做 800 台，正好在我们起订量之上"},
                 [1] {"user_words": "再给 Sam 写条附言", "recipient_user_requested": "Sam",
                      "recipient_ai_understood": 3, "email_user_requested": "", "email_ai_understood": "",
                      "form_user_requested": "linkedin_note", "emphasis_user_requested": [],
                      "form_ai_planned": "", "selling_points_ai_planned": ["categories"],
                      "opening_ai_planned": 2733, "reason_ai_planned": "Sam 在报道里提到他们，附言讲品类"},
                 [2] {"user_words": "Dana 那封再提一下固件", "recipient_user_requested": "Dana",
                      "recipient_ai_understood": 2, "email_user_requested": "", "email_ai_understood": "",
                      "form_user_requested": "email", "emphasis_user_requested": ["固件"],
                      "form_ai_planned": "", "selling_points_ai_planned": ["product_families"],
                      "opening_ai_planned": 2720, "reason_ai_planned": "固件对应我们现成的平台"},
                 [3] {"user_words": "", "recipient_user_requested": "", "recipient_ai_understood": 0,
                      "email_user_requested": "", "email_ai_understood": "", "form_user_requested": "",
                      "emphasis_user_requested": [], "form_ai_planned": "article",
                      "selling_points_ai_planned": ["moq"], "opening_ai_planned": 0,
                      "reason_ai_planned": "补一篇讲起订量的文章"}],
             "language_user_requested": "", "tone_user_requested": "", "unsupported": []}

        输入 plan（上一版定下的计划，用户看到的就是这两份）：
            {"slots": [{"number": 1, "user_words": "给 Dana 写封邮件强调 MOQ 500；Dana 那封再提一下固件",
                        "form": "email", "address": "dana@acme-audio.example", ..., "sources": [0, 2]},
                       {"number": 2, "user_words": "再给 Sam 写条附言", "form": "linkedin_note", ...,
                        "sources": [1]}],
             ...}
            [0] 和 [2] 都是「写给 Dana、邮件、dana@」，合成了第 1 份；[1] 是第 2 份；
            [3] 是模型违反 prompt 多补的：他说清了要写什么、没说份数，finalize_plan 第 5 步不替他
            多加一份，没要它，所以哪一份的 sources 里都没有。
            平时 AI 补的份都会进计划，只要是下面三种之一：他一份都没说（「写吧」）/ 他说的份数比说清的多
            （「写三份」只说清两份）/ 要改计划（上一版里 AI 补的照留）。会丢的只有这个例子这一种

        中间 number_of（ai_plan 下标 -> 第几份）：{0: 1, 2: 1, 1: 2}

        输出（四项，顺序照 ai_plan["drafts"]，不照正式plan的编号）：
            [{"plan_number": 1, "user_words": "给 Dana 写封邮件强调 MOQ 500",
              "recipient_user_requested": "Dana", "recipient_ai_understood": 2, ...,
              "reason_ai_planned": "Dana 本人说要找工厂做 800 台，…"},
             {"plan_number": 2, "user_words": "再给 Sam 写条附言", ...,
              "form_user_requested": "linkedin_note", ...},
             {"plan_number": 1, "user_words": "Dana 那封再提一下固件", ...,
              "emphasis_user_requested": ["固件"], ...},
             {"plan_number": None, "user_words": "", ..., "form_ai_planned": "article", ...}]
            除了 plan_number，每一项的十一格跟输入里那一项一模一样（上面用 ... 省掉的就是原样那几格）

        模型拿 plan_number 对用户说的编号：
            「第 2 份改成私信」  -> plan_number=2 的那一项，就是 [1]（给 Sam 的附言）
            「第 1 份改成私信」  -> plan_number=1 的两项，[0] 和 [2]，要一起改
        如果按数组的顺序数，「第 2 份」会数到 [1]、「第 3 份」会数到 [2]，可用户看到的计划里根本没有
        第 3 份，[2] 是第 1 份的一部分。所以必须按 plan_number 对，不按数组顺序数

    plan_number 的三种取值：
        1、2、…   这一项进了计划，是用户看到的第几份（合并过的几项是同一个号）
        None      这一项没进计划：AI 多补、第 5 步没要的（上面的 [3]），或者超了每轮上限被截掉的
                  （用户一次说了 5 份，第 5 份）。交给模型时变成 JSON 的 null，prompt 里讲了 null 是什么
    还没定过计划（ai_plan 是 {}，这一轮第一次进 plan_drafts）-> 返回 []，plan_drafts 据此认出不是在改计划

    用户说的「第 2 份」是计划里的编号，跟 ai_plan 里的顺序可能对不上（合并、截上限之后重新编过号），
    所以要靠 PlanSlot.sources 对回去。plan_number 放在每项最前面：模型读到一项先看到它是第几份。
    返回的是新字典（{**draft}），不改 state 里那份
    """
    if not ai_plan:
        return []
    # 输入   plan 每份的 sources，例 第 1 份 [0, 2]、第 2 份 [1]
    # 中间   number_of -> {0: 1, 2: 1, 1: 2}（ai_plan 下标 -> 第几份）；[3] 不在里面，get 出来是 None
    number_of = {source: slot["number"] for slot in plan["slots"] for source in slot["sources"]}
    # 输出   [{"plan_number": 1, **[0]}, {"plan_number": 2, **[1]}, {"plan_number": 1, **[2]},
    #         {"plan_number": None, **[3]}]
    #        每一项自己也带着 plan_number（模型上次填的），以这里按 sources 算的为准，所以先去掉它那一格
    return [
        {"plan_number": number_of.get(i), **{k: v for k, v in draft.items() if k != "plan_number"}}
        for i, draft in enumerate(ai_plan["drafts"])
    ]


def _slot_brief(slot: Mapping[str, Any]) -> str:
    """一份计划写成一句，给模型看那几份「上一版 drafts 里没有原样」的。

    输入 -> 输出（编的例子）：
        Sam 的加好友附言 -> "LinkedIn 加好友附言 → Sam Park；卖点 product_families；开场引 [2741]"
        发官网的邮件     -> "邮件 → 这家公司本身（1 号），发到 sales@acme-audio.example；卖点 moq、services；
                             开场不引帖子"
    """
    whom = slot["recipient"]["name"] if slot["recipient"] is not None else "这家公司本身（1 号）"
    parts = [f"{FORM_TEXT[slot['form']]} → {whom}" + (f"，发到 {slot['address']}" if slot["address"] else "")]
    if slot["emphasis"]:
        parts.append(f"要强调 {'、'.join(slot['emphasis'])}")
    parts.append(f"卖点 {'、'.join(slot['selling_points'])}")
    parts.append(f"开场引 [{slot['opening']}]" if slot["opening"] else "开场不引帖子")
    return "；".join(parts)


def _revision_text(
    feedback: str,
    previous: Sequence[Mapping[str, Any]],
    drafts: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> str:
    """要改计划时多给模型的那一段，用话讲清三件事：上一版你给的每一项各变成了他看到的计划里的哪一份、
    他看完计划说了什么、你要怎么改。

    输入：
        feedback   他对着计划说的那句，例 "第 2 份改成私信"
        previous   previous_drafts 的结果：上一版 ai_plan 的每一项，最前面多一个 plan_number，例（编的，
                   Acme Audio，跟 previous_drafts 那个例子同一份）
                   [{"plan_number": 1, "user_words": "给 Dana 写封邮件强调 MOQ 500", ...},
                    {"plan_number": 2, "user_words": "再给 Sam 写条附言", ...},
                    {"plan_number": 1, "user_words": "Dana 那封再提一下固件", ...},
                    {"plan_number": None, "user_words": "", ..., "form_ai_planned": "article", ...}]
    输出（真的那段就是下面这样，每行一行；[i] 后面那一串是那一项原样的 JSON，这里省成 {…}）：

        这一轮是在改上一版计划。下面按他看到的计划，一份一份列出你上一版 drafts 里的每一项。
        [i] 是这一项在你上一版 drafts 数组里的位置（从 0 数）；后面是这一项你上次填的原样，十一格都在。
        他说的「第 N 份」指的是他看到的计划里的第 N 份，按下面的分组对，不按数组的顺序数。

        他看到的第 1 份，是你上一版的 [0] 和 [2] 合成的（几项写给同一个对象、同一种写法、同一个地址，
        程序合成了一份）：
          [0] {"user_words": "给 Dana 写封邮件强调 MOQ 500", ...}
          [2] {"user_words": "Dana 那封再提一下固件", ...}
        他看到的第 2 份，就是你上一版的 [1]：
          [1] {"user_words": "再给 Sam 写条附言", ...}
        没进计划的（你多补的、程序没要；或者超了每轮上限被截掉的）：他没看到这几项
          [3] {"user_words": "", ..., "form_ai_planned": "article", ...}

        他看完这份计划说的：第 2 份改成私信

        请在上一版上改，交回完整的一轮 drafts（不是只交改了的那几项）：
        - 他点名的「第 N 份」：改上面第 N 份那一组里的项。那一份是几项合成的，几项都按他说的改
        - 他没点名的那几份：那一组里的每一项，十一格原样照抄上面的值，不要重新发挥
        - 他说不要的那一份：那一组里的项都不再交
        - 他要加的：接在最后，user_words 抄他修改里说它的那句（例「再加一篇讲小批量的文章」），不要留空
        - 没进计划的那几项：他没提就不再交。交了的话，程序会把它当成你新补的一份放进计划，
          他没要过的东西就冒出来了
        - 管整轮的话（语言、语气、「都强调 X」）：改到外面那几格，或者每一项里对应的格子

    为什么按份分组、不整段给一个 JSON 数组：他说的「第 N 份」是计划里的编号，跟数组顺序可能对不上
    （上面的 [2] 在数组里排第三，在计划里是第 1 份）。分好组、用话说清「第 1 份是哪几项合成的」，
    模型不用自己去对 plan_number。每一项的值仍然原样给出（一行一个 JSON）：没点名的要逐格照抄，
    转述成话会丢精度（原话差一个字，parse_ai_plan 就在对话里找不到它）。
    previous 是空的时候不该调它：build_messages 只在要改计划时拼这一段
    """
    # 输入   previous   中间   groups -> {1: [(0, {...}), (2, {...})], 2: [(1, {...})]}；
    #                          left_out -> [(3, {...})]（plan_number 是 None 的）
    #        每一项去掉 plan_number 再印：分组已经说了它是第几份，交回来的那份里也没有这一格
    #        看稿时刚说不要的那份还在上一版计划里，它的项不列（列在下面「已经不要的」里）
    status = {int(k): entry["status"] for k, entry in drafts.items()}
    groups: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    left_out: list[tuple[int, dict[str, Any]]] = []
    for i, item in enumerate(previous):
        draft = {key: value for key, value in item.items() if key != "plan_number"}
        if item["plan_number"] is None:
            left_out.append((i, draft))
        elif status.get(item["plan_number"]) != DROPPED:
            groups.setdefault(item["plan_number"], []).append((i, draft))

    lines = [
        "这一轮是在改上一版计划。下面按他看到的计划，一份一份列出你上一版 drafts 里的每一项。",
        "[i] 是这一项在你上一版 drafts 数组里的位置（从 0 数）；后面是这一项你上次填的原样，十一格都在。",
        "他说的「第 N 份」指的是他看到的计划里的第 N 份，按下面的分组对，不按数组的顺序数。",
        "",
    ]
    # 输出   一份一个标题，下面一项一行，例
    #        "他看到的第 1 份（已定稿，不能改，原样交回），就是你上一版的 [0]：" / "  [0] {...}"
    #        标题里的括号按账本里这一份的状态加（_STATUS_HINT），没有状态说明的不加
    for number in sorted(groups):
        members = groups[number]
        hint = _STATUS_HINT.get(drafts.get(str(number), {}).get("status", ""), "")
        if len(members) == 1:
            lines.append(f"他看到的第 {number} 份{hint}，就是你上一版的 [{members[0][0]}]：")
        else:
            names = " 和 ".join(f"[{i}]" for i, _draft in members)
            lines.append(
                f"他看到的第 {number} 份{hint}，是你上一版的 {names} 合成的"
                "（几项写给同一个对象、同一种写法、同一个地址，程序合成了一份）："
            )
        lines.extend(f"  [{i}] {json.dumps(draft, ensure_ascii=False)}" for i, draft in members)
    # 还活着、上一版 drafts 里却没有原样的（刚要回来的、定稿时你没交回来的）：按计划给，照着填一项
    for slot in plan["slots"]:
        number = slot["number"]
        if number in groups or status.get(number) == DROPPED:
            continue
        hint = _STATUS_HINT.get(status.get(number, ""), "")
        lines.append(
            f"他看到的第 {number} 份{hint}，你上一版的 drafts 里没有它的原样，它现在的计划是："
            f"{_slot_brief(slot)}。交回时照这个计划填一项，plan_number 填 {number}"
        )
    if left_out:
        lines.append("没进计划的（你多补的、程序没要；或者超了每轮上限被截掉的）：他没看到这几项")
        lines.extend(f"  [{i}] {json.dumps(draft, ensure_ascii=False)}" for i, draft in left_out)
    gone = sorted(int(k) for k, entry in drafts.items() if entry["status"] == DROPPED and entry["slot"])
    if gone:
        names = "、".join(f"第 {n} 份（{_slot_brief(drafts[str(n)]['slot'])}）" for n in gone)
        lines.append(
            f"已经不要的：{names}。他没说要回来就不要交这几个号；"
            "他要哪一份回来，就交一项、plan_number 填那个号，照它原来的计划填，再改上他说要改的"
        )

    lines += [
        "",
        f"他说的：\n{feedback}",
        "",
        "请在上一版上改，交回完整的一轮 drafts（不是只交改了的那几项），每一项填 plan_number：",
        "接着上面第 N 份的就填 N（那一份是几项合成的，几项都填 N），新加的填 0。",
        "- 他点名的「第 N 份」：改上面第 N 份那一组里的项。那一份是几项合成的，几项都按他说的改",
        "- 他没点名的那几份、标着「原样交回」的：每一项原样照抄上面的值，不要重新发挥",
        "- 他说不要的那一份：那一组里的项都不再交",
        "- 他要加的：接在最后，plan_number 填 0，user_words 抄他说它的那句（例「再加一篇讲小批量的文章」），"
        "不要留空",
        "- 没进计划的那几项：他没提就不再交。交了的话，程序会把它当成你新补的一份放进计划，"
        "他没要过的东西就冒出来了",
        "- 管整轮的话（语言、语气、「都强调 X」）：改到外面那几格，或者每一项里对应的格子",
    ]
    return "\n".join(lines)


# 改计划时，每一份标题后面按账本状态加的那句。没列的状态（planned / written）不加
_STATUS_HINT = {
    APPROVED: "（已定稿，不能改，原样交回）",
    REWRITE: "（他只要改正文，计划原样交回）",
    REPLAN: "（他要改这一份的计划）",
}


def untouched_changes(
    previous: Sequence[Mapping[str, Any]],
    ai_plan: Mapping[str, Any],
    feedback: str,
    drafts: Mapping[str, Any],
) -> list[str]:
    """他没点名的那几份，这一版有没有被模型改掉。只给日志用，不拦：代码判断不了他到底点没点名。

    输入：previous（上一版，previous_drafts 的结果）；ai_plan（这一版，摊平的）；feedback（他的修改意见）；
          drafts（账本：看稿时已经说了不要的那份没交回来是对的，不提醒）
    输出：一份一句，例 ["第 1 份他没点名，这一版却找不到原样的那一项（给 Dana 写封邮件）"]；都没变是 []

    「点名」只认「第 N 份」这种说法（N 是阿拉伯数字或一到九）；「都…」「整体…」这类管整轮的话
    会让每一份都合理地变，那时这里的提醒是误报，所以只打日志。
    「原样」看四格：原话、收件人编号、邮箱号、他说的写法。AI 补的那几项（没有原话）不查
    """
    squashed = _squash(feedback)
    kept = {
        (d["user_words"], d["recipient_ai_understood"], d["email_ai_understood"], d["form_user_requested"])
        for d in ai_plan["drafts"]
    }
    notes = []
    for old in previous:
        number = old["plan_number"]
        if number is None or not old["user_words"] or drafts.get(str(number), {}).get("status") == DROPPED:
            continue
        named = {f"第{number}份", f"第{_CHINESE_DIGITS.get(number, number)}份"}
        if any(name in squashed for name in named):
            continue
        key = (
            old["user_words"],
            old["recipient_ai_understood"],
            old["email_ai_understood"],
            old["form_user_requested"],
        )
        if key not in kept:
            notes.append(f"第 {number} 份他没点名，这一版却找不到原样的那一项（{old['user_words']}）")
    return notes


# 「第二份」里的二。一轮最多 4 份，到九足够
_CHINESE_DIGITS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}


def _clean(values: Sequence[Any]) -> tuple[str, ...]:
    """列表字段去空白、扔空串。例：["MOQ 500", " ", ""] -> ("MOQ 500",)"""
    return tuple(str(s).strip() for s in values if str(s).strip())


def _squash(text: str) -> str:
    """只留字母、数字、汉字，全转小写。拿来比「这句原话在不在对话里」时不被空格、标点、大小写绊倒。

    输入 -> 输出：
        "给 Dana 写封邮件，强调 MOQ 500"  -> "给dana写封邮件强调moq500"
        "给Dana写封邮件 强调MOQ500。"     -> "给dana写封邮件强调moq500"     两句比出来一样
    """
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _user_text(lines: Sequence[str]) -> str:
    """对话里用户说过的全部话，压扁拼成一串（_squash 过）。AI 说的不算。

    输入：["AI: 请问你想给 Acme Audio 写什么…", "用户: 给 Dana 写封邮件", "用户: 中文"]
    输出："给dana写封邮件中文"
    """
    return "".join(_squash(line.removeprefix("用户: ")) for line in lines if line.startswith("用户: "))


@dataclass(frozen=True)
class _Allowed:
    """查一份稿合不合法时要对照的那几样，都是从 lead 和 knowledge 现算的。

    例（North Field Technologies）：
        _Allowed(target_count=4, email_labels={"1-1", "3-1"},
                 selling_points=("moq", "categories", ...), signal_ids={2720, 2733}, said="给3号写封邮件")

    target_count 只数对象，不数邮箱：这家有几个官网邮箱都只占 1 号，邮箱另有自己的号
    """

    target_count: int
    email_labels: Collection[str]
    selling_points: Collection[str]
    signal_ids: Collection[int]
    # 这一轮用户说过的全部话，压扁拼成一串（_user_text）。AI 说的（第一问、计划）不在里面。
    # 要改计划时：多了他的修改意见，例
    #   "给3号写封邮件强调moq500再给2号写条私信第2份改成加好友附言再加一篇讲小批量的文章"。
    # 计划里我们写的字（「语言：英文」「卖点：起订量低」）不在里面，模型从计划里抄的东西在这里找不到
    said: str
    # 这一次是不是在改上一版计划（read_ai_plan 看 previous 空不空定的）。
    # 管两件事：两格写法都填了时信哪一格（见 _parse_draft 里「改计划时写法填错了半边」那段）；
    # plan_number 认不认（第一次定计划一律当 0）
    revising: bool
    # 改计划时 plan_number 能填的号：上一版计划里还在的那几份，例 {1, 3, 4}。第一次定计划是空的
    numbers: Collection[int]


def _is_int(value: Any) -> bool:
    """是不是一个真的整数。bool 是 int 的子类，True 会被当成 1，单独挡掉"""
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_draft(item: Mapping[str, Any], number: int, allowed: _Allowed) -> AiPlanDraft:
    """模型返回里的一份稿变成 AiPlanDraft，两半各查一遍。parse_ai_plan 调它，number 只用来写报错。

    输入：
        item      {"user_words": "给 Dana 写封邮件", "recipient_user_requested": "Dana",
                   "recipient_ai_understood": 2, "email_user_requested": "", "email_ai_understood": "",
                   "form_user_requested": "email", "emphasis_user_requested": [],
                   "form_ai_planned": "", "selling_points_ai_planned": ["moq"],
                   "opening_ai_planned": 2720, "reason_ai_planned": "…"}
        number    这是第几份，例 1
        allowed   对照的那几样，见 _Allowed
    输出：AiPlanDraft(user_words="给 Dana 写封邮件", ..., selling_points_ai_planned=("moq",), ...)

    不合法当场抛 AiPlanCallFailed，不兜底。用户那一半（user_words 不是空的才查）：
        recipient_ai_understood 出了名单的范围     模型编了一个不存在的人。当成「没说」的话，
                                                  用户以为写给了他点的那位
        user_words 在用户说过的话里找不到          这一份多半是模型自己编出来的（用户没提过的一份）
        email_user_requested 在用户说过的话里找不到
                                                  模型自己填了一个地址（多半是从卡片上抄的）
        email_ai_understood 卡片上没有这个号，或者填了号却没抄用户说的原话
                                                  模型编了一个邮箱，或者用户根本没提邮箱
    AI 那一半（每一份都查）：
        写哪一种用户没说、也没说邮箱，AI 却没定     这一份不知道写什么
        selling_points_ai_planned 是空的，或者有 facts.json 里没有的键
                                                  卖点是编的，写稿时就会编我们的本事
        opening_ai_planned 不是 0 也不是这家的证据编号
                                                  开场会引一条不存在的帖子

    AI 补的那一份（user_words 是 ""）用户那一半本该全空；模型填了也不看，一律当没说（写给这家公司），
    打一行日志：这一份反正写给公司，不会写错人

    要改计划时：三种情况多起来，都不报错，各打一行日志：
        写法填错了半边（新写法在 form_ai_planned，     当作他换了写法，挪进 form_user_requested
        form_user_requested 还是原来的）
        要强调的点是从计划里抄的（「起订量低（MOQ）」）    在他说过的话里找不到，扔掉那几条
        他在修改里新加的那份 user_words 留成了空串         这一份会被当成 AI 补的，finalize_plan 会丢掉它；
                                                           这里认不出来（代码分不清哪份是他要加的），
                                                           只能靠 prompt 讲明，见 prompts.AI_PLAN_SYSTEM
    """
    # 接着第几份；0 = 新的
    plan_number = item["plan_number"]
    # 用户原话
    user_words = str(item["user_words"]).strip()
    # ai理解的收件人
    pick = item["recipient_ai_understood"]
    # 用户要求的收件人
    recipient_words = str(item["recipient_user_requested"]).strip()
    # 用户要求的email
    email = str(item["email_user_requested"]).strip()
    # ai理解的email
    email_pick = str(item["email_ai_understood"]).strip()
    # 用户要求的form
    form = str(item["form_user_requested"]).strip()
    # 用户要求的重点tuple
    emphasis = _clean(item["emphasis_user_requested"])
    # ai理解的form
    form_ai = str(item["form_ai_planned"]).strip()
    # facts.json里的卖点tuple
    selling_points = tuple(dict.fromkeys(_clean(item["selling_points_ai_planned"])))
    # ai开场白想引用的某一个signal id
    opening = item["opening_ai_planned"]

    # 号只能是 0 或上一版还在的那几份。不在的号（已经不要的、编的）当场报错：
    # 当成新的一份的话，用户说不要的那份会换个号又回来
    if not _is_int(plan_number) or plan_number < 0:
        raise AiPlanCallFailed(f"第 {number} 份的 plan_number 返回了 {plan_number!r}，只能是 0 或计划里的号")
    if plan_number and not allowed.revising:
        print(f"[解析] 第一次定计划，第 {number} 份却填了 plan_number={plan_number}，当作新的一份")
        plan_number = 0
    if plan_number and plan_number not in allowed.numbers:
        raise AiPlanCallFailed(
            f"第 {number} 份的 plan_number 返回了 {plan_number}，上一版计划里只有 {sorted(allowed.numbers)}"
        )

    # 检查 用户要求的和ai理解的两种form 是否是在 email，Linkedin note，linkedin message，article 之一
    # （两半各一格）
    if form not in _FORM_VALUES or form_ai not in _FORM_VALUES:
        raise AiPlanCallFailed(f"第 {number} 份的写法返回了 {form!r} / {form_ai!r}，只认 {_FORM_VALUES}")
    # llm 挑选的 recipient 的序号必须是在 -1（没有对应到任何收件人）
    # 和 target_count（这家公司本身 + 该 lead 所有的 contacts 人数）之间
    if not _is_int(pick) or not -1 <= pick <= allowed.target_count:
        raise AiPlanCallFailed(
            f"第 {number} 份的 recipient_ai_understood 返回了 {pick!r}，名单只有 {allowed.target_count} 号"
            "（0 没说，-1 对不上）"
        )

    # 如果该plan draft里ai没有如实记录用户原话，那要么说明该draft质量存疑，
    # 要么就是用户只说了一句类似“写吧”，未指明要“写一份”
    if not user_words:
        # 无论哪种情况，以下几种涉及到用户原话的数值都不该有，因为原话为空
        if recipient_words or pick or email or email_pick or form or emphasis:
            print(f"[解析] 第 {number} 份是 AI 补的，却填了用户说的那几格，不看，当没说")
        # 不无论哪种情况，以下数值都该为空
        recipient_words, pick, email, email_pick, form, emphasis = "", 0, "", "", "", ()

    # 若有用户原话
    else:
        # 检查 llm 是在 user_words 里如实保留了用户说的原话，即，检查 user_words 是否被包含在 said 里。
        if _squash(user_words) not in allowed.said:
            raise AiPlanCallFailed(
                f"第 {number} 份的 user_words {user_words!r} 在用户说过的话里找不到，这一份可能是模型编的"
            )
        # 邮箱同一条规矩：必须是用户说过的
        if email and _squash(email) not in allowed.said:
            raise AiPlanCallFailed(f"第 {number} 份的 email_user_requested {email!r} 用户没说过")
        # 邮箱号只能是该lead所有邮箱范围之内，
        # 或者是"-1"(ai判断出不符合任何一个现有邮箱)，
        # 或者是""(用户没提)
        if email_pick not in ("", "-1", *allowed.email_labels):
            raise AiPlanCallFailed(
                f"第 {number} 份的 email_ai_understood 返回了 {email_pick!r}，"
                f"卡片上的邮箱号只有 {sorted(allowed.email_labels)}"
            )
        if email_pick and not email:
            raise AiPlanCallFailed(f"第 {number} 份选了邮箱 {email_pick!r}，却没抄用户说邮箱的原话")
        # 填了 -1（说了但认不出）却没抄他说的是谁：没有原话就没法在计划里说「你说的「…」没认出」，
        # 当作没说写给谁
        if pick == -1 and not recipient_words:
            print(f"[解析] 第 {number} 份的 recipient_ai_understood 是 -1 却没有原话，当作没说写给谁")
            pick = 0
        # 反过来：抄了他说的是谁（例「Mike」），编号却填 0（没说）。他明明说了，
        # 填 0 是模型没对上又没按规矩填 -1。当作认不出：计划里要点出「你说的「Mike」名单上没认出」，
        # 而不是说成「没说写给谁」
        if pick == 0 and recipient_words:
            print(f"[解析] 第 {number} 份说了写给「{recipient_words}」，编号却是 0，当作名单上认不出（-1）")
            pick = -1
        # 要强调的点也只能是他说过的：对话里有一版计划时，模型可能把计划里的「卖点：起订量低」抄成他说的。
        # 找不到的那几条扔掉，不报错（强调点错了只影响稿子多提一句，不会写错人）
        heard_emphasis = tuple(e for e in emphasis if _squash(e) in allowed.said)
        if heard_emphasis != emphasis:
            dropped = [e for e in emphasis if e not in heard_emphasis]
            print(f"[解析] 第 {number} 份要强调的 {dropped} 在用户说过的话里找不到，不算他说的")
            emphasis = heard_emphasis

    # 改计划时写法填错了半边：他在修改里换了写法（例「第 1 份改成私信」），模型却把新写法填进
    # form_ai_planned、form_user_requested 还留着他原来说的。照 prompt，他说了写法时 AI 那格必须留空，
    # 而 AI 不会去推翻他自己点名的写法，所以两格都填了、又不一样，只可能是他换了写法：挪进用户那一半。
    #   输入   form="email"、form_ai="linkedin_message"（Slack 实测 gpt-4.1 真这样填过）
    #   输出   form="linkedin_message"、form_ai=""，计划里也不会标成「写哪一种是我定的」
    # 第一次定计划时同样的两格不挪：那是模型违反 prompt 自己出主意，信他说的
    if allowed.revising and user_words and form and form_ai and form_ai != form:
        print(
            f"[解析] 第 {number} 份改计划时写法填在了 AI 那一半（{form} / {form_ai}），当作他改成了 {form_ai}"
        )
        form, form_ai = form_ai, ""

    # 如果用户没说写法、也没提邮箱（提了邮箱就是写邮件），AI 必须定一种
    if not form and not email and not form_ai:
        raise AiPlanCallFailed(f"第 {number} 份用户没说写哪一种，form_ai_planned 却是空的")
    # ai必须建议卖点
    if not selling_points:
        raise AiPlanCallFailed(f"第 {number} 份一个卖点都没挑")
    # ai建议的卖点必须基于facts.json
    unknown = [key for key in selling_points if key not in allowed.selling_points]
    if unknown:
        raise AiPlanCallFailed(
            f"第 {number} 份的卖点 {unknown} 不是 facts.json 的键，只认 {list(allowed.selling_points)}"
        )
    # 开头句signal id检查
    if not _is_int(opening) or (opening != 0 and opening not in allowed.signal_ids):
        raise AiPlanCallFailed(
            f"第 {number} 份的 opening_ai_planned 返回了 {opening!r}，"
            f"这家的证据只有 {sorted(allowed.signal_ids)}（0 = 不引）"
        )

    # 包装成 AiPlanDraft
    return AiPlanDraft(
        plan_number=plan_number,
        user_words=user_words,
        recipient_user_requested=recipient_words,
        recipient_ai_understood=pick,
        email_user_requested=email,
        email_ai_understood=email_pick,
        form_user_requested=form,
        emphasis_user_requested=emphasis,
        form_ai_planned=form_ai,
        selling_points_ai_planned=selling_points,
        opening_ai_planned=opening,
        reason_ai_planned=str(item["reason_ai_planned"]).strip(),
    )


def parse_ai_plan(
    payload: Mapping[str, Any],
    lines: Sequence[str],
    lead: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    *,
    revising: bool,
    numbers: Collection[int] = (),
) -> AiPlan:
    """把模型返回的那份 JSON 变成 AiPlan，每份稿都查一遍合不合法。

    输入：
        payload     json.loads 出来的字典，例
                    {"count_user_requested": 0, "count_user_words": "",
                     "drafts": [{"user_words": "给 Dana 写封邮件", ..., "selling_points_ai_planned": ["moq"],
                                 "opening_ai_planned": 2720, "reason_ai_planned": "…"}],
                     "language_user_requested": "", "tone_user_requested": "", "unsupported": []}
        lines       这一轮的 messages（messages[round_start:]），拿来核对每份的 user_words 真出自用户。
                    要改计划时：里面多了计划（AI 说的，不算）和他的修改意见（算），
                    例 ["用户: 给 3 号写封邮件…", "AI: 好，给 North Field Technologies 写这 2 份：…",
                        "用户: 第 2 份改成加好友附言，再加一篇讲小批量的文章"]
        lead        state["lead"]，名单和邮箱的号从这里现算
        knowledge   state["knowledge"]，卖点的键和证据的编号从这里现算
        revising    这一次是不是在改上一版计划。纠正「写法填错了半边」、认不认 plan_number，见 _parse_draft
        numbers     改计划时 plan_number 能填的号（上一版计划里还在的几份）；第一次定计划不传
    输出：AiPlan(count_user_requested=0, count_user_words="", drafts=(AiPlanDraft(...),), ...)
          用户说的那几份排在前面（按模型给的顺序），AI 补的接在后面

    不合法的情况全部抛 AiPlanCallFailed，不兜底：每份稿的见 _parse_draft；
    count_user_requested 是负数；用户一份都没说清、AI 也一份都没补；
    缺键（strict schema 下不该发生，发生了说明 schema 和这里漂了）。
    份数的原话在对话里找不到不抛，当作没说份数（份数错了只影响补不补，不会写错人）；
    语言、语气、每份要强调的点在对话里找不到也不抛，当作没说。
    这里只查「模型答得对不对」；份数对不上、超过上限这类「用户说得清不清」的，是 finalize_plan 的事
    """
    # 从该公司的lead+knowledge里计算出来硬性限制，包成_Allowed
    allowed = _Allowed(
        # 最多可联系的人数，公司不管有几个官网都只算1个
        target_count=1 + len(listed_people(lead)),
        # 所有邮箱编号set，官网1：1-1, 1-2，Abby: 2-1, 2-2...
        email_labels={number for number, _owner, _email in email_numbers(lead)},
        # fact.json里解析出来的卖点tuple
        selling_points=selling_point_keys(knowledge),
        # 该lead关联的全部 signal ids set
        signal_ids={e["signal_id"] for e in knowledge["evidence"]},
        # state["messages"]里“用户”说过话的string
        said=_user_text(lines),
        revising=revising,
        numbers=frozenset(numbers),
    )
    try:
        # 拆解llm返回的payload
        count = payload["count_user_requested"]
        count_words = str(payload["count_user_words"]).strip()
        raw_drafts = payload["drafts"]
        language = str(payload["language_user_requested"]).strip()
        tone = str(payload["tone_user_requested"]).strip()
        unsupported = _clean(payload["unsupported"])
        # 对每一份 llm 给出的 draft，检查格式是否合法，并且把每份 raw draft 包成 AiPlanDraft 的形式
        drafts = [_parse_draft(item, number, allowed) for number, item in enumerate(raw_drafts, start=1)]
    except KeyError as err:
        raise AiPlanCallFailed(
            f"返回里少了 {err.args[0]!r} 这个键。strict schema 下不该发生，多半是 AI_PLAN_SCHEMA 和 "
            f"parse_ai_plan 漂了。整份回答：{json.dumps(payload, ensure_ascii=False, default=str)[:300]}"
        ) from err
    # 检查 count 是否是数字，注意，有可能 count 为 0，但 drafts 不为空
    if not _is_int(count) or count < 0:
        raise AiPlanCallFailed(f"count_user_requested 返回了 {count!r}，只能是 0 或正整数")
    # 份数的原话找不到：这个数多半是模型自己数出来的，当作没说
    if count and (not count_words or _squash(count_words) not in allowed.said):
        print(f"[解析] 份数 {count} 的原话 {count_words!r} 在用户说过的话里找不到，当作没说份数")
        count, count_words = 0, ""
    # 语言、语气的原话同一条规矩：找不到就当没说（用默认值，计划里会写「你没说，按默认」）。
    # 对话里有一版计划时，模型会把计划里的「语言：英文」「语气：简短、专业…」抄成他说的，
    # 计划上那两句「按默认」的说明就没了，用户以为是他自己要求的
    if language and _squash(language) not in allowed.said:
        print(f"[解析] 语言 {language!r} 在用户说过的话里找不到，当作没说")
        language = ""
    if tone and _squash(tone) not in allowed.said:
        print(f"[解析] 语气 {tone!r} 在用户说过的话里找不到，当作没说")
        tone = ""

    # 用户说的排前面，AI 补的接后面（各自保持模型给的顺序）
    heard = [d for d in drafts if d.user_words]
    added = [d for d in drafts if not d.user_words]
    # 用户什么都没说清（「写吧」「看你」），ai却一份都没补：这一轮没东西可写
    if not heard and not count and not added:
        raise AiPlanCallFailed("用户一份都没说清，AI 也一份都没补")

    # 把 plan_drafts 里 llm 的返回值包装成 AiPlan
    return AiPlan(
        count_user_requested=count,
        count_user_words=count_words if count else "",
        drafts=tuple(heard + added),
        language_user_requested=language,
        tone_user_requested=tone,
        unsupported=unsupported,
    )


def read_ai_plan(
    client: Any,
    lines: Sequence[str],
    lead: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    *,
    feedback: str,
    previous: Sequence[Mapping[str, Any]],
    drafts: Mapping[str, Any],
    plan: Mapping[str, Any],
    config: PlanConfig,
) -> AiPlan:
    """调一次模型，把这条对话读成一份 AiPlan（用户说的 + AI 补齐的）。这一步花钱（很少）。

    输入：
        client      一个能 chat 的对象。真跑时是 openai.OpenAI，测试里是照剧本回答的假客户端
        lines       这一轮的 messages（messages[round_start:]）
        lead        state["lead"]，名单从这里来
        knowledge   state["knowledge"]，这家的情况、证据、facts.json
        feedback    replan时他说的那句修改意见；第一次定计划是 ""
        previous    replan时的上一版（previous_drafts 标好了第几份）；第一次定计划是 []
        drafts      账本，见 build_messages
        plan        上一版计划，见 build_messages；第一次定计划是 {}
        config      模型、max_tokens、温度、超时
        feedback / previous 两个不给默认值：漏传的话，改计划时模型只能从对话里自己找修改意见和上一版
    输出：AiPlan，例子见那个类

    对库的写：一笔都没有，写 state 的是调它的节点。几种拿不到好回答的情况全部抛 AiPlanCallFailed，
    怎么认的跟 query/intent.read_intent 一样
    """
    # 拼在 try 外面：build_messages 抛的异常是我们的 bug，不该被报成「请求失败」
    # feedback，和上一版的信息，主要是build_messages来完成
    messages = build_messages(
        lines, lead, knowledge, feedback=feedback, previous=previous, drafts=drafts, plan=plan
    )
    try:
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": AI_PLAN_SCHEMA},
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout_seconds,
        )
    except Exception as err:
        raise AiPlanCallFailed(f"请求失败：{type(err).__name__}: {err}") from err

    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise AiPlanCallFailed(
            f"输出被 max_tokens={config.max_tokens} 截断，调 config/drafting.yaml 的 plan.max_tokens"
        )
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise AiPlanCallFailed(f"模型拒答：{refusal}")
    content = choice.message.content
    if content is None:
        raise AiPlanCallFailed(f"模型没有返回内容，finish_reason={choice.finish_reason}")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as err:
        raise AiPlanCallFailed(f"返回的不是合法 JSON：{err}。原文前 200 字：{content[:200]}") from err
    if not isinstance(payload, dict):
        raise AiPlanCallFailed(f"返回的 JSON 不是一个对象，是 {type(payload).__name__}：{content[:200]}")
    # 检查llm的返回+parse成AiPlan
    # previous 不空就是在改计划
    # plan_number 能填的号：上一版还活着的每一份，加上不要了、可以要回来的（账本里存着它的计划）
    numbers = {slot["number"] for slot in plan.get("slots", [])} | {
        int(k) for k, entry in drafts.items() if entry["status"] == DROPPED and entry["slot"]
    }
    return parse_ai_plan(payload, lines, lead, knowledge, revising=bool(previous), numbers=numbers)


def ai_plan_to_state(ai_plan: AiPlan) -> dict[str, Any]:
    """把 AiPlan 摊成能进 state 的普通字典，元组转 list。理由同 query/intent.to_state_dict。

    输入 -> 输出（编的例子）：
        AiPlan(count_user_requested=0, count_user_words="",
                     drafts=(AiPlanDraft(user_words="给 Dana 写封邮件", recipient_user_requested="Dana",
                                          recipient_ai_understood=2, email_user_requested="",
                                          email_ai_understood="", form_user_requested="email",
                                          emphasis_user_requested=("MOQ 500",), form_ai_planned="",
                                          selling_points_ai_planned=("moq",), opening_ai_planned=2720,
                                          reason_ai_planned="…"),),
                     language_user_requested="", tone_user_requested="", unsupported=())
            -> {"count_user_requested": 0, "count_user_words": "",
                "drafts": [{"user_words": "给 Dana 写封邮件", "recipient_user_requested": "Dana",
                            "recipient_ai_understood": 2, "email_user_requested": "",
                            "email_ai_understood": "", "form_user_requested": "email",
                            "emphasis_user_requested": ["MOQ 500"], "form_ai_planned": "",
                            "selling_points_ai_planned": ["moq"], "opening_ai_planned": 2720,
                            "reason_ai_planned": "…"}],
                "language_user_requested": "", "tone_user_requested": "", "unsupported": []}
    """
    return {
        "count_user_requested": ai_plan.count_user_requested,
        "count_user_words": ai_plan.count_user_words,
        "drafts": [
            {
                "plan_number": d.plan_number,
                "user_words": d.user_words,
                "recipient_user_requested": d.recipient_user_requested,
                "recipient_ai_understood": d.recipient_ai_understood,
                "email_user_requested": d.email_user_requested,
                "email_ai_understood": d.email_ai_understood,
                "form_user_requested": d.form_user_requested,
                "emphasis_user_requested": list(d.emphasis_user_requested),
                "form_ai_planned": d.form_ai_planned,
                "selling_points_ai_planned": list(d.selling_points_ai_planned),
                "opening_ai_planned": d.opening_ai_planned,
                "reason_ai_planned": d.reason_ai_planned,
            }
            for d in ai_plan.drafts
        ],
        "language_user_requested": ai_plan.language_user_requested,
        "tone_user_requested": ai_plan.tone_user_requested,
        "unsupported": list(ai_plan.unsupported),
    }


@dataclass(frozen=True)
class PlanSlot:
    """这一轮的一份稿怎么写：写给谁、写哪一种、发到哪、推哪几样、开场引哪条。

    一个例子（编的，Acme Audio，用户说「给 Dana 写封邮件，强调 MOQ 500」，Dana 有自己的邮箱）：

        PlanSlot(number=1, plan_version=1, user_words="给 Dana 写封邮件，强调 MOQ 500",
                  recipient={"name": "Dana Lee", "relation": "member", "position": "Founder", ...},
                  recipient_user_requested="Dana", email_user_requested="",
                  form="email", address="dana@acme-audio.example", emphasis=("MOQ 500",),
                  selling_points=("moq",), opening=2720, reason="Dana 本人说要找工厂做 800 台…",
                  ai_decides=(), notes=(), sources=(0,))

    对照用户说「给 Mike 写一份」、名单里没有 Mike（写给这家公司，写哪一种 AI 定了私信）：

        PlanSlot(number=1, ..., recipient=None, recipient_user_requested="Mike", form="linkedin_message",
                  address=None, ai_decides=("form",),
                  notes=("你说的「Mike」名单上没认出，这份先写给 Acme Audio 这家公司本身（称呼他们的团队）；"
                         "是名单上哪一号告诉我就改",
                         "LinkedIn 消息只能发给一个人或公司主页，称呼写这家公司，发给谁由你定"),
                  sources=(0,))

    user_words 是 "" 的那种是 AI 补上的份（用户说「写吧」，或者说要三份、只说清两份）
    """

    # 这一份的身份证号。第一次出现时发，之后永不改、永不复用（不要了的号空着，新的从账本最大号 + 1 起）
    number: int
    # 这一份的计划改过几次，从 1 起。finalize_plan 比较前后（_plan_key）决定要不要 +1
    plan_version: int
    # 用户原话
    user_words: str
    # 最终计划的收件人，是 state["lead"]["people"] 里的一个；
    # None代表写给这家公司本身（1 号），发生在以下情况：
    # 用户点了 1 号、没说写给谁、说了但名单上认不出、文章、AI 补的份
    recipient: dict[str, Any] | None
    # 用户要求的收件人
    recipient_user_requested: str
    # 用户要求的邮箱
    email_user_requested: str
    # 最终计划写成哪一种form，可能是用户说的，或者用户选了邮箱（写邮件），
    # 或者 AI 定的（ai_decides 里有 "form"）
    form: str
    # 写邮件时发到哪个地址；不是邮件、这家没有邮箱的是 None
    address: str | None
    # 用户说要强调的（原话）
    emphasis: tuple[str, ...]
    # AI 挑的卖点（facts.json 的键）、开场引的证据（signal_id，0 = 不引）、为什么这样定
    selling_points: tuple[str, ...]
    # ai挑的hook signal id
    opening: int
    # ai为何这样制定计划
    reason: str
    # 用户提出的要求里不完整的地方里，哪些AI 替他定的，现在只有 "form"
    # 卖点、开场一直是 AI 定的，无论用户有没有提出要求
    ai_decides: tuple[str, ...]
    # 要让用户知道的，每条一句，例「Sam Park 不是这家公司的人（…），照样给他写」
    notes: tuple[str, ...]
    # 这一份正式的draft plan是出自 ai初步解读的Plan里的/ai_plan["drafts"] 的哪几项（下标，从 0 数）。
    # 用户看到的「第 N 份」是这里的 number，跟 ai_plan 里的顺序可能对不上（合并过、号是固定的）。
    # 例（编的，Acme Audio）。ai_plan["drafts"] 有四项：
    #     [0] 给 Dana 写封邮件强调 MOQ      [1] 再给 Sam 写条附言
    #     [2] Dana 那封再提一下固件          [3] AI 补的一篇文章（用户说了「写三份」）
    #   [0] 和 [2] 都是「写给 Dana、邮件、dana@」，合成一份，因此正式Plan里是：
    #     第1份planslot：sources=(0, 2)
    #     第2份planslot：sources=(1,)
    #     第3份planslot：sources=(3,)
    #   当replan时，用户说「第 2 份改成私信」，
    # 这里的第2份指的是用户所看到的正式Plan里的第二份，也是ai_plan 里的 [1]
    # 要改计划时：previous_drafts 靠它给上一版每一项标上 plan_number（[0] 和 [2] 都标 1），
    # 模型才知道「第 2 份」是哪一项
    sources: tuple[int, ...]


@dataclass(frozen=True)
class Plan:
    """这一轮怎么写：每份的 PlanSlot，加上所有份共用的语言、语气。

    finalize_plan 算出来，plan_drafts 当场摊成字典存进 state["plan"]（plan_to_state）。
    存下来、不每次现算：用户在计划那一停看到的、以后 write_draft 照着写的，必须是同一份；
    现算的话，两次之间改过规则或配置（例如默认语气），写出来的就跟用户点头的那份对不上。

    一个例子（编的，Acme Audio，用户说「给 Dana 写封邮件，强调 MOQ 500，再给 Sam 发条加好友附言」）：

        Plan(slots=(PlanSlot(number=1, ..., form="email", ...),
                          PlanSlot(number=2, ..., form="linkedin_note", ...)),
                   language="英文", tone="简短、专业，不用推销腔", unsupported=(),
                   notes=("语言你没说，按默认用英文", "语气你没说，按默认：简短、专业，不用推销腔"),
                   ai_plans_all=False)

    ai_plans_all=True 表示用户一份都没说（「写吧」「看你」），slots 全是 AI 补的

    要改计划时：state["plan"] 整份换成新的一版（LastValue），旧的那版不另存。下一次再改时，
    previous_drafts 读的是 state["ai_plan"] 和 state["plan"] 里的「上一版」，也就是用户刚看过的那一版；
    更早的几版只剩 messages 里那几段计划文字。
    ai_plans_all 也按这一版重算：他第一句说的是「写吧」、后来又点名改了某一份，那一份就有了原话，
    这一版 ai_plans_all 是 False。AI 补的另外几份照样留着（finalize_plan 第 5 步「要改计划」那种）
    """

    # 最终决定每一份draft的计划。只放还在的份（含已定稿的），按号从小到大；
    # 不要了的份不在这里，它的号和状态在账本（state["drafts"]）里
    slots: tuple[PlanSlot, ...]
    # 最终决定的语言
    language: str
    # 最终决定的语气
    tone: str
    # 最终决定的不能支持的事
    unsupported: tuple[str, ...]
    # 整轮的说明：用了哪些默认、合并了哪几份、超了上限怎么处理的、份数怎么处理的
    notes: tuple[str, ...]
    # 最终的Plan是否每份draft都是ai计划的
    ai_plans_all: bool


def _pick_address(
    recipient: Mapping[str, Any] | None, emails: Sequence[Mapping[str, Any]]
) -> tuple[str, str]:
    """写邮件、用户没说地址时发到哪个，和一句说明（没换就是空串）。emails 不为空时才调。

    输入 -> 输出（编的例子。Acme Audio 两个邮箱：官网上的 hello@acme-audio.example、
    Dana 的 dana@acme-audio.example）：
        收件人 Dana Lee                  -> ("dana@acme-audio.example", "")            他自己的
        收件人 Sam Park                  -> ("hello@acme-audio.example",
                                             "Sam Park 没有找到邮箱，发到官网上的公开邮箱 "
                                             "hello@acme-audio.example")
        收件人 None（写给这家公司）      -> ("hello@acme-audio.example", "")           称呼团队，发官网那个

    顺序：收件人自己的 -> 官网上公开的 -> 剩下的第一个。官网的排在别人的前面：发到公司的公开邮箱，
    比发到另一个人的私人邮箱更不容易发错
    """
    if recipient is not None:
        own = [e for e in emails if e["person_name"] == recipient["name"]]
        if own:
            return own[0]["value"], ""
    site = [e for e in emails if e["source"] == ContactSource.WEBSITE.value]
    address = (site or list(emails))[0]["value"]
    if recipient is None:
        if site:
            return address, ""
        return address, f"这家没有官网上的公开邮箱，发到卡片上的 {address}"
    where = "官网上的公开邮箱" if site else "卡片上的另一个邮箱"
    return address, f"{recipient['name']} 没有找到邮箱，发到{where} {address}"


def _looks_like_address(said: str) -> bool:
    """用户说的邮箱是一个地址，还是一种说法。例："mike@acme-audio.example" -> True；「sales 那个」-> False"""
    return "@" in said


def _finalize_slot(number: int, draft: Mapping[str, Any], lead: Mapping[str, Any], source: int) -> PlanSlot:
    """一份稿：模型给的（用户说的 + AI 定的）+ 卡片上有的，算出写给谁、写哪一种、发到哪。纯函数。

    输入：
        number      第几份
        draft       ai_plan["drafts"] 里的一份（ai_plan_to_state 摊平的那种）
        lead        state["lead"]
        source      ai_plan的idx
    输出：PlanSlot，例子见那个类

    四步，按顺序：用户选了哪个邮箱 -> 写哪一种 -> 写给谁 -> 发到哪个邮箱。
    每一步的几种情况列在那一步的注释里
    """
    # db里该公司所有的联系人，和联系人邮箱
    people, emails = listed_people(lead), lead["emails"]
    # Db里该公司的名字
    name = lead["display_name"]
    # 记录原因
    notes: list[str] = []

    # 一、确定用户选择收件邮箱
    # 输入   email_ai_understood -> "1-2"
    # 输出   chosen -> (1, {"value": "sales@acme-audio.example", ...})（邮箱组的编号, 那个邮箱）；
    #        没选、对不上是 None
    #        用户亲手写了卡片上有的地址、模型却没填号的，按地址认回来（不分大小写）
    said_email = draft["email_user_requested"]
    numbered = email_numbers(lead)
    chosen = next(
        ((owner, email) for number, owner, email in numbered if number == draft["email_ai_understood"]), None
    )
    if chosen is None and _looks_like_address(said_email):
        chosen = next(
            (
                (owner, email)
                for _n, owner, email in numbered
                if email["value"].casefold() == said_email.casefold()
            ),
            None,
        )

    # 二、确定最终draft形式
    #   用户说了                    用用户说的
    #   用户没说、但提了邮箱         写邮件（提了邮箱就是要发邮件，不管卡片上对不对得上）
    #   都没有                      用 AI 定的（parse_ai_plan 保证这时 AI 一定定了）
    # 输出   form -> "email"；ai_decides -> () 或 ("form",)
    if draft["form_user_requested"]:
        form, ai_decides = draft["form_user_requested"], ()
    elif said_email:
        form, ai_decides = EMAIL, ()
    else:
        form, ai_decides = draft["form_ai_planned"], (AI_FORM,)

    # 三、确定最终收件人是谁，拿出她在lead里的一整份信息
    #   文章                        不写给具体的人，写给这家公司
    #   recipient_ai_understood=1   这家公司本身
    #   >= 2                        名单上 2 号起的那个人
    #   没点出人、但写邮件选了邮箱   这个邮箱的主人（官网邮箱的主人就是这家公司）
    #   == -1                       说了、认不出：写给这家公司，说一句他说的原话
    #   == 0                        没说：写给这家公司（AI 不替他挑人）
    # 输出   recipient -> Dana Lee 那一份 / None（这家公司）
    pick = draft["recipient_ai_understood"]
    recipient = None
    if form == ARTICLE:
        if pick > COMPANY_NUMBER:
            notes.append(f"文章不写给具体的人，按写给 {name} 这家公司写")
    elif pick > COMPANY_NUMBER or (pick <= 0 and form == EMAIL and chosen is not None):
        owner = pick if pick > COMPANY_NUMBER else chosen[0]
        if owner > COMPANY_NUMBER:
            recipient = people[owner - COMPANY_NUMBER - 1]
            if recipient["relation"] not in INSIDERS:
                notes.append(f"{person_line(recipient)}，照样给他写")
        # 说了一个认不出的人、又选了邮箱：按邮箱的主人写，说一句（「给 Mike 发到 3-1」）
        if pick == -1:
            whom = recipient["name"] if recipient is not None else f"{name} 这家公司"
            notes.append(
                f"你说的「{draft['recipient_user_requested']}」名单上没认出，按这个邮箱的主人写给 {whom}"
            )
    elif pick == -1:
        notes.append(
            f"你说的「{draft['recipient_user_requested']}」名单上没认出，这份先写给 {company_line(lead)}；"
            "是名单上哪一号告诉我就改"
        )
    elif pick == 0 and form in NEEDS_RECIPIENT and draft["user_words"]:
        notes.append(f"没说写给谁，写给 {company_line(lead)}")
    if recipient is None and form in (LINKEDIN_MESSAGE, LINKEDIN_NOTE):
        notes.append("LinkedIn 消息只能发给一个人或公司主页，称呼写这家公司，发给谁由你定")

    # 四、最终计划发到哪个邮箱（只有邮件要）
    # 用户要求了卡片上的邮箱号                            用它
    # 用户要求了某个邮箱、但并非卡片上的                   照用户说的发，note里提一句
    # 用户要求了某个收件人(“发给团队”)                     按固定顺序挑，note一句挑的是哪个
    # 用户没要求                                         按固定顺序挑（_pick_address）
    # 这家一个邮箱都没有                                  照样写，note一句让用户自己找邮箱
    # 输出   address -> "dana@acme-audio.example" / None
    address = None
    if form == EMAIL:
        if chosen is not None:
            address = chosen[1]["value"]
            # 邮箱的主人跟收件人不是同一个：照发，但说一句（称呼写的是一个，信进的是另一个人的邮箱）
            # 例：「给 2 号写，发到 3-1」；「给他们团队写，发到 3-1」（称呼团队，发到 3 号本人的邮箱）
            target = COMPANY_NUMBER if recipient is None else people.index(recipient) + COMPANY_NUMBER + 1
            if chosen[0] != target:
                owner = chosen[1]["person_name"]
                owner_text = f"{owner} 的邮箱" if owner else f"{name} 官网上的邮箱"
                whom = recipient["name"] if recipient is not None else f"{name} 这家公司"
                notes.append(f"{address} 是 {owner_text}，称呼写的是 {whom}，照你说的发")
        elif said_email and _looks_like_address(said_email):
            address = said_email
            notes.append(f"{said_email} 不是卡片上的邮箱，照你说的发到这个地址")
        elif emails:
            address, note = _pick_address(recipient, emails)
            if said_email:
                notes.append(f"你说的「{said_email}」没认出是哪个邮箱，按默认发到 {address}；要换说邮箱的号")
            elif note:
                notes.append(note)
        else:
            notes.append("这家没有找到邮箱，邮件照样写，发之前要你自己找地址")

    # 包成PlanSlot
    return PlanSlot(
        number=number,
        # 先填 1：是不是 1 要跟上一版比过才知道，finalize_plan 最后统一定
        plan_version=1,
        user_words=draft["user_words"],
        recipient=recipient,
        recipient_user_requested=draft["recipient_user_requested"],
        email_user_requested=said_email,
        form=form,
        address=address,
        emphasis=tuple(draft["emphasis_user_requested"]),
        selling_points=tuple(draft["selling_points_ai_planned"]),
        opening=draft["opening_ai_planned"],
        reason=draft["reason_ai_planned"],
        ai_decides=ai_decides,
        notes=tuple(notes),
        sources=(source,),
    )


def _same_draft_key(slot: PlanSlot) -> tuple[str, str, str] | None:
    """认「同一个对象、同一种、同一个地址」用的钥匙；没有钥匙的不参与合并。

    输入 -> 输出：
        写给 Dana Lee 的邮件，发到 dana@…      -> ("Dana Lee", "email", "dana@acme-audio.example")
        写给这家公司的邮件，发到 hello@…       -> ("", "email", "hello@acme-audio.example")
        文章                                   -> None
        写哪一种是 AI 定的                     -> None

    文章不合并：文章都写给这家公司，靠讲的卖点区分，「讲小批量的一篇」和「讲骨传导的一篇」是两份。
    AI 定了写法的不合并：用户没说这两份是同一种，AI 碰巧定成同一种，不能替他把两份并成一份
    """
    if slot.ai_decides or slot.form == ARTICLE:
        return None
    whom = slot.recipient["name"] if slot.recipient is not None else ""
    return whom, slot.form, slot.address or ""


def _combine(earlier: PlanSlot, slot: PlanSlot) -> PlanSlot:
    """两份并成一份，号用前面那份的。

    强调的点和卖点取并集（顺序照先后），原话和理由用「；」接起来，说明各自保留；
    开场用前面那份的，前面那份没引证据才用后面的；sources 接起来（这一份出自 ai_plan 的哪几项）
    """
    return replace(
        earlier,
        user_words="；".join(w for w in (earlier.user_words, slot.user_words) if w),
        emphasis=tuple(dict.fromkeys(earlier.emphasis + slot.emphasis)),
        selling_points=tuple(dict.fromkeys(earlier.selling_points + slot.selling_points)),
        opening=earlier.opening or slot.opening,
        reason="；".join(r for r in (earlier.reason, slot.reason) if r),
        notes=tuple(dict.fromkeys(earlier.notes + slot.notes)),
        sources=earlier.sources + slot.sources,
    )


def _merge_repeats(
    slots: Sequence[PlanSlot], frozen: Collection[int]
) -> tuple[list[PlanSlot], list[str], list[int]]:
    """同一个对象、同一种、同一个地址出现不止一份的，后面的并进前面那份。号不改。

    输入：slots（按号排好）；frozen（已定稿的号，不参与合并：定稿不能被别的份改掉）
    输出：(留下的, 每并一次一句说明, 被并掉的号)
        例 [#1 给 Dana 的邮件（MOQ 500），#2 给 Sam 的附言，#3 给 Dana 的邮件（固件）]
        -> ([#1 给 Dana 的邮件（MOQ 500、固件），#2 给 Sam 的附言],
            ["第 3 份跟第 1 份都是给 Dana Lee 的邮件，合成了一份"], [3])
    """
    kept: list[PlanSlot] = []
    notes: list[str] = []
    gone: list[int] = []
    for slot in slots:
        key = None if slot.number in frozen else _same_draft_key(slot)
        earlier = next(
            (k for k in kept if key is not None and k.number not in frozen and _same_draft_key(k) == key),
            None,
        )
        if earlier is None:
            kept.append(slot)
            continue
        whom = slot.recipient["name"] if slot.recipient else "这家公司"
        notes.append(
            f"第 {slot.number} 份跟第 {earlier.number} 份都是给 {whom} 的{FORM_TEXT[slot.form]}，合成了一份"
        )
        kept[kept.index(earlier)] = _combine(earlier, slot)
        gone.append(slot.number)
    return kept, notes, gone


def _plan_key(slot: PlanSlot, language: str, tone: str) -> tuple:
    """比「这一份的计划变没变」用的几样：变了 plan_version +1，正文要照新计划重写。
    reason 和 notes 不比：措辞换了不代表要重写
    """
    whom = slot.recipient["name"] if slot.recipient is not None else ""
    return (
        whom,
        slot.form,
        slot.address or "",
        slot.emphasis,
        slot.selling_points,
        slot.opening,
        language,
        tone,
    )


@dataclass(frozen=True)
class Finalized:
    """finalize_plan 的结果：这一版计划，和这一版里不要了的号。

    例：Finalized(plan=Plan(slots=(#1, #3, #4, #5), ...), dropped=(2,))
    dropped 交给 drafts_after_plan 记进账本；Plan 里只放还在的份
    """

    plan: Plan
    # 上一版还在、这一版没了的号：模型没交回来（用户说不要），或者被合并掉了
    dropped: tuple[int, ...]


def finalize_plan(
    ai_plan: Mapping[str, Any],
    lead: Mapping[str, Any],
    config: PlanConfig,
    *,
    previous: Mapping[str, Any],
    drafts: Mapping[str, Any],
) -> Finalized:
    """把模型给的（ai_plan）和卡片上有的（lead）合在一起，算出「这一轮怎么写」。纯函数。

    输入：
        ai_plan   state["ai_plan"]，模型那次调用摊平的结果；每项带 plan_number（0 = 新的）
        lead      state["lead"]
        config    每轮上限、默认语言、默认语气
        previous  上一版计划（state["plan"]）；第一次定计划是 {}。不空就是在改计划
        drafts    账本（state["drafts"]），看哪几份已定稿、发过的最大号；第一次定计划是 {}
        后两个不给默认值：漏传的话改计划会把号重新从 1 发
    输出：Finalized，例子见那个类

    一步一步算（代码就是这个顺序）：
        第 1 步  上一版就有的那几份（模型填了旧号的）：
                     已定稿的    不看模型交回的内容，原样留着（只更新 sources）
                     交回来的    照这一次算；同一个号几项的，并成一份
                     没交回来的  用户说不要了，进 dropped
                 账本里已经不要了的号，模型交回来了：用户要它回来，原号复活，照这一次算
                 （跟它不要时存在账本里的那版计划比 plan_version）
        第 2 步  新的（plan_number 是 0 的）：用户说的每一份各算一个 PlanSlot，临时号接在最大号后面
        第 3 步  合并重复的（同一个对象、同一种、同一个地址，_merge_repeats），已定稿的不参与。
                 放在比份数前面：用户把同一份说了两次时，他心里是一份
        第 4 步  上限：数活着的份（已定稿的也算），超了从后面截。上一版就有的排在前面，所以截掉的只会是新的
        第 5 步  AI 补的（user_words 是空的新项）留几份：
                     一份都没说（第一次定计划）     全要（最多到上限）
                     改计划                        有空位就留（计划里看得见，他可以说不要）
                     说的份数比说清的多            取缺的那几份
                     说的份数比说清的少            按说清的写，写一句说明
                     没说份数，或者对得上          不要（用户说清了要什么，不替他加）
        第 6 步  新的份发正式的号：从账本最大号 + 1 起，按顺序
        第 7 步  语言和语气：说了用他的原话；没说用配置里的默认，各写一句说明
        第 8 步  plan_version：新的是 1；已定稿的不变；其余跟上一版比 _plan_key，变了 +1

    例（编的，Acme Audio。上一版 #1 #2 #3 #4，#1 已定稿；用户看稿后说「第 2 份不要了，
    第 3 份改成给 Dana 发私信，再给 1-2 那个邮箱发一封」）：
        模型交回 [plan_number 1 原样, 3 改成私信, 4 原样, 0 新的邮件]
        -> #1 原样（plan_version 1）/ #3 私信给 Dana（plan_version +1）/ #4 不变 /
           #5 新的邮件发 sales@（plan_version 1）；dropped=(2,)
    """
    revising = bool(previous)
    status = {int(k): entry["status"] for k, entry in drafts.items()}
    # 带上下标：每一份要记下自己出自 ai_plan 的哪几项（PlanSlot.sources）
    indexed = list(enumerate(ai_plan["drafts"]))
    # 输入   上一版还活着的每一份，例 {1: {...}, 3: {...}, 4: {...}}。
    #        看稿时刚说不要的那份还在上一版计划里，但账本里已经是 dropped，不算活着
    old = {
        slot["number"]: slot
        for slot in (previous["slots"] if revising else [])
        if status.get(slot["number"]) != DROPPED
    }
    # 要回来的：账本里已经不要了、模型这次又交回了它的号。「之前」那版计划从账本里取
    revived = {
        d["plan_number"]: drafts[str(d["plan_number"])]["slot"]
        for _i, d in indexed
        if status.get(d["plan_number"]) == DROPPED
    }
    # 号是旧的（活着的、要回来的）就不重新发
    known = {**old, **revived}
    # 已定稿的号，例 {1}
    approved = {n for n in old if status[n] == APPROVED}
    # 新号从这里起：账本里发过的最大号 + 1（不要了的也在账本里，所以号不会复用）
    next_number = max([0, *known, *status]) + 1
    limit = config.max_drafts_per_round
    count = ai_plan["count_user_requested"]
    notes: list[str] = []
    dropped: list[int] = []

    # 1) 上一版就有的，和要回来的
    kept: list[PlanSlot] = []
    for number, old_slot in sorted(known.items()):
        group = [(i, d) for i, d in indexed if d["plan_number"] == number]
        if number in approved:
            if not group:
                print(f"[计划] 第 {number} 份已定稿，模型没交回来，照样留着")
            kept.append(replace(_slot_from_state(old_slot), sources=tuple(i for i, _d in group)))
            continue
        if not group:
            dropped.append(number)
            continue
        parts = [_finalize_slot(number, d, lead, i) for i, d in group]
        merged = parts[0]
        for part in parts[1:]:
            merged = _combine(merged, part)
        kept.append(merged)

    # 2) 新的：用户说的 / AI 补的
    fresh = [(i, d) for i, d in indexed if not d["plan_number"]]
    heard = [(i, d) for i, d in fresh if d["user_words"]]
    added = [(i, d) for i, d in fresh if not d["user_words"]]
    # 用户一份都没说（「写吧」「看你」）：这一版里没有一项带原话、也没说份数。改计划时照样按这一版算
    ai_plans_all = not count and not any(d["user_words"] for _i, d in indexed)
    new = [_finalize_slot(next_number + k, d, lead, i) for k, (i, d) in enumerate(heard)]

    # 3) 合并重复的
    slots, merged_notes, gone = _merge_repeats(kept + new, approved)
    notes.extend(merged_notes)
    dropped.extend(n for n in gone if n in known)

    # 4) 上限：数活着的份
    if len(slots) > limit:
        over = len(slots) - limit
        slots = slots[:limit]
        if revising:
            notes.append(f"一轮最多 {limit} 份（已定稿的也算），新加的 {over} 份没加上；要加可以先不要一份")
        else:
            notes.append(f"一轮最多写 {limit} 份，先写前 {limit} 份，后面的下一轮再说")
    if count > limit and not revising:
        if not any(note.startswith("一轮最多") for note in notes):
            notes.append(f"你说要 {count} 份，一轮最多写 {limit} 份，这一轮按 {limit} 份写")
        count = limit

    # 5) AI 补的留几份
    said = len(slots)
    if revising:
        extra = added[: max(0, limit - said)]
    elif ai_plans_all:
        extra = added[:limit]
    elif count > said:
        extra = added[: count - said]
        total = said + len(extra)
        if total == count:
            notes.append(f"你说要 {count} 份，说清的是 {said} 份，另外 {len(extra)} 份由我来定")
        else:
            notes.append(f"你说要 {count} 份，说清的是 {said} 份，我补了 {len(extra)} 份，一共 {total} 份")
    else:
        if count and count < said:
            notes.append(f"你说要 {count} 份，我听出了 {said} 份，按 {said} 份写")
        if added:
            print(f"[计划] 用户说清了要写什么，AI 多补的 {len(added)} 份不要")
        extra = []
    slots += [_finalize_slot(next_number + len(heard) + k, d, lead, i) for k, (i, d) in enumerate(extra)]

    # 6) 新的份发正式的号：合并、截上限之后连着发，用户看到的是 5、6，不是 5、7
    #    输入   [#1, #3, #4, 临时 #6（新的）]  输出   [#1, #3, #4, #5]
    renumbered: list[PlanSlot] = []
    number = next_number
    for slot in slots:
        if slot.number in known:
            renumbered.append(slot)
        else:
            renumbered.append(replace(slot, number=number))
            number += 1

    # 7) 语言、语气：说了用他的原话，没说用默认并记一句
    language = ai_plan["language_user_requested"] or config.default_language
    if not ai_plan["language_user_requested"]:
        notes.append(f"语言你没说，按默认用{config.default_language}")
    tone = ai_plan["tone_user_requested"] or config.default_tone
    if not ai_plan["tone_user_requested"]:
        notes.append(f"语气你没说，按默认：{config.default_tone}")

    # 8) plan_version
    versioned: list[PlanSlot] = []
    for slot in renumbered:
        if slot.number not in known:
            versioned.append(slot)
            continue
        before = _slot_from_state(known[slot.number])
        if slot.number in approved:
            versioned.append(slot)
            continue
        same = _plan_key(before, previous["language"], previous["tone"]) == _plan_key(slot, language, tone)
        versioned.append(replace(slot, plan_version=before.plan_version + (0 if same else 1)))

    plan = Plan(
        slots=tuple(versioned),
        language=language,
        tone=tone,
        unsupported=tuple(ai_plan["unsupported"]),
        notes=tuple(notes),
        ai_plans_all=ai_plans_all,
    )
    return Finalized(plan=plan, dropped=tuple(sorted(set(dropped))))


def new_entry() -> dict[str, Any]:
    """账本里新的一条：计划里刚出现、还没写过。slot 由 drafts_after_plan 填上这一份的计划。"""
    return {
        "status": PLANNED,
        "version": 0,
        "plan_version": 0,
        "text": "",
        "feedback": "",
        "problems": [],
        "slot": None,
    }


def copy_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """账本里一条复制一份（里面的 list 也复制）。改账本之前都先复制：原地改 state 存档里看不见。"""
    return {**entry, "problems": list(entry["problems"])}


def drafts_after_plan(drafts: Mapping[str, Any], finalized: Finalized) -> dict[str, Any]:
    """定完一版计划之后的账本。纯函数，返回新字典，不改传进来的那份。

    输入：drafts（上一版账本，第一次是 {}）；finalized（这一版计划和不要了的号）
    输出：新账本。每一份：
        新号                                  新建一条 planned
        已定稿                                不动
        还没写过（version 0）                  planned
        计划变了（plan_version 跟正文照的不一样） planned（正文过时了，照新计划从头写）
        原来是 replan、计划却没变               有正文意见 -> rewrite；没有 -> planned（日志）
        原来是 dropped、这一版又有了（要回来的）   planned（照这一版计划从头写）
        其余（rewrite / written）              不动
        dropped 里的号                         dropped
    计划里的每一份都把这一版计划刷进账本的 slot；不要了的那份 slot 停在它最后一版计划，
    要回来时照它复活（原号）
    """
    out = {key: copy_entry(entry) for key, entry in drafts.items()}
    for number in finalized.dropped:
        out[str(number)]["status"] = DROPPED
    for slot in finalized.plan.slots:
        key = str(slot.number)
        entry = out.get(key)
        if entry is None:
            out[key] = new_entry() | {"slot": slot_to_state(slot)}
            continue
        entry["slot"] = slot_to_state(slot)
        if entry["status"] == APPROVED:
            continue
        if entry["status"] == DROPPED:
            print(f"[计划] 第 {key} 份不要了又要回来，原号复活，照这一版计划从头写")
            entry["status"] = PLANNED
            continue
        if entry["version"] == 0 or slot.plan_version != entry["plan_version"]:
            if entry["status"] == WRITTEN:
                print(f"[计划] 第 {key} 份没被点名，计划却变了，正文作废、照新计划重写")
            entry["status"] = PLANNED
        elif entry["status"] == REPLAN:
            print(f"[计划] 第 {key} 份要改计划，这一版计划却没变")
            entry["status"] = REWRITE if entry["feedback"] else PLANNED
    return out


def plan_to_state(plan: Plan) -> dict[str, Any]:
    """把 Plan 摊成能进 state 的普通字典，元组转 list。理由同 ai_plan_to_state。

    输入 -> 输出（编的例子）：
        Plan(slots=(PlanSlot(number=1, user_words="给 Dana 写封邮件",
                                    recipient={"name": "Dana Lee", ...},
                                    recipient_user_requested="Dana", email_user_requested="", form="email",
                                    address="dana@acme-audio.example", emphasis=("MOQ 500",),
                                    selling_points=("moq",), opening=2720, reason="…",
                                    ai_decides=(), notes=(), sources=(0,)),),
                   language="英文", tone="简短、专业，不用推销腔", unsupported=(),
                   notes=("语言你没说，按默认用英文",), ai_plans_all=False)
            -> {"slots": [{"number": 1, "user_words": "给 Dana 写封邮件",
                           "recipient": {"name": "Dana Lee", ...},
                           "recipient_user_requested": "Dana", "email_user_requested": "", "form": "email",
                           "address": "dana@acme-audio.example", "emphasis": ["MOQ 500"],
                           "selling_points": ["moq"], "opening": 2720, "reason": "…",
                           "ai_decides": [], "notes": [], "sources": [0]}],
                "language": "英文", "tone": "简短、专业，不用推销腔", "unsupported": [],
                "notes": ["语言你没说，按默认用英文"], "ai_plans_all": False}
    """
    return {
        "slots": [slot_to_state(slot) for slot in plan.slots],
        "language": plan.language,
        "tone": plan.tone,
        "unsupported": list(plan.unsupported),
        "notes": list(plan.notes),
        "ai_plans_all": plan.ai_plans_all,
    }


def slot_to_state(slot: PlanSlot) -> dict[str, Any]:
    """一份 PlanSlot 摊成字典（plan_to_state 的每一份，也是账本里存的那一格 slot）。"""
    return {
        "number": slot.number,
        "plan_version": slot.plan_version,
        "user_words": slot.user_words,
        "recipient": dict(slot.recipient) if slot.recipient is not None else None,
        "recipient_user_requested": slot.recipient_user_requested,
        "email_user_requested": slot.email_user_requested,
        "form": slot.form,
        "address": slot.address,
        "emphasis": list(slot.emphasis),
        "selling_points": list(slot.selling_points),
        "opening": slot.opening,
        "reason": slot.reason,
        "ai_decides": list(slot.ai_decides),
        "notes": list(slot.notes),
        "sources": list(slot.sources),
    }


def _slot_from_state(slot: Mapping[str, Any]) -> PlanSlot:
    """state["plan"]["slots"] 里的一份读回 PlanSlot，list 转回元组。缺键当场 KeyError。"""
    return PlanSlot(
        number=slot["number"],
        plan_version=slot["plan_version"],
        user_words=slot["user_words"],
        recipient=dict(slot["recipient"]) if slot["recipient"] is not None else None,
        recipient_user_requested=slot["recipient_user_requested"],
        email_user_requested=slot["email_user_requested"],
        form=slot["form"],
        address=slot["address"],
        emphasis=tuple(slot["emphasis"]),
        selling_points=tuple(slot["selling_points"]),
        opening=slot["opening"],
        reason=slot["reason"],
        ai_decides=tuple(slot["ai_decides"]),
        notes=tuple(slot["notes"]),
        sources=tuple(slot["sources"]),
    )


def plan_from_state(plan: Mapping[str, Any]) -> Plan:
    """plan_to_state 的反方向：state["plan"] 读回 Plan，list 转回元组。

    输入 -> 输出：plan_to_state 那个例子的右边 -> 左边，一模一样（tests 焊着来回一趟不变）
    缺键当场 KeyError：存档里的 plan 跟这里不是同一个版本，猜一个值会照着一份错的计划写稿
    """
    return Plan(
        slots=tuple(_slot_from_state(slot) for slot in plan["slots"]),
        language=plan["language"],
        tone=plan["tone"],
        unsupported=tuple(plan["unsupported"]),
        notes=tuple(plan["notes"]),
        ai_plans_all=plan["ai_plans_all"],
    )


def recipient_text(slot: PlanSlot, lead: Mapping[str, Any]) -> str:
    """一份稿「写给谁」给人看的说法。计划里列每一份时用。

    输入 -> 输出（编的例子）：
        写给 Dana Lee      -> "Dana Lee（Founder，这家公司的人）"
        写给这家公司       -> "Acme Audio 这家公司本身（称呼他们的团队）"
    """
    return person_line(slot.recipient) if slot.recipient is not None else company_line(lead)


def form_text(slot: PlanSlot) -> str:
    """一份稿「写哪一种」给人看的说法，写邮件的带上发到哪。

    输入 -> 输出：
        邮件，发到 dana@acme-audio.example  -> "邮件，发到 dana@acme-audio.example"
        邮件，没有邮箱                  -> "邮件"
        文章                            -> "文章"
    """
    text = FORM_TEXT[slot.form]
    return f"{text}，发到 {slot.address}" if slot.address else text


def selling_point_text(key: str) -> str:
    """一个卖点给人看的说法。例："moq" -> "起订量低（MOQ）"；表里没有的原样印，例 "new_key" -> "new_key" """
    return SELLING_POINT_TEXT.get(key, key)
