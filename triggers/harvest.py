"""开一次 run、跑完 discovery、给 runs 那行收尾 —— scheduled 和 interactive 共用的那一段。

两条路都要花钱采集，而花钱这件事的顺序纪律只能有一份：

    ① 先问「现在有没有别人在跑」（advisory lock，拿不到就当场退，不排队）
    ② insert_run 记 running
    ③ run_discovery 干活，整跑一个事务
    ④ 按返回的 status 给 runs 那行收尾，单独一个事务

两条路的差别只在这一段的外面：scheduled 前面要选今天跑哪几条 query、后面要推 Slack
卡片；interactive 前面是用户在对话里拍的板、后面由图上另一个节点去发。差别留在各自的
trigger 里，这一段不认识它们。

这个模块不做任何业务判断 —— 采集、闸门、归并、打分全在 discovery/pipeline.py 的
run_discovery 里。它只管「一次 run 的账怎么开、怎么收」。
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
import yaml

from adapters.base import SourceAdapter
from adapters.harvestapi_posts import HarvestApiPostsAdapter, RecordedPostsAdapter
from discovery.analyzer import ScoringConfig, load_facts, load_scoring
from discovery.cooldown import CooldownConfig, load_cooldown_config
from discovery.pipeline import run_discovery
from discovery.prefilter import FilterRules, load_rules
from models import POSTED_WITHIN_VALUES, DiscoveryResult, QuerySpec
from persistence import db, discovery_db
from persistence.discovery_db import try_lock_discovery
from persistence.store import PostgresDiscoveryStore


class DiscoveryBusy(RuntimeError):
    """现在已经有一跑 discovery 在跑，这一次没开始。

    这不是失败，是「没轮到」：一个字都还没写库，一分钱都还没花。两条路各自决定
    怎么办 —— scheduled 当天这一跑就没了（要看得见），interactive 那边用户还在
    对话里，再说一次「跑」就行。
    """


DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "discovery.yaml"

# 采集回来的原始 dataset 存这里，跟着 repo 走、进 git，不放临时目录。
# 它同时是两件东西：花钱买回来的原料的唯一副本（供应商哪天关停都还在），
# 和下一次改闸门时的回归集。跟 scripts/harvest_once.py 用同一个目录。
DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class RunConfig:
    """discovery.yaml 里属于「这一跑怎么跑」的那几个数。

    没跟 SelectionConfig 合并：那个是「今天跑哪几条 query」的旋钮，
    这个是「每条 query 怎么跑」的。两件事改动的时机完全不同 ——
    额度那两个数会跟着预算调，这几个数跟着数据质量调。

    一个实际值（仓库里那份配置）：

        RunConfig(max_posts_per_query=40, seen_days=10, review_score=40,
                  posted_within='month')
    """

    # 每条 query 最多采几条 post。天花板不是地板：供应商只有 12 条就返回
    # 12 条，不凑数。它跟 daily_query_budget 相乘，决定这一跑花多少采集的钱
    # （实测 5 条 query 采 191 条约 0.38 美元）。
    max_posts_per_query: int

    # prefilter 的跨天记忆往回看几天：这些天里出现过的正文摘要，再来一次
    # 就当重复丢掉。定这个数的理由是语义不是存储 —— 超过十天还一模一样的
    # 正文再出现，更可能是一件新事，而不是同一轮联播的尾巴。
    #
    # 它跟另外两个时间闸门不是一回事，三个的主语各不相同：
    #     cadence_days   主语是 query   管采集的钱（这条 query 几天跑一次）
    #     seen_days      主语是正文     管重复（这段话见过没有）
    #     cooldown_until 主语是公司     管 LLM 的钱（这家还要不要再分析）
    seen_days: int

    # 「够得着但没过线」那一档的下界，只进报告、不参与任何判断。
    #
    # 过线（>= cooldown 那边的 qualified_score）是要推给 Riley 的，这一档是
    # 攒给她标注的：推给她的要严，给她标的要有量，否则阈值永远校准不了
    # （A/B/C 阈值本来就要由她标 20-50 条反推）。
    # 两个数都是临时值，所以放配置不写死在代码里。
    review_score: int

    # 采集窗口的默认值：没人特别指定时，只要这段时间里发的帖子。
    # 取值见 models.POSTED_WITHIN_VALUES。
    #
    # 它是「默认」不是「唯一」：interactive 那条路上用户说了时间范围的话，
    # 走的是他说的那个，这里这份只在他没说的时候用。
    posted_within: str


def load_run_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> RunConfig:
    """读 discovery.yaml 里这一跑要用的三个数。

    输入：配置文件路径，默认是仓库里那份
    输出：一个 RunConfig

    一个例子：

        文件   max_posts_per_query: 40
               seen_days: 10
               review_score: 40
               posted_within: month
        输出   RunConfig(max_posts_per_query=40, seen_days=10, review_score=40,
                         posted_within='month')

    用下标不用 get，跟 load_rules / load_selection_config 同一条口径：
    键缺了当场 KeyError。这里退化成默认值的后果是「实际采的条数跟配置说的
    不一样」，而它不报错 —— 明天有人把 40 改成 10，发现线索变少之后先怀疑
    的是 query，不会想到配置根本没被读。

    qualified_score 刻意不在这里读：它已经在 cooldown 那份配置里，
    两处各读一次的话，改了一处会让「过线」和「冷却按过线分档」用两个数。
    """
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))

    # 取值查一遍闭集。写错一个字母（'weeks'）的话，actor 会判输入非法、
    # 整跑失败 —— 失败在花钱之前是好的，但那仍然是今天这一跑没了，
    # 而排查要从 Apify 的报错一路回溯到这份 yaml。在这里炸能直接说清。
    posted_within = str(raw["posted_within"])
    if posted_within not in POSTED_WITHIN_VALUES or not posted_within:
        raise ValueError(
            f"discovery.yaml 的 posted_within 是 {posted_within!r}，"
            f"合法值只有 {POSTED_WITHIN_VALUES[1:]}（空串在这里没有意义）"
        )

    return RunConfig(
        max_posts_per_query=int(raw["max_posts_per_query"]),
        seen_days=int(raw["seen_days"]),
        review_score=int(raw["review_score"]),
        posted_within=posted_within,
    )


def build_llm_client() -> Any:
    """造一个 OpenAI 客户端。

    输入：无（密钥从环境变量或仓库根的 .env 读）
    输出：一个 openai.OpenAI

    在这里读密钥，而不在 analyzer 里读：那一层收的是「一个能 chat 的
    对象」，谁给它不关它的事。这样 analyzer 的纯计算部分在没有密钥的
    机器上也测得动，而测试里传进去的就是个假客户端。

    在函数里 import openai 也是同一个道理 —— --plan-only 那条路根本不
    需要模型，没装 openai 也该能跑。
    """
    from openai import OpenAI

    key = os.environ.get("OPENAI_API_KEY") or db._load_env_file().get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("找不到 OPENAI_API_KEY：设环境变量，或写进仓库根的 .env")
    return OpenAI(api_key=key)


def dump_path_for(run_id: int, now: dt.datetime) -> pathlib.Path:
    """这一跑的原始 dataset 存哪儿。

    输入：这一跑的 run_id；这一跑的时刻
    输出：一个路径，形如 data/harvest_run27_2026-09-02.json.gz

    一个 run 一个文件，不是一条 query 一个文件：文件里每条 item 自带
    query.search 归因，所以一份文件能同时服务那一跑的所有 query，而回放
    那一侧本来就是这么读的（早期那份手工采的里面有五条 query）。

    名字里带 run_id 是为了跟库里的账对得上（哪一跑采的、花了多少、
    发现了几家，都在 runs 和 query_executions 里）；带日期是为了 ls
    出来能一眼看出时间跨度。
    """
    return DATA_DIR / f"harvest_run{run_id}_{now:%Y-%m-%d}.json.gz"


def build_adapter(
    replay: str | None, *, dump_path: pathlib.Path, posted_within: str, posted_since: str | None = None
) -> SourceAdapter:
    """选采集实现：真跑还是回放。

    输入：回放文件路径，None 表示真采集；真跑时原始数据存哪儿；采集窗口（那几档之一）；
          按日期截的采集窗口，如 '2026-07-28'，给了就用它、不用那一档（只有对话那条路会给）
    输出：一个 SourceAdapter

    posted_within 只对真跑那一支有意义：回放读的是已经采好的文件，
    窗口在当初采的时候就定死了。这也是回放的一个已知窄处 —— 拿回放
    验不了「换个窗口会采到什么」。

    回放那条路是给「整条链路跑一遍」用的：真 store、真库、真模型，
    只有采集是存下来的数据，一分钱不花。跟 from_file 是同一个定位 ——
    开发主力入口，不是测试桩。

    三种情况：

    一、真跑（没给 --replay）

        输入   replay=None，dump_path=Path('data/harvest_run27_2026-09-02.json.gz')
        输出   HarvestApiPostsAdapter(dump_path=那个路径)
        后果   run_discovery 里每条 query 起一个 Apify run、轮询到跑完。
               今天选中 2 条 query x 每条最多 40 条 post，约 0.15 美元。
               这是唯一会花采集钱的一支。
               每条 query 采完，原始 dataset 原样写进那个文件 ——
               两件事都靠它：钱花出去买回来的东西要在自己手上（供应商
               哪天关停都不影响已经采到的），以及它是下一次改闸门时的
               回归集。--no-persist 也照样存：钱已经花了，
               库里回滚跟数据要不要留是两件事

    二、回放（--replay data/harvest_2026-08-27_010047.json）

        输入   replay='data/harvest_2026-08-27_010047.json'
               印一行 "[replay] 回放模式，采集不花钱：..."，
               构造时把整份 JSON 读进内存（191 条，五条 query）
        输出   RecordedPostsAdapter(那个路径)
        后果   fetch 按 spec.text 精确匹配 query_search 挑出那一条当初采到的
               post。文件里那五条 query 中有三条正好是库里 id 29/30/31 的
               种子，所以今天选中的两条都回放得出来。
               选中的 query 文本在文件里没有的话，那一条会回放出 0 条 ——
               RecordedPostsAdapter 会印一行警告并列出文件里有哪几条，
               否则整跑会一路正常跑完、产出 0 家公司，跟「今天确实没线索」
               在报表上长得一模一样

    三、回放文件不存在

        输入   replay='data/typo.json'
        输出   不返回，from_file 里 FileNotFoundError
        后果   炸在采集之前、也在开 run 之前，库里什么都不会留。
               这正是想要的顺序 —— 路径打错不该先花掉一次采集的钱

    两个实现都满足 SourceAdapter，所以 run_discovery 分辨不出拿到的是哪个。
    这就是那个 Protocol 存在的意义：换实现时 discovery/ 一行不动。
    """
    if replay:
        print(f"[trigger] 回放模式，采集不花钱：{replay}")
        return RecordedPostsAdapter(replay)
    # adapter 那侧这个参数叫 posted_limit（跟着供应商的字段名走），
    # 我们这侧叫 posted_within。翻译就发生在这一行。
    return HarvestApiPostsAdapter(dump_path=dump_path, posted_limit=posted_within, posted_since=posted_since)


@dataclass(frozen=True)
class HarvestConfigs:
    """跑一次 discovery 要用的五份配置，一次读齐。

    单独拼成一个对象，是为了让「读配置」这件事只有一个地方。两条路各读一遍的话，
    哪天多一份配置、或者某两份之间多一条互相制约的关系，只会有一边被改到，
    而症状是两条路跑出不一样的结果、两边都不报错。

    一个实际值（仓库里那几份配置）：

        HarvestConfigs(run=RunConfig(max_posts_per_query=40, seen_days=10,
                                     review_score=40),
                       rules=..., facts=..., scoring=..., cooldown=...)
    """

    run: RunConfig
    rules: FilterRules
    facts: dict[str, Any]
    scoring: ScoringConfig
    cooldown: CooldownConfig


def load_harvest_configs() -> HarvestConfigs:
    """把五份配置一次读齐，顺手查一条跨文件的约束。

    输入：无（路径都是仓库里那几份）
    输出：一个 HarvestConfigs

    那条跨文件的约束只能在这里查，因为只有这里两个数同时在手上：品类完全不对那一档
    的封顶值（scoring.yaml 的 off_category_cap）必须低于边界档的下界（discovery.yaml
    的 review_score），否则那批公司会挤进给 Riley 标注的样本里，而那一档的阈值本来
    就要靠她标出来的东西反推。

    不把其中一个数抄到另一份配置里凑到一起查 —— 一个数抄两份，改了一处之后两处会用
    两个数，那个坑在 qualified_score 上已经避过一次。

    缺键的话在这里就炸，也就是在花任何钱之前。
    """
    run = load_run_config()
    scoring = load_scoring()
    if scoring.off_category_cap >= run.review_score:
        raise ValueError(
            f"scoring.yaml 的 off_category_cap={scoring.off_category_cap} 不低于 "
            f"discovery.yaml 的 review_score={run.review_score}："
            f"品类完全不对的公司会挤进给 Riley 标注的边界档"
        )
    return HarvestConfigs(
        run=run,
        rules=load_rules(),
        facts=load_facts(),
        scoring=scoring,
        cooldown=load_cooldown_config(),
    )


def harvest_once(
    conn: psycopg.Connection,
    cur: psycopg.Cursor,
    queries: Sequence[QuerySpec],
    *,
    run_type: str,
    thread_id: str | None,
    now: dt.datetime,
    configs: HarvestConfigs,
    replay: str | None = None,
    persist: bool = True,
    skip_cooldown: bool = False,
    posted_within: str = "",
    posted_since: str | None = None,
) -> tuple[int, DiscoveryResult]:
    """开一次 run、跑完 discovery、给 runs 那行收尾。

    输入：
        conn / cur     同一个连接上的连接和游标。锁和 runs 那一行都跟着这个事务走
        queries        这一跑要跑哪几条。scheduled 由 selector 选出来，interactive 是
                       用户在对话里拍板的那几条
        run_type       'scheduled' / 'interactive'，直接写进 runs.run_type
        thread_id      是哪条对话让它跑的。scheduled 传 None
        now            当前时刻，带时区。整跑只取一次，分头取会让两次跑不出同样的结果
        configs        五份配置，load_harvest_configs() 读出来的
        replay         回放文件路径。给了就一分钱不花，只有 scheduled 的排查用得上
        persist        False 时整跑最后回滚（连 runs 那行一起）
        skip_cooldown  冷却期整个让路，冷却中的和终态的一起重新分析。很贵
        posted_within  采集窗口，那几档之一。空串表示「没指定」，用 configs.run 里那个默认。
                       空串当默认而不是把默认值写进签名：默认值写在签名上的话，
                       它和 discovery.yaml 里那份是两个数，改了一处不会报错
        posted_since   按日期截的采集窗口，如 '2026-07-28'。给了就用它，posted_within 不再起作用。
                       只有 interactive 那条路会给：用户说了「最近两个月」，plan_queries 换算好的那个日期。
                       scheduled 不给
    输出：
        (run_id, DiscoveryResult)。一个实际值（一条 query、采回 40 条的那一跑）：

            run_id = 36

            DiscoveryResult(
                run_id=36,
                status=RunStatus.COMPLETED,
                per_query=(
                    QueryOutcome(
                        query_id=44,
                        execution_id=27,
                        status=ExecutionStatus.COMPLETED,
                        counters=ExecutionCounters(
                            raw_result_count=40,          供应商返回的原始条数
                            billed_item_count=40,         按这个数计费
                            signal_dropped_count=23,      prefilter 在帖子这一级砍掉的
                            candidate_dropped_count=5,    在公司这一级砍掉的
                            drop_reasons={'no_relevance_signal': 13, 'supplier_content': 9,
                                          'company_page_author': 5, 'off_topic': 1},
                            unique_company_count=1,       归并后剩几家
                            researchable_lead_count=1,    这次真花 LLM 钱分析的
                            cooldown_skipped_count=0,     冷却期没到、省下的
                            excluded_skipped_count=0,     已是终态、连 LLM 都省了的
                            novel_lead_count=1,           头一回见到的
                            qualified_lead_count=1,       分数过线的
                        ),
                        error=None,
                    ),
                ),
                scored={'li:200000013': Assessment(llm=..., det=..., score=66)},
                lead_ids={'li:200000013': 1281},
                discarded={},
                warnings=(),
            )

        四项的分工：per_query 是每条 query 各自的账，一条对一行 query_executions；
        scored 是打完分的公司，键是归并用的 company_key；lead_ids 把同一个键换成
        leads.id，company_count 和日志里的 lead= 要用（拼卡片不用它，按 run_id 从库里查）；
        discarded 是反复答不好被丢弃的那批，只进日志和计数，一条 Assessment 都没有，
        跟 scored 不重叠。
        要「这一跑一共几家公司」用 result.company_count，它数的是去重后的 lead_id ——
        把各条 query 的 unique_company_count 加起来是错的，一家公司被两条 query 命中时
        两条账里各记一次，那个和会大于实际家数。

        status 三种：全成 COMPLETED，全挂 FAILED，五条里两条超时三条成功 PARTIAL。
        它只在 run 这一层有意义，每条 execution 自己要么成功要么失败。

    抛 DiscoveryBusy：已经有一跑在跑。这时候一个字都没写库、一分钱都没花。

    三处非对称，是这个函数唯一容易写错的地方：

      run_discovery 抛异常时不要再 finish_run —— 那一行连同整跑已经被回滚掉了，
      库里根本没有它。所以下面没有 try/except 兜底，抛出去就是抛出去。

      store.finish() 提交的是整个连接，而 insert_run 那行也在这个连接上，所以收尾是
      store 收尾之后的第二个事务，要自己提交。

      那把 advisory lock 是事务级的，所以 store.finish() 提交的那一刻它就放了。这是
      对的：要防的四个撞点全在 run_discovery 里面，它提交的时候已经全过去了。

    这个函数对库里做的全部改动，按发生顺序（除了最后一张，其余都在 run_discovery 里面）：

        runs                  开头 insert 一行 status='running'（run_type / run_date /
                              thread_id），结尾 update 成 completed / partial / failed
        query_executions      每条 query 一行。真开始采那一刻插 running，采完回填上面
                              那十来个计数；失败的那条回填半截计数加错误原文
        signals               过了闸门留下的每条帖子一行，挂在这一跑的 run_id 上
        leads                 头一回见到的公司 insert 一行；已经有的 update（打分七列、
                              status、cooldown_until、strikes、last_analyzed_at）
        lead_evidence         每条被引用的原文一行
        lead_discovery_hits   (query_id, lead_id, run_id) 每条命中一行。「哪条 query 找到
                              这家公司」的账本，approval yield 的分母靠它
        queries.last_run_at   只给这一跑真跑成功的那几条写，用 greatest 防倒退。
                              采集挂了的那条不写 —— 写了它会白吃掉一整个 cadence

    persist=False 时上面这些连同 runs 那一行全部回滚，库里干干净净，但采集的钱照花、
    原始 dataset 照样落在 data/。
    """
    # ① 先问一句：整个库同一时刻只允许跑一个 discovery。拿不到就当场退，不排队
    #
    # 拿到锁之后，我们八张业务表里一行都没变 —— 这正是 advisory lock 的意思，它跟任何
    # 数据都无关、也不落表，只是在 Postgres 内存的锁表里多一条「20260902 这个号被这个
    # 事务占着」的登记，事务一结束（提交、回滚、进程被 kill、连接断掉）那条登记自动消失。
    #
    # 走一遍实际的：
    #
    #     07:00  定时那跑执行这一句，返回 true。锁表上多一条 advisory 20260902 的登记，
    #            业务表一行没动。它接着往下走，insert_run 插出 run 36
    #     07:02  用户在对话里说「跑」，interactive 这跑执行同一句，返回 false
    #            （try_ 的意思是拿不到就立刻返回，不等）。下面 insert_run 那句根本没执行
    #            -> 抛 DiscoveryBusy -> run_harvest 抛出去 -> 库里一个字没写、一分钱没花
    #     07:09  定时那跑 store.finish() 提交，事务结束，锁表那条登记自动没了
    #     07:10  用户再说一次「跑」，这一句返回 true，正常往下走，insert_run 插出 run 37
    #
    # 所以「拿到锁」本身没有任何可见结果，它的全部作用是让第二个进来的人拿不到。挡的是
    # upsert_lead 那个 select-then-insert 不原子：两跑同时发现同一家新公司时，第二个撞上
    # leads 的唯一索引抛出来，而我们整跑一个事务 —— 抛了就是整跑回滚，采集的钱白花。
    if not try_lock_discovery(cur):
        raise DiscoveryBusy("已经有一跑 discovery 在跑，这次没开始")

    # ② 开一次 run，先记 running
    # 输入   run_type、今天是哪天、哪条对话
    # 输出   run_id。这一跑的 signals 和 query_executions 都挂在它上面
    run_id = discovery_db.insert_run(
        cur, run_type=run_type, run_date=now.date().isoformat(), thread_id=thread_id
    )
    store = PostgresDiscoveryStore(conn, run_id=run_id)
    print(f"[harvest] run_id={run_id}  run_type={run_type}  persist={persist}")

    # ③ 干活。采集、闸门、归并、打分全在 pipeline 那边，整跑一个事务
    #          原始 dataset 一个 run 一个文件写进 data/，--no-persist 也照写：
    #          钱已经花了，库里回滚跟数据要不要留是两件事
    result = run_discovery(
        queries,
        adapter=build_adapter(
            replay,
            dump_path=dump_path_for(run_id, now),
            # 没指定就用配置里那个默认。这一句是「空串等于没指定」这条
            # 口径的唯一实现，两条路都走它。
            posted_within=posted_within or configs.run.posted_within,
            posted_since=posted_since,
        ),
        store=store,
        llm_client=build_llm_client(),
        rules=configs.rules,
        facts=configs.facts,
        scoring=configs.scoring,
        cooldown=configs.cooldown,
        max_posts_per_query=configs.run.max_posts_per_query,
        seen_days=configs.run.seen_days,
        now=now,
        skip_cooldown=skip_cooldown,
        persist=persist,
    )

    # ④ 收尾 runs 那一行
    if persist:
        # store.finish() 已经把这一跑提交了，包括 ② 插的那行 runs。
        # 所以这里是新的一个事务，要自己提交。
        discovery_db.finish_run(cur, run_id, status=str(result.status))
        conn.commit()
        print(f"[harvest] run {run_id} 收尾为 {result.status}，已提交")
    else:
        # store.finish(commit=False) 已经回滚，runs 那行也不存在了，
        # 再去 update 它会改到 0 行然后抛。
        print(f"[harvest] --no-persist，整跑已回滚，runs 那行也没留（曾用 id={run_id}）")

    return run_id, result
