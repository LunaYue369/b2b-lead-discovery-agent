"""常驻进程：接 Slack 的按钮点击，写一行 feedback，把那张卡片改成已收到。
也接频道里的消息，把跟 bot 说的话交给找公司、写稿那两张图
（triggers/interactive_discovery/runner.py 调度，每张图自己的那份在 <图>/slack_conversation.py）。

    python -m triggers.slack_buttons          起长连接，一直挂着

按钮和消息必须在同一个进程里收：同一个 app token 开两条 Socket Mode 连接时，
每个事件只投给其中一条，不是广播。拆成两个进程，按钮点击就可能被投到只认识
消息的那个进程，静默丢掉。

这是两个进程里的第二个，跟 scheduled_discovery 是完全不同的形状：

    scheduled_discovery   时钟拉起，跑几分钟，跑完退出
    这里                  永远在线，Socket Mode 长连接，等人点按钮

分开的理由有两条，任一条都够。时间尺度不同：采集打分几分钟就完了，而
Riley 可能三天后才点那个按钮，那时候前一个进程早就退出了。失败域不同：
采集挂了不该让按钮失灵，按钮进程重启也不该影响明天的采集。

用 Socket Mode 不用 HTTP 回调：那是一条我们主动拨出去的长连接，Slack
不需要能访问到我们 —— 不用公网 IP、不用端口、不用 Request URL。
这也是 EC2 每次重启换 IP 都不影响它的原因。

一下点击在这套东西里怎么流动（这里没有轮询，全程是推的）：

    1. 进程起来时 SocketModeHandler 拿 app token 跟 Slack 建一条
       WebSocket，然后 start() 就阻塞在那儿收消息。这条连接是我们拨出去
       的，所以 Slack 那边不需要知道我们在哪
    2. Riley 按下「要联系」。Slack 服务器把一个 block_actions 载荷顺着
       那条已经建好的连接推过来 —— 我们没有去问，是它主动送的
    3. Bolt 收到之后看载荷里的 actions[0].action_id，去它自己的注册表里
       找哪个函数认领了这个 action_id。那张注册表就是下面 build_app 里
       app.action(...) 那几句建起来的
    4. 找到了就调那个函数，参数按名字注入（ack / body / client / logger
       都是 Bolt 按参数名给的，不是位置参数）
    5. 那个函数第一句 ack() 告诉 Slack「收到了」。三秒内必须 ack，
       不然 Slack 认为我们挂了，会把这一下点击重投一遍
    6. ack 之后才是干活：解析载荷、写库、改卡片。这几步慢一点没关系，
       Slack 已经不等了

所以「实时监控」其实不是监控：没有循环去问 Slack「有人点了吗」，
是那条长连接上有人推过来。进程要做的只有两件事 —— 保持连接、
把推过来的东西路由到对的函数。

这个文件只做编排。读懂载荷、拼回执、拼 modal 全在 delivery/buttons.py
（纯函数，脱离 token 就能测）；写库在 persistence/feedback_db.py。
这里负责把它们串起来，加上「一次点击一个事务」。

一次点击做的事，顺序固定：

    ① 读懂载荷        buttons.parse_click
    ② 写一行 feedback feedback_db.record_feedback
    ③ approve 的话推进 in_draft   feedback_db.advance_to_draft
    ④ 把那张卡片改成已收到        chat.update

②③ 在同一个事务里，先记账后改状态：账本写进去就不该回退，而状态推进
可以重试。④ 在事务提交之后 —— 卡片改了收不回来，而库那边还能回滚。

还没做幂等。Slack 的回调三秒不回应就重投，而 lead_feedback 是
append-only，重投会再写一行一样的 approve。当前态度不受影响（取最新
一行），但按行数算 yield 就多算了。眼下的一道软防线是 ④ 会把按钮去掉，
所以人点不了第二次；机器重投挡不住。Alex 说这一条后续做。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from delivery import buttons, conversation
from models import FeedbackType
from persistence import db, feedback_db
from triggers.interactive_discovery.drafting.slack_conversation import drafting_graph
from triggers.interactive_discovery.query.slack_conversation import query_graph
from triggers.interactive_discovery.runner import ConversationRunner, setup_checkpointer


def handle_click(click: buttons.ButtonClick, *, note: str = "") -> None:
    """写一行 feedback，approve 的话再把 lead 推进 in_draft。一次一个事务。

    输入：读懂了的那一下点击；reject 时她填的理由（没填就是空串）
    输出：无

    三种情况：

    一、approve

        输入   click.feedback=APPROVE  lead_id=1281  run_id=27
               user_id="U04ABCD"
        库里   lead_feedback 多一行 (1281, 'approve', null, 'U04ABCD', 27)
               leads 第 1281 行的 status 从 pushed 变成 in_draft，
               cooldown_until 原样不动
        输出   无
        后果   下一跑的 build_cards 会跳过这一条，不再推第二张卡。
               但它照样会被重新分析（lead_states 的分流只看 excluded
               和冷却期），只是不会再上卡片

    二、skip 或者 reject

        输入   click.feedback=SKIP，或者 REJECT 带上她填的理由
        库里   只多一行 feedback，status 一个字不动
        输出   无
        后果   skip 是「这次先不」，以后这家再出现照样推；
               reject 是「不该联系」，最值钱的一种反馈 —— 它不改 status，
               让这家沉下去靠的是 cooldown 的退避和以后的标注

    三、这条 lead 库里根本没有

        输入   lead_id=999999999（卡片太旧，那一行已经被删了）
        输出   不返回。record_feedback 那一句就撞上外键，
               psycopg 抛 ForeignKeyViolation，事务回滚
        后果   连那一行 feedback 也不会留下。这是刻意的：留下一行
               「她 approve 了一个不存在的 lead」比什么都不留更难查。
               抛的不是 RuntimeError —— 挡住它的是库里那条外键，
               不是 advance_to_draft 里的检查（那一句根本轮不到）

    四、lead 在库里，但状态不允许推进（比如已经是 excluded）

        输入   click.feedback=APPROVE，而那一行是 excluded
        输出   不返回，advance_to_draft 抛 RuntimeError，事务回滚
        后果   同上，连 feedback 那一行也不留。终态不该被一次点击复活，
               而这种情况说明她看到的那张卡片是这条线索被判成终态之前
               推的 —— 卡片和库不是同一套数据

    一次点击一个事务，不复用长连接那边的连接：两下点击之间可能隔三天，
    而一条 psycopg 连接挂那么久必然已经断了（Neon 免费层闲置五分钟就
    挂起）。每次现开一条，代价是每次多一次冷启动（约一秒），
    而她点按钮本来就在等一个回执。
    """
    with db.connect() as conn, conn.cursor() as cur:
        # 先记账。这一行是账本，写进去就不该因为后面某一步失败而回退 ——
        # 但它跟状态推进必须在同一个事务里，理由见情况三。
        feedback_db.record_feedback(
            cur,
            click.lead_id,
            feedback_type=click.feedback,
            author=click.user_id,
            run_id=click.run_id,
            note=note or None,
        )

        # 只有 approve 动状态。skip 和 reject 都只是记一行 ——
        # 「这次先不」和「不该联系」都不该让 discovery 停止关注这家公司，
        # 让它沉下去是 cooldown 退避和标注的事。
        if click.feedback is FeedbackType.APPROVE:
            feedback_db.advance_to_draft(cur, click.lead_id)

        conn.commit()


def update_card(client: Any, click: buttons.ButtonClick, message: dict, *, note: str = "") -> None:
    """把那张卡片改成「已收到」：去掉按钮，加一行回执。

    输入：Slack 客户端；这一下点击；那条消息原来的样子（回调载荷里的
          message 那一段）；reject 时她填的理由
    输出：无

    四种情况：

    一、卡片是带色条发的（正常情况）

        输入   message={"attachments": [{"color": "#2eb886",
                                         "blocks": [十几块]}]}
               取 attachments[0].blocks，换掉 actions 那一块
        输出   无。chat.update 把那条消息改掉，色条保留
        后果   必须原样把 attachments 传回去：只传 blocks 的话色条会
               消失，那张卡片在频道里会突然变成没有左边那条线的样子

    二、卡片是顶层 blocks 发的（早期版本，频道里可能还挂着）

        输入   message={"blocks": [十几块]}
        输出   无。按顶层 blocks 更新
        后果   两种都认，因为频道里的老卡片不会自己消失

    三、那条消息里一块 actions 都没有（重复点击）

        输入   message 的 blocks 里已经是回执那一行了
               acknowledged_blocks 原样返回
        输出   无。照样调一次 chat.update，内容跟原来一样
        后果   幂等，看不出区别。不在这里提前 return：判断「改了没有」
               要比两份块列表，而那件事 acknowledged_blocks 已经做了，
               在这里再判一次就是两处各写一份同样的逻辑

    四、那条消息整个不在了（她点的是一张停在客户端里的旧卡片）

        输入   message={}（modal 提交那一支现查原消息，查不到）
        输出   无。印一行就返回，不调 chat.update
        后果   不这么挡的话 chat.update 会因为「没有内容」直接报错，
               而库里那一行已经写好了 —— 报错帮不上任何人。
               结果是她看不到回执，但反馈是记上了。这属于「旧卡片」
               这一类问题，真正的修法是幂等那一块（Alex 说后续做）

    改卡片这一步放在写库之后：卡片改了收不回来，而库那边还能回滚。
    反过来的话，库写失败但卡片已经变成「已记下」，她会以为记上了。
    """
    attachments = message.get("attachments") or []
    if not attachments and not message.get("blocks"):
        # 一块都没有，说明那条消息已经不在了（她点的是一张停留在客户端里
        # 的旧卡片，而那条消息被删了）。这时候 chat.update 会因为没有内容
        # 直接报错，而库里那一行已经写好了 —— 报错帮不上任何人，
        # 印一行让 journal 里留下痕迹就够了。
        #
        # 这条路真的会走到：modal 提交那一支要现查一遍原消息，
        # 查不到的时候 message 就是空字典。
        print(f"[buttons] lead={click.lead_id} 那条消息已经不在了，卡片没法改")
        return

    if attachments:
        original = attachments[0].get("blocks") or []
        updated = buttons.acknowledged_blocks(original, click, note=note)
        # 原样带上色条和降级文本，只换 blocks。
        new_attachment = {**attachments[0], "blocks": updated}
        client.chat_update(
            channel=click.channel,
            ts=click.message_ts,
            attachments=[new_attachment, *attachments[1:]],
        )
        return

    updated = buttons.acknowledged_blocks(message.get("blocks") or [], click, note=note)
    client.chat_update(channel=click.channel, ts=click.message_ts, blocks=updated)


def _parsed_or_told(body: dict, client: Any, logger: Any) -> buttons.ButtonClick | None:
    """读懂这一下点击；读不懂就在频道里只给她一个人回一句，然后返回 None。

    输入：Slack 推过来的载荷；一个带好 token 的客户端；Bolt 的 logger
    输出：读懂了的 ButtonClick，或者 None 表示这一下放弃了

    两种情况：

    一、读得懂

        输入   正常的 block_actions 载荷
        输出   ButtonClick(lead_id=1281, run_id=27, ...)

    二、读不懂（value 不是我们塞的那个 JSON、action_id 不认识、
        缺 channel）

        输入   一张老版本的卡片点出来的载荷
               往频道里发一条只有她能看见的消息（ephemeral），
               再往日志里记一行
        输出   None
        后果   必须告诉她。不告诉的话她点下去毫无反应 —— 而「没反应」
               跟「系统挂了」在她那边长得一模一样，她只会再点几次。
               这一条以前只写在注释里、代码没做，是个真窟窿

    不抛异常：抛出去 Bolt 会认为这次处理失败，Slack 那边会重投同一个
    载荷，而同一个坏载荷再来一次还是坏的 —— 只会把同一条日志刷几遍。

    回执发不出去的时候（比如连 channel 都没解析出来）不再抛第二次：
    那时候已经没有任何办法通知她了，日志是唯一的落点。
    """
    try:
        return buttons.parse_click(body)
    except buttons.MalformedClick as err:
        logger.error(f"读不懂这一下点击：{err}")

        channel = (body.get("container") or {}).get("channel_id") or (
            body.get("channel") or {}
        ).get("id")
        user = (body.get("user") or {}).get("id")
        if not channel or not user:
            return None

        try:
            client.chat_postEphemeral(
                channel=channel,
                user=user,
                text=(
                    "这张卡片认不出来了，大概是旧版本发的。"
                    "反馈没记上 —— 等下一份早报再点，或者告诉我们这条是哪家。"
                ),
            )
        except Exception as reply_err:  # noqa: BLE001 - 通知失败不该盖住原因
            logger.error(f"连回执都发不出去：{reply_err}")
        return None


def build_app() -> Any:
    """造 Bolt 应用并挂上三个按钮、一个 modal、以及频道消息的处理。

    输入：无（两个 token 从环境变量或 .env 读）
    输出：一个 slack_bolt.App

    在函数里 import slack_bolt：这个文件是常驻进程的入口，而
    scheduled_discovery 那条路不需要 Bolt。跟 build_llm_client 同一条口径。

    「挂 handler」到底是什么意思：app.action("lead_approve")(函数) 这一句
    不调用那个函数，它只是往 Bolt 内部一张表里记一行 ——

        action_id            谁来处理
        ------------------   ---------------------
        lead_approve         _immediate
        lead_skip            _immediate
        lead_reject          _open_reject_modal
        （view callback_id）  谁来处理
        lead_reject_reason   _submit_reject
        （event type）        谁来处理
        message              _on_message（对话那条路，见本函数最后一段）

    以后 Slack 每推过来一个载荷，Bolt 看载荷里的 action_id（modal 提交
    看的是 view.callback_id），在这张表里查一行，调那个函数。整个「实时」
    就是这么来的：没有循环去问，是长连接上有人推过来、然后查表分发。

    那几个参数名是 Bolt 的约定，按名字注入不是按位置：

        ack     一个函数，调它就等于告诉 Slack「收到了」。三秒内必须调
        body    整个载荷（就是 parse_click 要读的那个字典）
        client  一个已经带好 bot token 的 WebClient，用来改卡片、开弹窗
        logger  Bolt 的 logger，打出来的东西 systemd 会收进 journal

    所以 handler 的参数名一个字都不能改 —— 改了 Bolt 注入不进来，
    而报错是「缺少必需参数」，看着像我们自己写错了函数签名。

    四个 handler 各自的分工：

        lead_approve / lead_skip   记账 + 改卡片，不弹窗；lead_approve 再在卡片 thread 里开起草对话
        lead_reject                只开 modal，不记账 —— 记账等她提交
        view_submission            modal 提交回来，这时候才记账 + 改卡片

    reject 那一支必须先 ack 再开 modal，而且 ack 要在三秒内：
    Slack 三秒收不到回应就当我们挂了，会把这次点击重投一遍。Bolt 的
    ack() 就是干这个的，所以每个 handler 第一句都是它。

    而 lead_reject 那一支刻意什么都不写库：她可能在 modal 上点「算了」。
    先记一行 reject 再等她填理由的话，点「算了」就留下一行她没打算留的
    reject —— 而那是 append-only 的账本，撤不回来。
    """
    from slack_bolt import App

    from delivery.send import build_client  # 复用同一套 token 读法

    app = App(token=build_client().token)

    # 对话那条路要的三样，按钮那一支（点「要联系」开起草对话）也要用，所以放在最前面造。
    #
    # 输入   bot_user_id，例 "U0000000002"。auth.test 用 bot token 问 Slack「我是谁」，
    #        进程起来时问一次。认 @bot 要用它：正文里的 @ 写成 <@U0000000002> 这个形状；
    #        认线索卡也要用它：只认 bot 自己发的卡片
    bot_user_id = app.client.auth_test()["user_id"]

    def root_card(channel: str, root_ts: str) -> int | None:
        """这个 thread 的开头那条是不是 bot 发的线索卡；是的话，是哪一家。

        输入：频道 id 和开头那条的 ts，例 ("C0000000001", "1788403874.643329")
        输出：lead_id，例 1281；不是线索卡是 None

        conversations.replies 按 thread 开头的 ts 取，第一条就是开头那条本身，
        limit=1 只要它。用的是 groups:history 那个 scope，清单里本来就有。
        """
        replies = app.client.conversations_replies(channel=channel, ts=root_ts, limit=1)
        root = (replies.get("messages") or [{}])[0]
        return conversation.lead_card_id(root, bot_user_id)

    # 输出   runner，整个进程一个。两张图的顺序就是它查存档的顺序：找公司的对话多，先查
    runner = ConversationRunner([query_graph(), drafting_graph()], root_card=root_card)

    def thread_sayer(client: Any, channel: str, root_ts: str) -> Callable[[dict], None]:
        """造一个「往这个 thread 里发一条消息」的函数，交给 runner 回话用。

        输入：带好 token 的客户端；频道；thread 开头那条的 ts，例 ("C0000000001", "1790199000.000100")
        输出：say。say({"text": "请问你想给 North Field Technologies 写哪一种 outreach 信息？…"})
              就发进那个 thread

        thread_ts 传的是开头那条的 ts：频道里直接 @bot 那一句，开头就是它自己，bot 的回复会在它
        下面开出一个 thread；thread 里的回话、点按钮开的起草对话，开头是那张卡片或者第一句，
        回复进同一个 thread。
        message 是 {"text": ...} 或者方案卡片 {"attachments": [...]}，原样展开
        """

        def say(message: dict) -> None:
            # unfurl_links / unfurl_media 关掉：推完卡片回的那句话里带着频道汇总的链接，
            # 不关的话 Slack 会在 thread 里把那条汇总展开成一个预览框，而汇总是 attachment，
            # 预览只显示「Part of this message can't be shown here」，看着像出错了
            client.chat_postMessage(
                channel=channel,
                thread_ts=root_ts,
                unfurl_links=False,
                unfurl_media=False,
                **message,
            )

        return say

    def _immediate(ack, body, client, logger):
        """approve 和 skip 走这一支：记账、改卡片、一次点击一个事务。点的是「要联系」的话，再开起草对话。"""
        ack()
        click = _parsed_or_told(body, client, logger)
        if click is None:
            return

        handle_click(click)
        update_card(client, click, body.get("message") or {})
        logger.info(f"lead={click.lead_id} {click.feedback} by {click.user_id}")

        # 「要联系」：在这张卡片的 thread 里开起草对话，bot 先问写什么。放在记账、改卡片之后：
        # handle_click 抛异常（这家已经是终态之类）的话走不到这里，不会给一家不该联系的公司开对话。
        # 交给 runner 的线程池，不在这里跑图（要连库，Bolt 的线程要马上还回去）
        # 输入   click.channel="C0000000001"，click.message_ts=那张卡片的 ts，click.lead_id=1289
        if click.feedback is FeedbackType.APPROVE:
            runner.start_from_card(
                click.channel,
                click.message_ts,
                click.lead_id,
                thread_sayer(client, click.channel, click.message_ts),
            )

    app.action(buttons.ACTION_APPROVE)(_immediate)
    app.action(buttons.ACTION_SKIP)(_immediate)

    @app.action(buttons.ACTION_REJECT)
    def _open_reject_modal(ack, body, client, logger):
        """reject 走这一支：只开 modal，一个字都不写库。

        她可能在 modal 上点「算了」—— 先记一行 reject 的话，那一行撤不
        回来（append-only 的账本）。
        """
        ack()
        click = _parsed_or_told(body, client, logger)
        if click is None:
            return

        client.views_open(trigger_id=click.trigger_id, view=buttons.reject_modal(click))

    @app.view(buttons.REJECT_MODAL_CALLBACK)
    def _submit_reject(ack, body, client, logger):
        """modal 提交回来：这时候才记账、改卡片。"""
        ack()
        try:
            click, note = buttons.parse_reject_submission(body)
        except buttons.MalformedClick as err:
            logger.error(f"读不懂这个 modal 提交：{err}")
            return

        handle_click(click, note=note)
        # modal 的载荷里没有原来那条消息，所以要现查一遍才能改它。
        # conversations_history 拿单条消息用 latest 加 inclusive 加 limit=1，
        # 这是 Slack 给的唯一办法（没有「按 ts 取一条」的接口）。
        history = client.conversations_history(
            channel=click.channel, latest=click.message_ts, inclusive=True, limit=1
        )
        found = (history.get("messages") or [{}])[0]
        update_card(client, click, found, note=note)
        logger.info(f"lead={click.lead_id} reject by {click.user_id} note={note!r}")

    # 下面是对话那条路。频道里的每一条消息都会推到这里（清单里订的是 message.groups），
    # 挑出跟 bot 说的话、交给图，是 delivery/conversation.py 和 interactive_discovery/runner.py 的事。
    @app.event("message")
    def _on_message(event, client, logger):
        """频道里来了一条消息：读懂它，是给 bot 的就交给线程池，马上返回。

        没有 ack 这个参数：事件类的请求 Bolt 自己替我们 ack，按钮那几个才要手动调。
        """
        inbound = conversation.parse_message(event, bot_user_id)
        if inbound is None:
            return

        # 输出   say 往这条消息所在的 thread 里发（root_ts 是那个 thread 开头那条的 ts）
        runner.submit(inbound, thread_sayer(client, inbound.channel, inbound.root_ts))

    return app


def main() -> int:
    """起长连接，一直挂着。

    输入：无
    输出：进程退出码。正常情况下它永远不返回 —— Socket Mode 的
          start() 会一直阻塞

    两种情况：

    一、正常起来

               印一行「正在等按钮」，然后阻塞在长连接上
        输出   不返回。Ctrl+C 或者 systemd 停它的时候才结束
        后果   Riley 每点一下按钮，上面那几个 handler 各自跑一次

    二、缺 SLACK_APP_TOKEN

        输出   抛 RuntimeError，退出码 1
        后果   缺的是 app token（xapp- 开头）不是 bot token：
               Socket Mode 那条长连接用的是前者，而发消息用后者。
               两个都要，最容易搞混的就是这一对

    这个进程一挂，按钮就全失灵，而 Slack 那边不会报错 —— 她点下去
    没反应，只会以为系统坏了。所以上 EC2 的时候它必须是一个
    Restart=always 的 systemd unit，而不是手动 nohup 起来的东西。
    """
    import logging
    import os

    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from persistence.db import _load_env_file

    # 把日志级别放到 INFO，不然 handler 里那几句 logger.info 一个字都不会
    # 出现。实测过：点击成功了、库也写了、卡片也改了，而日志里只有启动那
    # 两行 —— 上了 EC2 之后「按钮到底有没有在工作」就查不出来，
    # 而这个进程恰恰是「挂了也不报错」的那一类（她点下去没反应，
    # 只会以为系统坏了）。
    #
    # 用 basicConfig 不用给某个 logger 单独设级别：Bolt 内部好几个 logger
    # （socket mode 客户端、listener 运行器各一个），一个个设会漏。
    # 格式带上时间和 logger 名，因为 journal 里要分得出「这一行是我们打的
    # 还是 Bolt 打的」。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # 发 HTTP 请求的那几个库降到 WARNING。它们在 INFO 级别每个请求打一行完整 URL，
    # 对排查没用（我们自己的 print 已经说了在调谁），却是最容易把 URL 里的密钥漏进 journal 的地方。
    # httpx2 是 openai 那个包里的那一份 httpx，名字不一样，要单独降
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app_token = os.environ.get("SLACK_APP_TOKEN") or _load_env_file().get("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError(
            "找不到 SLACK_APP_TOKEN（xapp- 开头，Socket Mode 用的那个）："
            "设环境变量，或写进仓库根的 .env"
        )

    # 对话的存档在 checkpointer 那几张表里。在连 Slack 之前确认表在：表不在的话
    # 第一句话进来才会炸，而那时用户已经在等回复了
    setup_checkpointer()

    print("[buttons] Socket Mode 连上了，正在等按钮和消息。Ctrl+C 退出")
    SocketModeHandler(build_app(), app_token).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
