"""今天跑哪几条 query。纯代码不调 LLM，也不连库。

输入是 pickable_queries 查好的候选加 stats 算好的历史指标，输出是一份
说得清楚的计划。这一层刻意不查库，是为了「今天为什么只选了这五条」
能拿一批固定的行反复重放 —— 一旦它自己查库，重放就依赖当时的库状态，
那个问题就没有立足点了。

同样的输入必须给同样的输出。所以每个池子内部都排到 query_id 兜底，
额度让渡也按固定的池子顺序走，没有任何一处依赖字典或集合的迭代顺序。

三个池子的排序判据不一样，这是这个模块最要紧的设计：

    pinned      last_run_at 最久的先跑。人工钦定的本来就该全跑，
                排序只在条数超过额度时才起作用
    proven      rank_score 降序。stats 算的 Wilson 下界一类的数，
                不是裸比率
    candidate   执行次数升序，跑得最少的先跑。刻意不按表现排

candidate 那条是要紧的：它们本来就没有足够样本，按表现排的话样本少的
永远拿不到机会，池子就只进不出、每条都跑不满最小样本、谁好谁坏永远
评价不了。candidate 池的目的是取得样本，不是产出线索。
"""

from __future__ import annotations

import math
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import yaml

from models import QueryPool, QuerySpec, QueryStats

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "discovery.yaml"

# 池子的固定顺序，从「最该跑」到「最该让」。三处用到它，全都是为了确定性：
# 算额度时的取整余数怎么分、额度让渡一条一条给谁、报表按什么顺序打印。
POOL_ORDER = (QueryPool.PINNED, QueryPool.PROVEN, QueryPool.CANDIDATE)


@dataclass(frozen=True)
class SelectionConfig:
    """组队的两个旋钮，来自 config/discovery.yaml。"""

    daily_query_budget: int
    pool_ratios: dict[QueryPool, float]


@dataclass(frozen=True)
class PoolOutcome:
    """一个池子今天的账。这几个数放在一起才能解释「为什么是这些」。

    一个例子（pinned 空着、proven 不够、富余额度落到 candidate 的一天）：

        PoolOutcome(pool=PINNED,    target=2, available=0, chosen=0, received=0)
        PoolOutcome(pool=PROVEN,    target=6, available=4, chosen=4, received=0)
        PoolOutcome(pool=CANDIDATE, target=2, available=9, chosen=6, received=4)

        读法：pinned 让出 2 条、proven 让出 2 条，candidate 接了 4 条。

    刻意不记「这 4 条分别是从哪个池子来的」。让出额度的池子和接额度的池子
    是不重叠的两拨（一个池子让出，正是因为它已经没条数可跑了，所以它不可能
    同时再接），但两个捐方对两个受方时，谁那一条给了谁没有事实可依 ——
    leftover 是一堆没有标记的额度。编一个配对出来跟给转发的原帖编一个
    query_search 是同一类错：报表上多了一列看着很有信息量的假数据。

    要对账的话，让出的总数和接到的总数必定相等，两边分别看得见就够了。
    """

    pool: QueryPool
    target: int  # 按比例算出来的目标条数
    available: int  # 这个池今天有几条可跑
    chosen: int  # 实际选了几条
    received: int  # 接了几条别人让出来的额度（已经算进 chosen）

    @property
    def shortfall(self) -> int:
        """让出去几条：想要 target 条，但只凑得出 chosen 条。

        接额度的池子这个数是 0（它的 chosen 比 target 还多）。
        全场的 shortfall 之和等于 received 之和，加上没人接得住的那部分
        （SelectionPlan.unmet_budget）。
        """
        return max(0, self.target - self.chosen)


@dataclass(frozen=True)
class SelectionPlan:
    """今天的组队结果，以及它为什么是这样。"""

    chosen: tuple[QuerySpec, ...]
    per_pool: tuple[PoolOutcome, ...]  # 按 POOL_ORDER
    unmet_budget: int  # 额度没用完的条数
    warnings: tuple[str, ...]


def load_selection_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> SelectionConfig:
    """读 config/discovery.yaml。

    输入：配置文件路径，默认是仓库里那份
    输出：一个 SelectionConfig

    一个例子：

        文件   daily_query_budget: 2
               pool_ratios: {pinned: 0.2, proven: 0.6, candidate: 0.2}
        输出   SelectionConfig(daily_query_budget=2,
                               pool_ratios={PINNED: 0.2, PROVEN: 0.6, CANDIDATE: 0.2})

    用下标不用 get，跟 prefilter 的 load_rules 同一条口径：键缺了当场
    KeyError，不退化成一个默认值。这里退化的后果是「今天跑的条数跟
    配置说的不一样」，而它不报错。

    三个池子一个都不能少，比例之和必须是 1。缺一个池子等于那一档永远
    分不到额度，而 selector 照样跑得好好的；和不为 1 则会安静地改变
    实际跑的总量（写成 0.2/0.6/0.1 的话每天少跑一成）。两种都在这里炸。
    """
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    budget = raw["daily_query_budget"]
    if budget < 1:
        raise ValueError(f"daily_query_budget 至少是 1，拿到 {budget}")

    ratios_raw = raw["pool_ratios"]
    missing = [pool for pool in POOL_ORDER if str(pool) not in ratios_raw]
    if missing:
        raise ValueError(f"pool_ratios 少了这几档：{[str(p) for p in missing]}")

    ratios = {pool: float(ratios_raw[str(pool)]) for pool in POOL_ORDER}
    total = sum(ratios.values())
    # 浮点数不能拿 == 1 去比（0.2 + 0.6 + 0.2 在二进制里不是整 1）。
    if not math.isclose(total, 1.0, abs_tol=1e-9):
        raise ValueError(f"pool_ratios 三个数加起来要等于 1，现在是 {total}")

    return SelectionConfig(daily_query_budget=budget, pool_ratios=ratios)


def _targets(budget: int, ratios: Mapping[QueryPool, float]) -> dict[QueryPool, int]:
    """把额度按比例切成整数条，切完正好等于 budget。

    输入：今天的总条数；三档的比例
    输出：每档几条，三个数之和恰好等于 budget

    先各取整数部分，剩下的按小数部分从大到小一条一条补，
    小数部分打平时按 POOL_ORDER 决定谁先拿（这一步不定死的话，
    同样的输入会得出不同的分配）。

    两个例子：

        budget=2  比例 0.2/0.6/0.2
            精确值   0.4 / 1.2 / 0.4
            取整     0 / 1 / 0        已分 1 条，还剩 1 条
            小数部分 0.4 / 0.2 / 0.4  最大的是 pinned 和 candidate 打平，
                                      按 POOL_ORDER 给 pinned
            输出     pinned 1  proven 1  candidate 0

        budget=10 比例 0.2/0.6/0.2
            精确值   2.0 / 6.0 / 2.0
            取整     2 / 6 / 2        正好 10，不用补
            输出     pinned 2  proven 6  candidate 2

    第一个例子说明一件事：budget 很小的时候，比例几乎没有意义 ——
    2 条里分不出 20/60/20。真正让今天能跑起来的是下面那步额度让渡，
    因为 pinned 和 proven 现在都是空的。
    """
    exact = {pool: budget * ratios[pool] for pool in POOL_ORDER}
    targets = {pool: int(exact[pool]) for pool in POOL_ORDER}

    leftover = budget - sum(targets.values())
    # 按小数部分从大到小排，打平的按 POOL_ORDER。sorted 是稳定排序，
    # 所以先按 POOL_ORDER 建列表、再按小数部分降序排，打平时顺序就是
    # POOL_ORDER —— 不用另外写 tie-break。
    by_fraction = sorted(POOL_ORDER, key=lambda pool: exact[pool] - targets[pool], reverse=True)
    for pool in by_fraction[:leftover]:
        targets[pool] += 1
    return targets


def _rank(pool: QueryPool, specs: Sequence[QuerySpec], stats: Mapping[int, QueryStats]) -> list[QuerySpec]:
    """把一个池子里的候选排好序，最该跑的在前。三个池子判据不同。

    输入：哪个池；这个池里的候选（顺序是 pickable_queries 给的，
          也就是「从没跑过的最前，其次最久没跑」）；历史指标
    输出：排好序的候选

    pinned 直接沿用传进来的顺序；另外两个各自算一个排序键，
    再拿 query_id 兜底成全序。

    三个例子：

        pinned      [id=1 从没跑过, id=4 上周跑的]
                    不动。pickable_queries 已经是「最久没跑的先」，
                    而 pinned 本来就该全跑，排序只在超额度时起作用
                    输出 [1, 4]

        proven      id=30 rank_score=0.31   id=12 rank_score=0.44
                    按 rank_score 降序 -> [12, 30]

        candidate   id=29 executions=0  id=31 executions=3  id=30 executions=0
                    按执行次数升序，打平按 query_id -> [29, 30, 31]

    candidate 刻意不看 rank_score：它们本来就没跑满最小样本，按表现排
    等于让样本少的永远排在后面、永远拿不到额度，于是永远跑不满样本。
    那正是「candidate 池只进不出」的死锁。这个池子的目的是取得样本。

    proven 的候选要是在 stats 里找不到，当成 rank_score 为 0 排到最后，
    并由调用方记一条 warning。不 raise 是因为它不该让整跑挂掉 ——
    一条 proven query 没有指标是记账不一致，不是数据坏了，而 7 点的定时
    任务因为记账不一致今天一条线索都不出，代价大得多。也不静默：
    它会出现在 SelectionPlan.warnings 里。
    """
    if pool is QueryPool.PINNED:
        return list(specs)

    if pool is QueryPool.PROVEN:
        return sorted(
            specs,
            key=lambda spec: (
                -(stats[spec.query_id].rank_score if spec.query_id in stats else 0.0),
                spec.query_id,
            ),
        )

    return sorted(
        specs,
        key=lambda spec: (
            stats[spec.query_id].executions if spec.query_id in stats else 0,
            spec.query_id,
        ),
    )


def select_queries(
    pickable: Sequence[QuerySpec],
    *,
    stats: Mapping[int, QueryStats],
    config: SelectionConfig,
) -> SelectionPlan:
    """决定今天跑哪几条 query，并说清楚为什么是这几条。

    输入：pickable_queries 查出来的候选；stats 算好的历史指标
          （缺的当没有）；额度和比例
    输出：一份 SelectionPlan

    两个输入的地位完全不同，这是理解这个函数的关键：

        pickable  资格。它是前置条件 —— 一条 query 不在里面，指标再好
                  今天也不会跑（它要么被停用了，要么 cadence 还没到）
        stats     先后。只对已经在 pickable 里的 query 起作用

    所以 stats 里有没有某条、准不准，影响的是顺序，不是资格。这也是为什么
    stats 缺项不算错：一条从没跑过的 query 本来就不会出现在 query_executions
    的聚合结果里，而它照样有资格今天就跑。

    一共四步：按池子分堆并各自排序（_rank）-> 按比例算每池目标条数
    （_targets）-> 每池取 min(目标, 实际有几条) -> 把没用掉的额度按比例
    一条一条让给还有余量的池子。

    三个例子。第一个把每一步的中间结果都写出来了。

    一、proven 不够、pinned 空着、富余额度落到 candidate（最常见的一天）

        输入   pickable   pinned    0 条
                          proven    4 条  id=101 102 103 104
                          candidate 9 条  id=201..209
               stats      proven 那四条都有 rank_score；
                          candidate 里 201..205 跑过几次，206..209 从没跑过
                          （从没跑过的压根不在 stats 里）
               config     budget=10  比例 0.2/0.6/0.2

        分堆   by_pool = {pinned: [], proven: [4 条], candidate: [9 条]}

        _rank  pinned     []
               proven     按 rank_score 降序
                          [103(0.44), 101(0.31), 104(0.22), 102(0.09)]
               candidate  按执行次数升序，打平按 id
                          [206, 207, 208, 209(都是 0 次),
                           201(2 次), 203(3), 202(5), 204(5), 205(8)]

        _targets(10, 0.2/0.6/0.2)
               -> {pinned: 2, proven: 6, candidate: 2}

        第一轮 taken = min(目标, 可跑)
               pinned     min(2, 0) = 0   想要 2 条，一条都没有
               proven     min(6, 4) = 4   想要 6 条，只有 4 条
               candidate  min(2, 9) = 2
               已用 6 条，leftover = 4

        让渡   pinned 和 proven 都取光了，只有 candidate 还有余量
               -> 4 条全给 candidate，candidate 变成 6

        输出   chosen  10 条 = proven 那 4 条 + candidate 前 6 条
                       [103, 101, 104, 102, 206, 207, 208, 209, 201, 203]
               per_pool
                   pinned     目标 2  可跑 0  实选 0  接 0   让出 2
                   proven     目标 6  可跑 4  实选 4  接 0   让出 2
                   candidate  目标 2  可跑 9  实选 6  接 4   让出 0
               unmet_budget 0

        这个例子说明为什么额度让渡不是可选项：照字面分配的话 pinned 那 2 条
        和 proven 那 2 条就凭空蒸发了，今天只跑 6 条 —— 而报表上看起来
        完全正常，像是「今天就是只有这么多」。

    二、今天可跑的太少，额度用不完

        输入   pickable 一共 3 条（proven 1 条、candidate 2 条），budget=10
        _targets -> 2 / 6 / 2
        第一轮 taken 0 / 1 / 2，已用 3，leftover = 7
        让渡   三个池子全取光了，没有池子能接 -> 循环立刻停
        输出   chosen 3 条，unmet_budget=7

        额度没用完必须写进返回值，不能只留在日志里。这是「配额是天花板
        不是地板」的另一半：多了要明示截断，少了也要明示，否则
        「今天为什么只有三条线索」查不到源头。

    三、两个池子都还有余量，让渡按比例走

        输入   pinned 20 条、proven 3 条、candidate 5 条，budget=10
        _targets -> 2 / 6 / 2
        第一轮 taken 2 / 3 / 2，leftover = 3（proven 让出 3 条）
        让渡   pinned 和 candidate 都还有余量，每一条给「拿了之后离自己
               那份比例最远」的那个，也就是 (已拿+1)/比例 最小者 ——
               这个式子读作「我再拿一条的话，整场一共发到多少条的时候，
               我手上这些才算正常水平」，数越小说明拿得越少、越该轮到它
               （推导见下面循环处的注释）：
                 第 1 条  pinned (2+1)/0.2=15  candidate (2+1)/0.2=15
                          打平，按 POOL_ORDER 给 pinned    -> 3 / 3 / 2
                 第 2 条  pinned (3+1)/0.2=20  candidate 15
                          -> candidate                     -> 3 / 3 / 3
                 第 3 条  pinned 20  candidate (3+1)/0.2=20
                          打平，给 pinned                  -> 4 / 3 / 3
        输出   chosen 10 条，per_pool 三档 4 / 3 / 3

        换成「POOL_ORDER 靠前的先吃满」的话结果是 5 / 3 / 2，candidate
        一条都多不到。candidate 池的存在意义是取得样本，长期被挤掉的话
        那些 query 永远跑不满最小样本，也就永远评价不了。

    选出来的顺序按 POOL_ORDER，池内按各自的排序。采集那一层不依赖这个
    顺序（每条 query 各跑各的），但报表和日志靠它 —— 顺序不定的话，
    同样一天的两份报告长得不一样，对不上就会以为哪里错了。
    """
    warnings: list[str] = []

    # 分堆。用 POOL_ORDER 建字典，保证后面遍历的顺序是固定的。
    by_pool: dict[QueryPool, list[QuerySpec]] = {pool: [] for pool in POOL_ORDER}
    for spec in pickable:
        by_pool[spec.pool].append(spec)

    ranked = {pool: _rank(pool, by_pool[pool], stats) for pool in POOL_ORDER}

    missing_stats = [
        spec.query_id for spec in ranked[QueryPool.PROVEN] if spec.query_id not in stats
    ]
    if missing_stats:
        warnings.append(
            f"这几条 proven query 在 stats 里没有指标，当成 rank_score=0 排在最后："
            f"{missing_stats}。proven 是靠 stats 升上去的，没有指标说明记账对不上"
        )

    targets = _targets(config.daily_query_budget, config.pool_ratios)
    # 第一轮：每池按目标取，取不满就取光。剩下的额度进 leftover。
    taken = {pool: min(targets[pool], len(ranked[pool])) for pool in POOL_ORDER}
    received = dict.fromkeys(POOL_ORDER, 0)
    leftover = config.daily_query_budget - sum(taken.values())

    # 第二轮：一条一条让渡，每次给「还有余量的池子里最吃亏的那个」。
    # 判据是 (已拿+1)/比例 最小者胜，打平按 POOL_ORDER。
    #
    # 这个式子用大白话说：
    #   比例 0.2 的意思是「每发 5 条，该有我 1 条」，所以 1/0.2 = 5 就是
    #   「我每拿一条，对应总共该发 5 条」。那么
    #
    #       (已拿 + 1) / 比例
    #       = 我要是再拿这一条，整场一共发到多少条的时候，
    #         我手上这些才算正常水平
    #
    #   这个数越小，说明按自己的份额算，我现在拿得越少、越吃亏，
    #   下一条就该轮到我。越大说明我已经拿超前了。
    #
    # 拿下面那个例子走一遍（pinned 和 candidate 比例都是 0.2，各已拿 2 条）：
    #   第 1 条  pinned (2+1)/0.2 = 15
    #            「整场要发到 15 条，我手上这 3 条才算正常水平」
    #            candidate 同样是 15      两边一样吃亏，打平 -> 给 pinned
    #   第 2 条  pinned 现在 3 条 -> (3+1)/0.2 = 20
    #            candidate 还是 2 条 -> 15
    #            20 比 15 大，意思是 pinned 已经领先了 -> 给 candidate
    #   第 3 条  两边都是 3 条 -> 又打平 -> 给 pinned
    # 结果 4 / 3 / 3，两个池子轮着拿，谁也不会把富余额度独吞。
    #
    # 一条一条给而不是按比例重分一轮：这个循环每轮必定消耗一条，一定会停；
    # 按比例重分则可能每轮谁都分不到整一条而空转。
    #
    # 判据不用「POOL_ORDER 最靠前的先拿满」，因为那会让 pinned 把所有
    # 富余额度吃光。举个会真实发生的例子：pinned 有 20 条、proven 只有
    # 3 条（目标 6）、candidate 5 条，budget=10 ——
    #     先各取     2 / 3 / 2，还剩 3 条
    #     按靠前吃满  pinned 全拿 -> 5 / 3 / 2，candidate 一条没多拿
    #     按这个判据  pinned 3、candidate 3、pinned 4 -> 4 / 3 / 3
    # candidate 池的存在意义就是取得样本，被 pinned 长期挤掉的话，
    # 那些 query 永远跑不满最小样本，也就永远评价不了。
    #
    # 比例是 0 的池子永远不接额度（那正是把比例写成 0 的意思），
    # 所以给它一个无穷大的优先级值，而不是去除以 0。
    while leftover > 0:
        eligible = [pool for pool in POOL_ORDER if taken[pool] < len(ranked[pool])]
        if not eligible:
            # 所有池子都取光了还有额度剩，那就是今天真的没那么多 query 可跑。
            break
        pool = min(
            eligible,
            key=lambda p: (
                (taken[p] + 1) / config.pool_ratios[p]
                if config.pool_ratios[p] > 0
                else math.inf
            ),
        )
        taken[pool] += 1
        received[pool] += 1
        leftover -= 1

    chosen: list[QuerySpec] = []
    for pool in POOL_ORDER:
        chosen.extend(ranked[pool][: taken[pool]])

    per_pool = tuple(
        PoolOutcome(
            pool=pool,
            target=targets[pool],
            available=len(ranked[pool]),
            chosen=taken[pool],
            received=received[pool],
        )
        for pool in POOL_ORDER
    )
    return SelectionPlan(
        chosen=tuple(chosen),
        per_pool=per_pool,
        unmet_budget=leftover,
        warnings=tuple(warnings),
    )
