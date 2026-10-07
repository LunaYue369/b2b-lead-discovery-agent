"""跑 migration：把 persistence/migrations/ 下还没应用的 SQL 按编号执行。

    python -m scripts.migrate          看一眼要跑哪些，不执行
    python -m scripts.migrate --apply  真跑

默认只看不跑，是因为这个脚本改的是生产库的形状，而「以为在测试库上跑」
是这类工具最常见的事故。多打一个 --apply 的成本可以忽略。

只往前，不做 down。回滚一步 migration 在实践中几乎总是要人工判断
（数据已经写进去了怎么办），预先写一段 down SQL 只会给人一种
「随时能退」的错觉。要退就再写一条往前的 migration。

走 direct 那条连接串：建表要拿锁、要会话级状态，而 pooled 那头
PgBouncer 在事务之间会把连接换给别人。
"""

from __future__ import annotations

import pathlib
import sys

from persistence.db import connect

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "persistence" / "migrations"

# 记账表自己不能靠 migration 建，鸡生蛋。它简单到不会变。
BOOTSTRAP = """
create table if not exists schema_migrations (
    version    text primary key,
    applied_at timestamptz not null default now()
)
"""


def pending(applied: set[str]) -> list[pathlib.Path]:
    """列出还没跑过的 migration，按文件名排序。

    输入：已经应用过的版本号
    输出：还要跑的文件，按编号从小到大

    版本号取文件名去掉后缀（0001_init.sql -> 0001_init）。用文件名而不是
    内容哈希：改一个已经应用过的 migration 本来就是错的做法，用哈希会让
    它表现为「又要跑一遍」，而那时候表已经建好了，跑起来只会报一堆
    already exists，掩盖真正的问题。
    """
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.stem not in applied)


def main() -> int:
    apply = "--apply" in sys.argv

    with connect(direct=True) as conn:
        with conn.cursor() as cur:
            # 只看不跑的时候一个字都不写，包括记账表 —— 说了不跑就真的
            # 不动库，否则「dry run 之后库里多了张表」下次就没人信这个开关了。
            if apply:
                cur.execute(BOOTSTRAP)
            cur.execute("select to_regclass('public.schema_migrations')")
            (exists,) = cur.fetchone()
            applied: set[str] = set()
            if exists is not None:
                cur.execute("select version from schema_migrations")
                applied = {row[0] for row in cur.fetchall()}
        conn.commit()

        todo = pending(applied)
        if not todo:
            print(f"没有待跑的 migration（已应用 {len(applied)} 条）")
            return 0

        print(f"已应用 {len(applied)} 条，待跑 {len(todo)} 条：")
        for path in todo:
            print(f"    {path.name}")

        if not apply:
            print("\n只看不跑。真要执行加 --apply")
            return 0

        # 一个文件一个事务。中途炸了只回滚这一个文件，前面跑成功的
        # 留在库里并且记了账 —— 修好之后接着跑，不用从头来。
        for path in todo:
            print(f"\n跑 {path.name} ...")
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute(
                    "insert into schema_migrations (version) values (%s)", (path.stem,)
                )
            conn.commit()
            print(f"    OK，已记账 version={path.stem}")

    print("\n全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
