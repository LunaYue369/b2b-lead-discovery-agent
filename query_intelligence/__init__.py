"""第二层 Query Intelligence —— 关于「该搜什么」的一切。

    stats.py             硬指标计算（yield / Wilson 下界 / 最小样本闸门）
    selector.py          scheduled 的 20/60/20 组队，纯代码不调 LLM
    memory_store.py      langmem store 读写（长期知识）
    planner.py           interactive 的 LLM query planner
    memory_processor.py  stats → 长期知识 + candidate queries

这一层不许 import slack，也不许连库（check_isolation.py 守这两条）。

原计划里的 query_repo.py 挪到了 persistence/query_db.py，跟当初把
discovery/ 的 lead_repo.py 和 dedup.py 挪走是同一个理由：一层里一半纯函数、
一半连库的话，「这层能不能拿历史数据回归」就没有统一答案。而这一层恰恰是
最需要能回归的 —— 「今天为什么只选了这五条 query」必须能在不连库的情况下
重放出来，否则出了问题只能对着当时的库状态干瞪眼。

所以这里的函数一律「收数据、算结果」：queries 的行和历史指标由调用方从
persistence/ 查好传进来。
"""
