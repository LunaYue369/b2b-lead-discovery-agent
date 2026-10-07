"""classify_reply 那一步：用户对着方案回的那句话，是要改、要跑、还是不跑了。

跟 intent.py / planner.py 同一个摆法：一次 LLM 调用的几个面放在一个文件里 ——
配置、schema、拼消息、解析。

这三次调用里，只有这一次的结果直接决定要不要花钱：判成 run 的下一步就是
run_harvest，钱当场出去。所以这个文件里到处都在往一个方向偏 —— 拿不准就判 replan。
prompt 里说了一次（prompts.CLASSIFY_SYSTEM），parse_reply 里那个闭集挡了一次，
节点那边还有一道确定性的兜底（一条 query 都没有的时候不许判 run，见 nodes.py）。

为什么不用关键词表。1b 阶段这一步是一张写死的词表（跑/run/go/开始 对
取消/算了/不用了/cancel），认不出来一律当「改」。它挡得住「别跑了」这种整句相等
的陷阱，挡不住的是：

    行吧就这样          明明是同意，词表判成改，白白多问一轮
    可以，不过再加一条  明明是改，如果哪天有人把词表改成包含匹配就会判成跑，然后花钱
    嗯                  同意还是敷衍，词表答不了

词表和模型的分界跟仓库里那条一样：能算的代码算。而「这句话是什么意思」算不出来。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from triggers.interactive_discovery.query.prompts import CLASSIFY_SYSTEM

# 往上四层到仓库根：query/ -> interactive_discovery/ -> triggers/ -> 仓库根
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "interactive.yaml"

# 三个取值，也是 graph.route_by_decision 那张表的全部键。
#
# 它在这里定义、在 graph.py 那张表里被用到，两处必须是同一套：判断函数返回一个表里
# 没有的键会当场 KeyError，而那要等用户真回话那一刻才发作。有测试拿这个元组跟编译
# 出来的那张表对一遍。
#
# 顺序是「最不花钱的排前面」，跟 schema 里那个 enum 的顺序一致。这只是写法上的提醒，
# 模型不会因为谁排前面就更爱选谁。
DECISIONS = ("replan", "run", "cancel")


@dataclass(frozen=True)
class ReplyConfig:
    """config/interactive.yaml 的 reply 那一段。

    实际值（仓库里那份配置）：

        ReplyConfig(model='gpt-4o-mini', max_tokens=200, temperature=0.0,
                    request_timeout_seconds=15.0)

    没有第五个数。另外两次调用各带一个自己的旋钮（intent 带 max_clarify_rounds，
    planner 带 max_queries 和单价），这一次没有要调的东西 —— 它只是三选一。
    """

    # 用哪个模型。
    model: str

    # 一次回答最多几个 token。一个词加一句中文理由，实测 60 以内。
    max_tokens: int

    # 采样温度。0：同一句话两次判出不同结果，意味着「跑」这个动作花不花钱要看运气。
    temperature: float

    # 单次请求超时，秒。这一步是全流程里用户等得最不耐烦的一处 —— 他刚敲完「跑」，
    # 正盯着屏幕。所以它比另外两次短，早点失败早点让他再说一次。
    request_timeout_seconds: float


def load_reply_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> ReplyConfig:
    """读 config/interactive.yaml 的 reply 那一段。

    输入：配置文件路径（不给就是仓库根的 config/interactive.yaml）
    输出：ReplyConfig

    一个实际值：

        文件   reply:
                 model: gpt-4o-mini
                 max_tokens: 200
                 temperature: 0
                 request_timeout_seconds: 15
        输出   ReplyConfig(model='gpt-4o-mini', max_tokens=200, temperature=0.0,
                           request_timeout_seconds=15.0)

    缺键当场 KeyError，跟另外两份配置同一条口径：退化成默认值的后果是
    「实际用的数跟配置说的不一样」，而它不报错。
    """
    # 输入   文件内容。raw 是 reply 那一段
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["reply"]

    # 输出   摊平成一个冻结的对象
    return ReplyConfig(
        model=str(raw["model"]),
        max_tokens=int(raw["max_tokens"]),
        temperature=float(raw["temperature"]),
        request_timeout_seconds=float(raw["request_timeout_seconds"]),
    )


@dataclass(frozen=True)
class ReplyJudgement:
    """这一次判断的完整输出。

    一个实际值：

        ReplyJudgement(decision='replan',
                       reason="他说「可以是可以，不过别带众筹的」，后半句是在提条件")

    reason 不进 state：它是这一轮的东西，下一轮就作废，而且没有任何代码读它
    （唯一的消费者是日志和排查的人）。这跟 decision 必须占一个 state 字段正好相反 ——
    那个是条件边要读的，见 state.py 里 decision 那一段。
    """

    # 三选一，取值就是 DECISIONS 那三个。
    decision: str

    # 中文一句，凭哪几个字判的。判错的时候要靠它一眼看出是哪个字被误读了。
    reason: str


# 交给 OpenAI 的 json_schema。两条硬要求跟另外两份一样：每一层 additionalProperties
# 都是 false，required 列全每一个键。
#
# decision 写成 enum，模型就没机会返回第四种值：第四种值会让条件边那张表 KeyError，
# 而那要等用户真回话那一刻才发作。三个值开头各不相同（run / replan / cancel 分出来
# 最晚在第二个字母），不会像以前的采集窗口那样被前缀带到另一个值上（见 time_window.py 开头）。
# parse_reply 里还会再查一遍 —— schema 是对方守的，那一遍是
# 我们自己守的。
REPLY_SCHEMA: dict[str, Any] = {
    "name": "reply_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "reason"],
        "properties": {
            "decision": {"type": "string", "enum": list(DECISIONS)},
            "reason": {"type": "string"},
        },
    },
}


class ReplyCallFailed(RuntimeError):
    """这一次判断没拿到能用的回答。

    跟 IntentCallFailed / PlannerCallFailed 同构，六种情况共用一个类型。
    不在这一层重试：temperature 是 0，同样的输入拿回同样的坏回答。

    抛出去会让这一次 invoke 整个失败。对用户来说那意味着他刚回的那句话白说了 ——
    不完全白说：await_go 那一步的写已经落库了，他那句原话在 messages 里，
    再说一次就接着往下走。
    """


def build_reply_messages(
    lines: Sequence[str], plan: Mapping[str, Any], *, feedback: str
) -> list[dict[str, str]]:
    """把要判的那一句、他正看着的那版方案、和整条对话，拼成喂给模型的那两条消息。

    输入：state["plan_feedback"]（要判的就是这一句）；state["plan"]；
          state["messages"]，带说话人前缀的每一句原话
    输出：OpenAI chat 格式的消息列表，永远是两条（system + user）

    一个实际值：

        输入   lines=["用户: 帮我找找骨传导耳机的初创",
                      "AI: 方案 v1，1 条 query，预计 $0.08。我理解的需求：…",
                      "用户: 行吧就这样"]
               plan={"version": 1,
                     "queries": [{"text": "Kickstarter AND bone conduction",
                                  "filters": {}}],
                     "assumptions": "我理解的需求：…",
                     "estimated_cost_usd": 0.08}
               feedback="行吧就这样"
        输出   [{'role': 'system', 'content': '我们刚给一位同事看了一版检索方案...'},
                {'role': 'user', 'content': 一份 JSON，见下}]

               那份 JSON 摊开是：
                   {"plan_feedback": "行吧就这样",
                    "conversation": ["用户: 帮我找找骨传导耳机的初创",
                                     "AI: 方案 v1，1 条 query，预计 $0.08。...",
                                     "用户: 行吧就这样"],
                    "plan": {"version": 1,
                             "queries": ["Kickstarter AND bone conduction"],
                             "estimated_cost_usd": 0.08}}

    三个键各是 state 里的哪一格：plan_feedback 和 plan 照抄 state 的字段名，
    conversation 是 state['messages'] —— 这一格不照抄，是因为 messages 这个词在这个
    文件里已经被占了（这个函数的名字、它的返回值、read_decision 里那个局部变量，
    说的都是 OpenAI 那份 system/user 列表）。plan 这边不用叫 previous_plan：
    这个文件不产出方案，不会跟别的混（planner 那边就得叫，那次的产出本身就是一版新的）。

    plan_feedback 为什么单独给，而不是让模型自己取 conversation 的最后一句：

        这里跟 understand_intent 那边买的不是同一样东西。那边要回答的是「最后这一句
        到底是不是对方案的意见」—— 开场白、答一句澄清、对方案提意见，三种在
        conversation 里长得一模一样，那个问题光看最后一句答不了。
        这边不存在那个歧义（这个节点只有一条入边，就是 await_go），要买的是另一件事：
        把「要判的是哪一句」从一条位置约定变成一份明写的输入。
        写成约定的话，它只活在 prompt 那句「判的是最后那一句」里 —— 哪天有人从别处
        接一条边进来，模型会去判一句不该判的话，而这件事不报错。

    conversation 照样全给，不是二选一：他上一句刚提了一个条件、这一句只回一个「好」
    的时候，那个「好」是在确认那个条件，不是在说整版方案可以跑了。只看要判的那一句
    分不出这两种。

    方案只给三样，不给整份：

        version              让「这是第几版」看得见。他说「还是第一版那个吧」时用得上
        queries              只给正文，不给 filters。他是对着这几句话拍板的
        estimated_cost_usd   他犹豫的时候八成是在犹豫这个数

        assumptions 不给：那一份是我们自己写给他看的清单，再喂回模型只会让它跟着
        复述我们的措辞，而它要判的是他说了什么。
    """
    #        payload 里的键名全用英文，跟另外两次调用一个口径。
    #        值里面的中文原样留着 —— 那正是要判的东西。
    #        plan_feedback 排在最前面：它是这次调用的主语，另外两样是上下文
    payload = {
        "plan_feedback": feedback,
        "conversation": list(lines),
        "plan": {
            "version": plan.get("version", 0),
            "queries": [str(item.get("text", "")) for item in plan.get("queries") or ()],
            "estimated_cost_usd": plan.get("estimated_cost_usd", 0.0),
        },
    }

    # 输出   两条消息。ensure_ascii 关掉，中文原样进去 —— 开着的话整段对话会变成
    #        一串转义码，人在日志里读不懂
    return [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_reply(payload: Mapping[str, Any]) -> ReplyJudgement:
    """把模型返回的那份 JSON 解析成 ReplyJudgement。

    输入：json.loads 出来的那个字典
    输出：ReplyJudgement。字段不对或者取值不在闭集里，当场抛 ReplyCallFailed

    两个实际值：

        输入   {"decision": "run", "reason": "他说「跑」，没有附加条件"}
        输出   ReplyJudgement(decision='run', reason='他说「跑」，没有附加条件')

        输入   {"decision": "RUN", "reason": "..."}
        输出   不返回，抛 ReplyCallFailed

    取值在 schema 里已经是 enum 了，这里再查一遍不是重复：schema 是对方守的规矩，
    这一遍是我们自己守的。真漏过来一个第四种值，条件边那张表会 KeyError，而那句
    报错说不出它是从哪儿来的。更要紧的是方向 —— 一个没见过的取值如果在这里被
    「善意地」折成 run，那就是静默花钱。

    缺键当场抛，不给默认值：strict 的 json_schema 下这两个键必然在，缺了只能是
    REPLY_SCHEMA 和这个函数漂了 = 我们的 bug。
    """
    try:
        decision = str(payload["decision"]).strip()
        reason = str(payload["reason"]).strip()
    except (KeyError, TypeError) as err:
        raise ReplyCallFailed(f"返回里少了字段：{err}。整份回答前 300 字：{str(payload)[:300]}") from err

    if decision not in DECISIONS:
        raise ReplyCallFailed(f"decision 是 {decision!r}，合法值只有 {DECISIONS}")

    # 输出   一份冻结的判断。decision 往下走进 state，reason 只进日志
    return ReplyJudgement(decision=decision, reason=reason)


def read_decision(
    client: Any,
    lines: Sequence[str],
    plan: Mapping[str, Any],
    *,
    feedback: str,
    config: ReplyConfig,
) -> ReplyJudgement:
    """调一次模型，判用户刚回的那句话是要改、要跑、还是不跑了。这一步花钱（很少）。

    输入：
        client   一个能 chat 的对象。真跑时是 openai.OpenAI，测试里是假客户端
        lines    state["messages"]，这条对话说过的每一句原话
        plan     state["plan"]，他正看着的那一版
        feedback state["plan_feedback"]，要判的就是这一句。必填，不给默认值：
                 漏传的后果是模型只能靠「取最后一句」这条位置约定去找它
        config   模型、max_tokens、温度、超时

    输出：ReplyJudgement

    一个实际值：

        输入   feedback="行吧就这样"（lines 最后一句是 "用户: 行吧就这样"）
        输出   ReplyJudgement(decision='run', reason='「行吧就这样」是认可，没有附加条件')

    对库的写：一笔都没有。真正写 state 的是调它的那个节点 return 的时候
    （见 nodes.classify_reply）。

    六种拿不到好回答的情况全部抛 ReplyCallFailed。不在这一层重试，也不在这一层
    退回关键词表：退回去是静默降级，而降级之后判错的那一次是要花钱的。宁可让用户
    再说一句话。
    """
    #        messages 拼在 try 外面：拼的时候抛异常会被下面报成「请求失败」，
    #        把我们自己的 bug 伪装成网络故障
    messages = build_reply_messages(lines, plan, feedback=feedback)

    try:
        # 输出   一次 HTTP 请求。超时是我们的、重试是 SDK 的
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": REPLY_SCHEMA},
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout_seconds,
        )
    except Exception as err:
        raise ReplyCallFailed(f"请求失败：{type(err).__name__}: {err}") from err

    #        choice 是唯一那个候选回答（没要 n>1）
    choice = response.choices[0]

    if choice.finish_reason == "length":
        raise ReplyCallFailed(
            f"输出被 max_tokens={config.max_tokens} 截断。一次判断只有一个词加一句理由，"
            f"截断说明它在长篇大论，调 config/interactive.yaml 的 reply.max_tokens"
        )

    # refusal 是结构化输出下模型拒答时才有值的字段，正常是 None
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise ReplyCallFailed(f"模型拒答：{refusal}")

    content = choice.message.content
    if content is None:
        raise ReplyCallFailed(f"模型没有返回内容，finish_reason={choice.finish_reason}")

    #        payload 是那份 JSON。strict schema 下它应该永远合法，
    #        真不合法的时候要把原文带上
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as err:
        raise ReplyCallFailed(f"返回的不是合法 JSON：{err}。原文前 200 字：{content[:200]}") from err

    # 合法 JSON 不等于一个对象：null、[]、3 都能解析成功
    if not isinstance(payload, dict):
        raise ReplyCallFailed(f"返回的 JSON 不是一个对象，是 {type(payload).__name__}：{content[:200]}")

    # 输出   解析成 ReplyJudgement。取值不在闭集里在这一步炸
    return parse_reply(payload)
