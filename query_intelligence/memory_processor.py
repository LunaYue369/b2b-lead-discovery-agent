"""每天一次：把硬指标提炼成长期知识 + 生成 candidate queries。

这个模块闭合了「memory 怎么把 candidate 传给 scheduler」那个接缝：
它自己往 queries 表写具体的 query 行（source=memory_generated），
scheduler 直接取。策略 memory 只给 planner 用 —— 因为 scheduler 不调 LLM，
它拿不到「把策略变成 query 文本」这一步。

candidate 的晋升淘汰规则（不定这条会死锁：池子只进不出，
越来越多的词分那 20% 额度，每条都跑不满样本，永远评价不了）：
    跑满 N 次 execution → Wilson 下界 ≥ proven 中位数 → 进 proven
                       → 明显低于基线                → is_active=false
                       → 中间                        → 留 candidate，优先级降

读两个 namespace 时分开读：query_strategy 只读 lead_feedback，
draft_style 只读 draft_revisions。不要让 LLM 从改稿意见里猜 query 好坏。

TODO(step 8)
"""
