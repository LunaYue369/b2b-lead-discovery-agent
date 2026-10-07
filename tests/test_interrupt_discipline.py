"""焊住一条纪律：interrupt 只能写在节点函数的第一行，而且一个节点最多一个。

这是一条测代码形状的测试，不测行为 —— 因为这个毛病的行为是对的。

在 interrupt 前面加一句话（算报价、读这个月已经花了多少、把 retrieve_knowledge 的结果
拼进去），程序照跑，结果一模一样，日志里一个异常都没有。唯一的区别是：用户每回一次话，
那句话就重新执行一遍。恢复的语义是「被中断的那个节点从第一行整个重跑」，所以 interrupt
前面站着什么，就重跑什么。如果那句是一次 LLM 调用，那就是每回一次话多付一次钱。

这件事没有任何运行时信号，普通测试也测不出来（输出是对的），所以只能看代码长什么样。
现在整张图只有 await_go 一个 interrupt、而且已经写在第一行，焊它不是为了今天，是为了
它前面迟早要长东西那天：报价要算、月度硬上限要读、方案要拼。

一个节点最多一个 interrupt 是同一条纪律的另一半：一个节点里问两句的话，第二句挂起时
第一句会连同它们中间的所有代码重跑一遍，而且两个挂起点的 id 分不开，恢复时裸的
Command(resume=值) 会直接报错。两件事就分成两个节点，各自第一行一个。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "triggers" / "interactive_discovery"


def _own_interrupt_calls(func: ast.AST) -> list[ast.Call]:
    """这个函数自己那一层里的 interrupt(...) 调用，不算嵌套函数里的。

    输入：一个 FunctionDef 的 ast 节点
    输出：属于它自己的那些 Call 节点

    认的是裸名字 interrupt(...)，因为 nodes.py 是 from langgraph.types import interrupt
    这么引进来的。哪天有人改成 langgraph.types.interrupt(...)，这里会一个都找不到、
    测试跟着全过 —— 所以下面有一条专门的兜底，断言至少焊到了一个。
    """
    found: list[ast.Call] = []

    def walk(node: ast.AST, *, top: bool) -> None:
        # 碰到嵌套的函数定义就不往里走了：那是另一个函数自己的事
        if not top and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "interrupt":
            found.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child, top=False)

    walk(func, top=True)
    return found


def _first_real_statement(func: ast.AST) -> ast.stmt | None:
    """跳过 docstring 之后的第一条语句。

    输入：一个 FunctionDef 的 ast 节点
    输出：那条语句；函数体除了 docstring 什么都没有时返回 None

    docstring 在 ast 里就是函数体第一条语句，长相是一个字符串常量的表达式。
    """
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1] if len(body) > 1 else None
    return body[0] if body else None


def _functions_with_interrupt() -> list[tuple[str, str, ast.AST, list[ast.Call]]]:
    """扫整个包，把带 interrupt 的函数挑出来。

    输入：无（路径写死在这个文件顶上）
    输出：(文件名, 函数名, 函数节点, 这个函数里的 interrupt 调用) 的列表

    扫的是整个包不是单个 nodes.py：以后节点可能拆到别的文件里去，只盯一个文件的话
    新文件一加进来这条纪律就没人管了。
    """
    out = []
    # rglob 连子文件夹一起扫：每张图一个子文件夹（query/，以后的 drafting/），纪律对每张图都成立
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = _own_interrupt_calls(node)
                if calls:
                    out.append((path.name, node.name, node, calls))
    return out


def test_the_check_actually_finds_something():
    """兜底：这套扫描一个 interrupt 都找不到的话，下面两条测试全是空过。

    会一个都找不到的两种情况，都不报错：整张图的 interrupt 被谁删光了（那时候图永远
    不会停下来等人，方案直接就跑了），或者 import 的写法改了、裸名字认不出来了。
    """
    assert _functions_with_interrupt(), (
        f"{PACKAGE} 里一个 interrupt 都没扫到。要么它真的没了，要么 import 的写法变了 —— "
        "两种都得有人看一眼，不能让这个文件里的测试静默空过"
    )


@pytest.mark.parametrize("case", _functions_with_interrupt(), ids=lambda c: f"{c[0]}::{c[1]}")
def test_interrupt_is_the_first_statement(case):
    """interrupt 必须是这个节点跳过 docstring 之后的第一条语句。"""
    filename, funcname, func, calls = case
    first = _first_real_statement(func)
    assert first is not None, f"{filename} 的 {funcname} 函数体是空的"
    in_first = [c for c in ast.walk(first) if c in calls]
    assert in_first, (
        f"{filename} 的 {funcname}：interrupt 不在第一行（第一条语句在第 {first.lineno} 行，"
        f"interrupt 在第 {calls[0].lineno} 行）。"
        "它前面那几句会在用户每回一次话时重新执行一遍，不报错、结果也对，只是多花钱"
    )


@pytest.mark.parametrize("case", _functions_with_interrupt(), ids=lambda c: f"{c[0]}::{c[1]}")
def test_a_node_has_at_most_one_interrupt(case):
    """一个节点最多一个 interrupt，要问两句就分成两个节点。"""
    filename, funcname, _func, calls = case
    assert len(calls) == 1, (
        f"{filename} 的 {funcname} 里有 {len(calls)} 个 interrupt"
        f"（第 {[c.lineno for c in calls]} 行）。"
        "第二个挂起时，第一个连同它们中间的代码会整个重跑一遍；而且两个挂起点的 id "
        "分不开，恢复时裸的 Command(resume=值) 会直接报错"
    )
