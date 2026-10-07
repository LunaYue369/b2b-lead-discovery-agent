"""langmem store 读写 —— 长期知识结论。

namespace 两组：
    ("b2b_outreach", "query_strategy")            给 planner 用
    ("b2b_outreach", "lead_preferences", "riley")  给 planner / analyzer 用

别把它当第二个业务库。 硬指标算完就算完，只把结论喂进来。
value 里存的是「该怎么做」，不是「发生了什么」。

confidence 不许 LLM 自己写。LLM 给的置信度是没有校准的常数，
拿它当门槛什么都筛不掉（spaCy 那个 0.85 已经教过一次）。
必须由 stats 算出来传进去，并且把原始支撑一起存：
    support: {executions: 42, qualified_rate: 0.31, baseline: 0.12}
这样结论可审计、可重算、可过期。

TODO(step 8)
"""
