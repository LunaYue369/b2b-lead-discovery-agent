"""classify_reply 那一步：配置、schema、拼消息、解析、那一次调用。

这三次 LLM 调用里只有这一次的结果直接决定要不要花钱（判成 run 的下一步就是采集），
所以这个文件焊的几类全都朝同一个方向 —— 不许有任何一条路把「拿不准」变成 run：

    第四种取值被收下   schema 里 decision 是 enum，这一层再查一遍。真漏过来一个，
                       条件边那张表会 KeyError；而如果它被善意地折成 run，那就是
                       静默花钱
    坏回答被当成好回答 截断、拒答、空内容、非法 JSON 四种。任意一种被吞掉，
                       下一步就是拿着一个瞎猜的结论去决定花不花钱
    只看最后一句       他上一句刚提了一个条件、这一句只回一个「好」的时候，那个
                       「好」是在确认那个条件，不是在说整版方案可以跑了
    方案喂多了         assumptions 是我们写给他看的清单，喂回模型只会让它跟着复述
                       我们的措辞，而它要判的是他说了什么
    要判哪一句靠约定   写成「取最后一句」的话，它只活在 prompt 里；哪天有人从别处
                       接一条边进 classify_reply，模型会去判一句不该判的话，不报错
    配置和代码漂了     config/interactive.yaml 少一个键，报错会发生在 OpenAI 那一侧，
                       离真正的原因隔着一整个网络请求

判得准不准这件事这里测不了，那要真调模型。这里测的是「代码这一侧没有任何地方会
把拿不准变成 run」。

不连库、不调模型：client 是个照剧本回答的假的，config 用的是仓库里那份真的 yaml。
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from triggers.interactive_discovery.query.reply import (
    DECISIONS,
    REPLY_SCHEMA,
    ReplyCallFailed,
    ReplyConfig,
    build_reply_messages,
    load_reply_config,
    parse_reply,
    read_decision,
)

LINES = (
    "用户: 帮我找找骨传导耳机的初创",
    "AI: 方案 v1，1 条 query，预计 $0.08。这版抓的是刚做完众筹那个时刻。",
    "用户: 行吧就这样",
)

# 要判的就是这一句。它跟 LINES 最后一行是同一句话，只是不带说话人前缀。
FEEDBACK = "行吧就这样"

PLAN = {
    "version": 1,
    "queries": [{"text": "Kickstarter AND bone conduction", "filters": {"authorKeywords": "Founder OR CTO"}}],
    "assumptions": "我理解的需求：找刚做完众筹的骨传导耳机团队",
    "estimated_cost_usd": 0.08,
}

CONFIG = ReplyConfig(model="gpt-4o-mini", max_tokens=200, temperature=0.0, request_timeout_seconds=15.0)

# 「这个参数没给」和「这个参数给的就是 None」是两件事：后者正是要测的那种失败
# （模型没返回内容）。
_KEEP = object()


class _FakeClient:
    """一个只会照着剧本回答的 client，形状跟 tests/test_intent.py 那个一样。"""

    def __init__(self, *, content=_KEEP, finish_reason="stop", refusal=None, boom=None):
        self._content = None if content is _KEEP else content
        self._finish_reason = finish_reason
        self._refusal = refusal
        self._boom = boom
        self.seen: dict = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.seen = kwargs
        if self._boom is not None:
            raise self._boom
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self._finish_reason,
                    message=SimpleNamespace(content=self._content, refusal=self._refusal),
                )
            ]
        )


# ---------- schema ----------


def test_every_property_is_listed_in_required():
    """strict 模式的硬要求。漏一个，模型就获得了「这个键可以不给」的自由。"""
    schema = REPLY_SCHEMA["schema"]

    assert REPLY_SCHEMA["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_the_schema_enum_and_the_code_are_one_set():
    """schema 里那个闭集和 DECISIONS 必须逐字相同。

    两边漂了的症状分两种：schema 宽了，模型能返回一个 parse_reply 不认的值；
    schema 窄了，代码里有一条永远走不到的路。两种都不报错。
    """
    assert REPLY_SCHEMA["schema"]["properties"]["decision"]["enum"] == list(DECISIONS)


def test_the_cheapest_path_is_one_of_the_choices():
    """replan 是三条里唯一不花钱的那条，prompt 和代码都往它那边倒。

    它要是哪天从取值里消失了，「拿不准往不花钱那边倒」这句话就没有落点了。
    """
    assert "replan" in DECISIONS


# ---------- 拼消息 ----------


def test_the_whole_conversation_goes_in_not_just_the_last_line():
    """他上一句刚提了条件、这一句只回一个「好」的时候，两种情况只看最后一句
    长得一模一样。
    """
    payload = json.loads(build_reply_messages(LINES, PLAN, feedback=FEEDBACK)[1]["content"])

    assert payload["conversation"] == list(LINES)


def test_only_three_things_about_the_plan_are_fed():
    """assumptions 是我们写给他看的清单，喂回去只会让模型复述我们的措辞。

    queries 只给正文不给旋钮：他是对着那几句话拍的板。
    """
    payload = json.loads(build_reply_messages(LINES, PLAN, feedback=FEEDBACK)[1]["content"])

    assert set(payload["plan"]) == {"version", "queries", "estimated_cost_usd"}
    assert payload["plan"]["queries"] == ["Kickstarter AND bone conduction"]


def test_an_empty_plan_does_not_crash_the_message():
    """第一版还没拟出来就被调到（不该发生，但别让它炸在拼字符串这一步）。"""
    payload = json.loads(build_reply_messages(LINES, {}, feedback=FEEDBACK)[1]["content"])

    assert payload["plan"]["queries"] == []


def test_the_payload_keeps_chinese_readable():
    """ensure_ascii 开着的话整段对话会变成一串转义码，而那正是出事时第一个要看的。"""
    assert "行吧就这样" in build_reply_messages(LINES, PLAN, feedback=FEEDBACK)[1]["content"]


# ---------- 解析 ----------


def test_a_good_answer_parses():
    judgement = parse_reply({"decision": "run", "reason": "  他说「跑」，没有附加条件  "})

    assert judgement.decision == "run"
    assert judgement.reason == "他说「跑」，没有附加条件"


def test_every_legal_decision_parses():
    for value in DECISIONS:
        assert parse_reply({"decision": value, "reason": "x"}).decision == value


def test_an_unknown_decision_blows_up_instead_of_being_rounded():
    """最要紧的是方向：一个没见过的取值如果在这里被善意地折成 run，那就是静默花钱。

    大小写也算没见过 —— 条件边那张表是精确匹配的。
    """
    for bad in ("RUN", "Run", "go", "", "改"):
        with pytest.raises(ReplyCallFailed, match="合法值只有"):
            parse_reply({"decision": bad, "reason": "x"})


def test_a_missing_field_blows_up_with_the_answer_attached():
    with pytest.raises(ReplyCallFailed, match="少了字段"):
        parse_reply({"decision": "run"})

    with pytest.raises(ReplyCallFailed, match="少了字段"):
        parse_reply({"reason": "x"})


# ---------- 那一次调用 ----------


def test_a_good_call_comes_back_parsed():
    client = _FakeClient(content=json.dumps({"decision": "run", "reason": "他说可以开始"}))

    judgement = read_decision(client, LINES, PLAN, feedback=FEEDBACK, config=CONFIG)

    assert judgement.decision == "run"
    # 发出去的那几个参数照配置走，不是各处写死的
    assert client.seen["model"] == CONFIG.model
    assert client.seen["max_tokens"] == CONFIG.max_tokens
    assert client.seen["temperature"] == CONFIG.temperature
    assert client.seen["timeout"] == CONFIG.request_timeout_seconds
    assert client.seen["response_format"]["json_schema"] is REPLY_SCHEMA


def test_a_truncated_answer_is_not_treated_as_an_answer():
    client = _FakeClient(content='{"decision": "ru', finish_reason="length")

    with pytest.raises(ReplyCallFailed, match="截断"):
        read_decision(client, LINES, PLAN, feedback=FEEDBACK, config=CONFIG)


def test_a_refusal_is_not_treated_as_an_answer():
    client = _FakeClient(content=None, refusal="我不能帮你做这个")

    with pytest.raises(ReplyCallFailed, match="拒答"):
        read_decision(client, LINES, PLAN, feedback=FEEDBACK, config=CONFIG)


def test_no_content_is_not_treated_as_an_answer():
    with pytest.raises(ReplyCallFailed, match="没有返回内容"):
        read_decision(_FakeClient(content=None), LINES, PLAN, feedback=FEEDBACK, config=CONFIG)


def test_a_non_json_answer_brings_the_original_text_along():
    client = _FakeClient(content="他好像是同意了")

    with pytest.raises(ReplyCallFailed, match="他好像是同意了"):
        read_decision(client, LINES, PLAN, feedback=FEEDBACK, config=CONFIG)


def test_a_json_that_is_not_an_object_blows_up_here_not_later():
    with pytest.raises(ReplyCallFailed, match="不是一个对象"):
        read_decision(_FakeClient(content="3"), LINES, PLAN, feedback=FEEDBACK, config=CONFIG)


def test_a_transport_error_is_wrapped_not_leaked():
    """这一层不 import openai 也不认它的异常类型。"""
    client = _FakeClient(boom=TimeoutError("read timeout"))

    with pytest.raises(ReplyCallFailed, match="请求失败"):
        read_decision(client, LINES, PLAN, feedback=FEEDBACK, config=CONFIG)


def test_a_failed_call_does_not_fall_back_to_a_guess():
    """失败了就失败，不退回一张关键词表。

    退回去是静默降级，而降级之后判错的那一次是要花钱的。宁可让用户再说一句话 ——
    他那句原话已经在 messages 里落了库，重说一次接着往下走。
    """
    client = _FakeClient(boom=TimeoutError("read timeout"))

    with pytest.raises(ReplyCallFailed):
        read_decision(client, [*LINES[:-1], "用户: 跑"], PLAN, feedback="跑", config=CONFIG)


# ---------- 配置 ----------


def test_config_reads_the_repo_file():
    config = load_reply_config()

    assert config.model
    assert config.max_tokens > 0
    assert config.temperature == 0.0
    assert config.request_timeout_seconds > 0


def test_this_call_times_out_sooner_than_the_other_two():
    """用户刚敲完「跑」，正盯着屏幕。挂在那儿比失败更难受。

    这一条同时焊住「三份配置各有各的超时」这件事：合成一份的话，给 analyzer 调宽
    会连带让这一步也挂得更久。
    """
    from triggers.interactive_discovery.query.intent import load_intent_config
    from triggers.interactive_discovery.query.planner import load_planner_config

    assert load_reply_config().request_timeout_seconds < load_intent_config().request_timeout_seconds
    assert load_reply_config().request_timeout_seconds < load_planner_config().request_timeout_seconds


def test_a_missing_key_is_not_papered_over(tmp_path):
    """退化成默认值的后果是「实际用的数跟配置说的不一样」，而它不报错。"""
    path = tmp_path / "interactive.yaml"
    path.write_text("reply:\n  model: gpt-4o-mini\n", encoding="utf-8")

    with pytest.raises(KeyError):
        load_reply_config(path)


def test_the_repo_config_has_every_key_the_code_reads(tmp_path):
    """仓库那份配置和这个函数读的键必须对得上。

    上面那条用的是临时文件，证明不了仓库里那份是全的 —— 而少一个键的症状是
    用户在 Slack 里等了十几秒，拿回一句 KeyError。
    """
    assert load_reply_config(pathlib.Path("config/interactive.yaml"))


def test_the_payload_keys_line_up_with_the_state_fields():
    """跟 tests/test_planner.py 那条同一个理由：出事的时候要能拿这份 payload
    直接对着 state 看。

    plan_feedback 和 plan 照抄 state 的字段名。conversation 装的是 state['messages']，
    名字让给了 OpenAI 那份消息列表（这个文件里 messages 指的是那个）。
    plan 不用叫 previous_plan —— 这个文件不产出方案，不会跟别的混。
    """
    payload = json.loads(build_reply_messages(LINES, PLAN, feedback=FEEDBACK)[1]["content"])

    assert set(payload) == {"plan_feedback", "conversation", "plan"}
    assert payload["plan_feedback"] == FEEDBACK
    assert payload["conversation"] == list(LINES)
    assert payload["plan"]["version"] == PLAN["version"]


def test_the_sentence_being_judged_is_an_input_not_a_position():
    """要判哪一句是明写进 payload 的，不是靠「取 conversation 最后一句」。

    这两者今天给出同一句话（classify_reply 只有一条入边，就是 await_go），所以
    换成位置约定不会立刻出错。它拦的是以后：哪天有人从别处接一条边进 classify_reply，
    最后一句就不再是他刚回的那句了，而模型会认认真真去判一句不该判的话 —— 不报错。

    这里把两者故意错开，证明代码真的读的是那一格，不是最后一句。
    """
    payload = json.loads(
        build_reply_messages(
            ["用户: 帮我找点公司", "AI: 方案 v1，…", "用户: 这是对话里的最后一句"],
            PLAN,
            feedback="这才是要判的那一句",
        )[1]["content"]
    )

    assert payload["plan_feedback"] == "这才是要判的那一句"
    assert payload["conversation"][-1] == "用户: 这是对话里的最后一句"


def test_the_sentence_being_judged_comes_first_in_the_payload():
    """它是这次调用的主语，另外两样是上下文。排前面只是给读日志的人看的 ——
    JSON 的键没有顺序语义，但 json.dumps 是照插入顺序写的，人读的时候第一眼
    看见的就该是要判的那句话。
    """
    content = build_reply_messages(LINES, PLAN, feedback=FEEDBACK)[1]["content"]

    assert content.index("plan_feedback") < content.index("conversation")
