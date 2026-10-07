"""焊住桩挡住的那段盲区：run_harvest 真实现里那几个调用的参数对不对得上。

这是一条测代码形状的测试，不测行为 —— 因为这段代码在测试里根本不会被执行。

`run_harvest` 是整张图上唯一真花钱的节点（连 Neon、调 Apify），所以
`tests/conftest.py` 那道闸之外，`test_interactive_graph.py` 和
`scripts/try_interactive.py` 还会把 `graph.run_harvest` 整个换成桩。那是 9-14
「测试全绿照样花了真钱」之后立的规矩，没得商量。

代价是它的真实现一行覆盖都没有：它调 `upsert_query`、构造 `QuerySpec`、
调 `harvest_once`，这三处的参数写错或漏写，几百条测试一条都不会红。

2c 之后多了三处同样的盲区：`understand_intent` / `plan_queries` / `classify_reply`。
这三个节点本身是照跑的，被换成桩的是它们里面真花钱的那一句（`read_intent` /
`draft_queries` / `read_decision`）—— 桩的签名跟真实现一样的时候传错了确实会红，
但那三份签名会漂，漂了之后就没人管了。所以这三处也一起扫。

（`understand_intent` 一度是跟 `run_harvest` 一样整个被换成桩的，那时它调
 `read_intent` 的参数一行覆盖都没有。后来改成只换里面那一句，理由见
 `test_interactive_graph.py` 的 `intents` fixture。）

这不是假设。`34fd562` 那个 commit 单独 checkout 出来跑是 425 passed 全绿，
而那一刻 `run_harvest` 运行时必炸 —— `upsert_query` 刚加了必填的 `filters`，
调用处还没跟上。破绽是 ast 扫出来的，不是测试。

所以这里用 ast 把调用处的关键字，跟被调方 `inspect.signature` 算出来的必填参数
对一遍。它拦的是「加了必填参数却漏改调用方」这一类，也就是
feedback_signature-change-static-audit 那条在这条路上唯一的安全网。

它拦不了的：参数名对、值是错的（把 topics 传成 filters 那种）。那种要靠真跑。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from models import QuerySpec
from persistence.query_db import upsert_query
from triggers.contacts import find_contacts_for_run
from triggers.harvest import harvest_once
from triggers.interactive_discovery.query.intent import read_intent
from triggers.interactive_discovery.query.planner import draft_queries
from triggers.interactive_discovery.query.reply import read_decision

NODES_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "triggers"
    / "interactive_discovery"
    / "query"
    / "nodes.py"
)

# (调用处长什么样, 被调的那个真东西)。
# 左边是源码里那个名字：`query_db.upsert_query(...)` 写成 'upsert_query'，
# `QuerySpec(...)` 写成 'QuerySpec' —— 取的都是最后一段。
TARGETS = (
    ("upsert_query", upsert_query),
    ("QuerySpec", QuerySpec),
    ("harvest_once", harvest_once),
    ("draft_queries", draft_queries),
    ("read_decision", read_decision),
    ("read_intent", read_intent),
    ("find_contacts_for_run", find_contacts_for_run),
)

# 在哪个函数里找这些调用。五个：run_harvest、understand_intent、find_contacts 整个被换成桩，
# 另外两个是自己照跑、里面那句花钱的被换成桩。
STUBBED_FUNCTIONS = ("run_harvest", "understand_intent", "plan_queries", "classify_reply", "find_contacts")


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    """在模块顶层找一个函数定义。

    输入：整份 ast；函数名
    输出：那个 FunctionDef，找不到给 None
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _call_name(node: ast.Call) -> str:
    """这个调用在源码里叫什么，只取最后一段。

    输入：一个 Call 节点
    输出：'upsert_query' / 'QuerySpec' / ''（认不出来的形状）

    `query_db.upsert_query(...)` 和 `upsert_query(...)` 都给 'upsert_query'：
    这里要认的是「调的是哪个东西」，不是「它从哪个名字空间来」。
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _required_parameters(callee: object) -> tuple[list[str], int]:
    """被调方有哪些必填参数，以及其中几个可以靠位置传。

    输入：一个真函数或真类
    输出：(必填的按名字能传的那些, 必须靠位置占掉的那几个的个数)

    一个实际值（upsert_query，签名是 (cur, *, text, source, ..., filters,
    generated_by_memory_key=None)）：

        输出 (['text', 'source', 'source_type', 'pool', 'cadence_days',
               'topics', 'filters'], 1)

        那个 1 是 cur —— 它只能靠位置传，所以不在名字那张清单里。

    有默认值的一律不算必填：漏传它们是「用了默认」，那是个决定不是 bug。
    """
    signature = inspect.signature(callee)

    named: list[str] = []
    positional_only = 0
    for parameter in signature.parameters.values():
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.kind is parameter.POSITIONAL_ONLY:
            positional_only += 1
        elif parameter.kind is parameter.KEYWORD_ONLY:
            named.append(parameter.name)
        else:
            # POSITIONAL_OR_KEYWORD：两种传法都行，所以两张清单都记一份，
            # 下面按这次调用实际用了哪种来判
            named.append(parameter.name)

    return named, positional_only


def _cases() -> list[tuple[str, str, ast.Call, object]]:
    """把要检查的 (函数名, 被调名, 调用节点, 被调对象) 全找出来。

    输入：无（读的是 nodes.py 的源码）
    输出：一条调用一个元组
    """
    tree = ast.parse(NODES_PATH.read_text(encoding="utf-8"))
    found: list[tuple[str, str, ast.Call, object]] = []

    for funcname in STUBBED_FUNCTIONS:
        func = _find_function(tree, funcname)
        if func is None:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            for target, callee in TARGETS:
                if name == target:
                    found.append((funcname, target, node, callee))

    return found


def test_every_target_call_was_found():
    """兜底：一条都没扫到的话，下面那条参数化测试会静默空过。

    会扫不到的两种情况都不报错：run_harvest 被改名或者挪走了，
    或者它不再调这几个东西了 —— 两种都得有人看一眼。
    """
    names = {target for _f, target, _n, _c in _cases()}
    missing = {target for target, _callee in TARGETS} - names
    assert not missing, (
        f"这几个调用在 {NODES_PATH.name} 的 {STUBBED_FUNCTIONS} 里一个都没扫到：{sorted(missing)}。"
        "要么代码挪了，要么这张表过期了 —— 不能让这个文件里的测试静默空过"
    )


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c[0]}::{c[1]}@{c[2].lineno}")
def test_required_arguments_are_all_supplied(case):
    """调用处必须把被调方的必填参数全传上。

    这一类错在运行时是 TypeError，而这段代码在测试里不会被执行 ——
    所以只能看代码长什么样。
    """
    funcname, target, node, callee = case

    # `f(**kwargs)` 这种写法下，传了什么要到运行时才知道，静态判不了。
    # 跳过而不是判失败：判失败会逼着以后的人为了过测试改写法，而那个写法本身没错。
    if any(keyword.arg is None for keyword in node.keywords):
        pytest.skip(f"{funcname} 调 {target} 时用了 **kwargs，静态看不出传了什么")

    named, positional_only = _required_parameters(callee)
    supplied = {keyword.arg for keyword in node.keywords}

    # 按位置传进去的那几个，按顺序占掉 named 前面的名字。
    # upsert_query(cur, text=...) 里 cur 是位置传的，不该被判成「漏了」。
    positional_count = len(node.args)
    consumed = positional_only + positional_count
    still_needed = named[max(0, consumed - positional_only) :] if consumed else named

    missing = [name for name in still_needed if name not in supplied]

    assert not missing, (
        f"{NODES_PATH.name} 第 {node.lineno} 行，{funcname} 调 {target} 少传了 {missing}。"
        f"这几个在被调方没有默认值 —— 运行时是 TypeError，"
        f"而 {funcname} 在测试里被整个换成了桩，所以别的测试一条都不会红"
    )
