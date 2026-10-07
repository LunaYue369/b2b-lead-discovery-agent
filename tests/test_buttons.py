"""按钮那一层：读懂载荷、拼回执、拼 modal。全是纯函数，不连库不连网。

这一层最值得测的是「载荷坏了会怎样」—— Slack 的回调有两种形状、字段名
还不一样，而坏载荷在真频道里造不出来（要么改应用版本、要么留着一张旧
卡片等三周）。
"""

from __future__ import annotations

import json

import pytest

from delivery import buttons
from delivery.cards import ACTION_APPROVE, ACTION_REJECT, ACTION_SKIP
from models import FeedbackType


def _payload(action_id: str = ACTION_APPROVE, value: str | None = None, **overrides) -> dict:
    """造一个 block_actions 载荷，形状跟 Slack 真发来的一样。"""
    body = {
        "user": {"id": "U04ABCD"},
        "trigger_id": "9012.34.abcd",
        "container": {"channel_id": "C0000000001", "message_ts": "1788400486.110719"},
        "actions": [
            {
                "action_id": action_id,
                "block_id": "lead:1281:run:27",
                "value": value
                if value is not None
                else json.dumps({"lead_id": 1281, "run_id": 27}, separators=(",", ":")),
            }
        ],
    }
    body.update(overrides)
    return body


def test_a_normal_click_is_read_into_all_seven_fields():
    """正常一下点击要读出七项，一项都不能缺。

    lead_id 和 run_id 从按钮的 value 里来；channel 和 message_ts 是为了
    点完之后改那张卡片，而它们来自 Slack 的载荷 —— 不是发消息时存下来的
    那一份，因为按钮进程和发消息的进程根本不是同一个进程。
    """
    click = buttons.parse_click(_payload())

    assert click.lead_id == 1281
    assert click.run_id == 27
    assert click.feedback is FeedbackType.APPROVE
    assert click.user_id == "U04ABCD"
    assert click.channel == "C0000000001"
    assert click.message_ts == "1788400486.110719"
    assert click.trigger_id == "9012.34.abcd"


def test_each_button_maps_to_its_feedback_type():
    """三个按钮各对一种态度，对照表只有一处。"""
    assert buttons.parse_click(_payload(ACTION_APPROVE)).feedback is FeedbackType.APPROVE
    assert buttons.parse_click(_payload(ACTION_SKIP)).feedback is FeedbackType.SKIP
    assert buttons.parse_click(_payload(ACTION_REJECT)).feedback is FeedbackType.REJECT
    # ignored 不在这张表里：那一档不是点出来的，是定时扫描写的
    assert FeedbackType.IGNORED not in buttons.FEEDBACK_BY_ACTION.values()


@pytest.mark.parametrize(
    ("value", "why"),
    [("1281", "光一个数字"), ("", "空的"), ('{"run_id":27}', "少了 lead_id")],
)
def test_a_value_we_cannot_read_is_refused_not_guessed(value, why):
    """value 读不出 lead_id 和 run_id 就拒掉，不猜。

    猜错会把反馈写到另一家公司头上，而那条错会一直留在库里当训练信号 ——
    比这一下点击丢掉严重得多。
    """
    with pytest.raises(buttons.MalformedClick, match="读不出"):
        buttons.parse_click(_payload(value=value))


def test_an_unknown_action_id_is_refused():
    """action_id 不在那三个里就拒掉。

    三个 action_id 是 cards.py 定的常量，出现第四个说明频道里那张卡片
    和这一层不是同一个版本。
    """
    with pytest.raises(buttons.MalformedClick, match="不认识的 action_id"):
        buttons.parse_click(_payload("lead_snooze"))


def test_a_payload_without_actions_is_refused():
    """载荷里没有 actions 说明订阅的事件类型不对。"""
    with pytest.raises(buttons.MalformedClick, match="没有 actions"):
        buttons.parse_click({})


def test_the_channel_falls_back_to_the_top_level_fields():
    """container 里没有就退回顶层的 channel 和 message。

    Slack 文档说 block_actions 的 container 一定有这两项，但顶层那一份
    在某些交互里才有，所以两处都认。
    """
    body = _payload()
    del body["container"]
    body["channel"] = {"id": "C999"}
    body["message"] = {"ts": "1700000000.000001"}

    click = buttons.parse_click(body)
    assert click.channel == "C999"
    assert click.message_ts == "1700000000.000001"


def test_a_payload_with_no_channel_at_all_is_refused():
    """连 channel 都没有就拒掉：那样改不了那张卡片。"""
    body = _payload()
    del body["container"]
    with pytest.raises(buttons.MalformedClick, match="改不了那张卡片"):
        buttons.parse_click(body)


def _blocks_with_buttons() -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "━━━"}},
        {"type": "header", "text": {"type": "plain_text", "text": ":office: Chatterly Inc."}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*A 档*    *80 / 100*"}},
        {"type": "actions", "block_id": "lead:1281:run:27", "elements": [{"type": "button"}]},
    ]


def test_the_acknowledged_card_loses_the_buttons_and_keeps_everything_else():
    """回执那一下：按钮换成一行「已记下」，别的一个字不动。

    保留公司名、分数、原话、证据链接是刻意的 —— 这张卡片是那次判断的
    档案。三周后回来看「当初为什么 approve 的」，要看到的是当时那些
    分数和那句原话，不是一行「已记下」。

    去掉按钮同时是一道软防线：她点不了第二次。机器重投挡不住，
    那一半幂等还没做。
    """
    click = buttons.parse_click(_payload())
    out = buttons.acknowledged_blocks(_blocks_with_buttons(), click)

    assert not any(b["type"] == "actions" for b in out)
    assert len(out) == 4  # 块数不变，只是最后一块换了
    assert "Chatterly Inc." in str(out)  # 原来的内容还在
    last = out[-1]["text"]["text"]
    assert "已记下：要联系" in last
    assert "<@U04ABCD>" in last  # Slack 会渲染成她的名字


def test_the_receipt_keeps_the_lead_block_id():
    """按钮换成回执之后，block_id 留在回执上。

    这条红了的症状：点过按钮的线索卡下面 @bot，收到的是「这个 thread 不是我的对话」
    而不是起草那一句 —— 对话那条路靠这个 block_id 认线索卡（conversation.lead_card_id），
    不留的话 Slack 会给回执随手编一个。
    """
    click = buttons.parse_click(_payload())
    out = buttons.acknowledged_blocks(_blocks_with_buttons(), click)

    assert out[-1]["type"] == "section"
    assert out[-1]["block_id"] == "lead:1281:run:27"


def test_a_reject_note_shows_up_on_the_card():
    """填了理由就留在卡片上，不是只进库。

    频道本身就是那批判断的档案，以后翻回来看「当初为什么拒的」
    不用查库。
    """
    click = buttons.parse_click(_payload(ACTION_REJECT))
    out = buttons.acknowledged_blocks(
        _blocks_with_buttons(), click, note="这家是做众筹营销的代理"
    )
    text = out[-1]["text"]["text"]
    assert "已记下：不该联系" in text
    assert "> 这家是做众筹营销的代理" in text


def test_acknowledging_twice_does_not_add_a_second_line():
    """已经是「已收到」的卡片再来一次，原样返回。

    Slack 的回调会重投，重投时这张卡片已经换过了；再加一行会变成两行
    回执，看起来像她点了两次。
    """
    click = buttons.parse_click(_payload())
    once = buttons.acknowledged_blocks(_blocks_with_buttons(), click)
    twice = buttons.acknowledged_blocks(once, click)
    assert twice == once


def test_the_reject_modal_carries_the_context_it_will_need_later():
    """modal 要把 lead_id、run_id、channel、message_ts 带进 private_metadata。

    modal 提交回来的是另一个回调（view_submission），那个载荷里没有原来
    那条消息的 channel 和 ts —— 而改那张卡片要这两个值。Slack 只给了
    private_metadata 这么一个能把上下文带过去的口子。
    """
    click = buttons.parse_click(_payload(ACTION_REJECT))
    view = buttons.reject_modal(click)

    assert view["callback_id"] == buttons.REJECT_MODAL_CALLBACK
    meta = json.loads(view["private_metadata"])
    assert meta == {
        "lead_id": 1281,
        "run_id": 27,
        "channel": "C0000000001",
        "message_ts": "1788400486.110719",
    }
    # 理由可以不填：强制填的结果是她干脆不点 reject，
    # 而 reject 是最值钱的一种反馈
    assert view["blocks"][0]["optional"] is True
    # trigger_id 不进去：它三秒就过期，带过去也没用
    assert "trigger_id" not in view["private_metadata"]


def _submission(note: str | None = "品类不对", callback_id: str | None = None) -> dict:
    return {
        "user": {"id": "U04ABCD"},
        "view": {
            "callback_id": callback_id or buttons.REJECT_MODAL_CALLBACK,
            "private_metadata": json.dumps(
                {
                    "lead_id": 1281,
                    "run_id": 27,
                    "channel": "C0000000001",
                    "message_ts": "1788400486.110719",
                },
                separators=(",", ":"),
            ),
            "state": {
                "values": {
                    buttons.REJECT_NOTE_BLOCK: {buttons.REJECT_NOTE_ACTION: {"value": note}}
                }
            },
        },
    }


def test_a_reject_submission_is_read_into_a_click_and_a_note():
    """modal 提交读出来是 (ButtonClick, 理由)。"""
    click, note = buttons.parse_reject_submission(_submission())

    assert click.lead_id == 1281 and click.run_id == 27
    assert click.feedback is FeedbackType.REJECT
    assert click.channel == "C0000000001"
    assert click.message_ts == "1788400486.110719"
    assert note == "品类不对"


def test_an_empty_note_is_still_a_valid_reject():
    """留空提交照样是一条 reject，只是 note 是空的。

    optional 的输入框留空时 value 是 None，不是空串 —— 这一条容易写错。
    """
    click, note = buttons.parse_reject_submission(_submission(note=None))
    assert click.feedback is FeedbackType.REJECT
    assert note == ""


def test_a_submission_from_another_modal_is_refused():
    """callback_id 不对就拒掉。

    以后加第二个 modal 时，这一行是唯一能挡住「两个 modal 的提交串到
    一起」的地方。
    """
    with pytest.raises(buttons.MalformedClick, match="不是拒绝理由那个 modal"):
        buttons.parse_reject_submission(_submission(callback_id="something_else"))


def test_a_submission_without_metadata_is_refused():
    """private_metadata 读不出来就放弃这一下，不猜。

    这时候连「她拒的是哪一条」都不知道，猜错会把 reject 写到另一家
    公司头上。
    """
    body = _submission()
    body["view"]["private_metadata"] = ""
    with pytest.raises(buttons.MalformedClick, match="private_metadata"):
        buttons.parse_reject_submission(body)
