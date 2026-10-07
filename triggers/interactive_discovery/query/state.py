"""interactive discovery 那张图的 state —— 图跑的时候手上拿着的全部东西。

字段的实际值、以及多条对话怎么分开，在下面 InteractiveState 那个类的 docstring 里。
开新对话时第一次 invoke 交给图的那一份，由文件末尾的 initial_state 生成。
字段为什么长这样、怎么合并、最后存进哪张表，在 langgraph学习/ 那个文件夹：

    langgraph学习/目录.txt              六份材料各讲什么
    langgraph学习/state/state_1a.txt    1a 那四个字段：是什么、怎么合并、存进哪张表
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class InteractiveState(TypedDict):
    """一条对话跑到中间的时候，这份 state 长什么样。

    下面这一份是同一条 Slack thread 里发生过这些事之后的样子：用户开口 -> 被追问过
    一次 -> 方案拟到第二版 -> 他刚说完「跑」，采集还没开始。

        {
            "messages": [
                "用户: 帮我找点公司",
                "AI: 你想找做哪一类设备的公司？",
                "用户: 骨传导耳机，最好是刚做完众筹的",
                "AI: 方案 v1，1 条 query，预计 $0.15。我把「初创」理解成 A 轮以前，没限国别",
                "用户: 别带 Kickstarter",
                "AI: 方案 v2，1 条 query，预计 $0.15。这次去掉了 Kickstarter",
                "用户: 跑",
            ],
            "intent_result": {
                "intent": {
                    "goal": "找刚做完众筹、正需要代工厂的骨传导耳机团队",
                    "topics": ["bone conduction", "bone conduction headphone"],
                    "events": ["刚做完众筹"],
                    "exclusions": ["Kickstarter"],
                    "time_said": "",
                    "max_spend_usd": 0.0,
                    "unsupported": [],
                },
                "clarification_question": "要不要把颈挂式的蓝牙耳机也算进来？",
            },
            "clarify_rounds": 1,
            "plan": {
                "version": 2,
                "queries": [{"text": "bone conduction AND (headphone OR earbuds)",
                             "filters": {}}],
                "assumptions": "这次去掉了 Kickstarter；没限国别",
                "estimated_cost_usd": 0.15,
            },
            "plan_feedback": "",
            "decision": "run",
            "run_id": None,
        }

    第一次 invoke 传进去的那份长这样，除了 messages 全是空的：

        {"messages": ["用户: 帮我找点公司"], "intent_result": {}, "clarify_rounds": 0,
         "knowledge": {}, "plan": {}, "plan_feedback": "", "decision": "", "run_id": None}

    上面那一整份，是「一条 Slack thread 的一份」。多个人一起用的时候是这样：

        thread_id = "slack:C123:1757900000.001"   Riley 在问骨传导   -> 上面那一份
        thread_id = "slack:C123:1757900111.002"   Morgan 在问会议麦  -> 另一份，完全独立

    分开的单位是 thread_id 不是人。state 按 thread_id 存进 checkpointer 那几张表，
    两条 thread 之间读不到对方任何一个字段；编译出来的那张图本身没有状态，整个进程
    共用一个（graph.build_graph）。

    所以同一条 thread 里两个人都说话时，两个人共用的是同一份 state：

        "messages": ["用户: 帮我找点公司",              <- Riley 说的
                     "AI: 你想找做哪一类设备的公司？",
                     "用户: 骨传导耳机",                <- Morgan 说的，前缀一模一样

    待定：前缀现在只有「用户:」和「AI:」两种，谁说的这件事没有存下来。接 Slack 那一步
    要换成真实说话人，连着一件更实际的事一起定 —— 这一跑采回来的线索，反馈算谁的
    （lead_feedback 那张表有 user_id）。

    图这一层管不着、但跟「多个人一起用」有关的还有三件：同一条 thread 同时被 invoke
    两次会撞；两条 thread 同时说「跑」由 advisory lock 挡（persistence.discovery_db）；
    月度花费的硬上限必须跨 thread 累计，不能一条 thread 算一份。
    """

    # 这条对话里说过的每一句原话，双方都存，只增不改。前缀「用户:」/「AI:」就是说话人。
    #
    # 值     ["用户: 帮我找找骨传导耳机的初创",
    #         "AI: 方案 v1，1 条 query，预计 $0.15。我把「初创」理解成 A 轮以前，没限国别"]
    # 合并   Annotated 第二格放的是 reducer。operator.add 就是加号本身：
    #        operator.add(["用户: 你好"], ["AI: 方案 v1"]) == ["用户: 你好", "AI: 方案 v1"]
    #        所以节点里要返回「只装新那一句的列表」：{"messages": ["用户: 别带 Kickstarter"]}
    #        写成「整段历史加新那句」是错的，结果会变成 历史 + 历史 + 新那句
    # 存     容器是 list，非基本类型，每一版整份单独一行进 checkpoint_blobs（state_1a.txt 第四节）
    #
    # 没用更复杂的结构（例如 LangChain 的 BaseMessage）是因为这一层只要能读回原话，
    # 不需要角色之外的东西；真需要时再换，换的时候只有这一个字段受影响。
    messages: Annotated[list[str], operator.add]

    # understand_intent 那次 LLM 调用读出来的整份结果，每轮整份覆盖。
    # 两层：intent 那七个字段是累积的需求，clarification_question 是这一轮的动作。
    # 七个字段各是什么、为什么是这七个，在 intent.py 的 DiscoveryIntent 那段注释里。
    #
    # 值     {"intent": {"goal": "找做骨传导耳机的早期团队",
    #                    "topics": ["bone conduction", "bone conduction headphone"], "events": [],
    #                    "exclusions": [], "time_said": "", "max_spend_usd": 0.0,
    #                    "unsupported": []},
    #         "clarification_question": "要不要把颈挂式的蓝牙耳机也算进来？"}
    #        空字典 {} 表示还没读过（第一次 invoke 的初值）
    # 合并   LastValue，整份覆盖 —— 上一轮那份不留。要回看用户当时看到的是什么，
    #        去 messages 里那几句原话
    # 存     容器是 dict，非基本类型，每一版整份单独一行进 checkpoint_blobs
    #
    # 存 dict 不存 IntentResult 那个对象，理由有两条：跟 plan 一致（读的人不用
    # import 任何类），以及 checkpoint 要在库里躺很久 —— 存对象的话，哪天 schema
    # 改一个字段，旧那条 thread 的快照反序列化出来就是「按新类解释的旧数据」，
    # 缺字段那一格怎么表现要看序列化器的脾气。dict 永远只是一份 jsonb。
    #
    # 这里没有 ready 这一格：够不够是纯函数，谁要谁算（intent.is_ready）。
    # 跟下面 decision 必须占一格正好相反，分界是「重算得出来吗」。
    intent_result: dict[str, Any]

    # 这条对话已经追问过几轮。只增不减。
    #
    # 值     0 = 一次都没问过；2 = 问过两轮（messages 里就有两问两答）
    # 合并   Annotated 第二格是 reducer，operator.add 就是加号本身：
    #        operator.add(1, 1) == 2。所以 ask_clarification 每次返回的是
    #        {"clarify_rounds": 1}（这一轮加一），不是「当前总数加一」
    # 存     int 是基本类型，留在 checkpoints 表的 jsonb 里
    #
    # 它存在的唯一理由是给 route_intent 当刹车：模型一直判「还差东西」的话，图会在
    # understand_intent 和 ask_clarification 之间无限绕。超过配置里那个上限就不再问，
    # 带着手上这份不完整的 intent 去拟方案（config/interactive.yaml 的
    # max_clarify_rounds，以及 graph_2a.txt 6.4）。
    #
    # 它不会被重复加。ask_clarification 挂起那一趟根本没走到 return，恢复之后整个
    # 节点从第一行重跑，但 return 只发生一次（回放语义见 nodes_1a.txt 2.4）。
    clarify_rounds: Annotated[int, operator.add]

    # retrieve_knowledge 从库里端上来的那份东西，整份覆盖。三个键：
    #
    #     avoid              别用哪种词。手写在 config/query_knowledge.yaml，
    #                        跟这次问什么无关，整份带
    #     known_queries      库里现有的每条 query，连它的表现和它历来带回过谁
    #     known_lead_total   那几条词加起来一共多少家不同的公司（去重之后）
    #
    # 值     {"avoid": [{"key": "supplier-seo-floods-manufacturer-queries", ...}],
    #         "known_queries": [{"id": 30, "text": "Kickstarter AND (microphone OR audio)",
    #                            "executions": 1, "novel_leads": 23,
    #                            "leads": {"total": 23, "excluded": 13, "in_cooldown": 5,
    #                                      "examples": [两家]}}],
    #         "known_lead_total": 31}
    #        空字典 {} 表示还没查过（第一次 invoke 的初值）
    # 合并   LastValue，整份覆盖
    # 存     容器是 dict，非基本类型，每一版整份单独一行进 checkpoint_blobs
    #
    # 它为什么要占一格，而不是查完直接交给 plan_queries：跨节点传值只有 state 这一条
    # 路，retrieve_knowledge 和 plan_queries 是两个节点。
    # （2c 之前这里写的理由是「改那条边绕回 plan_queries、不经过 retrieve_knowledge，
    # 不存的话第二版就没知识了」。那条边后来改成绕回 understand_intent 了，所以每一版
    # 方案都会重查一次库 —— 理由变了，字段还是得留。）
    #
    # 跟 intent_result 分开而不是合并进去：那一份是用户说了什么（读自对话，每轮被
    # understand_intent 整份覆盖），这一份是库里有什么（读自库）。来源不同、生命
    # 周期不同的东西不许住一个字段 —— 合并的话知识每轮会被冲掉，而且 intent 那一份
    # 以后要整份落进 queries.filters 那个 jsonb，库里的统计数跟着落进去是脏的。
    knowledge: dict[str, Any]

    # planner 拟的那一版方案，整份覆盖。四个键：
    #
    #     version              第几版。用户说「改」一次就 +1，用来一眼看出「回到
    #                          plan_queries 重拟」这条路真的走了
    #     queries              打算真去搜的那几条。一条是 {'text': ..., 'filters': {...}}：
    #                          text 是 LinkedIn 搜索框里那句话，filters 是这一条自己的
    #                          采集旋钮。旋钮按条带而不是整跑一套，因为它参与 query 的
    #                          身份（0009 的唯一键含 filters）—— 同一句词配不同旋钮
    #                          采回来的是两批帖子，在库里就是两条 query
    #     assumptions          代码拼出来的那份清单：这次真的要搜什么、真的会避开什么、
    #                          哪几条被丢掉了为什么。它逐字可信，也是卡片上给用户看的
    #                          全部内容（state_1a.txt 第六节）
    #     estimated_cost_usd   报价，花钱之前先说清楚
    #
    # 这四样里没有一句是模型写的自由文字。为什么不让模型来写、也不让它润色，
    # 写在 planner.build_assumptions 的 docstring 最后一节。
    #
    # 值     {"version": 2,
    #         "queries": [{"text": "bone conduction AND headphone", "filters": {}}],
    #         "assumptions": 一段话，每行一件事（"我理解的需求：…" /
    #                        "要搜的词：bone conduction" / "拟词时避开：Kickstarter"），
    #                        行与行之间是换行符，
    #                        卡片上按行渲染
    #         "estimated_cost_usd": 0.08}
    # 合并   LastValue，整份覆盖 —— 上一版方案不留，要回看用户当时看到的是什么，
    #        去 messages 里那句 AI 的话
    # 存     容器是 dict，非基本类型，整份进 checkpoint_blobs
    plan: dict[str, Any]

    # 用户看完一版方案之后回的那一句原话，整份覆盖。空串表示「这一轮没有方案反馈」。
    #
    # 值     '别带众筹的' / '第二条不要了' / ''（还没走到拍板那一步，或者上一句不是要改）
    # 合并   LastValue，后写的盖掉先写的
    # 存     str 是基本类型，留在 checkpoints 表的 jsonb 里
    #
    # 它跟 messages 最后那一句是同一句话 —— 但只在「他刚看完一版方案」那一种情况下。
    # 走到 understand_intent 的最后一句有三种，在 messages 里长得一模一样：
    #
    #     用户: 帮我找找骨传导耳机的初创     第一次开口。这是需求，不是对方案的意见
    #     用户: 骨传导耳机                   刚答完一句澄清。这是补需求，也不是意见
    #     用户: 第二条不要了                 看完方案回的话。只有这一种是 feedback
    #
    # 所以「最后一句就是 feedback」这条规律不成立，靠 prompt 说一句也说不清 ——
    # 三种都以「用户: 」开头，模型没有别的线索去分。
    #
    # 这一格装的不只是那句话，还装着「这一句是对着一版方案说的」这件事：空串和非空
    # 就是上面前两种和第三种的分界。understand_intent 和 plan_queries 都靠它分岔，
    # 图里也没有别的办法知道这一趟是不是从 classify_reply 绕回来的。
    #
    # 还有一层：messages 存的是渲染过的一整行（'用户: 别带众筹的'），这一格存的是
    # interrupt 收到的那个原值（'别带众筹的'）。从前者推回后者要剥掉我们自己加的
    # 前缀，而那个前缀迟早要换成真实说话人（见本文件开头那条待定）—— 推的那份代码
    # 到时候会跟着错，而且不报错。
    #
    # 这一格干的另一件事：把 messages 那一长串历史里最新的一句指出来。
    # messages 绕三轮之后长这样：
    #
    #     用户: 帮我找找骨传导耳机的初创
    #     AI: 方案 v1，2 条 query，预计 $0.16。我理解的需求：… 要搜的词：… 做不到的：…
    #     用户: 别带众筹的
    #     AI: 方案 v2，1 条 query，预计 $0.08。我理解的需求：… 要搜的词：… 做不到的：…
    #     用户: 第二条不要了          <- 只有这一句是新的
    #
    # 不指出来的话，模型得自己从这一堆里认出最后那一句。而那件事越绕越难：每出一版
    # 方案就往里写一整份清单（那几行是我们说的，不是他说的），绕三轮之后我们写进去
    # 的字数比他说的多好几倍。
    #
    # 写它的是 await_go（interrupt 收到的那个值，原样，不剥前缀也不解析）。
    # 清它的是 classify_reply：判成 run 或 cancel 的时候写空串 —— 那两句话不是
    # 「要改方案」，留着的话它会跟着 checkpoint 一直躺在那儿，等这条 thread 下次
    # 被唤醒时冒充成一条反馈。两个节点前后脚，管的是同一轮握手的两半。
    #
    # 读它的有三处：classify_reply（要判的就是这一句）、understand_intent（这一轮的
    # 需求该往哪改）、plan_queries（这一版方案该往哪改）。
    # 它非空还顺带说明「这一趟是从 classify_reply 绕回来的，而且他给了修改意见」——
    # 图里没有「我是从哪个节点来的」这种东西，这是唯一能现算出来的替代品。
    # 反过来不成立：绕回来但这一格是空的也有一种，就是他对着一版空方案说「跑」、
    # 被那道闸改判成 replan 那次 —— 那句「跑」不是修改意见，所以被一起清掉了。
    plan_feedback: str

    # 用户对着方案回的那句话被判成哪一种，每轮重判、整份覆盖。
    #
    # 值     'replan' 绕回 understand_intent，把他这句话吸收进需求再重拟一版 /
    #        'run' 往下走 run_harvest / 'cancel' 直接到 END。
    #        空串表示还没判过（第一次 invoke 的初值）。
    #        这三个取值定义在 reply.DECISIONS，graph 里那张表的键是同一套
    # 合并   LastValue，后写的盖掉先写的
    # 存     str 是基本类型，留在 checkpoints 表的 jsonb 里
    #
    # 为什么非得占一个 state 字段，而不是让 classify_reply 把结论直接交给条件边：
    # 条件边的判断函数收到的是一份 state，不是上一个节点的返回值。classify_reply
    # return 之后、这一步收尾之前，库把所有 channel 复制一份、只把这个任务这一步的
    # 写盖上去，然后把这份副本交给判断函数。
    # 它也是这条对话里唯一一个「只给图自己看」的字段。
    decision: str

    # 这次对话有没有真的采过一轮。
    #
    # 值     None = 还没花过钱；有值 = runs 表里那一行的 id，例如 31
    # 合并   LastValue
    # 存     int / None 都是基本类型，留在 checkpoints 表的 jsonb 里
    #
    # 留它不是为了防「resume 把采集重放一遍」（按回放语义，重放只发生在被中断
    # 的那个节点内部，而 run_harvest 里没有 interrupt，它不会被重放，
    # 见 nodes_1a.txt 2.4）；留它是为了回答另一个问题：用户在同一条 Slack thread 里
    # 第二次说「跑」的时候，我们要能知道上一轮已经采过、采的是哪一跑，才谈得上要不要
    # 再花一次钱。
    run_id: int | None


def initial_state(first_line: str) -> dict:
    """开新对话时交给图的第一份 state。

    输入：去掉 @ 之后的第一句话，例 "帮我找找骨传导耳机的初创"
    输出：
        {"messages": ["用户: 帮我找找骨传导耳机的初创"], "intent_result": {},
         "clarify_rounds": 0, "knowledge": {}, "plan": {}, "plan_feedback": "",
         "decision": "", "run_id": None}

    InteractiveState 的每个字段都要给上：LangGraph 只把已经有值的 channel 交给节点，
    没给的那个在节点里读是 KeyError（state_1a.txt 第三节）。
    tests/test_conversation.py 有一条拿这份字典的键去对 InteractiveState 的字段表。
    """
    return {
        "messages": [f"用户: {first_line}"],
        "intent_result": {},
        "clarify_rounds": 0,
        "knowledge": {},
        "plan": {},
        "plan_feedback": "",
        "decision": "",
        "run_id": None,
    }
