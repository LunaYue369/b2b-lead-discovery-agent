"""interactive 采集的月度上限：这个账期对话里已经花了多少，这一次还能不能跑。

    load_budget_config   读 config/interactive.yaml 的 budget 那一段
    billing_period       某一刻属于哪个账期：这个账期的起点和下一个账期的起点
    check_monthly_cap    已花 + 这次报价 > 上限就抛 MonthlyCapReached

谁在调：
    nodes.run_harvest           在开任何事务、写任何一行之前。抛出去之后走的是
                                runner._recover 的情况一（query/slack_conversation._harvest_crash）：
                                退回 await_go，方案原样再发一遍，一分钱没花
    contacts.find_contacts      调付费 actor 之前。抛出来就不找付费的那一半（公司详情、查人），
                                卡片照推，免费的官网那一步照做

已花算两样（找联系方式的钱，两条路都算进同一个上限）：

    对话里的采集    query_executions 里 interactive 那几跑的计费条数 x 单价
    找联系方式      actor_spend 的全部行，定时和对话两条路都算，记的是 Apify 回的真扣费

定时那一跑的采集钱不在里面：定时每天花多少是可预测的，这道闸挡的是对话那边一天问十次。

算已花用的是我们自己的账，不去问 Apify：那样每说一次「跑」就要多一次外部请求，
而且 Apify 那个数把定时那一跑的采集也算进去了，分不出来。
我们的账有一处比 Apify 少：一跑在采集之后、提交之前崩溃，整跑回滚，query_executions 那几行
跟着没了，而 Apify 那边已经扣了钱。所以这道闸是软的那一道，Apify 后台设的 19 美元是硬的那一道。
"""

from __future__ import annotations

import datetime as dt
import math
import pathlib
from dataclasses import dataclass
from typing import Any

import yaml

from persistence import contacts_db, discovery_db

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "interactive.yaml"


@dataclass(frozen=True)
class BudgetConfig:
    """月度上限的两个数。

    一个实际值（仓库里那份配置）：

        BudgetConfig(monthly_cap_usd=13.0, billing_day=18)
    """

    monthly_cap_usd: float
    billing_day: int


class MonthlyCapReached(RuntimeError):
    """这个账期已花（对话采集 + 找联系方式）加上这一次的报价，超过了上限。这一次没跑，一分钱没花。

    一个实际值：

        MonthlyCapReached(spent_usd=9.95, estimate_usd=0.15, cap_usd=10.0,
                          since=datetime(2026, 9, 21, tzinfo=UTC),
                          resets_at=datetime(2026, 10, 21, tzinfo=UTC))
        str(那个值) -> "本账期（2026-09-21 起）对话采集和找联系方式已经花了 $9.95，这次报价 $0.15，
                        加起来会超过上限 $10.00。2026-10-21 开始的下一个账期恢复。"

    五个值都留在属性上：_recover 拼给用户的那句话直接用 str()，测试拿属性对值
    """

    def __init__(
        self,
        *,
        spent_usd: float,
        estimate_usd: float,
        cap_usd: float,
        since: dt.datetime,
        resets_at: dt.datetime,
    ) -> None:
        self.spent_usd = spent_usd
        self.estimate_usd = estimate_usd
        self.cap_usd = cap_usd
        self.since = since
        self.resets_at = resets_at
        super().__init__(
            f"本账期（{since:%Y-%m-%d} 起）对话采集和找联系方式已经花了 ${spent_usd:.2f}，"
            f"这次报价 ${estimate_usd:.2f}，加起来会超过上限 ${cap_usd:.2f}。"
            f"{resets_at:%Y-%m-%d} 开始的下一个账期恢复。"
        )


def load_budget_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> BudgetConfig:
    """读 config/interactive.yaml 的 budget 那一段。

    输入：配置文件路径（不给就是仓库根的 config/interactive.yaml）
    输出：BudgetConfig，如 BudgetConfig(monthly_cap_usd=13.0, billing_day=18)

    缺键当场 KeyError，不给默认值，跟这个文件夹里别的 load_*_config 同一条口径。
    """
    # 输入   文件内容。raw 是 budget 那一段，如 {"monthly_cap_usd": 10, "billing_day": 21}
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["budget"]

    # 上限得是一个有限的正数。NaN 要单独挡：NaN 跟任何数比大小都是 False，
    # 「已花 + 报价 > NaN」永远不成立，闸门就永远放行，而且不报错
    cap = float(raw["monthly_cap_usd"])
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError(f"budget.monthly_cap_usd 得是一个正数，拿到 {raw['monthly_cap_usd']!r}")

    # 1 到 28：29、30、31 号有的月份没有，账期的起点会算不出来
    day = int(raw["billing_day"])
    if not 1 <= day <= 28:
        raise ValueError(f"budget.billing_day 只许 1 到 28，拿到 {day}")

    return BudgetConfig(monthly_cap_usd=cap, billing_day=day)


def billing_period(now: dt.datetime, billing_day: int) -> tuple[dt.datetime, dt.datetime]:
    """now 这一刻属于哪个账期：返回这个账期的起点和下一个账期的起点。纯函数。

    输入：这一刻，带时区，如 2026-09-24T14:00Z；账期从每月几号开始，如 21
    输出：(这个账期的起点, 下一个账期的起点)，都是那天 UTC 零点，如
          (2026-09-21T00:00Z, 2026-10-21T00:00Z)

    四个例子（billing_day=21）：

        2026-09-24  -> (2026-09-21, 2026-10-21)   过了 21 号，账期从这个月 21 号起
        2026-09-21  -> (2026-09-21, 2026-10-21)   21 号当天零点就是新账期
        2026-09-20  -> (2026-08-21, 2026-09-21)   还没到 21 号，还在上个月开始的那个账期
        2026-01-05  -> (2025-12-21, 2026-01-21)   跨年

    按 UTC 零点切。Apify 后台的账期按什么时区切没有核实过，差出来的是账期交界那几个小时：
    那几个小时里我们算的已花可能比 Apify 多一天或少一天。影响只在月底那一两跑
    """
    # 输入   2026-09-24T14:00+08:00 这种别的时区的时刻
    # 输出   先换成 UTC 再看日期，否则东八区早上八点前会被算到前一天
    now = now.astimezone(dt.UTC)

    # this_month 是这个月的 billing_day 那天零点，如 2026-09-21T00:00Z
    this_month = dt.datetime(now.year, now.month, billing_day, tzinfo=dt.UTC)
    if now >= this_month:
        start = this_month
    else:
        # 还没到这个月的 billing_day：账期是上个月那天开始的。1 月往前一个月是去年 12 月
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        start = dt.datetime(year, month, billing_day, tzinfo=dt.UTC)

    # 下一个账期的起点：start 往后一个月。12 月往后一个月是明年 1 月
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start, dt.datetime(year, month, billing_day, tzinfo=dt.UTC)


def check_monthly_cap(
    cur: Any, *, estimate_usd: float, now: dt.datetime, budget: BudgetConfig, unit_price_usd: float
) -> float:
    """这一次花钱放不放行。放行就返回这个账期已经花掉的钱，不放行就抛 MonthlyCapReached。

    输入：
        cur              游标，查 query_executions 和 actor_spend
        estimate_usd     这次的报价，如 0.15（采集是 plan["estimated_cost_usd"]，planner 算的；
                         找联系方式是 contacts.quote_usd 算的）
        now              这一刻，带时区
        budget           BudgetConfig(monthly_cap_usd=10.0, billing_day=21)
        unit_price_usd   每条计费的单价，如 0.002，跟报价用的是 planner 配置里同一个数
    输出：本账期已花，如 1.23

    三个例子（上限 10）：

        已花 1.23 + 报价 0.15 = 1.38    -> 返回 1.23，放行
        已花 9.85 + 报价 0.15 = 10.00   -> 返回 9.85，放行。正好等于上限不算超
        已花 9.95 + 报价 0.15 = 10.10   -> 抛 MonthlyCapReached，这一次不跑

    用报价不用实际：实际花多少要采完才知道，而闸门必须在花钱之前。报价是按每条 query
    采满 max_posts 算的，实际只会少不会多，所以按报价挡是往严了挡。

    两条路同时说「跑」时这道闸不排队：两边各查一次、都放行、都去采，最多超出一次报价。
    真正让两跑串行的是 harvest_once 里那把 advisory lock，它在这道闸后面
    """
    # 输出   (2026-09-21T00:00Z, 2026-10-21T00:00Z)
    since, resets_at = billing_period(now, budget.billing_day)

    # 输入   since=2026-09-21T00:00Z
    # 输出   615，这个账期里 interactive 那几跑一共计费多少条
    billed = discovery_db.interactive_billed_items(cur, since=since)

    # 输入   since=2026-09-21T00:00Z
    # 输出   Decimal("0.48600")，这个账期找联系方式调 actor 一共真扣了多少，两条路都算
    contacts = contacts_db.spend_since(cur, since=since)

    # 输出   615 x 0.002 + 0.486 = 1.716
    #        contacts 是 Decimal，转 float 跟采集那一份加：这道闸下面本来就按 0.0001 四舍五入再比，
    #        float 的尾巴影响不到判断
    spent = billed * unit_price_usd + float(contacts)

    # 比之前先四舍五入到 0.0001 美元：浮点数的乘法会留尾巴，4850 x 0.002 + 0.3 算出来是
    # 10.000000000000002，不修的话「正好等于上限」会被当成超了
    if round(spent + estimate_usd, 4) > budget.monthly_cap_usd:
        raise MonthlyCapReached(
            spent_usd=spent,
            estimate_usd=estimate_usd,
            cap_usd=budget.monthly_cap_usd,
            since=since,
            resets_at=resets_at,
        )
    return spent
