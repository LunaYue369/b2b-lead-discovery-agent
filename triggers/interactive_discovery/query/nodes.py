"""图上每个节点的实现。

节点只做编排：读 state、调下面那几层、返回一个字典。真正的判断逻辑在
query_intelligence / discovery / persistence 里，节点自己不写业务规则 ——
这样「同样的输入给同样的输出」这条性质留在那几层，图只负责顺序和 interrupt。

interrupt / resume 那套机制的讲解不在代码里，在 langgraph学习/ 那个文件夹：

    langgraph学习/目录.txt              六份材料各讲什么
    langgraph学习/nodes/nodes_1a.txt    每个节点干什么，interrupt / resume 的全部语义
    langgraph学习/graph/graph_2a.txt    2a 那两个节点为什么这么拆，以及 intent 的字段

下面注释里写「nodes_1a.txt 2.5」，指的就是那份材料里编号 2.5 那一节。

2a 之后这个文件里有两个 interrupt（await_go 和 ask_clarification），各自写在自己
函数的第一行。tests/test_interrupt_discipline.py 用 ast 扫整个包焊着这条纪律。
"""

from __future__ import annotations

import datetime as dt

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from delivery import send
from models import QueryPool, QuerySource, QuerySpec, SourceType
from persistence import db, discovery_db, lead_db, query_db
from query_intelligence.knowledge import build_knowledge, load_avoid, pick_relevant
from triggers.contacts import find_contacts_for_run
from triggers.digest import InteractivePush, push_interactive
from triggers.harvest import (
    build_llm_client,
    harvest_once,
    load_harvest_configs,
    load_run_config,
)
from triggers.interactive_discovery.budget import check_monthly_cap, load_budget_config
from triggers.interactive_discovery.query.intent import (
    load_intent_config,
    read_intent,
    to_state_dict,
)
from triggers.interactive_discovery.query.messages import window_text
from triggers.interactive_discovery.query.planner import (
    build_assumptions,
    draft_queries,
    estimate_cost_usd,
    load_planner_config,
    screen_queries,
)
from triggers.interactive_discovery.query.reply import load_reply_config, read_decision
from triggers.interactive_discovery.query.state import InteractiveState
from triggers.interactive_discovery.query.time_window import resolve_window


def understand_intent(state: InteractiveState) -> dict:
    """读这条对话说过的每一句，用一次 LLM 读出「他到底想找什么」。

    这个节点只做一件事：reasoning。它不问用户、不停下来，读出来的东西够不够都照常
    return —— 够不够是它后面那条条件边的事（graph.route_intent），要不要问是再后面
    那个节点的事（ask_clarification）。把这三件事拆开是 2a 的整个设计，
    理由在 langgraph学习/graph/graph_2a.txt 第 3 节和第 6 节。

    输入（实际值，用户刚开口那一轮）：

        {"messages": ["用户: 帮我找点公司"],
         "intent_result": {}, "clarify_rounds": 0,
         "plan": {}, "decision": "", "run_id": None}

    输出（实际值，同一轮。topics 是空的，所以这一份随后会被判成「还差东西」）：

        {"intent_result": {
             "intent": {"goal": "找潜在客户公司", "topics": [], "events": [],
                        "exclusions": [], "time_said": "", "max_spend_usd": 0.0,
                        "unsupported": []},
             "clarification_question": "你想找做哪一类设备的公司？"}}

    对照用户答完第二轮之后再进来这一次（messages 多了一问一答，这次才读得到「骨传导」）：

        输入 messages  ["用户: 帮我找点公司",
                        "AI: 你想找做哪一类设备的公司？",
                        "用户: 骨传导耳机，最好是刚做完众筹的"]
        输出          {"intent_result": {
                           "intent": {"goal": "找刚做完众筹的骨传导耳机团队",
                                      "topics": ["bone conduction", "bone conduction headphone"],
                                      "events": ["刚做完众筹"], "exclusions": [],
                                      "time_said": "", "max_spend_usd": 0.0,
                                      "unsupported": []},
                           "clarification_question": "要不要把颈挂式的蓝牙耳机也算进来？"}}

    这一步产生的写，两笔（实测：跑一遍看 checkpoint 里这个 task 名下的行）：

        intent_result                整份覆盖。dict 是容器，所以它这一版会整份单独
                                     一行落进 checkpoint_blobs；checkpoints 那张表
                                     里只留这一步的快照
        branch:to:<下一个节点>       这一笔不是这个函数 return 的，是挂在它身上的
                                     条件边那个写手加的，值是 None ——
                                     它就是「下一步该排谁」那个信号。实测这一笔
                                     长这样：branch:to:ask_clarification = None
        此外                         不碰 messages（这个节点自己不说话），
                                     不连 Neon，我们自己那八张业务表一个字都不写

    它没有 interrupt，所以永远不会被重放（nodes_1a.txt 2.4）—— 那次 LLM 调用一轮
    只付一次钱。这正是方案 B 比方案 A 省的地方：方案 A 把 interrupt 放在同一个节点里，
    三轮澄清会让最早那次调用重跑三遍。

    模型没读出好回答的五种情况（SDK 抛异常、被 max_tokens 截断、拒答、没有内容、
    返回的不是合法 JSON）一律从 read_intent 里抛 IntentCallFailed，这一层不接。
    整次 invoke 就此失败，用户看到的是「出错了」而不是一句问题 —— 这是对的：
    宁可显式失败，也不要编一份 intent 接着往下走，因为再往下就是花钱那一步。
    """
    # 输入   配置：用哪个模型、最多几个 token、超时多少秒
    #        每次调用读一遍那个十行的 yaml。不缓存的理由在 load_intent_config 里
    config = load_intent_config()

    # 输入   一个能 chat 的客户端。跟 analyzer 共用同一把 key、同一个构造函数
    #        在这儿造而不是在 intent.py 里造：那一层收的是「一个能 chat 的对象」，
    #        谁给它不关它的事，这样它在没有密钥的机器上也测得动
    client = build_llm_client()

    # 输入   state["messages"] 是这条对话说过的每一句原话，每轮都整段重新喂；
    #        feedback 是用户看完上一版方案之后回的那一句，从 messages 里拎出来
    #        单独强调一次。它空着就是「这一趟不是来改方案的」（第一次开口，
    #        或者刚答完一句澄清）
    # 输出   IntentResult。这一句是这个节点里唯一花钱的地方
    result = read_intent(
        client,
        state["messages"],
        feedback=state["plan_feedback"],
        config=config,
    )

    print(
        f"[节点] understand_intent 读到 topics={list(result.intent.topics)} "
        f"events={list(result.intent.events)}"
    )

    # 输出   这个函数 return 的只有一笔：intent_result 整份覆盖（摊成 dict 再交出去，
    #        理由见 to_state_dict）。条件边那个写手随后还会在同一个 task 名下加一笔
    #        branch:to:<下一个节点>，那一笔不归这里管
    return {"intent_result": to_state_dict(result)}


def ask_clarification(state: InteractiveState) -> dict:
    """把 understand_intent 已经想好的那句问题交出去，停下来等用户回答。

    这是整张图第二个 interrupt（第一个在 await_go），同样写在函数第一行。
    为什么必须是第一行、停下来和恢复各自发生了什么，在 nodes_1a.txt 第二节。

    这个节点刻意什么都不会做：不判断、不调模型、不查库。它前面站着什么，恢复时就
    重跑什么 —— 而它前面除了一句 interrupt 什么都没有，所以恢复这一趟是白跑的、
    也是免费的。想好问哪一句是 understand_intent 的活，已经存在 state 里了。

    输入（实际值，understand_intent 判下来还差东西那一轮）：

        {"messages": ["用户: 帮我找点公司"],
         "intent_result": {"intent": {"topics": [], ...},
                           "clarification_question": "你想找做哪一类设备的公司？"},
         "clarify_rounds": 0, "plan": {}, "decision": "", "run_id": None}

    交出去的东西（外面 result["__interrupt__"][0].value 里那个字典）：

        {"kind": "clarify", "question": "你想找做哪一类设备的公司？"}

    输出（用户回了「骨传导耳机，最好是刚做完众筹的」之后，这个函数的返回值）：

        {"messages": ["AI: 你想找做哪一类设备的公司？",
                      "用户: 骨传导耳机，最好是刚做完众筹的"],
         "clarify_rounds": 1}

    问和答一起写进 messages，是这个节点存在的另一半理由：下一轮
    understand_intent 读的是 state 里的 messages，用户那句答话只有真进了 messages，
    它才看得见（graph_2a.txt 3.11）。只写答话不写问题也不行 —— 「骨传导耳机」
    这句答话脱离了「你想找哪一类设备」那句问题就没法读。

    这一步产生的写，分两个时刻。下面这些是实测跑一遍、把 checkpoint 里的行印出来
    看到的（idx 那几个数在内存存档器里看不到，它们是 nodes_1a.txt 2.2 在 Postgres
    那边数出来的）：

        挂起那一刻   只有一行：channel=__interrupt__（idx=-3），value 就是上面那个
                     字典。这个节点自己那几笔写一笔都没有 —— 它还没走到 return
        恢复那一趟   先是两行 __resume__（idx=-4）：一行挂在全零的 task_id 下，
                     value 是用户那句原话（外面投进来的那一个），另一行挂在这个节点
                     自己的 task 下，value 是一个 list（问过几轮就有几个答案）。
                     然后节点跑完，三笔一起落：messages 追加两句（list 是容器，
                     进 checkpoint_blobs）、clarify_rounds 加一（int 是基本类型，
                     留在 checkpoints 的 jsonb 里）、branch:to:understand_intent
                     那个边信号（值是 None）。控制写占掉 0 号位，所以这三笔的正数
                     座位是从 1 往后排的
        自始至终     不连 Neon，我们自己那八张业务表一个字都不写

    clarify_rounds 返回的是 1 不是「当前值加一」：那个字段的 reducer 是加号，
    库会自己把 1 加到旧值上。写成 state["clarify_rounds"] + 1 的话，结果会变成
    旧值的两倍加一，而且不报错。
    """
    # 第一行，前面不许有任何东西（理由见 nodes_1a.txt 2.5 第一条）
    # 输入   state["intent_result"] 里那句已经想好的问题，只读，不改
    # 输出   挂起时这一句抛 GraphInterrupt，节点断在这里；恢复时它返回用户那句原话
    #
    # kind 这个键是给外面用的：两个 interrupt 分属两个节点，图自己不会搞混
    # （同一时刻只停在一个节点上，Command(resume=值) 落不错地方），但 Slack 那边
    # 要知道这次该发一句提问还是发一张方案卡片。
    #
    # 键和 kind 的取值都是 ASCII，跟 decision 那几个值同一条纪律：外面要拿它做
    # if，它还会随 interrupt 的值落进 checkpoint_writes 变成历史数据，而这条路上
    # 有 GBK 的终端和日志。卡片上给人看的那句中文由渲染那一层自己写，
    # 不从键名里来
    answer = interrupt(
        {
            "kind": "clarify",
            "question": state["intent_result"]["clarification_question"],
        }
    )

    # 下面这些只有用户回话之后才会执行，每次恢复只执行一次
    #        question 是刚才交出去的那句问题，原样再取一遍写进 messages
    question = state["intent_result"]["clarification_question"]
    print(f"[节点] ask_clarification 问了 {question!r}，收到 {answer!r}")

    # 输出   两笔写：messages 追加一问一答（列表里只装新那两句），clarify_rounds 加一
    return {
        "messages": [f"AI: {question}", f"用户: {answer}"],
        "clarify_rounds": 1,
    }


def retrieve_knowledge(state: InteractiveState) -> dict:
    """拟词之前先看看库里有什么：别用哪种词、已经有哪些词、它们带回过谁。

    输入（实际值，understand_intent 刚判完够了那一轮。这七个字段里只有一处被读到，
    就是 intent_result 里的 topics）：

        {"messages": ["用户: 帮我找找做会议麦克风的初创", "AI: ..."],
         "intent_result": {"intent": {"goal": "找潜在客户公司",
                                      "topics": ["microphone"], "events": [], ...},
                           "clarification_question": ""},
         "clarify_rounds": 0, "knowledge": {}, "plan": {}, "decision": "", "run_id": None}

    输出（一笔写，整份覆盖 knowledge 那个字段）：

        {"knowledge": {
            "avoid": [{"key": "supplier-seo-floods-manufacturer-queries", "scope": "query",
                       "statement": "query 里不要用 manufacturer ...", "source": "三跑实测 ..."},
                      两条],
            "topics_searched": ["microphone"],
            "known_queries": [
              {"id": 30, "text": "Kickstarter AND (microphone OR audio)",
               "matched_topics": [{"topic": "microphone", "matched": "microphone"}],
               "topics": [], "pool": "candidate", "cadence_days": 1,
               "is_active": True, "last_run_at": "2026-09-02",
               "executions": 1, "unique_companies": 23, "novel_leads": 23,
               "qualified_leads": 4,
               "leads": {"total": 23, "excluded": 13, "in_cooldown": 5,
                         "examples": [两家]}}],
            "known_lead_total": 23}}

        topics 是 ["bone conduction"] 那一轮：known_queries 是 []、known_lead_total 是 0，
        只有 avoid 有内容。那是正确答案不是故障 —— 库里确实没有骨传导的经验。

    这一步在库里留下什么：checkpoint 那几张表各一行（knowledge 是 dict，这一版整份单独
    进 checkpoint_blobs；固定边那个写手随后再加一笔 branch:to:plan_queries）。
    我们那八张业务表一个字都不写，它只读，也不拿 advisory lock —— 那把锁防的是两跑同时
    采集。它挂在 route_intent 的 ready 和 give_up 后面，不是无条件跟着 understand_intent：
    澄清每绕一轮就白查一次 Neon，那不是 LLM 的钱，是一秒的冷启动，而用户在等。

    这个节点不调模型：查哪几样是代码写死的，加一次 LLM 让它决定该查什么，等于让它猜
    一件确定的事，而猜错没有任何症状。它也不砍不判断，该不该用是 plan_queries 的事。
    """
    # 输入   avoid 是手写的那三条负面知识：
    #            (AvoidNote(key='supplier-seo-floods-manufacturer-queries', ...),
    #             AvoidNote(key='buying-guide-words-return-seo-articles', ...),
    #             AvoidNote(key='hardware-collides-with-the-other-industry', ...))
    #        它约束的是词的形状不是题目，跟这次问什么无关，所以既不按 topics 筛，
    #        也不用等连上库
    avoid = load_avoid()

    # 输入   topics 是这一轮要照着查的品类词，如 ['microphone'] 或 ['bone conduction']。
    #        give_up 那条路进来时它是 []，那时下面一条 query 都挑不出来，
    #        这份 knowledge 就只剩 avoid
    topics = state["intent_result"]["intent"]["topics"]

    #        三样一次查齐，共用一个连接一个事务：分几次连的话几份数据可能来自几个
    #        时刻，而它们要拼成同一份东西交给模型
    with db.connect() as conn, conn.cursor() as cur:
        #        all_queries 给的是库里现有的全部 query（包括关掉的 —— 对话里新建的
        #        默认就是关着的），pick_relevant 当场按词面挑出沾边的那几条：
        #            all_queries -> [KnownQuery(29, 'looking for a hardware ...'),
        #                            KnownQuery(30, 'Kickstarter AND (microphone ...)'),
        #                            KnownQuery(31, 'wearable AND (microphone ...)')]
        #            relevant    -> [(KnownQuery(30, ...), ('microphone',)),
        #                            (KnownQuery(31, ...), ('microphone',))]
        #        全取是因为这张表的行数上限就是「我们一共写过多少条 query」，几百条
        #        封顶（selector 每天也是全表扫）。挑的那一步放在纯函数里不放 SQL 的
        #        where：「这次为什么给模型看的是这两条词」要能拿一批固定的行重放出来
        relevant = pick_relevant(query_db.all_queries(cur), topics)

        # 输入   totals 是每条 query 跑完的执行累加起来，一条 query 一份：
        #            {29: QueryTotals(executions=2, unique_companies=40, ...),
        #             30: QueryTotals(executions=1, unique_companies=23, ...)}
        #        也是全取。它在库里已经 group by query_id 聚过一次，行数同样以 query
        #        条数封顶，按 id 收窄省不到东西；用不上的那几份在拼装时被 dict.get
        #        直接跳过，而「取不到就当全 0」那个判断本身属于拼装，留在纯函数里
        totals = discovery_db.execution_totals(cur)

        # 输入   hits 是挑中的那几条 query 历来命中过哪些公司，一行一条命中、没去重：
        #            [LeadHit(query_id=30, lead_id=1288, 'Sonora', pushed, 67, True),
        #             LeadHit(query_id=30, lead_id=1301, 'Bonewave', excluded, 34, False),
        #             ...]
        #        只有这一张按 id 收窄，因为只有它是明细表：一条 query 一跑一家公司就是
        #        一行，随着跑的次数和公司数一起长，没有上限。一条都没挑中时传的是空
        #        列表，那个函数直接返回空、不查库
        hits = lead_db.hits_by_query(cur, [query.query_id for query, _ in relevant])

    #        knowledge 就是上面「输出」那一份。拼装在纯函数里做不在这儿做：
    #        那一份能拿固定的行反复回归，「这次为什么给模型看的是这两家」要能不连库
    #        就重放出来
    knowledge = build_knowledge(avoid=avoid, topics=topics, relevant=relevant, totals=totals, hits=hits)

    print(
        f"[节点] retrieve_knowledge 照 {topics} 查到 {len(knowledge['known_queries'])} 条相关 query、"
        f"{knowledge['known_lead_total']} 家已知公司、{len(knowledge['avoid'])} 条要避开的写法"
    )

    # 输出   一笔写：knowledge 整份覆盖。固定边那个写手随后还会加一笔 branch:to:plan_queries
    return {"knowledge": knowledge}


def plan_queries(state: InteractiveState) -> dict:
    """拟一版方案：搜哪几句话、大概花多少钱、我做了哪些猜测。

    一次 LLM 调用加三件确定性的事，顺序不能换：

        1 draft_queries     模型拟词，拿回一段中文思路和几条还没检查过的 query
        2 screen_queries    逐条查（超长、布尔符超额、带 NOT、混中文、撞上说好要
                            避开的词、旋钮不合法），合格的再按上限截断
        3 estimate_cost_usd 按留下来的条数报价
        4 build_assumptions 把这一版替用户做的猜测写成给人看的清单

        报价和清单都要等筛完才算得出来 —— 它们说的是「这次真的会搜什么」。

    输入（实际值，用户第一次开口、understand_intent 和 retrieve_knowledge 都跑完）：

        {"messages": ["用户: 帮我找找骨传导耳机的初创，最好是刚做完众筹的"],
         "intent_result": {"intent": {"goal": "找刚做完众筹的骨传导耳机团队",
                                      "topics": ["bone conduction"],
                                      "events": ["刚做完众筹"], "exclusions": [],
                                      "time_said": "", "max_spend_usd": 0.0,
                                      "unsupported": []},
                           "clarification_question": "要不要把颈挂式的也算进来？"},
         "clarify_rounds": 0,
         "knowledge": {"avoid": [三条], "topics_searched": ["bone conduction"],
                       "known_queries": [], "known_lead_total": 0},
         "plan": {}, "decision": "", "run_id": None}

    输出（实际值，第一次进来）：

        {"plan": {"version": 1,
                  "queries": [{"text": "Kickstarter AND bone conduction",
                               "filters": {"authorKeywords": "Founder OR CTO"}}],
                  "assumptions": 四行，行与行之间是换行符 —— 我理解的需求：
                                 找刚做完众筹的骨传导耳机团队 / 要搜的词：
                                 bone conduction / 要找的时刻：刚做完众筹 /
                                 只看这些人发的帖：Founder OR CTO，
                  "estimated_cost_usd": 0.08},
         "messages": ["AI: 方案 v1，1 条 query，预计 $0.08。我理解的需求：…"]}

    第二次及以上进来长什么样（用户看完 v1 说了「别带众筹的」，图绕回
    understand_intent 重读一遍、再查一次库，然后才到这儿）：

        输入   messages      多了两行：我们那一版方案，和他回的那一句
               intent_result exclusions 多了一条 'Kickstarter'（他那句话是冲着需求
                             去的，所以这一份变了；要是他只说「第二条不要了」，
                             这七个字段会一个都不动）
               knowledge     topics 没变，所以原样再查一次、拿回同一份
               plan_feedback 从空串变成 '别带众筹的'
               plan          上一版还在里面（version=1），这次进来才会被盖掉
               decision      'replan'

        输出   {"plan": {"version": 2,
                         "queries": [{"text": "bone conduction AND (launch OR preorder)",
                                      "filters": {}}],
                         "assumptions": 比上一版多一行「拟词时避开：Kickstarter」，
                         "estimated_cost_usd": 0.08},
                "messages": ["AI: 方案 v2，1 条 query，预计 $0.08。我理解的需求：…"]}

        这一轮拟出来的词跟上一版不一样，靠的是输入真的变了（多了他那句话、
        exclusions 多了一条、previous_plan 从 None 变成 v1）。温度是 0，输入一个字
        没变的话拟出来的会是逐字相同的一版 —— 那正是「改」这条边不绕回这里、
        而是绕回 understand_intent 的原因。

    卡片上给用户看的就是 assumptions 那一份，它是代码拼出来的不是模型写的：
    逐条列出这次真的要搜什么、真的会避开什么、哪几条被丢掉了为什么，逐字可信。
    模型这一步只产出 query，不写任何解释性的文字 —— 理由见
    planner.build_assumptions 的 docstring 最后一节，简短版是「被检查的人不能
    自己誊抄检查表」。

    两个 key 的性质不一样，一起返回但走的是两条路：plan 整份覆盖（判断，可以被
    推翻），messages 追加一行（原话，只增不改）。我们自己说的话也要进 messages 的
    理由：用户那句「别带 Kickstarter」只有对着我们当时给他看的方案才有意义，
    而 plan 下一版就把上一版盖掉了。

    这个节点没有 interrupt，所以它不会被重放（nodes_1a.txt 2.4），追加那一行不会
    重复写。

    version 这个数存在的意义：上面那一轮它变成 2，让「条件边真的绕回来了」在日志和
    state 里看得见 —— 否则它跟「同一个节点被重放了一次」长得一模一样。

    一条都没留下（模型拟的全不合格）不算失败：plan['queries'] 是空列表，照样往下走到
    await_go，卡片第一行会是「一条能用的 query 都没拟出来，换个说法…」。让用户看见
    比当场炸有用 —— 炸了他只知道出错了，看见了他知道该换个说法。真要防的是他对着
    一版空方案说「跑」，那道闸在 classify_reply 里。
    """
    # 输入   intent 那七个字段（planner 只看其中四个）、库里捞出来的知识、对话原话、
    #        这一轮的方案反馈、上一版方案。后三样是「改」这条路读得到用户修改的地方，
    #        其中 plan_feedback 是从对话里拎出来单独强调的那一句
    intent = state["intent_result"]["intent"]
    knowledge = state["knowledge"]
    previous_plan = state["plan"] or None

    #        planner_config 是模型参数加上「最多拟几条」和单价；
    #        max_posts 单独从 discovery.yaml 读 —— 报价要用的是真采的时候用的那个数
    planner_config = load_planner_config()
    run_config = load_run_config()
    max_posts = run_config.max_posts_per_query

    # 输出   draft 是模型说的那一份，还没检查过。这一句花钱（很少）
    draft = draft_queries(
        build_llm_client(),
        intent=intent,
        knowledge=knowledge,
        lines=state["messages"],
        feedback=state["plan_feedback"],
        previous_plan=previous_plan,
        config=planner_config,
    )

    #        kept 是查完又按上限截断之后留下的，dropped 是丢掉的那几条连原因。
    #        exclusions 在这里第二次起作用：understand_intent 记下它，这里硬拦
    kept, dropped = screen_queries(
        draft.queries,
        exclusions=intent["exclusions"],
        max_queries=planner_config.max_queries,
    )

    # 采集窗口：他的原话换算成日期，放进 plan。run_harvest 从 plan 里取，不重算 ——
    # 他看完卡片过几个小时才回「跑」的话，重算出来的日期会跟卡片上那个不一样
    # 输入   intent["time_said"]='最近两个月'，今天 2026-09-28
    # 输出   PostWindow(since='2026-07-28', limit=None, said='最近两个月', understood=True)
    # 输入   intent["time_said"]=''
    # 输出   PostWindow(since=None, limit='month', said='', understood=False)，month 是 discovery.yaml 的默认
    window = resolve_window(
        intent["time_said"], now=dt.datetime.now(dt.UTC), default_limit=run_config.posted_within
    )

    # 输出   plan 这一份，整份覆盖旧的
    plan = {
        "version": (previous_plan or {}).get("version", 0) + 1,
        "queries": kept,
        "assumptions": build_assumptions(intent, kept=kept, dropped=dropped),
        "estimated_cost_usd": estimate_cost_usd(
            len(kept),
            max_posts_per_query=max_posts,
            unit_price_usd=planner_config.unit_price_usd,
        ),
        # 采集窗口四格，意思见 time_window.PostWindow。since 和 limit 恰好有一个不是 None
        "posted_since": window.since,
        "posted_limit": window.limit,
        "time_said": window.said,
        "time_understood": window.understood,
    }

    print(
        f"[节点] plan_queries 拟了 {len(draft.queries)} 条，留下 {len(kept)} 条，"
        f"丢掉 {len(dropped)} 条，报价 ${plan['estimated_cost_usd']}"
    )

    #        told_user 是进 messages 的那一句，内容就是卡片上那一份。
    #        几条 query 本身不往里塞：它们在 plan 字段里已经有了，而 messages
    #        每一版都整份重存。但那份清单要塞 —— 用户下一句「别带 Kickstarter」
    #        只有对着我们当时给他看的那几行才读得懂，而 plan 下一版就把上一版盖掉了
    #        采集窗口也要写进这一句：卡片上它在最上面那一栏，不在 assumptions 里，
    #        不写的话用户下一句「改成一个月」，模型对着的那几行里没有它
    told_user = (
        f"AI: 方案 v{plan['version']}，{len(plan['queries'])} 条 query，"
        f"预计 ${plan['estimated_cost_usd']}，采集窗口 {window_text(plan)}。{plan['assumptions']}"
    )

    # 输出   两笔写：plan 覆盖，messages 追加（列表里只装新那一句）
    return {"plan": plan, "messages": [told_user]}


def await_go(state: InteractiveState) -> dict:
    """把方案和报价交出去，然后停在 interrupt 上，等用户拍板。

    这是整张图唯一一个 interrupt，而且是这个函数的第一行。为什么必须是第一行、
    停下来和恢复各自发生了什么、库里写了哪几行，全在 nodes_1a.txt 第二节，这里不重复。

    输入（实际值）：

        {"messages": ["用户: 帮我找找骨传导耳机的初创"],
         "intent_result": {"intent": {"topics": ["bone conduction", "bone conduction headphone"], ...},
                           "clarification_question": "..."},
         "clarify_rounds": 0,
         "plan": {"version": 1, "queries": [...], "assumptions": "...",
                  "estimated_cost_usd": 0.15},
         "run_id": None}

    交出去的东西（就是外面 result["__interrupt__"][0].value 里那个字典）：

        {"kind": "go",
         "reply_hint": "回一句话",
         "plan": {"version": 1, "queries": [...], ...},
         "estimated_cost_usd": 0.15}

    kind 那个键是 2a 加的。图上有两个 interrupt 之后，外面要能分清这次该发一句
    提问还是发一张方案卡片 —— 分清不是为了防投错（同一时刻只停在一个节点上，
    投不错），是为了知道怎么渲染。另一个取值是 ask_clarification 那边的 clarify。

    输出（用户回了「跑」之后，这个函数的返回值）：

        {"messages": ["用户: 跑"]}

    返回的是只装一句话的列表，不是整段历史 —— messages 带 reducer，LangGraph 会自己
    把它接到后面。

    这个节点为什么不自己算报价：报价是在 plan_queries 里算好、存进 state 的，这里
    只负责读。理由是 nodes_1a.txt 2.5 第一条那笔账 —— 放在 interrupt 前面的每一句
    话，用户每回一次就多跑一遍。
    """
    # 第一行，前面不许有任何东西（理由见 nodes_1a.txt 2.5 第一条）
    # 输入   state["plan"]，只读，不改
    # 输出   挂起时这一句抛 GraphInterrupt，节点断在这里；恢复时它返回用户投进来的值
    reply = interrupt(
        {
            "kind": "go",
            "reply_hint": "回一句话",
            "plan": state["plan"],
            "estimated_cost_usd": state["plan"]["estimated_cost_usd"],
        }
    )

    # 下面这些只有用户回话之后才会执行，每次恢复只执行一次
    #          reply 就是外面 Command(resume=...) 投进来的那个值，我们投的是用户原话
    print(f"[节点] await_go 收到回复: {reply}")

    # 输出   两笔写：messages 追加渲染好的那一行，plan_feedback 存 interrupt 收到的原值。
    #        存两份是刻意的，理由在 state.py 那个字段的注释里。一句话版：光有 messages
    #        分不出最后那一句是「对着方案提的意见」还是「开场白 / 答一句澄清」，
    #        而这三种要走的路不一样。
    #        这里存原值不存渲染过的那一行：前缀是我们加的，而「用户:」这个前缀本身
    #        还是个待定项（同一条 thread 两个人都回话那天要换）
    return {"messages": [f"用户: {reply}"], "plan_feedback": str(reply)}


def classify_reply(state: InteractiveState) -> dict:
    """读用户刚回的那句话，判成改 / 跑 / 取消三选一，写进 decision。

    这个 node 的产出只有一个字段 decision，它唯一的消费者是后面那条条件边：
    边上的判断函数读它，决定下一个跑哪个 node（graph.route_by_decision）。

    三条路里只有 run 会真花钱，而且钱当场就出去。所以这个节点里所有的偏向都朝
    一个方向：拿不准就判 replan。prompt 里说了一次，reply.parse_reply 的闭集挡了
    一次，下面那道「一条 query 都没有就不许跑」是第三次。

    输入（实际值，用户回了「行吧就这样」之后）：

        {"messages": ["用户: 帮我找找骨传导耳机的初创",
                      "AI: 方案 v1，1 条 query，预计 $0.08。这版抓的是刚做完众筹…",
                      "用户: 行吧就这样"],
         "intent_result": {"intent": {"topics": ["bone conduction"], ...}, ...},
         "clarify_rounds": 0,
         "knowledge": {...},
         "plan": {"version": 1, "queries": [{"text": "Kickstarter AND bone conduction",
                                             "filters": {}}], ...},
         "decision": "", "run_id": None}

    输出（实际值，判成 run 那一次）：

        {"decision": "run", "plan_feedback": ""}

    判成 replan 时只有 decision 一个键 —— 那一句是「要我们怎么改」，得留给下一轮的
    understand_intent 和 plan_queries 读。只有一种例外：他对着一版空方案说「跑」、
    被下面那道闸改判成 replan 的时候，plan_feedback 跟着被清掉，因为「跑」不是
    「要我们怎么改」。

    三个取值：replan（回 understand_intent，把他这句话吸收进需求再重拟）/
    run（往下走 run_harvest）/ cancel（直接到 END）。取值那一套定义在
    reply.DECISIONS，跟 graph 里那张表是同一套，有测试对着。

    要判的那一句单独给，整条对话照样全喂：他上一句刚提了一个条件、这一句只回一个
    「好」的时候，那个「好」是在确认那个条件，不是在说整版方案可以跑了。
    模型那次调用返回的 reason 不进 state —— 它是这一轮的东西，没有任何代码读它，
    唯一的消费者是下面那句 print 和排查的人。
    """
    # 输入   要判的那一句（plan_feedback），加上整条对话和他正看着的那一版方案
    #        当上下文。要判哪一句写成一份明写的输入，不靠「取最后一句」那条位置约定 ——
    #        理由见 reply.build_reply_messages
    # 输出   judgement，模型判出来的三选一加一句中文理由。这一句花钱（很少）
    judgement = read_decision(
        build_llm_client(),
        state["messages"],
        state["plan"],
        feedback=state["plan_feedback"],
        config=load_reply_config(),
    )
    decision = judgement.decision

    #        一条能用的 query 都没有的时候不许判 run。这是确定性的事实，所以在代码里
    #        拦不靠模型：让它走下去的话，run_harvest 会开一行 run、拿零条 query 跑完、
    #        然后告诉用户「发现 0 家公司」—— 采集的钱没花，但那一跑和那行 run 是假的，
    #        而且用户会以为是市场上没有，不是我们没搜。
    #        改成 replan 而不是当场炸：这时候该做的事正是重拟一版
    # 输出   这一支两笔写。plan_feedback 也要清掉：他说的那句是「跑」，那不是
    #        「要我们怎么改」。留着的话下一轮 understand_intent 和 plan_queries 会被
    #        明确告知「他刚看完方案，这一轮要照着它改：跑」—— 一句没有意义的指令，
    #        纯噪音，而且不报错
    if decision == "run" and not state["plan"]["queries"]:
        print(
            f"[节点] classify_reply 判成 run，但这一版一条 query 都没有，改判 replan。"
            f"理由：{judgement.reason}"
        )
        return {"decision": "replan", "plan_feedback": ""}

    print(f"[节点] classify_reply 判成 {decision}。理由：{judgement.reason}")

    # 输出   一到两笔写：decision 整份覆盖（每轮重判，不累计）。
    #        判成 run 或 cancel 的时候还要把 plan_feedback 清掉 —— 那两句话不是
    #        「要改方案」，而它会跟着 checkpoint 一直躺着，等这条 thread 下次被唤醒
    #        时冒充成一条反馈。存它的是 await_go，清它的是这里，两个节点前后脚，
    #        管的是同一轮握手的两半（见 state.py 那个字段的注释）
    if decision == "replan":
        return {"decision": decision}
    return {"decision": decision, "plan_feedback": ""}


# 对话里拟出来的 query 建行时填的两个数。queries 表这两列都是 not null，建行必须给。
#
# pool 填 candidate：这一列是「scheduled 组队时这条 query 算哪一档」，三档各有含义 ——
# pinned 是人工钦定不参与表现筛选，proven 是硬指标达标的，candidate 是样本量还不够的
# 试跑期。对话里刚拟出来的词没有任何表现记录，只能是 candidate。
#
# cadence_days 填 7：这一列是「这条 query 隔多少天才值得再跑一次」。scheduled 每天组队
# 时会跳过 last_run_at + cadence_days 还没到的那些，所以它管的是采集的钱 —— 填 1 是每天
# 都可以跑，填 7 是一周最多跑一次。
# 对 interactive 来说它暂时是个摆设：新建的那条随后就被 set_active(False) 关掉了，根本
# 不参与组队，这个数对它不起作用。它要等「以后有人把这条词显式打开」那天才生效，而那天
# 7 是更省钱的起点 —— 对话里探出来的词多半是存量类（同样那几个人反复出现），天天跑只会
# 天天捞回同一批，而 novel_lead_count 会因为跑得太勤而长期偏低、被 proven 排名冤枉降级。
_INTERACTIVE_POOL = QueryPool.CANDIDATE
_INTERACTIVE_CADENCE_DAYS = 7


def run_harvest(state: InteractiveState, config: RunnableConfig) -> dict:
    """真去采一轮：把方案里那几句话落成 query，开一次 run，跑完整条 discovery。

    开 run、采集、打分、落库、给 runs 那行收尾这一段跟 scheduled 共用 harvest_once。
    那个函数的第一句是一把 advisory lock：整个库同一时刻只允许跑一个 discovery。

    谁让谁：先拿到锁的先跑，后到的当场退，没有排队也没有优先级。两种撞法：

        定时那跑 07:00 先开始，用户 07:02 在对话里说「跑」
            这个节点抛 DiscoveryBusy。库里一个字没写、一分钱没花，而且这个节点一个
            state 字段都没写成 —— 所以同一条对话再 invoke 一次就是重新跑它一遍，
            用户再说一次「跑」就行

        用户先说「跑」，定时那跑随后 07:00 开始
            定时那跑当天就没了，main() 返回非零让 systemd 在 journal 里记一笔。
            这条路一天只有一次机会，所以它要看得见，不能静默跳过

    锁是事务级的，采集那一跑一提交就放了 —— 挡人的时间是那一跑的几分钟，不是一整天。

    第二个参数收 config 是 LangGraph 允许的更长签名。thread_id 不在 state 里，
    它在 config 里 —— state 是信的内容，thread_id 是这封信寄到哪个信箱。

    输入（实际值，用户回了「跑」、classify_reply 判完之后）：

        state   {"messages": [..., "用户: 跑"],
                 "intent_result": {"intent": {"topics": ["bone conduction"], ...}, ...},
                 "clarify_rounds": 0,
                 "plan": {"version": 1,
                          "queries": [{"text": "bone conduction AND (headphone OR earbuds)",
                                       "filters": {}}],
                          "assumptions": "...", "estimated_cost_usd": 0.15},
                 "decision": "run", "run_id": None}
        config  {"configurable": {"thread_id": "slack:C0C0139:1788400486.110719"}}

    输出（实际值）：

        {"run_id": 31,
         "messages": ["AI: 采完了。run 31，completed，发现 23 家公司"]}

    这个函数对库里做的全部改动，按发生顺序：

        queries              方案里每句话保证有一行。已经存在的一个字都不动；新建的
                             随后把 is_active 改成 false
        剩下的全是 harvest_once 做的        runs / query_executions / signals / leads /
                             lead_evidence / lead_discovery_hits / queries.last_run_at，
                             逐张写在那个函数 docstring 的末尾

    整段在同一个连接上：中途炸了全部一起回滚，库里不会留下一条「建过但从没跑过」的
    query，也不会留下一行没有 signals 的 run。
    """
    # 输入   thread_id 从 config 拿。写进 runs.thread_id，是这一跑唯一的来历线索
    thread_id = config["configurable"]["thread_id"]

    # 输入   这一轮的品类词，如 ['bone conduction']。它跟着每条新建的 query 一起落库，
    #        是下一次对话拿 topic 对 topic 的那一头
    topics = state["intent_result"]["intent"]["topics"]

    # 输入   采集窗口，plan_queries 算好放在方案里的那一份，也就是卡片上给他看的那一个。
    #        不在这里重算：他看完卡片过几个小时才回「跑」的话，重算出来的日期会往后挪
    #        posted_since='2026-07-28'、posted_limit=None：传 postedLimitDate
    #        posted_since=None、posted_limit='month'：传 postedLimit（没说时间，或者说了不限时间）
    posted_since = state["plan"]["posted_since"]
    posted_limit = state["plan"]["posted_limit"]

    #        now 整跑只取一次：分头取的话 recency 和 cooldown 会差几分钟，
    #        而那点差别会让同一批数据两次跑不出同样的结果
    now = dt.datetime.now(dt.UTC)

    #        五份配置连同它们之间那条跨文件的约束一次读齐。缺键在这里就炸，
    #        也就是在花任何钱之前
    configs = load_harvest_configs()

    # 输出   BudgetConfig(monthly_cap_usd=13.0, billing_day=18)；unit_price_usd=0.002；estimate_usd=0.15。
    #        单价跟 plan_queries 报价时用的是同一个数，所以「已花」和「报价」是一个口径
    budget = load_budget_config()
    unit_price_usd = load_planner_config().unit_price_usd
    estimate_usd = state["plan"]["estimated_cost_usd"]

    with db.connect() as conn, conn.cursor() as cur:
        # 月度上限，在这个事务写任何一行之前。
        # 输入   estimate_usd=0.15，plan_queries 算好放在方案里的报价，用户看到的就是这个数
        # 输出   本账期已花，如 1.23；超了抛 MonthlyCapReached，这里一个字都还没写、一分钱没花。
        #        那个异常走 runner._recover 的情况一（slack_conversation._harvest_crash）：
        #        退回 await_go，方案原样再发一遍
        spent = check_monthly_cap(
            cur, estimate_usd=estimate_usd, now=now, budget=budget, unit_price_usd=unit_price_usd
        )
        print(f"[节点] run_harvest 本账期已花 ${spent:.2f}，这次报价 ${estimate_usd:.2f}")

        # 输入   state["plan"]["queries"]，对话里拟出来、用户点过头的那几句话
        # 输出   specs，跟 scheduled 那边 selector 给出来的是同一种东西
        #        每句话在 queries 表里恰好一行。落表不是记录对话，是记账：
        #        query_executions 有外键指着它，而跨 thread 跨天的聚合
        #        （这条词跑过几次、采到几家、几家过线）只有那张表做得出来
        specs = []
        # 一条 query 现在是 {'text': ..., 'filters': {...}} 而不是一个裸字符串。
        # 换形状是因为旋钮是 per-query 的：同一次对话里可以有一条筛 Founder、
        # 另一条不筛，而它们是库里两条不同的 query（0009 的唯一键含 filters）。
        for item in state["plan"]["queries"]:
            text = item["text"]
            filters = item["filters"]
            query_id, created = query_db.upsert_query(
                cur,
                text=text,
                source=QuerySource.INTERACTIVE,
                source_type=SourceType.LINKEDIN_POST,
                pool=_INTERACTIVE_POOL,
                cadence_days=_INTERACTIVE_CADENCE_DAYS,
                # 这一跑所有的 query 共用同一批品类词：它们是同一次 understand_intent
                # 读出来的同一个题目，planner 只是把它拟成了几条不同的检索式。
                # 存下来是为了下一次对话能拿 topic 直接对 topic，不用再去猜正文
                topics=topics,
                # 旋钮跟着这一条走，不是整跑一套：同一次对话里可以有一条筛
                # Founder、另一条不筛。桩阶段 planner 还只会给空字典。
                filters=filters,
            )
            # 新建的那条默认不进每天的组队。一次探索不等于「从此每天跑」：每日额度
            # 是固定的，探索出来的词一条条堆进 candidate 池，会把种子词挤出去，
            # 而报表上完全正常。要它进轮转是一个显式动作（set_active(active=True)）。
            # 只对新建的做：撞上的那条可能是库里早就在跑的种子，不能被这里关掉。
            if created:
                query_db.set_active(cur, query_id, active=False)
            # pool 这一栏对已经存在的那条可能不准（库里也许是 proven）。它只进报告、
            # 不参与任何判断，pipeline 那边一次都没读过，所以不为它多查一次库。
            specs.append(
                QuerySpec(
                    query_id=query_id,
                    text=text,
                    source_type=SourceType.LINKEDIN_POST,
                    pool=_INTERACTIVE_POOL,
                    cadence_days=_INTERACTIVE_CADENCE_DAYS,
                    filters=filters,
                )
            )

        # 输出   (run_id, DiscoveryResult)。这里面才是真花钱的那一步
        #        上面那几行 upsert 跟这一跑在同一个事务里：采集半路炸了整批一起回滚，
        #        库里不会留下一条「建过但从没跑过」的 query
        run_id, result = harvest_once(
            conn,
            cur,
            specs,
            run_type="interactive",
            thread_id=thread_id,
            now=now,
            configs=configs,
            posted_within=posted_limit or "",
            posted_since=posted_since,
        )

    print(f"[节点] run_harvest 采完 run={run_id} status={result.status} 公司={result.company_count}")

    # 输出   两笔写：run_id 覆盖（这条对话花过钱了），messages 追加一句给用户看的
    told_user = f"AI: 采完了。run {run_id}，{result.status}，发现 {result.company_count} 家公司"
    return {"run_id": run_id, "messages": [told_user]}


def find_contacts(state: InteractiveState, config: RunnableConfig) -> dict:
    """推卡之前，给这一跑要推卡的那几家找联系方式（公司详情、查帖子作者、官网邮箱），结果写进库。

    run_harvest 之后固定走这里，走完固定去 deliver。deliver 拼卡片时从库里读这一步写进去的东西
    （triggers/contacts.card_contacts），所以这一步必须在 deliver 前面。

    输入（实际值，run_harvest 刚采完）：

        state   {"messages": [..., "AI: 采完了。run 45，completed，发现 23 家公司"], "run_id": 45, ...}
        config  {"configurable": {"thread_id": "slack:C0000000001:1790126072.000100"}}

    输出（样子；这一步还没真跑过，数是尖括号里那几样）：

        {"messages": ["AI: 找联系方式：<几家要推卡> 家要推卡，公司详情 <抓到几家> 家，"
                      "查人 <查了几个> 个（对上 <几个> 个），邮箱 <几个> 个，花了 $<Apify 回的扣费>"]}

        只追加一句话，不写别的字段。这一句不发给用户（用户看到的是 deliver 那一句），
        留在 messages 里是为了以后翻这条对话时知道这一步做了什么

    找哪几家、怎么找、花多少，全在 triggers/contacts.find_contacts_for_run，这里只开连接、调它。

    出错不往外抛，照样去 deliver：找联系方式是锦上添花，不能因为它推不出卡片。
    find_contacts 自己已经接住了调 actor 出错、超了月度上限这两种；这里再接一层，接的是连库、
    查库这类意外。花了的钱在它里面每次调完 actor 就单独提交了，这里出错不会让那几笔账丢掉。
    也因为不往外抛，这个节点崩溃重跑那种情况（runner._recover 的情况三）基本碰不到
    """
    # state["run_id"]=45。走到这里一定有值：run_harvest 采完才会写它
    run_id = state["run_id"]

    try:
        # 输出   ContactsReport，例子见 triggers/contacts.py 那个类
        with db.connect() as conn:
            report = find_contacts_for_run(conn, run_id=run_id, now=dt.datetime.now(dt.UTC))
    except Exception as err:
        print(f"[节点] find_contacts run={run_id} 出错，跳过，卡片照推：{type(err).__name__}: {err}")
        return {"messages": [f"AI: 找联系方式出错了（{type(err).__name__}），这一次卡片上联系方式是空的"]}

    # 输出   '找联系方式：<几家> 家要推卡，公司详情 <几家> 家，查人 <几个> 个（对上 <几个> 个），
    #         邮箱 <几个> 个，花了 $<扣费>'；超了月度上限、调 actor 出错时各多一句
    text = (
        f"找联系方式：{report.lead_count} 家要推卡，公司详情 {report.companies_fetched} 家，"
        f"查人 {report.people_looked_up} 个（对上 {report.people_matched} 个），"
        f"邮箱 {report.emails_from_people + report.emails_from_websites} 个，花了 ${report.spent_usd}"
    )
    if report.blocked_by_cap:
        text += f"。超了月度上限，付费的那一半没找：{report.blocked_by_cap}"
    if report.actor_errors:
        text += f"。有一步出错跳过了：{report.actor_errors}"
    print(f"[节点] find_contacts run={run_id} {text}")
    return {"messages": [f"AI: {text}"]}


def deliver(state: InteractiveState, config: RunnableConfig) -> dict:
    """把这一跑过线的线索卡推到频道顶层，再往对话 thread 里回一句推了几张、在哪。

    run_harvest 之后固定走这里，走完是 END。这个节点不调模型、不花采集的钱，
    只查库、发 Slack、写 pushed_at。

    输入（实际值，run_harvest 刚采完）：

        state   {"messages": [..., "用户: 跑", "AI: 采完了。run 31，completed，发现 23 家公司"],
                 "run_id": 31, ...}
        config  {"configurable": {"thread_id": "slack:C0000000001:1790126072.000100"}}

    输出（实际值，分析了 12 家、3 家过线、都发出去了）：

        {"messages": ["AI: 分析了 12 家，3 家过线，推了 3 张到频道："
                      "https://acme-team.slack.com/archives/C0000000001/p1790126080000200"]}

        只追加一句话，不写别的字段。走到 END 之后 render_ended 拿 messages 最后一句
        发回 thread，所以这一句就是用户最后看到的那句。

    关键步骤：

        ① 拿这条对话开头那句话的链接     印在频道汇总上，Riley 点它能回到这次对话
        ② push_interactive               查素材、挡 in_draft、取过线的、防重推、发、记账
        ③ 提交                           pushed_at 落库，重跑时 pushed_in_run 才查得到
        ④ 拿频道汇总那条消息的链接       放进回给用户的那句话里
        ⑤ 拼回给用户的那句话             _delivered_text，六种情况各一句

    ③ 在 ④ 前面，顺序不能反：④ 是又一次调 Slack，它要是在提交之前崩溃，卡片已经发出去了、
    账却没记上，用户回一句话重跑的时候就会再发一遍。提交之后再崩溃，重跑时 pushed_in_run
    查得到那几张，一张都不会重发，只是回的那句话里没有链接。

    这个节点崩溃走 runner._recover 的情况三：先回一句「出错了」，用户回任意一句
    就从这个节点重跑。能放心重跑，靠的就是 push_interactive 的「推过不再推」。

    一家都没过线时，只在对话 thread 里提示用户换个说法、去频道里重新 @ bot 开一个新对话。
    图走到 END 之后，同一个 thread 里再说话会被判成 FINISHED，所以不能说「在这里回一句我重拟」。
    另一种做法是从这里接一条边绕回 await_go，让用户在原 thread 里说「改」，上下文都还在。
    现在不这么做，要等两件事：一是月度上限做完（已经做了，见 budget.py），因为那条边会让一个 thread 反复花钱；
    二是真跑下来没过线的次数确实多（query_executions.qualified_lead_count 按 run_type 汇总就查得到）。
    真换的时候 run_id 的含义要跟着改：今天它的意思是「这条对话采过了」，绕回去之后要变成
    「最近一次采的是哪一跑」，deliver 查的也要是最近那一跑
    """
    # state["run_id"]=31。走到这里一定有值：run_harvest 采完才会写它，
    run_id = state["run_id"]

    # thread_id，如 'slack:C0000000001:1790126072.000100'
    thread_id = config["configurable"]["thread_id"]
    prefix, _, rest = thread_id.partition(":")
    # origin_channel='C0000000001'，root_ts='1790126072.000100'
    origin_channel, _, root_ts = rest.partition(":")

    # 一个 slack_sdk.WebClient。跟定时那一跑用同一个 bot token、同一个频道
    client = send.build_client()
    channel = send.read_channel()

    # ① 'https://acme-team.slack.com/archives/C0000000001/p1790126072000100'，拿不到是 None
    origin = send.permalink(client, origin_channel, root_ts) if prefix == "slack" and root_ts else None

    # ② 和 ③  同一个连接：push_interactive 只写不提交，提交在这里
    # 使用 push_interactive，result 是真的发送到Slack里，也被记账到数据库里的卡片信息
    # InteractivePush(analyzed=12, qualified=3, best_score=81, posted=3, failures={},
    #                  summary_ts="1790126080.000200", already_pushed=0, run_failed=False)
    with db.connect() as conn, conn.cursor() as cur:
        result = push_interactive(
            cur, client, channel, run_id=run_id, now=dt.datetime.now(dt.UTC), origin=origin
        )
        conn.commit()

    # ④ 使用 summary_ts='1790126080.000200'。没往频道发（没过线、推过了、采集全失败）时是 None
    #   得到'https://acme-team.slack.com/archives/C0000000001/p1790126080000200' 或 None
    link = send.permalink(client, channel, result.summary_ts) if result.summary_ts else None

    print(
        f"[节点] deliver run={run_id} 分析 {result.analyzed} 家 过线 {result.qualified} 家 "
        f"推了 {result.posted} 张 之前已推 {result.already_pushed} 张"
    )

    # ⑤ 追加一句run和推送的结果给用户看
    return {"messages": [f"AI: {_delivered_text(result, link)}"]}


# 没往频道发卡片的几种情况都用这一句收尾：图已经走到 END，这个 thread 里再说话不会再进图
_START_OVER = "换个说法在频道里重新 @ 我，会开一个新的对话。"


def _delivered_text(result: InteractivePush, link: str | None) -> str:
    """把推卡的结果拼成回给用户的一句话。纯函数。

    输入：push_interactive 的返回值；频道汇总那条消息的链接（没有是 None）
    输出：一句中文，七种情况：

        采集全失败      InteractivePush(run_failed=True, ...)
                        -> "这一跑的采集全失败了，一条线索都没有。这不是没有合格的公司，是没采到数据。
                            晚点再在频道里 @ 我试一次。"
        一条都没采到    InteractivePush(harvested=0, analyzed=0, ...)
                        -> "这次一条帖子都没采回来。多半是检索式太窄，或者「只看这些人发的帖」那一条
                            筛得太严。换个说法在频道里重新 @ 我，会开一个新的对话。"
        一家都没分析    InteractivePush(harvested=40, analyzed=0, ...)
                        -> "采回来 40 条帖子，但没有一家公司进到分析：要么被过滤规则砍掉了（供应商、
                            跟品类无关的），要么已经在冷却期里或者之前判过不合格。换个说法……"
        全在写稿        InteractivePush(analyzed=2, qualified=0, best_score=None, ...)
                        -> "这次分析的 2 家都已经在写稿了，没有新的卡片可推。"
        没过线          InteractivePush(analyzed=12, qualified=0, best_score=45, ...)
                        -> "这次分析的 12 家都没过线（最高 45 分）。换个说法在频道里重新 @ 我，
                            会开一个新的对话。"
        推过了          InteractivePush(already_pushed=3, ...)
                        -> "这一跑的 3 张卡片之前已经推到频道了，这次没有再发一遍。"
        正常            InteractivePush(analyzed=12, qualified=3, posted=3, ...), link='https://...'
                        -> "分析了 12 家，3 家过线，推了 3 张到频道：https://..."
                        有发失败的，后面再接一句「另外 1 张没发出去（lead 1290：msg_too_long）」
        连没过线的也推  InteractivePush(analyzed=12, qualified=0, posted=12, below_line=12, ...)
                        -> "分析了 12 家，0 家过线，推了 12 张到频道（其中 12 张没过线，灰色的）：https://..."

    判断的顺序跟 push_interactive 里提前返回的顺序一样，同一个结果只可能落进一种
    """
    if result.run_failed:
        return (
            "这一跑的采集全失败了，一条线索都没有。这不是没有合格的公司，是没采到数据。"
            "晚点再在频道里 @ 我试一次。"
        )

    if result.harvested == 0:
        # 供应商一条都没返回。该改的是检索式，所以回话里点出最可能的两个原因
        return (
            "这次一条帖子都没采回来。多半是检索式太窄，或者「只看这些人发的帖」那一条筛得太严。"
            f"{_START_OVER}"
        )

    if result.analyzed == 0:
        # 采回来了，却一家都没进 LLM：帖子在过滤那一步全被砍掉，或者公司全被冷却期、之前的判决挡掉。
        # 冷却期里那几家不在卡片上显示，它们库里是旧分数，推出去等于重推一张旧卡
        return (
            f"采回来 {result.harvested} 条帖子，但没有一家公司进到分析：要么被过滤规则砍掉了"
            "（供应商、跟品类无关的），要么已经在冷却期里或者之前判过不合格。"
            f"{_START_OVER}"
        )

    # 没过线、也没往频道发东西。summary_ts 是 None 说明 push_interactive 在发之前就返回了；
    # push_below_line 打开时没过线的照样会发（summary_ts 有值），那种走下面的「正常」
    if result.qualified == 0 and result.summary_ts is None and not result.already_pushed:
        if result.best_score is None:
            # 分析了的那几家全是 in_draft，被 build_cards 挡掉了，没有一张能出卡
            return f"这次分析的 {result.analyzed} 家都已经在写稿了，没有新的卡片可推。"
        return f"这次分析的 {result.analyzed} 家都没过线（最高 {result.best_score} 分）。{_START_OVER}"

    if result.already_pushed:
        return f"这一跑的 {result.already_pushed} 张卡片之前已经推到频道了，这次没有再发一遍。"

    # 输出   "分析了 12 家，3 家过线，推了 3 张到频道：https://..."
    #        拿不到链接时停在「推了 3 张到频道」：卡片已经在频道里了，少的只是一个跳转
    #        push_below_line 打开时写成「0 家过线，推了 12 张到频道（其中 12 张没过线，灰色的）」
    text = f"分析了 {result.analyzed} 家，{result.qualified} 家过线，推了 {result.posted} 张到频道"
    if result.below_line:
        text += f"（其中 {result.below_line} 张没过线，灰色的）"
    if link:
        text += f"：{link}"
    if result.failures:
        # 输入   failures={1290: "msg_too_long"}
        # 输出   "。另外 1 张没发出去（lead 1290：msg_too_long）"。错码原样给，那是查 Slack 文档的钥匙
        detail = "，".join(f"lead {lead_id}：{code}" for lead_id, code in sorted(result.failures.items()))
        text += f"。另外 {len(result.failures)} 张没发出去（{detail}）"
    return text
