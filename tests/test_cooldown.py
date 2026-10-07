"""cooldown 分级的测试。

焊住的四类，全是「不报错但几周后表现成一笔莫名其妙的花费」：

    分档看总分     品类为零而其余六维漂亮时总分未必低，按总分判会把
                   完全不相关的公司留在复查队列里
    退避不生效     认不出来的供应商会一直落在最短那一档，每两周白花一次钱
    退避不重置     真买方出了新事件、分数跳上来，却还背着上一轮的翻倍
    第一次就算 strike  头一回分析没有「上一次」可比，算它没起色等于
                   把所有公司的第一个冷却期都翻了一倍

不连库也不读文件：打分和既有状态都从参数进。
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from discovery.cooldown import (
    CooldownConfig,
    load_cooldown_config,
    next_cooldown,
)
from models import Assessment, DeterministicScores, LeadStatus, LlmAssessment

NOW = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)

CONFIG = CooldownConfig(
    qualified_score=60,
    qualified_days=30,
    in_range_days=14,
    marginal_days=60,
    in_range_icp_fit=5,
    max_days=180,
)


def assessment(*, icp_fit: int, score: int) -> Assessment:
    """拼一个只有分档要用的两个数是真的的 Assessment。

    输入：品类分和总分
    输出：一个 Assessment

    另外五维填的是不影响分档的占位值 —— 这个模块只看 icp_fit 和 score，
    把别的维度也写成有意义的数，会让读测试的人以为它们参与了判断。
    """
    return Assessment(
        llm=LlmAssessment(
            icp_fit=icp_fit,
            signal_strength=5,
            seniority=5,
            moq_fit=5,
            analysis="placeholder",
            evidence_quotes=("placeholder",),
        ),
        det=DeterministicScores(recency=5, reachability=5, corroboration=3),
        score=score,
    )


def decide(
    *, icp_fit: int, score: int, previous_score: int | None = None, strikes: int = 0
):
    return next_cooldown(
        assessment(icp_fit=icp_fit, score=score),
        previous_score=previous_score,
        strikes=strikes,
        config=CONFIG,
        now=NOW,
    )


# ── 分档 ──────────────────────────────────────────────────────────


def test_off_category_is_terminal():
    got = decide(icp_fit=0, score=20)
    assert got.tier == "off_category"
    assert got.status is LeadStatus.EXCLUDED
    # 库里那条约束要求终态不带复查时刻。
    assert got.cooldown_until is None


def test_off_category_beats_a_high_score():
    """品类为零就是终态，哪怕总分过了线。

    七维加权之后，一家品类完全不对但事件很硬、发帖人很资深的公司总分
    可以很高。分档先看 icp_fit 就是为了挡住这一类。
    """
    got = decide(icp_fit=0, score=88)
    assert got.tier == "off_category"
    assert got.status is LeadStatus.EXCLUDED


def test_qualified_gets_the_medium_cooldown():
    got = decide(icp_fit=9, score=71)
    assert got.tier == "qualified"
    assert got.cooldown_until == NOW + timedelta(days=30)
    # 过线只说明够格被推，真的推出去是 delivery 的事，这里不替它记账。
    assert got.status is LeadStatus.NEW


def test_in_range_gets_the_shortest_cooldown():
    got = decide(icp_fit=7, score=34)
    assert got.tier == "in_range"
    assert got.cooldown_until == NOW + timedelta(days=14)


def test_marginal_gets_the_long_cooldown():
    got = decide(icp_fit=3, score=22)
    assert got.tier == "marginal"
    assert got.cooldown_until == NOW + timedelta(days=60)


def test_icp_fit_on_the_line_counts_as_in_range():
    """分档线是「大于等于」。写成大于的话，配置里那个数的含义就反了。"""
    assert decide(icp_fit=5, score=30).tier == "in_range"
    assert decide(icp_fit=4, score=30).tier == "marginal"


# ── 退避 ──────────────────────────────────────────────────────────


def test_first_analysis_is_not_a_strike():
    """头一回分析没有上一次可比，不算「没起色」。"""
    got = decide(icp_fit=7, score=34, previous_score=None, strikes=0)
    assert got.strikes == 0
    assert got.cooldown_until == NOW + timedelta(days=14)


def test_no_improvement_doubles_the_cooldown():
    got = decide(icp_fit=7, score=34, previous_score=34, strikes=0)
    assert got.strikes == 1
    assert got.cooldown_until == NOW + timedelta(days=28)


def test_strikes_accumulate():
    """认不出来的供应商靠这条自己沉下去：品类对、分数不会变好。"""
    got = decide(icp_fit=7, score=34, previous_score=34, strikes=2)
    assert got.strikes == 3
    assert got.cooldown_until == NOW + timedelta(days=14 * 8)


def test_improvement_resets_the_strikes():
    """出了新事件、分数跳上来，就不该再背着上一轮的翻倍。"""
    got = decide(icp_fit=9, score=71, previous_score=34, strikes=3)
    assert got.strikes == 0
    assert got.cooldown_until == NOW + timedelta(days=30)


def test_a_lower_score_still_counts_as_no_improvement():
    """比上次还低当然不是起色。判据是「有没有变好」不是「有没有变化」。"""
    got = decide(icp_fit=7, score=20, previous_score=34, strikes=1)
    assert got.strikes == 2


def test_backoff_is_capped():
    got = decide(icp_fit=7, score=34, previous_score=34, strikes=40)
    assert got.cooldown_until == NOW + timedelta(days=180)


def test_terminal_keeps_the_strikes_untouched():
    """终态不再复查，退避次数对它没有意义，但也没有理由在这里清掉。"""
    assert decide(icp_fit=0, score=20, previous_score=20, strikes=2).strikes == 2


# ── 时区 ──────────────────────────────────────────────────────────


def test_naive_now_is_rejected():
    """不带时区的时刻落进 timestamptz，会被库按服务器时区解释。"""
    with pytest.raises(ValueError, match="时区"):
        next_cooldown(
            assessment(icp_fit=7, score=34),
            previous_score=None,
            strikes=0,
            config=CONFIG,
            now=datetime(2026, 9, 1, 7, 0),
        )


# ── 配置 ──────────────────────────────────────────────────────────


def test_repo_config_loads():
    """仓库里那份配置本身要是合法的，否则线上第一次跑才发现。"""
    config = load_cooldown_config()
    assert config.in_range_days < config.qualified_days < config.marginal_days
    assert config.max_days >= config.marginal_days


def write_config(tmp_path: pathlib.Path, **overrides) -> pathlib.Path:
    """写一份临时配置，只改指定的那几个键。

    输入：临时目录；要覆盖的键值
    输出：写好的文件路径

    先取仓库里那份的形状，再把 overrides 铺上去 ——
    这样测试只表达「哪个键坏了」，不用重抄一遍全部字段。
    """
    base = {
        "qualified_score": 60,
        "cooldown": {
            "qualified_days": 30,
            "in_range_days": 14,
            "marginal_days": 60,
            "in_range_icp_fit": 5,
            "max_days": 180,
        },
    }
    for key, value in overrides.items():
        if key == "qualified_score":
            base[key] = value
        else:
            base["cooldown"][key] = value
    path = tmp_path / "discovery.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return path


def test_missing_key_raises(tmp_path):
    """键缺了当场炸，不退化成一个默认值。"""
    path = tmp_path / "discovery.yaml"
    path.write_text(yaml.safe_dump({"qualified_score": 60}), encoding="utf-8")
    with pytest.raises(KeyError):
        load_cooldown_config(path)


def test_base_days_over_the_cap_raises(tmp_path):
    """上限比某一档的基础值还小，那一档一进来就被截短，设计意图静默失效。"""
    with pytest.raises(ValueError, match="max_days"):
        load_cooldown_config(write_config(tmp_path, max_days=30))


def test_zero_days_raises(tmp_path):
    with pytest.raises(ValueError, match="at least|至少"):
        load_cooldown_config(write_config(tmp_path, in_range_days=0))


def test_icp_fit_line_out_of_range_raises(tmp_path):
    with pytest.raises(ValueError, match="in_range_icp_fit"):
        load_cooldown_config(write_config(tmp_path, in_range_icp_fit=11))


def test_score_line_out_of_range_raises(tmp_path):
    with pytest.raises(ValueError, match="qualified_score"):
        load_cooldown_config(write_config(tmp_path, qualified_score=140))
