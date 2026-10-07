"""conftest.py 那两道闸自己的测试。

它们测的不是哪个模块，是「测试不许碰生产」这条纪律本身 —— 没人守着的话，
那两道闸哪天被删掉、或者被某个文件里的 fixture 悄悄顶掉，不会有任何东西响。

所以它们单独住一个文件，不跟别的测试混：会跑图的那几个文件里到处是把
build_llm_client 换成桩的 autouse fixture，而那种 fixture 恰好会把要测的东西盖掉 ——
那两条测试放在那种文件里会永远绿，无论闸还在不在。

出处：2026-09-14，run_harvest 一接上真 pipeline，interactive 那几条测试就真的连了
Neon、真的调了 Apify，两跑各 40 条计费，而测试全绿，唯一的症状是跑完从 2 秒变成
35 秒。第二道闸（build_llm_client）是 2c 之前补的，堵的是另一条花钱路径 ——
调模型不需要连库，第一道闸挡不住它。
"""

from __future__ import annotations

import pytest


def test_tests_cannot_reach_the_production_database():
    """第一道闸：db.connect 换成当场炸。

    采集要先有 store，store 要先有连接，所以堵住它就不可能悄悄花掉 Apify 的钱。
    """
    from persistence import db

    with pytest.raises(RuntimeError, match="被 tests/conftest.py 挡下了"), db.connect():
        pass


def test_tests_cannot_build_a_real_llm_client():
    """第二道闸：build_llm_client 换成当场炸。三个入口各验一次。

    全仓只有这一个地方造 OpenAI client（analyzer、understand_intent、plan_queries、
    classify_reply、写稿那张图的 plan_drafts 都走它），所以它就是 LLM 这条路上的 db.connect。

    为什么要验三个入口：两张图的 nodes 那份都是 from 引进来的，import 那一刻就把函数对象取走了，
    只挡 harvest 那一份是挡不住它们的。同款的坑在换 run_harvest 的桩时栽过一次。
    """
    from triggers import harvest
    from triggers.interactive_discovery.drafting import nodes as drafting_nodes
    from triggers.interactive_discovery.query import nodes

    for module in (harvest, nodes, drafting_nodes):
        with pytest.raises(RuntimeError, match="被 tests/conftest.py 挡下了"):
            module.build_llm_client()


def test_tests_cannot_build_a_real_slack_client():
    """第三道闸：send.build_client 换成当场炸。

    推卡片的两条路都从它造客户端。漏过去的话，一条走到 deliver 的测试会往真的线索频道
    发卡片，而卡片发出去就收不回来 —— Riley 在频道里看到的是一批假线索。
    """
    from delivery import send

    with pytest.raises(RuntimeError, match="被 tests/conftest.py 挡下了"):
        send.build_client()


def test_tests_cannot_open_the_production_checkpointer():
    """db.connection_url 那道闸：对话那边的 checkpointer 拿不到连接串。

    open_saver 和 setup_checkpointer 是把连接串直接交给 PostgresSaver 的，不经过
    db.connect，上面第一道闸挡不住它们。漏过去的话，测试会在生产库的 checkpoint
    表里留下一条条假对话，以后查「有哪些对话挂着」时混在真的里面。
    """
    from triggers.interactive_discovery import runner

    with pytest.raises(RuntimeError, match="被 tests/conftest.py 挡下了"), runner.open_saver():
        pass
    with pytest.raises(RuntimeError, match="被 tests/conftest.py 挡下了"):
        runner.setup_checkpointer()
