"""stats：排序分怎么算，以及它拒绝做的那几件事。

这里焊住的核心只有一条 —— 小样本不许靠裸比率插队。它不报错、看着还很
好看，而排在最前面的那条 query 每天都会被选中，吃掉 60% 的采集预算。
"""

from __future__ import annotations

import pytest

from models import QueryTotals
from query_intelligence.stats import (
    RankingConfig,
    compute,
    has_enough_samples,
    load_ranking_config,
    rank_score,
    wilson_lower_bound,
)

CONFIG = RankingConfig(
    min_executions=5, wilson_z=1.96, qualified_weight=0.6, novel_weight=0.4
)


def totals(
    *,
    executions: int = 1,
    unique: int = 10,
    researchable: int = 10,
    qualified: int = 0,
    novel: int = 0,
) -> QueryTotals:
    return QueryTotals(
        executions=executions,
        unique_companies=unique,
        researchable_leads=researchable,
        qualified_leads=qualified,
        novel_leads=novel,
    )


# ---------- 配置 ----------


def test_the_real_config_file_loads():
    """仓库里那份 discovery.yaml 要能被读出来，键名对不上当场知道。"""
    config = load_ranking_config()
    assert config.min_executions >= 1
    assert config.wilson_z > 0
    assert config.qualified_weight + config.novel_weight == pytest.approx(1.0)


def test_weights_that_do_not_add_up_are_refused(tmp_path):
    """权重之和不是 1 要炸。

    写成 0.6/0.3 不报错，只会让所有分一起变小 —— 排序不变，但报表上的
    绝对值跟历史对不上，而「这条 query 的分怎么突然掉了」会被当成
    质量问题去查。
    """
    path = tmp_path / "discovery.yaml"
    path.write_text(
        "query_ranking:\n"
        "  min_executions: 5\n"
        "  wilson_z: 1.96\n"
        "  weights: {qualified: 0.6, novel: 0.3}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="加起来要等于 1"):
        load_ranking_config(path)


def test_a_missing_key_blows_up_instead_of_defaulting(tmp_path):
    """键缺了当场 KeyError，不退化成默认值 —— 退化的后果是「实际用的
    权重跟配置说的不一样」，而它不报错。"""
    path = tmp_path / "discovery.yaml"
    path.write_text("query_ranking:\n  min_executions: 5\n  wilson_z: 1.96\n", encoding="utf-8")
    with pytest.raises(KeyError):
        load_ranking_config(path)


# ---------- Wilson 下界 ----------


def test_one_out_of_one_ranks_below_forty_out_of_a_hundred():
    """这套东西存在的全部理由：裸比率 100% 排在裸比率 40% 后面。"""
    assert wilson_lower_bound(1, 1, z=1.96) == pytest.approx(0.2065, abs=1e-4)
    assert wilson_lower_bound(40, 100, z=1.96) == pytest.approx(0.3094, abs=1e-4)
    assert wilson_lower_bound(1, 1, z=1.96) < wilson_lower_bound(40, 100, z=1.96)


def test_more_samples_of_the_same_ratio_score_higher():
    """同样的比率，样本越多分越高 —— 下界量的是「至少有多少」。"""
    small = wilson_lower_bound(2, 4, z=1.96)
    big = wilson_lower_bound(50, 100, z=1.96)
    assert small < big


def test_no_trials_scores_zero_not_something_in_the_middle():
    """没有证据就没有信用。

    给个中间值的话，一条从没分析过任何公司的 query 会排在一条实测很差的
    query 前面，而后者至少是有据可查的。
    """
    assert wilson_lower_bound(0, 0, z=1.96) == 0.0


def test_zero_hits_never_goes_negative():
    """下界算出来可能是负数，截到 0：负分在报表上只会引起误解。"""
    assert wilson_lower_bound(0, 10, z=1.96) == 0.0


def test_more_hits_than_trials_blows_up():
    """两个数不是同一个口径时要炸，不能安静地算出一个像模像样的小数。

    典型的错法是把「撞到的公司数」当成「分析过的数」传进来。
    """
    with pytest.raises(ValueError, match="不能多过"):
        wilson_lower_bound(11, 10, z=1.96)


# ---------- rank_score ----------


def test_rank_score_weighs_both_halves():
    """两个分量各按权重进来，一个都不能少。"""
    total = totals(executions=12, unique=418, researchable=140, qualified=61, novel=203)
    expected = 0.6 * wilson_lower_bound(61, 140, z=1.96) + 0.4 * wilson_lower_bound(
        203, 418, z=1.96
    )
    assert rank_score(total, config=CONFIG) == pytest.approx(expected)
    assert rank_score(total, config=CONFIG) == pytest.approx(0.3891, abs=1e-4)


def test_a_query_that_only_rediscovers_known_companies_is_punished():
    """合格率一样好，但一家新公司都没带来的那条要排在后面。

    只看 qualified 的话这两条一模一样，而那正是 novel 那一半要防的事。
    """
    fresh = totals(executions=10, unique=100, researchable=50, qualified=25, novel=60)
    stale = totals(executions=10, unique=100, researchable=50, qualified=25, novel=0)
    assert rank_score(fresh, config=CONFIG) > rank_score(stale, config=CONFIG)


def test_a_query_that_finds_lots_of_junk_is_punished():
    """新公司一样多，但没几家值得联系的那条要排在后面。"""
    good = totals(executions=10, unique=100, researchable=50, qualified=25, novel=60)
    junk = totals(executions=10, unique=100, researchable=50, qualified=1, novel=60)
    assert rank_score(good, config=CONFIG) > rank_score(junk, config=CONFIG)


def test_the_qualified_denominator_is_researchable_not_unique():
    """合格率的分母是「真花钱分析过的」，不是「撞到的公司数」。

    冷却期里没分析的那些没有分数，算进分母等于因为 cooldown 起作用而
    惩罚这条 query —— 而 cooldown 起作用正是我们要的。
    """
    # 两条 query 分析过的和过线的完全一样，差别只在撞到多少冷却期里的老公司。
    few_cooling = totals(executions=10, unique=60, researchable=50, qualified=25, novel=30)
    many_cooling = totals(executions=10, unique=200, researchable=50, qualified=25, novel=30)
    # 两条的分差必须正好等于 novel 那一半的分差 —— 也就是说合格那一半
    # 被 unique 的变化影响了 0。分母写错成 unique 的话这个等式立刻不成立。
    difference = rank_score(few_cooling, config=CONFIG) - rank_score(many_cooling, config=CONFIG)
    novel_only = 0.4 * (
        wilson_lower_bound(30, 60, z=1.96) - wilson_lower_bound(30, 200, z=1.96)
    )
    assert difference == pytest.approx(novel_only)
    assert difference > 0


# ---------- compute ----------


def test_compute_keeps_the_raw_support_alongside_the_score():
    """结论和原始支撑一起带出来：几周后要能回答「这个分当时怎么来的」。"""
    stats = compute({30: totals(executions=12, unique=418, researchable=140,
                                qualified=61, novel=203)}, config=CONFIG)
    entry = stats[30]
    assert entry.query_id == 30
    assert entry.executions == 12
    assert (entry.unique_companies, entry.researchable_leads) == (418, 140)
    assert (entry.qualified_leads, entry.novel_leads) == (61, 203)
    assert entry.rank_score == pytest.approx(0.3891, abs=1e-4)


def test_compute_does_not_invent_rows_for_queries_that_never_ran():
    """没跑过的 query 不在输出里，也不给它一行 0。

    selector 那边「在不在 stats 里」是有意义的：candidate 当 0 次执行排最前，
    proven 当记账不一致记一条 warning。补一行 0 会把后面那条告警静默掉。
    """
    assert compute({}, config=CONFIG) == {}


# ---------- 最小样本闸门 ----------


def test_samples_gate_is_about_count_not_quality():
    """跑够次数才够格被当成 proven 评价，跟它跑得多好无关。

    一条跑过两次、两次都撞上好线索的 query 下界能到 0.2 出头，在一个还
    很空的 proven 池里可能就是第一名，然后以 proven 的身份长期吃掉 60%
    的预算，而支撑它只有两次样本。
    """
    lucky = totals(executions=2, unique=2, researchable=2, qualified=2, novel=2)
    assert rank_score(lucky, config=CONFIG) > 0
    assert has_enough_samples(lucky, config=CONFIG) is False

    dull = totals(executions=5, unique=100, researchable=40, qualified=1, novel=2)
    assert has_enough_samples(dull, config=CONFIG) is True
