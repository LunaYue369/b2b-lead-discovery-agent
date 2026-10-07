"""selector 的测试。

焊住的三类，全是「不报错但今天跑错了东西」：

    额度不让渡   pinned 和 proven 空着的日子会跑 0 条，而报表看起来正常
    让渡不公平   pinned 把富余额度吃光，candidate 永远跑不满最小样本
    排序不确定   同样的库状态两次选出不同的 query，「今天为什么只有两条
                 线索」就没有立足点

不连库也不读文件：候选和指标都从参数进，这正是这一层被要求做到的事。
"""

from __future__ import annotations

import pytest

from models import QueryPool, QuerySpec, QueryStats, SourceType
from query_intelligence.selector import (
    POOL_ORDER,
    SelectionConfig,
    load_selection_config,
    select_queries,
)

RATIOS = {QueryPool.PINNED: 0.2, QueryPool.PROVEN: 0.6, QueryPool.CANDIDATE: 0.2}


def spec(query_id: int, pool: QueryPool, *, cadence_days: int = 1) -> QuerySpec:
    return QuerySpec(
        query_id=query_id,
        text=f"query-{query_id}",
        source_type=SourceType.LINKEDIN_POST,
        pool=pool,
        cadence_days=cadence_days,
        filters={},
    )


def stats_of(query_id: int, *, executions: int = 0, rank_score: float = 0.0) -> QueryStats:
    return QueryStats(
        query_id=query_id,
        executions=executions,
        rank_score=rank_score,
        unique_companies=0,
        researchable_leads=0,
        qualified_leads=0,
        novel_leads=0,
    )


def config(budget: int) -> SelectionConfig:
    return SelectionConfig(daily_query_budget=budget, pool_ratios=dict(RATIOS))


def outcome(plan, pool):
    return next(item for item in plan.per_pool if item.pool is pool)


# ---------- 配置 ----------


def test_real_config_file_loads():
    """仓库里那份 discovery.yaml 必须能读出来，而且比例合法。"""
    loaded = load_selection_config()
    assert loaded.daily_query_budget >= 1
    assert set(loaded.pool_ratios) == set(POOL_ORDER)


def test_ratios_that_do_not_add_up_are_refused(tmp_path):
    """写成 0.2/0.6/0.1 的话每天会安静地少跑一成，没有任何报错。"""
    path = tmp_path / "discovery.yaml"
    path.write_text(
        "daily_query_budget: 2\npool_ratios:\n  pinned: 0.2\n  proven: 0.6\n  candidate: 0.1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="加起来要等于 1"):
        load_selection_config(path)


def test_missing_pool_is_refused(tmp_path):
    """少一档等于那一档永远分不到额度，而 selector 照样跑得好好的。"""
    path = tmp_path / "discovery.yaml"
    path.write_text(
        "daily_query_budget: 2\npool_ratios:\n  pinned: 0.4\n  proven: 0.6\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="少了这几档"):
        load_selection_config(path)


# ---------- 今天的真实处境 ----------


def test_empty_pinned_and_proven_hand_everything_to_candidate():
    """2026-08-31 的真实状态：三条种子全是 candidate，pinned 和 proven 空着。

    照字面按 20/60/20 分的话，今天会跑 0 条 —— 而那看起来完全正常，
    像是「今天就是没轮到」。额度让渡不是可选项，是这一天能不能跑起来。
    """
    pickable = [spec(29, QueryPool.CANDIDATE, cadence_days=7),
                spec(30, QueryPool.CANDIDATE),
                spec(31, QueryPool.CANDIDATE)]
    plan = select_queries(pickable, stats={}, config=config(2))

    assert [s.query_id for s in plan.chosen] == [29, 30]
    assert plan.unmet_budget == 0
    candidate = outcome(plan, QueryPool.CANDIDATE)
    assert (candidate.target, candidate.available, candidate.chosen) == (0, 3, 2)
    assert candidate.received == 2  # 两条都是让渡来的


def test_budget_that_cannot_be_filled_is_reported_not_swallowed():
    """额度用不完要写进返回值。这是「配额是天花板不是地板」的另一半：
    多了要明示截断，少了也要明示，否则「今天为什么只有一条线索」查不到源头。"""
    plan = select_queries([spec(29, QueryPool.CANDIDATE)], stats={}, config=config(2))
    assert len(plan.chosen) == 1
    assert plan.unmet_budget == 1


# ---------- 额度让渡的公平性 ----------


def test_spare_budget_is_not_all_eaten_by_pinned():
    """pinned 有的是条数、proven 不够，富余额度不能全归 pinned。

    先各取 2/3/2，还剩 3 条。按「POOL_ORDER 靠前的先吃满」的话
    pinned 全拿走，candidate 一条都多不到 —— 而 candidate 池的存在意义
    就是取得样本，被长期挤掉就永远跑不满最小样本、永远评价不了。
    """
    pickable = (
        [spec(i, QueryPool.PINNED) for i in range(1, 21)]
        + [spec(i, QueryPool.PROVEN) for i in range(100, 103)]
        + [spec(i, QueryPool.CANDIDATE) for i in range(200, 205)]
    )
    stats = {i: stats_of(i, rank_score=0.5) for i in range(100, 103)}
    plan = select_queries(pickable, stats=stats, config=config(10))

    assert len(plan.chosen) == 10
    assert outcome(plan, QueryPool.PINNED).chosen == 4
    assert outcome(plan, QueryPool.PROVEN).chosen == 3
    assert outcome(plan, QueryPool.CANDIDATE).chosen == 3


def test_full_pools_get_exactly_their_ratio():
    """三个池子都够用时，比例真的起作用，没有任何让渡。"""
    pickable = (
        [spec(i, QueryPool.PINNED) for i in range(1, 4)]
        + [spec(i, QueryPool.PROVEN) for i in range(100, 108)]
        + [spec(i, QueryPool.CANDIDATE) for i in range(200, 205)]
    )
    stats = {i: stats_of(i, rank_score=1.0 / i) for i in range(100, 108)}
    plan = select_queries(pickable, stats=stats, config=config(10))

    assert [outcome(plan, pool).chosen for pool in POOL_ORDER] == [2, 6, 2]
    assert [outcome(plan, pool).received for pool in POOL_ORDER] == [0, 0, 0]


# ---------- 池内排序 ----------


def test_proven_is_ranked_by_score_not_by_id():
    pickable = [spec(100, QueryPool.PROVEN), spec(101, QueryPool.PROVEN)]
    stats = {100: stats_of(100, rank_score=0.31), 101: stats_of(101, rank_score=0.44)}
    plan = select_queries(pickable, stats=stats, config=config(1))
    assert [s.query_id for s in plan.chosen] == [101]


def test_candidate_is_ranked_by_fewest_runs_not_by_performance():
    """这个池子的目的是取得样本，不是产出线索。

    按表现排的话样本少的永远排在后面、永远拿不到额度，于是永远跑不满
    最小样本 —— 那正是「candidate 池只进不出」的死锁。所以哪怕 id=200
    的 rank_score 高得多，跑得少的 id=201 也要先跑。
    """
    pickable = [spec(200, QueryPool.CANDIDATE), spec(201, QueryPool.CANDIDATE)]
    stats = {
        200: stats_of(200, executions=9, rank_score=0.9),
        201: stats_of(201, executions=1, rank_score=0.0),
    }
    plan = select_queries(pickable, stats=stats, config=config(1))
    assert [s.query_id for s in plan.chosen] == [201]


def test_pinned_keeps_the_order_it_came_in():
    """pinned 沿用 pickable_queries 的顺序（最久没跑的在前），不重排。"""
    pickable = [spec(5, QueryPool.PINNED), spec(1, QueryPool.PINNED)]
    plan = select_queries(pickable, stats={}, config=config(2))
    assert [s.query_id for s in plan.chosen] == [5, 1]


def test_selection_is_deterministic_across_input_order():
    """同样的库状态必须选出同样的 query。

    候选的先后不该改变结果 —— 不然「今天为什么只选了这几条」两次问会
    得到两个答案，而两次都不报错。
    """
    pickable = [spec(i, QueryPool.CANDIDATE) for i in (203, 200, 202, 201)]
    stats = {i: stats_of(i, executions=0) for i in (200, 201, 202, 203)}
    first = select_queries(pickable, stats=stats, config=config(2))
    second = select_queries(list(reversed(pickable)), stats=stats, config=config(2))
    assert [s.query_id for s in first.chosen] == [s.query_id for s in second.chosen] == [200, 201]


# ---------- 记账不一致 ----------


def test_proven_without_stats_is_warned_not_crashed():
    """一条 proven query 在 stats 里没有指标，是记账对不上，不是数据坏了。

    raise 的话 7 点的定时任务今天一条线索都不出，代价比排到最后大得多。
    但也不能静默 —— 它会出现在 warnings 里。
    """
    pickable = [spec(100, QueryPool.PROVEN), spec(101, QueryPool.PROVEN)]
    plan = select_queries(pickable, stats={101: stats_of(101, rank_score=0.2)}, config=config(2))

    assert [s.query_id for s in plan.chosen] == [101, 100]  # 没指标的排最后
    assert len(plan.warnings) == 1
    assert "100" in plan.warnings[0]


def test_shortfall_and_received_add_up():
    """让出的总数 = 接到的总数 + 没人接得住的那部分。

    这条对账是 PoolOutcome 不记「从哪个池借的」的理由：谁让出几条、
    谁接了几条各自看得见就够了，而两个捐方对两个受方时那个配对
    本来就没有事实可依。
    """
    pickable = (
        [spec(i, QueryPool.PROVEN) for i in range(100, 104)]
        + [spec(i, QueryPool.CANDIDATE) for i in range(200, 209)]
    )
    stats = {i: stats_of(i, rank_score=1.0 / i) for i in range(100, 104)}
    plan = select_queries(pickable, stats=stats, config=config(10))

    assert outcome(plan, QueryPool.PINNED).shortfall == 2  # 目标 2，一条都没有
    assert outcome(plan, QueryPool.PROVEN).shortfall == 2  # 目标 6，只有 4 条
    assert outcome(plan, QueryPool.CANDIDATE).shortfall == 0
    assert outcome(plan, QueryPool.CANDIDATE).received == 4

    given = sum(item.shortfall for item in plan.per_pool)
    taken = sum(item.received for item in plan.per_pool)
    assert given == taken + plan.unmet_budget


def test_shortfall_when_nobody_can_take_it():
    """没人接得住的那部分落在 unmet_budget 上，不会凭空消失。"""
    plan = select_queries([spec(200, QueryPool.CANDIDATE)], stats={}, config=config(10))
    given = sum(item.shortfall for item in plan.per_pool)
    assert given == 0 + plan.unmet_budget == 9
