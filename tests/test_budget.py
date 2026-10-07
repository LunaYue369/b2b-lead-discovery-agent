"""月度上限的测试：账期怎么切、配置写坏了会怎样、闸门在哪个数上放行和拒绝、它排在花钱之前。

不连库：check_monthly_cap 里查已花的两句（对话采集、找联系方式）换成桩。两条 SQL 本身在真库上跑过
（找联系方式那句在 scripts/verify_contacts_db.py 里）。
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
from decimal import Decimal

import pytest

from persistence import contacts_db, discovery_db
from triggers.interactive_discovery.budget import (
    BudgetConfig,
    MonthlyCapReached,
    billing_period,
    check_monthly_cap,
    load_budget_config,
)

UTC = dt.UTC


def _at(year, month, day, hour=0):
    return dt.datetime(year, month, day, hour, tzinfo=UTC)


# ── 账期 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_at(2026, 9, 24, 14), (_at(2026, 9, 21), _at(2026, 10, 21))),  # 过了 21 号
        (_at(2026, 9, 21), (_at(2026, 9, 21), _at(2026, 10, 21))),  # 21 号当天零点就是新账期
        (_at(2026, 9, 20, 23), (_at(2026, 8, 21), _at(2026, 9, 21))),  # 还差一小时
        (_at(2026, 1, 5), (_at(2025, 12, 21), _at(2026, 1, 21))),  # 跨年往回
        (_at(2026, 12, 25), (_at(2026, 12, 21), _at(2027, 1, 21))),  # 跨年往后
    ],
)
def test_billing_period_cuts_on_the_billing_day(now, expected):
    assert billing_period(now, 21) == expected


def test_a_time_in_another_timezone_is_cut_by_its_utc_date():
    """东八区 9-21 早上 7 点，UTC 还是 9-20 晚上 11 点：还在上一个账期。

    不先换成 UTC 的话，按本地日期就被算进了新账期，已花会从零开始算，闸门等于白开一次。
    """
    beijing = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime(2026, 9, 21, 7, tzinfo=beijing)
    assert billing_period(now, 21)[0] == _at(2026, 8, 21)


# ── 配置 ────────────────────────────────────────────────────────────────────


def test_the_real_config_file_loads():
    assert load_budget_config() == BudgetConfig(monthly_cap_usd=13.0, billing_day=18)


@pytest.mark.parametrize(
    ("cap", "day", "message"),
    [
        (".nan", 21, "monthly_cap_usd"),  # NaN 跟什么比都是 False，闸门会永远放行
        ("0", 21, "monthly_cap_usd"),
        ("-5", 21, "monthly_cap_usd"),
        ("10", 29, "billing_day"),  # 2 月没有 29 号，账期起点算不出来
        ("10", 0, "billing_day"),
    ],
)
def test_a_broken_budget_config_fails_loudly(tmp_path, cap, day, message):
    path = tmp_path / "interactive.yaml"
    path.write_text(f"budget:\n  monthly_cap_usd: {cap}\n  billing_day: {day}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_budget_config(path)


# ── 闸门 ────────────────────────────────────────────────────────────────────

BUDGET = BudgetConfig(monthly_cap_usd=10.0, billing_day=21)
NOW = _at(2026, 9, 24, 14)


@pytest.fixture
def billed(monkeypatch):
    """查已花的两句换成桩。

    输出一个 dict：
        items      对话采集的计费条数，默认 0
        contacts   找联系方式真扣的钱，Decimal，默认 0
        since      记下被问的起点（两句问的应该是同一个起点）
    """
    env = {"items": 0, "contacts": Decimal("0"), "since": None, "contacts_since": None}

    def fake(cur, *, since):
        env["since"] = since
        return env["items"]

    def fake_contacts(cur, *, since):
        env["contacts_since"] = since
        return env["contacts"]

    monkeypatch.setattr(discovery_db, "interactive_billed_items", fake)
    monkeypatch.setattr(contacts_db, "spend_since", fake_contacts)
    return env


def test_well_under_the_cap_lets_it_run_and_reports_what_was_spent(billed):
    """已花 615 条 x 0.002 = 1.23，加报价 0.15 远没到 10：放行，交回已花。"""
    billed["items"] = 615

    spent = check_monthly_cap(None, estimate_usd=0.15, now=NOW, budget=BUDGET, unit_price_usd=0.002)

    assert spent == pytest.approx(1.23)
    assert billed["since"] == _at(2026, 9, 21)


def test_contact_spending_counts_toward_the_same_cap(billed):
    """找联系方式花的钱跟对话采集加在一起算：采集 9.50 + 找联系方式 0.45 + 报价 0.15 = 10.10，拒。

    这条红了的症状：找联系方式天天在花，采集那边的闸却以为还有很多钱，两边加起来冲过 10 美元
    """
    billed["items"] = 4750  # 4750 x 0.002 = 9.50
    billed["contacts"] = Decimal("0.45000")

    with pytest.raises(MonthlyCapReached) as caught:
        check_monthly_cap(None, estimate_usd=0.15, now=NOW, budget=BUDGET, unit_price_usd=0.002)

    assert caught.value.spent_usd == pytest.approx(9.95)
    # 两句问的是同一个账期起点
    assert billed["since"] == billed["contacts_since"] == _at(2026, 9, 21)


def test_landing_exactly_on_the_cap_is_still_allowed(billed):
    """已花 9.85 + 报价 0.15 = 10.00，正好等于上限：放行。超过才拒，不是到了就拒。"""
    billed["items"] = 4925  # 4925 x 0.002 = 9.85

    check_monthly_cap(None, estimate_usd=0.15, now=NOW, budget=BUDGET, unit_price_usd=0.002)


def test_a_float_tail_does_not_turn_exactly_the_cap_into_over_it(billed):
    """4850 x 0.002 + 0.3 在浮点数里是 10.000000000000002：要按 10.00 算，放行。

    这条红了的症状：已花加报价正好是上限，却被拒了，而回给用户的话里印的两个数加起来明明是 $10.00。
    """
    billed["items"] = 4850

    check_monthly_cap(None, estimate_usd=0.3, now=NOW, budget=BUDGET, unit_price_usd=0.002)


def test_going_over_the_cap_refuses_and_says_when_it_resets(billed):
    """已花 9.95 + 报价 0.15 = 10.10：拒跑。异常里说清已花、报价、上限、哪天恢复。"""
    billed["items"] = 4975  # 4975 x 0.002 = 9.95

    with pytest.raises(MonthlyCapReached) as caught:
        check_monthly_cap(None, estimate_usd=0.15, now=NOW, budget=BUDGET, unit_price_usd=0.002)

    err = caught.value
    assert (err.spent_usd, err.estimate_usd, err.cap_usd) == (pytest.approx(9.95), 0.15, 10.0)
    assert err.resets_at == _at(2026, 10, 21)
    assert str(err) == (
        "本账期（2026-09-21 起）对话采集和找联系方式已经花了 $9.95，这次报价 $0.15，"
        "加起来会超过上限 $10.00。2026-10-21 开始的下一个账期恢复。"
    )


def test_run_harvest_checks_the_cap_before_it_writes_or_harvests():
    """run_harvest 里查上限那一句，必须排在第一次写库（upsert_query）和采集（harvest_once）前面。

    顺序反了的话，闸门照样会拒，但拒之前 queries 表已经多了一行、或者 Apify 已经扣了钱 ——
    测试全绿，钱照花。这条用 ast 看代码里的先后，不用真跑。
    """
    source = pathlib.Path("triggers/interactive_discovery/query/nodes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_harvest = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_harvest")

    def first_call(name):
        lines = [
            node.lineno
            for node in ast.walk(run_harvest)
            if isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) == name or getattr(node.func, "attr", None) == name)
        ]
        assert lines, f"run_harvest 里找不到 {name} 的调用"
        return min(lines)

    assert first_call("check_monthly_cap") < first_call("upsert_query")
    assert first_call("check_monthly_cap") < first_call("harvest_once")
