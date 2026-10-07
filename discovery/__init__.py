"""第三层 Shared Discovery Core —— scheduled 和 interactive 共用的那段。

    pipeline.py   把下面四步串起来。两个入口调的是同一个它
    harvest.py    调 adapter 取数
    normalize.py  RawSignal → Signal → Candidate → CompanyCluster
    prefilter.py  规则层砍（signal 级 + candidate 级）
    analyzer.py   LLM 打分

这一层不许 import slack，不许有任何供应商知识（那在 adapters/），
也不许连库（那在 persistence/）—— 三条都由 check_isolation.py 守着。

原计划这里还有 dedup.py（查 leads 表比 cooldown）和 lead_repo.py（写库），
两个都挪去了 persistence/：它们要连库，而这一层的闸门是纯函数 ——
同样的输入永远给同样的输出，才能拿历史数据反复回归。一层里一半纯函数
一半连库的话，「这层能不能回归」就没有统一答案了。
    查 cooldown 分流 -> persistence/lead_db.py 的 triage_by_cooldown
    写 leads / 证据   -> persistence/lead_db.py
    写 runs / 执行账  -> persistence/discovery_db.py
"""
