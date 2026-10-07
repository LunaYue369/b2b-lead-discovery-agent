"""write_draft 那一步：照计划写一份稿，写完用代码查一遍，不过关带着问题重写。一份调一次模型。

一次调用的几个面放在一个文件里：配置、schema、拼消息、调用、代码检查。跟 planner.py 同一个路数
（为什么缺键就炸、为什么调用失败不自己重试），那边讲过的这里不重复。

代码检查只查确定的事，不让模型自审（Alex 9-30 定的）：
    数字        稿子里的每个数字都要能在 facts.json 对外那几格、证据、公司情况、用户说要强调的里找到
    附言长度    LinkedIn 加好友附言不超过 300 字符
    假装偶遇    开场引的证据不是收件人本人说的，不许写「看到你发的帖子」这类
    称呼        写给人的第一行要有他的名字；写给公司的第一行不许出现名单上别人的名字
    空          正文是空的；邮件没有标题
不过关的那一份带着问题重写，最多 max_retries 次；还不过就照样交出去，问题挂在账本的 problems 上，
发稿时提醒用户。
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from triggers.interactive_discovery.drafting.planner import (
    ARTICLE,
    EMAIL,
    FORM_TEXT,
    INSIDERS,
    LINKEDIN_NOTE,
    REWRITE,
    WRITTEN,
    Plan,
    PlanSlot,
    company_line,
    evidence_line,
    listed_people,
    person_line,
    selling_point_text,
)
from triggers.interactive_discovery.drafting.prompts import WRITE_SYSTEM

# 往上四层到仓库根：drafting/ -> interactive_discovery/ -> triggers/ -> 仓库根
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "drafting.yaml"

# 证据不是收件人本人说的时候，正文里不许出现的说法（小写比）。
# 例：Sam Park 发帖提到了 Acme Audio，写给 Dana 的信说「saw your post」就是装作看到了 Dana 的帖子
_FAKE_COINCIDENCE = (
    "your post",
    "your recent post",
    "your linkedin post",
    "you posted",
    "you shared",
    "saw your",
    "noticed your post",
    "read your post",
    "你发的",
    "看到你",
    "刷到你",
)

# facts.json 每条里不给写稿模型看的格子：why_it_matters 是我们内部的理由（「大多数中国 ODM 起订 1000-5000 台」
# 那种，写进开发信就成了对同行的断言），source 是官网文件路径。其余（claim、具体数据、note 写法规矩）都给
_INTERNAL_FACT_FIELDS = ("why_it_matters", "source")

# 数字：1,000 / 2.4 / 500 / 15。拿去比之前去掉千分位逗号和末尾的点
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")


@dataclass(frozen=True)
class WriteConfig:
    """config/drafting.yaml 的 write 那一段。

    实际值：WriteConfig(model='gpt-4.1', max_tokens=1500, temperature=0.3, request_timeout_seconds=60.0,
                        max_retries=2, note_limit=300)
    """

    model: str
    max_tokens: int
    temperature: float
    request_timeout_seconds: float
    # 代码检查不过关时最多重写几次
    max_retries: int
    # LinkedIn 加好友附言的字符上限
    note_limit: int


def load_write_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> WriteConfig:
    """读 config/drafting.yaml 的 write 那一段。缺键当场 KeyError，不缓存。"""
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["write"]
    return WriteConfig(
        model=str(raw["model"]),
        max_tokens=int(raw["max_tokens"]),
        temperature=float(raw["temperature"]),
        request_timeout_seconds=float(raw["request_timeout_seconds"]),
        max_retries=int(raw["max_retries"]),
        note_limit=int(raw["note_limit"]),
    )


# 交给 OpenAI 的 json_schema。subject 是邮件标题 / 文章标题，LinkedIn 两种填空串
WRITE_SCHEMA: dict[str, Any] = {
    "name": "draft_text",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["subject", "body"],
        "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
    },
}


class WriteCallFailed(RuntimeError):
    """这一次写稿调用没拿到能用的回答（请求失败、被截断、拒答、没内容、不是 JSON、缺键）。

    抛出去整次 invoke 失败，用户看到「出错了，回任意一句重试」，重试从 write_draft 第一行重跑。
    代码检查不过关不抛这个：那种带着问题重写，见 write_one
    """


def public_fact(entry: Mapping[str, Any]) -> dict[str, Any]:
    """facts.json 的一条去掉内部那几格（_INTERNAL_FACT_FIELDS），剩下的给写稿模型、也是数字检查的出处。

    例：{"value": 500, "unit": "units", "claim": "minimum order quantity of 500 units",
         "why_it_matters": [...]}
        -> {"value": 500, "unit": "units", "claim": "minimum order quantity of 500 units"}
    """
    return {k: v for k, v in entry.items() if k not in _INTERNAL_FACT_FIELDS}


def _opening(slot: PlanSlot, knowledge: Mapping[str, Any]) -> dict[str, Any] | None:
    """开场引的那条证据；不引是 None。编号 parse_ai_plan 查过，一定在 knowledge 里。"""
    if not slot.opening:
        return None
    return next(e for e in knowledge["evidence"] if e["signal_id"] == slot.opening)


def _is_recipients_own(slot: PlanSlot, evidence: Mapping[str, Any]) -> bool:
    """这条证据是不是收件人自己发的帖子：发帖的人就是收件人。

    只看是不是同一个人，不看 first_person（发帖人是不是这家公司的人）：写给发帖人本人时，
    哪怕他不是这家的人（例 James Diaz 发帖提到 Hollis Audio），「看到你的帖子」也是真话
    """
    return slot.recipient is not None and evidence["author"] == slot.recipient["name"]


def _brief_text(slot: PlanSlot, plan: Plan, lead: Mapping[str, Any], knowledge: Mapping[str, Any]) -> str:
    """这一份怎么写，写成给模型看的一段：写给谁、写哪一种、语言语气、强调、卖点、开场、这家的情况。

    输出（编的例子，折了行）：
        "写哪一种：邮件\\n写给：Dana Lee（Founder，这家公司的人），称呼用她的名 Dana\\n语言：英文\\n
         语气：简短、专业，不用推销腔\\n要强调（他说的，一定要写进去）：MOQ 500\\n
         卖点（只能从下面这几样里说）：\\n- 起订量低（MOQ）：{"value": 500, ...}\\n
         开场引这条证据（收件人本人说的，可以说看到了她的帖子）：Dana Lee（…）…：We just wrapped ...\\n
         这家公司：Acme Audio\\n概况：{...}\\n为什么找他们：Acme Audio just finished a Kickstarter ..."
    """
    if slot.recipient is not None:
        first = slot.recipient["name"].split()[0]
        whom = f"{person_line(slot.recipient)}，称呼用名 {first}"
    elif slot.form == ARTICLE:
        whom = f"不写给具体的人，是把我们的卖点套到 {lead['display_name']} 身上写的一篇文章"
    else:
        whom = f"{company_line(lead)}，称呼写「{lead['display_name']} team」"
    lines = [
        f"写哪一种：{FORM_TEXT[slot.form]}",
        f"写给：{whom}",
        f"语言：{plan.language}",
        f"语气：{plan.tone}",
    ]
    # 写给外人（发帖提到这家、但不是这家的人）：他不是买家，要他帮的是引荐（Alex 10-01 定：两样都问）
    if slot.recipient is not None and slot.recipient["relation"] not in INSIDERS:
        lines.append(
            f"这一份的目的：他不是 {lead['display_name']} 的人，是发帖提到了这家。不要把他当买家推销。"
            f"先问他能不能把我们引荐给 {lead['display_name']} 负责产品或采购的人；"
            "再顺带问一句，他认不认识别的正在找音频或语音设备代工厂的品牌、团队"
        )
    if slot.emphasis:
        lines.append(f"要强调（他说的，一定要写进去）：{'；'.join(slot.emphasis)}")
    lines.append("卖点（关于我们只能说下面这几样，数字照抄；note 是写法规矩，照着守，不要写进稿子）：")
    lines.extend(
        f"- {selling_point_text(key)}：{json.dumps(public_fact(knowledge['facts'][key]), ensure_ascii=False)}"
        for key in slot.selling_points
    )
    evidence = _opening(slot, knowledge)
    if evidence is None:
        lines.append("开场：不引帖子")
    else:
        if _is_recipients_own(slot, evidence):
            whose = "收件人本人说的，可以说看到了他的帖子"
        else:
            whose = "不是收件人本人说的，要说清消息从哪来，不许装作看到了收件人的帖子"
        text = evidence["text"]
        if evidence["reposted_text"]:
            text += f"（转发了 {evidence['reposted_author']} 的：{evidence['reposted_text']}）"
        lines.append(f"开场引这条证据（{whose}）：{evidence_line(evidence)}：{text}")
    profile = knowledge["profile"]
    lines += [
        f"这家公司：{lead['display_name']}",
        "概况：" + (json.dumps(profile, ensure_ascii=False) if profile else "（没抓过）"),
        f"为什么找他们：{knowledge['assessment']['analysis']}",
    ]
    if knowledge["avoid"]:
        lines.append("别这样写：")
        lines.extend(f"- {note['statement']}" for note in knowledge["avoid"])
    return "\n".join(lines)


def build_write_messages(
    slot: PlanSlot,
    plan: Plan,
    lead: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    *,
    previous_text: str,
    feedback: str,
    problems: Sequence[str],
) -> list[dict[str, str]]:
    """拼喂给模型的消息。

    输入：
        slot / plan / lead / knowledge   这一份的计划、整轮的语言语气、这家公司、证据和 facts
        previous_text   改正文（rewrite）时的上一版；照计划从头写是 ""（计划变了，旧稿会带偏）
        feedback        他对这一份的正文意见，例「短一点，别提众筹」；没有是 ""
        problems        上一次写的没过代码检查的那几条；第一次写是 ()
    输出：[system, 这一份怎么写, (上一版 + 意见), (上一次没过的检查)]
    """
    messages = [
        {"role": "system", "content": WRITE_SYSTEM},
        {"role": "user", "content": _brief_text(slot, plan, lead, knowledge)},
    ]
    if previous_text:
        messages.append(
            {
                "role": "user",
                "content": f"上一版：\n{previous_text}\n\n他的意见：{feedback}\n\n在上一版上改，只改他说的。",
            }
        )
    elif feedback:
        messages.append({"role": "user", "content": f"他对这一份的意见（写的时候照着做）：{feedback}"})
    if problems:
        listed = "\n".join(f"- {p}" for p in problems)
        messages.append({"role": "user", "content": f"上一次写的没过检查，重写时改掉：\n{listed}"})
    return messages


def compose_text(form: str, subject: str, body: str) -> str:
    """标题和正文拼成账本里的那一份 text，也是发给用户看、他复制走的那一份。

    输入 -> 输出：
        ("email", "Building your mic", "Hi Dana, ...")       -> "Subject: Building your mic\\n\\nHi Dana, ..."
        ("article", "Small batches", "...")                   -> "Small batches\\n\\n..."
        ("linkedin_note", "", "Hi Dana, ...")                 -> "Hi Dana, ..."
    """
    if form == EMAIL:
        return f"Subject: {subject}\n\n{body}"
    if form == ARTICLE and subject:
        return f"{subject}\n\n{body}"
    return body


def _numbers(text: str) -> set[str]:
    """一段话里的数字，去掉千分位逗号和末尾的点。例："1,000 units, 2.4 GHz." -> {"1000", "2.4"}"""
    return {match.replace(",", "").rstrip(".") for match in _NUMBER.findall(text)}


def check_draft(
    slot: PlanSlot,
    lead: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    subject: str,
    body: str,
    *,
    note_limit: int,
) -> list[str]:
    """代码检查。返回没过的那几条（给模型看的中文一句），都过了是 []。

    输入：这一份的计划、这家公司、knowledge、模型写的标题和正文、附言上限
    输出：例 ["数字 800 在 facts.json、证据和公司情况里都找不到", "附言 342 个字符，超过 300"]
    """
    problems: list[str] = []
    text = f"{subject}\n{body}"
    if not body.strip():
        return ["正文是空的"]
    if slot.form == EMAIL and not subject.strip():
        problems.append("邮件要有标题（subject）")

    # 数字：能找到出处的才许写。出处 = facts.json 对外那几格、证据全文、打分结论、公司概况、他说要强调的。
    # why_it_matters 里的数（1000-5000）不算：写稿模型本来就看不到它，写出来就是编的
    facts = {k: public_fact(v) for k, v in knowledge["facts"].items() if not k.startswith("_")}
    sources = [json.dumps(facts, ensure_ascii=False), json.dumps(knowledge["profile"])]
    sources.append(knowledge["assessment"]["analysis"])
    sources.extend(knowledge["assessment"]["quotes"])
    for e in knowledge["evidence"]:
        sources += [e["text"], e["reposted_text"] or "", e["when"], e["author"], e["role"]]
    sources.extend(slot.emphasis)
    # 名字和地址里的数字也算有出处：公司叫 3M、职位写着 Web3，称呼里出现不是编的
    sources += [lead["display_name"], slot.address or ""]
    sources += [f"{p['name']} {p['position'] or ''}" for p in lead["people"]]
    allowed = set().union(*(_numbers(s) for s in sources))
    unknown = sorted(_numbers(text) - allowed)
    if unknown:
        problems.append(f"数字 {'、'.join(unknown)} 在 facts.json、证据和公司情况里都找不到，不许写")

    if slot.form == LINKEDIN_NOTE and len(body) > note_limit:
        problems.append(f"加好友附言 {len(body)} 个字符，超过 {note_limit}，要压短")

    evidence = _opening(slot, knowledge)
    if evidence is not None and not _is_recipients_own(slot, evidence):
        lowered = body.casefold()
        found = [p for p in _FAKE_COINCIDENCE if p in lowered]
        if found:
            problems.append(
                f"开场那条证据不是收件人本人说的（是 {evidence['author']} 说的），不许写「{found[0]}」这种"
                "装作看到了收件人帖子的话，要说清消息从哪来"
            )

    if slot.form != ARTICLE:
        greeting = next((line for line in body.splitlines() if line.strip()), "").casefold()
        if slot.recipient is not None:
            first = slot.recipient["name"].split()[0].strip(".,").casefold()
            if first not in greeting:
                problems.append(f"第一行的称呼要写给 {slot.recipient['name']}（用名 {first}）")
        else:
            others = [p["name"].split()[0].strip(".,") for p in listed_people(lead)]
            wrong = [name for name in others if name.casefold() in greeting]
            if wrong:
                problems.append(f"这份写给 {lead['display_name']} 的团队，第一行的称呼里不该出现 {wrong[0]}")
    return problems


def _call(client: Any, messages: list[dict[str, str]], config: WriteConfig) -> tuple[str, str]:
    """调一次模型，返回 (标题, 正文)。拿不到好回答抛 WriteCallFailed，认法同 planner.read_ai_plan。"""
    try:
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": WRITE_SCHEMA},
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout_seconds,
        )
    except Exception as err:
        raise WriteCallFailed(f"请求失败：{type(err).__name__}: {err}") from err
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise WriteCallFailed(
            f"输出被 max_tokens={config.max_tokens} 截断，调 config/drafting.yaml 的 write.max_tokens"
        )
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise WriteCallFailed(f"模型拒答：{refusal}")
    content = choice.message.content
    if content is None:
        raise WriteCallFailed(f"模型没有返回内容，finish_reason={choice.finish_reason}")
    try:
        payload = json.loads(content)
        return str(payload["subject"]).strip(), str(payload["body"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError) as err:
        raise WriteCallFailed(f"返回的不是要的 JSON：{err}。原文前 200 字：{content[:200]}") from err


def write_one(
    client: Any,
    slot: PlanSlot,
    plan: Plan,
    lead: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    entry: Mapping[str, Any],
    config: WriteConfig,
) -> dict[str, Any]:
    """写一份稿：调模型、代码检查、不过关带着问题重写。返回账本里这一份的新那条。这一步花钱。

    输入：
        slot    这一份的计划
        entry   账本里这一份现在那条，例 {"status": "rewrite", "version": 1, "plan_version": 1,
                "text": "Subject: ...", "feedback": "短一点", "problems": []}
    输出：{"status": "written", "version": 2, "plan_version": 1, "text": "Subject: ...",
           "feedback": "", "problems": []}
          version +1；plan_version 记下照的是计划第几版；feedback 用完清空；
          重写了 max_retries 次还没过的，problems 是最后一次没过的那几条
    """
    previous_text = entry["text"] if entry["status"] == REWRITE else ""
    problems: list[str] = []
    for attempt in range(config.max_retries + 1):
        messages = build_write_messages(
            slot,
            plan,
            lead,
            knowledge,
            previous_text=previous_text,
            feedback=entry["feedback"],
            problems=problems,
        )
        subject, body = _call(client, messages, config)
        problems = check_draft(slot, lead, knowledge, subject, body, note_limit=config.note_limit)
        if not problems:
            break
        print(f"[写稿] 第 {slot.number} 份第 {attempt + 1} 次没过检查：{problems}")
    return {
        **entry,
        "status": WRITTEN,
        "version": entry["version"] + 1,
        "plan_version": slot.plan_version,
        "text": compose_text(slot.form, subject, body),
        "feedback": "",
        "problems": problems,
    }
