"""手跑一遍 interactive discovery 的图，看两个 interrupt、两个环和那三条分支。

    python -m scripts.try_interactive

不连数据库、不花钱、不调模型。换掉的有七处：存档器用 InMemorySaver（存在内存里，
进程一退就没），understand_intent / retrieve_knowledge / run_harvest / deliver 四个节点整个
换成桩，plan_queries 和 classify_reply 只换掉里面调模型那一句（draft_queries 和
read_decision），节点本身照跑。

后面那几个是必须的。run_harvest 接的是真 pipeline，真跑一次就是真连 Neon、真调
Apify、真往 leads 里写；另外三次调用每跑一次就真调一次 OpenAI，而且每次读出来的
东西不完全一样 —— 这个脚本是拿来看图怎么走的，不是拿来采集也不是拿来评估模型的。

plan_queries 和 classify_reply 为什么只换里面那一句、不整个换掉：换掉整个节点的话，
真正想看的东西就没了 —— 那份清单、报价、被丢掉的那几条，全是节点里那几个纯函数
算出来的。只把花钱的那一句顶掉，剩下的照跑。

1d 那一步会把存档器换成真的 Neon，那时才谈得上「昨天停在 interrupt 上、今天进程
重启还能接着聊」。要真调模型、真跑采集是接上 Slack 之后的事，不在这个脚本里。

跑两条对话。第一条从「帮我找点公司」开始，先被问一句才说清楚，然后走「改」再走
「跑」；第二条一句话就说清楚了，直接走「取消」。两个 interrupt、两个环、三条分支
都过一遍。

要盯的八件事，都印在输出里：

  1. 第一条对话的第一次 invoke 停在 ask_clarification，交出来的东西带着 kind: clarify。
     这时候那句问题还没进 messages，它只是 interrupt 的 payload。
  2. 回答之后，问和答一起进了 messages，clarify_rounds 从 0 变成 1 —— 恢复时
     ask_clarification 整个从第一行重跑，但它只 return 了一次。
  3. 同一次 invoke 里 understand_intent 只打印一次：它没有 interrupt，不会跟着被重放。
     方案 A 省不掉的就是这一次重复调用。
  4. 第二个 interrupt 在 await_go，交出来的东西带着 kind: go 和报价。
     两个 interrupt 分属两个节点，同一时刻只会停在其中一个上。
  5. plan_queries 在同一次 invoke 里只打印一次，恢复时它不重跑。
  6. 回「别带 Kickstarter」那次，图绕回的是 understand_intent 不是 plan_queries：
     那句修改要先变成需求里的 exclusions，才拦得住、也才会出现在下一版的清单上。
     plan 里的 version 从 1 变成 2 —— 这是条件边真的绕回去了，不是节点被重放。
     两件事在日志里长得一样，version 是唯一的分辨办法。
  7. 第一版方案里有一条是故意写坏的（带 NOT），所以清单末尾看得见「丢掉了 1 条」
     连原因。丢掉而不说等于静默丢弃。
  8. 回「算了」那次，run_harvest 一个字都没印：那条路根本没被走。
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from triggers.interactive_discovery.query import graph as graph_module
from triggers.interactive_discovery.query import nodes as nodes_module
from triggers.interactive_discovery.query.graph import build_graph
from triggers.interactive_discovery.query.planner import PlanDraft
from triggers.interactive_discovery.query.reply import ReplyJudgement

# understand_intent 那个桩这一趟该照着念的几份 intent_result，一轮取一份。
# 每条对话开始前重新塞满（见 main）。
_SCRIPT: list[dict] = []


def _intent_result(topics: list[str], question: str) -> dict:
    """拼一份 understand_intent 该写进 state 的东西。

    输入：这一轮「读出来」的品类词；这一轮想问的那句话
    输出：一份 intent_result，形状跟真的那份一模一样

    一个实际值：

        _intent_result([], "你想找做哪一类设备的公司？")
        -> {"intent": {"goal": "找潜在客户公司", "topics": [], "events": [],
                       "exclusions": [], "time_said": "", "max_spend_usd": 0.0,
                       "unsupported": []},
            "clarification_question": "你想找做哪一类设备的公司？"}
    """
    return {
        "intent": {
            "goal": "找潜在客户公司",
            "topics": topics,
            "events": [],
            "exclusions": [],
            "time_said": "",
            "max_spend_usd": 0.0,
            "unsupported": [],
        },
        "clarification_question": question,
    }


def _stub_understand_intent(state) -> dict:
    """顶掉真的 understand_intent：照剧本念，不调模型。

    输入：跟真的那个一样（一份 state）
    输出：一笔写，intent_result 整份覆盖

    剧本里最后一份是粘着的：取完不弹，之后每轮都给同一份。这样「模型一直读不出
    品类词」那种情况不用在剧本里堆一排重复的东西。
    """
    value = _SCRIPT[0] if len(_SCRIPT) == 1 else _SCRIPT.pop(0)
    print(f"[节点] understand_intent 执行（脚本里的桩）读到 topics={value['intent']['topics']}")
    return {"intent_result": value}


def _stub_retrieve_knowledge(state) -> dict:
    """顶掉真的 retrieve_knowledge：它要连 Neon。

    输入：跟真的那个一样（state）
    输出：一份形状对、内容空的 knowledge

    这个节点不花采集的钱，但它连库，而这个脚本要能在没有网、没有连接串的机器上跑。
    四个键都给全：少一个的话 plan_queries 以后读它会 KeyError，而那种错该在真跑的
    时候暴露，不该被这个桩掩盖。

    换的是 graph 模块上那个名字，不是 nodes 上的：graph.py 在 import 的时候就把函数
    对象取走了，build_graph 用的是 graph 这个模块的全局名。
    """
    print("[节点] retrieve_knowledge 执行（脚本里的桩，没有真查库）")
    return {
        "knowledge": {
            "avoid": [],
            "topics_searched": list(state["intent_result"]["intent"]["topics"]),
            "known_queries": [],
            "known_lead_total": 0,
        }
    }


def _stub_draft_queries(client, *, intent, knowledge, lines, feedback, previous_plan, config) -> PlanDraft:
    """顶掉拟词那次 LLM 调用。节点本身没被换掉，只有花钱的这一句被换掉了。

    输入：跟真的那个一样
    输出：一份 PlanDraft，内容照着 previous_plan 有没有分两种

    换这一句而不是把整个 plan_queries 换成桩，是为了让这个脚本仍然跑真的
    screen_queries 和 build_assumptions —— 卡片上那份清单、报价、被丢掉的那几条
    都是真算出来的。故意夹一条带 NOT 的，好让「丢掉了 1 条」在输出里看得见。
    """
    if previous_plan:
        print(f"[桩] draft_queries 看到上一版，按 {feedback!r} 改（没有真调模型）")
        return PlanDraft(queries=({"text": "bone conduction AND (launch OR preorder)", "filters": {}},))
    print("[桩] draft_queries 第一次拟（没有真调模型）")
    return PlanDraft(
        queries=(
            {"text": "Kickstarter AND bone conduction", "filters": {"authorKeywords": "Founder OR CTO"}},
            {"text": "bone conduction NOT supplier", "filters": {}},
        ),
    )


def _stub_read_decision(client, lines, plan, *, feedback, config) -> ReplyJudgement:
    """顶掉判断那次 LLM 调用。照用户最后那句话查一张小表。

    输入：跟真的那个一样
    输出：一份 ReplyJudgement

    这张表只是为了让这个脚本每次跑出同一条路径，不是 classify_reply 的实现 ——
    真的那个读的是要判的那一句加整条对话加这一版方案，「行吧就这样」这种它答得了，
    这张表答不了。
    表里没有的一律 replan，跟真实现里「拿不准往不花钱那边倒」是同一个方向。
    """
    table = {"跑": "run", "算了": "cancel"}
    decision = table.get(feedback.strip(), "replan")
    print(f"[桩] read_decision 把 {feedback!r} 判成 {decision}（没有真调模型）")
    return ReplyJudgement(decision=decision, reason=f"桩：照小表把 {feedback!r} 判成 {decision}")


def _stub_run_harvest(state, config) -> dict:
    """顶掉真的 run_harvest：只打印，不连库不采集。

    输入：跟真的那个一样（state 和 config）
    输出：一份假的写，run_id 给个看得出是假的数

    换的是 graph 模块上那个名字，不是 nodes 上的：graph.py 在 import 的时候就把函数
    对象取走了，build_graph 用的是 graph 这个模块的全局名。
    """
    print("[节点] run_harvest 执行（脚本里的桩，没有真采集）")
    return {"run_id": 31, "messages": ["AI: 采完了（桩）"]}


def _stub_find_contacts(state, config) -> dict:
    """顶掉真的 find_contacts：只打印，不连库、不调付费 actor。

    输入：跟真的那个一样（state 和 config）
    输出：一句假的回话
    """
    print("[节点] find_contacts 执行（脚本里的桩，没有真找联系方式）")
    return {"messages": ["AI: 找联系方式（桩）"]}


def _stub_deliver(state, config) -> dict:
    """顶掉真的 deliver：只打印，不连库、不往 Slack 频道发卡片。

    输入：跟真的那个一样（state 和 config）
    输出：一句假的回话
    """
    print("[节点] deliver 执行（脚本里的桩，没有真推卡片）")
    return {"messages": ["AI: 推了 0 张到频道（桩）"]}


def _show(app, config, result, title: str) -> None:
    # 输入   一次 invoke 的返回值
    # 输出   把「交出来的东西」「读出来的 intent」「问过几轮」「这一版方案」
    #        「判成了什么」「现在停在哪」六样印出来
    print()
    print(f"--- {title} ---")
    for item in result.get("__interrupt__", ()):
        print("    交出来的东西：", item.value)
    print("    intent   =", result["intent_result"].get("intent", {}))
    print("    问过几轮 =", result["clarify_rounds"])
    print("    plan     =", result["plan"])
    print("    decision =", repr(result.get("decision", "")))
    print("    现在停在：", app.get_state(config).next, "（空元组 = 跑完了）")


def main() -> None:
    # 先顶掉会花钱、会连库、会发 Slack 的那五个节点，再建图。顺序不能反：build_graph 是在调用的
    # 那一刻才去取这几个名字的，建完再换就晚了
    graph_module.understand_intent = _stub_understand_intent
    graph_module.retrieve_knowledge = _stub_retrieve_knowledge
    graph_module.run_harvest = _stub_run_harvest
    graph_module.find_contacts = _stub_find_contacts
    graph_module.deliver = _stub_deliver
    # 这三个换在 nodes 模块上，不是 graph 上：plan_queries 和 classify_reply 这两个
    # 节点本身照跑，被顶掉的是它们函数体里那几句 —— 那几个名字是在 nodes 这个模块的
    # 全局里查的。build_llm_client 也得顶掉，它会去找 OPENAI_API_KEY
    nodes_module.build_llm_client = lambda: None
    nodes_module.draft_queries = _stub_draft_queries
    nodes_module.read_decision = _stub_read_decision
    app = build_graph(InMemorySaver())

    print("=" * 70)
    print("对话一：一句话说不清 -> 被问一句 -> 说清楚 -> 停在拍板 -> 改 -> 跑")
    print("=" * 70)

    # 剧本：第一轮读不出品类词（停下来问），第二轮读出来了（往下走）
    _SCRIPT[:] = [
        _intent_result([], "你想找做哪一类设备的公司？"),
        _intent_result(["bone conduction"], "要不要把颈挂式的蓝牙耳机也算进来？"),
    ]

    # thread_id = 这是哪一条对话。真实的会是 slack:<频道 id>:<thread 时间戳>，
    # 这里随便给一个。同一个 id 才能接着上次聊。
    config = {"configurable": {"thread_id": "demo:1"}}

    # invoke 的第一个参数是初始 state。InteractiveState 那几个 key 一个不少地给上：
    # LangGraph 只把「已经有值的 channel」交给节点，没给的 key 在节点里读会 KeyError。
    # 今天少给 knowledge 也跑得动（retrieve_knowledge 会在有人读它之前写上），
    # 但那是碰巧 —— 哪天有个节点在它之前读它，这里就炸，而且只炸这一条路。
    # tests/test_interactive_graph.py 有一条 ast 测试焊着「每处 invoke 都给全」
    result = app.invoke(
        {
            "messages": ["用户: 帮我找点公司"],
            "intent_result": {},
            "clarify_rounds": 0,
            "knowledge": {},
            "plan": {},
            "plan_feedback": "",
            "decision": "",
            "run_id": None,
        },
        config,
    )
    _show(app, config, result, "第一次 invoke：停在 ask_clarification 的 interrupt 上")

    # 恢复不是再 invoke 一次输入，而是投一个 Command(resume=值)。
    # 这个值会原样变成那个节点里 interrupt(...) 的返回值。
    # 这一趟一口气走完 ask_clarification -> understand_intent -> plan_queries -> await_go
    result = app.invoke(Command(resume="骨传导耳机，最好是刚做完众筹的"), config)
    _show(app, config, result, "回答之后：问答进了 messages，方案拟好，停在 await_go")

    # 「别带 Kickstarter」不在桩那张小表里，所以判成 replan
    result = app.invoke(Command(resume="别带 Kickstarter"), config)
    _show(app, config, result, "回「别带 Kickstarter」：绕回 understand_intent，version 变 2")

    result = app.invoke(Command(resume="跑"), config)
    _show(app, config, result, "回「跑」：走 run_harvest、deliver，到 END")

    print()
    print("这条对话说过的每一句：")
    for line in result["messages"]:
        print("   ", line)

    print()
    print("=" * 70)
    print("对话二：一句话就说清楚了 -> 直接停在拍板 -> 取消，run_harvest 不该被走到")
    print("=" * 70)

    # 剧本：第一轮就读出了品类词，所以这条对话一次都不会问
    _SCRIPT[:] = [_intent_result(["conference microphone"], "要不要只看北美的？")]

    config2 = {"configurable": {"thread_id": "demo:2"}}
    result = app.invoke(
        {
            "messages": ["用户: 看看会议麦克风这块最近有什么"],
            "intent_result": {},
            "clarify_rounds": 0,
            "knowledge": {},
            "plan": {},
            "plan_feedback": "",
            "decision": "",
            "run_id": None,
        },
        config2,
    )
    _show(app, config2, result, "第一次 invoke：一轮都没问，直接停在 await_go")

    result = app.invoke(Command(resume="算了"), config2)
    _show(app, config2, result, "回「算了」：直接到 END")


if __name__ == "__main__":
    main()
