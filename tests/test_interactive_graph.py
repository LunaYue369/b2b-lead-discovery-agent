"""interactive discovery 那张图的接线测试：三条分支、那个环、decision 这个字段。

焊住的六类，全是「不报错，但要等用户真回话那一刻才发作」：

    decision 没声明      从 InteractiveState 里拿掉这个字段，classify_reply 那笔写会被
                         静默丢掉，条件边的判断函数读到的是 KeyError —— 而这要等用户
                         回了话、图走到 classify_reply 才发生
    判断函数没注解       第一个参数不写 InteractiveState，库会把节点的返回值额外并进来
                         一份，读到哪个值要看键撞不撞。行为不唯一，而且怎么错都不报错
    认不出来判成 run     三条路里只有「改」不花钱，拿不准必须往那条上落
    对着空方案说跑       一条 query 都没拟出来的时候判 run，会开一行假的 run，
                         而用户以为是市场上没有，不是我们没搜
    取值和分支表漂了     decision 多一个取值而那张表里没有，症状是用户回了那句话
                         才 KeyError；表里多一条而取值里没有，那条路就是死的
    取消也走了采集       cancel 那条路上 run_harvest 一次都不能被执行

2a 加的三类：

    澄清那一轮白问了     ask_clarification 只写 messages 不写问题，或者问完不绕回
                         understand_intent，症状是模型第二轮读到的还是旧对话，
                         于是问同一句话问到天荒地老
    轮数被重复加         ask_clarification 带 interrupt，恢复时整个节点从第一行重跑。
                         clarify_rounds 每轮该只加一次，加两次的话上限提前触发，
                         而且没有任何报错
    澄清环不收口         模型一直判「还差东西」的话，图会在两个节点之间无限绕，
                         最后撞上 LangGraph 的 recursion_limit 抛异常 —— 那是炸，
                         不是收口，而且炸在用户已经认真答了三四轮之后

不连库、不花钱、不调模型：用 InMemorySaver，跑的是真的 build_graph 出来的那张图。
understand_intent / retrieve_knowledge / run_harvest / find_contacts / deliver 五个节点整个换成桩；
plan_queries 和 classify_reply 只把里面调模型那一句换掉，节点本身照跑 —— 这个文件
要测的正是那两个节点自己的接线（筛完之后 plan 长什么样、判完之后走到了哪）。
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from models import validate_filters
from triggers.interactive_discovery.query.graph import build_graph, route_by_decision, route_intent
from triggers.interactive_discovery.query.intent import (
    DiscoveryIntent,
    IntentResult,
    load_intent_config,
)
from triggers.interactive_discovery.query.nodes import classify_reply
from triggers.interactive_discovery.query.planner import PlanDraft
from triggers.interactive_discovery.query.reply import DECISIONS, ReplyCallFailed, ReplyJudgement, parse_reply
from triggers.interactive_discovery.query.state import InteractiveState

FIRST_LINE = "用户: 帮我找找骨传导耳机的初创"
STUB_RUN_ID = 31

# retrieve_knowledge 那个桩交出来的东西。四个键都在，值是空的 —— 测这个文件的
# 是接线，不是查库；形状对不对由 tests/test_knowledge.py 焊。
STUB_KNOWLEDGE = {"avoid": [], "topics_searched": [], "known_queries": [], "known_lead_total": 0}
QUESTION = "你想找做哪一类设备的公司？"


def _intent_result(topics=("bone conduction",), question=QUESTION) -> dict:
    """拼一份 understand_intent 该写进 state 的东西。

    输入：这一轮模型读出来的品类词；这一轮它想问的那句话
    输出：一份 intent_result，形状跟真的那份一模一样

    一个实际值：

        _intent_result(topics=())
        -> {"intent": {"goal": "找潜在客户公司", "topics": [], "events": [],
                       "exclusions": [], "time_said": "", "max_spend_usd": 0.0,
                       "unsupported": []},
            "clarification_question": "你想找做哪一类设备的公司？"}

    七个字段一个不少地写出来，不图省事只写 topics：is_ready 今天只看 topics，
    哪天 _REQUIRED 多一条，这里少写的那个字段会让测试跟着一起瞎。
    """
    return {
        "intent": {
            "goal": "找潜在客户公司",
            "topics": list(topics),
            "events": [],
            "exclusions": [],
            "time_said": "",
            "max_spend_usd": 0.0,
            "unsupported": [],
        },
        "clarification_question": question,
    }


@pytest.fixture(autouse=True)
def _stub_retrieve_knowledge(monkeypatch):
    """把 retrieve_knowledge 换成桩。它要连 Neon。

    这个节点不花采集的钱，但它连库 —— 而 conftest.py 那道闸把 db.connect 换成了当场
    炸，所以不换的话这个文件里每一条测试都会在它那儿死。这个文件测的是接线对不对，
    不是查库对不对；查库那一层自己有 tests/test_knowledge.py。

    换的是 graph 模块上那个名字，不是 nodes 上的：graph.py 在 import 的时候就把函数
    对象取走了，build_graph 用的是 graph 这个模块的全局名。
    """

    def fake_retrieve_knowledge(state):
        print("[节点] retrieve_knowledge 执行（测试桩）")
        return {"knowledge": STUB_KNOWLEDGE}

    monkeypatch.setattr(
        "triggers.interactive_discovery.query.graph.retrieve_knowledge", fake_retrieve_knowledge
    )


@pytest.fixture(autouse=True)
def _stub_run_harvest(monkeypatch):
    """把 run_harvest 换成桩。这条是硬纪律，不是图省事。

    run_harvest 接的是真 pipeline：连 Neon、调 Apify、调模型、往 leads 里写。
    不换掉的话，跑一次「回「跑」」的测试就真花一次采集的钱，而测试照样全绿 ——
    这件事真发生过一次，两跑 80 条计费。conftest.py 里那道闸是第二层兜底，
    这里是第一层：这个文件测的是接线对不对，不是采集对不对。

    换的是 graph 模块上那个名字，不是 nodes 上的：graph.py 在 import 的时候就把
    函数对象取走了，build_graph 用的是 graph 这个模块的全局名。
    """

    def fake_run_harvest(state, config):
        print("[节点] run_harvest 执行（测试桩）")
        return {"run_id": STUB_RUN_ID, "messages": ["AI: 采完了（测试桩）"]}

    monkeypatch.setattr("triggers.interactive_discovery.query.graph.run_harvest", fake_run_harvest)


@pytest.fixture(autouse=True)
def _stub_find_contacts(monkeypatch):
    """把 find_contacts 换成桩。理由跟 run_harvest 那个一样：它连库、调付费 actor。

    这一条尤其不能省：真的 find_contacts 节点会接住自己里面的所有异常（找联系方式出错不许拖垮
    推卡），conftest.py 那道闸抛出来的「测试想连生产库」也会被它吞掉 —— 测试照样绿，闸门的
    报警却没人看得见。所以这里在第一层就换掉，不让它有机会去碰闸门。
    find_contacts 自己的逻辑在 tests/test_contacts.py 里测
    """

    def fake_find_contacts(state, config):
        print("[节点] find_contacts 执行（测试桩）")
        return {"messages": ["AI: 找联系方式（测试桩）"]}

    monkeypatch.setattr(
        "triggers.interactive_discovery.query.graph.find_contacts", fake_find_contacts
    )


@pytest.fixture(autouse=True)
def _stub_deliver(monkeypatch):
    """把 deliver 换成桩。理由跟 run_harvest 那个一样：它连库、往 Slack 频道发卡片。

    conftest.py 挡着 db.connect 和 send.build_client，不换的话每条走到「跑」的测试都会
    在它那儿死。deliver 自己的逻辑在 tests/test_deliver.py 里测，这个文件只看接线：
    run_harvest 之后是不是走到了它、走完是不是 END。
    """

    def fake_deliver(state, config):
        print("[节点] deliver 执行（测试桩）")
        return {"messages": ["AI: 推了 1 张到频道（测试桩）"]}

    monkeypatch.setattr("triggers.interactive_discovery.query.graph.deliver", fake_deliver)


@pytest.fixture(autouse=True)
def _no_llm_client(monkeypatch):
    """plan_queries 和 classify_reply 里那句 build_llm_client 换成给个 None。

    这两个节点本身照跑（测的就是它们的接线），被顶掉的只有真正花钱的那两句
    （draft_queries / read_decision，见下面两个 fixture）。那两句既然是桩，
    client 是什么就无所谓了 —— 但不顶掉的话它会去找 OPENAI_API_KEY，
    而且 conftest.py 那道闸会让它当场炸。
    """
    monkeypatch.setattr("triggers.interactive_discovery.query.nodes.build_llm_client", lambda: None)


@pytest.fixture(autouse=True)
def plans(monkeypatch):
    """把拟词那次 LLM 调用换成桩，并且让每个测试能改它拟出什么。

    输入：测试可以改 box["queries"]；跑完可以读 box["seen_feedback"]，那是这个节点
          真交到拟词那一步手上的那一句
    输出：一个字典，测试写 plans["queries"] = [...] 就改了这一版方案

    默认拟一条合格的。换的是 nodes 模块上那个名字 —— plan_queries 这个节点自己
    照跑，所以 screen_queries、build_assumptions、报价那几步都是真算的。
    """
    box = {
        "queries": [{"text": "Kickstarter AND bone conduction", "filters": {}}],
        "seen_feedback": None,
    }

    def fake_draft_queries(client, *, intent, knowledge, lines, feedback, previous_plan, config):
        print("[节点] plan_queries 执行（测试桩拟词）")
        box["seen_feedback"] = feedback
        return PlanDraft(queries=tuple(box["queries"]))

    monkeypatch.setattr(
        "triggers.interactive_discovery.query.nodes.draft_queries", fake_draft_queries
    )
    return box


@pytest.fixture(autouse=True)
def replies(monkeypatch):
    """把判断那次 LLM 调用换成桩：照用户最后那句话查一张小表。

    输入：测试可以改 box["table"]，键是 plan_feedback 那一句，值是三个取值之一
    输出：一个字典

    这张表不是 classify_reply 的实现，只是为了让这个文件里每条测试跑出确定的
    一条路径 —— 这个文件测的是接线（判成 run 之后是不是真走到了 run_harvest），
    不是模型判得准不准。判得准不准那件事没法在这里测，因为那要真调模型。
    表里没有的一律 replan，跟真实现里「拿不准往不花钱那边倒」同一个方向。
    """
    box = {"table": {"跑": "run", "算了": "cancel"}}

    def fake_read_decision(client, lines, plan, *, feedback, config):
        decision = box["table"].get(feedback.strip(), "replan")
        return ReplyJudgement(decision=decision, reason=f"测试桩：{feedback!r} -> {decision}")

    monkeypatch.setattr(
        "triggers.interactive_discovery.query.nodes.read_decision", fake_read_decision
    )
    return box


@pytest.fixture(autouse=True)
def intents(monkeypatch):
    """把 understand_intent 里那次 LLM 调用换成桩，并且让每个测试写这一轮它该读出什么。

    输入：测试可以改 box["queue"]（一轮取一份）
    输出：一个字典。跑完还可以读 box["seen_feedback"] 和 box["seen_last_line"] ——
          这个节点最后一次交到 read_intent 手上的那一句反馈，和当时对话的最后一行。
          两者在改方案那一轮指向同一句话，在别的轮不是

    默认剧本只有一份「已经够了」，所以那些不测澄清环的用例一次都不会绕进去。
    列表里最后一份是粘着的：取完不弹，之后每轮都给同一份 —— 「模型永远判不够」
    那个测试就是这么写的，否则要在剧本里堆一排一模一样的东西。

    换的是 nodes 模块上的 read_intent，不是 graph 上的 understand_intent ——
    也就是说这个节点本身照跑。一开始图省事把整个节点换掉了，代价是它的真身
    （读配置、拼参数、把 IntentResult 摊成 dict）一行覆盖都没有：把
    feedback=state["plan_feedback"] 改成 state["messages"][-1] 这种错，
    整套测试一条都不会红。跟 plan_queries / classify_reply 一个待遇。

    不换的话，跑一次测试就真去调一次 OpenAI：花的钱不多，但 CI 上没有密钥、
    而且模型每次读出来的东西不完全一样，这个文件测的是接线不是模型。
    """
    box = {"queue": [_intent_result()], "seen_feedback": None, "seen_last_line": None}

    def fake_read_intent(client, lines, *, feedback, config):
        queue = box["queue"]
        value = queue[0] if len(queue) == 1 else queue.pop(0)
        box["seen_feedback"] = feedback
        box["seen_last_line"] = lines[-1]
        print("[节点] understand_intent 执行（测试桩读需求）")
        return _as_intent_result(value)

    monkeypatch.setattr("triggers.interactive_discovery.query.nodes.read_intent", fake_read_intent)
    return box


def _as_intent_result(value: dict) -> IntentResult:
    """把剧本里那份 dict 变回 read_intent 真实现会返回的那种对象。

    输入：_intent_result() 给出来的那份 dict（形状跟 state 里的一模一样）
    输出：IntentResult

    剧本写成 dict 是因为测试里到处拿它跟 state 比；而这个桩顶的是 read_intent，
    那一层返回的是对象，摊成 dict 是节点自己调 to_state_dict 干的 —— 桩直接返回
    dict 的话，那一步就又被跳过去了，而它正是这次要覆盖的东西之一。
    """
    intent = value["intent"]
    return IntentResult(
        intent=DiscoveryIntent(
            goal=intent["goal"],
            topics=tuple(intent["topics"]),
            events=tuple(intent["events"]),
            exclusions=tuple(intent["exclusions"]),
            time_said=intent["time_said"],
            max_spend_usd=intent["max_spend_usd"],
            unsupported=tuple(intent["unsupported"]),
        ),
        clarification_question=value["clarification_question"],
    )


def _fresh(thread_id: str):
    """开一张新图、一条新对话，跑到第一个 interrupt 停下。

    输入：这条对话的 thread_id（同一个进程里各测试之间不能撞）
    输出：(app, config, 第一次 invoke 的返回值)

    每个测试自己建一张图：编译出来的图对象无状态，建一张不花什么，
    而共用一张会让「上一条对话留下的 checkpoint」串进来。
    """
    app = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": thread_id}}
    result = app.invoke(
        {
            "messages": [FIRST_LINE],
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
    return app, config, result


def _state(reply: str) -> InteractiveState:
    """拼一个 await_go 刚跑完的 state，用来单独调 classify_reply。

    输入：用户回的那句原话（不带说话人前缀）
    输出：一个 InteractiveState

    那句话同时落两处，跟 await_go 真实现做的一样：messages 里是渲染过的一行
    （带说话人前缀），plan_feedback 里是 interrupt 收到的原值。两处都给，是因为
    classify_reply 两样都读 —— 要判的那一句取自 plan_feedback，整条对话当上下文。

    另外几个字段填的是不影响判断的占位值，别把它们写成有意义的值：会让读测试的人
    以为它们参与了判断。
    """
    return {
        "messages": [FIRST_LINE, "AI: 方案 v1，…", f"用户: {reply}"],
        "intent_result": _intent_result(),
        "clarify_rounds": 0,
        "plan": {},
        "plan_feedback": reply,
        "decision": "",
        "run_id": None,
    }


def test_the_graph_has_exactly_the_three_branches_and_the_loop():
    app = build_graph(InMemorySaver())
    branches = {
        (e.source, e.target, e.data)
        for e in app.get_graph().edges
        if e.conditional and e.source == "classify_reply"
    }
    assert branches == {
        # 改那条绕回 understand_intent，不是 plan_queries：那句修改得先变成 intent 里的
        # exclusions，才拦得住、也才会出现在下一版的清单上（理由写在 graph.py 那张表上）
        ("classify_reply", "understand_intent", "replan"),
        ("classify_reply", "run_harvest", "run"),
        ("classify_reply", "__end__", "cancel"),
    }


def test_understand_intent_has_exactly_three_ways_out():
    """2a 那条分支的那张表：三个键、两个去处。

    ready 和 give_up 后果一样（都去拟方案），分开是为了让「问够了」和「问不动了」
    各有各的名字。判据跟 classify_reply 那条一样：route_intent 能返回的每个键，
    表里都得有；表里有一条永远走不到的路，那条路就是死的。
    """
    app = build_graph(InMemorySaver())
    ends = app.builder.branches["understand_intent"]["route_intent"].ends

    assert ends == {
        "ready": "retrieve_knowledge",
        "give_up": "retrieve_knowledge",
        "clarify": "ask_clarification",
    }


def test_the_drawn_graph_hides_one_of_those_three():
    """画出来的图不是那张表：同一对起点终点的两条虚线会被合成一条。

    实测留下的是后登记的 give_up，ready 那条在图上根本看不见。焊住它是因为这件事
    很容易让人得出反的结论 —— 看着图说「ready 那条边没接上」，然后去改一个没坏的地方。
    give_up 这一趟真正的现场是 route_intent 里那句 print，不在图上。
    """
    app = build_graph(InMemorySaver())
    drawn = {
        (e.target, e.data) for e in app.get_graph().edges if e.conditional and e.source == "understand_intent"
    }

    assert drawn == {("retrieve_knowledge", "give_up"), ("ask_clarification", "clarify")}


def test_knowledge_is_read_before_the_plan_is_drafted():
    """ready 那条路要先经过 retrieve_knowledge，再到 plan_queries。

    顺序反了的话 planner 手上没有那份负面知识，拟出带 manufacturer 的词也没人拦，
    而方案卡片看起来完全正常 —— 直到采回来一整批工厂的推广贴，钱已经花了。
    """
    app = build_graph(InMemorySaver())
    plain = {(e.source, e.target) for e in app.get_graph().edges if not e.conditional}

    assert ("retrieve_knowledge", "plan_queries") in plain
    # 没有一条边让 understand_intent 直接跳到 plan_queries
    assert ("understand_intent", "plan_queries") not in plain


def test_the_knowledge_lands_in_state():
    """retrieve_knowledge 返回的那份东西要真的进 state，下一轮还在。

    没声明这个字段的话，它返回的东西会被静默丢掉：图照跑，plan_queries 读到空，
    而 state.py 里少一行的症状就只有这个。
    """
    app, config, _ = _fresh("slack:C1:knowledge")

    snapshot = app.get_state(config).values

    assert snapshot["knowledge"] == STUB_KNOWLEDGE


def test_the_clarify_loop_goes_back_to_understand_intent():
    """问完必须绕回 understand_intent，不能直接去拟方案。

    直接去的话，用户刚补的那句话谁都没读过 —— 方案是照着他补话之前那份 intent
    拟的，而 messages 里明明有他那句话，看起来一切正常。
    """
    app = build_graph(InMemorySaver())
    plain = {(e.source, e.target) for e in app.get_graph().edges if not e.conditional}
    assert ("__start__", "understand_intent") in plain
    assert ("ask_clarification", "understand_intent") in plain


def test_the_routing_function_is_handed_only_the_state_copy():
    """判断函数第一个参数上的注解不是给人看的，库拿它决定给判断函数看什么。

    注解在，库只给「所有 channel 复制一份、盖上这个节点这一步的写」那份副本；
    注解不在，节点的返回值会被额外并进来一份，撞键时读到哪个要看情况。
    """
    app = build_graph(InMemorySaver())
    spec = app.builder.branches["classify_reply"]["route_by_decision"]
    assert spec.input_schema is InteractiveState


def test_saying_run_reaches_run_harvest(capsys):
    app, config, _ = _fresh("t:run")
    capsys.readouterr()

    result = app.invoke(Command(resume="跑"), config)

    assert result["decision"] == "run"
    assert "run_harvest" in capsys.readouterr().out
    # run_id 从 None 变成有值 = run_harvest 真的执行了，不只是被路由到
    assert result["run_id"] == STUB_RUN_ID
    assert app.get_state(config).next == ()


def test_saying_cancel_never_reaches_run_harvest(capsys):
    """取消那条路一分钱都不能花：run_harvest 连执行都不能被执行。"""
    app, config, _ = _fresh("t:cancel")
    capsys.readouterr()

    result = app.invoke(Command(resume="算了"), config)

    assert result["decision"] == "cancel"
    assert "run_harvest" not in capsys.readouterr().out
    assert result["run_id"] is None
    assert app.get_state(config).next == ()


def test_asking_for_a_change_goes_back_and_bumps_the_version(capsys):
    """「绕回 plan_queries」和「plan_queries 被重放」在日志里长得一模一样。

    分辨它们的唯一办法是 plan 里那个 version：重放不会让它变，绕回来才会。
    """
    app, config, first = _fresh("t:replan")
    assert first["plan"]["version"] == 1
    capsys.readouterr()

    result = app.invoke(Command(resume="别带 Kickstarter"), config)

    assert result["decision"] == "replan"
    assert result["plan"]["version"] == 2
    assert "plan_queries" in capsys.readouterr().out
    # 绕回来之后又停在同一个 interrupt 上，等用户看新的一版
    assert app.get_state(config).next == ("await_go",)
    assert result["__interrupt__"][0].value["plan"]["version"] == 2


def test_the_plan_carries_the_post_window_that_will_really_be_used():
    """用户没说时间（intent 里是空串）：plan 里放 discovery.yaml 的默认那一档，不给日期。

    方案卡最上面那一栏印的就是这几格，run_harvest 也从这里取、不重算 ——
    不然卡片说一个月、实际采的是另一个
    """
    from triggers.harvest import load_run_config

    app, config, _ = _fresh("t:window")
    plan = app.get_state(config).values["plan"]

    assert plan["posted_since"] is None
    assert plan["posted_limit"] == load_run_config().posted_within
    assert (plan["time_said"], plan["time_understood"]) == ("", False)


def test_plan_queries_does_not_run_again_when_the_graph_resumes(capsys):
    """恢复时只有被中断的那个节点从第一行重跑，前面跑完的不重放。

    这条破了的症状是每恢复一次就多付一次 planner 的钱，而输出看不出区别。
    """
    app, config, _ = _fresh("t:noreplay")
    capsys.readouterr()

    app.invoke(Command(resume="跑"), config)

    # 这一趟走的是 await_go -> classify_reply -> run_harvest，plan_queries 不在路上
    assert "plan_queries" not in capsys.readouterr().out


def test_a_reply_the_model_judged_as_run_reaches_run_harvest(replies):
    """判成 run 之后真的走到了采集那个节点，不只是路由过去。"""
    replies["table"] = {"就这样吧": "run"}
    app, config, _ = _fresh("t:model-run")

    result = app.invoke(Command(resume="就这样吧"), config)

    assert result["decision"] == "run"
    assert result["run_id"] == STUB_RUN_ID


def test_an_unrecognised_reply_falls_back_to_the_free_path(replies):
    """三条路里只有「改」不花钱，拿不准必须往那条上落。

    这条焊的是桩那张表的兜底方向，同时也是 prompts.CLASSIFY_SYSTEM 里写死的那条
    规矩：只有明确表示可以开始了才判 run。真实现里这条规矩靠 prompt，而 prompt
    没法在这里测；能在这里测的是「代码这一侧没有任何地方会把拿不准变成 run」。
    """
    for reply in ("别带 Kickstarter，其他 OK", "$0.15 是每条还是总共", ""):
        assert classify_reply(_state(reply))["decision"] == "replan"


def test_a_plan_with_no_usable_query_cannot_be_run(replies, capsys):
    """一条 query 都没有的时候不许判 run，哪怕模型说 run。

    这是确定性的事实，所以在代码里拦不靠模型。让它走下去的话，run_harvest 会开
    一行 run、拿零条 query 跑完、然后说「发现 0 家公司」—— 采集的钱没花，但那行
    run 是假的，而且用户会以为是市场上没有，不是我们没搜。
    """
    replies["table"] = {"跑": "run"}
    state = _state("跑") | {"plan": {"version": 1, "queries": []}}

    assert classify_reply(state)["decision"] == "replan"
    assert "一条 query 都没有" in capsys.readouterr().out

    # 有 query 的时候同一句话就是 run —— 证明改判的原因是那个空列表，不是别的
    ok = _state("跑") | {"plan": {"version": 1, "queries": [{"text": "a", "filters": {}}]}}
    assert classify_reply(ok)["decision"] == "run"


def test_the_decision_values_and_the_branch_table_are_one_set():
    """reply.DECISIONS 和那张表的键必须逐字相同。

    多一个取值而表里没有，症状是用户回了那句话才 KeyError；表里多一条而取值里
    没有，那条路就是死的 —— 两个方向都不报错，都要等真跑到才发作。
    """
    app = build_graph(InMemorySaver())
    ends = app.builder.branches["classify_reply"]["route_by_decision"].ends

    assert set(DECISIONS) == set(ends)


def test_the_model_cannot_invent_a_fourth_decision():
    """模型返回一个第四种值的时候当场炸，不是被善意地折成某一条路。

    schema 里 decision 已经是 enum，这一遍是我们自己守的。真漏过来一个，
    条件边那张表会 KeyError，而那句报错说不出它是从哪儿来的。
    更要紧的是方向：折成 run 就是静默花钱。
    """
    with pytest.raises(ReplyCallFailed, match="合法值只有"):
        parse_reply({"decision": "RUN", "reason": "大小写不一样"})


def test_the_routing_function_just_reads_the_field():
    """判断函数自己不做判断：判成哪一种是 classify_reply 的活，它只负责说出结论。"""
    assert route_by_decision(_state("跑") | {"decision": "run"}) == "run"


def _routing_state(topics, rounds) -> InteractiveState:
    """拼一个只有 intent_result 和 clarify_rounds 是真的的 state，用来单独调 route_intent。

    输入：这一轮读出来的品类词；已经问过几轮
    输出：一个 InteractiveState

    别的字段填的是不影响路由的值 —— route_intent 只读这两样，把别的也写成有意义的值
    会让读测试的人以为它们参与了判断。
    """
    return {
        "messages": [FIRST_LINE],
        "intent_result": _intent_result(topics=topics),
        "clarify_rounds": rounds,
        "plan": {},
        "decision": "",
        "run_id": None,
    }


def test_route_intent_reads_exactly_two_things():
    """三个实际值，跟 route_intent 那三条路一一对应。"""
    cap = load_intent_config().max_clarify_rounds

    # 有品类词：够了，去拟方案
    assert route_intent(_routing_state(("bone conduction",), 0)) == "ready"
    # 没品类词、还没问满：再问一句
    assert route_intent(_routing_state((), cap - 1)) == "clarify"
    # 没品类词、已经问满：不问了，带着现有的去拟方案
    assert route_intent(_routing_state((), cap)) == "give_up"


def test_every_key_route_intent_can_return_has_a_branch():
    """route_intent 的取值和那张表的键必须是同一套（跟 classify_reply 那条同一个理由）。

    多一个取值而表里没有，症状是当场 KeyError，而且只在走到那条路的那一轮才发生。
    """
    app = build_graph(InMemorySaver())
    ends = app.builder.branches["understand_intent"]["route_intent"].ends
    cap = load_intent_config().max_clarify_rounds

    produced = {
        route_intent(_routing_state(topics, rounds))
        for topics, rounds in [(("bone conduction",), 0), ((), 0), ((), cap)]
    }
    assert produced == set(ends)


def test_the_routing_function_is_handed_only_the_state_copy_2a():
    """跟 classify_reply 那条一样：第一个参数上的注解决定库给判断函数看什么。"""
    app = build_graph(InMemorySaver())
    spec = app.builder.branches["understand_intent"]["route_intent"]
    assert spec.input_schema is InteractiveState


def test_missing_information_stops_at_a_question(intents):
    """第一轮读不出品类词，图该停在 ask_clarification 上，而不是往下拟方案。"""
    intents["queue"] = [_intent_result(topics=()), _intent_result(topics=("bone conduction",))]

    app, config, first = _fresh("t:clarify")

    assert app.get_state(config).next == ("ask_clarification",)
    assert first["__interrupt__"][0].value == {"kind": "clarify", "question": QUESTION}
    # 方案还没拟：这一轮连 plan_queries 都没走到
    assert first["plan"] == {}
    # 挂起那一趟 ask_clarification 没走到 return，所以这个数还是 0
    assert first["clarify_rounds"] == 0
    # 问题本身也还没进 messages —— 它现在只是 interrupt 的 payload
    assert first["messages"] == [FIRST_LINE]


def test_the_answer_and_the_question_both_land_in_messages(intents, capsys):
    """用户答完，一问一答一起进 messages，然后绕回 understand_intent 重读。

    只写答话不写问题也不行：「骨传导耳机」这句答话脱离了那句问题就没法读。
    """
    intents["queue"] = [_intent_result(topics=()), _intent_result(topics=("bone conduction",))]
    app, config, _ = _fresh("t:answer")
    capsys.readouterr()

    result = app.invoke(Command(resume="骨传导耳机"), config)

    # 前三句是这一轮的问答本身。第四句是 plan_queries 接着拟完方案说的那句 ——
    # 这一次 invoke 一口气走完了 ask_clarification -> understand_intent -> plan_queries
    assert result["messages"][:3] == [FIRST_LINE, f"AI: {QUESTION}", "用户: 骨传导耳机"]
    assert result["messages"][3].startswith("AI: 方案 v1")
    # 这一轮真的绕回去重读了一次，而且只读了一次 —— understand_intent 没有 interrupt，
    # 所以它不会跟着 ask_clarification 一起被重放。方案 A 省不掉的就是这一次
    assert capsys.readouterr().out.count("understand_intent 执行") == 1
    # 读到品类词了，于是继续往下走，停在拍板那个 interrupt 上
    assert app.get_state(config).next == ("await_go",)
    assert result["plan"]["version"] == 1


def test_a_resumed_question_counts_as_one_round_not_two(intents):
    """ask_clarification 带 interrupt，恢复时整个节点从第一行重跑。

    这个数每轮该只加一次。加两次的话上限提前触发，而且没有任何报错 ——
    症状是「明明只问了两次就不问了」，而那时候已经在用户面前了。
    """
    intents["queue"] = [_intent_result(topics=())]
    app, config, _ = _fresh("t:rounds")

    first = app.invoke(Command(resume="不知道"), config)
    assert first["clarify_rounds"] == 1

    second = app.invoke(Command(resume="还是不知道"), config)
    assert second["clarify_rounds"] == 2


def test_the_clarify_loop_gives_up_instead_of_looping_forever(intents, capsys):
    """模型永远判「还差东西」的时候，图要自己收口，不能撞 recursion_limit。

    问满上限之后带着手上这份不完整的 intent 去拟方案：后面还有 await_go 那道人拍板
    的闸门，让用户对着方案说话比继续追问有效。
    """
    # 剧本里只有一份，而且是粘着的：无论问几轮，模型都读不出品类词
    intents["queue"] = [_intent_result(topics=())]
    cap = load_intent_config().max_clarify_rounds

    app, config, _ = _fresh("t:giveup")
    for i in range(cap):
        assert app.get_state(config).next == ("ask_clarification",)
        result = app.invoke(Command(resume=f"第 {i + 1} 次也说不清"), config)

    # 问满之后不再问，而是去拟了方案，停在拍板那个 interrupt 上
    assert result["clarify_rounds"] == cap
    assert app.get_state(config).next == ("await_go",)
    assert result["plan"]["version"] == 1
    # 问过的次数正好是上限，不多不少
    assert result["messages"].count(f"AI: {QUESTION}") == cap
    # give_up 这一趟在画出来的图上看不见，唯一的现场就是这句话
    assert "澄清已经问满" in capsys.readouterr().out


def test_both_interrupts_say_which_kind_they_are(intents):
    """两个 interrupt 分属两个节点，图自己不会搞混，但外面要知道该怎么渲染。

    澄清是发一句提问，拍板是发方案和报价，两种在 Slack 里长得完全不一样。
    """
    intents["queue"] = [_intent_result(topics=()), _intent_result(topics=("bone conduction",))]
    app, config, first = _fresh("t:kinds")

    assert first["__interrupt__"][0].value["kind"] == "clarify"

    second = app.invoke(Command(resume="骨传导耳机"), config)
    payload = second["__interrupt__"][0].value
    assert payload["kind"] == "go"
    assert payload["estimated_cost_usd"] == payload["plan"]["estimated_cost_usd"]


def test_every_planned_query_carries_its_own_filters():
    """方案里一条 query 是 {text, filters} 不是一个裸字符串，这条形状要焊住。

    run_harvest 按这个形状取值（item['text'] / item['filters']），而它在测试里
    被整个换成了桩 —— 所以形状变了不会有任何测试红，直到真跑那一刻 KeyError。
    这里从生产这一头焊：plan_queries 产出什么形状，就是契约。

    旋钮按条带不是整跑一套，因为它参与 query 的身份（0009 的唯一键含 filters）：
    同一句词配不同旋钮采回来的是两批帖子，在库里就是两条 query。
    """
    app = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "shape"}}

    app.invoke(
        {
            "messages": ["用户: 帮我找找会议麦克风的初创"],
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

    queries = app.get_state(config).values["plan"]["queries"]

    assert queries, "方案里一条 query 都没有"
    for item in queries:
        assert set(item) == {"text", "filters"}, item
        assert isinstance(item["text"], str) and item["text"].strip()
        # 旋钮过一遍进库那一关的同一个校验：拟出来的东西必须真的存得进去
        validate_filters(item["filters"])


def test_the_reply_to_a_plan_is_kept_in_its_own_field(plans):
    """await_go 收到的那个值要原样存进 plan_feedback，下一轮两个节点都要读它。

    存两份（messages 里一份、这一格一份）是刻意的：绕几轮之后对话里我们自己复述的
    字数远多于用户说的，得有个地方指出哪一句是新的。
    """
    app, config, _ = _fresh("t:feedback")

    app.invoke(Command(resume="别带 Kickstarter"), config)

    values = app.get_state(config).values
    assert values["plan_feedback"] == "别带 Kickstarter"
    # 原样存，不带说话人前缀 —— 前缀是我们加的，而它本身还是个待定项
    assert values["messages"][-2] == "用户: 别带 Kickstarter"
    # 而且它真的到了下一版拟词那一步手上
    assert plans["seen_feedback"] == "别带 Kickstarter"


def test_deciding_to_run_or_cancel_clears_the_feedback(replies):
    """「跑」和「算了」不是「要改方案」。

    不清掉的话它会跟着 checkpoint 一直躺在那儿，等这条 thread 下次被唤醒时
    冒充成一条反馈 —— 那时候拟出来的方案会照着一句早就过时的话改，而且不报错。
    """
    replies["table"] = {"跑": "run"}
    app, config, _ = _fresh("t:clear")

    app.invoke(Command(resume="跑"), config)

    assert app.get_state(config).values["plan_feedback"] == ""


def test_the_feedback_is_empty_before_any_plan_was_shown():
    """第一次走到 plan_queries 的时候还没人看过方案，这一格必须是空的。

    非空的话 planner 那边会以为这是在改不是在拟（prompt 第六节按它分岔）。
    """
    app, config, _ = _fresh("t:first")

    assert app.get_state(config).values["plan_feedback"] == ""


def test_the_last_line_alone_cannot_tell_these_three_apart(intents):
    """三种情况下 messages 都以「用户: …」结尾，只有 plan_feedback 分得出是哪一种。

    这条焊的是 plan_feedback 存在的理由本身。它要是被换成「读 messages 最后一句」，
    下面三行里的前两行会被当成对方案的意见 —— 而那两句一个是开场白、一个是在答
    我们的澄清问题，两种都不是「要改方案」。
    """
    intents["queue"] = [_intent_result(topics=()), _intent_result(topics=("bone conduction",))]

    # 一、第一次开口，停在澄清那个 interrupt 上
    app, config, _ = _fresh("t:three")
    assert app.get_state(config).values["messages"][-1].startswith("用户: ")
    assert app.get_state(config).values["plan_feedback"] == ""

    # 二、答完一句澄清，方案拟出来了，停在拍板那个 interrupt 上
    app.invoke(Command(resume="骨传导耳机"), config)
    values = app.get_state(config).values
    assert values["messages"][-2] == "用户: 骨传导耳机"
    assert values["plan_feedback"] == ""

    # 三、看完方案回话，这一句才是对方案的意见
    app.invoke(Command(resume="第二条不要了"), config)
    values = app.get_state(config).values
    assert values["messages"][-2] == "用户: 第二条不要了"
    assert values["plan_feedback"] == "第二条不要了"


def test_the_two_copies_of_that_sentence_do_not_drift():
    """plan_feedback 跟 messages 里那一行是同一句话的两份，这里焊住它们不许漂。

    存两份的代价就是这个：没人拦的话，哪天有人改了其中一处的写法，另一处不会跟着改，
    而症状是模型看见的「最新那一句」跟对话里那一行对不上 —— 不报错。

    对账的办法是剥掉前缀。这一行同时也是那个前缀今天唯一被反向解析的地方：
    哪天它换成真实说话人（state.py 开头那条待定），这条测试会第一个红。
    """
    app, config, _ = _fresh("t:drift")

    app.invoke(Command(resume="别带 Kickstarter"), config)

    values = app.get_state(config).values
    # messages 里那一行是渲染过的，plan_feedback 是 interrupt 收到的原值
    assert values["messages"][-2] == "用户: " + values["plan_feedback"]


def test_understand_intent_is_handed_the_field_not_the_last_line(intents):
    """这个节点交给 read_intent 的那一句，必须是 plan_feedback 那一格，
    不能是「messages 最后一句」。

    两者在改方案那一轮长得一样，所以换成读最后一句不会立刻出错 —— 出错的是另外
    两种：第一次开口和刚答完澄清，那两轮的最后一句也是「用户: …」，但它们不是
    对方案的意见。换过去之后模型每一轮都会被告知「这一轮要照着它改」，
    连第一句开场白都算，而全程不报错。
    """
    intents["queue"] = [_intent_result(topics=("bone conduction",))]

    # 一、第一次开口：这一轮对话的最后一行是他的开场白，而交出去的反馈是空串 ——
    #     两者在这一轮不是同一样东西，换成读最后一行就会把开场白当成对方案的意见
    app, config, _ = _fresh("t:hands-first")
    assert intents["seen_last_line"] == FIRST_LINE
    assert intents["seen_feedback"] == ""

    # 二、看完方案回话：这一轮两者才指向同一句话，而交出去的那份不带说话人前缀
    app.invoke(Command(resume="别带 Kickstarter"), config)
    assert intents["seen_last_line"] == "用户: 别带 Kickstarter"
    assert intents["seen_feedback"] == "别带 Kickstarter"


def test_a_run_that_gets_forced_back_does_not_become_a_change_request(replies):
    """他对着一版空方案说「跑」，那句「跑」不许被当成修改意见留下来。

    那道闸把 run 改判成 replan，于是下一轮又会走一遍 understand_intent 和
    plan_queries。留着的话那两次调用会被明确告知「他刚看完方案，这一轮要照着它改：
    跑」—— 一句没有意义的指令，纯噪音，而且不报错。

    跟正常的 replan 对照：那一种必须留着，那一句正是「要我们怎么改」。
    """
    replies["table"] = {"跑": "run"}
    empty = _state("跑") | {"plan": {"version": 1, "queries": []}}

    assert classify_reply(empty) == {"decision": "replan", "plan_feedback": ""}

    # 对照：真的要改的那一句留着，下一轮两个节点都要读它
    replies["table"] = {}
    assert classify_reply(_state("别带众筹的")) == {"decision": "replan"}


def test_every_invoke_hands_the_graph_a_complete_state():
    """每一处 invoke 的初始 state，都要把 InteractiveState 的字段给全。

    LangGraph 只把「已经有值的 channel」交给节点，没给的 key 在节点里读是 KeyError。
    少给一个今天可能照样跑得动（写它的节点排在读它的节点前面），但那是碰巧 ——
    哪天顺序变了，或者有个节点提前读它，就只炸那一条路，而且只炸那几处漏给的 invoke。

    这条是加字段那天的安全网：今天加 plan_feedback 就要同时改四处调用，
    漏一处的症状是「别人的测试全绿，只有我这条路炸」。用 ast 看代码，
    因为那几处大多在测试自己里，跑是跑不到的。
    """
    roots = (pathlib.Path("scripts"), pathlib.Path("tests"))
    declared = set(InteractiveState.__annotations__)
    missing = []

    for root in roots:
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "invoke"
                    and node.args
                    and isinstance(node.args[0], ast.Dict)
                ):
                    continue
                keys = {k.value for k in node.args[0].keys if isinstance(k, ast.Constant)}
                # 只看真的在传一份 state 的那几处：Command(resume=...) 那种没有 messages
                if "messages" not in keys:
                    continue
                gap = declared - keys
                if gap:
                    missing.append(f"{path.as_posix()}:{node.lineno} 少了 {sorted(gap)}")

    assert not missing, "这几处 invoke 传的 state 不全：" + "；".join(missing)


def test_that_check_would_have_caught_something():
    """上面那条是拿 ast 看代码的，跑不到的地方也查得到 —— 但它自己也可能什么都没扫到。

    所以这里确认它真的扫到了 invoke：一个都没扫到的话，上面那条会永远绿。
    """
    found = 0
    for root in (pathlib.Path("scripts"), pathlib.Path("tests")):
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "invoke"
                    and node.args
                    and isinstance(node.args[0], ast.Dict)
                    and any(isinstance(k, ast.Constant) and k.value == "messages" for k in node.args[0].keys)
                ):
                    found += 1

    assert found >= 3, f"只扫到 {found} 处 invoke，比预期少 —— 要么代码挪了，要么扫描写坏了"


def test_after_harvest_it_finds_contacts_then_delivers(capsys):
    """说「跑」之后的顺序：run_harvest -> find_contacts -> deliver。

    find_contacts 必须在 deliver 前面：deliver 拼卡片时从库里读它写进去的联系方式，
    顺序反了卡片上的联系方式永远是上一次的
    """
    app, config, _ = _fresh("t:run-order")
    capsys.readouterr()

    app.invoke(Command(resume="跑"), config)

    out = capsys.readouterr().out
    names = ("run_harvest", "find_contacts", "deliver")
    order = [out.index(f"[节点] {name} 执行（测试桩）") for name in names]
    assert order == sorted(order)
