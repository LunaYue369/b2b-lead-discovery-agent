"""写稿那张图交给 runner 的那一份：存档钥匙、在哪儿开、第一份 state、怎么拼回复。

跟 query/slack_conversation.py 同一个形状，最后由 drafting_graph() 打包成一个 ConversationGraph：

    name             "drafting"
    thread_prefix     "draft"                    存档钥匙 "draft:C0000000001:<卡片的 ts>"
    starts_in         "lead_card"                线索卡的 thread 里 @bot、或者点「要联系」开的对话归它
    build             graph.build_graph          给一个存档器、编好写稿这张图
    first_input       _first_input               (那句话, 1289) -> initial_state(1289, 那句话)
    before_interrupt  _BEFORE_INTERRUPT          ask_request -> retrieve_knowledge，
                                                 await_plan -> plan_drafts，await_write -> write_draft
    render            _reply_for                 第一问 / 计划「好，给这家写这 2 份：…」/ 同意或不写了那一句
                                                 （怎么拼在 messages.py）
    finished_text     messages.FINISHED_TEXT     「这一轮起草已经结束了。要接着写，在这个 thread 里 @ 我，…」
    restart           _restart                   走到 END 之后又 @ 了 -> 同一份存档从头再跑，
                                                 lead_id 从旧存档读
    on_crash          _never                     没有不经人点头就花钱的节点，崩了都交给 runner 通用的两种
    unfinished_retry  _never                     同上，留到下一句的崩溃都能直接重跑
"""

from __future__ import annotations

from typing import Any

from triggers.interactive_discovery.drafting import messages
from triggers.interactive_discovery.drafting.graph import build_graph
from triggers.interactive_discovery.drafting.state import initial_state
from triggers.interactive_discovery.runner import ConversationGraph


def _first_input(text: str, lead_id: int | None) -> dict:
    """START 时交给 invoke 的第一份 state。

    输入 -> 输出：
        ("帮我写封邮件给他们，强调 MOQ 500", 1289)   卡片 thread 里 @bot 开的
            -> {"messages": ["用户: 帮我写封邮件给他们，强调 MOQ 500"], "lead_id": 1289, "lead": {},
                "knowledge": {}, "request": "帮我写封邮件给他们，强调 MOQ 500", "round_start": 0,
                "ai_plan": {}, "plan": {}, "plan_feedback": "",
                "decision": ""}
        ("", 1289)                                   点按钮开的
            -> {"messages": [], "lead_id": 1289, "lead": {}, "knowledge": {}, "request": "",
                "round_start": 0, "ai_plan": {}, "plan": {}, "plan_feedback": "",
                "decision": ""}

    lead_id 不会是 None：runner 只在认出 thread 开头是线索卡（读出了 lead_id）之后，才把 START 交给这张图。
    真是 None 说明那条规矩变了，当场停，不去给一家不知道是谁的公司开对话
    """
    if lead_id is None:
        raise ValueError("写稿对话必须知道是哪一家（lead_id 是 None）")
    return initial_state(lead_id, text)


# interrupt 节点 -> 它前面那个节点。runner._back_to 退回某个 interrupt 时 update_state 的 as_node 填这个。
# ask_request 前面是 retrieve_knowledge：假装 retrieve_knowledge 刚跑完，route_after_retrieve 重判一次，
# request 还是空的（ask_request 崩溃时那句回答没写进去），判出来还是 ask，又停在 ask_request 上重问。
# await_plan 前面是 plan_drafts：假装 plan_drafts 刚跑完，固定边点亮 await_plan，同一份计划再发一遍。
# 两个前面的节点本身都不重跑：不会为了退回多查一次库，也不会多调一次模型、换出一版不一样的计划
# await_write 前面填 write_draft：固定边点亮 await_write，同一批稿子再发一遍，write_draft 本身不重跑、不多花钱
_BEFORE_INTERRUPT = {
    "ask_request": "retrieve_knowledge",
    "await_plan": "plan_drafts",
    "await_write": "write_draft",
}


def _restart(values: dict) -> int:
    """起草对话走到 END 之后又 @ 了 bot：从旧存档里读出 first_input 要的那一家。

    输入 -> 输出：旧存档的 values，例 {"lead_id": 1289, "messages": [...], "ai_plan": {...}, ...} -> 1289

    一张卡片只有一份起草存档（钥匙由卡片算出来），重开还是这一份：runner 拿 initial_state(1289, 这一句)
    当 START 的输入再跑一遍。messages 接着往后加，前几轮说过的话都还在；lead、knowledge 重新取一遍
    （这家的联系方式可能在两轮之间补上了）；ai_plan 整份换成这一轮的
    """
    return values["lead_id"]


def _never(*_args: Any) -> None:
    """on_crash / unfinished_retry 都用它：这张图没有「崩了不能直接重跑」的节点，一律 None。

    输入：runner 传进来的 (snapshot, err) 或者 (snapshot)，都不看
    输出：None

    为什么都能直接重跑：retrieve_knowledge 只读库，重跑多读一次；ask_request / await_plan 第一行就是
    interrupt，收到回复之后崩溃那种由 runner 的情况二退回重问；plan_drafts / classify_plan_reply 调模型，
    崩了重跑只是多花一次模型的钱，不会像 run_harvest 那样不经拍板就去采集。以后的 write_draft 也一样
    """
    return None


def _reply_for(result: dict) -> dict:
    """一轮 invoke 正常结束之后，发回 thread 的那条消息。

    输入：result，app.invoke 的返回值，停在 interrupt 上时多一个 "__interrupt__" 键：
              停在 ask_request   {..., "__interrupt__": (Interrupt(value={"kind": "drafting_request", ...}),)}
              停在 await_plan    {..., "__interrupt__": (Interrupt(value={"kind": "drafting_plan", ...}),)}
              走到 END           {..., "decision": "approve"} 或 {..., "decision": "cancel"}
    输出：
        停在第一问   {"text": "请问你想给 North Field Technologies 写哪些 outreach 信息？…"}
        停在计划     {"text": "好，给 North Field Technologies 写这 1 份：\\n"
                               "第 1 份：邮件，发到 marcandre@… → …"}
        走完了       {"text": "好，就按这份写。写稿这一步还没接上，接上之后会从这里接着写。"}
                     或 {"text": "好，这一轮不写了。要再写，在这个 thread 里 @ 我，说这次要写什么。"}
    """
    interrupts = result.get("__interrupt__") or ()
    if interrupts:
        return messages.render_interrupt(interrupts[0].value)
    return messages.render_ended(result)


def drafting_graph() -> ConversationGraph:
    """把上面几样打包成 runner 认的那一份。

    输入：无
    输出：ConversationGraph(name="drafting", thread_prefix="draft", starts_in="lead_card",
                            build=build_graph, ...)
    """
    return ConversationGraph(
        name="drafting",
        thread_prefix="draft",
        starts_in="lead_card",
        build=build_graph,
        first_input=_first_input,
        before_interrupt=_BEFORE_INTERRUPT,
        render=_reply_for,
        finished_text=messages.FINISHED_TEXT,
        restart=_restart,
        on_crash=_never,
        unfinished_retry=_never,
    )
