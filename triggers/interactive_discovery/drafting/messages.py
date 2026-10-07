"""写稿那张图发进 thread 的话。全是纯函数。

跟 delivery/conversation.py 的分工同 query/messages.py：那边是两张图共用的，这里只放写稿这张图自己的。
谁在用：

    drafting/slack_conversation._reply_for      render_interrupt / render_ended，一轮 invoke 跑完拼回复
    drafting/slack_conversation.drafting_graph  FINISHED_TEXT，对话已经结束之后再说话回的那一句
    drafting/nodes.await_plan                   plan_text，停 2 交出去、同时写进 messages 的那份计划
    drafting/nodes.await_write                  drafts_text / drafts_summary，停 3 交出去的稿子、
                                                写进 messages 的摘要
"""

from __future__ import annotations

from delivery.conversation import text_reply
from triggers.interactive_discovery.drafting.planner import (
    APPROVED,
    DROPPED,
    FORM_TEXT,
    PLANNED,
    REWRITE,
    WRITTEN,
    Plan,
    PlanSlot,
    evidence_line,
    form_text,
    plan_from_state,
    recipient_text,
    selling_point_text,
)

# 起草对话走到 END 之后，同一个 thread 里再说话、但没 @ bot 时回的那句。
# @ 了的会在同一份存档上从头再跑（slack_conversation._restart），所以这句话教他 @
FINISHED_TEXT = "这一轮起草已经结束了。要接着写，在这个 thread 里 @ 我，说这次要写什么。"

# 用户说不写了（decision=cancel）走到 END 回的那句
CANCELLED_TEXT = "好，这一轮不写了。要再写，在这个 thread 里 @ 我，说这次要写什么。"

# 计划最后那段：告诉他怎么回。三种回法对着 classify_plan_reply 的三个取值
_PLAN_HINT = (
    "要改就直接说，例如「第 2 份写给 3 号」「第 1 份改成私信」「再加一篇文章」；"
    "没问题回「写吧」；不写了回「算了」。"
)

# 看稿那一停最后那段：告诉他怎么回。对着 classify_feedback 能拆出来的几种
_DRAFTS_HINT = (
    "要改就直接说，例如「第 1 份短一点」「第 2 份不要了」「第 3 份改成私信」「再加一封发到 1-2」；"
    "都可以了回「都可以了」；不写了回「算了」。"
)

# 会交出来的 interrupt 的 kind。问的那句（第一问、计划、稿子）已经拼好放在 question 里，这里原样发
_QUESTION_KINDS = ("drafting_request", "drafting_plan", "drafting_write")


def render_interrupt(payload: dict) -> dict:
    """图停在 interrupt 上时，交出来的那个字典写成发回 thread 的一条消息。

    输入：payload，也就是 result["__interrupt__"][0].value。两种（编的例子）：
        ask_request 交出来的
            {"kind": "drafting_request",
             "question": "请问你想给 Acme Audio 写哪些 outreach 信息？一次可以写几份（最多 4 份），…"}
        await_plan 交出来的
            {"kind": "drafting_plan",
             "question": "好，给 Acme Audio 写这 2 份：\\n第 1 份：邮件，发到 dana@acme-audio.example → …"}
    输出：{"text": <question 原样>}

    问的那句是节点那边拼好的，这里原样发，不改一个字：写进 messages 的也是那一句，
    两处一样，下一个节点读对话记录时看到的就是用户看到的
    """
    if payload.get("kind") in _QUESTION_KINDS:
        return text_reply(payload["question"])
    # 图上加了新的 interrupt、这里没跟着加的话当场停：猜一种发出去，用户会看到一句答非所问的话
    raise ValueError(f"不认识的 interrupt：{payload!r}")


def _slot_mark(slot: PlanSlot, drafts: dict) -> str:
    """计划里每份号后面的那句状态。只在这一轮已经写过稿之后才有（第一次定计划都是空串）。

    输入 -> 输出（账本里这一份的状态）：
        approved                    -> "（已定稿，不动）"
        rewrite                     -> "（计划不变，按你说的改正文）"
        planned、写过（version > 0） -> "（计划改了）"
        planned、没写过、别的份写过  -> "（新加的）"
    """
    if not any(e["version"] for e in drafts.values()):
        return ""
    entry = drafts[str(slot.number)]
    if entry["status"] == APPROVED:
        return _APPROVED_MARK
    if entry["status"] == REWRITE:
        return "（计划不变，按你说的改正文）"
    if entry["status"] == PLANNED:
        return "（计划改了）" if entry["version"] else "（新加的）"
    return ""


_APPROVED_MARK = "（已定稿，不动）"


def round_lines(plan: Plan, lead: dict, knowledge: dict, drafts: dict | None = None) -> list[str]:
    """「这一轮写哪几份、每份怎么写」写成给人看的几行。plan_text 拿它拼 await_plan 交出去的那份计划。

    输入：Plan、state["lead"]（「这家公司本身」那句要用公司名）、state["knowledge"]（开场引的
          那条证据要写成人话）。一个例子（编的，Acme Audio，用户说「给 Dana 写封邮件强调 MOQ 500，
          再给 Sam 写一条加好友附言」，Sam 是提到这家的人）
    输出：
        ["第 1 份：邮件，发到 dana@acme-audio.example → Dana Lee（Founder，这家公司的人）",
         "  要强调（你说的）：MOQ 500",
         "  卖点（我定的）：起订量低（MOQ）",
         "  开场引：Dana Lee（Founder @ Acme Audio） 2026-09-20 的帖子，本人说的",
         "  为什么这样定：Dana 本人说要找工厂做 800 台，正好在我们起订量之上",
         "第 2 份：LinkedIn 加好友附言 → Sam Park（发帖的人不是这家公司的，…）",
         "  卖点（我定的）：…",
         "  开场不引帖子",
         "  Sam Park（发帖的人不是这家公司的，…），照样给他写",
         "语言：英文",
         "语气：简短、专业，不用推销腔",
         "说明：",
         "- 语言你没说，按默认用英文",
         "- 语气你没说，按默认：简短、专业，不用推销腔"]

    每份第一行分三种：用户说的照上面；写哪一种是 AI 定的，写法后面标「（写哪一种是我定的）」，
    下一行缩进「你说的是「给 Dana 写一份」」，让用户认得出是他说的哪一份；AI 补的整份，
    第一行写「第 3 份（我加的）：…」。
    每份自己的说明（换了邮箱、写给了外人、写给了这家公司、认不出）缩进挂在那一份下面，整轮的说明放最后
    """
    # 输入   knowledge["evidence"]   中间   evidence -> {2720: Dana 那条, 2733: Audio Weekly 那条}
    evidence = {e["signal_id"]: e for e in knowledge["evidence"]}
    drafts = drafts or {}
    lines: list[str] = []
    for slot in plan.slots:
        status = _slot_mark(slot, drafts)
        # 已定稿的只列一行：它不会再变，细节用户看稿时看过了
        if status == _APPROVED_MARK:
            lines.append(f"第 {slot.number} 份{status}：{form_text(slot)} → {recipient_text(slot, lead)}")
            continue
        # 输出   "第 1 份：邮件，发到 dana@… → Dana Lee（…）" / "第 3 份（我加的）：文章 → Acme Audio …"
        mark = "（我加的）" if not slot.user_words else ""
        decided = "（写哪一种是我定的）" if slot.ai_decides and slot.user_words else ""
        lines.append(
            f"第 {slot.number} 份{mark}{status}：{form_text(slot)}{decided} → {recipient_text(slot, lead)}"
        )
        # 写哪一种是 AI 定的，附上用户对这一份说的原话：他才认得出这是他说的哪一份
        if slot.ai_decides and slot.user_words:
            lines.append(f"  你说的是「{slot.user_words}」")
        if slot.emphasis:
            lines.append(f"  要强调（你说的）：{'；'.join(slot.emphasis)}")
        lines.append(f"  卖点（我定的）：{'；'.join(selling_point_text(k) for k in slot.selling_points)}")
        # 开场那条证据的编号 parse_ai_plan 已经查过，一定在 knowledge 里
        if slot.opening:
            lines.append(f"  开场引：{evidence_line(evidence[slot.opening], short=True)}")
        else:
            lines.append("  开场不引帖子")
        if slot.reason:
            lines.append(f"  为什么这样定：{slot.reason}")
        lines.extend(f"  {note}" for note in slot.notes)

    gone = sorted(int(k) for k, e in drafts.items() if e["status"] == DROPPED)
    if gone:
        lines.append(f"已不要：{'、'.join(f'第 {n} 份' for n in gone)}")
    lines.append(f"语言：{plan.language}")
    lines.append(f"语气：{plan.tone}")
    if plan.unsupported:
        lines.append(f"做不到的：{'；'.join(plan.unsupported)}（稿子只写不发，要你自己复制粘贴发出去）")
    if plan.notes:
        lines.append("说明：")
        lines.extend(f"- {note}" for note in plan.notes)
    return lines


def plan_text(values: dict) -> str:
    """这一轮的计划写成一条消息：开头一句、每份怎么写、最后告诉他怎么回。await_plan 交出去的就是它。

    输入：values，整份 state，用 plan / lead / knowledge 三格。例（编的）
          {"lead": {"display_name": "Acme Audio", ...}, "knowledge": {"evidence": [...], ...},
           "plan": {"slots": [{"number": 1, "form": "email", "address": "dana@acme-audio.example", ...}, ...],
                    "language": "英文", ..., "ai_plans_all": False}, ...}
    输出："好，给 Acme Audio 写这 2 份：\\n第 1 份：邮件，发到 dana@acme-audio.example → Dana Lee（…）\\n
           …\\n\\n要改就直接说，例如「第 2 份写给 3 号」…；没问题回「写吧」；不写了回「算了」。"
          用户一份都没说时开头是「你没说要写什么，我定了这 2 份：」

    读的是 plan_drafts 存下的那份（state["plan"]），不现算：见 planner.Plan。
    纯函数：await_plan 第一行交出去、恢复时写进 messages 各拼一次，两次一字不差
    """
    plan = plan_from_state(values["plan"])
    drafts = values["drafts"]
    body = "\n".join(round_lines(plan, values["lead"], values["knowledge"], drafts))
    name = values["lead"]["display_name"]
    count = len(plan.slots)
    if any(e["version"] for e in drafts.values()):
        head = "改好了，这一轮的计划："
    elif plan.ai_plans_all:
        head = f"你没说要写什么，我定了这 {count} 份："
    else:
        head = f"好，给 {name} 写这 {count} 份："
    return f"{head}\n{body}\n\n{_PLAN_HINT}"


def render_ended(values: dict) -> dict:
    """图走到 END 时，发回 thread 的那一条消息。两种，看 decision：

        "cancel"   用户说不写了（对着计划或对着稿子说的）-> CANCELLED_TEXT
        "done"     每份都定稿或不要了 -> 列出终稿是哪几份、各是第几版

    输入：values，这一轮 invoke 跑完之后的整份 state
    输出：例 {"text": "定稿 4 份：\n第 1 份 邮件 → Dana Lee（第 2 版）\n…\n
                       正文都在上面的消息里，复制发出去就行。…"}

    decision 是别的值说明图上加了一条通到 END 的路、这里没跟着加，当场停，不猜一句发出去
    """
    decision = values["decision"]
    if decision == "cancel":
        return text_reply(CANCELLED_TEXT)
    if decision == "done":
        plan = plan_from_state(values["plan"])
        drafts = values["drafts"]
        final = [slot for slot in plan.slots if drafts[str(slot.number)]["status"] == APPROVED]
        lines = [f"定稿 {len(final)} 份："]
        lines += [
            f"第 {slot.number} 份 {FORM_TEXT[slot.form]} → {_short_name(slot, values['lead'])}"
            f"（第 {drafts[str(slot.number)]['version']} 版）"
            for slot in final
        ]
        lines.append("正文都在上面的消息里，复制发出去就行。要再写，在这个 thread 里 @ 我。")
        return text_reply("\n".join(lines))
    raise ValueError(f"走到 END 时 decision 是 {decision!r}，只认 cancel / done")


def _short_name(slot: PlanSlot, lead: dict) -> str:
    """一份写给谁的短说法：人名，或者公司名。例 "Dana Lee" / "Acme Audio" """
    return slot.recipient["name"] if slot.recipient is not None else lead["display_name"]


def drafts_summary(values: dict) -> str:
    """看稿那一停写进 messages 的一句摘要，不带正文。

    输出例：
        这一次写了几份  "[稿子] 第 3 份 LinkedIn 私信→Dana Lee 第 2 版 / 第 5 份 邮件→Acme Audio 第 1 版"
        什么都没写      "[稿子] 等你说：第 4、5 份"
    """
    plan = {slot.number: slot for slot in plan_from_state(values["plan"]).slots}
    drafts = values["drafts"]
    if values["written_now"]:
        parts = [
            f"第 {n} 份 {FORM_TEXT[plan[n].form]}→{_short_name(plan[n], values['lead'])} "
            f"第 {drafts[str(n)]['version']} 版"
            for n in values["written_now"]
        ]
        return "[稿子] " + " / ".join(parts)
    waiting = sorted(int(k) for k, e in drafts.items() if e["status"] == WRITTEN)
    return "[稿子] 等你说：" + "、".join(f"第 {n} 份" for n in waiting)


def drafts_text(values: dict) -> str:
    """看稿那一停交出去的那条消息：这一次写好的每一份全文，一行总览，最后告诉他怎么回。

    输入：整份 state，用 plan / drafts / written_now / lead
    输出例：
        "第 3 份 · LinkedIn 私信 → Dana Lee（Founder，这家公司的人）· 第 2 版\n
         Hi Dana, ...\n\n
         第 5 份 · 邮件，发到 sales@acme-audio.example → Acme Audio 这家公司本身（称呼他们的团队）· 第 1 版\n
         Subject: ...\n\n...\n\n
         总览：已定稿 第 1 份；已不要 第 2 份\n\n要改就直接说，…"
        这一次什么都没写（上一句只判了一部分，剩下的还在等）：
        开头换成「还在等你说：第 4 份、第 5 份（正文在上面）。」

    检查没过的那份（problems 不空）在正文下面写一句「注意：…」，让他发之前看一眼。
    纯函数：await_write 第一行交出去、恢复时各拼一次，两次一字不差
    """
    plan = {slot.number: slot for slot in plan_from_state(values["plan"]).slots}
    drafts = values["drafts"]
    lead = values["lead"]
    blocks: list[str] = [values["notice"]] if values["notice"] else []
    for n in values["written_now"]:
        slot, entry = plan[n], drafts[str(n)]
        head = f"第 {n} 份 · {form_text(slot)} → {recipient_text(slot, lead)} · 第 {entry['version']} 版"
        block = f"{head}\n{entry['text']}"
        if entry["problems"]:
            block += "\n注意：代码检查没过，发之前看一眼：" + "；".join(entry["problems"])
        blocks.append(block)
    waiting = sorted(
        int(k) for k, e in drafts.items() if e["status"] == WRITTEN and int(k) not in values["written_now"]
    )
    if not values["written_now"] and waiting:
        blocks.append("还在等你说：" + "、".join(f"第 {n} 份" for n in waiting) + "（正文在上面）。")
    elif not values["written_now"]:
        # 同意的那版计划里没有要写的（只剩已定稿的）：说清楚，别发一句空的「还在等你说」
        blocks.append("这一轮没有要写的稿子了。")
    overview = []
    for label, status in (("已定稿", APPROVED), ("已不要", DROPPED)):
        numbers = sorted(int(k) for k, e in drafts.items() if e["status"] == status)
        if numbers:
            overview.append(f"{label} " + "、".join(f"第 {n} 份" for n in numbers))
    if values["written_now"] and waiting:
        overview.append("还在等你说 " + "、".join(f"第 {n} 份" for n in waiting))
    if overview:
        blocks.append("总览：" + "；".join(overview))
    blocks.append(_DRAFTS_HINT)
    return "\n\n".join(blocks)
