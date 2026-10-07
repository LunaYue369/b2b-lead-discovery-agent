"""Slack 对话的调度员：一句话进来，交给图跑一轮，再把图的回答发回那个 thread。几张图共用这一个。

跟按钮住在同一个进程里（triggers/slack_buttons.build_app 在那里挂上 message 事件）。
不单开进程的理由：同一个 app token 开两条 Socket Mode 连接时，每个事件只投给其中
一条，而且同一个 thread 里前后两句可能被投到不同的连接上 —— 拆进程就会静默丢事件，
连「一条对话由一个进程接」都保证不了。

三处分工：

    delivery/conversation.py            纯函数：读懂消息、定下怎么处理（decide）、拼那几句固定的回复
    这个文件                            跟是哪张图无关的部分：并发、跑一轮图、崩溃之后怎么收拾
    <图的文件夹>/slack_conversation.py  每张图自己的部分，交成一个 ConversationGraph，
                                        例 query/slack_conversation.query_graph()

这里负责三件事：

    1 并发      Bolt 收到事件的线程马上还回去，真正的活交给自己的线程池；
                同一个 thread 同一时刻只处理一句，不同 thread 之间互不耽误
    2 跑图      每一轮现开一条到 Neon 的 checkpointer 连接，两张图都用它编；
                认出这句话归哪张图（_route），查状态、invoke、关掉
    3 出错      收到回复之后崩溃的 interrupt 节点，把对话退回那个 interrupt 重新问一遍；
                图自己说不能重跑的节点（query 的 run_harvest）按图自己的规矩退回；
                其余失败等下一句话重试。进程被杀留下的存档也一样处理

对话从哪几个地方开始：

    频道里 @bot                     「@b2b-outreach 帮我找骨传导耳机」    -> query（找公司）
    线索卡的 thread 里 @bot         「@b2b-outreach 帮我写封邮件给他们」  -> drafting（起草），
                                                                            那句话就是要写什么
    线索卡上点「要联系」按钮        （没有一句话）                         -> drafting，bot 先问要写什么
                                                                            （start_from_card）

一句话从进来到回复的全程（用户在频道里发「@b2b-outreach 帮我找骨传导耳机」）：

    Bolt 的线程      parse_message 读懂 -> submit 查重、交给线程池 -> 马上返回
    线程池的线程     open_saver() 开 checkpointer -> 两张图各编一张 -> _route 认出：频道里、@ 了
                     -> START、归 query -> 占住这个 thread -> invoke(graph.first_input(...))
                     -> 图停在 await_go -> 放开这个 thread -> graph.render 拼出方案卡片 -> 发进 thread

用词：Slack 的对话串叫 thread，线程池里干活的叫线程，两个不混用。

发回 thread 的每一条都是 chat.postMessage 的参数字典（{"text": ...} 或者带 attachments
的卡片），理由写在 delivery/conversation.py 开头。
"""

from __future__ import annotations

import collections
import contextlib
import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

from delivery import conversation
from delivery.conversation import Inbound

logger = logging.getLogger(__name__)

# 线程池开几条线程，也就是同一时刻最多几条对话在跑。
# 4 的理由：我们的活几乎全在等网络（OpenAI 几秒、Apify 几分钟、Neon 几十毫秒），
# 等的时候线程让出位置，所以一条线程一条对话就够；4 条已经多于我们实际会同时
# 说话的人数。真采集那一步本来就被 advisory lock 限成同一时刻一个，开多了也快不了。
# 几张图共用这 4 条：不管是找公司还是写稿，同一时刻说话的还是那几个人。
_MAX_WORKERS = 4

# 记住最近处理过的多少条消息，用来挡 Slack 的重投。
# Slack 在它认为我们没收到的时候会把同一个事件再推一遍，重投的那份 channel 和 ts
# 跟第一份一模一样。1000 条远多于几分钟内频道里会出现的消息数。
_SEEN_LIMIT = 1000


@dataclass(frozen=True)
class ConversationGraph:
    """一张能在 Slack thread 里对话的图，交给 runner 的十一样东西。

    runner 只认这十一样，不认识任何一个节点名：哪个节点花钱、interrupt 前面是谁、
    结果怎么拼成一条消息，都是图自己知道的事。拿 query 那张图（query/slack_conversation.query_graph）举例，
    写稿那张（drafting/slack_conversation.drafting_graph）不一样的几格写在括号里：

        name              "query"（"drafting"）
                          只进日志，例 "slack:C0000000001:1790126036.992409 query status=waiting ..."
        thread_prefix     "slack"（"draft"）
                          这张图的存档钥匙的前缀：thread_id_of(channel, root_ts, prefix=这个)
                          -> "slack:C0000000001:1790126036.992409"。两张图的存档在同一套表里，靠它分开
        starts_in         "channel"（"lead_card"）
                          这张图的对话在哪儿开：频道里 @bot 开 query，线索卡的 thread 里 @bot 或者点
                          「要联系」开 drafting。runner 判出 START 之后按它挑图，每个取值只许一张图
        build             query/graph.build_graph（drafting/graph.build_graph）
                          给一个存档器、编好这张图。runner 每一轮开一条连接，两张图都用它编
        first_input       ("帮我找骨传导耳机", None)
                              -> {"messages": ["用户: 帮我找骨传导耳机"], "intent_result": {}, ...}
                          （("帮我写封邮件给他们", 1289) -> {"messages": ["用户: 帮我写封邮件给他们"],
                             "lead_id": 1289, "lead": {}, "request": "帮我写封邮件给他们"}；
                            点按钮开的是 ("", 1289)）
                          START 时交给 invoke 的那一整份初始 state。第二个参数是这个 thread 开头那张线索卡
                          是哪一家，只有 lead_card 那张图用得上，query 收到的永远是 None
        before_interrupt  {"await_go": "plan_queries", "ask_clarification": "understand_intent"}
                          （{"ask_request": "retrieve_knowledge"}）
                          interrupt 节点 -> 它前面那个节点。_back_to 退回某个 interrupt 时，
                          update_state 的 as_node 填这个前面的节点（理由写在 _back_to）
        render            invoke 返回的整份 state -> 发回 thread 的那条消息
                              停在 await_go   -> {"attachments": [方案卡片]}
                              走到 END        -> {"text": "分析了 12 家，3 家过线，推了 3 张到频道：https://..."}
        finished_text     "这次对话已经结束了。要再找一批，请在频道里重新 @ 我，会开一个新的对话。"
                          对话走到 END 之后，同一个 thread 里再说话回的那一句（decide 判 FINISHED）。
                          每张图一句：「再找一批」只对找公司的对话说得通
        restart           None（drafting 是 lambda values: values["lead_id"] 那种函数）
                          对话走到 END 之后，同一个 thread 里又 @ 了 bot，要不要在同一份存档上从头再跑：
                              None      不重开，回 finished_text（query：一个 thread 一次采集，
                                        要再找一批去频道里重新 @）
                              一个函数  重开：这一句当 START，交给 first_input 的第二个参数由这个函数
                                        从旧存档的 values 里读出来（drafting 读出 1289）。旧存档不删，
                                        messages 这类 operator.add 的字段接着往后加，前几轮的东西都还在
                          只看 @：没 @ 的话照样回 finished_text，免得一句闲聊就把对话重开了
        on_crash / unfinished_retry
                          （drafting 两个都永远返回 None：它没有花钱的节点，崩了都能直接重跑）
        on_crash         一轮 invoke 刚崩溃时，先问图：这个崩溃你有自己的收拾法吗
                              run_harvest 崩溃，DiscoveryBusy
                                  -> ("await_go", "没跑成（DiscoveryBusy: …），一分钱没花。方案还在，…")
                              understand_intent 崩溃
                                  -> None，交给 _recover 里通用的两种
        unfinished_retry  下一句话判了 RETRY、准备 invoke(None) 重跑之前，先问图：
                          这份存档你允许直接重跑吗
                              next == ('run_harvest',)
                                  -> ("await_go", "上一次采集没跑完就断了（进程中断，…），…")
                              next == ('deliver',)
                                  -> None，可以直接重跑

    on_crash / unfinished_retry 返回的 (节点, 说明) 都交给 _back_to：退回那个节点的 interrupt，
    说明放在重发的那条消息最上面。
    """

    name: str
    thread_prefix: str
    starts_in: str
    build: Callable[[Any], Any]
    first_input: Callable[[str, int | None], dict]
    before_interrupt: Mapping[str, str]
    render: Callable[[dict], dict]
    finished_text: str
    restart: Callable[[dict], int | None] | None
    on_crash: Callable[[Any, Exception], tuple[str, str] | None]
    unfinished_retry: Callable[[Any], tuple[str, str] | None]


def conversation_status(snapshot: Any) -> str:
    """看 checkpointer 里一个 thread 的存档，说出它现在是四种状态里的哪一种。

    输入：snapshot，app.get_state(config) 的返回值（StateSnapshot），调用方在 handle 里
           拿 thread_id 现查的。

          拿 thread_id 查，查到的永远是这个 thread 最新的那一份存档。config 里只有
          thread_id、没有 checkpoint_id 时，PostgresSaver 就是按 thread_id 取 checkpoint_id
          最大的那一行（checkpoint_id 按时间递增，最大的就是最后落的）。
          例：_back_to 第 1 步 update_state 落了一份新存档 ...5fb42e，parent 指向旧的 ...a1025a。
          旧的那份还在库里，但之后再拿 thread_id 查，拿到的就是 ...5fb42e：
              config = {"configurable": {"thread_id": "slack:C0000000001:1790126036.992409"}}
              app.get_state(config)   -> ...5fb42e 那一份，next=('await_go',)
          parent 只是用来往回翻历史的（get_state_history），不影响查到哪一份。
          要查某一份旧的，得在 config 里另外带上它的 checkpoint_id，这个文件里没有这么用的。
          invoke 读存档也是同一个规矩：带着 thread_id 接着跑，就是从最新那份接着跑。

          用到 snapshot 的四样：
        values       这个 thread 最新的整份 state；从没存过是空字典 {}
        interrupts   现在正挂着的 interrupt，停在 await_go 或 ask_clarification 时是
                     一个元素的元组，别的时候是空元组 ()
        next         下一步要跑的节点名，例 ('await_go',)；走到 END 是空元组 ()
        tasks        最新那份存档上排着的任务，每个带 name / error / interrupts。
                     只用来认下面第二行（_node_crashed_after_reply）
    输出：delivery/conversation.py 里那四个取值之一，从上往下第一行对上的就是。
          只看存档的样子，不看节点名，所以哪张图都能用；下面的例子都是 query 那张图的：

        values={}                                          -> NO_CONVERSATION
            库里没有这个 thread_id 的存档，这个 thread 从没开过对话

        _node_crashed_after_reply 找到了节点               -> FAILED
            例：用户回「跑」，await_go 拿到「跑」之后、return 之前崩溃。
            这时 interrupts 还挂着，next=()，存档样子见 _node_crashed_after_reply。
            这一行必须排在 WAITING 前面：只看 interrupts 会判成 WAITING，下一句「算了」
            就变成 Command(resume="算了")，可 await_go 拿到的还是存下的「跑」，
            「算了」被丢掉、照样去采集。判 FAILED 才会走到退回（_recover 情况二）

        interrupts=(Interrupt(...),)  next=('await_go',)   -> WAITING
                                  或  next=('ask_clarification',)
            图停在我们自己放的两个 interrupt 之一上，等人回话。这是正常的停：
            bot 发完方案或者问完澄清，每一次都停在这里。next 就是那个调了
            interrupt(...) 的节点本身 —— 它还没跑完，下一句话进来时从它的第一行重跑

        interrupts=()                 next=('understand_intent',) -> FAILED
            例：understand_intent 调 OpenAI 超时。它的写一笔都没落，最新那份存档还是
            它开跑前那份，所以 next 就是崩溃的那个节点

        interrupts=()                 next=()              -> ENDED
            没有 interrupt，next 也空了：图已经走到 END（用户说了「算了」，或者采集
            跑完了）。这是正常结束，不是出错

    注意「停」只是数据库存档里的样子，没有哪个进程在「暂停」着：每一轮对图的 invoke 跑到上面
    某一种样子就返回了，连接也关了，下一句话来时再从库里读出来接着跑。

    判 FAILED 的三种误会，都不会出事：

        正在跑的 thread 也会被判成 FAILED
            例：understand_intent 正在调 OpenAI，同一个 thread 又来一句「好了吗」。
            这时最新那份存档是 next=('understand_intent',)、interrupts=()，跟它崩溃了
            长得一模一样。挡住它的是存档之外的 _busy：第一次 _route 判 RETRY -> _occupy
            占不到（正在跑的那一轮占着）-> 回 BUSY_TEXT，这句不进图。
            _busy 只管得了这一个进程：别的进程（比如手跑的 scripts）同时对同一个 thread
            invoke，它看不见。

        进程半路被杀（重启、部署、断电）
            _busy 跟着进程没了，存档停在当时那个节点。下一句判 RETRY，这时它确实就是
            「上一轮没跑完」，判 FAILED 没判错。停在 run_harvest 前面的不重跑，退回 await_go
            （见 _invoke 的 RETRY）。

        一个认不出来的窄缝
            进程恰好在 await_go 拿到「跑」之后、return 之前那一瞬间被杀：「跑」已经存进
            __resume__，但进程没来得及留下 __error__，存档跟正常等回话一模一样，这里判
            WAITING，下一句会被丢掉。那几行只有一句 print 和 return，是微秒级的一瞬间，
            没有为它去读 checkpoint_writes 的内部格式。
    """
    if not snapshot.values:
        return conversation.NO_CONVERSATION
    if _node_crashed_after_reply(snapshot):
        return conversation.FAILED
    if snapshot.interrupts:
        return conversation.WAITING
    if snapshot.next:
        return conversation.FAILED
    return conversation.ENDED


def _node_crashed_after_reply(snapshot: Any) -> str:
    """找出「收到用户回复之后、return 之前崩溃」的那个节点，返回它的名字；没有就返回 ""。

    输入：snapshot，app.get_state(config) 的返回值
    输出：节点名，例 "await_go"；没有就是 ""

    只有第一行就是 interrupt 的节点会这样崩溃（query 那张图是 await_go / ask_clarification）：
    第一次进去只会停住。等用户回了话、interrupt 那一行把回复交出来之后，下面那几行才可能崩溃
    （今天只有一句 print 和 return，比如 print 往 GBK 编码的输出里写 emoji）。

    例：图停在 await_go，用户回「跑」，await_go 拿到「跑」之后在 print 那一行崩溃。
    库里最新那份存档还是 await_go 停下时落的那份（没有新落存档），样子是：

        next               ()                                 <- 空的，不是 ('await_go',)
        interrupts         (Interrupt(value={"kind": "go", ...}),)   还挂着
        tasks              (PregelTask(name="await_go", error="RuntimeError(...)",
                                       interrupts=(Interrupt(...),)),)
        checkpoint_writes  __interrupt__   方案 v1
                           __resume__      「跑」              <- 用户那句存在这里
                           __error__       RuntimeError(...)
        state              messages 里没有「用户: 跑」：await_go 没 return，一笔写都没落

    认法就是 tasks 里有一个任务既带 error、又带 interrupt。对照另外两种：

        正常停着等回话   PregelTask(name="await_go", error=None, interrupts=(Interrupt(...),))
        普通节点崩溃     PregelTask(name="understand_intent", error="...", interrupts=())
    """
    for task in snapshot.tasks:
        if task.error and task.interrupts:
            return task.name
    return ""


def setup_checkpointer() -> None:
    """确认 checkpointer 自己那几张表都在，进程起来时调一次。

    输入：无
    输出：无。表已经在就什么都不做（setup 是幂等的，按它自己的
          checkpoint_migrations 表判断缺哪几步）。若缺某些表，会直接建出来。

    放在进程启动时而不是每一轮：它要查一遍迁移记录，每一轮都做就是每一轮白查一次。
    几张图共用这一套表（存档靠 thread_id 分开），所以只调一次，不是一张图一次。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from persistence import db

    with PostgresSaver.from_conn_string(db.connection_url(direct=True)) as saver:
        saver.setup()


@contextlib.contextmanager
def open_saver() -> Iterator[Any]:
    """开一条到 Neon 的 checkpointer 连接，交出存档器，用完关掉。

    输入：无（连接串从环境变量或 .env 读，走 direct 那条）
    输出：一个 PostgresSaver，连着 Neon

    每来一句要处理的话（新开的对话也好，已有 thread 里的回话也好）就调一次，做三步：

        1 连上 Neon          PostgresSaver.from_conn_string 开一条数据库连接
        2 两张图各编一张     graph.build(saver)：在 Python 进程的内存里现编一张图（CompiledStateGraph，
                             底层是 Pregel：节点表、channel、tick 那套循环），把这条连接交给它当存档器。
                             两张图共用这一条连接：要认出一句话归哪张图，得两张图的存档都查一次
        3 用完关掉连接       with 块一结束，连接就关了，两张图也跟着扔掉

    编出来的图是空的：它只有「有哪些节点、边怎么连、每个节点跑哪个函数」，不带任何
    对话的 state。这一轮是哪组对话，要到 get_state / invoke 带着 config 里的 thread_id
    时，它才去 Neon 读出那一份存档。好比一台空机器，按 thread_id 从柜子里抽出档案夹。
    频道闲聊这类不用处理的话，在 handle 里调它之前就返回了，一次都不开。

    为什么查状态也要先编一张图，不直接 SELECT checkpoints 那几张表：库里存的不是
    我们要的答案，只是原料。
        checkpoints        每个 channel 的版本号、每个节点上次看到的是哪一版（简单的值
                           如字符串、数字也内联在这一列 JSONB 里）
        checkpoint_blobs   其余 channel 的值（列表、字典），序列化过的
        checkpoint_writes  还没合并的写，挂着的 interrupt 就在这里
    库里没有「下一步是哪个节点」这一列，也不存图的形状（只存节点名）。get_state 的 next
    是读出存档之后，拿这张图的节点表和边现算的。没有这张图就算不出来 next，
    也就分不出 FAILED 和 ENDED。
    再加上这几张表的格式是 langgraph 内部的，升级时会变（它自己有一张 checkpoint_migrations
    管版本），我们直接查就得跟着它改。invoke / update_state 更不用说，本来就要图才能跑。

    连接每一轮现开，不在进程里常驻一条：两句话之间可能隔三天，而 Neon 闲置五分钟就
    挂起，一条挂了三天的连接必然已经断了。代价是每一轮多一次冷启动（约一秒），而用户
    本来就在等一句回复。按钮那条路是同一个做法（handle_click）。
    图也每一轮重编：它不带状态，每轮重编一张跟一直用同一张没有区别，编译只是几毫秒的
    内存操作。

    走 direct 不走 pooled：两条都能用，checkpointer 连接少又不常驻，用不上池子。

    import 放在函数里：测试从来不走这个函数（它们传一个交出 InMemorySaver 的替身进
    ConversationRunner），放在顶上的话连 import 这个模块都要装着 postgres 那个包。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from persistence import db

    with PostgresSaver.from_conn_string(db.connection_url(direct=True)) as saver:
        yield saver


@dataclass(frozen=True)
class _Route:
    """_route 认出来的结果：这一句归哪张图、读写哪一份存档、怎么处理。

    三个真例子：

        频道里「@bot 帮我找骨传导耳机」
            _Route(graph=query, config={"configurable": {"thread_id": "slack:C0000000001:1790126036.992409"}},
                   action="start", lead_id=None)
        North Field Technologies 那张卡片（lead 1289）下面「@bot 帮我写封邮件给他们」，这个 thread 还没有对话
            _Route(graph=drafting,
                   config={"configurable": {"thread_id": "draft:C0000000001:1790199000.000100"}},
                   action="start", lead_id=1289)
        同一个 thread 里 bot 问完「请问你想给 North Field Technologies 写哪些 outreach 信息？」之后，
        用户不 @ 回了一句「给 2 号写封邮件」
            _Route(graph=drafting, config={... "draft:C0000000001:1790199000.000100" ...},
                   action="resume", lead_id=None)

    lead_id 只在「线索卡下面 @ 了、还没有对话」那一种情况下有值（那时才去 Slack 读卡片），
    其余都是 None：已经有存档的对话，lead_id 早就在 state 里了
    """

    graph: ConversationGraph
    config: dict
    action: str
    lead_id: int | None


class ConversationRunner:
    """管这个进程里所有正在进行的对话：谁在跑、谁在排、同一句话有没有处理过。

    整个进程只有一个实例（build_app 里造的），所有 thread 共用。它手上只有三样
    内存里的东西，一样都不落库，进程重启就清空 —— 清空了也没关系，对话本身的
    状态全在 checkpointer 里：

        _busy    正在跑图的 thread 的 thread_id，例 {"slack:C0000000001:1790126036.992409"}
        _seen    最近处理过的消息，例 deque([("C0000000001", "1790126036.992409", ""), ...])
        _pool    线程池，_MAX_WORKERS 条线程

    再加一样不变的：_graphs，这个 runner 管的几种图，从进程起来到结束一直就是这两个：
        (ConversationGraph(name="query", thread_prefix="slack", build=query 的 build_graph, ...),
         ConversationGraph(name="drafting", thread_prefix="draft", build=drafting 的 build_graph, ...))
    它们是「一种图」的说明书，不是某一条对话，也不是编好的图：
        说明书（ConversationGraph）   两个，进程起来时造好，不带任何对话，不连库
        编好的图（CompiledStateGraph）每一轮现编：handle 开一条连接 -> graph.build(saver)，
                                      两种各编一张；也不带对话，只是连着库、知道节点和边
        一条对话                      库里 checkpoint 表的一份存档，钥匙是「前缀:频道:thread 开头的 ts」。
                                      频道里同时有十条对话，库里就有十份存档，说明书还是那两个
    所以「这句话归哪张图」不是去比对哪个对象，而是按每种图的前缀拼出钥匙、拿编好的图去库里查
    有没有这份存档（_route 第一步）。

    上面三样只有一份，几种图共用：_busy 的钥匙是 Slack thread（Inbound.thread_id），
    不是某张图的存档钥匙，所以同一个 thread 同一时刻只跑一轮，不管是哪张图的。
    """

    def __init__(
        self,
        graphs: Sequence[ConversationGraph],
        *,
        root_card: Callable[[str, str], int | None],
        saver_opener: Callable[[], contextlib.AbstractContextManager] = open_saver,
    ):
        """每个 runner 要三样东西。

        输入：
            graphs         这个 runner 管的几张图，例 (query_graph(), drafting_graph())。
                           顺序就是 _route 查存档的顺序；starts_in 和 thread_prefix 每个取值只许一张图
            root_card      问 Slack「这个 thread 的开头是不是线索卡，是哪一家」的函数，
                           例 root_card("C0000000001", "1790199000.000100") -> 1289，不是线索卡 -> None。
                           由 build_app 造好传进来（它手上有 Slack 客户端和 bot_user_id），
                           这里不认识 Slack 客户端
            saver_opener   每一轮调一次、交出存档器的上下文管理器。生产不传，用 open_saver（Neon）；
                           测试传一个交出 InMemorySaver 的替身
        """
        # 两张图抢同一个 starts_in 的话，START 挑哪张就看顺序了；同一个前缀的话两张图读写同一份存档。
        # 都是配错了，进程起来时就停，不等到第一句话进来才错
        for field in ("starts_in", "thread_prefix"):
            values = [getattr(g, field) for g in graphs]
            if len(values) != len(set(values)):
                raise ValueError(f"几张图的 {field} 撞了：{values}")
        self._graphs = tuple(graphs)
        self._root_card = root_card
        self._saver_opener = saver_opener
        self._pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="conversation")
        self._lock = threading.Lock()  # 只保护下面这两样，拿住的时间只有几行
        self._busy: set[str] = set()
        self._seen: collections.deque[tuple[str, str, str]] = collections.deque(maxlen=_SEEN_LIMIT)

    def submit(self, inbound: Inbound, say: Callable[[dict], Any]) -> None:
        """收下一条消息，交给线程池里的一条空线程去处理，自己马上返回。

        输入：
            inbound   读懂了的消息
            say       往这条消息所在的 thread 里发一条消息的函数，
                      例 say({"text": "你想找做哪一类设备的公司？"})
        输出：无

        谁在调它：triggers/slack_buttons.py 里的 _on_message。整条链是

        1.Slack 通过 Socket Mode 长连接推来一个 message 事件。
        整个进程只造一个 ConversationRunner 实例（build_app 里造的），所有 Slack 对话都交给它。

        2.Bolt 查表调 _on_message(event, client, logger)。

        3.parse_message 读成 inbound（读不懂、bot 自己说的这类在这里就丢了）。
        频道里读得懂的每一条都会进来，包括闲聊；挑出说给 bot 的那些是 handle 的事

        4.当场造一个 say。
        say 是 _on_message 当场造的：它记着这条消息的 channel 和所在 thread 开头那条的
        ts（root_ts），调用时用带 bot token 的客户端发 chat.postMessage。所以不管这边
        什么时候调 say，回复都落在这条消息所在的那个 thread 里。这里不认识 Slack 客户端，
        只管调它。

        5.runner.submit(inbound, say) => self._pool.submit(self._run, inbound, say)
        在线程池里找一条空线程，在那条线程上调用 self._run(inbound, say)，调用方不等它跑完就马上返回。
        self._pool 在 __init__ 里就建好了，是一个 ThreadPoolExecutor，里面有 _MAX_WORKERS 条线程。
        self._pool 的 submit 是 ThreadPoolExecutor 自己的方法，跟这个 submit 只是同名。
        线程的工作模式：
        4 条线程是同时处理几条inbound messages事件的上限，并不是一条线程包一组对话。
        一条线程处理完一条消息就空出来，可接下一条。
        假设同时有5条消息，那么第 5 条消息在线程池的队列里等，有线程空出来就轮到它。不丢，也不报错。
        真正慢的只有采集，要好几分钟，而且同一时刻只能跑一个（被 advisory lock 锁着）。
        不要把两种情况搞混：线程池的排队管的是「整个进程同一时刻最多处理几条消息」，
        不分这些消息来自哪个 thread；同一个 thread 正在跑图时又来一句，不排队、直接回忙，
        是另一件事（在 handle 里）。

        6.在交给线程池之前，先挡重投：同一条消息第二次来就直接丢。认的是 Inbound.dedup_key
        （channel、ts、编辑时间三样都相同才算同一条；编辑过的消息 ts 跟原句一样，要靠编辑时间分开）：
            第一次   ("C0000000001", "1790126036.992409", "") 不在 _seen 里 -> 记下，交给线程池
            重投     同样三样已经在 _seen 里 -> 什么都不做
        """
        key = inbound.dedup_key
        with self._lock:
            if key in self._seen:
                logger.info(f"重投的消息，丢掉：{key}")
                return
            self._seen.append(key)
        self._pool.submit(self._run, inbound, say)

    def _run(self, inbound: Inbound, say: Callable[[dict], Any]) -> None:
        """线程池里的一条线程跑这个：处理这一条消息，要回话就用 say 发回它所在的 thread。

        输入：同 submit
        输出：无

        谁在调它：只有 submit 里那句 self._pool.submit(self._run, inbound, say)，
        由池子里的某条空线程来调。

        异常一律在这里接住，变成一句发回 thread 的话：线程池里的异常没人会去取
        （submit 没去拿 ThreadPoolExecutor 返回的那个 Future），不接的话它就无声地没了，
        用户那边什么都看不到。

        「同一个 thread 同一时刻只跑一轮图」不在这里管，在 handle 里，理由写在 handle。
        """
        try:
            reply = self.handle(inbound)
            if reply:
                say(reply)
        except Exception as err:  # noqa: BLE001 - 线程池里的异常必须在这里变成一句话
            logger.exception(f"{inbound.thread_id} 这一轮出错")
            say(conversation.text_reply(f"出错了：{type(err).__name__}: {err}"))

    def _occupy(self, thread_id: str) -> bool:
        """完成占住一个 thread这件事：
            占到了返回 True，已经被别的线程占着返回 False。

        输入：thread_id，例 "slack:C0000000001:1790126036.992409"
        输出：True / False

        「查一下在不在 _busy 里」和「加进去」必须在同一把锁里做完。不锁的话：

            线程 a（「跑」）            查 _busy：不在
            线程 b（「再加一条」）      查 _busy：不在        a 还没来得及加
            线程 a                      加进去，开始 invoke
            线程 b                      也加进去，也开始 invoke

        两条线程同时对同一份存档 invoke，两句话都被当成对同一版方案的回答，
        「跑」那一句已经在花钱，「再加一条」又让它重拟。

        锁住的时候：a 先拿到锁，查、加、放锁，一气呵成；b 停在 with self._lock 那一行
        等 a 放锁，轮到它时再查，发现已经在里面，返回 False。with 进去时拿锁、出来时
        放锁，中间出了异常也会放。锁只包这几行，不包跑图：一个 thread 在采集，
        别的 thread 照样能占、能跑。
        """
        with self._lock:
            if thread_id in self._busy:
                return False
            self._busy.add(thread_id)
            return True

    def _release(self, thread_id: str) -> None:
        """放开 _occupy 占住的 thread。例：放开之后同一个 thread 的下一句就能进图了。"""
        with self._lock:
            self._busy.discard(thread_id)

    def _config(self, graph: ConversationGraph, channel: str, root_ts: str) -> dict:
        """某个 Slack thread 在某张图里的那份存档，交给 get_state / invoke 的 config。

        输入 -> 输出：(drafting, "C0000000001", "1790199000.000100")
                   -> {"configurable": {"thread_id": "draft:C0000000001:1790199000.000100"}}
        """
        thread_id = conversation.thread_id_of(channel, root_ts, prefix=graph.thread_prefix)
        return {"configurable": {"thread_id": thread_id}}

    def _starter(self, starts_in: str) -> ConversationGraph:
        """在这个地方开对话的是哪张图。例："channel" -> query，"lead_card" -> drafting"""
        return next(g for g in self._graphs if g.starts_in == starts_in)

    def _route(
        self,
        apps: Mapping[str, Any],
        inbound: Inbound,
        lookup: bool,
        *,
        known_card: int | None = None,
        recheck: bool = False,
    ) -> _Route:
        """认出这一句归哪张图、读写哪一份存档、怎么处理。

        输入：
            apps        这一轮编好的几张图，图名 -> 编好的图，
                        例 {"query": <query 那张>, "drafting": <写稿那张>}
            inbound     这一条消息
            lookup      要不要查存档（needs_lookup 的结果：在 thread 里才查）
            known_card  handle 占住之后第二次认的时候，把第一次读到的 lead_id 带进来，例 1289；第一次是 None。
                        thread 开头那张卡片是哪一家，两次之间不会变，第二次不用再去 Slack 读一趟
            recheck     是不是占住之后的第二次。只管打不打日志：一条要跑图的消息会认两次，
                        只在真正生效的那一次打一行（第二次；不用跑图的第一次就是最后一次，也打）
        输出：_Route，例子见那个类

        四步，每一步的例子都是同一个 thread：North Field Technologies（lead 1289）那张卡片下面，
        卡片的 ts 是 "1790199000.000100"。
        """
        # 一、查存档。
        # 同一时刻，slack里可能有多个用户在交互，库里同时躺着很多份存档（一条对话一份），
        # 有的是 query 的，有的是 drafting 的。
        # 因此，当接受到一个Inbound，我们需要调查清楚，它属于哪种图来管理。
        #
        # 怎么查：self._graphs 从头到尾只有两个（query、drafting），是「一种图」的说明书，不是某一条对话
        # （见类说明）。不是拿这两个对象去比对，而是对每一种图：
        #     1 用这种图的前缀，给这个 thread 拼出一把钥匙（config）
        #     2 拿这种图编好的那一张（apps[graph.name]，handle 用同一条连接编的，这里不再开连接）
        #       去库里查这把钥匙下有没有存档（get_state）
        # 查到有存档的那种就停：一个 thread 最多有一种图的对话
        #
        # 如果lookup=True，说明该Inbound在某个thread里，才要查；lookup=False（频道里的起始句）不查库
        owner: ConversationGraph | None = None
        status = conversation.NO_CONVERSATION
        # 查到的那份存档，第三步重开对话时要从里面读 lead_id
        snapshot = None
        if lookup:
            for graph in self._graphs:
                # 1 拼钥匙：同一个 thread，两种图拼出来的钥匙只差前缀
                #   query     {"configurable": {"thread_id": "slack:C0000000001:1790199000.000100"}}
                #   drafting  {"configurable": {"thread_id": "draft:C0000000001:1790199000.000100"}}
                config = self._config(graph, inbound.channel, inbound.root_ts)
                # 2 查库：get_state 按这把钥匙去 checkpoint 表取最新那份存档，conversation_status 说出它的状态
                #   这把钥匙下一份存档都没有 -> NO_CONVERSATION，说明这个 thread 不是这种图的对话
                snapshot = apps[graph.name].get_state(config)
                status = conversation_status(snapshot)
                # 如果不是 no_conversation，说明这把钥匙下有存档，则该thread找到了自己的图
                if status != conversation.NO_CONVERSATION:
                    owner = graph
                    break
        # 两种都没查到：owner=None, status=no conversation。
        # 有两种可能：这句话是频道里的起始句（lookup=False，根本没查）；
        # 或者它在一个还没有对话的 thread 里（线索卡下面的第一句、同事自己开的讨论串）

        # 二、检查 needs_card_check
        # 若 needs_card_check 为 false，则这条Inbound是发送到了某个query类型的thread的对话里，
        # 或者是query的起始句
        if not conversation.needs_card_check(inbound, status):
            lead_id = None
        # 反之，该Inbound是发送到了某个线索卡下，属于drafting类型的对话，
        # 有可能是该线索卡下的第一句话，有可能是其他句
        # 若第一次已经读到了的（known_card），第二次直接用
        elif known_card is not None:
            lead_id = known_card
        # 若这是第一次读到该线索卡，则取出lead_id
        else:
            lead_id = self._root_card(inbound.channel, inbound.root_ts)

        # 三、怎么处理，规则是 conversation.decide 里那四问，它不管是哪张图
        # 输出   action，七种之一，例 "resume"
        action = conversation.decide(inbound, status, root_is_card=lead_id is not None)

        # 三（续）、对话走完了又 @ 了 bot：这张图肯重开（graph.restart 不是 None），就在同一份存档上从头再跑。
        #        例：写稿对话走到 END，用户在卡片 thread 里又说「<@bot> 再给 3 号写一条附言」
        #            decide 判的是 FINISHED -> 改成 START，lead_id 从旧存档里读（1289）
        #        query 的 restart 是 None，照旧回「这次对话已经结束了」
        if (
            owner is not None
            and owner.restart is not None
            and action == conversation.FINISHED
            and inbound.mentioned
            and snapshot is not None
        ):
            action = conversation.START
            lead_id = owner.restart(snapshot.values)

        # 四、归哪张图
        #        有存档的：就是存档所在那张（owner）
        #        没存档的：看这句话在哪儿说的。在 thread 里 -> lead_card 那张（drafting），
        #        不在 -> channel 那张（query）。
        #        没存档又不是 START 的（IGNORE / OTHER_THREAD / EDITED）用不到图，挑哪张都一样，照这条规矩挑
        graph = owner or self._starter("lead_card" if inbound.thread_ts else "channel")
        # 一条消息只打一行：要跑图的，第一次认完还要占住再认一次，这一行留给第二次
        if recheck or action not in _GRAPH_ACTIONS:
            logger.info(
                f"{inbound.thread_id} {graph.name} user={inbound.user} status={status} "
                f"edited={bool(inbound.edited_ts)} action={action}"
            )
        return _Route(
            graph=graph,
            config=self._config(graph, inbound.channel, inbound.root_ts),
            action=action,
            lead_id=lead_id,
        )

    def handle(self, inbound: Inbound) -> dict | None:
        """处理一条inbound message，返回要发回 thread 的那条消息；不用回就返回 None。

        输入：inbound message
        输出：chat.postMessage 的参数字典，或者 None。

        同一个 thread，也就是同一组对话里，每个时刻只能跑一轮 invoke，不可能同时跑两轮。
        注意「一轮 invoke」不是一个 superstep：superstep 是图里的一步（这一步排上的节点
        一起跑、跑完合并写），一轮 invoke 会连着走好几步，直到排不出新任务（走到 END），
        或者停在一个 interrupt 上，才返回。例：用户说完第一句，一轮 invoke 走
        understand_intent -> retrieve_knowledge -> plan_queries -> await_go 四步，
        停在 await_go 的 interrupt 上返回。
        例：
        如果在一个thread里，message1 正在跑一轮 invoke，此时又来了 message2 也想跑一轮，
        那么，直接回复 message1还在处理。注意，这里是直接忽略 message2， 而不是让它排队。
        因为，如果让 message2 排队，等 message1 处理完，那时候图已经走到下一步了，
        message2 就会被当成对下一步的回答，就会错位。
            12:00:00  「跑」                    该 thread 正在跑这一轮，要走到 run_harvest（几分钟）
            12:00:40  「等下，再加一条会议麦的」  检测到该thread 被占着 -> 回 BUSY_TEXT，这句不进图

        因此，如果决定该 message 所在的 thread需要跑图之前，也就是说，如果decide的结果是
        START或RESUME或RETRY三种情况，在正式跑这一轮之前，必须标记这个thread进入self._busy，
        表示这个thread即将跑图，已被占用，不能再对这个thread进行跑图指令。
        然后才可以给这个thread invoke一轮，跑完这一轮后，状态再放开。

        并且，占住之后、真正 invoke 之前，还需要再 _route 一次，真正用的是第二次的结果。原因：
        第一次 _route 是在还没占住的时候查的状态。从查完到 _occupy 占住之间有一个空档，
        同一个 thread 的另一句话，可能正好在这个空档里被别的线程占住、跑完、放开。
        注意 handle 从头到尾都在同一条线程上跑，线程 a 不会在 _route 之后「空出来」、
        把 _occupy 交给别的线程；空档只是线程 a 自己两行代码之间的那一小段时间。
        例：图停在 await_go，用户几乎同时发了两句
            线程 a  收到「我觉得这个方案还要改进 XXX」  第一次 _route：WAITING -> RESUME
            线程 b  收到「算了，取消吧」                第一次 _route：WAITING -> RESUME
            线程 b  先 _occupy 成功 -> invoke -> classify_reply 判 cancel -> 图走到 END -> 放开
            线程 a  这时才 _occupy，占得到（b 已经放开了）
                    如果直接用第一次的 RESUME：Command(resume=...) 投给一个已经到 END 的图，
                    图一个节点都不跑、也不报错，原样交回那份 state，a 就又回了一遍
                    「好，这次不跑了」，用户会以为「改进 XXX」那句被当成了取消。
                    再 _route 一次：查到 ENDED -> FINISHED，回「这次对话已经结束了」，是真话。
        这个空档只有几毫秒，后果是回错一句话，不是多花钱；代价是多一次 get_state。

        例：
        用户发了起始句：@bot 帮我找骨传导耳机有关的leads
        => 第一次_route判断状态为 START
        => 代表该message会引发这个thread的第一轮invoke
        => _occupy标记busy
        => 第二次_route，状态还是START（起始句不查库，两次一定一样；而且每句起始句的
           ts 都不同，thread_id 也不同，不会有两句话抢同一个 thread）
        => _invoke
        => 该thread就跑 start -> understand_intent，信息不够就停在 ask_clarification
           的 interrupt 上问一句；够了就一路走到 await_go，停住，交出方案

        用户在某个thread里发：别带 Kickstarter（图正停在 await_go）
        => 第一次_route判断状态为 RESUME
        => 代表该message是用户对某条Interrupt回复的RESUME值
        => _occupy标记busy
        => 第二次_route，状态还是RESUME
        => _invoke，该thread把此message当resume值还给await_go
           -> classify_reply 判 replan -> understand_intent -> retrieve_knowledge
           -> plan_queries -> await_go 停住，交出方案 v2

        上一轮 understand_intent 调 OpenAI 超时，存档停在它前一步，用户在 thread 里回：好了吗
        => 第一次_route判断状态为 RETRY（存档里 interrupts 空、next=('understand_intent',)）
        => 代表这句话只是「接着跑」的信号，它自己的内容不进图
        => _occupy标记busy
        => 第二次_route，状态还是RETRY
        => _invoke，invoke(None)：不给新输入，understand_intent 从第一行重跑，
           读的还是它崩溃前那份 state，「好了吗」不会进 messages
        """
        # 使用 needs_lookup 来检查这条消息是否需要查数据库。
        # 如果这条消息没有 thread_ts，就说明它只有 ts，因此它自己就是起始句，
        # 不可能储存在db里，那就不需要费时间去查找，就返回 false。
        # 如果这条消息有 thread_ts，就说明它是某个已经存在的 thread 下面的回复。
        # 这个 thread 可能是 bot 的对话，也可能是同事的讨论串或者线索卡（库里没有存档），
        # 所以要查一下该 thread 在库里的状态，返回 true。
        lookup = conversation.needs_lookup(inbound)

        # 如果该消息不在某个thread里，不需要查库，
        # 并且通过conversation.decide方程判断出来，该消息的处理结果是IGNORE，
        # 那直接可以不处理该消息，连 Neon 都不连。
        if (
            not lookup
            and conversation.decide(inbound, conversation.NO_CONVERSATION, root_is_card=False)
            == conversation.IGNORE
        ):
            return None

        # 对于并非IGNORE的消息，开一条到 Neon 的连接，几张图都用它编好，
        # 之后查状态、invoke 都用这几张图
        with self._saver_opener() as saver:
            # 输出   apps -> {"query": <query 那张编好的图>, "drafting": <写稿那张编好的图>}
            apps = {graph.name: graph.build(saver) for graph in self._graphs}

            # 使用 _route 看这一句归哪张图、读写哪一份存档、怎么处理
            route = self._route(apps, inbound, lookup)

            # 如果该消息并非可以触发跑图，那么就对应给出回复
            if route.action not in _GRAPH_ACTIONS:
                return _fixed_reply(route.graph, route.action)

            # 如果该消息可以触发跑图，那么，需要在跑图之前，标记该thread。
            # 在 occupy里，如果标记该thread进self._busy的这个动作失败了，即occupy会返回false，
            # 就代表该 thread 已经在跑图了，因此，需要提示用户，正在繁忙。
            # 占的是 Slack thread（inbound.thread_id），不是某张图的存档：两张图共用这一把锁
            if not self._occupy(inbound.thread_id):
                return conversation.text_reply(conversation.BUSY_TEXT)

            # 如果 occupy 返回 true，就代表，该 thread 没有在跑图，且成功被标记进入busy。
            # 那接下来，就可以 invoke 一轮了。
            try:
                # 占住之后再_route一次，用这一次的结果去_invoke（理由见上面 docstring）。
                # 第一次读到的卡片（lead_id）带进去，不再打一趟 Slack
                route = self._route(apps, inbound, lookup, known_card=route.lead_id, recheck=True)
                if route.action not in _GRAPH_ACTIONS:
                    return _fixed_reply(route.graph, route.action)
                return self._invoke(
                    route.graph,
                    apps[route.graph.name],
                    route.config,
                    route.action,
                    text=inbound.text,
                    lead_id=route.lead_id,
                )
            # 不管try里的return是成功还是失败，finally 都会放开这个 thread。
            # 不放的话它会一直留在 _busy 里，直到进程重启
            finally:
                self._release(inbound.thread_id)

    def start_from_card(self, channel: str, card_ts: str, lead_id: int, say: Callable[[dict], Any]) -> None:
        """点了「要联系」：在那张卡片的 thread 里开一个起草对话，交给线程池，自己马上返回。

        输入：
            channel   卡片所在的频道，例 "C0000000001"
            card_ts   那张卡片那条消息的 ts，例 "1790199000.000100"，就是起草 thread 的开头
            lead_id   哪一家，例 1289（按钮上 block_id "lead:1289:run:46" 里读出来的）
            say       往那张卡片的 thread 里发一条消息的函数
        输出：无

        谁在调它：triggers/slack_buttons.py 的 _immediate，点的是「要联系」、记完账、改完卡片之后。
        不在 Bolt 的 handler 里跑图，理由同 submit：Bolt 的线程要马上还回去，跑图要连库、调模型
        """
        self._pool.submit(self._run_card_start, channel, card_ts, lead_id, say)

    def _run_card_start(self, channel: str, card_ts: str, lead_id: int, say: Callable[[dict], Any]) -> None:
        """线程池里的一条线程跑这个：开起草对话，把 bot 的第一句发进卡片的 thread。异常规矩同 _run。"""
        try:
            reply = self.handle_card_start(channel, card_ts, lead_id)
            if reply:
                say(reply)
        except Exception as err:  # noqa: BLE001 - 线程池里的异常必须在这里变成一句话
            logger.exception(f"lead={lead_id} 卡片 {card_ts} 开起草对话出错")
            say(conversation.text_reply(f"出错了：{type(err).__name__}: {err}"))

    def handle_card_start(self, channel: str, card_ts: str, lead_id: int) -> dict | None:
        """在卡片的 thread 里开起草对话，返回 bot 的第一句；这个 thread 已经有对话了就不重开。

        输入：同 start_from_card，例 ("C0000000001", "1790199000.000100", 1289)
        输出：
            这个 thread 还没有对话   跑一轮 drafting：retrieve_knowledge -> ask_request 停住
                                     -> {"text": "请问你想给 North Field Technologies 写哪一种 …"}
            已经有了（先 @ 过 bot） {"text": ALREADY_STARTED_TEXT}，存档一个字不动
            正有一轮在跑            {"text": BUSY_TEXT}

        跟 handle 用的是同一把锁（_busy），钥匙也是同一个 Slack thread：卡片下面刚 @ 了 bot、那一轮
        还在跑的时候点了按钮，这里占不到，不会两边各开一个。
        「已经有了就不重开」只看写稿那张图的存档：卡片的 thread 里不会有找公司的对话
        """
        graph = self._starter("lead_card")
        # 我们需要检查，该线索卡所在的thread是否正在忙
        # 因为，检查_occupy的key的形式还是按照query的来的，所以：
        # 我们还是拼凑成prefix只有slack的形式去检查，slack_thread = "slack:C0000000001:1790199000.000100"
        slack_thread = conversation.thread_id_of(channel, card_ts, prefix="slack")
        # 要查看该条thread是否在忙，如果在忙，说明正有用户在该线索卡下交互
        if not self._occupy(slack_thread):
            return conversation.text_reply(conversation.BUSY_TEXT)
        try:
            with self._saver_opener() as saver:
                app = graph.build(saver)
                # 输出   config -> {"configurable": {"thread_id": "draft:C0000000001:1790199000.000100"}}
                config = self._config(graph, channel, card_ts)
                status = conversation_status(app.get_state(config))
                logger.info(f"{slack_thread} {graph.name} 点了要联系 lead={lead_id} status={status}")
                if status != conversation.NO_CONVERSATION:
                    return conversation.text_reply(conversation.ALREADY_STARTED_TEXT)
                # 点按钮没有一句话，text 是空串：第一份 state 的 request 为空，图会先问「写什么」
                return self._invoke(graph, app, config, conversation.START, text="", lead_id=lead_id)
        finally:
            self._release(slack_thread)

    def _invoke(
        self,
        graph: ConversationGraph,
        app: Any,
        config: dict,
        action: str,
        *,
        text: str,
        lead_id: int | None,
    ) -> dict:
        """跑一轮图，返回要发回 thread 的那条消息。调用方已经占住了这个 thread。

        输入：
            graph    这一句归哪张图，例 query_graph()
            app      这一轮编好的图（graph.build(saver) 编出来的）
            config   {"configurable": {"thread_id": ...}}
            action   START / RESUME / RETRY 之一
            text     用户这一句（去掉 @ 之后），例 "别带 Kickstarter"；点按钮开的对话是 ""
            lead_id  START 时这个 thread 开头那张线索卡是哪一家，例 1289；别的时候 None
            （* 后面的两个只能按名字传：两个都是「这一句的内容」，按位置传容易传反）
        输出：chat.postMessage 的参数字典

        三种 action 的差别只在交给 app.invoke 的第一个参数（graph_input）：
        如果action是START：
            invoke需要一整份初始state的参数，由图自己给（graph.first_input）
            query：graph_input = initial_state("帮我找找骨传导耳机的初创")
            库里还没有这个 thread_id 的存档，图从 START 开始，第一步跑 understand_intent
            drafting：graph_input = initial_state(1289, "帮我写封邮件给他们")，第一步跑 retrieve_knowledge
        如果action是RESUME：
            invoke需要用户输入的resume数值
            graph_input = Command(resume="别带 Kickstarter")
            图从停着的那个 interrupt 节点第一行重跑，这一次 interrupt(...) 那一行不再停，
            而是把「别带 Kickstarter」当返回值交给节点，节点接着往下跑
        如果action是RETRY：
            graph_input = None，意思是不给新输入，从当崩溃前的那份存档。
            例：崩溃前的存档是存档 next=('understand_intent',)，就是说 understand_intent是崩溃的节点，
            所以崩溃前的存档的next才是understand_intent。
            用户回「好了吗」，这句话被decide为RETRY，因此invoke(None)。
            但是「好了吗」这句话本身是没有进入 state.messages里的。
                -> understand_intent 重跑，往下走到下一个 interrupt 或者 END

            有两种存档不能这样重跑，要先退回（_back_to）。它们就是 _recover 的情况一、二：
            正常崩溃时 _recover 当场就退回了，存档留不到下一句。留到了这里，说明上一轮
            _recover 没机会跑，或者它退回的时候自己又出了错：

                图自己说不能重跑（graph.unfinished_retry 给了节点）  -> 退回它给的那个节点
                    例（query）：next == ('run_harvest',)，退回 await_go。
                    用户说「跑」，采到一半进程被重启，_recover 没机会跑。
                    下一句「算了」要是照常 invoke(None)，run_harvest 会直接重跑、又花一次钱。
                    说明里不说「一分钱没花」：被杀的时候可能已经在采了
                _node_crashed_after_reply 找到节点     -> 退回那个节点
                    例：await_go 拿到「跑」之后崩溃，_recover 退回的时候 Neon 又掐了连接。
                    下一句「算了」要是照常 invoke(None)，await_go 用的还是存下的「跑」。
                    （进程被杀留不下这种存档，见 conversation_status 那条窄缝）

            为什么不能重跑见 _recover 情况一、二，库里怎么变见 _back_to。

        三种 action 都走下面同一句 app.invoke。不管是哪一种，跑这一轮的时候哪个节点
        抛了异常，都进 except，交给 _recover 看存档停在哪一步、该怎么收拾。
        """
        # 输出   where -> "draft:C0000000001:1790199000.000100"，这份存档的钥匙，只进日志
        where = config["configurable"]["thread_id"]
        if action == conversation.START:
            graph_input = graph.first_input(text, lead_id)
        elif action == conversation.RESUME:
            graph_input = Command(resume=text)
        else:
            # RETRY 的两种存档不能直接重跑，先退回（理由见上面 docstring）。
            # 多查这一次 get_state 只发生在 RETRY 上，而 RETRY 本身就少见
            snapshot = app.get_state(config)

            # 输入   snapshot.next，例 ('run_harvest',)
            # 输出   back，例 ("await_go", "上一次采集没跑完就断了（…），…")；能直接重跑是 None
            back = graph.unfinished_retry(snapshot)
            if back:
                node, note = back
                logger.warning(f"{where} {graph.name} 存档停在 {snapshot.next}，不许直接重跑，退回 {node}")
                return self._back_to(graph, app, config, node, note)

            crashed = _node_crashed_after_reply(snapshot)
            if crashed:
                logger.warning(f"{where} {crashed} 收到回复之后崩溃过，退回它的 interrupt")
                return self._back_to(
                    graph,
                    app,
                    config,
                    crashed,
                    "上一次你那句回复没处理完就出错了，那句不算数。请对着下面这条再回一次。",
                )
            graph_input = None

        try:
            # START / RESUME / RETRY 三种都走这里，差别只在 graph_input
            result = app.invoke(graph_input, config)
        except Exception as err:
            # 这一轮里任何一个节点抛了异常都走这里，不分是哪种 action
            logger.exception(f"{where} invoke 出错")
            return self._recover(graph, app, config, err)

        return graph.render(result)

    def _back_to(self, graph: ConversationGraph, app: Any, config: dict, node: str, note: str) -> dict:
        """把对话退回到 node 那个 interrupt 上，重新发一遍方案 / 提问，返回要发的那条消息。

        输入：
            graph    这个对话归哪张图（要用它的 before_interrupt 和 render）
            app      这一轮编好的图
            config   这一轮的 config（里面是 thread_id）
            node     退回哪个 interrupt 节点，query 是 "await_go" 或 "ask_clarification"
            note     放在最上面的那句说明，例「没跑成（DiscoveryBusy: …），一分钱没花。…」
        输出（query 的两种）：
            退回 await_go            {"text": note, "attachments": [方案卡片]}
            退回 ask_clarification   {"text": note + "\n\n你想找做哪一类设备的公司？"}

        谁在调它：_recover 情况一、二（当场崩溃），_invoke 的 RETRY（崩溃留到了下一句）。

        一共两步：
            第 1 步  app.update_state(config, None, as_node=graph.before_interrupt[node])
            第 2 步  app.invoke(None, config)

        用 query 那张图的三个实际例子，看库里每一步变成什么样。
        例一：图停在 await_go 等拍板，用户回「跑」，run_harvest 抛 DiscoveryBusy。

        崩溃之后，库里最新那份存档（step 5，classify_reply 跑完时落的）：
            checkpoint_id      ...a1025a
            next               ('run_harvest',)
            interrupts         ()
            checkpoint_writes  __error__   RuntimeError("DiscoveryBusy")
            state              messages 最后一句「用户: 跑」，decision="run"，plan 是 v1，
                               run_id=None

        第 1 步 update_state(config, None, as_node="plan_queries")：库里多落一份新存档（新落了一个step）
            checkpoint_id      ...5fb42e（新的），parent 指向 ...a1025a
            metadata.source    "update"（正常跑出来的存档是 "loop"）
            next               ('await_go',)
            interrupts         ()
            checkpoint_writes  空
            state              跟 ...a1025a 一模一样
        三个参数：
            config    改哪个 thread；只给 thread_id，就是接在它最新那份后面
            None      state 一个字段都不写
            as_node   「假装 plan_queries 刚跑完」。LangGraph 按 plan_queries 身上的边
                      点亮下一个节点，也就是 await_go。plan_queries 本身不会重跑

        旧值哪些保留、哪些没保留：
            保留     state 的每个字段原样：messages（含「用户: 跑」）、plan v1、
                     decision="run"、run_id=None
                     旧存档 ...a1025a 本身，连同它下面那笔 __error__，都还在历史里，没删
            没保留   旧存档下面的 checkpoint_writes（这里是 __error__，例二里还有
                     __resume__「跑」）：writes 挂在某一个 checkpoint_id 下面，新存档换了 id，
                     下面一笔都没有。退回能丢掉旧回复，靠的就是这一点
            变了     next：('run_harvest',) -> ('await_go',)

        第 2 步 invoke(None)：不落新存档，还是第 1 步新落的那份 ...5fb42e
            checkpoint_writes  多一笔 __interrupt__（方案 v1）
            interrupts         (Interrupt(value={"kind": "go", ...}),)
            -> 就是正常等拍板的样子，conversation_status 判 WAITING
        await_go 第一行就是 interrupt，这次找不到回复，只能停住，交出 state["plan"]，还是 v1。
        decision="run" 留着没关系：用户下一句进来，classify_reply 会重判覆盖它。

        例二：图停在 await_go 等拍板，用户回「跑」，await_go 拿到「跑」之后、return 之前
        在 print 那一行崩溃（比如往 GBK 编码的输出里写了 emoji）。

        崩溃之后，库里最新那份存档还是 await_go 停下来等拍板时落的那份，
        崩溃没有落新存档，只在它下面多了几笔 writes：
            checkpoint_id      ...6b69d5
            next               ()（注意是空的，不是 ('await_go',)）
            interrupts         (Interrupt(value={"kind": "go", ...}),)，还挂着
            checkpoint_writes  __interrupt__   方案 v1
                               __resume__      「跑」（库里是两笔，一笔全局、一笔挂在 await_go
                                               这个任务上，值都是「跑」）
                               __error__       RuntimeError(...)
            state              messages 最后一句是「AI: 方案 v1，…」，没有「用户: 跑」
                               （await_go 没 return，一笔写都没落），decision=""，plan 是 v1，
                               run_id=None

        第 1 步 update_state(config, None, as_node="plan_queries")：库里多落一份新存档
            checkpoint_id      ...b5e214（新的），parent 指向 ...6b69d5
            metadata.source    "update"
            next               ('await_go',)
            interrupts         ()
            checkpoint_writes  空
            state              跟 ...6b69d5 一模一样
        三个参数：
            config    改哪个 thread；只给 thread_id，就是接在它最新那份后面
            None      state 一个字段都不写
            as_node   「假装 plan_queries 刚跑完」。LangGraph 按 plan_queries 身上的边
                      点亮下一个节点，也就是 await_go。plan_queries 本身不会重跑

        旧值哪些保留、哪些没保留：
            保留     state 的每个字段原样：messages（仍然没有「用户: 跑」）、plan v1、
                     decision=""、run_id=None
                     旧存档 ...6b69d5 本身，连同它下面那几笔 writes，都还在历史里，没删
            没保留   旧存档下面的 __resume__「跑」、__interrupt__、__error__。
                     这次要丢的就是 __resume__「跑」：它不跟过来，这句「跑」就作废了
            变了     next：() -> ('await_go',)

        第 2 步 invoke(None)：不落新存档，还是第 1 步新落的那份 ...b5e214
            checkpoint_writes  多一笔 __interrupt__（方案 v1）
            interrupts         (Interrupt(value={"kind": "go", ...}),)
            -> 就是正常等拍板的样子，conversation_status 判 WAITING
        await_go 第一行就是 interrupt，新存档下面没有 __resume__，它只能停住，交出 plan v1。
        用户要采集，得对着这张方案再说一次「跑」。

        例三：图停在 ask_clarification，问的是「你想找做哪一类设备的公司？」，用户回「音频」，
        ask_clarification 拿到「音频」之后、return 之前崩溃。

        崩溃之后，库里最新那份存档还是 ask_clarification 停下来提问时落的那份（step 1）：
            checkpoint_id      ...3bb781
            next               ()
            interrupts         (Interrupt(value={"kind": "clarify", ...}),)，还挂着
            checkpoint_writes  __interrupt__   那句提问
                               __resume__      「音频」（两笔，同例二）
                               __error__       RuntimeError(...)
            state              messages 只有「用户: 帮我找骨传导耳机」，没有「AI: 你想找…」
                               「用户: 音频」；intent_result 里 topics=[]、
                               clarification_question="你想找做哪一类设备的公司？"；
                               clarify_rounds=0；plan、knowledge 都还是空的

        第 1 步 update_state(config, None, as_node="understand_intent")：库里多落一份新存档
            checkpoint_id      ...16e797（新的），parent 指向 ...3bb781
            metadata.source    "update"
            next               ('ask_clarification',)
            interrupts         ()
            checkpoint_writes  空
            state              跟 step 1 一模一样
        三个参数跟例一相同，只有 as_node 不一样：
            config    改哪个 thread；只给 thread_id，就是接在它最新那份后面
            None      state 一个字段都不写
            as_node   「假装 understand_intent 刚跑完」。它后面是条件边，于是 route_intent
                      被重新调一次。route_intent 只看 intent_result 和 clarify_rounds：
                      topics 还是 []、clarify_rounds 还是 0，判出来还是 clarify，
                      点亮的还是 ask_clarification。understand_intent 本身不会重跑

        旧值哪些保留、哪些没保留：
            保留     state 的每个字段原样：intent_result（topics=[] 和那句提问）、
                     clarify_rounds=0、messages 只有第一句
                     旧存档 ...3bb781 本身，连同它下面那几笔 writes，都还在历史里，没删
            没保留   旧存档下面的 __resume__「音频」、__interrupt__、__error__：
                     「音频」这句作废
            变了     next：() -> ('ask_clarification',)

        第 2 步 invoke(None)：不落新存档，还是第 1 步新落的那份 ...16e797
            checkpoint_writes  多一笔 __interrupt__（那句提问）
            interrupts         (Interrupt(value={"kind": "clarify", ...}),)
            -> 就是正常等回答的样子，conversation_status 判 WAITING
        ask_clarification 重新问「你想找做哪一类设备的公司？」。用户再回一次，
        messages 里才会记下这一问一答，而且只记一次。

        as_node 为什么填 node 前面那个节点（graph.before_interrupt），不填别的，拿例一的存档说
        （decision="run"，messages 最后一句「用户: 跑」）：
            填 plan_queries     跑完的下一步只有 await_go，next=('await_go',)          对
            填 classify_reply   它后面的条件边读到 decision="run"，next=('run_harvest',)，
                                又去采集
            填 await_go         假装 await_go 跑完了，next=('classify_reply',)，classify_reply
                                读到的最后一句还是「跑」，又判 run

        这两步本身也可能出错（比如 Neon 掐了连接）。异常交给 _run 变成一句「出错了」，
        存档不会乱，拿例一说：
            第 1 步没成                 库里没有新存档，最新那份还是 ...a1025a，
                                        next=('run_harvest',)。下一句 RETRY 又走到这里
            第 1 步成了、第 2 步没成     最新那份是新存档 ...5fb42e，next=('await_go',)，
                                        interrupts=()，判 FAILED。下一句 RETRY 就是
                                        invoke(None)，await_go 停在 interrupt 上
        两种都不会去采集，也不会用上旧的回复。
        """
        # 假装 node 前面那个节点刚跑完，next 变成 node。
        # 图上加了新的 interrupt 节点、before_interrupt 没跟着加的话，这里 KeyError
        app.update_state(config, None, as_node=graph.before_interrupt[node])
        # 跑到 node 的 interrupt 停住，重新交出方案 / 那句提问
        result = app.invoke(None, config)
        card = graph.render(result)
        # 方案卡片只有 attachments，说明直接当 text；提问是一句话，接在说明后面
        text = f"{note}\n\n{card['text']}" if card.get("text") else note
        return {**card, "text": text}

    def _recover(self, graph: ConversationGraph, app: Any, config: dict, err: Exception) -> dict:
        """一轮 invoke 半路抛了异常，看存档停在哪一步，把对话收拾好，返回要发的那条消息。

        输入：
            graph    这个对话归哪张图
            app      这一轮编好的图
            config   这一轮的 config（里面是 thread_id）
            err      抛出来的那个异常，例 DiscoveryBusy("已经有一跑 discovery 在跑，这次没开始")
        输出：发回 thread 的那条消息（chat.postMessage 的参数字典）

        按崩溃在哪、下一句能不能直接重跑，分三种。前两种不能，当场用 _back_to 退回一个
        interrupt 重新问（库里怎么变见 _back_to）；第三种能，交给用户的下一句。

        一、图自己有收拾法的（graph.on_crash 给了 (节点, 说明)）
            query：run_harvest 崩溃（next == ('run_harvest',)），退回 await_go 重发同一版方案。
            原因：run_harvest 是唯一花钱的节点，崩溃之后用户的下一句可能是「算了」。
            像情况三那样直接重跑，就是不经拍板又花一次钱。
            说明里敢不敢说「一分钱没花」看是什么异常，细节写在 query/slack_conversation._harvest_crash

        二、interrupt 节点收到回复之后崩溃（_node_crashed_after_reply 找到节点）
            例：用户回「跑」，await_go 拿到「跑」之后在 print 那一行崩溃。
            存档样子见 _node_crashed_after_reply：「跑」存在 checkpoint_writes 的 __resume__ 里。
            处理：退回这个节点，重发同一版方案 / 同一句提问，告诉用户上一句不算数，再回一次。
            原因：不退回的话，下一句不管说什么（Command(resume="算了") 也好，invoke(None) 也好），
            await_go 拿到的都是存下的「跑」，「算了」被丢掉，照样去采集。
            这一种跟是哪张图无关：只要节点第一行是 interrupt，存档都长这样

        三、别的节点崩溃（query 的 understand_intent / retrieve_knowledge / plan_queries /
            classify_reply / find_contacts / deliver）
            例：understand_intent 调 OpenAI 超时，存档 next=('understand_intent',)，判 FAILED。
            处理：这里只回一句话，不重跑。用户回任意一句 -> RETRY -> _invoke 里 invoke(None)，
            understand_intent 从第一行重跑，那句话不进 messages。
            原因：刚崩溃的原因多半还在，当场重跑大概率再崩一次。这几个节点不花钱、
            也不读用户那一句，等下一句再重跑是安全的。
            deliver 也在这一档：它不花采集的钱，但会往频道发卡片。重跑不会重复发，
            靠的是 push_interactive 先查这一跑推过没有（lead_db.pushed_in_run）。
            find_contacts 也在这一档，而且基本走不到：它自己接住了所有异常（出错就跳过、照样去推卡）。
            真崩在它里面、重跑时也不会重复花钱：查过的公司和人库里都有，重跑时不再查

        进程被杀、_recover 根本没机会跑时，情况一、二的存档会留到下一句，由 _invoke 的
        RETRY 接着处理（见那里）。
        """
        snapshot = app.get_state(config)
        reason = f"{type(err).__name__}: {err}"

        # 情况一：图自己有收拾法。
        # 输入   snapshot.next=('run_harvest',)，err=DiscoveryBusy(...)
        # 输出   back=("await_go", "没跑成（DiscoveryBusy: …），一分钱没花。方案还在，…")；
        #        不归图管是 None
        back = graph.on_crash(snapshot, err)
        if back:
            node, note = back
            return self._back_to(graph, app, config, node, note)

        # 情况二：interrupt 节点收到回复之后崩溃，退回它的 interrupt，重新问一遍
        crashed = _node_crashed_after_reply(snapshot)
        if crashed:
            return self._back_to(
                graph,
                app,
                config,
                crashed,
                f"你刚才那句没处理成（{reason}），不算数。请对着下面这条再回一次。",
            )

        # 情况三：别的节点崩溃，这里不重跑，等用户下一句话来了再从崩溃那一步重跑
        return conversation.text_reply(
            f"出错了（{reason}）。回任意一句话，我从出错的那一步重试，那句话不会被当成新内容。"
        )


# 要跑图的三种处理：只有它们要占住 thread
_GRAPH_ACTIONS = frozenset({conversation.START, conversation.RESUME, conversation.RETRY})

# 只回一句固定的话、不碰图、跟是哪张图无关的两种。第三种 FINISHED 每张图各一句，见 _fixed_reply
_FIXED_TEXTS = {
    conversation.EDITED: conversation.EDITED_TEXT,
    conversation.OTHER_THREAD: conversation.OTHER_THREAD_TEXT,
}


def _fixed_reply(graph: ConversationGraph, action: str) -> dict | None:
    """不跑图的那几种处理各回什么。

    输入 -> 输出：
        IGNORE     -> None
        FINISHED   -> {"text": graph.finished_text}，query 是「这次对话已经结束了。要再找一批，…」
        EDITED     -> {"text": EDITED_TEXT}，OTHER_THREAD 同理
    """
    if action == conversation.IGNORE:
        return None
    if action == conversation.FINISHED:
        return conversation.text_reply(graph.finished_text)
    return conversation.text_reply(_FIXED_TEXTS[action])
