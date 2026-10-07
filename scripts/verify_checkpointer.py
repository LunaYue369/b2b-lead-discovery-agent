"""验证 LangGraph 的 Postgres checkpointer 能不能在 Neon 上跑通。

这个脚本要回答的是一个「越晚做返工越贵」的未知数：step 7 的 interactive
discovery 要靠 checkpointer 把一次对话挂几天，而它连的是哪条串、建的表长
什么样、进程重启之后还能不能接上，前面所有连接配置都押在这上面。真到
step 7 才发现连不上，persistence 那一层要返工。

四段，每段回答一件事：

    1 建表    setup() 能不能在 direct 串上把它自己那几张表建出来
    2 挂起    最小图跑到 interrupt() 停住，状态是不是真落到库里了
    3 恢复    换一条全新连接、重新编译一张图，能不能从 checkpoint 接着跑
    4 压测    连着跑几十次会不会随机报错（pooler 那条老担忧的落点）

第 3 段是核心。同一个进程里接着跑不算数 —— 那可能只是内存里还留着东西。
必须把连接关掉、把图对象重新造一遍，才等价于「昨天挂起、今天 systemd
把进程拉起来接着跑」。

清理口径跟 verify_persistence.py 相反，这里是「删数据、留表」：
表本来就是 step 7 要用的，删了下次还得建；测试线程是垃圾，留着会让以后
查「有哪些对话挂着」时混进来一堆假的。

用法：
    PYTHONPATH=. python scripts/verify_checkpointer.py
    PYTHONPATH=. python scripts/verify_checkpointer.py --pooled   # 顺带验 pooled 串
    PYTHONPATH=. python scripts/verify_checkpointer.py --keep     # 不删测试线程
"""

from __future__ import annotations

import argparse
import time
import uuid
from typing import Annotated, TypedDict

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from persistence import db

# checkpointer 自己的四张表。它有一套跟我们无关的 migration 机制
# （checkpoint_migrations 里记版本号），所以这几张表不进我们的
# schema_migrations，别拿 scripts/migrate.py 去管它们。
CHECKPOINT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)


class TrialState(TypedDict):
    """最小图的状态。刻意跟真业务无关，只为验证「挂起前写的东西还在不在」。"""

    company: str
    steps: Annotated[list[str], lambda a, b: a + b]
    decision: str


def build_graph(saver: PostgresSaver):
    """造一张会在中间停下来等人的最小图。

    输入：一个已经 setup 过的 saver
    输出：编译好的图，带 checkpointer

    三个节点串成一条线，中间那个调 interrupt() 停住 ——
    这正是 step 7 里「Riley 还没点按钮」的形状。

    每次调用都重新造一张新图，是故意的：第 3 段要验的就是
    「图对象重新造一遍还能不能接上」，复用同一个编译结果就验不到了。
    """

    def collect(state: TrialState) -> dict:
        # 挂起之前写一点东西。恢复之后要能读回来，否则「恢复」只是把图跑了一遍。
        return {"steps": ["collected"]}

    def await_decision(state: TrialState) -> dict:
        # interrupt() 外面不包 try/except，这条是硬规则：包了会把恢复时的
        # 重放吞掉，症状是「恢复之后从错误的节点接着跑」，极难查。
        answer = interrupt({"question": f"要联系 {state['company']} 吗", "steps": state["steps"]})
        return {"decision": answer, "steps": ["decided"]}

    def record(state: TrialState) -> dict:
        return {"steps": ["recorded"]}

    graph = StateGraph(TrialState)
    graph.add_node("collect", collect)
    graph.add_node("await_decision", await_decision)
    graph.add_node("record", record)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "await_decision")
    graph.add_edge("await_decision", "record")
    graph.add_edge("record", END)
    return graph.compile(checkpointer=saver)


def report_tables(url: str) -> dict[str, int]:
    """看 checkpointer 那几张表建出来没有，各有多少行。

    输入：连接串
    输出：表名到行数的字典，表不存在就不出现在字典里

    单开一条普通 psycopg 连接来查，不复用 saver 的连接：这一段是「从外面
    看库里有什么」，跟被验证的那套东西用同一条连接会让结论没有说服力。
    """
    counts: dict[str, int] = {}
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        for table in CHECKPOINT_TABLES:
            cur.execute("select to_regclass(%s)", (f"public.{table}",))
            row = cur.fetchone()
            if row is None or row[0] is None:
                continue
            cur.execute(f"select count(*) from {table}")  # noqa: S608 表名来自上面的常量元组
            counts[table] = cur.fetchone()[0]
    return counts


def run_suite(url: str, label: str, *, stress: int, keep: bool) -> list[str]:
    """在一条连接串上把四段全跑一遍。

    输入：连接串；给报告用的名字；压测轮数；跑完删不删测试线程
    输出：这一跑创建的 thread_id 列表（--keep 时用来打印，好手动查）

    每一段都打印一行结论，失败一律抛异常 —— 这个脚本不返回
    「部分成功」，它存在的意义就是给一个能不能往下走的判断。
    """
    print(f"\n===== {label} =====")
    threads: list[str] = []
    tag = uuid.uuid4().hex[:8]

    # ---------- 第 1 段：建表 ----------
    t0 = time.perf_counter()
    with PostgresSaver.from_conn_string(url) as saver:
        connect_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        saver.setup()
        setup_ms = (time.perf_counter() - t1) * 1000
    print(f"1 建表   首次连接 {connect_ms:.0f} ms（含 Neon 冷启动）· setup {setup_ms:.0f} ms")
    tables = report_tables(url)
    missing = [t for t in CHECKPOINT_TABLES if t not in tables]
    if missing:
        raise RuntimeError(f"setup 跑完了但这几张表不在：{missing}")
    print(f"         四张表都在：{', '.join(f'{k}={v}行' for k, v in tables.items())}")

    # ---------- 第 2 段：挂起 ----------
    thread_id = f"verify-checkpointer-{tag}-main"
    threads.append(thread_id)
    config = {"configurable": {"thread_id": thread_id}}

    with PostgresSaver.from_conn_string(url) as saver:
        app = build_graph(saver)
        result = app.invoke({"company": "Chatterly", "steps": [], "decision": ""}, config)

    interrupts = result.get("__interrupt__") or ()
    if not interrupts:
        raise RuntimeError("图跑完了没停下来，interrupt() 没生效")
    print(f"2 挂起   停在 await_decision，问的是：{interrupts[0].value['question']}")

    # ---------- 第 3 段：换连接换图对象恢复 ----------
    # 上面那个 with 已经退出，连接关了、saver 没了、app 也不再引用。
    # 下面这一段等价于「进程重启之后接着跑」。
    with PostgresSaver.from_conn_string(url) as saver:
        fresh_app = build_graph(saver)

        snapshot = fresh_app.get_state(config)
        if not snapshot.next:
            raise RuntimeError("新连接看不到这个线程还有没跑完的节点，状态没落库")
        pending = snapshot.next
        print(f"3 恢复   新连接读到线程还欠着 {pending}，挂起前写的 steps={snapshot.values['steps']}")

        finished = fresh_app.invoke(Command(resume="approve"), config)

    if finished["decision"] != "approve":
        raise RuntimeError(f"恢复之后决定没写进去：{finished!r}")
    if finished["steps"] != ["collected", "decided", "recorded"]:
        raise RuntimeError(f"恢复之后步骤对不上，挂起前那段可能丢了：{finished['steps']}")
    print(f"         跑完了，steps={finished['steps']} decision={finished['decision']}")

    # ---------- 第 4 段：压测 ----------
    # 「跑一会儿随机报错」是那条老担忧的症状，一次成功证明不了什么。
    # 每轮换一个新 thread_id，因为要压的是连接和写入，不是同一条线程的重放。
    if stress:
        t2 = time.perf_counter()
        for i in range(stress):
            stress_id = f"verify-checkpointer-{tag}-stress-{i}"
            threads.append(stress_id)
            stress_config = {"configurable": {"thread_id": stress_id}}
            with PostgresSaver.from_conn_string(url) as saver:
                app = build_graph(saver)
                app.invoke({"company": f"Trial {i}", "steps": [], "decision": ""}, stress_config)
        elapsed = time.perf_counter() - t2
        print(f"4 压测   {stress} 轮各自新建连接跑到挂起，零报错，共 {elapsed:.1f}s"
              f"（每轮 {elapsed / stress * 1000:.0f} ms）")

    # ---------- 清理：删线程留表 ----------
    if keep:
        print(f"清理     --keep，{len(threads)} 个测试线程留在库里")
    else:
        with PostgresSaver.from_conn_string(url) as saver:
            for one in threads:
                saver.delete_thread(one)
        left = report_tables(url)
        print(f"清理     删掉 {len(threads)} 个测试线程，表留着："
              f"{', '.join(f'{k}={v}行' for k, v in left.items())}")
    return threads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pooled", action="store_true",
                        help="direct 之外，顺带在 pooled 串上跑一遍。checkpointer 生产不走这条，"
                             "跑它是为了把「pooler 到底行不行」这个问题彻底钉死")
    parser.add_argument("--keep", action="store_true",
                        help="不删测试线程。默认删，留着会污染以后「有哪些对话挂着」的查询")
    parser.add_argument("--stress", type=int, default=30,
                        help="压测轮数，默认 30。给 0 跳过")
    args = parser.parse_args()

    run_suite(db.connection_url(direct=True), "direct 串（checkpointer 生产走这条）",
              stress=args.stress, keep=args.keep)

    if args.pooled:
        run_suite(db.connection_url(direct=False), "pooled 串（对照，不是生产配置）",
                  stress=args.stress, keep=args.keep)

    print("\n结论：checkpointer 在 Neon 上跑通了，step 7 可以按现在的连接配置往下走。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
