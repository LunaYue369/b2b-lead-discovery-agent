"""真连库，把 retrieve_knowledge 那一步捞回来的东西打印出来。只读，不花采集的钱。

    python -m scripts.peek_knowledge                      跑那一排探针题目 + 打印一份完整的
    python -m scripts.peek_knowledge microphone wearable  照这几个品类词跑一次

它回答的是写 2c 的 prompt 之前必须先知道的两件事：

    一、库里到底有什么          三条种子 query 各自跑过几次、带回过谁，纸面上的数字和
                                真数字对不对得上
    二、一个真实题目能命中几条   种子那三条词覆盖面有多窄。命中 0 条是预期内的结果，
                                那时 planner 手上只剩三条 avoid，2c 的 prompt 必须按
                                「多数时候库里没有相关经验」来写，而不是按「库里总有」

跟 tests/test_knowledge.py 不冲突：那边拿几行假数据焊形状和排序，能不连库反复回归；
这里要的恰恰是真数据 —— 真 query 的正文长什么样、命中率有多低，假数据里看不出来。

它只发 select：三个查库函数（all_queries / execution_totals / hits_by_query）都是只读的，
一行都不写。conftest.py 那道「测试里不许连库」的闸拦的是 pytest，不拦脚本。
"""

from __future__ import annotations

import argparse
import json

from persistence import db, discovery_db, lead_db, query_db
from query_intelligence.knowledge import build_knowledge, load_avoid, pick_relevant

# 默认那一排探针。挑的原则是「每条都对着一个我们想知道答案的问题」，不是凑数：
#
#     microphone / audio   种子 30 和 31 的正文里就有这个词，应该命中两条。
#                          它们是对照组 —— 一条都不中就说明匹配那一段坏了
#     voice recorder       种子 30 里它带引号。验引号挡不挡路
#     wearable / hardware  各自只在一条种子里出现，验挑出来的是不是只有那一条
#     bone conduction      Alex 真问过的题目。预期 0 条
#     conference microphone
#                          真实提问的形状（两个词的名词短语）。库里没有连着的
#                          conference microphone，所以它走的是中心词那条降级路，
#                          打出来是 conference microphone->microphone。这一条量的是
#                          降级那一步到底有没有把命中率救起来
PROBES = [
    ["microphone"],
    ["audio"],
    ["voice recorder"],
    ["wearable"],
    ["hardware"],
    ["bone conduction"],
    ["conference microphone"],
]


def print_ledger(cur) -> None:
    """把库里现有的 query 连同它们的战绩逐条打印出来。

    输入：游标
    输出：没有返回值，只往终端打

    这一段不经过 pick_relevant，是库里的全部 —— 先看清底账，再看挑出来的是其中哪几条。
    """
    #        queries 是库里全部（含关掉的），totals 是跑完过的那些的累计，
    #        hits 是这些 query 历来命中过哪些公司的明细
    queries = query_db.all_queries(cur)
    totals = discovery_db.execution_totals(cur)
    hits = lead_db.hits_by_query(cur, [query.query_id for query in queries])

    #        每条 query 命中过多少家（按 lead 去重之后）
    leads_per_query: dict[int, set[int]] = {}
    for hit in hits:
        leads_per_query.setdefault(hit.query_id, set()).add(hit.lead_id)

    companies = len({hit.lead_id for hit in hits})
    print(f"\n库里一共 {len(queries)} 条 query，命中明细 {len(hits)} 行，涉及 {companies} 家公司\n")
    for query in queries:
        total = totals.get(query.query_id)
        ran = query.last_run_at.date().isoformat() if query.last_run_at else "从没跑过"
        found = len(leads_per_query.get(query.query_id, ()))
        print(f"  id={query.query_id}  {query.text}")
        print(
            f"      pool={query.pool.value}  cadence={query.cadence_days}天  "
            f"is_active={query.is_active}  last_run_at={ran}"
        )
        if total is None:
            print("      一次都没跑完（executions=0），所以没有战绩")
        else:
            print(
                f"      executions={total.executions}  unique_companies={total.unique_companies}  "
                f"novel={total.novel_leads}  qualified={total.qualified_leads}"
            )
        print(f"      历来命中过 {found} 家公司（去重后）")


def print_probes(cur, probes: list[list[str]]) -> None:
    """对每一组品类词跑一次 pick_relevant，只打命中了哪几条。

    输入：游标；一批品类词组
    输出：没有返回值，只往终端打

    这一段是这个脚本存在的主要理由：它量的是「种子那三条词的覆盖面」。
    """
    #        queries 取一次就够，下面每一组探针拿同一份去挑
    queries = query_db.all_queries(cur)

    print("\n照品类词挑出来的相关 query：\n")
    for topics in probes:
        relevant = pick_relevant(queries, topics)
        if not relevant:
            print(f"  {topics}  ->  0 条。库里没有相关经验，planner 手上只剩 avoid")
            continue
        #        一个命中印成「命中的那个词:怎么命中的」，如 microphone:text。
        #        只中中心词的那种把两个词都印出来（conference microphone->microphone:head），
        #        因为那时候用户说的和真正命中的不是一个词
        picked = ", ".join(
            f"id={query.query_id}["
            + "/".join(f"{w}:{how}" if t == w else f"{t}->{w}:{how}" for t, w, how in matched)
            + "]"
            for query, matched in relevant
        )
        print(f"  {topics}  ->  {len(relevant)} 条：{picked}")


def print_one(cur, topics: list[str]) -> None:
    """把一个题目的完整 knowledge 原样打印出来，就是进 state 的那一份。

    输入：游标；一组品类词
    输出：没有返回值，只往终端打

    打完整的一份是为了看两件事：字段对不对，以及它有多大 —— 这一份要整个进
    plan_queries 那次调用的 prompt。
    """
    #        这四句跟 retrieve_knowledge 里的顺序一模一样，是照着那个节点抄的；
    #        抄不是复制逻辑，那个节点还要读 state、还要 print、还要返回写
    avoid = load_avoid()
    relevant = pick_relevant(query_db.all_queries(cur), topics)
    totals = discovery_db.execution_totals(cur)
    hits = lead_db.hits_by_query(cur, [query.query_id for query, _ in relevant])
    knowledge = build_knowledge(avoid=avoid, topics=topics, relevant=relevant, totals=totals, hits=hits)

    # 输出   原样打出来。ensure_ascii=False 是为了中文能看，那几条 avoid 是中文写的
    dumped = json.dumps(knowledge, indent=2, ensure_ascii=False)
    print(f"\n{topics} 那一份完整的 knowledge（{len(dumped)} 个字符）：\n")
    print(dumped)


def main() -> None:
    parser = argparse.ArgumentParser(description="只读地看一眼 retrieve_knowledge 捞回来的东西")
    parser.add_argument(
        "topics",
        nargs="*",
        help="品类词，不给就跑那一排默认探针。给了就只跑这一组，并打印完整的一份",
    )
    args = parser.parse_args()

    #        一次连接看完所有东西。Neon 闲置五分钟会挂起，首连大约一秒是冷启动不是慢
    with db.connect() as conn, conn.cursor() as cur:
        print_ledger(cur)
        if args.topics:
            print_probes(cur, [args.topics])
            print_one(cur, args.topics)
        else:
            print_probes(cur, PROBES)
            # 默认那一趟也打印一份完整的，挑对照组那一个 —— 它是预期能命中的
            print_one(cur, ["microphone"])


if __name__ == "__main__":
    main()
