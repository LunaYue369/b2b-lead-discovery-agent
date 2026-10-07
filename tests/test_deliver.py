"""deliver 节点的测试：推完卡片回给用户的那句话，以及节点里那几步的先后。

两层：

    _delivered_text    纯函数。push_interactive 的结果 -> 回到对话 thread 里的一句话
    deliver            节点本身。库、Slack、push_interactive 全换成桩，只看它按什么顺序
                       调了谁、把什么传了下去

推卡片本身（查素材、挡 in_draft、不截配额、推过不再推）在 tests/test_digest.py 里测。
"""

from __future__ import annotations

import contextlib

import pytest

from delivery import send
from persistence import db
from triggers.digest import InteractivePush
from triggers.interactive_discovery.query import nodes
from triggers.interactive_discovery.query.nodes import _delivered_text

# ── 第一层：回给用户的那句话 ─────────────────────────────────────────────────


def test_a_normal_push_says_how_many_and_where():
    """分析了 12 家、3 家过线、3 张都发出去了：一句话说清数目，后面跟频道汇总的链接。"""
    result = InteractivePush(
        harvested=40, analyzed=12, qualified=3, best_score=81, posted=3, summary_ts="1.0"
    )
    assert _delivered_text(result, "https://x/p1") == "分析了 12 家，3 家过线，推了 3 张到频道：https://x/p1"


def test_cards_under_the_line_are_called_out_as_grey():
    """push_below_line 打开时推了没过线的：回话里说出其中几张没过线、是灰色的。

    不说的话「0 家过线，推了 12 张」读起来自相矛盾
    """
    result = InteractivePush(
        harvested=40, analyzed=12, qualified=0, best_score=45, posted=12, below_line=12, summary_ts="1.0"
    )
    assert _delivered_text(result, "https://x/p1") == (
        "分析了 12 家，0 家过线，推了 12 张到频道（其中 12 张没过线，灰色的）：https://x/p1"
    )


def test_a_failed_card_is_named_with_its_error_code():
    """有一张发失败：说出是哪家、Slack 回的错码原样给。拿不到链接时停在「推了几张到频道」。"""
    result = InteractivePush(
        harvested=40,
        analyzed=12,
        qualified=3,
        best_score=81,
        posted=2,
        failures={1290: "msg_too_long"},
        summary_ts="1.0",
    )
    assert _delivered_text(result, None) == (
        "分析了 12 家，3 家过线，推了 2 张到频道。另外 1 张没发出去（lead 1290：msg_too_long）"
    )


def test_nothing_over_the_line_names_the_best_score_and_says_start_over():
    """一家都没过线：说出最高几分，让用户去频道里重新 @ bot。

    不能说「在这里回一句我重拟」：图已经走到 END，同一个 thread 里再说话会被判成 FINISHED。
    """
    result = InteractivePush(harvested=40, analyzed=12, qualified=0, best_score=45, posted=0)
    text = _delivered_text(result, None)
    assert text.startswith("这次分析的 12 家都没过线（最高 45 分）。")
    assert "在频道里重新 @ 我" in text


def test_nothing_analyzed_is_told_apart_from_nothing_over_the_line():
    """采回来 40 条、一家都没进分析（被过滤砍掉，或者全在冷却期里）跟「分析了都没过线」是两件事。"""
    result = InteractivePush(harvested=40, analyzed=0, qualified=0, best_score=None, posted=0)
    text = _delivered_text(result, None)
    assert text.startswith("采回来 40 条帖子，但没有一家公司进到分析")
    assert "没过线" not in text


def test_nothing_harvested_points_at_the_query_not_at_cooldown():
    """一条帖子都没采回来：该改的是检索式，不能说成「它们在冷却期里」。

    这条红了的症状出现过一次：run 38 的检索式带着「只看创始人发的帖」，Apify 一条都没返回，
    回话却说「采回来的公司要么还在冷却期里，要么判过不合格」—— 那时候根本没有采回来的公司。
    """
    result = InteractivePush(harvested=0, analyzed=0, qualified=0, best_score=None, posted=0)
    text = _delivered_text(result, None)
    assert text.startswith("这次一条帖子都没采回来")
    assert "冷却期" not in text


def test_everything_already_in_draft_does_not_claim_a_score():
    """分析了的全是在写稿的：没有能出卡的，也就没有「最高几分」可说。"""
    result = InteractivePush(harvested=40, analyzed=2, qualified=0, best_score=None, posted=0)
    assert _delivered_text(result, None) == "这次分析的 2 家都已经在写稿了，没有新的卡片可推。"


def test_a_retry_after_a_crash_says_nothing_new_was_sent():
    """崩溃之后的重跑：卡片之前已经发过了，这次一张没发，话里要说清楚，不能再报「推了 3 张」。"""
    result = InteractivePush(
        harvested=40, analyzed=12, qualified=3, best_score=81, posted=0, already_pushed=3
    )
    assert _delivered_text(result, None) == "这一跑的 3 张卡片之前已经推到频道了，这次没有再发一遍。"


def test_a_failed_harvest_is_not_told_as_nothing_found():
    """采集全失败不能说成「没找到合格的」：那是故障，不是结果。"""
    result = InteractivePush(harvested=0, analyzed=0, qualified=0, best_score=None, posted=0, run_failed=True)
    text = _delivered_text(result, None)
    assert text.startswith("这一跑的采集全失败了")
    assert "不是没有合格的公司" in text


# ── 第二层：节点本身 ─────────────────────────────────────────────────────────


@pytest.fixture
def deliver_env(monkeypatch):
    """库、Slack、push_interactive 全换成桩，每一步往 calls 里记一笔。

    输出：一个 dict
        calls     按发生顺序记下的每一步，例
                  ["permalink C0000000001/1790126072.000100",
                   "push run=31 channel=C0LEADS origin=https://x/root",
                   "commit", "permalink C0LEADS/1790126080.000200"]
        result    push_interactive 桩要返回的那份 InteractivePush，测试可以换掉
    """
    env = {
        "calls": [],
        "result": InteractivePush(
            harvested=40, analyzed=12, qualified=3, best_score=81, posted=3, summary_ts="1790126080.000200"
        ),
    }

    class FakeConn:
        def commit(self):
            env["calls"].append("commit")

        def cursor(self):
            return contextlib.nullcontext("cur")

    @contextlib.contextmanager
    def fake_connect(*args, **kwargs):
        yield FakeConn()

    def fake_permalink(client, channel, ts):
        env["calls"].append(f"permalink {channel}/{ts}")
        return "https://x/root" if ts == "1790126072.000100" else "https://x/summary"

    def fake_push(cur, client, channel, *, run_id, now, origin):
        env["calls"].append(f"push run={run_id} channel={channel} origin={origin}")
        return env["result"]

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(send, "build_client", lambda: "client")
    monkeypatch.setattr(send, "read_channel", lambda: "C0LEADS")
    monkeypatch.setattr(send, "permalink", fake_permalink)
    # nodes 是 from triggers.digest import push_interactive 引进来的，要换 nodes 上那个名字
    monkeypatch.setattr(nodes, "push_interactive", fake_push)
    return env


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_deliver_commits_before_it_asks_slack_for_the_summary_link(deliver_env):
    """先提交、再去拿汇总的链接，顺序不能反。

    拿链接是又一次调 Slack。它要是排在提交前面、又恰好崩溃，卡片已经发出去了、账却没记上，
    用户回一句话重跑的时候就会把同一批卡片再发一遍。
    """
    out = nodes.deliver({"run_id": 31}, _config("slack:C0000000001:1790126072.000100"))

    assert deliver_env["calls"] == [
        "permalink C0000000001/1790126072.000100",
        "push run=31 channel=C0LEADS origin=https://x/root",
        "commit",
        "permalink C0LEADS/1790126080.000200",
    ]
    assert out == {"messages": ["AI: 分析了 12 家，3 家过线，推了 3 张到频道：https://x/summary"]}


def test_deliver_does_not_ask_for_a_link_when_nothing_went_to_the_channel(deliver_env):
    """没往频道发（这里是没过线）：没有汇总可链，第二次 permalink 不该发生。"""
    deliver_env["result"] = InteractivePush(harvested=40, analyzed=12, qualified=0, best_score=45, posted=0)

    out = nodes.deliver({"run_id": 31}, _config("slack:C0000000001:1790126072.000100"))

    assert [c for c in deliver_env["calls"] if c.startswith("permalink C0LEADS")] == []
    assert out["messages"][0].startswith("AI: 这次分析的 12 家都没过线")


def test_a_thread_id_that_is_not_from_slack_gets_no_origin_link(deliver_env):
    """scripts/try_interactive.py 手跑时的 thread_id 不是 slack:频道:ts 的格式：不去取来处的链接。"""
    nodes.deliver({"run_id": 31}, _config("demo-1"))

    assert deliver_env["calls"][0] == "push run=31 channel=C0LEADS origin=None"
