"""understand_intent 那一次 LLM 调用的全部东西：配置、schema、解析、够不够。

四样放在一个文件里，因为它们是同一次调用的四个面：schema 定它能说什么，解析定我们
怎么收，够不够定这一轮要不要再问，配置定用哪个模型花多少 token。改其中一样，另外
三样通常要一起看。

不放进 models/ 的理由：那个包只依赖标准库（scripts/check_isolation.py 用 ast 守着），
而这里要读 yaml、要认 OpenAI 那个返回对象的形状。

喂给模型的那几段话不在这里，在同目录的 prompts.py。分开的理由跟 discovery/prompts.py
一样：改措辞和改解析是两种改动，混在一个文件里，一次调词的 diff 会淹在解析逻辑中间。

设计本身（为什么是这七个字段、为什么没有 ready、为什么 events 是自由字符串）写在
langgraph学习/graph/graph_2a.txt 第 7 节，这里不重复。
"""

from __future__ import annotations

import json
import math
import pathlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from triggers.interactive_discovery.query.prompts import INTENT_SYSTEM

# 往上四层到仓库根：query/ -> interactive_discovery/ -> triggers/ -> 仓库根
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "interactive.yaml"


# max_spend_usd 的上界，美元。超过它的数字不是预算，是同事把 0.2 说成了 20，
# 或者模型把「两毛钱」读成了 20。
#
# 19 不是我们定的政策，是账户那边的事实：Apify 订阅（Starter）整月含 $19 用量，
# 而一次 interactive 采集约 $0.15。所以「这次最多花 $200」这句话在物理上就不成立。
#
# 它不是那道真闸门，而且比以前更不是了：免费版用光会直接停，那是一道天然的
# 保险；订阅版在 Billing 的 Limits 里开了 overage 之后，花费可以超过 $19 记进
# 下月账单。跨 thread 累计的月度上限还没做，这里只拦明显不可能的数。
MAX_SPEND_CEILING_USD = 19.0

# 够不够的判据。缺了这里列的就得再问一轮，其余字段缺了一律用默认值往下走。
#
# 只有 topics 在里面：一个品类词都没有就拟不出 query，「帮我找点公司」卡在这一条上。
# events 空了 planner 就两种形状各拟一条，time_said 空了用 discovery.yaml 的默认窗口，max_spend_usd 是 0
# 就用配置里那个默认 —— 这些都不值得多问一句，因为每多问一句用户就多一次机会走开，
# 而方案摆在他面前的时候他自然会说「别带 Kickstarter」。
#
# 想加一条就在这个元组里多写一个字段名，别处不用改。真要加之前先看几轮真实对话：
# 现在一条真实开场白都还没有，定几条本质上是猜。
_REQUIRED = ("topics",)


@dataclass(frozen=True)
class IntentConfig:
    """config/interactive.yaml 摊平之后的样子。

    实际值（今天那份配置）：

        IntentConfig(model='gpt-4o-mini', max_tokens=800, temperature=0.0,
                     request_timeout_seconds=30.0, max_clarify_rounds=3)

    跟 scoring.yaml 里那组 llm 参数分开放，因为两者性质不同：那组是 analyzer 的
    （每天一次、一批十家、六千 token、失败了明天再说），这组是对话的（用户在等、
    一次几百 token、失败了他当场就看见）。共用一份配置的话，给 analyzer 调大
    max_tokens 会连带让每一句对话都变贵。
    """

    model: str
    max_tokens: int
    temperature: float
    request_timeout_seconds: float

    # 澄清最多问几轮。超了不再问，带着手上这份不完整的 intent 去拟方案 ——
    # 后面还有 await_go 那道人拍板的闸门，让他对着方案说话比继续追问有效。
    max_clarify_rounds: int


def load_intent_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> IntentConfig:
    """读 config/interactive.yaml 的 intent 那一段。

    输入：配置文件路径（不给就是仓库根的 config/interactive.yaml）
    输出：IntentConfig

    一个实际值：

        文件   intent:
                 model: gpt-4o-mini
                 max_tokens: 800
                 temperature: 0
                 request_timeout_seconds: 30
                 max_clarify_rounds: 3
        输出   IntentConfig(model='gpt-4o-mini', max_tokens=800, temperature=0.0,
                            request_timeout_seconds=30.0, max_clarify_rounds=3)

    缺键当场 KeyError，不给默认值：一个缺了 model 的配置跑起来会去调一个空模型名，
    报错在 OpenAI 那一侧，离真正的原因隔着一整个网络请求。

    不缓存。每次路由和每次 LLM 调用各读一遍这个十行的文件（几十微秒），
    比缓存划算 —— 缓存要考虑什么时候失效，而这条路一天走不了几次。
    """
    # 输入   文件内容
    #        raw 是整份 yaml 解析出来的字典，我们只要 intent 那一段
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["intent"]

    # 输出   摊平成一个冻结的对象，调用处不用记哪个数在第几层
    return IntentConfig(
        model=str(raw["model"]),
        max_tokens=int(raw["max_tokens"]),
        temperature=float(raw["temperature"]),
        request_timeout_seconds=float(raw["request_timeout_seconds"]),
        max_clarify_rounds=int(raw["max_clarify_rounds"]),
    )


@dataclass(frozen=True)
class DiscoveryIntent:
    """从这条对话里读出来的检索需求。七个字段，每一个都可以是空的。

    一个实际值。用户说的是「帮我找找骨传导耳机的初创，最好是刚做完众筹的，
    别带那种预热帖，这次别超过两毛钱」：

        DiscoveryIntent(
            goal="找刚做完众筹、正需要代工厂的骨传导耳机团队",
            topics=("bone conduction", "bone conduction headphone"),
            events=("刚做完众筹",),
            exclusions=("pre-launch",),
            time_said="",
            max_spend_usd=0.2,
            unsupported=(),
        )

    对照第一轮什么都还没有的样子。用户只说了「帮我找点公司」：

        DiscoveryIntent(goal="找潜在客户公司", topics=(), events=(), exclusions=(),
                        time_said="", max_spend_usd=0.0, unsupported=())

    再对照一个有 unsupported 的。用户说「只要加州的、200 人以下的公司」：

        DiscoveryIntent(
            goal="找加州的小型音频设备公司",
            topics=("audio device",),
            events=(), exclusions=(), time_said="", max_spend_usd=0.0,
            unsupported=("只要加州的", "200 人以下"),
        )

    空值一律用空串 / 空元组 / 0，不用 None。跟 state 里 decision 用空串表示「还没判过」
    是同一套：用户没说不是「外部数据真实缺失」，是「这一栏还空着」。
    """

    # 一句中文复述，给用户看的。唯一的消费者是 plan_queries 拼给用户那句话 ——
    # 让他一眼看出我们哪里理解错了，一句中文比 topics 那几个词好读。
    goal: str

    # 品类词、形态词，只收英文。planner 拟 query 文本时的主语：
    # bone conduction -> bone conduction AND (headphone OR earbuds)。
    #
    # 只收英文的理由是它唯一的消费者是 LinkedIn 搜索词，那一侧只吃英文。混着中文
    # 的话 planner 可能把中文词直接拼进 query 串，采回零条、照样计费、而且不报错；
    # 它以后还要整份落进 queries.filters 那个 jsonb，混装等于「骨传导耳机」和
    # bone conduction 永远算两条。
    #
    # 同事说的中文一个字都没丢：原话在 messages 里，中文复述在 goal 里。
    # 代价是翻译由那次调用顺手做，翻歪了很静默（「录音笔」翻成 recording pen 而
    # 不是 voice recorder，query 照跑照花钱）—— 所以 prompt 里给了几个对照词，
    # 而且 topics 要原样摆进 plan 的 assumptions 里给人看。
    topics: tuple[str, ...]

    # 要什么样的事件。决定 query 的形状 —— 实测只有两种形状有效（config/queries.yaml
    # 里那段）：形状 A 是平台或事件词（Kickstarter AND 品类），形状 B 是形态词加感官词
    # （wearable AND (microphone OR audio OR voice)）。
    #
    # 值长这样：「刚做完众筹」「发布或预售公告」「在招量产 NPI 岗」
    # 「抱怨现在的代工厂」「明确在找代工厂」。
    #
    # 是自由字符串不是闭集，判据是「代码要不要在这个值上分支」：读它的是 planner
    # 那次 LLM 调用，它读得懂「刚做完众筹」，没有任何一段 Python 拿它做 if。
    # 哪天代码真要按事件挑 cadence 或者池子，那天再收成 StrEnum。
    events: tuple[str, ...]

    # 明确不要的，只收英文。两个消费者：拟词时这几个词不许写进 query 正文，
    # 以及原样进 plan 的 assumptions 给人看。
    #
    # 它原来的第一个消费者是「query 里的 NOT 子句」，那条路已经作废。
    # 实测（同一次 run 三条 query，配对比存活率）：LinkedIn 的 NOT 不是过滤器，
    # 是相关性罚分 —— 语义离题目远的词（hiring）罚一下全掉出去，而语义就是题目
    # 本身的词（supplier）罚了照样排在最前面，含它的帖子存活率 75%，比不含的
    # 44% 还高。我们最想排掉的恰恰是后一种（工厂广告跟「找代工厂」是同一件事），
    # 所以 NOT 对我们没有用。不采回来的办法只剩正面圈定发帖人（filters 那一列）。
    #
    # 只收英文的理由也跟着换了：不再是「它要进搜索词」，而是 planner 要拿它跟
    # 自己正在写的那句英文 query 比对。混着中文的话比不上，而且不报错。
    # 说不成一个词的排除（「不要那些只是转发的帖子」）该进 unsupported。
    exclusions: tuple[str, ...]

    # 他关于时间范围的原话，一字不改，例如「最近两个月」「这周」「不限时间」。没提是空串。
    #
    # 只抄原话、不让模型换算：以前让它从那几档里选，会被前缀带跑（「最近两周」选成 24h），
    # 而抄原话实测十条抄对十条。换算成日期在 plan_queries 里调 time_window.resolve_window，
    # 结果放进 plan，卡片上和真采集用的是同一个日期
    time_said: str

    # 用户给的硬上限，美元。0 表示他没说，用配置里那个默认。
    # 消费者是花钱之前那道闸门：plan 的 estimated_cost_usd 超过它的话，
    # await_go 不该只问「跑不跑」，得先说超了多少。
    #
    # 进来的数会先过 _spend_limit：负数和 NaN 当场炸，大得不可能的按上限算并且
    # 在 unsupported 里说一句。
    max_spend_usd: float

    # 用户提了、而我们做不到的要求。例如「只要加州的」「200 人以下」。
    #
    # 这里要分清「actor 做不到」和「我们今天没接」，它们的处置不一样：
    #   真做不到   地理、公司规模 —— actor 的输入里根本没有这两样，只能进 unsupported。
    #              地名当关键词塞进 topics 筛的是「正文写了 Irvine」不是「在 Irvine」
    #   只是没接   职位（authorKeywords 按 headline 和 job title 筛人）、行业
    #              （authorsIndustryId）、指定公司（authorsCompanies）。actor 支持，
    #              管道也通了（queries.filters -> QuerySpec -> build_input），
    #              但 planner 还没开始往里填，所以「找 CTO」今天仍然落在这里。
    #              2c 让 planner 填 authorKeywords 之后，它就该从这里挪走
    #
    # actor 的 24 个输入字段里没有任何一个是「排除」语义，全是正向筛选。
    #    想少采一类人，只能靠正面圈定要谁（authorKeywords），排除不了。
    #
    # 这个字段存在的理由是防静默忽略：没有它，模型要么把这种话硬塞进某个字段里
    # 假装做到了，要么当没听见 —— 两种都会让用户以为方案是按他说的拟的。
    # 消费者是 plan_queries：原样写进 plan["assumptions"]，跟报价一起交给用户看。
    unsupported: tuple[str, ...]


@dataclass(frozen=True)
class IntentResult:
    """understand_intent 那一次 LLM 调用的完整输出。

    一个实际值（对话第一轮，用户只说了「帮我找点公司」）：

        IntentResult(
            intent=DiscoveryIntent(goal="找潜在客户公司", topics=(), ...),
            clarification_question="你想找做哪一类设备的公司？",
        )

    两层不是因为有两个填写者 —— 两层都是同一个节点、同一次调用一次填出来的。
    分两层是因为生命周期不同：intent 那一半是累积的，每轮更新、最后要整份交给
    plan_queries（以后还要整份落进 queries.filters 那个 jsonb）；
    clarification_question 是这一轮的动作，下一轮就作废。

    这里没有 ready 字段。够不够是一个纯函数（is_ready），代码现算，不存 state ——
    跟 decision 必须占一个 state 字段正好相反，分界是「重算得出来吗」。
    """

    intent: DiscoveryIntent

    # 这一轮如果要追问，问哪一句。模型每轮都给，用不用由代码决定（见 is_ready）。
    # 够了的时候它也会给一句（比如「要不要把颈挂式蓝牙耳机也算进来」），
    # 那句话现在被丢掉 —— 代价是模型没有追问的否决权，这是刻意选的。
    clarification_question: str


# 交给 OpenAI 的 json_schema。strict 模式有两条硬要求：每一层都要
# additionalProperties: false，且 required 必须列全 properties 里的每一个键 ——
# 所以「可以为空」不是靠不给，是靠给一个空串或空数组。
#
# 显式给 schema、不靠 prompt 求它返回 JSON，是 analyzer 那边已经踩过的路
# （discovery/analyzer.py 的 RESPONSE_SCHEMA）。
#
# 加字段的时候三处要一起改：这里的 properties、这里的 required、
# 还有 parse_intent_result 里那几行。少改一处的症状分两种：漏在 required 里，
# 模型可能整个不返回那个键，解析当场 KeyError；漏在 parse 里，那个字段永远是空的，
# 而且不报错。
INTENT_SCHEMA: dict[str, Any] = {
    "name": "discovery_intent",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "clarification_question"],
        "properties": {
            "intent": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "goal",
                    "topics",
                    "events",
                    "exclusions",
                    "time_said",
                    "max_spend_usd",
                    "unsupported",
                ],
                "properties": {
                    "goal": {"type": "string"},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "events": {"type": "array", "items": {"type": "string"}},
                    "exclusions": {"type": "array", "items": {"type": "string"}},
                    # 自由字符串，不是闭集。以前这里是那几档的 enum，模型会被前缀带跑
                    # （「最近 2 个月」写出「2」之后只剩 24h 拼得成），理由见 time_window.py 开头
                    "time_said": {"type": "string"},
                    "max_spend_usd": {"type": "number"},
                    "unsupported": {"type": "array", "items": {"type": "string"}},
                },
            },
            "clarification_question": {"type": "string"},
        },
    },
}


class IntentCallFailed(RuntimeError):
    """这一次 intent 调用没拿到能用的回答。

    六种情况共用这一个类型：SDK 抛的异常、输出被 max_tokens 截断、模型拒答、
    没有内容、返回的不是合法 JSON、少了字段或者数字不合法。共用一个类型是因为
    这一层对它们做的事一样 —— 都是把上下文补上再抛，让上面知道是哪条对话、
    哪一句话出的事。自己重试没有意义：temperature 是 0，同样的输入拿回同样的
    坏回答。

    最后那种跟前面几种性质不同：strict 的 json_schema 下七个键必然在，所以缺键
    说明 INTENT_SCHEMA 和 parse_intent_result 漂了，那是我们的 bug，不是模型
    答得差。不为它单开一个类型，是因为这一层对它做的事跟前面一模一样（补上
    上下文再抛，整次 invoke 失败）—— 不同的类型是用来承载不同的行为的，
    这里没有不同的行为。

    抛到上面（节点）会让这一次 invoke 整个失败。对一条 Slack 对话来说，
    那意味着用户看到的是「出错了」而不是一句问题 —— 这是对的：宁可显式失败，
    也不要编一个 intent 出来接着往下走，因为下一步就是花钱。
    """


def build_messages(lines: Sequence[str], *, feedback: str) -> list[dict[str, str]]:
    """把一条对话的全部原话拼成喂给模型的那两条（或三条）消息。

    输入：
        lines      state["messages"]，带说话人前缀的每一句原话
        feedback   lines 里最新的那一句（state["plan_feedback"]），单独再给一遍。
                   空串表示这一趟不是来改方案的
    输出：OpenAI chat 格式的消息列表。feedback 空着是两条，非空是三条

    两个实际值。

    一、第一次开口，或者刚答完一句澄清（feedback 是空串）

        输入   lines=["用户: 帮我找找骨传导耳机的初创",
                      "AI: 你想找做哪一类设备的公司？",
                      "用户: 骨传导耳机，最好是刚做完众筹的"]
               feedback=""
        输出   [{"role": "system", "content": INTENT_SYSTEM},
                {"role": "user", "content": 三句原话用换行接起来}]

    二、用户对着一版方案提了意见，图绕回来重读（feedback 非空）

        输入   lines=[..., "AI: 方案 v1，2 条 query，预计 $0.16。我理解的需求：…",
                      "用户: 别带众筹的"]
               feedback="别带众筹的"
        输出   前两条一样，第三条是
               {"role": "user", "content": "他刚看完我们给的那一版方案，回的是这一句，"
                                           "这一轮要照着它改：\n别带众筹的"}

    第三条干的事就一件：把 lines 那一长串历史里最新的一句指出来。
    lines 绕三轮之后长这样：

        用户: 帮我找找骨传导耳机的初创
        AI: 方案 v1，2 条 query，预计 $0.16。我理解的需求：… 要搜的词：… 做不到的：…
        用户: 别带众筹的
        AI: 方案 v2，1 条 query，预计 $0.08。我理解的需求：… 要搜的词：… 做不到的：…
        用户: 第二条不要了          <- 只有这一句是新的

    不指出来的话，模型得自己从这一堆里认出最后那一句。而那件事越绕越难：每出一版
    方案就往里写一整份清单（那几行是我们说的，不是用户说的），绕三轮之后我们写进去
    的字数比他说的多好几倍。prompt 里说了一遍「以他的原话为准」，但那是请求；
    把那一句原样再放一遍、放在最后，是这一层能出的力。

    它不替代上面那段：用户说「第二条不要了」的时候，"第二条"指的是上面那几行里列的
    那几条，只有完整对话读得出来。所以是两段都给，不是二选一。

    整段对话塞进一条 user 消息，不按行拆成 user / assistant 轮流的形式。两个理由：
    我们要模型做的是「读这段记录得出一份需求」，不是「扮演对话里的一方」；
    而且拆的话要靠前缀猜每一行是谁说的，前缀哪天换成真实说话人（state.py 里
    那个待定：同一条 Slack thread 两个人都回话），拆法就跟着错，而且不报错。

    每一轮都把全部历史重新喂一遍，是方案 B 的核心：用户新补的那句话已经在
    checkpoint 里了，这一次调用才第一次看见它（graph_2a.txt 3.12）。
    代价是 token 随轮数线性涨 —— 澄清最多三轮，涨不到哪儿去。
    """
    # 输入   lines 里的每一句原话，一个字不改地拼起来
    messages = [
        {"role": "system", "content": INTENT_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]

    #        feedback 只在非空时加这一条。空串也加的话，模型会看见一条内容是空的
    #        指令，而那比不给更糟 —— 它得自己猜那是什么意思
    if feedback.strip():
        messages.append(
            {
                "role": "user",
                "content": "他刚看完我们给的那一版方案，回的是这一句，这一轮要照着它改：\n"
                + feedback.strip(),
            }
        )

    # 输出   两条或三条消息。不写库，不碰 state
    return messages


def parse_intent_result(payload: Mapping[str, Any]) -> IntentResult:
    """把模型返回的那份 JSON 变成 IntentResult。

    输入：json.loads 出来的字典
    输出：IntentResult

    一个实际值：

        输入   {"intent": {"goal": "找刚做完众筹的骨传导耳机团队",
                           "topics": ["bone conduction", "bone conduction headphone"],
                           "events": ["刚做完众筹"], "exclusions": ["pre-launch"],
                           "time_said": "", "max_spend_usd": 0.2, "unsupported": []},
                "clarification_question": "要不要把颈挂式的蓝牙耳机也算进来？"}
        输出   IntentResult(intent=DiscoveryIntent(...), clarification_question="...")

    缺键不兜底，当场抛 IntentCallFailed：strict schema 下模型必须给全七个字段，
    缺了说明 INTENT_SCHEMA 和这里漂了，那是我们的 bug。抛的不是裸 KeyError，
    是因为裸 KeyError 只有一个键名，而查这件事第一个要看的是「那份回答到底
    长什么样」。

    列表里的空串和纯空白当场扔掉（模型偶尔会给 [""]）。留着的话，topics 会变成
    「非空」，is_ready 就判成够了，然后 planner 拿着一个空词去拟 query ——
    一条不报错、但会花钱的路。

    max_spend_usd 另外查一遍（_spend_limit）：schema 里它只是一个 number，
    而它是花钱那道闸门要拿来比大小的数。
    """
    try:
        #        raw 是 intent 那一层。clarification_question 在外面一层
        raw = payload["intent"]

        #        时间范围的原话，只去掉首尾空白，一个字不改。换算成日期在 time_window.resolve_window，
        #        不在这里：这一层只管「他说了什么」，而且这里不知道现在是几号
        # 输入   "最近两个月"          输出   "最近两个月"
        time_said = str(raw["time_said"]).strip()

        #        上限和 unsupported 一起算：上限大得不可能的时候，那件事本身就是
        #        「他要的我们做不到」，要跟报价一起摆给他看
        spend, unsupported = _spend_limit(raw["max_spend_usd"], _clean(raw["unsupported"]))

        #        品类词挑一遍形状，不合格的变成一句话进 unsupported。
        #        排在 _spend_limit 后面是因为两者都往同一个收纳盒里加东西，
        #        顺序决定了 assumptions 里哪句话在前，没有别的影响
        topics, unsupported = _sift_topics(_clean(raw["topics"]), unsupported)

        #        要避开的词跟要找的东西撞了，按要找处理，撞了的那几个也变成一句话进 unsupported。
        #        排在 _sift_topics 后面：拿来比的是挑过形状之后真正会用的那几个品类词
        events = _clean(raw["events"])
        exclusions, unsupported = _drop_contradicted_exclusions(
            _clean(raw["exclusions"]), topics=topics, events=events, unsupported=unsupported
        )

        # 输出   一个冻结的对象。不写库，不碰 state
        return IntentResult(
            intent=DiscoveryIntent(
                goal=str(raw["goal"]).strip(),
                topics=topics,
                events=events,
                exclusions=exclusions,
                time_said=time_said,
                max_spend_usd=spend,
                unsupported=unsupported,
            ),
            clarification_question=str(payload["clarification_question"]).strip(),
        )
    except KeyError as err:
        # 输出   包一层再抛。裸 KeyError 只有键名，这里要的是整份回答
        raise IntentCallFailed(
            f"返回里少了 {err.args[0]!r} 这个键。strict schema 下不该发生，多半是 "
            f"INTENT_SCHEMA 和 parse_intent_result 漂了。整份回答："
            f"{json.dumps(payload, ensure_ascii=False, default=str)[:300]}"
        ) from err


def _clean(values: Sequence[Any]) -> tuple[str, ...]:
    """列表字段的清洗：去空白、扔掉空串、保持原顺序。

    输入：模型给的那个数组
    输出：元组

    一个实际值：

        输入   ["骨传导耳机", "  ", "", "bone conduction"]
        输出   ("bone conduction", "bone conduction headphone")

    不去重：「骨传导耳机」和「bone conduction」看着像重复其实是两条，
    真重复了也无害（planner 拟词时自己会合）。去重要判「算不算同一个词」，
    那是 normalize_query_text 那一层的事，不该在这里各写一份。
    """
    return tuple(s.strip() for s in values if str(s).strip())


# 品类词里不许出现的东西：布尔符（大写独立成词，LinkedIn 只认大写）、引号、括号。
_TOPIC_BANNED = re.compile(r'(?:(?<![A-Za-z0-9])(?:AND|OR|NOT)(?![A-Za-z0-9]))|["()]')

# 品类词最多几个词。用户说话的粒度就是两三个词的名词短语
# （conference microphone / bone conduction headphone）。
_TOPIC_MAX_WORDS = 3


def _sift_topics(
    topics: tuple[str, ...], unsupported: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """挑出形状合格的品类词，不合格的转成一句话记进 unsupported。

    输入：清洗过的 topics；到这一步为止的 unsupported
    输出：(合格的 topics, 加长了的 unsupported)

    两个实际值：

        输入   topics=("Bone Conduction", "microphone NOT supplier")
        输出   (("bone conduction",),
                ("把「microphone NOT supplier」当品类词用不了：...",))

        输入   topics=("conference microphone",)
        输出   (("conference microphone",), 原样的 unsupported)

    为什么在这一层拦，不留给 plan_queries：

        一、plan_queries 是一次 LLM 调用，靠 prompt 约束不如靠代码，而这件事
            能确定算出来
        二、topics 要原样落进 queries.topics 那一列，下一次对话靠它做「这个题目
            以前搜过吗」的匹配。一条带布尔符的品类词进了库，词面匹配永远命中
            不了它，而且不报错 —— 症状只是「明明搜过却查不出来」
        三、拟词那一步在花钱的路上，闸门该放在更前面

    不合格的不是丢掉而是搬进 unsupported：丢掉的话用户以为我们按他说的搜了。
    unsupported 原样进 plan 的 assumptions，跟报价一起摆给他看。

    顺手统一转小写。大小写不影响匹配（term_pattern 本来就不区分），但库里那一列
    将来要跟一批词比重合，存进去的写法统一一点，那天不用先纠结口径。
    """
    kept: list[str] = []
    notes: list[str] = []

    for topic in topics:
        if _TOPIC_BANNED.search(topic):
            notes.append(
                f"把「{topic}」当品类词用不了：品类词是一个名词短语，"
                f"不能带 AND/OR/NOT 或者引号括号（那是 planner 拟检索式时才用的）"
            )
        elif len(topic.split()) > _TOPIC_MAX_WORDS:
            notes.append(
                f"把「{topic}」当品类词用不了：它有 {len(topic.split())} 个词，"
                f"品类词最多 {_TOPIC_MAX_WORDS} 个"
            )
        else:
            kept.append(topic.lower())

    return tuple(kept), unsupported + tuple(notes)


def _drop_contradicted_exclusions(
    exclusions: tuple[str, ...],
    *,
    topics: tuple[str, ...],
    events: tuple[str, ...],
    unsupported: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """要避开的词如果同时出现在要找的事件或品类词里，就当作读错了：不避开它，并记一句话。

    输入：清洗过的 exclusions；挑过形状的 topics；清洗过的 events；到这一步为止的 unsupported
    输出：(还要避开的 exclusions, 加长了的 unsupported)

    两个实际值：

        输入   exclusions=("Kickstarter", "Indiegogo", "pre-launch")
               events=("在 Kickstarter 或 Indiegogo 上众筹",)
        输出   (("pre-launch",),
                ("「Kickstarter」出现在要找的事件里，按要找处理，拟词时不避开它",
                 "「Indiegogo」出现在要找的事件里，按要找处理，拟词时不避开它"))

        输入   exclusions=("pre-launch",)  events=("刚做完众筹",)
        输出   (("pre-launch",), 原样的 unsupported)

    为什么要有这一道：用户说「找在 Kickstarter 上众筹的团队」，模型把 Kickstarter 填进了
    exclusions。planner 拟出来的 Kickstarter AND (…) 于是全被当成「写进了说好要避开的词」
    丢掉，剩下的检索式一条帖子都采不回来 —— 而 Kickstarter AND (品类) 恰恰是最准的那种写法。
    一个词不可能既要找又要避开，两边撞了只可能是读错了。按要找处理，是因为 exclusions 的
    唯一动作是「拟词时不许写这个词」，错放一个进去会直接丢掉整条 query；而漏避开一个词，
    最坏是多采几条噪音。

    撞没撞按整词比、不分大小写：Kickstarter 算撞上「在 kickstarter 上众筹」，
    kick 不算撞上 Kickstarter。这一步不调模型，同样的输入永远给同样的输出
    """
    kept: list[str] = []
    notes: list[str] = []

    for word in exclusions:
        # 输入   word="Kickstarter"
        # 输出   一个整词匹配、不分大小写的正则。前后不许紧挨着字母或数字，所以 kick 碰不到 Kickstarter
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", re.IGNORECASE)
        if any(pattern.search(event) for event in events):
            notes.append(f"「{word}」出现在要找的事件里，按要找处理，拟词时不避开它")
        elif any(pattern.search(topic) for topic in topics):
            notes.append(f"「{word}」出现在要搜的品类词里，按要找处理，拟词时不避开它")
        else:
            kept.append(word)

    return tuple(kept), unsupported + tuple(notes)


def _spend_limit(value: Any, unsupported: tuple[str, ...]) -> tuple[float, tuple[str, ...]]:
    """查一遍同事给的花费上限，顺手把「大得不可能」那种记进 unsupported。

    输入：模型填的 max_spend_usd，以及已经清洗过的 unsupported
    输出：(能用的上限, 可能多了一条的 unsupported)

    三个实际值：

        (0.2, ())     -> (0.2, ())      他说了两毛钱
        (0.0, ())     -> (0.0, ())      他没说。0 就是「没说」，不是「一分不花」
        (20.0, ())    -> (5.0, ('同事说的上限 $20 超过账户整月的 $5，按 $5 算',))

    负数和 NaN 当场抛，因为它们不是「填得不好」，是会让闸门反着走的值：负数比
    任何一次报价都小，闸门永远拦；NaN 跟任何数比大小都是 False，闸门永远放行 ——
    后一种会真的花钱，而且报表上完全正常。json.loads 认 NaN 这个字面量，
    所以它不是想出来的情况。

    超上限那种不抛。同事把 0.2 说成 20 是个手误，为它整条对话失败太重，而且
    unsupported 本来就是「他提了、我们做不到」那一栏，会原样进 plan 的
    assumptions 摆给他看 —— 压到上限这件事因此不是静默的。
    """
    #        先变成一个 float。strict schema 下它已经是 number，这一步防的是
    #        以后有人把 schema 改松了
    try:
        spend = float(value)
    except (TypeError, ValueError) as err:
        raise IntentCallFailed(f"max_spend_usd 返回了 {value!r}，它不是一个数字") from err

    # 输出   反向的值当场抛
    if not math.isfinite(spend) or spend < 0:
        raise IntentCallFailed(f"max_spend_usd 返回了 {value!r}，它必须是 0 或者一个正的有限数字")

    # 输出   超上限的压到上限，并且多记一条 unsupported
    if spend > MAX_SPEND_CEILING_USD:
        note = (
            f"同事说的上限 ${spend:g} 超过账户整月的 ${MAX_SPEND_CEILING_USD:g}，"
            f"按 ${MAX_SPEND_CEILING_USD:g} 算"
        )
        return MAX_SPEND_CEILING_USD, unsupported + (note,)

    return spend, unsupported


def to_state_dict(result: IntentResult) -> dict[str, Any]:
    """把 IntentResult 摊成一份能进 state 的普通字典。

    输入：IntentResult
    输出：dict，里面只有 str / list / float，没有元组也没有我们自己的类

    一个实际值：

        输入   IntentResult(
                   intent=DiscoveryIntent(goal="找做骨传导耳机的早期团队",
                                          topics=("bone conduction", "bone conduction headphone"),
                                          events=(), exclusions=(), time_said="",
                                          max_spend_usd=0.0, unsupported=()),
                   clarification_question="要不要把颈挂式的蓝牙耳机也算进来？")
        输出   {"intent": {"goal": "找做骨传导耳机的早期团队",
                           "topics": ["bone conduction", "bone conduction headphone"],
                           "events": [], "exclusions": [], "time_said": "",
                           "max_spend_usd": 0.0, "unsupported": []},
                "clarification_question": "要不要把颈挂式的蓝牙耳机也算进来？"}

    元组特意转成 list：这份东西要被序列化进 checkpoint_blobs，读回来的时候本来就
    不是元组了。在这里转一次，读的人看到的永远是同一种形状 —— 否则「刚写完那一步
    读到元组、下一步从库里读到列表」，而两者 == 不相等，这种不一致不报错。

    存 dict 不存对象的理由在 state.py 的 intent_result 那段注释里。
    """
    # 输入   result 的两层
    # 输出   两层都摊成 dict / list。这个函数自己不写库，写是节点 return 之后的事
    intent = result.intent
    return {
        "intent": {
            "goal": intent.goal,
            "topics": list(intent.topics),
            "events": list(intent.events),
            "exclusions": list(intent.exclusions),
            "time_said": intent.time_said,
            "max_spend_usd": intent.max_spend_usd,
            "unsupported": list(intent.unsupported),
        },
        "clarification_question": result.clarification_question,
    }


def is_ready(intent: Mapping[str, Any]) -> bool:
    """这份 intent 够不够拟方案。

    输入：state["intent_result"]["intent"]，也就是一份已经摊平成 dict 的 intent
    输出：True 表示可以去 plan_queries，False 表示还得问一轮

    三个实际值：

        {"topics": ["bone conduction"], "events": [], ...}          -> True
        {"topics": [], "events": ["刚做完众筹"], ...}           -> False（品类还不知道）
        {"topics": [], "events": [], "goal": "找点公司", ...}   -> False

    收的是 dict 不是 DiscoveryIntent：它的调用方是 route_intent，那里拿到的是
    从 checkpoint 里读出来的 state，里面本来就是 dict（见 nodes.to_state_dict）。

    这件事让代码算不让模型填，是这个仓库用过三次的那条原则（models/scoring.py
    开头那句）：LLM 只填需要判断的，能算的一律代码算。够不够是纯函数，
    让模型填等于让它赌一件确定的事，而且它会飘 —— 同一份 intent 两次调用给出
    不同的答案，事后查都没处查。
    """
    # 输入   _REQUIRED 里列的那几个字段
    # 输出   全都非空才算够。不写库，不碰 state
    return all(intent[field] for field in _REQUIRED)


def read_intent(client: Any, lines: Sequence[str], *, feedback: str, config: IntentConfig) -> IntentResult:
    """调一次模型，把这条对话读成一份 intent。这一步花钱（很少，但是花）。

    输入：
        client   一个能 chat 的对象。真跑时是 openai.OpenAI，测试里是假客户端 ——
                 这一层不 import openai，也不认它的异常类型，理由跟 analyzer
                 那边一样：认死一个供应商的类型，换实现或者写测试都得跟着改
        lines    state["messages"]，这条对话说过的每一句原话
        feedback lines 里最新的那一句（state["plan_feedback"]），单独再给一遍，
                 好让模型不用自己从一长串历史里找它。空串表示这一趟不是来改方案的。
                 必填，不给默认值：漏传的后果是那一句不再被指出来，没有任何症状
        config   模型、max_tokens、温度、超时

    输出：IntentResult

    一个实际值：

        输入   lines=["用户: 帮我找点公司"]，feedback=""
        输出   IntentResult(intent=DiscoveryIntent(goal="找潜在客户公司", topics=(), ...),
                            clarification_question="你想找做哪一类设备的公司？")

    对库的写：一笔都没有。它不连 Neon，也不碰 checkpoint 的任何一张表 ——
    真正写 state 的是调它的那个节点 return 的时候（见 nodes.understand_intent）。

    五种拿不到好回答的情况全部抛 IntentCallFailed，每一种都把上下文补上。
    不在这一层重试：temperature 是 0，同样的输入拿回同样的坏回答。
    """
    # 输入   整段对话拼成的两条消息。拼在 try 外面：build_messages 里抛异常
    #        会被下面报成「请求失败」，把我们自己的 bug 伪装成网络故障
    messages = build_messages(lines, feedback=feedback)

    try:
        # 输出   一次 HTTP 请求。超时是我们的、重试是 SDK 的 —— 不显式给 timeout
        #        的话 openai 默认 600 秒，而用户正在 Slack 那头等着
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": INTENT_SCHEMA},
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout_seconds,
        )
    except Exception as err:
        raise IntentCallFailed(f"请求失败：{type(err).__name__}: {err}") from err

    #        choice 是唯一那个候选回答（没要 n>1）
    choice = response.choices[0]

    if choice.finish_reason == "length":
        raise IntentCallFailed(
            f"输出被 max_tokens={config.max_tokens} 截断。一份 intent 约 200 tokens，"
            f"截断多半是对话太长或者模型在凑字，调 config/interactive.yaml 的 max_tokens"
        )

    # refusal 是结构化输出下模型拒答时才有值的字段，正常是 None
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise IntentCallFailed(f"模型拒答：{refusal}")

    content = choice.message.content
    if content is None:
        raise IntentCallFailed(f"模型没有返回内容，finish_reason={choice.finish_reason}")

    #        payload 是那份 JSON。strict schema 下它应该永远合法，
    #        真不合法的时候要把原文带上 —— 光说「JSON 解析失败」查不了
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as err:
        raise IntentCallFailed(f"返回的不是合法 JSON：{err}。原文前 200 字：{content[:200]}") from err

    # 合法 JSON 不等于一个对象："null"、"[]"、"3" 都能解析成功。strict schema 下不该
    # 发生，但真发生时得在这儿炸 —— 不然下一句 payload["intent"] 抛的是 TypeError，
    # 那种报错读起来像我们自己的 bug，查的方向完全反了
    if not isinstance(payload, dict):
        raise IntentCallFailed(f"返回的 JSON 不是一个对象，是 {type(payload).__name__}：{content[:200]}")

    # 输出   解析成 IntentResult。缺字段在这一步炸
    return parse_intent_result(payload)
