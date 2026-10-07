"""classify_feedback 那一步：用户看完稿子回的一句话，拆到每一份，再由代码推出下一步。

两半：
    read_feedback    调一次模型，把一句话拆成每份的判决（approve / drop / change）、
                     每份要改的话分成「动计划的」和「动正文的」两半，外加要新加的、管整轮的
    apply_feedback   纯函数：判决落到账本上、拼 plan_feedback、记 verdicts、按顺序推 decision

拆开的两半去处不一样（讲解见 langgraph学习/drafting/replan_rewrite_1a.txt 第 4 步）：
    动计划的话（换人、换写法、换邮箱）  -> 这份 replan，那句话按份拼进 plan_feedback，交给 plan_drafts
    只动正文的话（短一点、别提什么）    -> 这份 rewrite，那句话放进账本这一份的 feedback，交给 write_draft
plan_drafts 一次定整轮（各份的计划互相影响），所以计划那半拼成一条；
write_draft 每份单独写，所以正文那半按份放。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from triggers.interactive_discovery.drafting.planner import (
    APPROVED,
    DROPPED,
    FORM_TEXT,
    PLANNED,
    REPLAN,
    REWRITE,
    WRITTEN,
    company_line,
    copy_entry,
    person_line,
)
from triggers.interactive_discovery.drafting.prompts import FEEDBACK_SYSTEM

# 往上四层到仓库根：drafting/ -> interactive_discovery/ -> triggers/ -> 仓库根
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "drafting.yaml"

# 五个去向，也是 graph.route_feedback 那张表的全部键（有测试对着）。排在前面的先判：
# 越上游越先，回上游往下走照样会经过下游
DECISIONS = ("cancel", "replan", "rewrite", "wait", "done")

# 模型给每份的三种判决
VERDICTS = ("approve", "drop", "change")

# 给模型看的「到了哪一步」
_STATUS_TEXT = {APPROVED: "已定稿", WRITTEN: "写好了在等他说", DROPPED: "已不要"}


@dataclass(frozen=True)
class FeedbackConfig:
    """config/drafting.yaml 的 feedback 那一段。

    实际值：FeedbackConfig(model='gpt-4.1', max_tokens=800, temperature=0.0, request_timeout_seconds=30.0)
    """

    model: str
    max_tokens: int
    temperature: float
    request_timeout_seconds: float


def load_feedback_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> FeedbackConfig:
    """读 config/drafting.yaml 的 feedback 那一段。缺键当场 KeyError，不缓存。"""
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["feedback"]
    return FeedbackConfig(
        model=str(raw["model"]),
        max_tokens=int(raw["max_tokens"]),
        temperature=float(raw["temperature"]),
        request_timeout_seconds=float(raw["request_timeout_seconds"]),
    )


@dataclass(frozen=True)
class SlotVerdict:
    """模型对一份的判决。例：SlotVerdict(number=3, verdict="change",
    plan_change="改成给 Dana 发 LinkedIn 私信", content_change="")"""

    number: int
    verdict: str
    plan_change: str
    content_change: str


@dataclass(frozen=True)
class FeedbackJudgement:
    """模型拆完的整句。

    例（「第 1 份可以了。第 2 份不要了。第 3 份改成给 Dana 发 LinkedIn 私信。第 4 份短一点，别提众筹。
    再给 1-2 那个邮箱发一封。」）：
        FeedbackJudgement(cancel=False,
                          slots=(SlotVerdict(1, "approve", "", ""), SlotVerdict(2, "drop", "", ""),
                                 SlotVerdict(3, "change", "改成给 Dana 发 LinkedIn 私信", ""),
                                 SlotVerdict(4, "change", "", "短一点，别提众筹")),
                          add=("再给 1-2 那个邮箱发一封",), round_plan_change="", round_content_change="",
                          reason="…")
    reason 只进日志
    """

    cancel: bool
    slots: tuple[SlotVerdict, ...]
    add: tuple[str, ...]
    round_plan_change: str
    round_content_change: str
    reason: str


FEEDBACK_SCHEMA: dict[str, Any] = {
    "name": "draft_feedback",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["cancel", "slots", "add", "round_plan_change", "round_content_change", "reason"],
        "properties": {
            "cancel": {"type": "boolean"},
            "slots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["number", "verdict", "plan_change", "content_change"],
                    "properties": {
                        "number": {"type": "integer"},
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "plan_change": {"type": "string"},
                        "content_change": {"type": "string"},
                    },
                },
            },
            "add": {"type": "array", "items": {"type": "string"}},
            "round_plan_change": {"type": "string"},
            "round_content_change": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
}


class FeedbackCallFailed(RuntimeError):
    """这一次拆意见没拿到能用的回答。抛出去整次 invoke 失败，用户回任意一句重试，
    从 classify_feedback 第一行重跑（await_write 那一步已经落了存档，他那句话在 draft_feedback 里）。
    """


def _draft_rows(drafts: Mapping[str, Any], plan: Mapping[str, Any], lead: Mapping[str, Any]) -> list[dict]:
    """每一份给模型看的那一行：编号、写哪一种、写给谁、到了哪一步、第几版。不给正文。

    输出例：[{"number": 1, "form": "邮件", "to": "Dana Lee（Founder，这家公司的人）",
              "status": "已定稿", "version": 1}, ...]
    已不要的那份计划已经不在 plan 里了，写哪一种、写给谁取账本里存的它最后那版计划
    """
    slots = {slot["number"]: slot for slot in plan["slots"]}
    rows = []
    for key in sorted(drafts, key=int):
        entry = drafts[key]
        slot = slots.get(int(key)) or entry["slot"]
        if slot is None:
            form, to = "", ""
        else:
            form = FORM_TEXT[slot["form"]]
            to = person_line(slot["recipient"]) if slot["recipient"] is not None else company_line(lead)
        rows.append(
            {
                "number": int(key),
                "form": form,
                "to": to,
                "status": _STATUS_TEXT.get(entry["status"], entry["status"]),
                "version": entry["version"],
            }
        )
    return rows


def build_feedback_messages(
    lines: Sequence[str],
    *,
    feedback: str,
    drafts: Mapping[str, Any],
    plan: Mapping[str, Any],
    lead: Mapping[str, Any],
) -> list[dict[str, str]]:
    """拼喂给模型的两条消息：system + 一份 JSON（要拆的那句、每一份、在等的号、这一轮对话）。"""
    payload = {
        "draft_feedback": feedback,
        "drafts": _draft_rows(drafts, plan, lead),
        "waiting": sorted(int(k) for k, e in drafts.items() if e["status"] == WRITTEN),
        "conversation": list(lines),
    }
    return [
        {"role": "system", "content": FEEDBACK_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_feedback(payload: Mapping[str, Any], drafts: Mapping[str, Any]) -> FeedbackJudgement:
    """模型返回的 JSON 变成 FeedbackJudgement。号不在账本里、同一个号判了两次，当场抛。"""
    try:
        slots = tuple(
            SlotVerdict(
                number=item["number"],
                verdict=str(item["verdict"]),
                plan_change=str(item["plan_change"]).strip(),
                content_change=str(item["content_change"]).strip(),
            )
            for item in payload["slots"]
        )
        judgement = FeedbackJudgement(
            cancel=bool(payload["cancel"]),
            slots=slots,
            add=tuple(str(a).strip() for a in payload["add"] if str(a).strip()),
            round_plan_change=str(payload["round_plan_change"]).strip(),
            round_content_change=str(payload["round_content_change"]).strip(),
            reason=str(payload["reason"]).strip(),
        )
    except (KeyError, TypeError) as err:
        raise FeedbackCallFailed(f"返回里少了字段：{err}。整份回答前 300 字：{str(payload)[:300]}") from err
    numbers = [sv.number for sv in judgement.slots]
    for sv in judgement.slots:
        if str(sv.number) not in drafts:
            raise FeedbackCallFailed(f"判了第 {sv.number} 份，账本里只有 {sorted(drafts, key=int)}")
        if sv.verdict not in VERDICTS:
            raise FeedbackCallFailed(f"第 {sv.number} 份的 verdict 是 {sv.verdict!r}，只认 {VERDICTS}")
    if len(numbers) != len(set(numbers)):
        raise FeedbackCallFailed(f"同一份判了两次：{numbers}")
    return judgement


def read_feedback(
    client: Any,
    lines: Sequence[str],
    *,
    feedback: str,
    drafts: Mapping[str, Any],
    plan: Mapping[str, Any],
    lead: Mapping[str, Any],
    config: FeedbackConfig,
) -> FeedbackJudgement:
    """调一次模型拆这句话。这一步花钱。拿不到好回答抛 FeedbackCallFailed，认法同 planner.read_ai_plan。"""
    messages = build_feedback_messages(lines, feedback=feedback, drafts=drafts, plan=plan, lead=lead)
    try:
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": FEEDBACK_SCHEMA},
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout_seconds,
        )
    except Exception as err:
        raise FeedbackCallFailed(f"请求失败：{type(err).__name__}: {err}") from err
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise FeedbackCallFailed(
            f"输出被 max_tokens={config.max_tokens} 截断，调 config/drafting.yaml 的 feedback.max_tokens"
        )
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise FeedbackCallFailed(f"模型拒答：{refusal}")
    content = choice.message.content
    if content is None:
        raise FeedbackCallFailed(f"模型没有返回内容，finish_reason={choice.finish_reason}")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as err:
        raise FeedbackCallFailed(f"返回的不是合法 JSON：{err}。原文前 200 字：{content[:200]}") from err
    if not isinstance(payload, dict):
        raise FeedbackCallFailed(f"返回的 JSON 不是一个对象：{content[:200]}")
    return parse_feedback(payload, drafts)


@dataclass(frozen=True)
class Applied:
    """apply_feedback 的结果，都写进 state。

    例（接 FeedbackJudgement 那个例子）：
        drafts         #1 approved / #2 dropped / #3 replan / #4 rewrite（feedback「短一点，别提众筹」）
        plan           计划：一般原样；有要回来的那份时，代码把它从账本放回计划
        plan_feedback  "第 3 份：改成给 Dana 发 LinkedIn 私信\\n新加一份：再给 1-2 那个邮箱发一封"
        verdicts       [{"number": 1, "verdict": "approved", "feedback": ""}, ...,
                        {"number": 4, "verdict": "rewrite", "feedback": "短一点，别提众筹"}]
        decision       "replan"
        written_now    要回来、原样再给他看一遍的那几份的号；一般是 []
        notice         下一条消息开头要告诉他的一句；一般是 ""
    """

    drafts: dict[str, Any]
    plan: dict[str, Any]
    plan_feedback: str
    verdicts: list[dict[str, Any]]
    decision: str
    written_now: list[int]
    notice: str


def _join(*parts: str) -> str:
    """几句意见用「；」接起来，空的跳过。"""
    return "；".join(p for p in parts if p)


def apply_feedback(
    drafts: Mapping[str, Any], plan: Mapping[str, Any], judgement: FeedbackJudgement, *, limit: int
) -> Applied:
    """判决落到账本上，推出下一步。纯函数，返回新账本和新计划，不改传进来的那两份。

    输入：drafts（账本）、plan（计划）、judgement（模型拆好的）、limit（每轮最多几份，数活着的）
    每份：
        approve   written -> approved（已经定稿的不变）
        drop      -> dropped（已定稿的也可以不要）
        change    有 plan_change      -> replan；plan_change 进 plan_feedback，
                                         content_change 进这份的 feedback
                  只有 content_change -> rewrite；content_change 进这份的 feedback
                  已定稿的照样这样改，就是「重新打开」
    已经不要的那份被点到（他要它回来）：原号复活，代码把账本里存的那版计划放回计划，不经模型：
        approve                  有正文 -> approved（就用原来那版）；没写过 -> planned
        change 带 plan_change    -> replan（跟上面一样回 plan_drafts 改计划）
        change 只有 content      有正文 -> rewrite（在原稿上改）；没写过 -> planned
        change 两半都空          有正文 -> written，进 written_now（原样再给他看一遍）；没写过 -> planned
        活着的已经满 limit 份    不复活，notice 一句「先说不要哪一份」
        复活放在最后判：同一句话里先不要了别的，就空出了位置
    整轮：
        round_plan_change      进 plan_feedback 一行「整轮：…」
        round_content_change   抄进每份没定稿、没不要的 feedback；还在等的（written）跟着变 rewrite
    verdicts 只记这一句真动了、而且写过正文的那几份（库里一行一份）；cancel 时把还在等的那几份记成 cancelled
    """
    out = {key: copy_entry(entry) for key, entry in drafts.items()}
    slots = [dict(slot) for slot in plan["slots"]]
    verdicts: dict[int, dict[str, Any]] = {}
    plan_lines: list[str] = []
    shown_again: list[int] = []
    notices: list[str] = []

    def record(number: int, verdict: str, feedback: str) -> None:
        if out[str(number)]["version"]:
            verdicts[number] = {"number": number, "verdict": verdict, "feedback": feedback}

    if judgement.cancel:
        cancelled = [
            {"number": int(k), "verdict": "cancelled", "feedback": ""}
            for k in sorted(out, key=int)
            if out[k]["status"] == WRITTEN
        ]
        return Applied(
            drafts=out,
            plan=dict(plan),
            plan_feedback="",
            verdicts=cancelled,
            decision="cancel",
            written_now=[],
            notice="",
        )

    revive = [sv for sv in judgement.slots if out[str(sv.number)]["status"] == DROPPED]
    for sv in judgement.slots:
        if sv in revive:
            continue
        entry = out[str(sv.number)]
        if sv.verdict == "approve":
            if entry["status"] == WRITTEN:
                entry["status"] = APPROVED
                record(sv.number, "approved", "")
        elif sv.verdict == "drop":
            entry["status"] = DROPPED
            record(sv.number, "dropped", "")
        elif sv.plan_change:
            entry["status"] = REPLAN
            entry["feedback"] = sv.content_change
            plan_lines.append(f"第 {sv.number} 份：{sv.plan_change}")
            record(sv.number, "replan", _join(sv.plan_change, sv.content_change))
        else:
            if not sv.content_change:
                print(f"[看稿] 第 {sv.number} 份判成 change，两半都是空的")
            entry["status"] = REWRITE
            entry["feedback"] = sv.content_change
            record(sv.number, "rewrite", sv.content_change)

    # 要回来的：放在最后，同一句里先不要了别的就空出了位置
    for sv in revive:
        entry = out[str(sv.number)]
        live = sum(1 for e in out.values() if e["status"] != DROPPED)
        if live >= limit:
            notices.append(
                f"第 {sv.number} 份要回来的话，一轮最多 {limit} 份（已定稿的也算），先说不要哪一份"
            )
            continue
        # 账本里存的那版计划放回计划，号不变；它在这一版 ai_plan 里没有原样，sources 清空
        if all(slot["number"] != sv.number for slot in slots):
            slots.append({**entry["slot"], "sources": []})
            slots.sort(key=lambda slot: slot["number"])
        written = bool(entry["version"])
        if sv.plan_change:
            entry["status"] = REPLAN
            entry["feedback"] = sv.content_change
            plan_lines.append(f"第 {sv.number} 份：{sv.plan_change}")
            record(sv.number, "replan", _join(sv.plan_change, sv.content_change))
        elif not written:
            entry["status"] = PLANNED
            entry["feedback"] = sv.content_change
        elif sv.verdict == "approve":
            entry["status"] = APPROVED
            record(sv.number, "approved", "")
        elif sv.content_change:
            entry["status"] = REWRITE
            entry["feedback"] = sv.content_change
            record(sv.number, "rewrite", sv.content_change)
        else:
            entry["status"] = WRITTEN
            shown_again.append(sv.number)
        print(f"[看稿] 第 {sv.number} 份要回来，原号复活成 {entry['status']}")

    plan_lines.extend(f"新加一份：{words}" for words in judgement.add)
    if judgement.round_plan_change:
        plan_lines.append(f"整轮：{judgement.round_plan_change}")
    if judgement.round_content_change:
        for key, entry in out.items():
            if entry["status"] in (APPROVED, DROPPED):
                continue
            entry["feedback"] = _join(entry["feedback"], judgement.round_content_change)
            number = int(key)
            if entry["status"] == WRITTEN:
                entry["status"] = REWRITE
                if number in shown_again:
                    shown_again.remove(number)
                record(number, "rewrite", entry["feedback"])
            elif number in verdicts:
                verdicts[number]["feedback"] = _join(
                    verdicts[number]["feedback"], judgement.round_content_change
                )

    statuses = {entry["status"] for entry in out.values()}
    if plan_lines:
        decision = "replan"
    elif statuses & {REWRITE, PLANNED}:
        decision = "rewrite"
    elif WRITTEN in statuses:
        decision = "wait"
    else:
        decision = "done"
    return Applied(
        drafts=out,
        plan={**plan, "slots": slots},
        plan_feedback="\n".join(plan_lines),
        verdicts=[verdicts[n] for n in sorted(verdicts)],
        decision=decision,
        written_now=sorted(shown_again),
        notice="；".join(notices),
    )
