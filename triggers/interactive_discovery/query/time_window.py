"""把用户关于时间范围的原话换算成采集窗口。纯函数，不连网、不调模型。

为什么不让模型直接选：以前 intent 那次调用在 schema 里从固定几档（1h / 24h / week / month /
3months / 6months / year / any）里挑一个，实测会被带跑：

    「最近两个月」「最近 2 个月」「最近两周」 -> 24h
    「最近六周」                             -> 6months
    「最近十天」 -> week，「最近五个月」 -> 3months

原因是结构化输出一个字一个字地生成、每个字都必须还拼得成合法值：它想写 2months，
写出「2」之后以 2 开头的合法值只有 24h，只能补完成 24h。prompt 里怎么写都救不回来。

而采集那边本来就不只有那几档：HarvestAPI 的 postedLimitDate 收任意日期，
「从现在一直采到这一天」（实测：截止 09-21、按相关度排，回来最早一条是 09-21 12:00；
截止放到 07-28，同一个词就能回来 09-07 的；只收回来那几条的钱）。
所以现在模型只把原话抄下来（time_said，实测十条抄对十条），算日期是这里的事。

换算出来是三种之一（PostWindow）：

    用户说了、换算得出      since='2026-07-28'          传给 postedLimitDate
    用户说了「不限时间」    limit='any'                 传给 postedLimit
    用户没说，或换算不了    limit=discovery.yaml 的默认  传给 postedLimit，卡片上说明是默认
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PostWindow:
    """这一跑只要多新的帖子。since 和 limit 恰好有一个不是 None。

    五个实际值（今天是 2026-09-28 11:00 UTC）：

        「最近两个月」   PostWindow(since='2026-07-28', limit=None, said='最近两个月', understood=True)
        「最近 6 小时」  PostWindow(since='2026-09-28T05:00:00Z', limit=None,
                                   said='最近 6 小时', understood=True)
        「不限时间」     PostWindow(since=None, limit='any', said='不限时间', understood=True)
        没说时间         PostWindow(since=None, limit='month', said='', understood=False)
        「今年上半年」   PostWindow(since=None, limit='month', said='今年上半年', understood=False)

    since      传给 actor 的 postedLimitDate。天以上的单位是日期，小时是 UTC 时刻
    limit      传给 actor 的 postedLimit，只在 since 是 None 时有值
    said       用户的原话，没说是空串。卡片上原样印出来给他核对
    understood said 换算成功了没有。没说时间也是 False：两种都落到默认
    """

    since: str | None
    limit: str | None
    said: str
    understood: bool


# 不限时间的说法。整句比，不做包含：「不限时间」是一句完整的话，
# 「时间不限但最好是最近的」这种夹着别的意思的，交给下面换算不了那一支
_ANY = {
    "不限时间",
    "不限",
    "时间不限",
    "多久以前的都行",
    "多久都行",
    "所有时间",
    "全部时间",
    "anytime",
    "any time",
    "all time",
}

# 从日历上的某一天算起的说法：这周从周一、这个月从 1 号、今年从 1 月 1 号
_CALENDAR = {
    "今天": "day",
    "今日": "day",
    "today": "day",
    "这周": "week",
    "本周": "week",
    "这一周": "week",
    "这个星期": "week",
    "this week": "week",
    "这个月": "month",
    "本月": "month",
    "this month": "month",
    "今年": "year",
    "本年": "year",
    "this year": "year",
}

# 单位 -> (换算成什么, 一个单位是几份)。月和年按日历往回数，不折成天：
# 「最近两个月」在 09-28 是 07-28，折成 60 天会是 07-30
_UNITS = {
    "小时": ("hours", 1),
    "个小时": ("hours", 1),
    "钟头": ("hours", 1),
    "个钟头": ("hours", 1),
    "hour": ("hours", 1),
    "hours": ("hours", 1),
    "天": ("days", 1),
    "日": ("days", 1),
    "day": ("days", 1),
    "days": ("days", 1),
    "周": ("days", 7),
    "个周": ("days", 7),
    "星期": ("days", 7),
    "个星期": ("days", 7),
    "礼拜": ("days", 7),
    "个礼拜": ("days", 7),
    "week": ("days", 7),
    "weeks": ("days", 7),
    "月": ("months", 1),
    "个月": ("months", 1),
    "month": ("months", 1),
    "months": ("months", 1),
    "年": ("months", 12),
    "year": ("months", 12),
    "years": ("months", 12),
}

# 「最近两个月」「过去 3 周」「近半年」「last 2 months」「past week」
# 前缀、后缀都可以没有；数字可以是阿拉伯数字、中文数字、「半」，英文里也可以不写数字（past week）
_RELATIVE = re.compile(
    r"(?:最近|近|过去|前|这|last|past|the last|the past)?\s*"
    r"(?P<number>\d+|[零一二两三四五六七八九十半]+|a|an|one)?\s*"
    r"(?P<unit>" + "|".join(sorted(map(re.escape, _UNITS), key=len, reverse=True)) + r")"
    r"\s*(?:内|以内|之内|以来|的)?"
)

_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def resolve_window(said: str, *, now: dt.datetime, default_limit: str) -> PostWindow:
    """把原话换算成这一跑的采集窗口。

    输入：
        said           intent 里的 time_said，用户关于时间的原话，如 '最近两个月'；没说是 ''
        now            现在，带时区。日期按 UTC 那一天算
        default_limit  没说或换算不了时用的那一档，discovery.yaml 的 posted_within，如 'month'
    输出：PostWindow，见上面那个类的例子

    判断顺序：

        一、原话是空串               -> 默认那一档，understood=False
        二、是「不限时间」那几句     -> limit='any'
        三、是「今天 / 这周 / 这个月 / 今年」 -> 从日历上那一天算起
        四、是「最近 N 个单位」     -> 从现在往回数 N 个单位
        五、都不是                   -> 默认那一档，understood=False，卡片上说换算不了

    例：now=2026-09-28 11:00 UTC
        '最近两个月' -> since='2026-07-28'
        '最近十天'   -> since='2026-09-18'
        '近半年'     -> since='2026-03-28'
        '这周'       -> since='2026-09-28'（09-28 是周一）
        '最近 6 小时' -> since='2026-09-28T05:00:00Z'
        '今年上半年' -> 默认。它还有一个结束时间（6 月底），postedLimitDate 只管得了开始那一头
    """
    text = " ".join(said.split())
    if not text:
        return PostWindow(since=None, limit=default_limit, said="", understood=False)

    key = text.lower().rstrip("的")
    if key in _ANY:
        return PostWindow(since=None, limit="any", said=text, understood=True)

    today = now.astimezone(dt.UTC).date()
    calendar = _CALENDAR.get(key)
    if calendar:
        start = {
            "day": today,
            "week": today - dt.timedelta(days=today.weekday()),
            "month": today.replace(day=1),
            "year": today.replace(month=1, day=1),
        }[calendar]
        return PostWindow(since=start.isoformat(), limit=None, said=text, understood=True)

    match = _RELATIVE.fullmatch(key)
    number = _number(match.group("number")) if match else None
    if match is None or number is None or number <= 0:
        return PostWindow(since=None, limit=default_limit, said=text, understood=False)

    kind, per_unit = _UNITS[match.group("unit")]
    amount = number * per_unit
    if kind == "hours":
        # 输入   6 小时    输出   '2026-09-28T05:00:00Z'（小时要带时刻，只给日期会变成「从今天零点起」）
        cutoff = now.astimezone(dt.UTC) - dt.timedelta(hours=amount)
        return PostWindow(since=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"), limit=None, said=text, understood=True)
    if kind == "days":
        # 半周、半天这种不是整天的，往前多算半天也不影响「从哪天起」，取整就行
        return PostWindow(
            since=(today - dt.timedelta(days=int(amount))).isoformat(), limit=None, said=text, understood=True
        )
    # 月：半个月折成 15 天；整月按日历往回数
    if amount != int(amount):
        return PostWindow(
            since=(today - dt.timedelta(days=round(amount * 30))).isoformat(),
            limit=None,
            said=text,
            understood=True,
        )
    return PostWindow(
        since=_months_before(today, int(amount)).isoformat(), limit=None, said=text, understood=True
    )


def _number(raw: str | None) -> float | None:
    """数字那一段换成数。读不懂返回 None。

    输入 -> 输出：None / 'a' / 'one' -> 1（「past week」「a month」）；'3' -> 3；'两' -> 2；
                  '十' -> 10；'十五' -> 15；'二十' -> 20；'半' -> 0.5；'一百' -> None（这么远的不认）
    """
    if raw is None or raw in ("a", "an", "one"):
        return 1
    if raw.isdigit():
        return int(raw)
    if raw == "半":
        return 0.5
    if raw in _DIGITS:
        return _DIGITS[raw]
    # 十、十五、二十、二十五：只认到九十九
    tens, sep, ones = raw.partition("十")
    if sep and (not tens or tens in _DIGITS) and (not ones or ones in _DIGITS):
        return (_DIGITS[tens] if tens else 1) * 10 + (_DIGITS[ones] if ones else 0)
    return None


def _months_before(day: dt.date, months: int) -> dt.date:
    """往回数几个月，日历上的同一天；那个月没有这一天就取月底。

    输入 -> 输出：(2026-09-28, 2) -> 2026-07-28；(2026-03-31, 1) -> 2026-02-28；(2026-09-28, 12) -> 2025-09-28
    """
    total = day.year * 12 + (day.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    # 下个月 1 号往前一天，就是这个月的最后一天
    first_of_next = dt.date(year + (month == 12), month % 12 + 1, 1)
    last_day = (first_of_next - dt.timedelta(days=1)).day
    return dt.date(year, month, min(day.day, last_day))
