"""classify_plan_reply 那次调用的测试：配置、schema、拼消息、解析、拿不到好回答时炸。不调模型、不连库。

焊住的几类：

    第四种取值      模型返回一个不在三选一里的值，被善意地折成某一条路
                    （折成 approve 就是照一份没点头的计划写稿）
    判错了那一句    要判的那句没作为一份明写的输入交出去，只能靠「取最后一句」
    静默降级        拿不到好回答时没报错，编一个判断往下走
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from triggers.interactive_discovery.drafting.reply import (
    DECISIONS,
    REPLY_SCHEMA,
    ReplyCallFailed,
    ReplyConfig,
    ReplyJudgement,
    build_reply_messages,
    load_reply_config,
    parse_reply,
    read_decision,
)

CONFIG = ReplyConfig(model="gpt-4o-mini", max_tokens=200, temperature=0.0, request_timeout_seconds=15.0)

LINES = [
    "用户: 给 3 号写封邮件",
    "AI: 好，给 North Field Technologies 写这 1 份：\n第 1 份：邮件，发到 marcandre@northfield-inc.example → …",
    "用户: 写吧",
]


class _FakeClient:
    """照剧本回答的 client，形状跟 tests/test_drafting_planner.py 那个一样。"""

    def __init__(self, content, *, finish_reason="stop"):
        self._content = content
        self._finish_reason = finish_reason
        self.kwargs: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs.append(kwargs)
        message = SimpleNamespace(content=self._content, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=self._finish_reason, message=message)])


def test_the_repo_config_loads():
    """仓库里那份 config/drafting.yaml 的 reply 段读得出来，数跟这里测试用的那份一样。"""
    assert load_reply_config() == CONFIG


def test_the_schema_only_allows_the_three_decisions():
    """strict 模式两条硬要求，加上 decision 是那三个取值的闭集。"""
    schema = REPLY_SCHEMA["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["decision"]["enum"] == list(DECISIONS)


def test_the_sentence_to_judge_is_given_explicitly():
    """要判的那一句单独给，整轮对话照样全给（里面有他对着的那份计划）。"""
    messages = build_reply_messages(LINES, feedback="写吧")

    assert messages[0]["role"] == "system"
    assert json.loads(messages[1]["content"]) == {"plan_feedback": "写吧", "conversation": LINES}


@pytest.mark.parametrize("decision", DECISIONS)
def test_parse_each_decision(decision):
    expected = ReplyJudgement(decision=decision, reason="测试")
    assert parse_reply({"decision": decision, "reason": " 测试 "}) == expected


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"decision": "APPROVE", "reason": "大小写不一样"}, "合法值只有"),
        ({"decision": "write", "reason": "第四种"}, "合法值只有"),
        ({"reason": "少了 decision"}, "少了字段"),
    ],
    ids=["wrong-case", "fourth-value", "missing-key"],
)
def test_parse_refuses_what_is_not_one_of_the_three(payload, match):
    """第四种值、大小写不对、缺键：当场炸，不折成某一条路。"""
    with pytest.raises(ReplyCallFailed, match=match):
        parse_reply(payload)


def test_read_decision_sends_the_right_request():
    """调一次模型：用的是配置里的模型、超时，schema 是 REPLY_SCHEMA；拿回来解析成 ReplyJudgement。"""
    client = _FakeClient(json.dumps({"decision": "approve", "reason": "「写吧」是同意"}))

    got = read_decision(client, LINES, feedback="写吧", config=CONFIG)

    assert got == ReplyJudgement(decision="approve", reason="「写吧」是同意")
    sent = client.kwargs[0]
    assert (sent["model"], sent["timeout"], sent["max_tokens"]) == ("gpt-4o-mini", 15.0, 200)
    assert sent["response_format"]["json_schema"] is REPLY_SCHEMA


@pytest.mark.parametrize(
    ("client", "match"),
    [
        (_FakeClient("{}", finish_reason="length"), "截断"),
        (_FakeClient("not json"), "不是合法 JSON"),
        (_FakeClient("[]"), "不是一个对象"),
        (_FakeClient(None), "没有返回内容"),
    ],
    ids=["truncated", "not-json", "not-an-object", "no-content"],
)
def test_read_decision_fails_loudly(client, match):
    """拿不到好回答一律抛 ReplyCallFailed：宁可让用户再说一句，也不编一个判断往下走。"""
    with pytest.raises(ReplyCallFailed, match=match):
        read_decision(client, LINES, feedback="写吧", config=CONFIG)
