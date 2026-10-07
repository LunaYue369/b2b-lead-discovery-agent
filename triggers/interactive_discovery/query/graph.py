"""interactive discovery 那张图：把节点连起来，编译成一个能跑的东西。

这个文件只做两件事 —— 登记节点和边，然后 compile。它不跑图，也不做任何业务判断：
跑图的是外面的调用方（现在是 scripts/try_interactive.py，以后是 Slack listener），
业务判断在节点里。
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from triggers.interactive_discovery.query.intent import is_ready, load_intent_config
from triggers.interactive_discovery.query.nodes import (
    ask_clarification,
    await_go,
    classify_reply,
    deliver,
    find_contacts,
    plan_queries,
    retrieve_knowledge,
    run_harvest,
    understand_intent,
)
from triggers.interactive_discovery.query.state import InteractiveState


def route_intent(state: InteractiveState) -> str:
    """条件边的判断函数：看 understand_intent 刚读出来的那份 intent，说出该走哪条路。

    跟 route_by_decision 一样，它不是节点：没有 add_node 登记它，自己一笔写都不产生，
    图上也画不出一个方块。它挂在 understand_intent 身上，在那个节点 return 之后、
    这一步收尾之前被调一次。

    输入：
        state   一份 state 的副本，understand_intent 刚写的 intent_result 在里面读得到
                （库把所有 channel 复制一份，只把这个任务这一步的写盖上去）。
                第一个参数上的 InteractiveState 注解不是给人看的，它决定库要不要把
                节点返回值额外并进来一份，写了就只给这份副本，行为唯一。

    输出：
        'ready' / 'give_up' / 'clarify' 三个字符串之一。它们不是节点名，是
        add_conditional_edges 第三个参数那张表的键。

    三个实际值：

        intent_result.intent.topics = ["bone conduction"]，clarify_rounds = 0
            -> 'ready'    够了，去 retrieve_knowledge 查库，然后拟方案
        topics = []，clarify_rounds = 1
            -> 'clarify'  还差品类，去 ask_clarification 再问一句
        topics = []，clarify_rounds = 3（上限也是 3）
            -> 'give_up'  问不下去了，还是往下走

    ready 和 give_up 都通到 retrieve_knowledge，合成一个键也能跑。分成两个是为了让「问够了」
    和「问不动了」各有各的名字：两者后果一样，但后者说明 prompt 或者用户那边出了事。

    注意它在画出来的图上看不见：get_graph() 会把同一对起点终点的两条虚线合成一条，
    只留后登记的那个标签。所以 give_up 这一趟唯一的现场就是下面那句 print，
    真要数它出现过几次，得从日志里数。

    够不够这件事在 intent.is_ready 里算，不在这里算，也不让模型填：这个函数只做
    路由，会变的判据留在一个能单独测的纯函数里（理由见 graph_2a.txt 7.4）。
    """
    # 输入   state["intent_result"]["intent"]，understand_intent 刚写进去的那七个字段
    # 输出   够了就走 ready。这一句不写库，也不改 state
    if is_ready(state["intent_result"]["intent"]):
        return "ready"

    # 输入   配置里那个上限，以及 state["clarify_rounds"]（已经问过几轮）
    #        每次路由读一遍那个十行的 yaml。一天走不了几次，不值得为它加缓存
    rounds = state["clarify_rounds"]
    if rounds >= load_intent_config().max_clarify_rounds:
        # 输出   这一句是 give_up 唯一的现场（画出来的图上看不见它，见 build_graph
        #        第四步那段注释）。带上问过几轮和当时手上的 topics，是因为排查这件事
        #        要问的第一个问题就是「它到底缺的是什么」
        print(
            f"[路由] 澄清已经问满 {rounds} 轮，不再问，带着现有 intent 去拟方案："
            f"topics={state['intent_result']['intent']['topics']}"
        )
        return "give_up"

    return "clarify"


def route_by_decision(state: InteractiveState) -> str:
    """条件边的判断函数：看 decision 这个字段，说出 classify_reply 之后该走哪条路。

    它不是节点：没有 add_node 登记它，它自己一笔写都不产生，图上也画不出一个方块。
    它挂在 classify_reply 身上，在那个节点 return 之后、这一步收尾之前被调一次。

    输入：
        state   一份 state 的副本。库把所有 channel 复制一份，只把 classify_reply
                这一个任务这一步的写盖上去，再把副本交给这里 —— 所以 classify_reply
                刚写的 decision 在这里读得到，而真正的 channel 要等这一步收尾才更新。
                第一个参数上的 InteractiveState 注解是有作用的，不只是给人看：库拿它
                决定「要不要把节点的返回值额外并进来一份」。写了注解就只给这份副本，
                行为唯一；不写的话两份会被合在一起，读到哪个值要看键撞不撞。

    输出：
        'replan' / 'run' / 'cancel' 三个字符串之一。它们不是节点名，是
        add_conditional_edges 第三个参数那张表的键，由那张表换成真正的节点名。
        返回一个表里没有的键会当场 KeyError，所以 classify_reply 的取值和那张表的
        键必须是同一套。
    """
    # 输入   state["decision"]，classify_reply 刚写进去的
    # 输出   原样返回。判成哪一种这件事在 classify_reply 里做，不在这里做 —— 那件事
    #        迟早要换成一次 LLM 调用，而会花钱的东西必须住在节点里（见 graph_1b.txt）
    return state["decision"]


def build_graph(checkpointer: BaseCheckpointSaver):
    """把节点连成图并编译，返回一个可以 invoke 的对象。

    今天这张图的全貌。下面每一条 add_edge / add_conditional_edges 改了，回来把这张
    图一起改 —— 这里是唯一一处能一眼看全的地方，图上那九个方块在别处只出现一次：

        START
          |
          v
        understand_intent ---- clarify ----> ask_clarification   停 1
          |    ^    ^                                |
          |    |    +--------------------------------+   问完绕回来
          |    |
          |    |  replan                  用户要改，绕回来重读需求再重拟
          |    +----------------------------------------+
          |                                             |
          | ready / give_up      两个键，同一个去处      |
          v                                             |
        retrieve_knowledge                              |   不调模型，只查库
          |                                             |
          v                                             |
        plan_queries                                    |   拟词，花钱（模型）
          |                                             |
          v                                             |
        await_go                                        |   停 2
          |                                             |
          v                                             |
        classify_reply ---------------------------------+
          |
          +---- run ----> run_harvest ----> find_contacts ----> deliver ----> END
          |                 花钱（采集）     花钱（找联系方式）   推卡片到频道顶层，不花钱
          |
          +---- cancel ---------------------------------> END

        停 1 / 停 2   两个 interrupt，各写在自己那个节点的第一行
        带字的箭头    条件边，字就是判断函数返回的那个键（route_intent /
                      route_by_decision）；不带字的是固定边
        两个环        上面那个补信息，绕一圈一次 LLM 调用；下面那个改方案，
                      绕一圈三次（understand_intent 重读需求、plan_queries 重拟、
                      classify_reply 再判一次）加一次查库

    输入：
        checkpointer   存档器，BaseCheckpointSaver 的任意一个实现
                       （见 graph_1a.txt 2.1、2.2）。
                       必填：没有它，interrupt 停下来之后那份状态没地方存。
                       测试传 InMemorySaver，生产传 PostgresSaver（见 graph_1a.txt 2.3）。

    输出：
        CompiledStateGraph。无状态，一个进程编译一次、所有对话共用一个。
    """
    # 第一步：拿一张空图纸。详见 graph_1a.txt 2.5
    # 输入   InteractiveState 这个类本身（当字段表用，不是实例）
    # 输出   一个 builder 对象，八个容器都是空的，只有 channels 已经照字段表填好四条
    # 干了什么   照 InteractiveState 的字段表建 channel，此外什么都没干
    builder = StateGraph(InteractiveState)

    # 第二步：登记九个节点（2a 加了开头两个，2b 又加了 retrieve_knowledge，后来加了 deliver，
    # 最后在它前面加了 find_contacts）。
    # 详见 graph_1a.txt 2.6
    # 输入   节点名字（字符串）＋ nodes.py 里那个函数
    # 输出   返回 builder 自己（可以链式调用，我们没用这个写法）
    # 干了什么   给函数定名、从第一个参数的注解猜出 input_schema、把函数包成
    #            RunnableCallable 装进 StateNodeSpec，放进 builder.nodes 那个字典。
    #            函数一次都没有被调用，只是被登记
    builder.add_node("understand_intent", understand_intent)
    builder.add_node("ask_clarification", ask_clarification)
    builder.add_node("retrieve_knowledge", retrieve_knowledge)
    builder.add_node("plan_queries", plan_queries)
    builder.add_node("await_go", await_go)
    builder.add_node("classify_reply", classify_reply)
    builder.add_node("run_harvest", run_harvest)
    builder.add_node("find_contacts", find_contacts)
    builder.add_node("deliver", deliver)

    # 第三步：先连固定的那几条边。详见 graph_1a.txt 2.8
    # 输入   两个字符串：从哪个节点、到哪个节点。START 和 END 就是 "__start__"
    #        和 "__end__" 两个字符串常量，不是特殊对象
    # 输出   返回 builder 自己；真正的产出是 builder.edges 里多出七个二元组
    #        （1a 那三条固定边，2a 那两条：入口改成 understand_intent，以及
    #        ask_clarification 问完之后绕回 understand_intent 那条回头边；
    #        2b 再加一条 retrieve_knowledge 查完无条件去 plan_queries；
    #        后来 run_harvest -> END 拆成 run_harvest -> deliver 和 deliver -> END 两条；
    #        最后 run_harvest -> deliver 中间插进 find_contacts，变成两条）
    # 干了什么   只拦两件事（END 当起点、START 当终点），然后把二元组塞进一个 set。
    #            不检查节点名存不存在，拼错了要等 compile 才炸；边身上挂不住任何
    #            属性，所以「什么条件下走这条边」写不进这里，那是条件边的事
    builder.add_edge(START, "understand_intent")

    # 下面这句左边是 ask_clarification 不是 understand_intent：add_edge 的第一个参数
    # 永远是「从哪个节点出发」，所以这一句连的是问完之后绕回去那条，不是去程。
    #
    # 跟 plan_queries -> await_go 的差别只有一个 —— 出边有没有第二种可能：
    #
    #     plan_queries         拟完一定去 await_go              固定边，它在左边
    #     retrieve_knowledge   查完一定去 plan_queries          固定边，它在左边
    #     ask_clarification    问完一定回 understand_intent     固定边，它在左边
    #     understand_intent    只在缺东西那一种情况下才去问      条件边，登记在第四步
    #                          （另外两种情况去 retrieve_knowledge）那张表的 clarify 那行
    #
    # 所以这个环的两条边被拆在两处登记：回头边在这里，去程在第四步那张表里。代码里
    # 没有一个地方能同时看见它们，上面那张 ASCII 图是唯一看得出这是一个环的地方。
    builder.add_edge("ask_clarification", "understand_intent")
    builder.add_edge("retrieve_knowledge", "plan_queries")
    builder.add_edge("plan_queries", "await_go")
    builder.add_edge("await_go", "classify_reply")
    # 采完一定去找联系方式，找完一定去推卡，推完一定结束，三条都是固定边。
    # deliver 不能省成 run_harvest 里的最后几行：推卡失败的话 run_harvest 整个节点崩溃，
    # 对话会退回 await_go，用户再说一次「跑」就又采一遍。单开一个节点，崩溃了只重跑推卡这一步。
    # find_contacts 单开一个节点是同一个道理：它也花钱、也会出错，不能跟采集绑在一起重跑；
    # 它必须在 deliver 前面，因为 deliver 拼卡片时要从库里读它写进去的联系方式
    builder.add_edge("run_harvest", "find_contacts")
    builder.add_edge("find_contacts", "deliver")
    builder.add_edge("deliver", END)

    # 第四步：建立分支
    # 2a 那条澄清分支。跟下面那条的区别只有一个：那条读的是模型判出来的 decision
    # （重算不出来，所以占一个 state 字段），这条读的是现算的 is_ready 加一个计数器。
    #
    # 三个键两个去处是故意的：ready 和 give_up 后果一样（都去拟方案），分开是为了这两种
    # 情况在代码和这张表里各有各的名字 —— 后者说明 prompt 或者用户那边出了事。
    #
    # 一个反直觉的地方：画出来的图上看不出这件事。get_graph() 把「同一对起点终点」的
    # 两条虚线合成一条，只留下后登记的那个标签（实测留下的是 give_up），
    # 所以 show_graph 画出来的 understand_intent 只有两条虚线，而且 ready 那条不见了。
    # 真相在下面这张表里，也在 route_intent 那个 print 里，不在图上。
    #
    # 这张表加上上面那条回头边，图上就有了两个环：这个环补信息（每圈一次 LLM），
    # classify_reply 那个环改方案（每圈两次）。
    builder.add_conditional_edges(
        "understand_intent",
        route_intent,
        {
            "ready": "retrieve_knowledge",  # 够了：先查库，再拟方案
            "give_up": "retrieve_knowledge",  # 问到上限了：带着不完整的 intent 也往下走
            "clarify": "ask_clarification",  # 还差东西：去问一句，问完绕回来
        },
    )

    # 三条分支和那个环。详见 graph_1b.txt
    # 输入   三样：从哪个节点分叉、判断函数、一张「判断函数返回什么 -> 去哪个节点」的表
    # 输出   返回 builder 自己；真正的产出是 builder.branches 里多出一条登记
    # 干了什么   登记这一下什么都不算，判断函数一次都没被调用。编译时这条分支变成挂在
    #            classify_reply 身上的第二个写手，排在「把 return 摊平成写」那个写手
    #            后面；跑的时候它先调判断函数拿到一个键，查这张表换成节点名，再往
    #            branch:to:<那个节点> 写一笔。固定边写哪一格编译时就定死了，条件边
    #            要跑起来才知道写哪一格 —— 除此之外两者写的是同一种东西。
    #            判断函数返回 cancel 那一次一笔都不写：没有任何边信号被点亮，下一步
    #            排不出任务，图就自己结束了，不需要真有一条边通到 END。
    #            这张表还有一个用处：show_graph 画出来的虚线上的字就是它的键。
    builder.add_conditional_edges(
        "classify_reply",
        route_by_decision,
        {
            # 改：绕回 understand_intent，不是直接回 plan_queries。
            # 理由是那句修改得有人读得到 —— 他说「别带 Kickstarter」要先变成
            # intent 里的 exclusions，才会被 check_query 硬拦、才会出现在下一版
            # 的 assumptions 上。直接回 plan_queries 的话 intent_result 原封不动，
            # 那句话只剩 planner 那次调用自己看着办，而 assumptions 上还写着旧的。
            # 代价是多一次 understand_intent（gpt-4o-mini，几百 token）和一次查库。
            # 不必担心绕多了撞上 recursion_limit：这个环每圈都经过 await_go 那个
            # interrupt，而每次 invoke 的步数额度是从当前 checkpoint 重新起算的
            # （实测 limit=3 的图连续恢复八轮，步数走到 17 也没炸）。
            "replan": "understand_intent",
            "run": "run_harvest",  # 跑：三条路里唯一花钱的那条
            "cancel": END,  # 取消：这一趟到此为止
        },
    )

    # 第五步：把图纸翻译成能跑的对象。详见 graph_1a.txt 2.9
    # 输入   checkpointer 这一个关键字参数。它是可选的，不传也能编译出来，但那样
    #        interrupt 停下来之后没地方存，恢复就无从谈起
    # 输出   CompiledStateGraph。无状态，整个进程共用一个；存档器挂在它身上
    # 干了什么   照 builder 那几个容器现造零件：state 八条 channel＋输入槽＋任务槽＋
    #            每个节点一个边信号、十个 PregelNode（比登记的九个多一个 __start__）、
    #            每条边拆成「订阅哪个边信号」和「写手写哪个边信号」两半。也是在这一步
    #            才校验节点名和边对得上，第三步第四步里拼错的名字都要等到这儿才炸
    return builder.compile(checkpointer=checkpointer)
