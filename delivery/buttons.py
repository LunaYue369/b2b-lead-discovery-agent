"""Slack 按钮点下来之后的解析和回执。这一层不连库、不起进程。

按钮那条路分两半，这里是「怎么读懂这一下点击、点完那张卡片该长什么样」，
另一半（长连接、事件分发、写库）在 triggers/slack_buttons.py。分开的理由
跟 cards 和 send 分开一样：解析和回执是纯函数，能脱离 token 和网络单独测，
而那正是最容易写错的部分 —— Slack 的回调载荷有两种形状，字段名还不一样。

这是 scheduled discovery 唯一的 feedback 通路，而 scheduled 才是每天出量
的那一路。不能只靠「Riley 恢复 interactive thread」收反馈 —— 那样占绝大
多数的 lead 拿不到 feedback，学习闭环会饿死。

三个按钮的落库形状（feedback_db 那边已经写好，这里只解析）：

    要联系    approve   点了直接落库，不弹窗
    这次先不  skip      同上
    不该联系  reject    弹一个 modal 问一句为什么，可以跳过不填

只在 reject 上加这道摩擦：她一天要过很多张卡，每张都弹窗会让人干脆不点；
而 approve 的理由基本能从 lead 自己推出来，reject 的理由推不出来 ——
「品类不对」和「这家太大」指向完全不同的修法，一个改 query 一个改打分权重。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from delivery.cards import ACTION_APPROVE, ACTION_REJECT, ACTION_SKIP
from models import FeedbackType

# action_id 到 feedback 类型的对照。action_id 是 cards.py 那边定的常量，
# 两处必须对得上 —— 这张表是那个对应关系唯一的落点。
#
# ignored 不在这里：那一档不是点出来的，是定时扫描写的（超过 N 天没动作）。
FEEDBACK_BY_ACTION = {
    ACTION_APPROVE: FeedbackType.APPROVE,
    ACTION_SKIP: FeedbackType.SKIP,
    ACTION_REJECT: FeedbackType.REJECT,
}

# reject 那个 modal 的两个 id。提交回调靠 callback_id 认出「这是拒绝理由
# 那个弹窗」，取值靠 block_id 加 action_id 定位那个输入框。
# 三个都是接口不是文案，改了要跟 triggers/slack_buttons.py 一起改。
REJECT_MODAL_CALLBACK = "lead_reject_reason"
REJECT_NOTE_BLOCK = "reject_note_block"
REJECT_NOTE_ACTION = "reject_note_input"


@dataclass(frozen=True)
class ButtonClick:
    """一次按钮点击里我们要的全部东西。

    一个实际值：

        ButtonClick(lead_id=1281, run_id=27,
                    feedback=FeedbackType.APPROVE,
                    user_id="U04ABCD", channel="C0000000001",
                    message_ts="1788400486.110719", trigger_id="9012.34.abcd")

    lead_id 和 run_id 从按钮的 value 里来（那是 cards.py 塞进去的 JSON）。
    刻意不带 query_id：一条 lead 可能被同一跑里两条 query 同时命中，
    那正是 corroboration 的分子，塞一个进去等于任选一条、静默错归因。
    归因去查 lead_discovery_hits (query_id, lead_id, run_id)，
    而查它需要的正好是这两个数。

    channel 和 message_ts 是为了点完之后把那张卡片改成「已收到」。
    这两个值来自 Slack 的回调载荷，不是我们发消息时存下来的那一份 ——
    按钮进程和发消息的进程根本不是同一个进程（一个常驻、一个跑几分钟就
    退出），所以内存里那份 PushResult 对它没用。这也是为什么 ts 不需要落库。

    trigger_id 不是 modal 的 id，这一点很容易记反。它是 Slack 为「这一下
    交互」临时签发的一张一次性凭证：

        谁签的      Slack。她按下按钮的那一刻，连同载荷一起发过来
        有什么用    唯一的用途是换一个弹窗 —— views.open 必须带它，
                    不带就是 401。它是「这个人刚刚真的点了一下」的证明，
                    所以 Slack 才允许我们在她屏幕上弹东西
        活多久      三秒，而且只能用一次。用过或者过期都换不出弹窗了
        谁有 id     弹窗自己有 id（views.open 的回复里那个 view.id），
                    那才是「modal 的 id」，跟这个是两回事

    所以只有 reject 那一支用得上它（approve 和 skip 不弹窗）。三秒这个
    限制也决定了那一支的写法：handler 里第一句必须先 ack、然后立刻
    views_open，中间不许插任何写库或者查库的动作 —— 那些都可能超过三秒，
    而超了她就会看到「弹窗打不开」。这也是 reject 那一支刻意什么都不写库
    的第二个理由（第一个是她可能点「算了」）。
    """

    lead_id: int
    run_id: int
    feedback: FeedbackType
    user_id: str
    channel: str
    message_ts: str
    trigger_id: str


class MalformedClick(ValueError):
    """载荷里少了我们要的东西，或者形状不对。

    单独一个类型，是为了让分发那一层能把它跟「库写失败」分开处理：
    载荷坏了重试没有意义（同一个载荷再来一次还是坏的），而库写失败
    可以重试。
    """


def parse_click(payload: dict) -> ButtonClick:
    """把 Slack 的按钮回调载荷读成一个 ButtonClick。纯函数。

    输入：block_actions 那种回调的整个载荷（Bolt 给的 body）
    输出：一个 ButtonClick

    Slack 的载荷长这样（只画我们要的那几个字段）：

        {"user": {"id": "U04ABCD"},
         "trigger_id": "9012.34.abcd",
         "container": {"channel_id": "C0000000001",
                       "message_ts": "1788400486.110719"},
         "channel": {"id": "C0000000001"},
         "message": {"ts": "1788400486.110719"},
         "actions": [{"action_id": "lead_approve",
                      "block_id": "lead:1281:run:27",
                      "value": "{\\"lead_id\\":1281,\\"run_id\\":27}"}]}

    四种情况：

    一、正常一下点击

        输入   上面那个载荷
        输出   ButtonClick(lead_id=1281, run_id=27,
                           feedback=FeedbackType.APPROVE, user_id="U04ABCD",
                           channel="C0000000001",
                           message_ts="1788400486.110719",
                           trigger_id="9012.34.abcd")

    二、value 不是我们塞的那个 JSON（应用换过版本、老卡片还挂在频道里）

        输入   actions[0]["value"] = "1281"
        输出   不返回，抛 MalformedClick("按钮 value 不是 JSON：'1281'")
        后果   分发那一层（triggers.slack_buttons._parsed_or_told）会在
               频道里只给她一个人回一句「这张卡片认不出来了」——
               必须告诉她，不然她点下去毫无反应，而「没反应」跟
               「系统挂了」在她那边长得一模一样。
               不去猜「大概是 lead_id」：猜错会把反馈写到另一家公司
               头上，而那条错会一直留在库里当训练信号

    三、action_id 不在我们那三个里

        输入   actions[0]["action_id"] = "lead_snooze"
        输出   抛 MalformedClick("不认识的 action_id：'lead_snooze'")
        后果   同上。三个 action_id 是 cards.py 定的常量，出现第四个说明
               卡片和这一层不是同一个版本

    四、载荷里缺 container 或者 actions

        输入   {}
        输出   抛 MalformedClick，说清缺了哪一项
        后果   这种情况说明订阅的事件类型不对（比如订到了 message 事件），
               不是数据问题。抛出去让日志里留下痕迹

    channel 和 message_ts 优先取 container 里那一份：Slack 文档说
    block_actions 的 container 一定有这两项，而顶层的 channel / message
    在某些交互（modal 里的按钮）里根本不存在。取不到再退回顶层。
    """
    actions = payload.get("actions") or []
    if not actions:
        raise MalformedClick("载荷里没有 actions，订阅的事件类型可能不对")

    action = actions[0]
    action_id = action.get("action_id", "")
    feedback = FEEDBACK_BY_ACTION.get(action_id)
    if feedback is None:
        raise MalformedClick(f"不认识的 action_id：{action_id!r}")

    raw_value = action.get("value") or ""
    try:
        value = json.loads(raw_value)
        lead_id = int(value["lead_id"])
        run_id = int(value["run_id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
        # 不猜。猜错会把反馈写到另一家公司头上，而那条错会一直留在库里
        # 当训练信号 —— 比这一下点击丢掉严重得多。
        raise MalformedClick(f"按钮 value 读不出 lead_id 和 run_id：{raw_value!r}") from err

    container = payload.get("container") or {}
    channel = container.get("channel_id") or (payload.get("channel") or {}).get("id")
    message_ts = container.get("message_ts") or (payload.get("message") or {}).get("ts")
    if not channel or not message_ts:
        raise MalformedClick("载荷里没有 channel 或 message_ts，改不了那张卡片")

    return ButtonClick(
        lead_id=lead_id,
        run_id=run_id,
        feedback=feedback,
        user_id=(payload.get("user") or {}).get("id", ""),
        channel=channel,
        message_ts=message_ts,
        trigger_id=payload.get("trigger_id", ""),
    )


# 三种态度在回执里怎么写。用中文短句不用枚举值：这一行是给人看的。
_DECISION_LABEL = {
    FeedbackType.APPROVE: ":white_check_mark: 已记下：要联系",
    FeedbackType.SKIP: ":clock3: 已记下：这次先不",
    FeedbackType.REJECT: ":no_entry: 已记下：不该联系",
}


def acknowledged_blocks(blocks: list[dict], click: ButtonClick, *, note: str = "") -> list[dict]:
    """把那张卡片改成「已收到」的样子：去掉按钮，加一行回执。纯函数。

    输入：那条消息原来的块列表（从回调载荷里取的）；这一下点击；
          reject 时她填的理由（没填就是空串）
    输出：新的块列表，可以直接交给 chat.update

    三种情况：

    一、正常一下 approve

        输入   blocks 是原来那十几块，click.feedback=APPROVE
        输出   同样那十几块，但 actions 那一块被换成一行
               ":white_check_mark: 已记下：要联系 · <@U04ABCD>"
        后果   按钮没了，她点不了第二次 —— 而 Slack 的回调本来就会重投，
               去掉按钮是最直接的一道防线（另一半幂等在写库那边，还没做）

    二、reject 而且填了理由

        输入   click.feedback=REJECT，note="这家是做众筹营销的代理"
        输出   回执那一行后面多一段引用：
               "> 这家是做众筹营销的代理"
        后果   理由留在卡片上，而不是只进库 —— 频道本身就是那批判断的
               档案，以后翻回来看「当初为什么拒的」不用查库

    三、原消息里没有 actions 那一块（重复点击，上一次已经换掉了）

        输入   blocks 里一块 actions 都没有
        输出   原样返回，不加第二行回执
        后果   幂等。Slack 的回调会重投，重投时这张卡片已经是「已收到」的
               样子了；再加一行会变成两行回执，看起来像她点了两次

    刻意保留除按钮以外的全部内容：公司名、分数明细、原话、证据链接都
    留在那儿。这张卡片是那次判断的档案 —— 三周后回来看「当初为什么
    approve 的」，要看到的是当时那些分数和那句原话，不是一行「已记下」。
    """
    # 没有按钮说明这张卡片已经被换过了，直接原样还回去。
    if not any(block.get("type") == "actions" for block in blocks):
        return list(blocks)

    line = _DECISION_LABEL[click.feedback]
    if click.user_id:
        # <@U04ABCD> 是 Slack 的用户提及语法，客户端会渲染成她的名字。
        line += f"  ·  <@{click.user_id}>"
    if note:
        line += f"\n> {note}"

    out: list[dict] = []
    for block in blocks:
        if block.get("type") == "actions":
            # block_id 从按钮那一块搬到回执这一块上，例 "lead:1281:run:27"。
            # 不搬的话 Slack 会给回执随手编一个随机串，点过按钮的
            # 卡片就再也认不出是线索卡了：对话那条路靠它分辨卡片下面的 thread
            receipt = {"type": "section", "text": {"type": "mrkdwn", "text": line}}
            if block.get("block_id"):
                receipt["block_id"] = block["block_id"]
            out.append(receipt)
            continue
        out.append(block)
    return out


def reject_modal(click: ButtonClick) -> dict:
    """拼「为什么不该联系」那个 modal。纯函数。

    输入：这一下点击（要把它原样带进 private_metadata）
    输出：views.open 要的那个 view 字典

    一个例子：

        输入   ButtonClick(lead_id=1281, run_id=27, feedback=REJECT,
                           user_id="U04ABCD", channel="C0000000001",
                           message_ts="1788400486.110719", ...)
        输出   {"type": "modal",
                "callback_id": "lead_reject_reason",
                "private_metadata": '{"lead_id":1281,...}',
                "title": ...,
                "submit": {"text": "记下"},
                "blocks": [一个可选的多行输入框]}

    private_metadata 里塞的是整个 ButtonClick 的四个关键字段。为什么必须
    塞：modal 提交回来的是另一个回调（view_submission），那个载荷里
    没有原来那条消息的 channel 和 ts —— 而改那张卡片要这两个值。
    Slack 只给了这么一个能把上下文带过去的口子。

    optional 那一项设成 True：理由可以不填。她一天要过很多张卡，
    强制填理由的结果是她干脆不点 reject，而 reject 是最值钱的一种反馈。
    留空提交也照样记一行 reject，只是 note 是空的。

    trigger_id 不进 private_metadata：它三秒就过期，带过去也没用。
    """
    return {
        "type": "modal",
        "callback_id": REJECT_MODAL_CALLBACK,
        "private_metadata": json.dumps(
            {
                "lead_id": click.lead_id,
                "run_id": click.run_id,
                "channel": click.channel,
                "message_ts": click.message_ts,
            },
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "不该联系"},
        "submit": {"type": "plain_text", "text": "记下"},
        "close": {"type": "plain_text", "text": "算了"},
        "blocks": [
            {
                "type": "input",
                "block_id": REJECT_NOTE_BLOCK,
                "optional": True,
                "label": {"type": "plain_text", "text": "为什么？（可以不填）"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": REJECT_NOTE_ACTION,
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "比如：品类不对 / 这家太大 / 是代理不是买方",
                    },
                },
            }
        ],
    }


def parse_reject_submission(payload: dict) -> tuple[ButtonClick, str]:
    """把 modal 提交的载荷读成 (ButtonClick, 理由)。纯函数。

    输入：view_submission 那种回调的整个载荷
    输出：(一个 ButtonClick，她填的理由；没填就是空串)

    载荷长这样（只画我们要的）：

        {"user": {"id": "U04ABCD"},
         "view": {"callback_id": "lead_reject_reason",
                  "private_metadata": "{\\"lead_id\\":1281,\\"run_id\\":27,...}",
                  "state": {"values": {"reject_note_block":
                            {"reject_note_input": {"value": "品类不对"}}}}}}

    四种情况：

    一、填了理由

        输入   上面那个载荷
        输出   (ButtonClick(lead_id=1281, run_id=27, feedback=REJECT, ...),
                "品类不对")

    二、留空提交

        输入   那个输入框的 value 是 None（optional 的输入框留空就是 None）
        输出   (同样的 ButtonClick, "")
        后果   照样记一行 reject，只是 note 是空的。强制填理由的结果是
               她干脆不点 reject，而 reject 是最值钱的一种反馈

    三、private_metadata 读不出来

        输入   view.private_metadata 是空串或者不是 JSON
        输出   抛 MalformedClick
        后果   这时候连「她拒的是哪一条」都不知道，只能放弃这一下。
               不猜 —— 猜错会把 reject 写到另一家公司头上

    四、callback_id 不是我们那个 modal

        输入   view.callback_id = "something_else"
        输出   抛 MalformedClick
        后果   说明分发那一层挂错了 handler。以后加第二个 modal 时，
               这一行是唯一能挡住「两个 modal 的提交串到一起」的地方

    trigger_id 在这个载荷里没有意义（modal 已经开着了），所以填空串。
    """
    view = payload.get("view") or {}
    if view.get("callback_id") != REJECT_MODAL_CALLBACK:
        raise MalformedClick(f"不是拒绝理由那个 modal：{view.get('callback_id')!r}")

    try:
        meta = json.loads(view.get("private_metadata") or "")
        lead_id = int(meta["lead_id"])
        run_id = int(meta["run_id"])
        channel = meta["channel"]
        message_ts = meta["message_ts"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
        raise MalformedClick(
            f"modal 的 private_metadata 读不出来：{view.get('private_metadata')!r}"
        ) from err

    # optional 的输入框留空时 value 是 None，不是空串。
    values = (view.get("state") or {}).get("values") or {}
    note = ((values.get(REJECT_NOTE_BLOCK) or {}).get(REJECT_NOTE_ACTION) or {}).get("value")

    click = ButtonClick(
        lead_id=lead_id,
        run_id=run_id,
        feedback=FeedbackType.REJECT,
        user_id=(payload.get("user") or {}).get("id", ""),
        channel=channel,
        message_ts=message_ts,
        trigger_id="",
    )
    return click, (note or "").strip()
