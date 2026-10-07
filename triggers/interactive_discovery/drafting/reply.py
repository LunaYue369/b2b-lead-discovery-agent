"""classify_plan_reply 那一步：用户对着计划回的那句话，是要改、同意、还是不写了。

跟 query/reply.py 同一个摆法：一次 LLM 调用的几个面放在一个文件里 —— 配置、schema、拼消息、解析。
那边讲过的（为什么不用关键词表、为什么缺键就炸、为什么不重试）这里不重复。

偏向跟 query 那边一样朝一个方向：拿不准就判 revise。判错成 approve 的后果是照一份用户没点头的计划
写好几份稿，判错成 revise 只是多问一轮。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from triggers.interactive_discovery.drafting.prompts import REPLY_SYSTEM

# 往上四层到仓库根：drafting/ -> interactive_discovery/ -> triggers/ -> 仓库根
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "drafting.yaml"

# 三个取值，也是 graph.route_plan_reply 那张表的全部键，两处必须是同一套（有测试对着）。
#   revise    要改：回 plan_drafts 重定一版
#   approve   同意：照这份写
#   cancel    不写了
DECISIONS = ("revise", "approve", "cancel")


@dataclass(frozen=True)
class ReplyConfig:
    """config/drafting.yaml 的 reply 那一段。

    实际值（仓库里那份配置）：

        ReplyConfig(model='gpt-4o-mini', max_tokens=200, temperature=0.0, request_timeout_seconds=15.0)
    """

    model: str
    max_tokens: int
    temperature: float
    request_timeout_seconds: float


def load_reply_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> ReplyConfig:
    """读 config/drafting.yaml 的 reply 那一段。

    输入：配置文件路径（不给就是仓库根的 config/drafting.yaml）
    输出：ReplyConfig，值见那个类

    缺键当场 KeyError，不给默认值；不缓存。理由同 query/reply.load_reply_config
    """
    # 输入   整份 yaml 里 reply 那一段
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["reply"]
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

        ReplyJudgement(decision="revise", reason="他说「第 2 份写给 3 号」，是在改收件人")

    reason 不进 state：没有任何代码读它，唯一的消费者是日志和排查的人
    """

    # 三选一，取值就是 DECISIONS 那三个
    decision: str
    # 中文一句，凭哪几个字判的
    reason: str


# 交给 OpenAI 的 json_schema。decision 写成 enum，模型没机会返回第四种值
REPLY_SCHEMA: dict[str, Any] = {
    "name": "plan_reply_decision",
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
    """这一次判断没拿到能用的回答。几种情况共用一个类型，理由同 query/reply.ReplyCallFailed。

    抛出去会让这一次 invoke 整个失败，用户看到「出错了，回任意一句重试」。await_plan 那一步的写
    已经落库了，他那句话在 messages 和 plan_feedback 里，重试时从 classify_plan_reply 第一行重跑
    """


def build_reply_messages(lines: Sequence[str], *, feedback: str) -> list[dict[str, str]]:
    """把要判的那一句、和这一轮的对话，拼成喂给模型的两条消息。

    输入：
        lines      这一轮的 messages（messages[round_start:]），最后两句是计划和他的回话，例
                   ["用户: 给 3 号写封邮件", "AI: 好，给 North Field Technologies 写这 1 份：…", "用户: 写吧"]
        feedback   state["plan_feedback"]，要判的就是这一句，例 "写吧"
    输出：
        [{"role": "system", "content": REPLY_SYSTEM},
         {"role": "user",
          "content": '{"plan_feedback": "写吧", "conversation": ["用户: 给 3 号写封邮件", …]}'}]

    计划不另外给：它就是 conversation 里那句「AI: 好，给 X 写这 N 份：…」，用户对着的正是那一段字。
    对话照样全给：他上一句刚提了一个条件、这一句只回一个「好」时，那个「好」是在确认那个条件
    """
    # 键名用英文，值里的中文原样留着；plan_feedback 排最前面，它是这次调用的主语
    payload = {"plan_feedback": feedback, "conversation": list(lines)}
    return [
        {"role": "system", "content": REPLY_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_reply(payload: Mapping[str, Any]) -> ReplyJudgement:
    """把模型返回的那份 JSON 解析成 ReplyJudgement。

    输入 -> 输出：
        {"decision": "approve", "reason": "他说「写吧」，没有附加条件"}
            -> ReplyJudgement(decision="approve", reason="他说「写吧」，没有附加条件")
        {"decision": "APPROVE", "reason": "…"} -> 抛 ReplyCallFailed

    取值再查一遍（schema 已经是 enum）：schema 是对方守的，这一遍是我们自己守的。
    缺键当场抛：strict schema 下不该发生，发生了是 REPLY_SCHEMA 和这里漂了
    """
    try:
        # ai理解出来的decision
        decision = str(payload["decision"]).strip()
        # ai给的理由它为什么这么理解
        reason = str(payload["reason"]).strip()
    except (KeyError, TypeError) as err:
        raise ReplyCallFailed(f"返回里少了字段：{err}。整份回答前 300 字：{str(payload)[:300]}") from err
    if decision not in DECISIONS:
        raise ReplyCallFailed(f"decision 是 {decision!r}，合法值只有 {DECISIONS}")
    return ReplyJudgement(decision=decision, reason=reason)


def read_decision(client: Any, lines: Sequence[str], *, feedback: str, config: ReplyConfig) -> ReplyJudgement:
    """调一次模型，判用户对着计划回的那句话是要改、同意、还是不写了。这一步花钱（很少）。

    输入：
        client     一个能 chat 的对象。真跑时是 openai.OpenAI，测试里是假客户端
        lines      这一轮的 messages
        feedback   state["plan_feedback"]
        config     模型、max_tokens、温度、超时
    输出：ReplyJudgement，例 ReplyJudgement(decision="approve", reason="「写吧」是同意，没有附加条件")

    对库的写：一笔都没有，写 state 的是调它的节点。几种拿不到好回答的情况全部抛 ReplyCallFailed，
    怎么认的跟 query/reply.read_decision 一样
    """
    # 拼在 try 外面：拼的时候抛的异常是我们的 bug，不该被报成「请求失败」
    messages = build_reply_messages(lines, feedback=feedback)
    # 调用ai来判断对用户的回复进行判断
    try:
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

    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise ReplyCallFailed(
            f"输出被 max_tokens={config.max_tokens} 截断，调 config/drafting.yaml 的 reply.max_tokens"
        )
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise ReplyCallFailed(f"模型拒答：{refusal}")
    content = choice.message.content
    if content is None:
        raise ReplyCallFailed(f"模型没有返回内容，finish_reason={choice.finish_reason}")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as err:
        raise ReplyCallFailed(f"返回的不是合法 JSON：{err}。原文前 200 字：{content[:200]}") from err
    if not isinstance(payload, dict):
        raise ReplyCallFailed(f"返回的 JSON 不是一个对象，是 {type(payload).__name__}：{content[:200]}")

    # 包装成ReplyJudgement
    return parse_reply(payload)
