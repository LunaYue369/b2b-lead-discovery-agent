"""time_window.resolve_window：用户关于时间的原话换算成采集窗口。

这些说法大多是实测里模型选错过的（以前让它从那几档里挑）：
「最近两个月」「最近 2 个月」「最近两周」被选成 24h，「最近六周」被选成 6months，
「最近十天」「最近五个月」被就近折成 week / 3months。现在它只抄原话，换算全在这里，
每一种都焊一条。

「今天」固定成 2026-09-28 11:00 UTC（那天是周一），所有日期都按它算。
"""

from __future__ import annotations

import datetime as dt

import pytest

from triggers.interactive_discovery.query.time_window import PostWindow, resolve_window

NOW = dt.datetime(2026, 9, 28, 11, 0, tzinfo=dt.UTC)


def _since(said: str) -> str | None:
    return resolve_window(said, now=NOW, default_limit="month").since


@pytest.mark.parametrize(
    ("said", "since"),
    [
        # 以前被前缀带跑、选成 24h 的那三种
        ("最近两个月", "2026-07-28"),
        ("最近 2 个月", "2026-07-28"),
        ("最近两周", "2026-09-14"),
        # 以前被带成 6months 的
        ("最近六周", "2026-08-17"),
        # 以前被就近折掉的
        ("最近十天", "2026-09-18"),
        ("最近五个月", "2026-04-28"),
        ("最近两年", "2024-09-28"),
        # 本来就对的几档，换算之后跟那一档是同一个意思
        ("最近一周", "2026-09-21"),
        ("最近三个月", "2026-06-28"),
        # 中文数字、半
        ("最近十五天", "2026-09-13"),
        ("过去二十天", "2026-09-08"),
        ("近半年", "2026-03-28"),
        ("半个月", "2026-09-13"),
        # 日历上的某一天
        ("今天", "2026-09-28"),
        ("这周", "2026-09-28"),
        ("这个月", "2026-09-01"),
        ("今年", "2026-01-01"),
        # 英文
        ("last 2 months", "2026-07-28"),
        ("past week", "2026-09-21"),
        ("the last 3 days", "2026-09-25"),
        ("this month", "2026-09-01"),
    ],
)
def test_said_windows_become_a_cutoff_date(said, since):
    assert _since(said) == since


def test_hours_keep_the_time_of_day():
    """小时要带时刻：只给日期的话，「最近 6 小时」会变成「从今天零点起」。"""
    assert _since("最近 6 小时") == "2026-09-28T05:00:00Z"
    assert _since("24小时内") == "2026-09-27T11:00:00Z"
    assert _since("past 24 hours") == "2026-09-27T11:00:00Z"


def test_months_count_back_on_the_calendar_and_clamp_to_month_end():
    """月按日历往回数，不折成 30 天；那个月没有这一天就取月底。"""
    march_31 = dt.datetime(2026, 3, 31, tzinfo=dt.UTC)

    assert resolve_window("最近一个月", now=march_31, default_limit="month").since == "2026-02-28"


def test_no_limit_uses_the_any_level():
    assert resolve_window("不限时间", now=NOW, default_limit="month") == PostWindow(
        since=None, limit="any", said="不限时间", understood=True
    )


def test_nothing_said_falls_back_to_the_default_level():
    assert resolve_window("", now=NOW, default_limit="month") == PostWindow(
        since=None, limit="month", said="", understood=False
    )


@pytest.mark.parametrize("said", ["今年上半年", "最近", "最近一个半月", "7 月到 8 月"])
def test_what_cannot_be_converted_falls_back_and_keeps_the_words(said):
    """换算不了的：用默认那一档，原话留着（卡片上要说「你说的 xxx 换算不了」），不猜一个日期。

    今年上半年还有一个结束时间，postedLimitDate 只管得了开始那一头；
    「最近」没说多久；一个半月、几月到几月这种不认
    """
    assert resolve_window(said, now=NOW, default_limit="month") == PostWindow(
        since=None, limit="month", said=said, understood=False
    )
