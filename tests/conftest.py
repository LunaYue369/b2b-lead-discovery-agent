"""所有测试共用的兜底：测试不许碰生产。

这个文件是一次真实事故的产物。interactive 那张图的测试跑的是真的 build_graph，
run_harvest 接上真 pipeline 的那一刻，整套测试就真的连了 Neon、真的调了 Apify：
两跑各 40 条计费、库里多了一条 lead 和 19 条 signal —— 而测试全绿，唯一的症状是
跑完从 2 秒变成 35 秒，没有人会盯着那个数。

所以这里把几个口子都堵死，它们各自是一条花钱或者对外发东西的路的必经之地：

    db.connect        采集要先有 store，store 要先有连接。堵住它，
                      就不可能悄悄花掉 Apify 的钱
    db.connection_url 跟 db.connect 同一道闸。它挡的是不经过 db.connect、
                      自己拿连接串去连的那条路：对话那边的 checkpointer
                      （runner.open_saver、runner.setup_checkpointer）是把连接串直接交给
                      PostgresSaver 的，db.connect 挡不住它。测试跑进去的话
                      会在生产库的 checkpoint 表里留下假对话
    build_llm_client  全仓只有这一个地方造 OpenAI client（analyzer 和
                      understand_intent 都走它）。堵住它，就不可能悄悄
                      花掉模型的钱
    send.build_client 全仓推卡片的两条路（定时那一跑的 push_digest、对话那边的
                      deliver 节点）都从这里造 Slack 客户端。堵住它，测试就不可能
                      往真的频道里发卡片。卡片发出去就收不回来，Riley 会以为那是真线索
    apify.run_actor   全仓调 Apify 只走这一处：搜帖子、公司详情、个人主页+找邮箱。
                      找联系方式那两个不经过 db.connect，第一道挡不住：花的钱是
                      $0.004 一家、$0.01 一个人
    website 的 httpx.get  抓官网首页。不花钱，但测试不该往外网发请求：结果随对方网站变，
                      今天绿明天红，而且对方看得到我们来过

第二道是 2c 之前补的。在那之前只有第一道，而 plan_queries 接真 planner 的
那一刻，忘了加桩的症状跟当初那次事故一模一样：测试全绿、钱照花、只有跑得慢了
一点这一个信号。db.connect 挡不住它 —— 调模型不需要连库。

真要连生产的测试显式要 allow_real_db / allow_real_llm / allow_real_slack / allow_real_web（现在一个都没有，
将来也该很少）。不连库的测试写法见 tests/test_discovery_db.py 那个假游标；
不调模型的写法见 tests/test_intent.py 那个照剧本回答的假 client，或者把会花钱
的那个节点整个换成桩（见 tests/test_interactive_graph.py）。
"""

from __future__ import annotations

import pytest

from adapters import apify, website
from delivery import send
from persistence import db
from triggers import harvest
from triggers.interactive_discovery.drafting import nodes as drafting_nodes
from triggers.interactive_discovery.query import nodes

# build_llm_client 要在这两个地方各挡一次，不是重复。
#
# nodes.py（query 和 drafting 两张图各一个）都是 `from triggers.harvest import build_llm_client` 这么引的，
# import 那一刻就把函数对象取走了 —— 之后再改 triggers.harvest 上那个名字，
# nodes 手上那份不会跟着变。9-14 换 run_harvest 的桩时已经栽过一次同款
# （要换 graph 模块上那个名字，不是 nodes 上的）。
#
# harvest.py 自己内部那次调用走的是模块全局，所以挡它自己这一份就够。
_LLM_CLIENT_SITES = (
    (harvest, "build_llm_client"),
    (nodes, "build_llm_client"),
    (drafting_nodes, "build_llm_client"),
)


@pytest.fixture
def allow_real_db():
    """显式声明这个测试真的要连生产库。要它就等于要为它花的钱负责。"""
    return True


@pytest.fixture
def allow_real_llm():
    """显式声明这个测试真的要调模型。同上，要它就等于要为它花的钱负责。"""
    return True


@pytest.fixture
def allow_real_slack():
    """显式声明这个测试真的要往 Slack 发消息。要它就等于要为频道里多出来的那几条负责。"""
    return True


@pytest.fixture
def allow_real_web():
    """显式声明这个测试真的要调找联系方式的 actor（花钱）、真的要去抓别人的官网。"""
    return True


@pytest.fixture(autouse=True)
def _no_production_access(monkeypatch, request):
    """把花钱和对外发东西的入口都换成当场炸的版本，除非这个测试显式要了对应的 fixture。

    输入：无（autouse，每个测试自动生效）
    输出：无

    db.connect 换的是 persistence.db 这个模块上的属性，所以所有
    `db.connect(...)` 的写法一起被挡住 —— 全仓都是这么调的。
    db.connection_url 同理，全仓都是 `db.connection_url(...)` 这么调的。

    build_llm_client 要换两处，理由见上面 _LLM_CLIENT_SITES 那段：
    from 引进去的名字是 import 那一刻就绑死的，改源模块改不到它。

    send.build_client 换一处就够：全仓都是 `send.build_client()` 这么调的，
    用的时候才去 send 这个模块上查名字，没有谁把它 from 引进去
    """
    if "allow_real_db" not in request.fixturenames:

        def no_db(*args, **kwargs):
            raise RuntimeError(
                "测试想连生产库，被 tests/conftest.py 挡下了。"
                "要么用假游标，要么把会连库的那一步换成桩；"
                "真需要连库的测试请显式用 allow_real_db 这个 fixture。"
            )

        monkeypatch.setattr(db, "connect", no_db)
        monkeypatch.setattr(db, "connection_url", no_db)

    if "allow_real_llm" not in request.fixturenames:

        def no_llm(*args, **kwargs):
            raise RuntimeError(
                "测试想造一个真的 OpenAI client，被 tests/conftest.py 挡下了。"
                "要么传一个照剧本回答的假 client（见 tests/test_intent.py），"
                "要么把会调模型的那个节点整个换成桩；"
                "真需要调模型的测试请显式用 allow_real_llm 这个 fixture。"
            )

        for module, name in _LLM_CLIENT_SITES:
            monkeypatch.setattr(module, name, no_llm)

    if "allow_real_slack" not in request.fixturenames:

        def no_slack(*args, **kwargs):
            raise RuntimeError(
                "测试想造一个真的 Slack 客户端，被 tests/conftest.py 挡下了。"
                "要么传一个记账的假 client（见 tests/test_send.py），"
                "要么把会发消息的那个节点整个换成桩（见 tests/test_interactive_graph.py 的 _stub_deliver）；"
                "真需要发 Slack 的测试请显式用 allow_real_slack 这个 fixture。"
            )

        monkeypatch.setattr(send, "build_client", no_slack)

    if "allow_real_web" not in request.fixturenames:

        def no_web(*args, **kwargs):
            raise RuntimeError(
                "测试想调找联系方式的 actor 或者抓别人的官网，被 tests/conftest.py 挡下了。"
                "actor 那边拿 data/probe_contacts_2026-09-27/ 里存下来的真返回去测翻译；"
                "抓官网那边把 website.httpx.get 换成假的（见 tests/test_contact_adapters.py）；"
                "真需要连网的测试请显式用 allow_real_web 这个 fixture。"
            )

        # apify.run_actor：三个 adapter 都是 `apify.run_actor(...)` 这么调的，换一处就够
        # website.httpx.get：换的是 httpx 模块上的 get，website.py 用的时候才去查这个名字
        monkeypatch.setattr(apify, "run_actor", no_web)
        monkeypatch.setattr(website.httpx, "get", no_web)
