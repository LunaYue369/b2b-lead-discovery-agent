"""apify.run_actor 的测试：三步走的顺序、失败怎么报、真扣的钱怎么取。

不连网：httpx.Client 换成一个用假网络层（httpx.MockTransport）的版本，每个请求由测试
按剧本回答，一个字节都不出本机。

conftest 会把 apify.run_actor 换成当场炸的版本（防测试花钱），所以这里在 import 的时候
先把真函数拿住：模块 import 早于 conftest 那个 autouse fixture 生效。
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from adapters import apify

_REAL_RUN_ACTOR = apify.run_actor


@pytest.fixture
def fake_apify(monkeypatch):
    """按剧本回答的假 Apify。

    输出：一个 dict，测试可以在调用前改它
        statuses   第二步每问一次依次回答的状态，默认先 RUNNING 再 SUCCEEDED
        usage      跑完那一次回复里的 usageTotalUsd，默认 0.020050000000000002（Apify 真给过这样带尾巴的数）
        items      第三步回的数据
        accounted  跑完那一次回复里的 accountedChargedEventCounts（算进钱里的次数），None 是跟 events 一样
        later      跑完之后再问状态（第四步等扣费记完）时依次回答的
                   (usageTotalUsd, chargedEventCounts, accountedChargedEventCounts)
        seen       依次收到的请求 (方法, 路径)，断言顺序用
    """
    script = {
        "statuses": ["RUNNING", "SUCCEEDED"],
        "usage": 0.020050000000000002,
        "events": {"apify-actor-start": 1, "apify-default-dataset-item": 5},
        "items": [{"id": "100000002"}],
        "accounted": None,
        "later": [],
        "seen": [],
    }

    def answer(request: httpx.Request) -> httpx.Response:
        script["seen"].append((request.method, request.url.path))
        # token 必须在请求头里，不在 URL 里
        assert request.headers["Authorization"] == "Bearer test-token"
        assert "token" not in str(request.url)

        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "defaultDatasetId": "ds1"}})
        if request.url.path.endswith("/actor-runs/run1"):
            if script["statuses"]:
                data = {"status": script["statuses"].pop(0)}
                if data["status"] == "SUCCEEDED":
                    data.update(
                        usageTotalUsd=script["usage"],
                        chargedEventCounts=script["events"],
                        accountedChargedEventCounts=script["accounted"] or script["events"],
                    )
            else:
                usage, events, accounted = script["later"].pop(0)
                data = {
                    "status": "SUCCEEDED",
                    "usageTotalUsd": usage,
                    "chargedEventCounts": events,
                    "accountedChargedEventCounts": accounted,
                }
            return httpx.Response(200, json={"data": data})
        if request.url.path.endswith("/datasets/ds1/items"):
            return httpx.Response(200, json=script["items"])
        raise AssertionError(f"剧本里没有这个请求：{request.method} {request.url}")

    real_client = httpx.Client
    monkeypatch.setattr(
        apify.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(answer), **kw)
    )
    monkeypatch.setattr(apify.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("APIFY_TOKEN", "test-token")
    return script


def test_three_steps_in_order_and_the_real_cost_comes_back(fake_apify):
    """起 run、问两次状态、取数据；钱取 Apify 给的那个数，去掉浮点尾巴。"""
    run = _REAL_RUN_ACTOR("harvestapi~linkedin-company", {"companies": ["x"]})

    assert fake_apify["seen"] == [
        ("POST", "/v2/acts/harvestapi~linkedin-company/runs"),
        ("GET", "/v2/actor-runs/run1"),
        ("GET", "/v2/actor-runs/run1"),
        ("GET", "/v2/datasets/ds1/items"),
    ]
    assert run.items == [{"id": "100000002"}]
    # 0.020050000000000002 -> 0.02005，五位小数，跟 actor_spend.cost_usd 同一个精度
    assert run.cost_usd == Decimal("0.02005") and isinstance(run.cost_usd, Decimal)
    assert run.charged_events == {"apify-actor-start": 1, "apify-default-dataset-item": 5}


def test_waits_until_every_item_is_charged_before_reading_the_cost(fake_apify):
    """跑完那一刻扣费还没记全：再问，直到按条收钱的次数追上数据条数，取那时的钱。

    剧本照实测：3 条数据，跑完那一刻只记了 1 条（$0.00405），再问记到 2 条，再问记全 3 条。
    """
    fake_apify["items"] = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    fake_apify["usage"] = 0.00405
    fake_apify["events"] = {"apify-actor-start": 1, "apify-default-dataset-item": 1}
    two = {"apify-actor-start": 1, "apify-default-dataset-item": 2}
    three = {"apify-actor-start": 1, "apify-default-dataset-item": 3}
    fake_apify["later"] = [(0.00805, two, two), (0.01205, three, three)]

    run = _REAL_RUN_ACTOR("harvestapi~linkedin-company", {})

    assert run.cost_usd == Decimal("0.01205")
    assert run.charged_events == three
    # 取完数据之后又问了两次状态
    assert fake_apify["seen"][-2:] == [("GET", "/v2/actor-runs/run1")] * 2
    assert fake_apify["later"] == []


def test_counts_that_are_in_but_not_yet_in_the_money_are_waited_for(fake_apify):
    """次数已经记全、钱还没跟上：再问，等算进钱里的次数也追上。

    剧本照单跑一次公司详情的每秒实测：+6.5 秒 dataset-item 记了 1 次、算进钱里 0 次、$0.00005；
    +7.6 秒两边都是 1、$0.00405。只看次数的话会在 +6.5 秒那一刻停下，记成 $0.00005
    """
    fake_apify["items"] = [{"id": "100000002"}]
    fake_apify["usage"] = 0.00005
    fake_apify["events"] = {"apify-actor-start": 1, "apify-default-dataset-item": 1}
    fake_apify["accounted"] = {"apify-actor-start": 1, "apify-default-dataset-item": 0}
    one = {"apify-actor-start": 1, "apify-default-dataset-item": 1}
    fake_apify["later"] = [(0.00405, one, one)]

    run = _REAL_RUN_ACTOR("harvestapi~linkedin-company", {})

    assert run.cost_usd == Decimal("0.00405")
    assert fake_apify["later"] == []


def test_charges_that_never_catch_up_do_not_fail_the_run(fake_apify, capsys):
    """等到 settle_seconds 还没记全：不炸，用当时的数，印一行提醒。数据已经拿到了。"""
    fake_apify["items"] = [{"id": "1"}, {"id": "2"}]
    fake_apify["usage"] = 0.00405
    fake_apify["events"] = {"apify-actor-start": 1, "apify-default-dataset-item": 1}

    run = _REAL_RUN_ACTOR("harvestapi~linkedin-company", {}, settle_seconds=-1)

    assert run.items == [{"id": "1"}, {"id": "2"}]
    assert run.cost_usd == Decimal("0.00405")
    assert "扣费还没记完" in capsys.readouterr().out


def test_a_failed_run_raises_instead_of_returning_nothing(fake_apify):
    """run 跑挂了：抛，不取数据。静默返回空列表会被当成「查过了、什么都没有」。"""
    fake_apify["statuses"] = ["RUNNING", "FAILED"]

    with pytest.raises(RuntimeError, match="FAILED"):
        _REAL_RUN_ACTOR("harvestapi~linkedin-company", {})

    assert ("GET", "/v2/datasets/ds1/items") not in fake_apify["seen"]


def test_missing_usage_is_none_not_a_made_up_number(fake_apify):
    """跑完的回复里没有 usageTotalUsd：交回 None，不编一个数。"""
    fake_apify["usage"] = None

    assert _REAL_RUN_ACTOR("harvestapi~linkedin-company", {}).cost_usd is None


def test_a_dataset_that_is_not_a_list_blows_up(fake_apify):
    """数据那一步回的不是列表：当场炸。拿 dict 去数条数会数出键的个数，而且不报错。"""
    fake_apify["items"] = {"data": []}

    with pytest.raises(RuntimeError, match="不是 item 列表"):
        _REAL_RUN_ACTOR("harvestapi~linkedin-company", {})
