"""Slack 对话入口的测试：一条消息该不该理、怎么理，以及出错之后对话停在哪。

两层：

    delivery/conversation.py + query/messages.py
        纯函数。读懂消息、decide 那四问（两张图共用）；拼方案卡片和结束语（query 自己的）
    triggers/interactive_discovery/runner.py + query/slack_conversation.py
        ConversationRunner(query_graph(...))。跑的是真的 build_graph 出来的那张图，存档器换成 InMemorySaver

焊住的几类，都是「不报错，但用户那边看起来就是 bot 坏了」：

    频道闲聊被当成对话     没 @ 也不在 thread 里的消息开了对话，或者为它开了一次 Neon 连接
    同一句话来两遍         Slack 重投的那份又进了一次图
    采集失败之后照花钱     run_harvest 崩溃之后下一句是「算了」，却被当成重试又采了一遍
                           （包括进程在采集半路被杀、存档停在 run_harvest 前面的那种）
    重试吞掉了新内容       出错之后用户回的那句被当成新的一句话写进 messages
    重放了上一句回复       await_go 收到回复之后崩溃，下一句被丢掉，节点拿着上一次那句接着跑
    回复进错地方           thread 里的回话算出来的 thread_id 跟开头那条不一样
    改过的话被当成新的一句  编辑旧消息进了图，或者改完之后 bot 一声不吭
    在别处开了对话         线索卡或者同事的 thread 下面 @bot，开出一个找公司的对话

不连库、不花钱、不调模型：会花钱的节点和调用全换成桩（换法跟
tests/test_interactive_graph.py 一样，换 graph 模块上的名字）。
"""

from __future__ import annotations

import contextlib
import dataclasses

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from delivery import conversation
from delivery.conversation import Inbound
from triggers.harvest import DiscoveryBusy
from triggers.interactive_discovery import runner as runner_module
from triggers.interactive_discovery.budget import MonthlyCapReached
from triggers.interactive_discovery.drafting import messages as draft_messages
from triggers.interactive_discovery.drafting import nodes as draft_nodes
from triggers.interactive_discovery.drafting import slack_conversation as draft_conversation
from triggers.interactive_discovery.drafting.graph import build_graph as build_draft_graph
from triggers.interactive_discovery.drafting.planner import (
    drafts_after_plan,
    finalize_plan,
    load_plan_config,
    plan_to_state,
)
from triggers.interactive_discovery.query import messages as query_messages
from triggers.interactive_discovery.query import nodes, slack_conversation
from triggers.interactive_discovery.query.graph import build_graph
from triggers.interactive_discovery.query.planner import PlanDraft
from triggers.interactive_discovery.query.reply import ReplyJudgement
from triggers.interactive_discovery.query.state import InteractiveState, initial_state

BOT = "U0000000002"
USER = "U0000000001"
CHANNEL = "C0000000001"
ROOT_TS = "1790126036.992409"


def _event(text, *, ts=ROOT_TS, thread_ts=None, **extra) -> dict:
    """拼一个 Slack 推过来的 message 事件。

    输入：正文；这条的 ts；所在 thread 的开头 ts（不在 thread 里就不给）
    输出：例
        {"type": "message", "channel": "C0000000001", "user": "U0000000001",
         "text": "<@U0000000002> 帮我找骨传导耳机", "ts": "1790126036.992409"}
    """
    event = {"type": "message", "channel": CHANNEL, "user": USER, "text": text, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event | extra


def _inbound(text, *, ts=ROOT_TS, thread_ts="", mentioned=False, edited_ts="") -> Inbound:
    return Inbound(
        channel=CHANNEL,
        ts=ts,
        thread_ts=thread_ts,
        user=USER,
        text=text,
        mentioned=mentioned,
        edited_ts=edited_ts,
    )


def _edit_event(new_text, old_text, *, ts="1790126062.118200", thread_ts=ROOT_TS, edited=True) -> dict:
    """拼一个 message_changed 事件：有人把 old_text 改成了 new_text。

    输出：例
        {"type": "message", "subtype": "message_changed", "channel": "C0000000001",
         "message": {"user": ..., "text": "别带 Indiegogo", "ts": ..., "thread_ts": ...,
                     "edited": {"user": ..., "ts": "1790126090.000000"}},
         "previous_message": {"user": ..., "text": "别带 Kickstarter", ...}}
    edited=False 时 message 里没有 edited 那一项（Slack 自己动了这条消息，比如加了链接预览）
    """
    message = {"user": USER, "text": new_text, "ts": ts}
    if thread_ts:
        message["thread_ts"] = thread_ts
    if edited:
        message["edited"] = {"user": USER, "ts": "1790126090.000000"}
    previous = {"user": USER, "text": old_text, "ts": ts}
    return {
        "type": "message",
        "subtype": "message_changed",
        "channel": CHANNEL,
        "message": message,
        "previous_message": previous,
    }


def _texts(message: dict) -> str:
    """一条要发出去的消息里人看得到的字全拼起来，给断言用。

    输入：例 {"text": "没跑成…", "attachments": [{"fallback": "方案 v1…", "blocks": [...]}]}
    输出：顶层 text、fallback、每一块的 text 和 fields 按行拼成一段
    """
    parts = [message.get("text") or ""]
    for attachment in message.get("attachments") or []:
        parts.append(attachment.get("fallback") or "")
        for block in attachment.get("blocks") or []:
            if block.get("text"):
                parts.append(block["text"]["text"])
            parts.extend(field["text"] for field in block.get("fields") or [])
    return "\n".join(parts)


# ── 第一层：纯函数 ──────────────────────────────────────────────────────────


def test_top_level_mention_starts_its_own_thread():
    """频道里直接 @bot：去掉 @ 那一串，它自己的 ts 就是 thread 的开头。"""
    inbound = conversation.parse_message(_event(f"<@{BOT}> 帮我找骨传导耳机"), BOT)

    assert inbound.text == "帮我找骨传导耳机"
    assert inbound.mentioned is True
    assert inbound.root_ts == ROOT_TS
    assert inbound.thread_id == f"slack:{CHANNEL}:{ROOT_TS}"


def test_reply_in_thread_maps_to_the_same_conversation():
    """thread 里的回话自己的 ts 不一样，但算出来的 thread_id 跟开头那条相同。

    这条红了的症状：bot 在 thread 里问了一句，用户回答之后 checkpointer 里查不到
    这条对话，回答被当成别人 thread 里的闲聊丢掉 —— bot 从此不再说话。
    """
    first = conversation.parse_message(_event(f"<@{BOT}> 帮我找骨传导耳机"), BOT)
    reply = conversation.parse_message(
        _event("别带 Kickstarter", ts="1790126062.118200", thread_ts=ROOT_TS), BOT
    )

    assert reply.mentioned is False
    assert reply.thread_id == first.thread_id


@pytest.mark.parametrize(
    "event",
    [
        _event("Slack 自己改的", subtype="message_changed"),
        _event("别的 bot 说的", bot_id="B123"),
        _event("bot 自己说的") | {"user": BOT},
        _event(f"<@{BOT}>"),  # 只 @ 了一下，没说话
        _event("没有频道") | {"channel": ""},
        _event("有人进频道", subtype="channel_join"),
    ],
    ids=["changed-without-message", "other-bot", "self", "bare-mention", "no-channel", "join"],
)
def test_messages_that_are_not_a_person_talking_are_dropped(event):
    assert conversation.parse_message(event, BOT) is None


def test_an_edit_is_read_as_an_edit_of_the_old_line():
    """人改了 thread 里的一句：读成一条 edited_ts 非空的 Inbound，ts 还是原来那句的。"""
    inbound = conversation.parse_message(_edit_event("别带 Indiegogo", "别带 Kickstarter"), BOT)

    assert inbound.text == "别带 Indiegogo"
    assert inbound.ts == "1790126062.118200"
    assert inbound.edited_ts == "1790126090.000000"
    assert inbound.thread_id == f"slack:{CHANNEL}:{ROOT_TS}"


@pytest.mark.parametrize(
    "event",
    [
        _edit_event("看这个 https://x.com", "看这个 https://x.com", edited=False),
        _edit_event("一样的字", "一样的字"),
        _edit_event("bot 的卡片", "bot 的卡片") | {"message": {"bot_id": "B0BU5ANPBKR", "user": BOT}},
    ],
    ids=["unfurl-no-edited", "same-text", "bot-card-updated"],
)
def test_changes_that_are_not_a_person_editing_are_dropped(event):
    """message_changed 不只是人改字：Slack 加链接预览、字没变、bot 改自己的卡片，都丢掉。

    这条红了的症状：有人在 bot 的 thread 里贴个链接，Slack 一加预览，bot 就对着他说
    「改动不算数」；或者点完按钮卡片一更新，bot 回一句话。
    """
    assert conversation.parse_message(event, BOT) is None


def test_an_edit_has_its_own_dedup_key():
    """改过的那句跟原来那句 ts 一样，但重投钥匙不一样，不会被当成重投丢掉。"""
    original = conversation.parse_message(
        _event("别带 Kickstarter", ts="1790126062.118200", thread_ts=ROOT_TS), BOT
    )
    edit = conversation.parse_message(_edit_event("别带 Indiegogo", "别带 Kickstarter"), BOT)

    assert original.ts == edit.ts
    assert original.dedup_key != edit.dedup_key


def test_thread_broadcast_is_still_a_person_talking():
    """在 thread 里回话、勾了「同时发到频道」：它仍然是 thread 里的一句，照常处理。"""
    inbound = conversation.parse_message(
        _event("跑", ts="1790126099.000100", thread_ts=ROOT_TS, subtype="thread_broadcast"), BOT
    )

    assert inbound is not None
    assert inbound.root_ts == ROOT_TS


EDIT_TS = "1790126090.000000"


@pytest.mark.parametrize(
    ("edited_ts", "thread_ts", "status", "mentioned", "root_is_card", "expected"),
    [
        (EDIT_TS, ROOT_TS, conversation.WAITING, False, False, conversation.EDITED),
        (EDIT_TS, ROOT_TS, conversation.ENDED, False, False, conversation.EDITED),
        (EDIT_TS, "", conversation.NO_CONVERSATION, True, False, conversation.EDITED),
        (EDIT_TS, ROOT_TS, conversation.NO_CONVERSATION, False, False, conversation.IGNORE),
        (EDIT_TS, "", conversation.NO_CONVERSATION, False, False, conversation.IGNORE),
        ("", "", conversation.NO_CONVERSATION, False, False, conversation.IGNORE),
        ("", "", conversation.NO_CONVERSATION, True, False, conversation.START),
        ("", ROOT_TS, conversation.WAITING, False, False, conversation.RESUME),
        ("", ROOT_TS, conversation.WAITING, True, False, conversation.RESUME),
        ("", ROOT_TS, conversation.FAILED, False, False, conversation.RETRY),
        ("", ROOT_TS, conversation.ENDED, False, False, conversation.FINISHED),
        ("", ROOT_TS, conversation.NO_CONVERSATION, False, False, conversation.IGNORE),
        ("", ROOT_TS, conversation.NO_CONVERSATION, True, True, conversation.START),  # 线索卡下面开起草
        ("", ROOT_TS, conversation.NO_CONVERSATION, True, False, conversation.OTHER_THREAD),
    ],
)
def test_decide_table(edited_ts, thread_ts, status, mentioned, root_is_card, expected):
    """decide 那四问的每一格逐个对一遍。"""
    inbound = _inbound(
        "x", ts="1790126100.000100", thread_ts=thread_ts, mentioned=mentioned, edited_ts=edited_ts
    )

    assert conversation.decide(inbound, status, root_is_card=root_is_card) == expected


def test_decide_refuses_an_unknown_status():
    """四种状态之外的取值当场报错，不猜一个处理。"""
    with pytest.raises(ValueError, match="不认识的对话状态"):
        conversation.decide(_inbound("x", thread_ts=ROOT_TS), "running", root_is_card=False)


@pytest.mark.parametrize(
    ("thread_ts", "status", "mentioned", "edited_ts", "expected"),
    [
        (ROOT_TS, conversation.NO_CONVERSATION, True, "", True),
        (ROOT_TS, conversation.NO_CONVERSATION, False, "", False),  # 同事 thread 里的闲聊
        (ROOT_TS, conversation.WAITING, True, "", False),  # bot 自己的对话
        ("", conversation.NO_CONVERSATION, True, "", False),  # 频道里开新对话
        (ROOT_TS, conversation.NO_CONVERSATION, True, EDIT_TS, False),  # 编辑
    ],
)
def test_card_check_only_when_it_decides_something(thread_ts, status, mentioned, edited_ts, expected):
    """只有「thread 里、没存档、@ 了、不是编辑」才去 Slack 读开头那条。"""
    inbound = _inbound("x", thread_ts=thread_ts, mentioned=mentioned, edited_ts=edited_ts)

    assert conversation.needs_card_check(inbound, status) is expected


def _card_root(**overrides) -> dict:
    """conversations.replies 读回来的一张线索卡，只画 lead_card_id 用到的几项。"""
    root = {
        "user": BOT,
        "bot_id": "B0BU5ANPBKR",
        "ts": "1788403874.643329",
        "attachments": [
            {
                "color": "#e8912d",
                "blocks": [
                    {"type": "header", "block_id": "aB3x", "text": {"type": "plain_text", "text": "x"}},
                    {"type": "actions", "block_id": "lead:1281:run:27", "elements": []},
                ],
            }
        ],
    }
    return root | overrides


def test_a_lead_card_is_recognised_before_and_after_the_click():
    """没点过的卡（按钮那一块）和点过的卡（回执那一块）都认得出，而且读得出是哪一家。"""
    clicked = _card_root()
    clicked["attachments"][0]["blocks"][-1] = {
        "type": "section",
        "block_id": "lead:1281:run:27",
        "text": {"type": "mrkdwn", "text": "已记下：要联系"},
    }

    assert conversation.lead_card_id(_card_root(), BOT) == 1281
    assert conversation.lead_card_id(clicked, BOT) == 1281


@pytest.mark.parametrize(
    "root",
    [
        _card_root(user=USER),  # 人贴进来的、长得一样的卡片
        {"user": USER, "text": "大家看看这个耳机", "ts": "1790199000.000100"},  # 同事开的讨论串
        {
            "user": BOT,
            "ts": "1788403871.653939",
            "attachments": [{"blocks": [{"type": "section", "block_id": "eOkm/"}]}],
        },  # 汇总卡
    ],
    ids=["posted-by-a-person", "plain-thread", "summary-card"],
)
def test_other_roots_are_not_lead_cards(root):
    assert conversation.lead_card_id(root, BOT) is None


def test_a_lead_card_with_an_unreadable_id_is_not_guessed():
    """block_id 是 lead: 开头、中间那一截却不是数字：当成认不出，不猜一个 id 去给别家开对话。"""
    root = _card_root()
    root["attachments"][0]["blocks"][-1]["block_id"] = "lead:abc:run:27"

    assert conversation.lead_card_id(root, BOT) is None


def test_initial_state_gives_every_field():
    """开新对话那份 state 要把 InteractiveState 的字段给全，少一个节点里读就是 KeyError。

    tests/test_interactive_graph.py 那条 ast 测试只扫 scripts 和 tests，扫不到这里。
    """
    state = initial_state("帮我找骨传导耳机")

    assert set(state) == set(InteractiveState.__annotations__)
    assert state["messages"] == ["用户: 帮我找骨传导耳机"]


def _plan(**overrides) -> dict:
    plan = {
        "version": 2,
        "queries": [{"text": "Kickstarter AND bone conduction", "filters": {}}],
        "assumptions": "我理解的需求：找骨传导团队\n要搜的词：bone conduction",
        "estimated_cost_usd": 0.1,
        # 采集窗口四格（time_window.PostWindow）。默认是「他说了、换算得出」那一种
        "posted_since": "2026-07-28",
        "posted_limit": None,
        "time_said": "最近两个月",
        "time_understood": True,
    }
    return plan | overrides


# 他没说时间 / 说了不限时间 / 说了但换算不了：没有日期，按档
_DEFAULT = {"posted_since": None, "posted_limit": "month", "time_said": "", "time_understood": False}


def _fields(message) -> list[str]:
    """方案卡最上面那一栏（要搜几条 / 预计花费 / 只要多新的帖子）的每一格，按顺序。"""
    blocks = message["attachments"][0]["blocks"]
    return [field["text"] for block in blocks for field in block.get("fields", [])]


def test_the_post_window_sits_in_the_top_row_with_the_words_he_said():
    """「最近两个月」被当成 24 小时那一轮的教训：窗口放在最上面那一栏、加粗，他的原话在括号里给他核对。"""
    fields = _fields(query_messages.plan_message(_plan()))

    # 收尾的 * 后面有空格：紧跟全角括号的话 Slack 不认加粗，星号原样印出来
    assert fields[4:] == ["*只要多新的帖子*", "*2026-07-28 之后的帖子* （你说的是「最近两个月」）"]


def test_an_hour_level_window_shows_the_time_in_utc():
    """小时级的截止带时刻，印成人读得懂的样子，并写明是 UTC。"""
    plan = _plan(posted_since="2026-09-28T05:00:00Z", time_said="最近 6 小时")

    assert _fields(query_messages.plan_message(plan))[5] == (
        "*2026-09-28 05:00 UTC 之后的帖子* （你说的是「最近 6 小时」）"
    )


def test_the_other_three_windows_on_the_card():
    """没说时间、说了不限时间、说了但换算不了：都不留空，说清楚用的是哪一档、为什么。"""
    unsaid = _plan(**_DEFAULT)
    anytime = _plan(**_DEFAULT | {"posted_limit": "any", "time_said": "不限时间", "time_understood": True})
    unparsed = _plan(**_DEFAULT | {"time_said": "今年上半年"})

    assert _fields(query_messages.plan_message(unsaid))[5] == "*最近一个月* （month，你没说时间，按默认）"
    assert _fields(query_messages.plan_message(anytime))[5] == "*不限时间* （你说的是「不限时间」）"
    assert _fields(query_messages.plan_message(unparsed))[5] == (
        "*最近一个月* （month，你说的「今年上半年」换算不了，按默认）"
    )


def test_every_default_window_has_a_chinese_name():
    """discovery.yaml 里能写的每一档都要印得出来：漏一个的话，卡片在那个值上 KeyError。"""
    from models import POSTED_WITHIN_VALUES

    for value in POSTED_WITHIN_VALUES[1:]:
        assert query_messages.plan_message(_plan(**_DEFAULT | {"posted_limit": value}))["attachments"]


def test_the_window_text_for_the_conversation_log_has_no_markup():
    """写进对话记录那一句用纯文字版：模型下一轮读得到窗口是什么，但不该读到 Slack 的星号。"""
    assert query_messages.window_text(_plan()) == "2026-07-28 之后的帖子（你说的是「最近两个月」）"


def test_plan_card_copies_assumptions_verbatim():
    """方案卡片：紫色竖条、检索式用反引号列出来、清单原样照抄一个字不改、钱数两位小数。"""
    plan = _plan()
    message = query_messages.render_interrupt({"kind": "go", "plan": plan, "estimated_cost_usd": 0.1})

    assert "text" not in message  # 顶层不给 text，不然客户端里 fallback 会再印一遍
    attachment = message["attachments"][0]
    assert attachment["color"] == query_messages.PLAN_COLOR
    assert attachment["fallback"] == "方案 v2，1 条 query，预计 $0.10"
    text = _texts(message)
    assert "`Kickstarter AND bone conduction`" in text
    assert plan["assumptions"] in text
    assert "「跑」" in text
    assert not any(block["type"] == "actions" for block in attachment["blocks"])  # 没有按钮


def test_plan_color_is_not_one_the_digest_already_uses():
    from delivery import cards

    assert query_messages.PLAN_COLOR not in {cards.SUMMARY_COLOR, *cards.BAND_COLORS.values()}


def test_plan_card_without_queries_skips_the_query_list():
    plan = _plan(queries=[], assumptions="一条能用的 query 都没拟出来", estimated_cost_usd=0.0)

    assert "要去搜的" not in _texts(query_messages.render_interrupt({"kind": "go", "plan": plan}))


def test_long_assumptions_are_split_below_the_section_limit():
    """清单很长（丢掉了很多条、每条带原因）时按行分成几块，每块都在 Slack 的上限以内。

    这条红了的症状：Slack 拒收整条消息（invalid_blocks），用户那边一张卡片都看不到，
    只收到一句「出错了」。
    """
    lines = [f"丢掉了第 {i} 条：{'x' * 200}" for i in range(40)]
    plan = _plan(assumptions="\n".join(lines))
    blocks = query_messages.plan_message(plan)["attachments"][0]["blocks"]

    assert all(len(block["text"]["text"]) <= 3000 for block in blocks if "text" in block)
    joined = "\n".join(block["text"]["text"] for block in blocks if "text" in block)
    assert all(line in joined for line in lines)  # 一行都没丢，也没被切断


def test_clarify_interrupt_is_just_the_question():
    assert query_messages.render_interrupt({"kind": "clarify", "question": "你想找哪一类设备？"}) == {
        "text": "你想找哪一类设备？"
    }


def test_ended_replies():
    """走到 END 的两条路各说一句：取消，或者采集那一行去掉「AI: 」。"""
    assert query_messages.render_ended({"decision": "cancel", "messages": []}) == {
        "text": query_messages.CANCELLED_TEXT
    }
    assert query_messages.render_ended(
        {"decision": "run", "messages": ["AI: 采完了。run 31，completed，发现 23 家公司"]}
    ) == {"text": "采完了。run 31，completed，发现 23 家公司"}


# ── 第二层：ConversationRunner，跑真的图 ─────────────────────────────────────


@pytest.fixture
def graph_env(monkeypatch):
    """造两张真的图（找公司、写稿，共用一个内存存档器），会花钱、会连库的地方全换成桩，返回一个可以调的账本。

    输出：一个 dict
        harvest_calls   run_harvest 桩被调了几次，例 [1]
        harvest_fails   不是 None 时 run_harvest 桩抛这个异常，例 DiscoveryBusy(...)（advisory lock 被占）
        deliver_calls   deliver 桩被调了几次，例 [2]（崩溃一次、重跑一次）
        deliver_fails   还要让 deliver 抛几次异常，例 [1]
        contacts_calls  find_contacts 桩被调了几次，例 [1]
        contacts_fails  还要让 find_contacts 抛几次异常，例 [1]
        intent_fails    还要让 understand_intent 抛几次异常，例 [1]
        ask_first       True 时 understand_intent 第一次读不出品类，图先去 ask_clarification 问一句
        await_fails     还要让 await_go 在收到回复之后抛几次异常，例 [1]
        clarify_fails   还要让 ask_clarification 在收到回复之后抛几次异常，例 [1]
        opened          saver_opener 被调了几次（= 开了几次 Neon 连接）
        card_roots      哪些 thread 开头的 ts 是线索卡、是哪一家，root_card 桩按它回答，
                        例 {"1790199000.000100": 1289}
        card_checks     root_card 桩被问过哪些 (channel, root_ts)
        load_calls      写稿那张图的 retrieve_knowledge 桩被调了几次，例 [1]
        ask_fails       还要让写稿那张图的 ask_request 在收到回复之后抛几次异常，例 [1]
        plan_calls      写稿那张图的 plan_drafts 桩被调了几次（= 调了几次模型），例 [1]
        judge_calls     写稿那张图的 classify_plan_reply 桩被调了几次，例 [1]
        await_plan_fails  还要让写稿那张图的 await_plan 在收到回复之后抛几次异常，例 [1]
        runner          ConversationRunner，管两张图；saver_opener 交出的是同一个存档器，
                        所以前后几轮看得到同一份存档
        app / draft_app 用同一个存档器编的两张图，测试拿它们直接查存档
    """
    env = {
        "harvest_calls": [0],
        "harvest_fails": None,
        "deliver_calls": [0],
        "deliver_fails": [0],
        "contacts_calls": [0],
        "contacts_fails": [0],
        "intent_fails": [0],
        "ask_first": False,
        "await_fails": [0],
        "clarify_fails": [0],
        "opened": [0],
        "card_roots": {},
        "card_checks": [],
        "load_calls": [0],
        "ask_fails": [0],
        "plan_calls": [0],
        "judge_calls": [0],
        "await_plan_fails": [0],
        "write_calls": [0],
        "await_write_fails": [0],
        "feedback_calls": [0],
    }

    def fake_understand_intent(state):
        if env["intent_fails"][0]:
            env["intent_fails"][0] -= 1
            raise RuntimeError("OpenAI 超时（测试桩）")
        # ask_first 时第一趟读不出品类：topics 空 -> route_intent 判 clarify；问过一轮之后就够了
        unclear = env["ask_first"] and state["clarify_rounds"] == 0
        return {
            "intent_result": {
                "intent": {
                    "goal": "找骨传导团队",
                    "topics": [] if unclear else ["bone conduction"],
                    "events": [],
                    "exclusions": [],
                    "time_said": "",
                    "max_spend_usd": 0.0,
                    "unsupported": [],
                },
                "clarification_question": "你想找做哪一类设备的公司？" if unclear else "",
            }
        }

    def fake_retrieve_knowledge(state):
        return {"knowledge": {"avoid": [], "topics_searched": [], "known_queries": [], "known_lead_total": 0}}

    def fake_run_harvest(state, config):
        env["harvest_calls"][0] += 1
        if env["harvest_fails"] is not None:
            raise env["harvest_fails"]
        return {"run_id": 31, "messages": ["AI: 采完了。run 31，completed，发现 23 家公司"]}

    def fake_deliver(state, config):
        env["deliver_calls"][0] += 1
        if env["deliver_fails"][0]:
            env["deliver_fails"][0] -= 1
            raise RuntimeError("Slack 超时（测试桩）")
        return {"messages": ["AI: 分析了 12 家，3 家过线，推了 3 张到频道：https://x/p1"]}

    def fake_find_contacts(state, config):
        # 真的 find_contacts 自己接住所有异常，几乎不会崩；这里模拟接不住的那种（比如进程级的错），
        # 看它被归到 _recover 的哪一种情况
        env["contacts_calls"][0] += 1
        if env["contacts_fails"][0]:
            env["contacts_fails"][0] -= 1
            raise RuntimeError("找联系方式崩溃（测试桩）")
        return {"messages": ["AI: 找联系方式（测试桩）"]}

    def fake_draft_queries(client, *, intent, knowledge, lines, feedback, previous_plan, config):
        return PlanDraft(queries=({"text": "Kickstarter AND bone conduction", "filters": {}},))

    def fake_read_decision(client, lines, plan, *, feedback, config):
        decision = {"跑": "run", "算了": "cancel"}.get(feedback.strip(), "replan")
        return ReplyJudgement(decision=decision, reason="测试桩")

    def crash_after_reply(real, counter):
        """包一层真的 interrupt 节点：interrupt 照停，只在收到回复、真节点跑完之后，
        按账本上的次数抛异常 —— 跟「interrupt 下面那几行崩溃了」是同一个存档样子。"""

        def node(state):
            out = real(state)
            if env[counter][0]:
                env[counter][0] -= 1
                raise RuntimeError(f"{real.__name__} 收到回复之后出错（测试桩）")
            return out

        return node

    monkeypatch.setattr(
        "triggers.interactive_discovery.query.graph.understand_intent", fake_understand_intent
    )
    monkeypatch.setattr(
        "triggers.interactive_discovery.query.graph.retrieve_knowledge", fake_retrieve_knowledge
    )
    monkeypatch.setattr("triggers.interactive_discovery.query.graph.run_harvest", fake_run_harvest)
    monkeypatch.setattr("triggers.interactive_discovery.query.graph.deliver", fake_deliver)
    # find_contacts 也换掉：真的那个接住所有异常，conftest 那道闸的报警会被它吞掉
    monkeypatch.setattr(
        "triggers.interactive_discovery.query.graph.find_contacts", fake_find_contacts
    )
    monkeypatch.setattr(
        "triggers.interactive_discovery.query.graph.await_go",
        crash_after_reply(nodes.await_go, "await_fails")
    )
    monkeypatch.setattr(
        "triggers.interactive_discovery.query.graph.ask_clarification",
        crash_after_reply(nodes.ask_clarification, "clarify_fails"),
    )
    monkeypatch.setattr("triggers.interactive_discovery.query.nodes.build_llm_client", lambda: None)
    monkeypatch.setattr(
        "triggers.interactive_discovery.query.nodes.draft_queries", fake_draft_queries
    )
    monkeypatch.setattr(
        "triggers.interactive_discovery.query.nodes.read_decision", fake_read_decision
    )

    def fake_drafting_retrieve(state):
        # 写稿那张图的 retrieve_knowledge 要连库，换成照 North Field Technologies（lead 1289）真值回答的桩
        env["load_calls"][0] += 1
        return {
            "lead": {
                "display_name": "North Field Technologies",
                "emails": [
                    {"value": "contact@northfield-inc.example", "source": "website", "person_name": None,
                     "email_status": None, "catch_all": None, "quality_score": None},
                ],
                "people": [
                    {"name": "Marc-Andre Lavoie", "relation": "member", "position": "Founder & CEO",
                     "profile_url": "https://www.linkedin.com/in/malavoie"},
                ],
            },
            # 这组测试只管分流和接线，写稿要的那份用不到，只给列计划时要查的证据（一条都没有）
            "knowledge": {"evidence": []},
        }

    # 写稿那张图的 plan_drafts 要调模型，换成照剧本回答的桩：用户最后一句 -> 模型给出的那一份。
    # 剧本里的每一句都读成一份稿（AI 那一半都挑 moq、开场不引帖子）：
    #   「帮我写封邮件给他们」「邮件」   读成 1 号这家公司、发官网邮箱
    #   「邮件，发给 Marc-Andre」       名单上 2 号（1 号是这家公司，这组桩的名单只有他一个人）
    # 剧本里没有的那句当成一份都没说：AI 补一份写给公司的邮件
    draft_script = {
        "帮我写封邮件给他们": {"recipient_ai_understood": 1, "form_user_requested": "email"},
        "邮件，发给 Marc-Andre": {"form_user_requested": "email", "recipient_user_requested": "Marc-Andre",
                                   "recipient_ai_understood": 2},
        "邮件": {"recipient_ai_understood": 1, "form_user_requested": "email"},
    }

    def fake_plan_drafts(state):
        env["plan_calls"][0] += 1
        line = state["messages"][-1].removeprefix("用户: ")
        # 改计划时这组剧本都是一份改成另一份：接着 1 号，跟真模型照规矩填的一样
        blank = {"plan_number": 1 if state["plan"] else 0,
                 "user_words": line, "recipient_user_requested": "", "recipient_ai_understood": 0,
                 "email_user_requested": "", "email_ai_understood": "", "form_user_requested": "",
                 "emphasis_user_requested": [], "form_ai_planned": "", "selling_points_ai_planned": ["moq"],
                 "opening_ai_planned": 0, "reason_ai_planned": ""}
        if line in draft_script:
            drafts = [blank | draft_script[line]]
        else:
            drafts = [blank | {"user_words": "", "form_ai_planned": "email"}]
        result = {"count_user_requested": 0, "count_user_words": "", "drafts": drafts,
                  "language_user_requested": "", "tone_user_requested": "", "unsupported": []}
        # plan 和账本跟真节点一样用代码现算：这组测试管的是接线，模型那一步才是桩
        finalized = finalize_plan(
            result, state["lead"], load_plan_config(), previous=state["plan"], drafts=state["drafts"]
        )
        return {
            "ai_plan": result,
            "plan": plan_to_state(finalized.plan),
            "drafts": drafts_after_plan(state["drafts"], finalized),
        }

    monkeypatch.setattr(
        "triggers.interactive_discovery.drafting.graph.retrieve_knowledge", fake_drafting_retrieve
    )
    monkeypatch.setattr("triggers.interactive_discovery.drafting.graph.plan_drafts", fake_plan_drafts)

    def fake_classify_plan_reply(state):
        # 判回话也要调模型，换成照剧本判的桩：「写吧」同意，「算了」不写了，其余都是要改
        env["judge_calls"][0] += 1
        decision = {"写吧": "approve", "算了": "cancel"}.get(state["plan_feedback"], "revise")
        return {"decision": decision} if decision == "revise" else {"decision": decision, "plan_feedback": ""}

    monkeypatch.setattr(
        "triggers.interactive_discovery.drafting.graph.classify_plan_reply", fake_classify_plan_reply
    )
    monkeypatch.setattr(
        "triggers.interactive_discovery.drafting.graph.await_plan",
        crash_after_reply(draft_nodes.await_plan, "await_plan_fails"),
    )
    monkeypatch.setattr(
        "triggers.interactive_discovery.drafting.graph.ask_request",
        crash_after_reply(draft_nodes.ask_request, "ask_fails"),
    )

    def fake_write_draft(state):
        # 写稿要调模型，换成桩：要写的每份都变成写好了、版本 +1
        env["write_calls"][0] += 1
        drafts = {k: dict(v) for k, v in state["drafts"].items()}
        written = []
        for key, entry in drafts.items():
            if entry["status"] in ("planned", "rewrite"):
                entry.update(status="written", version=entry["version"] + 1, text=f"稿子 {key}", feedback="")
                entry["plan_version"] = next(
                    s["plan_version"] for s in state["plan"]["slots"] if s["number"] == int(key)
                )
                written.append(int(key))
        return {"drafts": drafts, "written_now": sorted(written)}

    def fake_classify_feedback(state):
        # 拆意见也要调模型，换成照剧本判的桩：「都可以了」全部定稿，「算了」不写了，其余都当成没判
        env["feedback_calls"][0] += 1
        reply = state["draft_feedback"]
        drafts = {k: dict(v) for k, v in state["drafts"].items()}
        if reply == "算了":
            return {"decision": "cancel", "verdicts": [], "written_now": []}
        if reply == "都可以了":
            for entry in drafts.values():
                if entry["status"] == "written":
                    entry["status"] = "approved"
            done = {"decision": "done", "verdicts": [], "written_now": [], "plan_feedback": ""}
            return {"drafts": drafts, **done}
        return {"decision": "wait", "verdicts": [], "written_now": [], "plan_feedback": ""}

    monkeypatch.setattr("triggers.interactive_discovery.drafting.graph.write_draft", fake_write_draft)
    monkeypatch.setattr(
        "triggers.interactive_discovery.drafting.graph.classify_feedback", fake_classify_feedback
    )
    # 写库换掉：这组测试管接线，写库那一段在 test_drafting_graph 里测
    monkeypatch.setattr("triggers.interactive_discovery.drafting.graph.record_round", lambda state: {})
    monkeypatch.setattr(
        "triggers.interactive_discovery.drafting.graph.await_write",
        crash_after_reply(draft_nodes.await_write, "await_write_fails"),
    )

    saver = InMemorySaver()

    @contextlib.contextmanager
    def opener():
        env["opened"][0] += 1
        yield saver

    def root_card(channel, root_ts):
        env["card_checks"].append((channel, root_ts))
        return env["card_roots"].get(root_ts)

    env["runner"] = runner_module.ConversationRunner(
        [slack_conversation.query_graph(), draft_conversation.drafting_graph()],
        root_card=root_card,
        saver_opener=opener,
    )
    env["app"] = build_graph(saver)
    env["draft_app"] = build_draft_graph(saver)
    return env


def _status(env, thread_id=f"slack:{CHANNEL}:{ROOT_TS}") -> str:
    return runner_module.conversation_status(
        env["app"].get_state({"configurable": {"thread_id": thread_id}})
    )


def _start(env) -> dict:
    return env["runner"].handle(_inbound("帮我找骨传导耳机", mentioned=True))


def _say(env, text, ts) -> dict:
    return env["runner"].handle(_inbound(text, ts=ts, thread_ts=ROOT_TS))


def _fallback(message: dict) -> str:
    """方案卡片的那句降级文本，例 "方案 v1，1 条 query，预计 $0.00" """
    return message["attachments"][0]["fallback"]


def test_channel_chatter_never_opens_a_connection(graph_env):
    """频道里没 @ 的一句：不回话，也不开 Neon 连接（每开一次都要等一秒冷启动）。"""
    assert graph_env["runner"].handle(_inbound("哈哈哈 一定要赶工完成呢")) is None
    assert graph_env["opened"] == [0]


def test_start_then_cancel_then_finished(graph_env):
    """开对话 -> 停在拍板、发方案卡片 -> 「算了」 -> 结束；结束之后再说话只回一句提示。"""
    card = _start(graph_env)
    assert _fallback(card).startswith("方案 v1")
    assert _status(graph_env) == conversation.WAITING

    assert _say(graph_env, "算了", "1790126100.000100") == {"text": query_messages.CANCELLED_TEXT}
    assert _status(graph_env) == conversation.ENDED
    assert graph_env["harvest_calls"] == [0]

    assert _say(graph_env, "再找一批", "1790126200.000100") == {"text": query_messages.FINISHED_TEXT}


def test_run_reports_what_deliver_said(graph_env):
    """说「跑」之后回到 thread 里的是 deliver 那一句，不是 run_harvest 那句「采完了」。"""
    _start(graph_env)

    assert _say(graph_env, "跑", "1790126100.000100") == {
        "text": "分析了 12 家，3 家过线，推了 3 张到频道：https://x/p1"
    }
    assert graph_env["harvest_calls"] == [1]
    assert graph_env["deliver_calls"] == [1]


def test_a_crashed_deliver_is_retried_without_harvesting_again(graph_env):
    """deliver 崩溃之后，用户回任意一句只重跑推卡这一步，不会再采一遍。

    这条红了的症状：推卡失败一次，用户回一句话，又花了一次采集的钱。
    deliver 单开一个节点就是为了这件事，写进 run_harvest 里的话崩溃会退回 await_go。
    """
    _start(graph_env)
    graph_env["deliver_fails"] = [1]

    reply = _say(graph_env, "跑", "1790126100.000100")
    assert reply["text"].startswith("出错了")

    reply = _say(graph_env, "好了吗", "1790126110.000100")
    assert reply == {"text": "分析了 12 家，3 家过线，推了 3 张到频道：https://x/p1"}
    assert graph_env["harvest_calls"] == [1]
    assert graph_env["deliver_calls"] == [2]


def test_a_crashed_find_contacts_is_retried_without_harvesting_again(graph_env):
    """find_contacts 崩溃：归到 _recover 的情况三（回一句出错了），用户回任意一句从 find_contacts
    重跑、接着推卡，不会退回 await_go、不会再采一遍。

    这条红了的症状：找联系方式崩一次，用户回一句话就又花一次采集的钱（被当成情况一退回了 await_go），
    或者卡片推不出去（重跑时跳过了 deliver）
    """
    _start(graph_env)
    graph_env["contacts_fails"] = [1]

    reply = _say(graph_env, "跑", "1790126100.000100")
    assert reply["text"].startswith("出错了")
    assert graph_env["deliver_calls"] == [0]

    reply = _say(graph_env, "好了吗", "1790126110.000100")
    assert reply == {"text": "分析了 12 家，3 家过线，推了 3 张到频道：https://x/p1"}
    assert graph_env["harvest_calls"] == [1]
    assert graph_env["contacts_calls"] == [2]
    assert graph_env["deliver_calls"] == [1]


def test_failed_harvest_goes_back_to_waiting_for_go(graph_env):
    """采集崩溃之后对话退回「等拍板」：下一句是「算了」就结束，采集不会被重跑。

    这条红了的症状：run_harvest 失败之后，下一句不管说什么都会被当成重试，
    用户说「算了」也照样采一遍、照样花钱。
    """
    _start(graph_env)
    graph_env["harvest_fails"] = DiscoveryBusy("已经有一跑 discovery 在跑（测试桩）")

    reply = _say(graph_env, "跑", "1790126100.000100")

    assert reply["text"].startswith("没跑成")
    assert "一分钱没花" in reply["text"]  # DiscoveryBusy 是在调 Apify 之前抛的
    assert _fallback(reply).startswith("方案 v1")  # 同一版方案又发了一遍
    assert _status(graph_env) == conversation.WAITING
    assert graph_env["harvest_calls"] == [1]

    assert _say(graph_env, "算了", "1790126200.000100") == {"text": query_messages.CANCELLED_TEXT}
    assert graph_env["harvest_calls"] == [1]


def test_hitting_the_monthly_cap_says_why_and_keeps_the_plan(graph_env):
    """撞了月度上限：说清已花、报价、上限、哪天恢复，一分钱没花，方案留在原地。

    这条红了的症状有两种：一是回了一句「再说一次跑就重新采」，用户照做又被拒一次；
    二是没退回 await_go，这次对话卡在出错那里，连「算了」都说不了。
    """
    import datetime as dt

    _start(graph_env)
    graph_env["harvest_fails"] = MonthlyCapReached(
        spent_usd=9.95,
        estimate_usd=0.15,
        cap_usd=10.0,
        since=dt.datetime(2026, 9, 21, tzinfo=dt.UTC),
        resets_at=dt.datetime(2026, 10, 21, tzinfo=dt.UTC),
    )

    reply = _say(graph_env, "跑", "1790126100.000100")

    assert reply["text"].startswith("没跑成：本账期（2026-09-21 起）")
    assert "上限 $10.00" in reply["text"] and "2026-10-21" in reply["text"]
    assert "一分钱没花" in reply["text"]
    assert "再说一次「跑」" not in reply["text"]
    assert _fallback(reply).startswith("方案 v1")
    assert _status(graph_env) == conversation.WAITING

    assert _say(graph_env, "算了", "1790126200.000100") == {"text": query_messages.CANCELLED_TEXT}


def test_harvest_crashing_after_apify_does_not_claim_nothing_was_spent(graph_env):
    """采集崩溃但不是 DiscoveryBusy：照样退回拍板，但不许说「一分钱没花」。

    这条红了的症状：run_harvest 已经调过 Apify、后面写库时崩溃，用户却被告知一分钱没花。
    """
    _start(graph_env)
    graph_env["harvest_fails"] = RuntimeError("Neon 掐了连接（测试桩）")

    reply = _say(graph_env, "跑", "1790126100.000100")

    assert reply["text"].startswith("没跑成")
    assert "一分钱没花" not in reply["text"]
    assert "可能已经花了钱" in reply["text"]
    assert _fallback(reply).startswith("方案 v1")
    assert _status(graph_env) == conversation.WAITING


def test_harvest_left_unfinished_goes_back_to_waiting_for_go_on_retry(graph_env):
    """进程在采集半路被杀留下的存档：下一句不重采，退回「等拍板」。

    造法：绕过 runner 直接对图 invoke「跑」，让 run_harvest 桩抛异常 —— _recover 没机会跑，
    存档就停在 run_harvest 前面，跟进程被杀之后留下的一模一样。

    这条红了的症状：进程重启之后用户回一句「算了」，被判成 RETRY、invoke(None)，
    run_harvest 被直接重跑，又采一遍、又花一次钱。
    """
    _start(graph_env)
    graph_env["harvest_fails"] = DiscoveryBusy("已经有一跑 discovery 在跑（测试桩）")
    config = {"configurable": {"thread_id": f"slack:{CHANNEL}:{ROOT_TS}"}}
    with pytest.raises(RuntimeError):
        graph_env["app"].invoke(Command(resume="跑"), config)
    assert graph_env["app"].get_state(config).next == ("run_harvest",)
    assert _status(graph_env) == conversation.FAILED
    graph_env["harvest_fails"] = None

    reply = _say(graph_env, "算了", "1790126100.000100")

    assert reply["text"].startswith("上一次采集没跑完")
    assert "一分钱没花" not in reply["text"]  # 进程被杀时可能已经在采，不能这么说
    assert _fallback(reply).startswith("方案 v1")  # 同一版方案又发了一遍
    assert _status(graph_env) == conversation.WAITING
    assert graph_env["harvest_calls"] == [1]

    assert _say(graph_env, "算了", "1790126200.000100") == {"text": query_messages.CANCELLED_TEXT}
    assert graph_env["harvest_calls"] == [1]


def test_crash_after_reply_to_the_plan_asks_for_the_reply_again(graph_env):
    """await_go 收到「跑」之后崩溃：退回拍板、重发方案，上一句「跑」作废。

    这条红了的症状：崩溃之后存档里还存着「跑」，用户下一句说「算了」，
    await_go 拿到的却还是「跑」，照样去采集。
    """
    _start(graph_env)
    graph_env["await_fails"][0] = 1

    reply = _say(graph_env, "跑", "1790126100.000100")

    assert reply["text"].startswith("你刚才那句没处理成")
    assert _fallback(reply).startswith("方案 v1")  # 同一版方案又发了一遍
    assert _status(graph_env) == conversation.WAITING
    assert graph_env["harvest_calls"] == [0]

    assert _say(graph_env, "算了", "1790126200.000100") == {"text": query_messages.CANCELLED_TEXT}
    assert graph_env["harvest_calls"] == [0]


def test_crash_after_answering_a_clarification_asks_it_again(graph_env):
    """ask_clarification 收到回答之后崩溃：同一句提问再问一遍，回答只记一次。"""
    graph_env["ask_first"] = True
    assert _start(graph_env) == {"text": "你想找做哪一类设备的公司？"}
    graph_env["clarify_fails"][0] = 1

    reply = _say(graph_env, "音频", "1790126100.000100")

    assert reply["text"].startswith("你刚才那句没处理成")
    assert reply["text"].endswith("你想找做哪一类设备的公司？")
    assert _status(graph_env) == conversation.WAITING

    card = _say(graph_env, "骨传导耳机", "1790126200.000100")
    assert _fallback(card).startswith("方案 v1")

    lines = (
        graph_env["app"]
        .get_state({"configurable": {"thread_id": f"slack:{CHANNEL}:{ROOT_TS}"}})
        .values["messages"]
    )
    assert "用户: 音频" not in lines  # 崩溃那一次的回答作废了
    assert lines.count("用户: 骨传导耳机") == 1
    assert lines.count("AI: 你想找做哪一类设备的公司？") == 1


def test_crash_after_reply_left_unrecovered_goes_back_on_retry(graph_env):
    """await_go 收到回复之后崩溃、_recover 又没机会跑（进程被杀之类）：下一句也不重放。

    造法跟上面采集那条一样：绕过 runner 直接对图 invoke。存档就停在「interrupt 还挂着、
    next 为空、任务带 error」的样子，只看 interrupts 会被误判成 WAITING。
    """
    _start(graph_env)
    graph_env["await_fails"][0] = 1
    config = {"configurable": {"thread_id": f"slack:{CHANNEL}:{ROOT_TS}"}}
    with pytest.raises(RuntimeError):
        graph_env["app"].invoke(Command(resume="跑"), config)
    snapshot = graph_env["app"].get_state(config)
    assert snapshot.interrupts and snapshot.next == ()
    assert _status(graph_env) == conversation.FAILED

    reply = _say(graph_env, "算了", "1790126100.000100")

    assert reply["text"].startswith("上一次你那句回复没处理完")
    assert _fallback(reply).startswith("方案 v1")
    assert _status(graph_env) == conversation.WAITING
    assert graph_env["harvest_calls"] == [0]

    assert _say(graph_env, "算了", "1790126200.000100") == {"text": query_messages.CANCELLED_TEXT}
    assert graph_env["harvest_calls"] == [0]


def test_failed_llm_step_retries_without_taking_the_new_line(graph_env):
    """模型那一步出错：告诉用户回任意一句重试；重试那一句不进 messages。"""
    graph_env["intent_fails"][0] = 1

    first = _start(graph_env)
    assert "回任意一句话" in first["text"]
    assert _status(graph_env) == conversation.FAILED

    card = _say(graph_env, "好了吗", "1790126100.000100")
    assert _fallback(card).startswith("方案 v1")

    lines = (
        graph_env["app"]
        .get_state({"configurable": {"thread_id": f"slack:{CHANNEL}:{ROOT_TS}"}})
        .values["messages"]
    )
    assert "用户: 好了吗" not in lines
    assert lines[0] == "用户: 帮我找骨传导耳机"


CARD_TS = "1790199000.000100"


def _draft_status(env, card_ts=CARD_TS) -> str:
    return runner_module.conversation_status(
        env["draft_app"].get_state({"configurable": {"thread_id": f"draft:{CHANNEL}:{card_ts}"}})
    )


def _draft_values(env, card_ts=CARD_TS) -> dict:
    return env["draft_app"].get_state({"configurable": {"thread_id": f"draft:{CHANNEL}:{card_ts}"}}).values


def test_mention_under_a_lead_card_opens_a_drafting_conversation(graph_env):
    """线索卡下面 @bot：开的是写稿那张图的对话，那句话就是要写什么，不再问第一句。

    这条红了的症状有两种：在卡片下面开出一个找公司的对话（存档落在 slack: 前缀下）；
    或者用户已经说了「写封邮件」，bot 还问一遍「给这家写什么」
    """
    graph_env["card_roots"][CARD_TS] = 1289

    reply = graph_env["runner"].handle(
        _inbound("帮我写封邮件给他们", ts="1790200000.000300", thread_ts=CARD_TS, mentioned=True)
    )

    assert reply["text"].startswith(
        "好，给 North Field Technologies 写这 1 份：\n第 1 份：邮件，发到 contact@northfield-inc.example → "
        "North Field Technologies 这家公司本身"
    )
    assert graph_env["card_checks"] == [(CHANNEL, CARD_TS)]
    assert _status(graph_env, f"slack:{CHANNEL}:{CARD_TS}") == conversation.NO_CONVERSATION
    assert _draft_status(graph_env) == conversation.WAITING
    values = _draft_values(graph_env)
    assert values["lead_id"] == 1289
    assert values["messages"] == ["用户: 帮我写封邮件给他们"]
    assert values["request"] == "帮我写封邮件给他们"


def test_card_button_asks_first_and_takes_a_reply_without_mention(graph_env):
    """点「要联系」：bot 先问写什么；用户在 thread 里不 @ 回一句，也进写稿那张图。

    这条红了的症状：用户回「邮件」不 @ bot，被当成同事闲聊丢掉（只查了找公司那张图的存档）
    """
    runner = graph_env["runner"]

    first = runner.handle_card_start(CHANNEL, CARD_TS, 1289)

    assert first["text"].startswith("请问你想给 North Field Technologies 写哪些 outreach 信息？")
    assert _draft_status(graph_env) == conversation.WAITING

    reply = runner.handle(_inbound("邮件，发给 Marc-Andre", ts="1790200100.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("好，给 North Field Technologies 写这 1 份：")
    assert "→ Marc-Andre Lavoie（Founder & CEO，这家公司的人）" in reply["text"]
    assert graph_env["card_checks"] == []  # 有存档的 thread 不去读开头那张卡片
    assert _draft_values(graph_env)["messages"] == [f"AI: {first['text']}", "用户: 邮件，发给 Marc-Andre"]
    assert runner._busy == set()


def test_clicking_the_card_again_does_not_restart(graph_env):
    """同一张卡片再开一次（先 @ 过 bot，或者按钮事件重投）：不重开，存档一个字不动。"""
    runner = graph_env["runner"]
    graph_env["card_roots"][CARD_TS] = 1289
    runner.handle(_inbound("帮我写封邮件给他们", ts="1790200000.000300", thread_ts=CARD_TS, mentioned=True))
    before = _draft_values(graph_env)

    reply = runner.handle_card_start(CHANNEL, CARD_TS, 1289)

    assert reply == {"text": conversation.ALREADY_STARTED_TEXT}
    assert _draft_values(graph_env) == before
    assert graph_env["load_calls"] == [1]


def test_card_start_while_the_thread_is_busy(graph_env):
    """卡片下面刚 @ 了 bot、那一轮还在跑时点了按钮：回一句忙，不另开一个。锁是两张图共用的那一把。"""
    runner = graph_env["runner"]
    slack_thread = f"slack:{CHANNEL}:{CARD_TS}"
    runner._busy.add(slack_thread)

    reply = runner.handle_card_start(CHANNEL, CARD_TS, 1289)

    assert reply == {"text": conversation.BUSY_TEXT}
    assert _draft_status(graph_env) == conversation.NO_CONVERSATION
    assert slack_thread in runner._busy  # 别人占着的，不归它放


def test_a_finished_drafting_conversation_says_its_own_line(graph_env):
    """写稿对话结束之后再说话：回写稿那张图自己的结束语，不是找公司那句「再找一批」。"""
    runner = graph_env["runner"]
    runner.handle_card_start(CHANNEL, CARD_TS, 1289)
    runner.handle(_inbound("邮件", ts="1790200100.000100", thread_ts=CARD_TS))
    runner.handle(_inbound("写吧", ts="1790200150.000100", thread_ts=CARD_TS))
    runner.handle(_inbound("都可以了", ts="1790200170.000100", thread_ts=CARD_TS))

    reply = runner.handle(_inbound("再改改", ts="1790200200.000100", thread_ts=CARD_TS))

    assert reply == {"text": draft_messages.FINISHED_TEXT}


def test_a_mention_after_drafting_ended_starts_over_on_the_same_archive(graph_env):
    """写稿对话结束之后又 @ 了 bot：同一份存档从头再跑，那句话就是这一轮要写什么；前几轮的话都还在。

    这条红了的症状：一张卡片起草过一次，就再也起草不了（只回结束语）；
    或者重开时换了一份新存档，前几轮说过的话丢了
    """
    runner = graph_env["runner"]
    runner.handle_card_start(CHANNEL, CARD_TS, 1289)
    plan = runner.handle(_inbound("邮件", ts="1790200100.000100", thread_ts=CARD_TS))["text"]
    runner.handle(_inbound("写吧", ts="1790200150.000100", thread_ts=CARD_TS))
    runner.handle(_inbound("都可以了", ts="1790200170.000100", thread_ts=CARD_TS))
    assert _draft_status(graph_env) == conversation.ENDED

    reply = runner.handle(
        _inbound("邮件，发给 Marc-Andre", ts="1790200200.000100", thread_ts=CARD_TS, mentioned=True)
    )

    assert reply["text"].startswith("好，给 North Field Technologies 写这 1 份：")
    values = _draft_values(graph_env)
    # 第一句是第一问（AI），然后是上一轮的话、计划、「写吧」、稿子摘要、「都可以了」，
    # 最后是这一轮那句：旧的没丢，新的接在后面
    assert values["messages"][1:] == [
        "用户: 邮件",
        f"AI: {plan}",
        "用户: 写吧",
        "AI: [稿子] 第 1 份 邮件→North Field Technologies 第 1 版",
        "用户: 都可以了",
        "用户: 邮件，发给 Marc-Andre",
    ]
    # 上一轮的账本被新一轮的初始 state 清掉了，号从 1 发起
    assert [s["number"] for s in values["plan"]["slots"]] == [1]
    assert values["drafts"]["1"]["status"] == "planned"
    assert values["request"] == "邮件，发给 Marc-Andre"
    assert values["ai_plan"]["drafts"][0]["recipient_ai_understood"] == 2
    assert graph_env["plan_calls"] == [2]
    assert _draft_status(graph_env) == conversation.WAITING


def test_a_finished_query_conversation_is_not_reopened_by_a_mention(graph_env):
    """找公司的对话结束之后再 @：不重开（一个 thread 一次采集），照旧回「去频道里重新 @」。"""
    _start(graph_env)
    _say(graph_env, "算了", "1790126100.000100")

    reply = graph_env["runner"].handle(
        _inbound("<@bot> 再找一批", ts="1790126200.000100", thread_ts=ROOT_TS, mentioned=True)
    )

    assert reply == {"text": query_messages.FINISHED_TEXT}


def test_crash_after_answering_the_first_question_asks_it_again(graph_env):
    """ask_request 收到回答之后崩溃：同一句再问一遍，回答只记一次，不重查库。

    退回靠 before_interrupt 的 ask_request -> retrieve_knowledge：假装 retrieve_knowledge 刚跑完，
    route_after_retrieve 看 request 还是空的，又停回 ask_request
    """
    runner = graph_env["runner"]
    question = runner.handle_card_start(CHANNEL, CARD_TS, 1289)["text"]
    graph_env["ask_fails"][0] = 1

    reply = runner.handle(_inbound("邮件", ts="1790200100.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("你刚才那句没处理成")
    assert reply["text"].endswith(question)
    assert _draft_status(graph_env) == conversation.WAITING

    reply = runner.handle(_inbound("邮件", ts="1790200200.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("好，给 North Field Technologies 写这 1 份：")
    assert _draft_values(graph_env)["messages"].count("用户: 邮件") == 1
    assert graph_env["load_calls"] == [1]
    assert graph_env["plan_calls"] == [1]


def test_card_start_errors_become_a_reply(graph_env):
    """点按钮开对话时出错（连不上库）：变成一句发进卡片 thread 的话，thread 也被放开。"""
    runner = graph_env["runner"]

    @contextlib.contextmanager
    def broken_opener():
        raise RuntimeError("Neon 连不上（测试桩）")
        yield

    runner._saver_opener = broken_opener
    said = []

    runner._run_card_start(CHANNEL, CARD_TS, 1289, said.append)

    assert said == [{"text": "出错了：RuntimeError: Neon 连不上（测试桩）"}]
    assert runner._busy == set()


def test_two_graphs_cannot_share_where_they_start():
    """两张图的 starts_in（或者存档前缀）撞了：进程起来时就停，不等第一句话进来才挑错图。"""
    graph = slack_conversation.query_graph()

    with pytest.raises(ValueError, match="starts_in 撞了"):
        runner_module.ConversationRunner(
            [graph, dataclasses.replace(graph, thread_prefix="x")], root_card=print
        )
    with pytest.raises(ValueError, match="thread_prefix 撞了"):
        runner_module.ConversationRunner(
            [graph, dataclasses.replace(graph, starts_in="x")], root_card=print
        )


def test_mention_under_a_colleagues_thread_does_not_open_a_conversation(graph_env):
    """同事自己开的 thread 下面 @bot：告诉他去频道里 @，也不开对话。"""
    other_ts = "1790199500.000100"
    reply = graph_env["runner"].handle(
        _inbound("帮我找耳机", ts="1790200000.000400", thread_ts=other_ts, mentioned=True)
    )

    assert reply == {"text": conversation.OTHER_THREAD_TEXT}
    assert _status(graph_env, f"slack:{CHANNEL}:{other_ts}") == conversation.NO_CONVERSATION


def test_lines_in_our_own_conversation_never_ask_slack_about_the_root(graph_env):
    """bot 自己的对话里（带不带 @）都不去读 thread 开头：状态已经说明了一切。"""
    _start(graph_env)
    graph_env["runner"].handle(
        _inbound("换成找智能眼镜", ts="1790126100.000100", thread_ts=ROOT_TS, mentioned=True)
    )

    assert graph_env["card_checks"] == []


def test_an_edit_in_our_conversation_is_answered_and_never_enters_the_graph(graph_env):
    """bot 的对话里改了一句：回 EDITED_TEXT，存档一个字不动。"""
    _start(graph_env)
    config = {"configurable": {"thread_id": f"slack:{CHANNEL}:{ROOT_TS}"}}
    before = graph_env["app"].get_state(config).values["messages"]

    reply = graph_env["runner"].handle(
        _inbound("别带 Indiegogo", ts="1790126062.118200", thread_ts=ROOT_TS, edited_ts=EDIT_TS)
    )

    assert reply == {"text": conversation.EDITED_TEXT}
    assert graph_env["app"].get_state(config).values["messages"] == before
    assert _status(graph_env) == conversation.WAITING


def test_an_edit_is_answered_even_while_the_thread_is_busy(graph_env):
    """采集跑着的时候改了一个字：回「改动不算数」，不是「上一句还在处理」。"""
    _start(graph_env)
    runner = graph_env["runner"]
    inbound = _inbound("别带 Indiegogo", ts="1790126062.118200", thread_ts=ROOT_TS, edited_ts=EDIT_TS)
    runner._busy.add(inbound.thread_id)
    said = []

    runner._run(inbound, said.append)

    assert said == [{"text": conversation.EDITED_TEXT}]
    assert inbound.thread_id in runner._busy  # 占着的那一轮没被它放开


def test_an_edit_of_channel_chatter_never_opens_a_connection(graph_env):
    """频道里同事改了自己一句没 @ 的闲聊：不回话，也不开 Neon 连接。"""
    reply = graph_env["runner"].handle(_inbound("哈哈哈哈", edited_ts=EDIT_TS))

    assert reply is None
    assert graph_env["opened"] == [0]


def test_second_line_while_busy_is_not_queued(graph_env):
    """bot 的对话正在跑图时来了第二句：回一句「上一句还在处理」，这句不进图。"""
    _start(graph_env)
    runner = graph_env["runner"]
    inbound = _inbound("等下，再加一条会议麦的", ts="1790126140.000100", thread_ts=ROOT_TS)
    runner._busy.add(inbound.thread_id)
    config = {"configurable": {"thread_id": inbound.thread_id}}
    before = graph_env["app"].get_state(config).values["messages"]
    said = []

    runner._run(inbound, said.append)

    assert said == [{"text": conversation.BUSY_TEXT}]
    assert graph_env["app"].get_state(config).values["messages"] == before
    assert inbound.thread_id in runner._busy  # 别的线程占着的，不归它放


def test_chatter_in_a_colleagues_thread_never_gets_the_busy_line(graph_env):
    """同事的 thread 里两个人快速你一句我一句：bot 一句话都不说。

    这条红了的症状：第一句还在查 Neon（约一秒）时第二句到了，撞上「被占着」，
    bot 往同事的 thread 里发一句「上一句还在处理」。现在不理的消息根本不占 thread。
    """
    runner = graph_env["runner"]
    other_ts = "1790199500.000100"
    first = _inbound("这个耳机不错", ts="1790199600.000100", thread_ts=other_ts)
    second = _inbound("是的", ts="1790199601.000100", thread_ts=other_ts)
    said = []

    runner._busy.add(first.thread_id)  # 假装第一句正占着
    runner._run(second, said.append)
    runner._busy.discard(first.thread_id)
    runner._run(first, said.append)

    assert said == []
    assert runner._busy == set()


def test_status_is_read_again_after_occupying(graph_env, monkeypatch):
    """占住之后重查一次状态，按重查的结果处理，不按占之前查到的。

    场景：两句几乎同时到，第一句「算了」已经把对话跑到 END 并放开；第二句没占的时候
    查到的还是「停在 await_go」。这条红了的症状：第二句拿着过期的状态去 resume 一个
    已经结束的对话。
    """
    _start(graph_env)
    _say(graph_env, "算了", "1790126100.000100")
    runner = graph_env["runner"]
    real_route = runner._route
    calls = []

    def stale_then_real(*args, **kwargs):
        # 第一次（占之前）照真的认，只把处理换成过期的 RESUME；第二次（占之后）原样用真的
        calls.append(1)
        route = real_route(*args, **kwargs)
        return dataclasses.replace(route, action=conversation.RESUME) if len(calls) == 1 else route

    monkeypatch.setattr(runner, "_route", stale_then_real)

    reply = _say(graph_env, "跑", "1790126101.000100")

    assert reply == {"text": query_messages.FINISHED_TEXT}
    assert len(calls) == 2
    assert graph_env["harvest_calls"] == [0]
    assert runner._busy == set()


def test_redelivered_message_is_handled_once(graph_env):
    """Slack 重投的那份（channel 和 ts 一模一样）直接丢掉，不再交给线程池。"""
    runner = graph_env["runner"]
    submitted = []
    runner._pool = type("FakePool", (), {"submit": lambda self, *args: submitted.append(args)})()
    inbound = _inbound("帮我找骨传导耳机", mentioned=True)

    runner.submit(inbound, print)
    runner.submit(inbound, print)

    assert len(submitted) == 1


def test_an_exception_becomes_a_reply_and_frees_the_thread(graph_env):
    """handle 里抛出来的异常变成一句发回 thread 的话，thread_id 也被放开。

    不接的话异常死在线程池里没人取，用户那边一个字都看不到；不放开的话
    这个 thread 以后每一句都只会收到「上一句还在处理」。
    """
    runner = graph_env["runner"]

    @contextlib.contextmanager
    def broken_opener():
        raise RuntimeError("Neon 连不上（测试桩）")
        yield

    runner._saver_opener = broken_opener
    inbound = _inbound("帮我找骨传导耳机", mentioned=True)
    said = []

    runner._run(inbound, said.append)

    assert said == [{"text": "出错了：RuntimeError: Neon 连不上（测试桩）"}]
    assert inbound.thread_id not in runner._busy


def test_the_plan_stop_approve_and_cancel(graph_env):
    """停在计划上：「写吧」去写稿、停在看稿那一停；「都可以了」走到 END 列出终稿；
    另一张卡片上对着计划「算了」走到 END 回不写了那句。
    """
    runner = graph_env["runner"]
    runner.handle_card_start(CHANNEL, CARD_TS, 1289)
    runner.handle(_inbound("邮件", ts="1790200100.000100", thread_ts=CARD_TS))

    reply = runner.handle(_inbound("写吧", ts="1790200200.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("第 1 份 · 邮件，发到 contact@northfield-inc.example → ")
    assert "稿子 1" in reply["text"]
    assert _draft_status(graph_env) == conversation.WAITING

    reply = runner.handle(_inbound("都可以了", ts="1790200250.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("定稿 1 份：\n第 1 份 邮件 → North Field Technologies（第 1 版）")
    assert _draft_status(graph_env) == conversation.ENDED

    other = "1790199100.000100"
    runner.handle_card_start(CHANNEL, other, 1289)
    runner.handle(_inbound("邮件", ts="1790200300.000100", thread_ts=other))

    reply = runner.handle(_inbound("算了", ts="1790200400.000100", thread_ts=other))

    assert reply == {"text": draft_messages.CANCELLED_TEXT}
    assert _draft_status(graph_env, other) == conversation.ENDED


def test_a_revision_plans_again_and_stops_again(graph_env):
    """对着计划说要改：重定一版（plan_drafts 再调一次），再停在新计划上等他。"""
    runner = graph_env["runner"]
    runner.handle_card_start(CHANNEL, CARD_TS, 1289)
    runner.handle(_inbound("邮件", ts="1790200100.000100", thread_ts=CARD_TS))

    reply = runner.handle(_inbound("邮件，发给 Marc-Andre", ts="1790200200.000100", thread_ts=CARD_TS))

    assert "→ Marc-Andre Lavoie（Founder & CEO，这家公司的人）" in reply["text"]
    assert graph_env["plan_calls"] == [2]
    assert graph_env["judge_calls"] == [1]
    assert _draft_status(graph_env) == conversation.WAITING


def test_crash_after_answering_the_plan_sends_the_same_plan_again(graph_env):
    """await_plan 收到回话之后崩溃：同一份计划再发一遍，回话只记一次，不为了退回多调一次模型。

    退回靠 before_interrupt 的 await_plan -> plan_drafts：假装 plan_drafts 刚跑完，固定边点亮 await_plan。
    plan_drafts 本身不重跑，所以模型还是只调了一次、计划还是同一份
    """
    runner = graph_env["runner"]
    runner.handle_card_start(CHANNEL, CARD_TS, 1289)
    plan = runner.handle(_inbound("邮件", ts="1790200100.000100", thread_ts=CARD_TS))["text"]
    graph_env["await_plan_fails"][0] = 1

    reply = runner.handle(_inbound("写吧", ts="1790200200.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("你刚才那句没处理成")
    assert reply["text"].endswith(plan)
    assert graph_env["plan_calls"] == [1]
    assert _draft_status(graph_env) == conversation.WAITING

    reply = runner.handle(_inbound("写吧", ts="1790200300.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("第 1 份 · 邮件")
    assert _draft_values(graph_env)["messages"].count("用户: 写吧") == 1


def test_crash_after_answering_the_drafts_sends_the_same_drafts_again(graph_env):
    """await_write 收到回话之后崩溃：同一批稿子再发一遍，回话只记一次，不为了退回再写一遍稿。

    退回靠 before_interrupt 的 await_write -> write_draft：假装 write_draft 刚跑完，固定边点亮 await_write。
    write_draft 本身不重跑，所以稿子还是那一版、写稿只调了一次
    """
    runner = graph_env["runner"]
    runner.handle_card_start(CHANNEL, CARD_TS, 1289)
    runner.handle(_inbound("邮件", ts="1790200100.000100", thread_ts=CARD_TS))
    drafts = runner.handle(_inbound("写吧", ts="1790200200.000100", thread_ts=CARD_TS))["text"]
    graph_env["await_write_fails"][0] = 1

    reply = runner.handle(_inbound("都可以了", ts="1790200300.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("你刚才那句没处理成")
    assert reply["text"].endswith(drafts)
    assert graph_env["write_calls"] == [1]
    assert graph_env["feedback_calls"] == [0]

    reply = runner.handle(_inbound("都可以了", ts="1790200400.000100", thread_ts=CARD_TS))

    assert reply["text"].startswith("定稿 1 份：")
    assert _draft_values(graph_env)["messages"].count("用户: 都可以了") == 1
    assert graph_env["write_calls"] == [1]
