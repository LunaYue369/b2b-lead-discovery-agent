#!/usr/bin/env python
"""分层隔离检查 —— 以后进 CI，现在手动跑：`python scripts/check_isolation.py`

四条规则，全是踩过坑之后焊上的：

1. 核心层不许 import slack。
   blog worker 上出过事：chat_generator 把 Slack 漏进了核心层，
   结果测一条业务链路要先有 Slack token。这次一开始就焊住。

2. discovery/ 不许有供应商知识。
   采集是最脆的一层（Proxycurl 2025-07 被起诉后直接关停，客户一夜断供）。
   供应商字段名只能出现在 adapters/ 里，换实现时 discovery/ 一行不动。

3. models/ 只依赖标准库。
   守住这条，Slack / 数据库 / LLM 谁都漏不进词汇表。
   用 ast 静态检查，不靠「当前环境没装」这种偶然。

4. discovery/ 和 query_intelligence/ 不许连库。
   这两层是纯函数：同样的输入永远给同样的输出，才能拿历史数据反复回归。
   一旦哪个节点自己查库，回归跑就依赖当时的库状态，那个性质就没了。
   query_intelligence 是后补进这条的 —— 原设计把 query_repo.py 放在那儿，
   跟这条规则冲突，改的是设计不是规则（跟当初 lead_repo.py 那次一样）。
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (目录, 禁止的模块名片段, 说明)
IMPORT_BANS = [
    (["discovery", "query_intelligence"], ["slack"], "核心层不许 import slack"),
    (["discovery"], ["harvestapi", "apify"], "discovery/ 不许有供应商知识（那在 adapters/）"),
    # 闸门是纯函数：同样的输入永远给同样的输出，才能拿历史数据反复回归。
    # 一旦哪个节点自己连库，回归跑就依赖当时的库状态，那个性质就没了。
    # 跨天 seen 这类库里的东西一律从参数传进来，查库在 persistence/。
    (
        ["discovery", "query_intelligence"],
        ["psycopg"],
        "这一层不许连库（闸门和组队都是纯函数，查库在 persistence/）",
    ),
]

STDLIB_ONLY_DIRS = ["models"]


def imported_modules(path: pathlib.Path) -> list[tuple[int, str]]:
    """返回 [(行号, 顶层模块名)]。只看 import 语句，不看注释和字符串 ——
    否决 grep 的原因：docstring 里写「不许 import slack」不该把自己判失败。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((node.lineno, a.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.lineno, node.module.split(".")[0]))
    return out


def main() -> int:
    # Windows 控制台默认 GBK，中文报错信息会 UnicodeEncodeError 或乱码。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failures: list[str] = []

    for dirs, banned, why in IMPORT_BANS:
        for d in dirs:
            for py in sorted((ROOT / d).rglob("*.py")):
                for lineno, mod in imported_modules(py):
                    if any(b in mod.lower() for b in banned):
                        rel = py.relative_to(ROOT).as_posix()
                        failures.append(f"{rel}:{lineno} import {mod} —— {why}")

    local = {p.stem for p in ROOT.glob("*.py")} | {
        d.name for d in ROOT.iterdir() if d.is_dir() and (d / "__init__.py").exists()
    }
    for d in STDLIB_ONLY_DIRS:
        for py in sorted((ROOT / d).rglob("*.py")):
            rel = py.relative_to(ROOT).as_posix()
            for lineno, mod in imported_modules(py):
                if mod in sys.stdlib_module_names or mod in local or mod == "__future__":
                    continue
                failures.append(f"{rel}:{lineno} import {mod} —— {d}/ 只能依赖标准库")

    # 输出刻意只用 ASCII 记号：Windows 控制台默认 GBK，打 会直接 UnicodeEncodeError。
    if failures:
        print("[FAIL] 隔离检查失败：")
        for f in failures:
            print("   ", f)
        return 1
    print("[OK] 隔离检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
