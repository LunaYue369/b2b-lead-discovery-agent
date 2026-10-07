"""落库。这一层是唯一认识 SQL 和连接串的地方。

    db.py            怎么连上（pooled / direct 两条串的分工）
    migrations/      手写编号 SQL，一个文件一步，只往前不回滚
    discovery_db.py  runs / query_executions / signals —— 一次跑批自己的账
    lead_db.py       leads / lead_evidence —— 公司这一侧，含 cooldown 分流
    query_db.py      queries —— 该搜什么
    feedback_db.py   lead_feedback —— Riley 的态度

按表分文件不按动作分：跑批的账写完就不再动，公司那边会被后面每一跑
反复更新，两者的生命周期不一样。

discovery/ 里原计划的 dedup.py 和 lead_repo.py、query_intelligence/ 里原计划的
query_repo.py，都挪到了这里，因为它们要连库，而那两层是纯函数
（同样的输入永远给同样的输出，才能拿历史数据反复回归）。

不用 ORM 也不用供应商的 SDK，是为了让换库的成本停在一个连接串上。
Postgres 是硬约束（LangGraph 的 checkpointer 只有 Postgres 官方实现），
但「哪家的 Postgres」应该随时能换。
"""
