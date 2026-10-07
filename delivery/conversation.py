"""Slack 对话那条路上几张图共用的纯函数：读懂一条消息、定下怎么处理、那几句跟是哪张图无关的回复。

跟 delivery/buttons.py 同一个分工：这里只读懂载荷、只拼消息，不连 Slack 不连库，
脱离 token 就能测。串起来、真去跑图、真去发消息的是
triggers/interactive_discovery/runner.py（每张图自己的那份在 <图>/slack_conversation.py）。

一条消息从进来到被处理，经过这个文件里的两步：

    parse_message   Slack 推过来的 event 字典 -> Inbound（或者 None，表示不是给我们的）
    decide          Inbound + 这个 thread 在 checkpointer 里的状态 -> 七种处理之一

图跑完之后发回 thread 的那条消息（方案卡片、一句提问、结束语）每张图各不一样，不在这里：
query 的在 triggers/interactive_discovery/query/messages.py。
这里只留跟是哪张图无关的几句：EDITED_TEXT（编辑不算数）、BUSY_TEXT（上一句还在处理）、
OTHER_THREAD_TEXT（别人的 thread），和把一句话包成参数字典的 text_reply。

频道里的每一条消息都会推过来（清单里订的是 message.groups，理由写在
delivery/slack_app_manifest.yaml 里 groups:history 那一行）。所以第一步的主要
工作是把不相干的丢掉，第二步才轮到分辨「开新对话」还是「接着聊」。

发回去的每一条都是 chat.postMessage 的参数字典，不是一个字符串：
    一句话       {"text": "你想找做哪一类设备的公司？"}
    方案卡片     {"attachments": [{"color": ..., "fallback": ..., "blocks": [...]}]}
两种长得不一样，统一成字典，发送那一头就只有一种写法：chat_postMessage(**message)。
"""

from __future__ import annotations

from dataclasses import dataclass

from delivery.cards import LEAD_BLOCK_PREFIX

# decide 给出的七种处理。取值是 ASCII：runner 要拿它做 if，日志里也要印它，
# 而这条路上有 GBK 的终端。
IGNORE = "ignore"  # 不是给 bot 的，什么都不做
START = "start"  # 开一个新对话：频道里 @bot 开找公司的，线索卡下面 @bot 开起草的
RESUME = "resume"  # 图停在 interrupt 上等回话：把这句话投进去
RETRY = "retry"  # 上一轮半路出错，图停在出错那个节点前：从那一步重跑
FINISHED = "finished"  # 这个对话已经走到 END 了
OTHER_THREAD = "other_thread"  # 在一个既不是 bot 的对话、也不是线索卡的 thread 里 @ 了 bot
EDITED = "edited"  # 有人编辑了一条说给 bot 的旧消息：告诉他改动不算数

# checkpointer 里一个 thread 的四种状态，由 runner 看 get_state 的结果算出来
# （triggers/interactive_discovery/runner.conversation_status）。
NO_CONVERSATION = "none"  # 库里没有这个 thread_id 的存档
WAITING = "waiting"  # 停在 interrupt 上
FAILED = "failed"  # 没停在 interrupt 上，但还有节点没跑（上一轮半路抛了异常）
ENDED = "ended"  # 走到 END 了，没有下一个节点

# 这几种 subtype 仍然是「一个人说了一句话」，照常处理：
#     thread_broadcast   在 thread 里回话时勾了「同时发到频道」，它仍然带 thread_ts
#     file_share         带了附件的一句话，text 里照样有字
# message_changed（有人编辑了旧消息）单独读，见 _parse_edit。
# 其余的一律丢掉。最常见的几种：message_deleted、channel_join（有人进频道）、
# bot_message（别的 bot 发的）。
_HUMAN_SUBTYPES = frozenset({"thread_broadcast", "file_share"})

OTHER_THREAD_TEXT = "这个 thread 不是我的对话。要找公司的话，请在频道里直接 @ 我，会开一个新的对话。"
EDITED_TEXT = (
    "编辑过的消息我不会重新读，改动不算数，我还是按你原来那句往下走。"
    "要补充或者更正，请在 thread 里另发一句；要开新对话，请在频道里重新 @ 我。"
)
BUSY_TEXT = "上一句还在处理，等我回完这一句再说。"
# 点「要联系」时这张卡片下面已经有对话了（先 @ 过 bot）：不重开，接着那个对话说
ALREADY_STARTED_TEXT = "这张卡片下面已经有一个对话了，接着在这个 thread 里说就行。"
# 对话走到 END 之后再说话回的那句，每张图各给一句（ConversationGraph.finished_text），不在这里


def text_reply(text: str) -> dict:
    """一句话写成 chat.postMessage 的参数。例："好" -> {"text": "好"}"""
    return {"text": text}


def thread_id_of(channel: str, root_ts: str, *, prefix: str) -> str:
    """一个 Slack thread 在某一张图里的那条对话的 id，也就是 checkpointer 里存档的钥匙。

    输入：
        channel   频道 id，如 "C0000000001"
        root_ts   这个 thread 开头那条消息的 ts，如 "1790126036.992409"
        prefix    哪张图（ConversationGraph.thread_prefix）：query 是 "slack"，写稿是 "draft"
    输出：
        找公司的对话       "slack:C0000000001:1790126036.992409"
        线索卡下面的起草   "draft:C0000000001:1790199000.000100"   root_ts 是那张卡片的 ts

    ts 是那条消息的发送时间（小数点前是 Unix 秒数，后面是微秒），只在一个频道里唯一，
    两个频道可能各有一条同一微秒发出的消息，所以频道 id 必须拼进去。

    前缀把两张图的存档分开：两张图的存档在同一套 checkpoint 表里，钥匙只要不同就互相看不见。
    不带前缀的话，runner 拿写稿那张图去读一份找公司留下的存档，节点名对不上，读出来的 next 是错的。
    query 的前缀叫 slack 是历史原因：它是第一张图，库里已有的存档都是这个前缀，改了就找不回来。

    这个 id 是算出来的，不存映射表：同一个 thread 里任何一条消息都能重新算出同一个
    id，存一张表只会多一处会漂的真相，每次还要多查一趟 Neon。
    """
    return f"{prefix}:{channel}:{root_ts}"


@dataclass(frozen=True)
class Inbound:
    """一条读懂了的、人发的消息。

    在频道里直接 @ bot 的起始句，是一组对话最开始的那条消息（它自己就是 thread 的开头），
    没有 thread_ts：

        Inbound(channel="C0000000001", ts="1790126036.992409", thread_ts="",
                user="U0000000001", text="帮我找找骨传导耳机的初创", mentioned=True,
                edited_ts="")

    在一个已经存在的 thread 下回复（不带 @），它的 thread_ts 是起始句的 ts：

        Inbound(channel="C0000000001", ts="1790126062.118200",
                thread_ts="1790126036.992409",
                user="U0000000001", text="别带 Kickstarter", mentioned=False,
                edited_ts="")

    有人把上面那句改成了「别带 Indiegogo」：ts 还是原来那句的，多了一个编辑时间：

        Inbound(channel="C0000000001", ts="1790126062.118200",
                thread_ts="1790126036.992409",
                user="U0000000001", text="别带 Indiegogo", mentioned=False,
                edited_ts="1790126090.000000")
    """

    channel: str
    ts: str  # 这条消息自己的发送时间；编辑过的消息也是原来那句的发送时间
    thread_ts: str  # 所在 thread 开头那条的 ts；不在 thread 里是空串
    user: str  # 说话人的 Slack user id，如 "U0000000001"
    text: str  # 去掉了 @bot 那一串的正文；编辑过的消息是改完之后的正文
    mentioned: bool  # 正文里有没有 @bot
    edited_ts: str  # 编辑的时间；这是刚说的一句新话就是空串

    @property
    def root_ts(self) -> str:
        """这条消息所在 thread 的开头那条的 ts。

        thread_ts 有就用 thread_ts，没有就用这条消息自己的 ts：

            频道里 @bot 的起始句     ts="1790126036.992409"  thread_ts=""
                                     -> "1790126036.992409"   它自己就是开头
            它下面的回复             ts="1790126062.118200"  thread_ts="1790126036.992409"
                                     -> "1790126036.992409"   跟开头那条一样
            线索卡下面讨论 draft 的回复
                                     ts="1790200000.000300"  thread_ts="1790199000.000100"
                                     -> "1790199000.000100"   卡片那条的 ts

        开头那条被发出来的时候还没有任何回复，Slack 不会给它 thread_ts；等 thread 里
        有了回复，每条回复都带着 thread_ts = 开头那条的 ts。所以一个 thread 里所有
        消息算出来的 root_ts 是同一个，对话就是靠这个认出来的。
        bot 回话的时候也把它当 thread_ts 传给 chat.postMessage，回复才会进这个
        thread 而不是发到频道里。
        """
        return self.thread_ts or self.ts

    @property
    def thread_id(self) -> str:
        """这条消息所在的 Slack thread 的钥匙。例：slack:C0000000001:1790126036.992409

        runner 拿它当占线锁（_busy）的钥匙、写日志：同一个 Slack thread 同一时刻只跑一轮，
        不管这一轮是哪张图的。它恰好也是 query 那张图的存档钥匙（前缀同为 slack）；
        写稿那张图的存档钥匙另算（thread_id_of(..., prefix="draft")）
        """
        return thread_id_of(self.channel, self.root_ts, prefix="slack")

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        """认 Slack 重投用的钥匙。例：("C0000000001", "1790126062.118200", "")

        光用 channel 和 ts 不够：编辑过的消息 ts 还是原来那句的，只看这两样的话，
        每一次编辑都会被当成原来那句的重投丢掉。带上 edited_ts，同一句改两次是
        两条不同的事件，而 Slack 重投的那份三样都一模一样。
        """
        return (self.channel, self.ts, self.edited_ts)


def parse_message(event: dict, bot_user_id: str) -> Inbound | None:
    """读懂 Slack 推过来的一个 message 事件；不是人发给我们的就返回 None。

    输入：
        event         Slack 推过来的 event 字典，例：
                          {"type": "message", "channel": "C0000000001",
                           "user": "U0000000001",
                           "text": "<@U0000000002> 帮我找找骨传导耳机的初创",
                           "ts": "1790126036.992409"}
        bot_user_id   bot 自己的 user id，进程起来时用 auth.test 查一次，
                      例 "U0000000002"
    输出：
        Inbound(..., text="帮我找找骨传导耳机的初创", mentioned=True)，或者 None

    返回 None 的五种：
        subtype 是 message_deleted 之类（不是一个人说了一句话）
        message_changed 但不是人改了字（见 _parse_edit）
        带 bot_id，或者 user 就是 bot 自己（bot 说的话）
        没有 user 或者没有 channel（系统消息）
        去掉 @ 之后一个字都不剩（只 @ 了一下）
    """
    # 输入   subtype，例 None / "message_changed" / "thread_broadcast"
    subtype = event.get("subtype")
    if subtype == "message_changed":
        return _parse_edit(event, bot_user_id)
    if subtype is not None and subtype not in _HUMAN_SUBTYPES:
        return None
    return _read(event, event.get("channel") or "", bot_user_id, edited_ts="")


def _parse_edit(event: dict, bot_user_id: str) -> Inbound | None:
    """读懂一个 message_changed 事件：是人改了一句话的字，才交出一个 Inbound。

    输入：event，形状跟普通消息不一样，改完的那句装在 message 里，改之前的在
          previous_message 里，channel 在外面一层：

              {"type": "message", "subtype": "message_changed",
               "channel": "C0000000001",
               "message": {"user": "U0000000001", "text": "别带 Indiegogo",
                           "ts": "1790126062.118200",
                           "thread_ts": "1790126036.992409",
                           "edited": {"user": "U0000000001", "ts": "1790126090.000000"}},
               "previous_message": {"text": "别带 Kickstarter", ...}}

    输出：Inbound(..., text="别带 Indiegogo", edited_ts="1790126090.000000")，或者 None

    编辑过的消息最后只会换来一句 EDITED_TEXT，不会进图：那句话可能是三天前说的，
    图早就往下走了，把改过的内容塞进去会被当成对当前这一步的回答。
    要告诉他一声，是因为他改完之后 bot 一声不吭的话，他会以为 bot 读到了。

    message_changed 不只是人改字才有，下面两种也会推过来，都得丢，不然 bot 会对着
    没人改过的消息说「改动不算数」：
        没有 edited 那一项     Slack 自己动了这条消息，比如给里面的链接加了预览，
                               或者有人回复之后更新了开头那条的回复数
        edited 有，字没变      只改了附件之类
    bot 自己的卡片被 chat.update 改掉（点完按钮换成回执）也是 message_changed，
    它会在 _read 里因为带 bot_id 被丢掉。
    """
    message = event.get("message") or {}
    edited = message.get("edited") or {}
    previous = event.get("previous_message") or {}
    if not edited.get("ts") or message.get("text") == previous.get("text"):
        return None
    return _read(message, event.get("channel") or "", bot_user_id, edited_ts=edited["ts"])


def _read(message: dict, channel: str, bot_user_id: str, *, edited_ts: str) -> Inbound | None:
    """从一条消息的字典里读出 Inbound。普通消息和编辑后的消息共用这一段。

    输入：
        message     一条消息的字典（普通消息就是 event 本身，编辑过的是 event["message"]）
        channel     频道 id
        bot_user_id bot 自己的 user id
        edited_ts   编辑的时间；普通消息是空串
    输出：Inbound，或者 None（bot 说的、系统消息、只 @ 了一下）
    """
    # 输入   bot_id 只有 bot 发的消息才有。Bolt 默认已经滤掉了这个 app 自己发的，
    #        这里再挡一次，连别的 bot 一起挡：两个 bot 在同一个 thread 里互相回话会
    #        没完没了
    user = message.get("user") or ""
    if message.get("bot_id") or not user or not channel or user == bot_user_id:
        return None

    # 输入   raw，例 "<@U0000000002> 帮我找找骨传导耳机的初创"
    #        Slack 把 @ 写进正文，写成 <@user id> 这个形状；客户端显示成「@b2b-outreach」
    #        只是渲染出来的，推过来的原文里就是这一串
    raw = message.get("text") or ""
    mention = f"<@{bot_user_id}>"
    mentioned = mention in raw

    # 输出   text，例 "帮我找找骨传导耳机的初创"
    #        @bot 那一串去掉再交给图：模型读到 <@U0000000002> 没有任何用处，
    #        还会被原样抄进 messages
    text = raw.replace(mention, "").strip()
    if not text:
        return None

    return Inbound(
        channel=channel,
        ts=message.get("ts") or "",
        thread_ts=message.get("thread_ts") or "",
        user=user,
        text=text,
        mentioned=mentioned,
        edited_ts=edited_ts,
    )


def needs_lookup(inbound: Inbound) -> bool:
    """要不要去 checkpointer 里查这个 thread 的状态。

    输入：一条读懂了的消息
    输出：True / False

        不在 thread 里   False   该 message 自己就是开头，ts 刚刚才有，库里不可能有这个
                                 thread_id 的存档
        在 thread 里     True    该 message 是某个已存在的 thread 下面的回复。这个 thread
                                 可能是 bot 的对话，也可能是同事自己开的讨论串、或者一张
                                 线索卡，光看消息分不出来，要查库看这个 thread_id 有没有存档、
                                 存档现在是什么状态

    查一次要开一条到 Neon 的连接（闲置挂起之后冷启动约一秒），所以频道里那些
    没 @ 也不在 thread 里的消息，一次都不查就丢掉。
    """
    return bool(inbound.thread_ts)


def needs_card_check(inbound: Inbound, status: str) -> bool:
    """要不要去 Slack 读这个 thread 的开头那条，看它是不是一张线索卡。

    输入：一条读懂了的消息；这个 thread 在 checkpointer 里的状态
    输出：True 只有一种情况 —— 在 thread 里、库里没有存档、@ 了 bot、不是编辑。
          也就是 decide 第四问里 @ 了的那两格，要靠「开头是不是线索卡」才分得开

    读一次是一趟 conversations.replies。只在这一种情况下读：频道里的闲聊、bot 自己
    对话里的每一句都用不着它，而这种情况本身很少见。
    """
    return (
        bool(inbound.thread_ts) and status == NO_CONVERSATION and inbound.mentioned and not inbound.edited_ts
    )


def lead_card_id(message: dict, bot_user_id: str) -> int | None:
    """一条消息是不是 bot 发的线索卡；是的话，是哪一家（lead_id）。

    输入：
        message       conversations.replies 读回来的那条，例（只画用到的几项）：
                          {"user": "U0000000002", "bot_id": "B0BU5ANPBKR",
                           "ts": "1788403874.643329",
                           "attachments": [{"color": "#e8912d",
                                            "blocks": [..., {"type": "actions",
                                                             "block_id": "lead:1281:run:27", ...}]}]}
        bot_user_id   bot 自己的 user id
    输出：
        上面那张      1281
        不是线索卡    None（别人发的、同事开的讨论串、早报的汇总卡）

    认的是按钮那一块的 block_id（"lead:" 开头，delivery/cards.lead_message 写的），
    点过按钮之后那一块换成了回执，block_id 跟着留在回执上（buttons.acknowledged_blocks）。
    两处都要看 user：别人贴一张长得一样的卡片进来，不能被当成我们的线索卡。

    lead_id 从 block_id 里读，不从按钮的 value 里读：点过按钮之后按钮没了，value 跟着没了，
    block_id 还在。
    "lead:" 开头但中间那一截不是数字（格式跟 cards.py 不是同一个版本）的，当成认不出，返回 None：
    猜一个 id 会给别的公司开起草对话。

    回执开始保留 block_id 之前就点过按钮的旧卡片，回执上的 block_id 是 Slack 随手编的，认不出来，
    它们下面 @bot 会收到 OTHER_THREAD_TEXT，不会开起草对话。
    """
    if message.get("user") != bot_user_id:
        return None
    blocks = list(message.get("blocks") or [])
    for attachment in message.get("attachments") or []:
        blocks.extend(attachment.get("blocks") or [])
    for block in blocks:
        block_id = str(block.get("block_id") or "")
        if not block_id.startswith(LEAD_BLOCK_PREFIX):
            continue
        # 输入   "lead:1281:run:27"
        # 输出   "1281" -> 1281
        middle = block_id[len(LEAD_BLOCK_PREFIX) :].split(":")[0]
        return int(middle) if middle.isdigit() else None
    return None


def decide(inbound: Inbound, status: str, *, root_is_card: bool) -> str:
    """拿到一条 inbound message，定下这条消息该怎么处理（七种处理之一）。

    输入：
        inbound        一条读懂了的消息
        status         这条消息所在的 thread 在数据库 checkpointer 里的状态，四种之一：
                       NO_CONVERSATION / WAITING / FAILED / ENDED。
                       不在 thread 里的消息不查库，调用方直接传 NO_CONVERSATION
        root_is_card   这个 thread 的开头是不是一张线索卡。只有 needs_card_check 为真时
                       调用方才去 Slack 读，其余情况传 False（用不到）
    输出：
        七种处理之一。其中 START / RESUME / RETRY 三种会去跑一轮图，
        另外四种不碰图，只回一句固定的话，或者什么都不做（IGNORE）。
        decide 不管是哪张图：START 交给哪张图、RESUME 投进哪一份存档，是 runner._route 的事。

    一条消息进来，按下面四问从上往下走，走到哪一格就是哪种处理。
    四问的顺序就是指标的重要性：前一问的答案，会让后面的问题不用再问。

    第一问  这是刚说的一句新话，还是编辑了一句旧话？（看 edited_ts）

        新话   edited_ts=""                     -> 往下走第二问
        编辑   edited_ts="1790126090.000000"    -> 到此为止，一律不进图
                   说给 bot 的     -> EDITED    库里有存档（是 bot 的对话），或者带着 @
                   别的            -> IGNORE    同事改自己的闲聊

        排在第一，是因为编辑过的消息落在哪里都不能进图：那句话可能是三天前说的，
        图早就往下走了，把改过的内容塞进去，会被当成对当前这一步的回答。

    第二问  它在不在一个 thread 里？（看 thread_ts）

        不在   在频道里直接发的，它自己就是一个 thread 的开头：
                   Inbound(ts="1790126036.992409", thread_ts="",
                           text="帮我找找骨传导耳机的初创", mentioned=True, ...)
               ts 刚刚才有，库里不可能有它的存档，所以不查库，只看 @：
                   @ 了 bot   -> START     开一个新对话，这句话就是第一句
                   没 @       -> IGNORE    频道里的闲聊，例「哈哈哈 一定要赶工完成呢」

        在     是某个 thread 下面的回复，thread_ts 是那个 thread 开头那条的 ts：
                   Inbound(ts="1790126062.118200", thread_ts="1790126036.992409",
                           text="别带 Kickstarter", mentioned=False, ...)
               这个 thread 可能是 bot 的对话，也可能是同事自己开的讨论串、或者一张线索卡。
               光看消息本身分不出来，要拿 thread_id 去库里查 -> 第三问

    第三问  这个 thread 在库里是什么状态？（看 status，由 conversation_status 算出来。
            runner 按两张图的钥匙各查一次，哪张图有存档就用哪张的状态；两张都没有是 NO_CONVERSATION）

        WAITING           -> RESUME
            库里：有存档，最新那份停在一个 interrupt 上（await_go 或 ask_clarification），
                  那个 interrupt 挂在 checkpoint_writes 里。bot 刚发完方案、或者刚问完
                  一句澄清，在等人回话。
            例：图停在 await_go，用户回「别带 Kickstarter」-> 投进图里当 resume 值
        FAILED            -> RETRY
            库里：有存档，上一轮 invoke 在某个节点里崩溃了，那个节点的写一笔都没落。
            例：understand_intent 调 OpenAI 超时，存档 next=('understand_intent',)，
                用户回任意一句 -> 从 understand_intent 重跑
            还有两种 FAILED 不重跑，runner 先把对话退回一个 interrupt、重新问一遍：
                run_harvest 崩溃                 退回 await_go，免得不经拍板又去采集
                await_go 拿到「跑」之后崩溃     退回 await_go，免得存下的「跑」被重放
                （ask_clarification 同理）
            这里照样给 RETRY，分辨它们是 runner 的事（interactive_discovery/runner 的
            _recover 和 _invoke），decide 只看 status
        ENDED             -> FINISHED
            库里：有存档，没挂 interrupt，next 也空了。图已经走到 END（说过「算了」，
                  或者采完了）。
            例：采完之后用户又回「再找一批」-> 告诉他去频道里重新 @
        NO_CONVERSATION   -> 第四问
            库里：这个 thread_id 一份存档都没有。这个 thread 不是 bot 开的对话。

        有存档的三种都不看 @：bot 的对话 thread 里只跟 bot 说话，不允许人跟人讨论
        （Alex 定的规矩），所以一旦库里有这个 thread 的存档，里面每一句都算说给 bot 的。
        在 bot 的对话 thread 里再 @bot 一次，也不会重新开一张图：<@bot> 那一串先被去掉，
        剩下的话按当前状态处理。比如停在 await_go 时说「<@bot> 换成找智能眼镜」，
        那是一条修改意见，classify_reply 把它送回 understand_intent 重拟，
        还是同一个 thread_id、同一份存档。

        「正常地在 thread 里跟 bot 说话」就是 WAITING -> RESUME 那一格，不是另外一种状态：
        这张图每次停下来等人说话，都是停在一个 interrupt 上（bot 问了一句澄清，停在
        ask_clarification；bot 发了方案，停在 await_go），所以 bot 说完一句、轮到人说的
        时候，状态一定是 WAITING。
        剩下那一种「有存档、正在跑」（图还在 invoke 里），存档长得跟 FAILED 一样，
        这里会判成 RETRY；但它走不到真去跑图那一步，runner 在 _occupy 那里就挡住了，
        回 BUSY_TEXT（理由写在 interactive_discovery/runner.conversation_status）。

    第四问  没有存档的 thread：他是想跟 bot 说话吗？这是谁的 thread？（先看 @，再看 root_is_card）

        没 @                          -> IGNORE         同事们在自己的 thread 里聊天
        @ 了，开头是 bot 发的线索卡   -> START          例：线索卡下面「<@bot> 帮我写一封开发信」
                                                        开的是起草对话，那句话就是这次要写什么
        @ 了，开头不是线索卡          -> OTHER_THREAD   例：同事的 thread 下面「<@bot> 帮我找耳机」

        线索卡下面的 START 跟频道里的 START 是同一个取值，开哪张图 runner 看在哪儿说的：
        频道里说的开找公司的（query），线索卡下面说的开起草的（drafting）。
        别人的 thread 是同事自己开的讨论串，不在这里开对话，告诉他去频道里 @。
        只有走到这一问、而且 @ 了的，才需要去 Slack 读 thread 开头那条（needs_card_check）：
        它是整张表里唯一要多打一趟 Slack 的格子，而这种情况本身很少见。
    """
    if inbound.edited_ts:
        # 编辑过的消息一律不进图。只在「这是说给 bot 的」时回一句：库里有存档说明
        # 是 bot 的对话，没存档但带着 @ 说明他想跟 bot 说话。别的都是同事改自己的闲聊
        if status != NO_CONVERSATION or inbound.mentioned:
            return EDITED
        return IGNORE

    if not inbound.thread_ts:
        return START if inbound.mentioned else IGNORE

    if status == WAITING:
        return RESUME
    if status == FAILED:
        return RETRY
    if status == ENDED:
        return FINISHED
    if status != NO_CONVERSATION:
        # 四种状态之外的取值说明 conversation_status 跟这张表不是同一个版本，
        # 猜一个处理的话，最可能猜成「当成别人的 thread 丢掉」，bot 从此不说话
        raise ValueError(f"不认识的对话状态：{status!r}")

    if not inbound.mentioned:
        return IGNORE
    return START if root_is_card else OTHER_THREAD
