"""把 config/queries.yaml 的种子 query 灌进 queries 表，并报出文件和库的差异。

    python -m scripts.seed_queries          看一眼会发生什么，不写
    python -m scripts.seed_queries --apply  真写

默认只看不跑，跟 scripts/migrate.py 同一个理由：这个脚本改的是生产库，
而「以为在测试库上跑」是这类工具最常见的事故。

灌库这件事本身很简单（upsert_query 撞了就什么都不做，天然幂等），
这个脚本真正的活是**报差异**。文件和库会在三个地方对不上，每一种
不报出来就会变成安静的错：

    字段不同   文件说 cadence_days=1，库里是 3。库里那个多半是 memory
               processor 学出来调慢的，不该被重跑 seed 打回去 —— 所以
               不改，但要说出来，否则「我明明改了文件」查不出所以然
    孤儿       库里有一条 source=manual 的 query，文件里却没有。最可能的
               成因是有人改了 yaml 里某条的文字：归一化文本是身份，改了
               文字就是新的一条，新行建出来了，旧行还在跑、还在花钱
    重复       文件里两条归一化之后一样。第二条会被当成「已经有了」跳过，
               而那条 query 从此不存在，库里也看不出少了什么

第三种在读文件时就炸，不进库。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from dataclasses import dataclass

import yaml

from models import (
    QueryPool,
    QuerySource,
    QuerySpec,
    SourceType,
    normalize_query_text,
    validate_filters,
)
from persistence import db, query_db

SEEDS_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "queries.yaml"

# 一条种子允许出现的键。note 是写给人看的，不进库。
# 用白名单而不是随便多余的键就忽略：把 cadence_days 拼成 cadance_days 的话，
# 忽略它等于静默用了默认值，而那条 query 会以错误的频率跑下去。
ALLOWED_KEYS = {"text", "source_type", "cadence_days", "pool", "note", "filters"}


def identity_of(text_normalized: str, filters: dict[str, str]) -> tuple[str, str]:
    """一条 query 的身份，跟库里那条唯一索引同一个口径。

    输入：归一化后的文本；采集旋钮
    输出：一个可以当字典键的二元组

    0009 之后库里的唯一键是 (text_normalized, filters)，所以文件和库比对时
    也必须按这两样一起比。只按文本比的话，同一句词配了不同旋钮的两条种子
    会被判成重复，而它们本来就该是两行。

    filters 那一半用 sort_keys 序列化成字符串：dict 不能当字典键，而键的
    顺序不该影响身份 —— 这跟 jsonb 自己会排序是同一个口径。
    """
    return text_normalized, json.dumps(filters, sort_keys=True)
# 文件里没写 pool 时用哪一档。种子是「目前最好的猜测」，一条 query_executions
# 数据都没有，该待在有晋升和淘汰规则的那个池子里，而不是永不评价的 pinned。
DEFAULT_POOL = QueryPool.CANDIDATE


@dataclass(frozen=True)
class Seed:
    """文件里的一条种子，已经算好归一化文本。"""

    text: str
    text_normalized: str
    source_type: SourceType
    pool: QueryPool
    cadence_days: int
    filters: dict[str, str]


@dataclass(frozen=True)
class DbQuery:
    """库里一条 source=manual 的 query，只取比对要用的列。"""

    query_id: int
    text: str
    text_normalized: str
    pool: QueryPool
    cadence_days: int
    is_active: bool
    filters: dict[str, str]


@dataclass(frozen=True)
class SeedPlan:
    """比对结果。四类互斥，加起来等于「文件条数 + 孤儿数」。"""

    to_create: list[Seed]
    unchanged: list[tuple[Seed, DbQuery]]
    differing: list[tuple[Seed, DbQuery, list[str]]]
    orphans: list[DbQuery]


def load_seeds(path: pathlib.Path = SEEDS_PATH) -> list[Seed]:
    """读 queries.yaml，逐条校验，算出归一化文本。

    输入：yaml 路径
    输出：Seed 列表，顺序照文件里的顺序

    一个例子（文件里的一条）：

        - text: 'Kickstarter AND (microphone OR audio OR "voice recorder")'
          source_type: linkedin_post
          cadence_days: 1
          note: |
            Shape A. A funded campaign means ...

        输出 Seed(text='Kickstarter AND (microphone OR audio OR "voice recorder")',
                  text_normalized='kickstarter and (microphone or audio or "voice recorder")',
                  source_type=SourceType.LINKEDIN_POST,
                  pool=QueryPool.CANDIDATE,     没写 pool，用默认
                  cadence_days=1)

    四种写错的写法一律当场炸，不带着往下走：

        多余的键     cadance_days 拼错 -> 忽略它等于静默用了默认频率
        缺必填键     text / source_type / cadence_days 少一个
        取值不合法   pool: pined
        归一化撞车   两条不同写法归一化后一样，第二条进库时会被当成
                     「已经有了」跳过，而那条 query 从此不存在

    最后一种是这个函数存在的主要理由 —— 它在文件层面就能查出来，
    而到了库里只会表现为「新增 1 条」，没人看得出少了一条。
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = raw["queries"]

    seeds: list[Seed] = []
    seen: dict[tuple[str, str], str] = {}  # 身份 -> 第一次出现的原文
    for index, entry in enumerate(entries):
        unknown = set(entry) - ALLOWED_KEYS
        if unknown:
            raise ValueError(f"第 {index + 1} 条有不认识的键 {sorted(unknown)}，是不是拼错了")

        # 用下标不用 get：键缺了当场 KeyError，不退化成「用了个默认值」。
        # 这条口径跟 prefilter 的 load_rules 一致。
        text = entry["text"]
        source_type = SourceType(entry["source_type"])
        cadence_days = entry["cadence_days"]
        # pool 是唯一有默认值的键，因为「没特别说明就是试跑期」本身是个决定。
        pool = QueryPool(entry.get("pool", DEFAULT_POOL))
        # filters 有默认值，理由跟 pool 一样：种子绝大多数不筛人，
        # 「没写就是不筛」本身是个决定，不是遗漏。
        # 先查再转 dict：yaml 里写成 `filters: [a, b]` 或者 `filters:`（空值）时，
        # dict() 自己会抛一个说不清是哪一条写错了的错。
        raw_filters = entry.get("filters", {})
        validate_filters(raw_filters)
        filters = dict(raw_filters)

        text_normalized = normalize_query_text(text)
        if not text_normalized:
            raise ValueError(f"第 {index + 1} 条的 text 是空的")
        identity = identity_of(text_normalized, filters)
        if identity in seen:
            raise ValueError(
                f"第 {index + 1} 条跟前面那条身份一样（文本和旋钮都一样），进库时会被当成同一条：\n"
                f"    {seen[identity]!r}\n    {text!r}"
            )
        seen[identity] = text

        seeds.append(
            Seed(
                text=text,
                text_normalized=text_normalized,
                source_type=source_type,
                pool=pool,
                cadence_days=cadence_days,
                filters=filters,
            )
        )
    return seeds


def plan_seed(seeds: list[Seed], db_rows: list[DbQuery]) -> SeedPlan:
    """比对文件和库，分成四类。纯函数，不连库。

    输入：文件里的种子；库里所有 source=manual 的行
    输出：SeedPlan

    按 text_normalized 建一张库里的索引，然后逐条种子查这张表。
    查得到再比 pool / cadence_days / 原文写法，比出不同就进 differing。
    最后剩在索引里没被认领的就是孤儿。

    一个例子。文件三条，库里四条：

        文件   A(cadence=1)  B(cadence=1)  C(cadence=1)
        库里   A(cadence=1)  B(cadence=3)  D(是 B 改文字之前的旧写法)
                                            C 还没进库

        to_create   [C]
        unchanged   [(A, A)]
        differing   [(B, B, ['cadence_days 文件 1、库里 3'])]
        orphans     [D]

    只拿 source=manual 的行来比，是因为 memory_generated 和 interactive
    那些本来就不该出现在文件里 —— 把它们算成孤儿，每跑一次 seed 都会
    报一堆假警告，而真正的孤儿会被淹掉。

    比原文写法（不只是归一化文本）是有意的：把 yaml 里的 kickstarter
    改成 Kickstarter，归一化后一样、库里那一行不会变，而卡片上显示的是
    库里那个旧写法。这件事不报出来就只能靠肉眼发现。
    """
    by_identity = {
        identity_of(row.text_normalized, row.filters): row for row in db_rows
    }
    claimed: set[tuple[str, str]] = set()

    to_create: list[Seed] = []
    unchanged: list[tuple[Seed, DbQuery]] = []
    differing: list[tuple[Seed, DbQuery, list[str]]] = []

    for seed in seeds:
        row = by_identity.get(identity_of(seed.text_normalized, seed.filters))
        if row is None:
            to_create.append(seed)
            continue

        claimed.add(identity_of(seed.text_normalized, seed.filters))
        diffs: list[str] = []
        if seed.pool is not row.pool:
            diffs.append(f"pool 文件 {seed.pool}、库里 {row.pool}")
        if seed.cadence_days != row.cadence_days:
            diffs.append(f"cadence_days 文件 {seed.cadence_days}、库里 {row.cadence_days}")
        if seed.text != row.text:
            diffs.append(f"原文写法不同，库里是 {row.text!r}")

        if diffs:
            differing.append((seed, row, diffs))
        else:
            unchanged.append((seed, row))

    orphans = [row for key, row in by_identity.items() if key not in claimed]
    return SeedPlan(
        to_create=to_create, unchanged=unchanged, differing=differing, orphans=orphans
    )


def fetch_manual_queries(cur) -> list[DbQuery]:
    """取库里所有 source=manual 的 query。

    输入：游标
    输出：DbQuery 列表，按 id 排序

    只取 manual：别的来源不归这个文件管。
    """
    cur.execute(
        "select id, text, text_normalized, pool, cadence_days, is_active, filters "
        "from queries where source = 'manual' order by id"
    )
    return [
        DbQuery(
            query_id=row[0],
            text=row[1],
            text_normalized=row[2],
            pool=QueryPool(row[3]),
            cadence_days=row[4],
            is_active=row[5],
            filters=dict(row[6]),
        )
        for row in cur.fetchall()
    ]


def print_plan(plan: SeedPlan, *, seed_count: int, db_count: int) -> None:
    """把比对结果打出来。四类各一段，空的那类不占篇幅。"""
    print(f"文件 {seed_count} 条 · 库里 source=manual 的 {db_count} 条\n")
    print(f"  新增      {len(plan.to_create)} 条")
    for seed in plan.to_create:
        print(f"      {seed.pool} / {seed.cadence_days} 天  {seed.text}")

    print(f"  已存在    {len(plan.unchanged)} 条")
    for seed, row in plan.unchanged:
        print(f"      id={row.query_id}  {seed.text}")

    print(f"  字段不同  {len(plan.differing)} 条（不会改，库里那个才是权威）")
    for seed, row, diffs in plan.differing:
        print(f"      id={row.query_id}  {seed.text}")
        for diff in diffs:
            print(f"          {diff}")

    print(f"  孤儿      {len(plan.orphans)} 条")
    for row in plan.orphans:
        state = "还在跑" if row.is_active else "已停用"
        print(f"      [warn] id={row.query_id}  {state}  {row.text}")
    if plan.orphans:
        print("      库里是 manual 但文件里没有。最可能是有人改了 yaml 里某条的文字：")
        print("      归一化文本是身份，改了文字就是新的一条，旧那条不会消失。")
        print("      要停掉得单独去做 —— 关掉一条 query 是决定，不该由 seed 顺手完成。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真的写进库。默认只看不跑")
    args = parser.parse_args()

    seeds = load_seeds()

    with db.connect() as conn, conn.cursor() as cur:
        db_rows = fetch_manual_queries(cur)
        plan = plan_seed(seeds, db_rows)
        print_plan(plan, seed_count=len(seeds), db_count=len(db_rows))

        if not args.apply:
            print("\n只看不跑。真要写加 --apply")
            # 这一路一个字都没写，但 db.connect 退出时会 commit 一个空事务，
            # 无所谓 —— 重要的是上面没有任何 upsert 被调用过。
            return 0

        print()
        for seed in plan.to_create:
            query_id, created = query_db.upsert_query(
                cur,
                text=seed.text,
                source=QuerySource.MANUAL,
                source_type=seed.source_type,
                pool=seed.pool,
                cadence_days=seed.cadence_days,
                # 种子是人写进 queries.yaml 的，没有「为哪个题目造的」这回事
                topics=[],
                filters=seed.filters,
            )
            # created 为 False 说明库里已经有它了，而上面刚判定过没有。
            # 那意味着这中间有别的东西写了库，比对结果已经不作数 —— 炸掉
            # 比接着灌安全，否则报告说「新增 3 条」实际只新增了 1 条。
            if not created:
                raise RuntimeError(
                    f"比对时说没有、真写时却撞上了，库被并发改过？{seed.text!r}"
                )
            print(f"  建了 id={query_id}  {seed.text}")

        print(f"\n写完 {len(plan.to_create)} 条。")
        # 建完之后立刻按 scheduled 的口径选一次，让人当场看见「明天会跑哪几条」。
        # 新建的行 last_run_at 是空，所以正常情况下应该全部在列。
        pickable: list[QuerySpec] = query_db.pickable_queries(
            cur, now=dt.datetime.now(dt.UTC)
        )
        print(f"现在到期可跑的 {len(pickable)} 条：")
        for spec in pickable:
            print(f"  {spec.pool} / {spec.cadence_days} 天  {spec.text}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
