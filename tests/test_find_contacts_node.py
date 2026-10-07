"""图里 find_contacts 那个节点：只开连接、调 find_contacts_for_run、回一句话；出错不往外抛。

找哪几家、怎么找在 tests/test_contacts.py 测。这里焊的是两件接线上的事：

    出错不往外抛     找联系方式出错不能让 deliver 走不到、卡片推不出去
    只追加一句话     不写别的 state 字段
"""

from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal

from triggers import contacts
from triggers.interactive_discovery.query import nodes

STATE = {"run_id": 45, "messages": ["用户: 跑", "AI: 采完了。run 45，completed，发现 8 家公司"]}
CONFIG = {"configurable": {"thread_id": "slack:C0TEST:1790126072.000100"}}


def test_a_normal_round_appends_one_line_about_what_it_found(monkeypatch):
    """正常：连接交给 find_contacts_for_run，回一句这一次找了多少、花了多少。"""
    seen = {}

    def fake_for_run(conn, *, run_id, now):
        seen["run_id"] = run_id
        return contacts.ContactsReport(
            lead_count=8,
            companies_fetched=7,
            people_looked_up=4,
            people_matched=3,
            emails_from_people=2,
            emails_from_websites=3,
            spent_usd=Decimal("0.05205"),
        )

    monkeypatch.setattr(nodes.db, "connect", lambda: nullcontext("conn"))
    monkeypatch.setattr(nodes, "find_contacts_for_run", fake_for_run)

    out = nodes.find_contacts(STATE, CONFIG)

    assert seen["run_id"] == 45
    assert out == {
        "messages": [
            "AI: 找联系方式：8 家要推卡，公司详情 7 家，查人 4 个（对上 3 个），邮箱 5 个，花了 $0.05205"
        ]
    }


def test_the_cap_and_actor_errors_are_mentioned(monkeypatch):
    """超了月度上限、调 actor 出错：那一句后面各多说一句。"""
    report = contacts.ContactsReport(
        lead_count=3, blocked_by_cap="本账期已经花了 $9.99", actor_errors={"person_email": "TimeoutError: x"}
    )
    monkeypatch.setattr(nodes.db, "connect", lambda: nullcontext("conn"))
    monkeypatch.setattr(nodes, "find_contacts_for_run", lambda conn, *, run_id, now: report)

    text = nodes.find_contacts(STATE, CONFIG)["messages"][0]

    assert "超了月度上限，付费的那一半没找：本账期已经花了 $9.99" in text
    assert "有一步出错跳过了" in text


def test_an_error_does_not_escape_so_the_cards_still_go_out(monkeypatch):
    """连库出错：不往外抛，回一句出错了，图照样走到 deliver。

    这条红了的症状：数据库连不上的那一天，找联系方式那一步崩了，一张卡片都推不出去
    """

    def broken():
        raise ConnectionError("Neon 掐了连接")

    monkeypatch.setattr(nodes.db, "connect", broken)

    out = nodes.find_contacts(STATE, CONFIG)

    assert out == {"messages": ["AI: 找联系方式出错了（ConnectionError），这一次卡片上联系方式是空的"]}
