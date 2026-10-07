"""发送那一层：顺序、部分失败、限流重试、幂等的缺口。

用假客户端测。不是「懒得连真的」—— 这一层最值得测的恰恰是异常路径
（某一条被 Slack 拒了、撞限流、汇总就失败），而那几种情况在真频道里
造不出来。真发一条的验证在 scripts/check_slack.py --post 那边。
"""

from __future__ import annotations

import random

import pytest

from delivery.cards import CardConfig, choose
from delivery.send import PushResult, permalink, post_digest
from models import NO_CONTACTS, CardEvidence, CardQuote, LeadCard

WEIGHTS = {
    "icp_fit": 25,
    "signal_strength": 20,
    "seniority": 10,
    "moq_fit": 15,
    "recency": 10,
    "reachability": 10,
    "corroboration": 10,
}

CONFIG = CardConfig(
    a_score=75,
    qualified_score=60,
    review_score=40,
    a_quota=5,
    b_quota=10,
    review_sample_size=5,
    push_below_line=False,
    weights=WEIGHTS,
)


def _card(score: int, lead_id: int) -> LeadCard:
    return LeadCard(
        lead_id=lead_id,
        display_name=f"公司 {lead_id}",
        score=score,
        icp_fit=8,
        signal_strength=8,
        seniority=8,
        moq_fit=8,
        recency=6,
        reachability=2,
        corroboration=3,
        analysis="品类：做耳机。\n结论：值得联系。",
        evidence=(
            CardEvidence(
                "Shah",
                "Founder",
                "2026-08-19",
                "https://x",
                (CardQuote("We just wrapped our Kickstarter", "创始人本人说的。"),),
            ),
        ),
        unplaced_quotes=(),
        unpaired_means="",
        contacts=NO_CONTACTS,
    )


class _FakeResponse(dict):
    """假的 SlackApiError.response：既是字典又有 headers。"""

    def __init__(self, error: str, retry_after: str | None = None) -> None:
        super().__init__(error=error)
        self.headers = {"Retry-After": retry_after} if retry_after else {}


class _FakeClient:
    """假 Slack 客户端。

    scripted 里按调用顺序放好每一次要发生什么：字符串表示成功并返回那个
    ts，SlackApiError 表示这一次抛错。sent 记下每次收到的参数，
    用来验顺序和内容。
    """

    def __init__(self, scripted: list) -> None:
        self._scripted = list(scripted)
        self.sent: list[dict] = []

    def chat_postMessage(self, **kwargs):  # noqa: N802 - Slack SDK 的方法名
        self.sent.append(kwargs)
        outcome = self._scripted.pop(0) if self._scripted else f"ts-{len(self.sent)}"
        if isinstance(outcome, Exception):
            raise outcome
        return {"ts": outcome}


def _error(code: str, retry_after: str | None = None):
    from slack_sdk.errors import SlackApiError

    return SlackApiError(code, _FakeResponse(code, retry_after))


def test_the_summary_goes_first_then_one_message_per_lead():
    """一条汇总在前，每条线索一条在后。

    Slack 里新消息在下面，所以先发汇总读起来就是「总账在上、卡片在下」。
    反过来发的话总账会压在一堆卡片底下。
    """
    cards = [_card(80, 1), _card(70, 2), _card(65, 3)]
    digest = choose(cards, config=CONFIG, rng=random.Random(0))
    client = _FakeClient(["ts-summary", "ts-1", "ts-2", "ts-3"])

    result = post_digest(
        client, "C123", digest, run_id=27, company_count=22, weights=WEIGHTS, origin=None, pause=0
    )

    assert len(client.sent) == 4
    # 第一条是汇总：没有按钮，标题是「线索报告」
    summary_blocks = client.sent[0]["attachments"][0]["blocks"]
    assert "线索报告" in summary_blocks[0]["text"]["text"]
    assert not any(b["type"] == "actions" for b in summary_blocks)
    # 后面三条各是一张卡片，A 档在前
    assert result.summary_ts == "ts-summary"
    assert result.posted == {1: "ts-1", 2: "ts-2", 3: "ts-3"}
    assert result.pushed_lead_ids == [1, 2, 3]
    assert result.failures == {}
    assert all(c["channel"] == "C123" for c in client.sent)


def test_one_bad_card_does_not_stop_the_others():
    """一条卡片发失败，别的照样发出去。

    一条发不出去通常是那一条的内容问题（太长、块不合法），不该让今天
    别的线索也推不出去。而失败那条不会进 pushed_lead_ids，所以 trigger
    不会给它记 pushed_at —— 不然 ignored 扫描七天后会把它算成
    「她没理」，而她根本没见过。
    """
    digest = choose([_card(80, 1), _card(70, 2), _card(65, 3)], config=CONFIG)
    client = _FakeClient(["ts-summary", "ts-1", _error("msg_too_long"), "ts-3"])

    result = post_digest(
        client, "C123", digest, run_id=27, company_count=22, weights=WEIGHTS, origin=None, pause=0
    )

    assert result.posted == {1: "ts-1", 3: "ts-3"}
    assert result.failures == {2: "msg_too_long"}
    assert result.pushed_lead_ids == [1, 3]
    # 四条都试过了，没有因为第二条失败就停下
    assert len(client.sent) == 4


def test_a_failed_summary_is_raised_not_swallowed():
    """汇总就发失败要抛出去，不接着发卡片。

    汇总发不出去说明的是「跟 Slack 的连接坏了」（token 失效、bot 没被
    邀请进频道），那时候接着发卡片只会把同一个错重复十几遍。
    """
    from slack_sdk.errors import SlackApiError

    digest = choose([_card(80, 1)], config=CONFIG)
    client = _FakeClient([_error("not_in_channel")])

    with pytest.raises(SlackApiError):
        post_digest(
            client, "C123", digest, run_id=27, company_count=22, weights=WEIGHTS, origin=None, pause=0
        )

    # 只试了汇总那一条，没往下发
    assert len(client.sent) == 1


def test_a_run_with_nothing_over_the_line_still_posts_the_summary():
    """一条都没过线也要发汇总。

    不发的话，「系统挂了」和「这一跑真没有」在频道里长得一模一样。
    而连着几跑没有线索是关键词或者 ICP 该调的信号。
    """
    digest = choose([_card(30, 1), _card(45, 2)], config=CONFIG)
    client = _FakeClient(["ts-summary"])

    result = post_digest(
        client, "C123", digest, run_id=27, company_count=15, weights=WEIGHTS, origin=None, pause=0
    )

    assert len(client.sent) == 1
    assert result.summary_ts == "ts-summary"
    assert result.posted == {}
    text = str(client.sent[0]["attachments"][0]["blocks"])
    assert "这一跑没有过线的线索" in text


def test_rate_limiting_is_retried_once_with_the_wait_slack_asks_for(monkeypatch):
    """撞限流按 Slack 给的 Retry-After 等一下再试一次。

    只重试这一种错码。别的重试没有意义：msg_too_long 再发一次还是太长，
    not_in_channel 再发一次还是没被邀请，而每次重试都要等一秒。
    """
    slept: list[float] = []
    monkeypatch.setattr("delivery.send.time.sleep", slept.append)

    digest = choose([_card(80, 1)], config=CONFIG)
    client = _FakeClient(["ts-summary", _error("ratelimited", retry_after="3"), "ts-1"])

    result = post_digest(
        client, "C123", digest, run_id=27, company_count=22, weights=WEIGHTS, origin=None, pause=0
    )

    assert result.posted == {1: "ts-1"}
    assert slept == [3.0]  # 用的是 Slack 给的数，不是我们的默认值
    assert len(client.sent) == 3


def test_a_non_rate_limit_error_is_not_retried():
    """不是限流的错不重试，直接记成这一条失败。"""
    digest = choose([_card(80, 1)], config=CONFIG)
    client = _FakeClient(["ts-summary", _error("invalid_blocks"), "不该用到这一项"])

    result = post_digest(
        client, "C123", digest, run_id=27, company_count=22, weights=WEIGHTS, origin=None, pause=0
    )

    assert result.failures == {1: "invalid_blocks"}
    # 汇总一条 + 卡片一条，没有第三次
    assert len(client.sent) == 2


def test_the_message_timestamps_are_kept_for_later_updates():
    """每条卡片的 ts 要留着。

    chat.update 要拿 ts 才能改那条消息 —— 点完按钮把卡片改成「已收到」
    就靠它。发消息那一刻是唯一能拿到它的时候，不存下来以后得回头去
    Slack 里搜。
    """
    digest = choose([_card(80, 7)], config=CONFIG)
    client = _FakeClient(["ts-summary", "1788393342.110719"])

    result = post_digest(
        client, "C123", digest, run_id=27, company_count=22, weights=WEIGHTS, origin=None, pause=0
    )
    assert result.posted[7] == "1788393342.110719"


def test_push_result_starts_empty():
    """空的 PushResult 是合法的：什么都没发的时候不该是 None。"""
    empty = PushResult()
    assert empty.summary_ts is None
    assert empty.pushed_lead_ids == []
    assert empty.failures == {}


def test_the_failed_flag_reaches_the_summary_card():
    """failed 要一路透传到汇总卡，不能在发送这一层丢掉。

    丢掉的话，采集全失败那天频道里会显示「这是合格的结果，不是故障」。
    """
    digest = choose([], config=CONFIG, rng=random.Random(0))
    client = _FakeClient(["ts-summary"])

    post_digest(
        client, "C123", digest, run_id=27, company_count=0, weights=WEIGHTS, origin=None, failed=True, pause=0
    )
    assert "采集全失败" in str(client.sent[0]["attachments"][0]["blocks"])


def test_a_link_slack_refuses_to_give_is_none_not_a_crash():
    """chat.getPermalink 回错：返回 None，不抛。

    链接只是让人少点一下。deliver 拿链接的时候卡片已经发出去、账已经提交了，
    为了一个链接让整个节点崩溃，用户看到的就是「出错了」，而卡片其实好好地在频道里。
    """

    class _Client:
        def chat_getPermalink(self, **kwargs):  # noqa: N802 - Slack SDK 的方法名
            raise _error("message_not_found")

    class _Ok:
        def chat_getPermalink(self, **kwargs):  # noqa: N802 - Slack SDK 的方法名
            return {"permalink": f"https://x/{kwargs['channel']}/{kwargs['message_ts']}"}

    assert permalink(_Client(), "C1", "1.0") is None
    assert permalink(_Ok(), "C1", "1.0") == "https://x/C1/1.0"


def test_a_network_error_on_one_card_does_not_lose_the_ones_already_sent():
    """第三条消息超时：前两条已经发出去了，要照样返回、照样把它们报成发成了。

    这条红了的症状：异常冲出 post_digest，调用方拿不到 PushResult，已经发出去的那几张
    一行 pushed_at 都记不上。对话那边 deliver 重跑时查不到它们推过，整批再发一遍。
    """

    class _Timeout(_FakeClient):
        def chat_postMessage(self, **kwargs):  # noqa: N802 - Slack SDK 的方法名
            if len(self.sent) == 2:
                self.sent.append(kwargs)
                raise TimeoutError("read timed out")
            return super().chat_postMessage(**kwargs)

    cards = [_card(80, 1), _card(70, 2), _card(65, 3)]
    digest = choose(cards, config=CONFIG, rng=random.Random(0))
    client = _Timeout([])

    result = post_digest(
        client, "C123", digest, run_id=27, company_count=3, weights=WEIGHTS, origin=None, pause=0
    )

    assert result.pushed_lead_ids == [1, 3]
    assert result.failures == {2: "TimeoutError"}
