"""query 的硬指标：把 query_executions 的原始累计变成「今天该先跑哪条」。

这一层只算不查库。库那边（discovery_db.execution_totals）只出原始计数，
判断全在这里，而且是纯函数 —— 「今天为什么选这几条 query」要能拿一份
固定的数字重放出来，不必连库。跟 discovery/ 那一层是纯函数同一条理由。

两个会静默出错的地方，实现时是照这个来的：

1. 别用裸比率。 1 个 qualified / 1 个 researchable = 100%，裸比率会把它
   排在 40/100 前面。这里用 Wilson 下界：同样这两组数算出来是 0.21 和
   0.31，样本少的自己沉下去，不需要额外的规则去压它。
   最小样本闸门是另一道、也是硬的那道，见 has_enough_samples。

2. novel_lead_count 要参与排名。 只看 qualified 的话，一条「只会重复
   发现别人已发现的公司」的 query 看起来跟真正带来新线索的一样好。
   rank_score 是两个 Wilson 下界的加权和，novel 那一半占 40%。

指标口径（分母全部写在这里，别在别处再算一遍）：
    qualified yield     = qualified_lead_count / researchable_lead_count
    novel yield         = novel_lead_count / unique_company_count
    researchable yield  = researchable_lead_count / unique_company_count
    cooldown rate       = cooldown_skipped_count / unique_company_count
    human approval yield= approve / (approve + skip + reject + ignored)
                          分母必须含 ignored，否则沉默那部分白送高分

前两个进 rank_score。后三个现在不算：researchable yield 和 cooldown rate
描述的是「这条 query 老撞到冷却期里的老公司」，那件事已经被 novel yield
从另一头量到了；human approval yield 要等 step 6 的按钮上线才有数据，
lead_feedback 现在一行都没有。等有了数据再加进来，那时候权重要重定。

rank_score 只有这里算，selector 绝不自己算。两个地方各算一次的话，
报表上的排名和实际跑的排名迟早会不一样，而那种不一致没有任何报错。
这跟「memory 的 confidence 不许 LLM 自己写、必须由 stats 算」是同一条纪律。

TODO(step 8)：feedback 那一路（approve/reject/ignored）进 rank_score；
lead_discovery_hits 上的「独占发现」——「这家公司是不是只有这条 query
找得到」，那张表是 2026-09-01 建的，明细在，算法还没定。
"""

from __future__ import annotations

import math
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass

import yaml

from models import QueryStats, QueryTotals

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "discovery.yaml"


@dataclass(frozen=True)
class RankingConfig:
    """算 rank_score 的三个旋钮，来自 config/discovery.yaml 的 query_ranking 段。"""

    min_executions: int  # 跑够几次才够格被当成 proven 来评价
    wilson_z: float  # 置信水平。1.96 是 95%，越大越保守（越压小样本）
    qualified_weight: float  # 两个权重加起来必须是 1
    novel_weight: float


def load_ranking_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> RankingConfig:
    """读 config/discovery.yaml 的 query_ranking 段。

    输入：配置文件路径，默认是仓库里那份
    输出：一个 RankingConfig

    一个例子：

        文件   query_ranking:
                 min_executions: 5
                 wilson_z: 1.96
                 weights: {qualified: 0.6, novel: 0.4}
        输出   RankingConfig(min_executions=5, wilson_z=1.96,
                             qualified_weight=0.6, novel_weight=0.4)

    用下标不用 get，跟 selector 的 load_selection_config 同一条口径：
    键缺了当场 KeyError，不退化成一个默认值。这里退化的后果是「排名用的
    权重跟配置说的不一样」，而它不报错。

    两个权重之和必须是 1，跟 pool_ratios 那条检查同理：写成 0.6/0.3 不会
    报错，只会让所有 rank_score 一起变小 —— 排序不变，但报表上的绝对值
    跟历史对不上，而「这条 query 的分怎么突然掉了」会被当成质量问题查。
    """
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["query_ranking"]

    min_executions = int(raw["min_executions"])
    if min_executions < 1:
        raise ValueError(f"min_executions 至少是 1，拿到 {min_executions}")

    z = float(raw["wilson_z"])
    if z <= 0:
        raise ValueError(f"wilson_z 要大于 0，拿到 {z}")

    weights = raw["weights"]
    qualified, novel = float(weights["qualified"]), float(weights["novel"])
    # 浮点数不能拿 == 1 去比（0.6 + 0.4 在二进制里不是整 1）。
    if not math.isclose(qualified + novel, 1.0, abs_tol=1e-9):
        raise ValueError(f"weights 两个数加起来要等于 1，现在是 {qualified + novel}")

    return RankingConfig(
        min_executions=min_executions,
        wilson_z=z,
        qualified_weight=qualified,
        novel_weight=novel,
    )


def wilson_lower_bound(hits: int, trials: int, *, z: float) -> float:
    """一个比例的置信下界：把样本量当成排名的一部分，而不是事后再补一条规则。

    输入：命中数、试验数、置信水平对应的 z（1.96 是 95%）
    输出：0 到 1 之间的一个数，样本越少压得越狠

    大白话：它回答的不是「这条 query 的合格率是多少」，而是「按目前这点
    数据，我最保守能说它至少有多少」。所以同样是「看起来 100%」，
    只跑过 1 次的那条给不出什么保证，跑过 100 次的那条才敢往上排。

    四个实际值（z=1.96）：

        hits=1   trials=1     -> 0.2065   裸比率 100%
        hits=40  trials=100   -> 0.3094   裸比率 40%
        hits=61  trials=140   -> 0.3564   裸比率 44%
        hits=0   trials=10    -> 0.0      裸比率 0%

        第一行和第二行就是这套东西存在的全部理由：1/1 的那条排在
        40/100 后面。裸比率会把它排在最前面，然后它每天都被选中，
        而它可能只是运气好过一次。

    trials 是 0 时返回 0，不是「没有数据所以给个中间值」。没有证据就
    没有信用 —— 给中间值的话，一条从没分析过任何公司的 query 会排在
    一条实测很差的 query 前面，而后者至少是有据可查的。

    hits 大于 trials 直接炸：那说明传进来的两个数不是同一个口径的
    （比如把公司数当成了分析数），而算出来的分照样是个像模像样的小数。
    库里那两条 CHECK（qualified 不多过 researchable、novel 不多过 unique）
    守的是同一件事，这里再守一次是因为这个函数也给别的地方用。
    """
    if trials < 0 or hits < 0:
        raise ValueError(f"命中数和试验数都不能是负的：hits={hits} trials={trials}")
    if hits > trials:
        raise ValueError(f"命中数不能多过试验数：hits={hits} trials={trials}")
    if trials == 0:
        return 0.0

    p = hits / trials
    denominator = 1 + z * z / trials
    center = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    # 下界可能算成负数（hits=0 时），截到 0：rank_score 是个排序用的分数，
    # 负数在报表上只会引起「这条 query 是不是欠了什么」的误解。
    return max(0.0, (center - margin) / denominator)


def compute(totals: Mapping[int, QueryTotals], *, config: RankingConfig) -> dict[int, QueryStats]:
    """把库里的原始累计变成 QueryStats。纯函数，不连库。

    输入：query_id 到 QueryTotals 的映射（discovery_db.execution_totals 查的）；
          排名配置
    输出：query_id 到 QueryStats 的映射，键跟输入完全一样

    一个例子（两条 query，配置是 min_executions=5 / z=1.96 / 0.6 和 0.4）：

        输入   {30: QueryTotals(executions=12, unique_companies=418,
                                researchable_leads=140, qualified_leads=61,
                                novel_leads=203),
                31: QueryTotals(executions=1, unique_companies=8,
                                researchable_leads=1, qualified_leads=1,
                                novel_leads=1)}

               30 号：qualified 61/140 -> 下界 0.3564
                      novel     203/418 -> 下界 0.4381
                      0.6 * 0.3564 + 0.4 * 0.4381 = 0.3891
               31 号：qualified  1/1 -> 下界 0.2065
                      novel      1/8 -> 下界 0.0224
                      0.6 * 0.2065 + 0.4 * 0.0224 = 0.1329

        输出   {30: QueryStats(query_id=30, executions=12, rank_score=0.3891,
                               unique_companies=418, researchable_leads=140,
                               qualified_leads=61, novel_leads=203),
                31: QueryStats(query_id=31, executions=1, rank_score=0.1329,
                               unique_companies=8, researchable_leads=1,
                               qualified_leads=1, novel_leads=1)}

        31 号那一跑撞到 8 家公司，只有 1 家过了冷却分流、而那 1 家过了线
        —— 裸比率是「合格率 100%」，排出来只有 0.13，远在 30 号后面。
        而且它只跑过 1 次，连 has_enough_samples 那道闸门都过不了，
        压根不该在 proven 池里。

    没跑过的 query 不会出现在输出里，也不给它一行 0。selector 那边
    「在不在 stats 里」是有意义的：candidate 池当成 0 次执行排最前
    （它就是该先跑），proven 池当成记账不一致、记一条 warning。
    补一行 0 会把后者那条告警静默掉。

    原始支撑（后面四个数）原样带出来，不只带结论：几周之后要能回答
    「这个 0.3891 当时是怎么来的」，换了算法也能拿旧数据重算。
    这跟 memories.support 存原始支撑是同一条。
    """
    return {
        query_id: QueryStats(
            query_id=query_id,
            executions=total.executions,
            rank_score=rank_score(total, config=config),
            unique_companies=total.unique_companies,
            researchable_leads=total.researchable_leads,
            qualified_leads=total.qualified_leads,
            novel_leads=total.novel_leads,
        )
        for query_id, total in totals.items()
    }


def rank_score(total: QueryTotals, *, config: RankingConfig) -> float:
    """一条 query 的排序分：两个下界的加权和，越大越该先跑。

    输入：这条 query 的原始累计；排名配置
    输出：0 到 1 之间的一个数

    两个分量量的是不同的失败方式，缺一个都会漏掉一整类坏 query：

        qualified 那一半  找到的公司里有多少真值得联系
                          缺它 -> 一条净捞垃圾但捞得很勤的 query 排得很高
        novel 那一半      找到的公司里有多少是头一回见到的
                          缺它 -> 一条天天重新发现同一批老公司的 query
                                  看起来跟真正带来新线索的一样好

    一个例子：

        输入   QueryTotals(executions=12, unique_companies=418,
                           researchable_leads=140, qualified_leads=61,
                           novel_leads=203)
               61/140 -> 0.3564    203/418 -> 0.4381
        输出   0.6 * 0.3564 + 0.4 * 0.4381 = 0.3891

    两个分母不一样，这是有意的：合格率的分母是「真花钱分析过的」，
    不是「撞到的公司数」—— 冷却期里没分析的那些没有分数，算进分母等于
    因为 cooldown 起作用而惩罚这条 query。而新公司率的分母就该是撞到的
    全部公司：一条 query 老撞到已知的老公司，正是要量出来的毛病。

    一次都没跑成功过的 query 走不到这里（execution_totals 不返回它们）。
    跑过但一家公司都没撞到的会得 0 分，那是它的真实表现。
    """
    qualified = wilson_lower_bound(
        total.qualified_leads, total.researchable_leads, z=config.wilson_z
    )
    novel = wilson_lower_bound(total.novel_leads, total.unique_companies, z=config.wilson_z)
    return config.qualified_weight * qualified + config.novel_weight * novel


def has_enough_samples(total: QueryTotals, *, config: RankingConfig) -> bool:
    """这条 query 跑够次数、够格被当成 proven 来评价了吗。

    输入：这条 query 的原始累计；排名配置
    输出：True 表示样本够了

    两个例子（min_executions=5）：

        QueryTotals(executions=4, ...)  -> False  再好看也只能待在 candidate
        QueryTotals(executions=5, ...)  -> True   可以拿它的 rank_score 说话

    这是硬闸门，不是参考值：Wilson 下界已经把小样本压下去了，但它压的是
    「排名」，压不住「升级」。一条跑过两次、每次都撞上一个好线索的 query
    下界能到 0.2 出头，在一个还很空的 proven 池里可能就是第一名，
    然后它以 proven 的身份长期吃掉 60% 的采集预算 —— 而支撑它的只有
    两次样本。样本量必须是能否进入那个池子的前置条件，不是分数的一部分。

    它只回答「够不够格被评价」，不回答「值不值得升级」。真正的升级还要
    比一条 baseline（跟同期别的 query 比、跟池子平均比），那条规则连同
    淘汰规则一起在 step 8 的 memory_processor 里，还没写。这个函数是那条
    规则的硬前置：不管将来怎么比，这里返回 False 的一律不许进 proven。
    """
    return total.executions >= config.min_executions
