"""understand_intent 那次 LLM 调用的这一层：schema、解析、够不够、配置。

焊住的六类，全是「不报错，但要等用户真开口那一刻才发作」：

    schema 和 required 漂了   strict 模式要求 required 列全每一个 property。漏一个，
                              模型可以整个不返回那个键，解析当场炸 —— 而这要等到
                              某次模型正好省掉那个字段才发生
    上限是个反向的数          max_spend_usd 是花钱那道闸门要拿来比大小的数，而
                              schema 里它只是一个 number。负数让闸门永远拦，
                              NaN 让闸门永远放行（跟任何数比都是 False）——
                              后一种会真的花钱，而且报表上完全正常
    空串混进列表              模型偶尔给 [""]。留着的话 topics 就算「非空」，
                              is_ready 判成够了，planner 拿一个空词去拟 query，
                              然后真花钱。一条不报错的路
    time_said 被改写          它只许是原话。换算成日期是代码的事（time_window），
                              schema 里再给它套一个 enum，模型会被前缀带跑（「最近 2 个月」变成 24h）
    够不够让模型说了算        同一份 intent 两次调用给出不同答案，事后无从查
    坏回答被当成好回答        截断、拒答、空内容、非法 JSON 四种，任意一种被吞掉，
                              下一步就是拿着一份空 intent 去拟方案、去花钱
    配置和代码漂了            config/interactive.yaml 少一个键，报错会发生在 OpenAI
                              那一侧，离真正的原因隔着一整个网络请求

不连库、不调模型：client 是个假的，config 用的是仓库里那份真的 yaml。
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from triggers.interactive_discovery.query.intent import (
    INTENT_SCHEMA,
    MAX_SPEND_CEILING_USD,
    DiscoveryIntent,
    IntentCallFailed,
    IntentConfig,
    IntentResult,
    build_messages,
    is_ready,
    load_intent_config,
    parse_intent_result,
    read_intent,
    to_state_dict,
)

# 一份模型返回的完整 payload。用户说的是「帮我找找骨传导耳机的初创，最好是刚做完
# 众筹的，别带那种预热帖，这次别超过两毛钱」。
FULL_PAYLOAD = {
    "intent": {
        "goal": "找刚做完众筹、正需要代工厂的骨传导耳机团队",
        "topics": ["bone conduction", "bone conduction headphone"],
        "events": ["刚做完众筹"],
        "exclusions": ["pre-launch"],
        "time_said": "",
        "max_spend_usd": 0.2,
        "unsupported": [],
    },
    "clarification_question": "要不要把颈挂式的蓝牙耳机也算进来？",
}

CONFIG = IntentConfig(
    model="gpt-4o-mini",
    max_tokens=800,
    temperature=0.0,
    request_timeout_seconds=30.0,
    max_clarify_rounds=3,
)


# 「这个参数没给」和「这个参数给的就是 None」是两件事：后者正是要测的那种失败
# （模型没返回内容）。用 None 当默认值的话，这两种情况在函数里长得一模一样。
_KEEP = object()


class _FakeClient:
    """一个只会照着剧本回答的 client。

    输入：构造时给这一次要返回的 payload（或者直接给一段原文），外加 finish_reason
    输出：一个长得像 openai 返回值的对象（choices[0].message.content / refusal）

    真 client 的形状里我们只用到这几层，所以只造这几层。这一层本来就不认
    openai 的类型（read_intent 收的是「一个能 chat 的对象」），假的真的都一样。
    """

    def __init__(self, payload=None, *, content=_KEEP, finish_reason="stop", refusal=None):
        # content 给了就用给的那一段原文（包括显式给 None，那是「模型没返回内容」
        # 那一种失败）；没给就把 payload 序列化成 JSON
        self._content = json.dumps(payload) if content is _KEEP else content
        self._finish_reason = finish_reason
        self._refusal = refusal
        self.kwargs: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self._finish_reason,
                    message=SimpleNamespace(content=self._content, refusal=self._refusal),
                )
            ]
        )


class _BoomClient:
    """调一下就抛的 client，模拟 SDK 那一侧的失败（网络、额度、密钥）。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._boom))

    def _boom(self, **kwargs):
        raise TimeoutError("read timeout")


# ───────────────────────────────────────────── schema


def test_every_property_is_listed_in_required():
    """strict 模式的硬要求：required 必须列全 properties 的每一个键。

    漏一个的症状不是报错，是模型获得了「这个字段可以不给」的自由 —— 它多数时候
    还是会给，直到某次不给，解析就 KeyError 在用户面前。

    两层都要查：外面那层（intent / clarification_question）和里面那层七个字段。
    """
    outer = INTENT_SCHEMA["schema"]
    assert set(outer["required"]) == set(outer["properties"])
    assert outer["additionalProperties"] is False

    inner = outer["properties"]["intent"]
    assert set(inner["required"]) == set(inner["properties"])
    assert inner["additionalProperties"] is False


def test_time_said_is_free_text_not_a_closed_set():
    """时间范围只让模型抄原话，schema 里不许再给它套 enum。

    套了的症状（实测）：「最近 2 个月」「最近两周」都被选成 24h，「最近六周」被选成 6months ——
    结构化输出一个字一个字生成，写出「2」之后以 2 开头的合法值只剩 24h。
    """
    time_said = INTENT_SCHEMA["schema"]["properties"]["intent"]["properties"]["time_said"]
    assert time_said == {"type": "string"}


# ───────────────────────────────────────────── 解析


def test_parse_reads_a_full_payload():
    result = parse_intent_result(FULL_PAYLOAD)

    assert result.intent.goal == "找刚做完众筹、正需要代工厂的骨传导耳机团队"
    assert result.intent.topics == ("bone conduction", "bone conduction headphone")
    assert result.intent.events == ("刚做完众筹",)
    assert result.intent.exclusions == ("pre-launch",)
    assert result.intent.time_said == ""
    assert result.intent.max_spend_usd == 0.2
    assert result.intent.unsupported == ()
    assert result.clarification_question == "要不要把颈挂式的蓝牙耳机也算进来？"


def test_parse_reads_an_empty_first_round():
    """第一轮什么都没有的样子：用户只说了「帮我找点公司」。"""
    payload = {
        "intent": {
            "goal": "找潜在客户公司",
            "topics": [],
            "events": [],
            "exclusions": [],
            "time_said": "",
            "max_spend_usd": 0,
            "unsupported": [],
        },
        "clarification_question": "你想找做哪一类设备的公司？",
    }

    result = parse_intent_result(payload)

    assert result.intent.topics == ()
    assert result.intent.max_spend_usd == 0.0
    assert is_ready(to_state_dict(result)["intent"]) is False


def test_parse_keeps_the_requirements_we_cannot_meet():
    """用户说「只要加州的、200 人以下」：那两条要原样留在 unsupported。

    它们被塞进 topics 或者被丢掉，都会让用户以为方案是按他说的拟的。
    """
    payload = {
        "intent": {
            "goal": "找加州的小型音频设备公司",
            "topics": ["audio device"],
            "events": [],
            "exclusions": [],
            "time_said": "",
            "max_spend_usd": 0,
            "unsupported": ["只要加州的", "200 人以下"],
        },
        "clarification_question": "要不要缩到某一类设备，比如耳机或者会议麦克风？",
    }

    result = parse_intent_result(payload)

    assert result.intent.unsupported == ("只要加州的", "200 人以下")
    assert result.intent.topics == ("audio device",)


def test_parse_drops_blank_entries():
    """模型偶尔给 [""]。留着它 topics 就算非空，is_ready 判成够了，然后真花钱。"""
    payload = {**FULL_PAYLOAD, "intent": {**FULL_PAYLOAD["intent"], "topics": ["  ", "", "headphone"]}}

    result = parse_intent_result(payload)

    assert result.intent.topics == ("headphone",)


def test_parse_keeps_the_time_words_exactly_as_said():
    """时间的原话原样留下，只去掉首尾空白。换算不在这一层：这里不知道今天几号。"""
    payload = {**FULL_PAYLOAD, "intent": {**FULL_PAYLOAD["intent"], "time_said": "  最近两个月 "}}

    assert parse_intent_result(payload).intent.time_said == "最近两个月"


def test_parse_does_not_paper_over_a_missing_field():
    """缺键当场抛：说明 schema 和解析漂了，那是我们的 bug，该当场看见。

    抛的是 IntentCallFailed 不是裸 KeyError，而且报错里要带上整份回答 ——
    只有一个键名的话，查的时候第一件事（那份回答到底长什么样）还是不知道。
    """
    payload = {"intent": {k: v for k, v in FULL_PAYLOAD["intent"].items() if k != "events"}}

    with pytest.raises(IntentCallFailed, match="events"):
        parse_intent_result(payload)


def test_parse_rejects_a_negative_spend():
    """负数比任何一次报价都小，闸门永远拦，而报表上看不出来。"""
    payload = {**FULL_PAYLOAD, "intent": {**FULL_PAYLOAD["intent"], "max_spend_usd": -1.0}}

    with pytest.raises(IntentCallFailed, match="max_spend_usd"):
        parse_intent_result(payload)


def test_parse_rejects_a_spend_that_is_not_a_finite_number():
    """NaN 跟任何数比大小都是 False，闸门永远放行 —— 这一种会真的花钱。

    不是想出来的情况：json.loads 认 NaN 这个字面量，解析不会报错。
    """
    assert json.loads('{"x": NaN}')["x"] != json.loads('{"x": NaN}')["x"]

    for bad in (float("nan"), float("inf")):
        payload = {**FULL_PAYLOAD, "intent": {**FULL_PAYLOAD["intent"], "max_spend_usd": bad}}

        with pytest.raises(IntentCallFailed, match="max_spend_usd"):
            parse_intent_result(payload)


def test_parse_moves_an_impossible_spend_into_unsupported():
    """把两毛钱说成 20 块是手误，为它整条对话失败太重 —— 压到上限，但要说一句。

    说的那一句落在 unsupported 里，而 unsupported 会原样进 plan 的 assumptions
    摆给同事看，所以压这一下不是静默的。
    """
    payload = {**FULL_PAYLOAD, "intent": {**FULL_PAYLOAD["intent"], "max_spend_usd": 20.0}}

    result = parse_intent_result(payload)

    assert result.intent.max_spend_usd == MAX_SPEND_CEILING_USD
    assert len(result.intent.unsupported) == 1
    assert "20" in result.intent.unsupported[0]


# ───────────────────────────────────────────── 够不够


def test_is_ready_only_looks_at_topics():
    """三个实际值。缺品类词就拟不出 query，别的缺了都有默认。"""
    ready = to_state_dict(parse_intent_result(FULL_PAYLOAD))["intent"]
    assert is_ready(ready) is True

    # 只有事件、没有品类：不知道找什么设备，拟不出词
    assert is_ready({**ready, "topics": []}) is False

    # 有品类，别的全空：够了。time_said 空着用 discovery.yaml 的默认窗口、max_spend 用配置默认、
    # events 空着就两种形状各拟一条
    assert is_ready({**ready, "events": [], "time_said": "", "max_spend_usd": 0.0}) is True


# ───────────────────────────────────────────── 摊平成 state


def test_to_state_dict_leaves_no_tuples_behind():
    """进 checkpoint 的东西读回来不是元组。两边形状不一致的话，== 不相等而且不报错。"""
    value = to_state_dict(parse_intent_result(FULL_PAYLOAD))

    assert value["intent"]["topics"] == ["bone conduction", "bone conduction headphone"]
    assert isinstance(value["intent"]["topics"], list)
    assert isinstance(value["intent"]["unsupported"], list)
    # 整份能被 json 序列化 = 里面没有我们自己的类，也没有元组之外的怪东西
    assert json.loads(json.dumps(value)) == value


def test_to_state_dict_and_the_schema_carry_the_same_fields():
    """state 里那份和模型那份必须是同一组字段。

    加字段只改了 schema 没改这里，症状是那个字段永远进不了 state，而且不报错。
    """
    in_state = set(to_state_dict(parse_intent_result(FULL_PAYLOAD))["intent"])
    in_schema = set(INTENT_SCHEMA["schema"]["properties"]["intent"]["properties"])
    assert in_state == in_schema


# ───────────────────────────────────────────── 拼消息


def test_build_messages_keeps_every_line_and_who_said_it():
    lines = [
        "用户: 帮我找点公司",
        "AI: 你想找做哪一类设备的公司？",
        "用户: 骨传导耳机，最好是刚做完众筹的",
    ]

    messages = build_messages(lines, feedback="")

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    # 整段对话一字不改地进 user 消息，前缀也留着 —— 谁说的这件事要能读出来
    assert messages[1]["content"] == "\n".join(lines)


# ───────────────────────────────────────────── 调模型


def test_read_intent_sends_the_configured_parameters():
    client = _FakeClient(FULL_PAYLOAD)

    result = read_intent(client, ["用户: 帮我找找骨传导耳机的初创"], feedback="", config=CONFIG)

    assert result.intent.topics == ("bone conduction", "bone conduction headphone")
    sent = client.kwargs[0]
    assert sent["model"] == "gpt-4o-mini"
    assert sent["max_tokens"] == 800
    assert sent["temperature"] == 0.0
    # 显式超时：不给的话 openai 默认 600 秒，而用户正在 Slack 那头等着
    assert sent["timeout"] == 30.0
    # 显式 schema，不靠 prompt 求它返回 JSON
    assert sent["response_format"] == {"type": "json_schema", "json_schema": INTENT_SCHEMA}


def test_read_intent_fails_loudly_when_the_answer_is_truncated():
    """截断的回答是半个 JSON。吞掉它等于拿着一份空 intent 往下走。"""
    client = _FakeClient(FULL_PAYLOAD, finish_reason="length")

    with pytest.raises(IntentCallFailed, match="max_tokens"):
        read_intent(client, ["用户: 帮我找点公司"], feedback="", config=CONFIG)


def test_read_intent_fails_loudly_when_the_model_refuses():
    client = _FakeClient(FULL_PAYLOAD, refusal="I can't help with that")

    with pytest.raises(IntentCallFailed, match="拒答"):
        read_intent(client, ["用户: 帮我找点公司"], feedback="", config=CONFIG)


def test_read_intent_fails_loudly_when_there_is_no_content():
    client = _FakeClient(content=None)

    with pytest.raises(IntentCallFailed, match="没有返回内容"):
        read_intent(client, ["用户: 帮我找点公司"], feedback="", config=CONFIG)


def test_read_intent_quotes_the_text_when_it_is_not_json():
    """光说「JSON 解析失败」查不了，得把原文带上。"""
    client = _FakeClient(content="这不是 JSON")

    with pytest.raises(IntentCallFailed, match="这不是 JSON"):
        read_intent(client, ["用户: 帮我找点公司"], feedback="", config=CONFIG)


def test_read_intent_adds_context_to_an_sdk_failure():
    """SDK 抛的异常里没有「这是哪条对话」，这一层的全部价值就是把上下文补上。"""
    with pytest.raises(IntentCallFailed, match="TimeoutError"):
        read_intent(_BoomClient(), ["用户: 帮我找点公司"], feedback="", config=CONFIG)


# ───────────────────────────────────────────── 配置


def test_the_real_config_file_has_every_key():
    """读仓库里那份真的 config/interactive.yaml。

    少一个键的话，报错会发生在 OpenAI 那一侧（比如模型名是空的），
    离真正的原因隔着一整个网络请求。
    """
    config = load_intent_config()

    assert config.model
    assert config.max_tokens > 0
    assert config.temperature == 0.0
    assert config.request_timeout_seconds > 0
    # 上限必须至少是 1：0 或者负数等于「永远不问」，那澄清这条路就是死的
    assert config.max_clarify_rounds >= 1


def test_the_dataclasses_are_frozen():
    """intent 这一份在一轮里不该被人就地改。改了的话 state 里那份和它就对不上了。"""
    result = parse_intent_result(FULL_PAYLOAD)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.intent.topics = ("别的",)  # type: ignore[misc]

    assert isinstance(result, IntentResult)
    assert isinstance(result.intent, DiscoveryIntent)


# ---------- topics 的形状 ----------


def test_topic_with_a_boolean_operator_is_moved_to_unsupported():
    """品类词带布尔符的话，落进 queries.topics 之后词面匹配永远命中不了它。

    症状是静默的：下一次对话问同一个题目，库里明明搜过却说没搜过。
    所以在解析这一层就挑出来，而且不是丢掉是记成「没能照办」——
    丢掉的话同事以为我们按他说的搜了。
    """
    payload = {
        **FULL_PAYLOAD,
        "intent": {
            **FULL_PAYLOAD["intent"],
            "topics": ["bone conduction", "microphone NOT supplier"],
        },
    }

    result = parse_intent_result(payload)

    assert result.intent.topics == ("bone conduction",)
    assert any("microphone NOT supplier" in note for note in result.intent.unsupported)


def test_topic_that_is_a_whole_sentence_is_moved_to_unsupported():
    """品类词是同事说话的粒度，两三个词。整句话拼进检索式只会采回零条。"""
    long_topic = "companies that are looking for an audio manufacturing partner"
    payload = {
        **FULL_PAYLOAD,
        "intent": {**FULL_PAYLOAD["intent"], "topics": ["earbuds", long_topic]},
    }

    result = parse_intent_result(payload)

    assert result.intent.topics == ("earbuds",)
    assert any(long_topic in note for note in result.intent.unsupported)


def test_topics_are_lowercased_so_the_stored_column_is_uniform():
    """大小写不影响匹配，但 queries.topics 那一列将来要跟一批词比重合。"""
    payload = {
        **FULL_PAYLOAD,
        "intent": {**FULL_PAYLOAD["intent"], "topics": ["Bone Conduction"]},
    }

    assert parse_intent_result(payload).intent.topics == ("bone conduction",)


def test_a_normal_topic_survives_untouched():
    """闸门不能把正常的词也拦下来 —— 三个词正好在上限上。"""
    payload = {
        **FULL_PAYLOAD,
        "intent": {**FULL_PAYLOAD["intent"], "topics": ["noise cancelling headphone"]},
    }

    result = parse_intent_result(payload)

    assert result.intent.topics == ("noise cancelling headphone",)
    assert result.intent.unsupported == ()


def test_the_latest_feedback_is_repeated_as_its_own_message():
    """用户看完方案回的那一句，除了留在对话里，还要单独再给一遍、放在最后。

    为什么值得重复一遍：每绕一轮改方案，对话里就多一句我们自己说的 AI 行，而那一句
    带着整份 assumptions（「我理解的需求：…」「要搜的词：…」）—— 那是上几轮读出来的
    结论的渲染，不是用户说的话。绕三轮之后我们复述的字数远多于他说的，而这次调用
    要从这一整段里认出哪一句是新的。
    """
    lines = [
        "用户: 帮我找找骨传导耳机的初创",
        "AI: 方案 v1，2 条 query，预计 $0.16。我理解的需求：…",
        "用户: 别带众筹的",
    ]

    messages = build_messages(lines, feedback="别带众筹的")

    assert len(messages) == 3
    assert messages[-1]["role"] == "user"
    # 原话一个字不改地再出现一次，而且在最后
    assert messages[-1]["content"].endswith("别带众筹的")
    # 上面那段照样全给：「第二条不要了」里那个「第二条」只有完整对话读得出来
    assert messages[1]["content"] == "\n".join(lines)


def test_no_feedback_means_no_extra_message():
    """空串也加一条的话，模型会看见一条内容是空的指令 —— 那比不给更糟，
    它得自己猜那是什么意思。只有空白的也算空。
    """
    for blank in ("", "   ", "\n"):
        assert len(build_messages(["用户: 帮我找点公司"], feedback=blank)) == 2


def test_the_feedback_reaches_the_model():
    """这一层的接线：read_intent 收到的 feedback 要真的进那次请求。

    漏传的症状是那一句不再被强调，而那件事没有任何报错 —— 图照跑，方案照出，
    只是模型越绕越容易认错哪句是新的。
    """
    client = _FakeClient(FULL_PAYLOAD)

    read_intent(client, ["用户: 帮我找点公司", "用户: 别带众筹的"], feedback="别带众筹的", config=CONFIG)

    sent = client.kwargs[0]["messages"]
    assert len(sent) == 3
    assert "别带众筹的" in sent[-1]["content"]


# ---------- 要避开的词跟要找的撞了 ----------


def test_an_exclusion_that_is_also_the_event_is_dropped_and_said_out_loud():
    """用户说「找在 Kickstarter 或 Indiegogo 上众筹的」，模型把那两个平台也填进了要避开的词。

    这条红了的症状出现过一次（run 38）：planner 拟的 Kickstarter AND (…) 和 Indiegogo AND (…)
    全被当成写进了要避开的词丢掉，剩下的那条一条帖子都没采回来。
    撞了的词不是悄悄去掉，是变成一句话进 unsupported，方案卡片上看得见。
    """
    payload = {
        **FULL_PAYLOAD,
        "intent": {
            **FULL_PAYLOAD["intent"],
            "events": ["在 Kickstarter 或 Indiegogo 上众筹"],
            "exclusions": ["Kickstarter", "indiegogo", "pre-launch"],
        },
    }

    result = parse_intent_result(payload)

    assert result.intent.exclusions == ("pre-launch",)
    notes = [n for n in result.intent.unsupported if "按要找处理" in n]
    assert notes == [
        "「Kickstarter」出现在要找的事件里，按要找处理，拟词时不避开它",
        "「indiegogo」出现在要找的事件里，按要找处理，拟词时不避开它",
    ]


def test_an_exclusion_that_is_also_a_topic_is_dropped():
    """要避开的词同时是要搜的品类词：一个词不可能既要找又要避开，按要找处理。"""
    payload = {
        **FULL_PAYLOAD,
        "intent": {
            **FULL_PAYLOAD["intent"],
            "topics": ["bone conduction"],
            "exclusions": ["bone conduction"],
        },
    }

    result = parse_intent_result(payload)

    assert result.intent.exclusions == ()
    assert any("出现在要搜的品类词里" in note for note in result.intent.unsupported)


def test_an_exclusion_only_matches_whole_words():
    """按整词比：kick 碰不到 Kickstarter，照样留在要避开的词里。"""
    payload = {
        **FULL_PAYLOAD,
        "intent": {**FULL_PAYLOAD["intent"], "events": ["在 Kickstarter 上众筹"], "exclusions": ["kick"]},
    }

    assert parse_intent_result(payload).intent.exclusions == ("kick",)
