"""写稿那张图：把节点连起来，编译成一个能跑的东西。

这个文件只登记节点和边、然后 compile，不跑图，也不做业务判断。add_node / add_edge / compile
每一步在库里干了什么，讲解在 query/graph.py 和 langgraph学习/graph/graph_1a.txt，这里不重复。
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from triggers.interactive_discovery.drafting.nodes import (
    ask_request,
    await_plan,
    await_write,
    classify_feedback,
    classify_plan_reply,
    plan_drafts,
    record_round,
    retrieve_knowledge,
    write_draft,
)
from triggers.interactive_discovery.drafting.state import DraftingState


def route_after_retrieve(state: DraftingState) -> str:
    """条件边的判断函数：retrieve_knowledge之后，要不要先问用户的需求「给这家写什么」。

    输入：一份 state 的副本
    输出：两个键之一，是 add_conditional_edges 那张表的键，不是节点名

        request=""                                     -> 'ask'     点按钮开的，还不知道要写什么，去问
        request="帮我写封邮件给他们，强调 MOQ 500"     -> 'given'   卡片 thread 里 @bot 开的，
                                                                 那句话就是要写什么
    """
    # 输入   state["request"]，开对话时 initial_state 放进去的
    # 输出   'ask' / 'given'。这一句不写库，也不改 state
    return "given" if state["request"] else "ask"


def route_plan_reply(state: DraftingState) -> str:
    """条件边的判断函数：classify_plan_reply 判完之后走哪条路。

    输入：一份 state 的副本，classify_plan_reply 刚写的 decision 在里面读得到
    输出：三个键之一，就是 reply.DECISIONS 那三个

        decision="revise"    -> 'revise'    回 plan_drafts 重定一版
        decision="approve"   -> 'approve'   照这份写，去 write_draft
        decision="cancel"    -> 'cancel'    去 record_round（之前写过、还在等的稿记成 cancelled），再到 END

    判成哪一种在 classify_plan_reply 里做（那件事要调模型，会花钱的必须住在节点里），这里原样返回
    """
    # 输入   state["decision"]        输出   原样返回
    return state["decision"]


def route_feedback(state: DraftingState) -> str:
    """条件边的判断函数：record_round 之后走哪条路。原样返回 classify_feedback 推出来的 decision。

        'replan'    有一份要改计划、或要加一份 -> plan_drafts（改正文的意见在账本里等着，定完计划一起写）
        'rewrite'   只有改正文的 -> write_draft，不再停下来看计划
        'wait'      没有要写的，还有没表态的 -> await_write 再问一次
        'done'      每份都定稿或不要了 -> END
        'cancel'    不写了 -> END

    五个键就是 feedback.DECISIONS（有测试对着）
    """
    return state["decision"]


def build_graph(checkpointer: BaseCheckpointSaver):
    """把节点连成图并编译，返回一个可以 invoke 的对象。

    今天这张图的全貌（要长成什么样见 drafting/__init__.py）：

        START
          |
          v
        retrieve_knowledge           查库和配置：发给谁（公司名、邮箱、卡片上的人）、写什么（概况、
                                     打分结论、证据全文、facts.json、avoid）。只读，不花钱
          |
          +---- ask ----> ask_request              停 1：「请问你想给 X 写哪些 outreach 信息？」
          |                  |
          +---- given -------+
                             v
        plan_drafts <-------------------------+    调一次模型：读这一轮的话，拆成几份（用户说的），
          |                                   |    再替每份定下没说的写法、卖点、开场（AI 定的）；
          |                                   |    代码把它定成最后的计划（finalize_plan），存进 plan
          v                                   |
        await_plan                            |    停 2：把计划发给用户，等他回话
          |                                   |
          v                                   |
        classify_plan_reply ---- revise ------+    调一次小模型：他那句话是要改、同意、还是不写了
          |                                   |
          +---- cancel ---> record_round      |    不写了：写过的稿记成 cancelled，再到 END
          +---- approve                       |
                  |                           |
                  v                           |
        write_draft  <-------------------+    |    每份要写的各调一次模型 + 代码检查
          |                              |    |
          v                              |    |
        await_write                      |    |    停 3：把稿子发给用户看
          |                              |    |
          v                              |    |
        classify_feedback                |    |    调一次模型：一句话拆到每一份，代码推去向
          |                              |    |
          v                              |    |
        record_round                     |    |    把判决写进 draft_revisions（唯一写库的节点）
          |                              |    |
          +---- rewrite -----------------+    |    只有改正文的
          +---- replan -----------------------+    有一份要改计划或要加一份
          +---- wait ----> await_write             没有要写的，还有没表态的
          +---- done ----> END                     每份都定稿或不要了
          +---- cancel --> END                     不写了

        带字的箭头是条件边，字就是判断函数返回的键（route_after_retrieve / route_plan_reply）；
        不带字的是固定边

    输入：checkpointer，存档器。测试传 InMemorySaver，生产传 PostgresSaver（跟 query 那张图
          用同一套表，存档靠 thread_id 的前缀分开）
    输出：CompiledStateGraph。无状态，每一轮现编一个
    """
    # 输入   DraftingState 的字段表        输出   一张空图纸，字段都已经建好
    builder = StateGraph(DraftingState)

    # 输入   节点名 + nodes.py 里的函数    输出   登记进 builder.nodes，函数一次都没被调用
    builder.add_node("retrieve_knowledge", retrieve_knowledge)
    builder.add_node("ask_request", ask_request)
    builder.add_node("plan_drafts", plan_drafts)
    builder.add_node("await_plan", await_plan)
    builder.add_node("classify_plan_reply", classify_plan_reply)
    builder.add_node("write_draft", write_draft)
    builder.add_node("await_write", await_write)
    builder.add_node("classify_feedback", classify_feedback)
    builder.add_node("record_round", record_round)

    # 固定边：开头一定先取齐这家的东西；第一问问完一定去定计划；定完计划一定停下来给他看；
    # 他回了话一定去判。
    # retrieve_knowledge 必须在 ask_request 前面，而不是合进它：见 retrieve_knowledge 的 docstring
    builder.add_edge(START, "retrieve_knowledge")
    builder.add_edge("ask_request", "plan_drafts")
    builder.add_edge("plan_drafts", "await_plan")
    builder.add_edge("await_plan", "classify_plan_reply")
    # 写完一定停下来给他看；他回了话一定去拆；拆完一定先记库再走（记库不花钱，放在去向前面，哪条路都记）
    builder.add_edge("write_draft", "await_write")
    builder.add_edge("await_write", "classify_feedback")
    builder.add_edge("classify_feedback", "record_round")

    # 条件边一：取齐这家的东西之后，用户已经说了要写什么就不问第一句
    builder.add_conditional_edges(
        "retrieve_knowledge",
        route_after_retrieve,
        {
            "ask": "ask_request",  # 点按钮开的：先问一句
            "given": "plan_drafts",  # @bot 开的：那句话直接拿去读
        },
    )

    # 条件边二：他对着计划回的那句话判完之后。
    # 要改绕回 plan_drafts 而不是只改 plan：他说的「第 2 份写给 3 号」要先被模型读成「2 份里第 2 份的
    # 收件人是 3 号」，再由 finalize_plan 定成地址和说明，跟第一次定计划走的是同一条路
    builder.add_conditional_edges(
        "classify_plan_reply",
        route_plan_reply,
        {
            "revise": "plan_drafts",  # 要改：重定一版，再停下来给他看
            "approve": "write_draft",  # 同意：照这份写
            # 不写了：先记库（之前写过、还在等他表态的稿记成 cancelled；一份都没写过就什么都不写），
            # record_round 看 decision 是 cancel 再到 END
            "cancel": "record_round",
        },
    )

    # 条件边三：看完稿子那句话拆完、记完库之后。越上游越先：有一份要改计划就整轮先回 plan_drafts
    builder.add_conditional_edges(
        "record_round",
        route_feedback,
        {
            "replan": "plan_drafts",
            "rewrite": "write_draft",
            "wait": "await_write",
            "done": END,
            "cancel": END,
        },
    )

    # 输入   checkpointer                  输出   CompiledStateGraph，节点名和边在这一步才校验
    return builder.compile(checkpointer=checkpointer)
