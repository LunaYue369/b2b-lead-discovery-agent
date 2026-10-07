"""数据源接口（ports/adapters 的 port 一侧）。

这个接口存在的全部理由：采集是整个系统最脆的一层。
Proxycurl 在 2025-07 被 LinkedIn 起诉后直接关停，客户一夜断供。
所以从第一天起就假定「供应商会死」：换实现时 discovery/ 一行不动。

极端情况也不改结构 —— 如果 LinkedIn 这条路彻底不行，入口换成众筹，
那正是这个 Protocol 存在的意义。

约定（写新 adapter 时必须遵守）：

1. adapter 负责翻译供应商格式，产出 RawSignal。 「归一格式」这件事
   只有 adapter 知道怎么做；normalize 只做跟供应商无关的事
   （算 content_hash / matched_terms / 展开候选）。这样 discovery/
   里没有一行供应商知识。

2. 原始 JSON 一个字都不动地塞进 RawSignal.raw。 采集数据当天落自己的库，
   这是供应商会死那条推论出来的硬规则。

3. 抓不到就 raise，不返回空列表冒充成功。 静默返回 0 条会让
   query 的表现指标记成「这条 query 没线索」，实际是网络断了 ——
   污染的是决定每天 60% 预算的那组数字。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from models import QuerySpec, RawSignal


@runtime_checkable
class SourceAdapter(Protocol):
    """一个数据源。HarvestAPI 有 16 个 actor，它们是同一个 adapter 的不同方法。"""

    name: str  # 落进 RawSignal.source，用来归因

    def fetch(self, spec: QuerySpec, *, max_items: int) -> list[RawSignal]:
        """跑一条 query，返回翻译好的信号。

        max_items 是天花板不是地板 —— 不够就返回少的，不要凑数。
        """
        ...

    def billed_items(self) -> int:
        """上一次 fetch 实际计费的 item 数。

        跟 len(fetch(...)) 不相等：评论会作为独立 item 计费
        （实测 60 条 post 的一跑，计费对象是 120 个 item）。
        """
        ...
