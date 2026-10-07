"""每天 07:00 PT 的定时 discovery —— systemd 的入口，不是 agent。

    python -m triggers.scheduled_discovery                  真跑，会落库
    python -m triggers.scheduled_discovery --plan-only       只看今天会跑哪几条
    python -m triggers.scheduled_discovery --replay FILE     回放存下来的采集
    python -m triggers.scheduled_discovery --no-persist      整跑最后回滚
    python -m triggers.scheduled_discovery --no-slack        照常落库但不推 Slack

刻意不用 LangGraph、不挂起、不等人。三个理由：
  1. 挂起等人 -> 今天没看，明天那跑怎么办？两个悬着的 run 状态想不清
  2. 采集失败重跑很常见（超时、限流），不挂起的流程可以整跑重来
  3. 审批粒度本来就是每条线索，不是每天一批

这个模块里没有业务判断。它只做五件事，顺序固定：

    ① 读配置              discovery.yaml / filters.yaml / scoring.yaml / facts.json
    ② 今天跑哪几条 query  pickable_queries -> stats.compute -> select_queries
    ③④⑤ 开 run、干活、收尾   harvest_once(...)  <- 跟 interactive 共用的那一段
    ⑥ 找联系方式         find_contacts_for_run(...)  <- 跟 interactive 共用，在 triggers/contacts.py
    ⑦ 推 Slack 卡片       push_digest(...)  <- 也跟 interactive 共用，在 triggers/digest.py

③④⑤ 那三步连同它们的顺序纪律在 triggers/harvest.py 里，两条路共用一份。
那个函数里还有一把 advisory lock：scheduled 跟 interactive 撞在一起时，
后到的那个当场退，不排队。这条路退了就是今天这一跑没了，所以它返回非零，
让 systemd 在 journal 里记一笔。

结果同时印在标准输出里，systemd 会把它收进 journal。

真跑的时候，每条 query 采回来的原始 dataset 会原样存一份到 data/
（gzip，一个 run 一个文件）。两个理由：钱花出去买回来的东西要在自己
手上，以及库里存的是闸门之后的结果，拿不回来被砍掉的那批 ——
而那批恰恰是以后验「这条规则有没有误砍真买方」要用的样本。

这个文件里剩下的那处非对称（另外两处跟着 ③④⑤ 搬去 harvest.py 了）：

  ⑦ 必须在 ⑤ 提交之后，顺序不能反：卡片发出去就收不回来，而库那边还能
  回滚。先发后提交的话，事务万一回滚，Riley 手上就有一张指向不存在的
  lead 的卡片，她点按钮会写到一个不存在的 lead_id 上。--no-persist 那条路
  ⑥ ⑦ 整段跳过，理由同上但更硬 —— 那一跑的 leads 已经被回滚掉了。
  ⑥ 在 ⑦ 前面：卡片拼的时候从库里读 ⑥ 写进去的联系方式。

trigger 是「时钟这个入口」，interactive 是「人这个入口」，中间那段
（开 run、采集、打分、落库、收尾）是同一个函数，在 triggers/harvest.py。

补跑一轮（原设计的第 ④ 步「数量不够就再来一轮」）刻意还没做：它的判据是
「今天的数量不达标」，而那个数跟 A/B/C 阈值是同一个还没定的数（要 Riley 标
20-50 条反推）。现在拍一个下限，跟「配额是天花板不是地板、不够就直说今天
只有两条」直接冲突。等阈值定了再加，加的位置就是这里、不在 pipeline。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from models import DiscoveryResult, RunStatus
from persistence import db, discovery_db, query_db
from query_intelligence import stats
from query_intelligence.selector import (
    SelectionPlan,
    load_selection_config,
    select_queries,
)
from triggers.contacts import find_contacts_for_run
from triggers.digest import push_digest
from triggers.harvest import (
    DiscoveryBusy,
    RunConfig,
    harvest_once,
    load_harvest_configs,
)


def report_plan(plan: SelectionPlan) -> None:
    """把「今天为什么跑这几条」印出来。

    输入：selector 的组队结果
    输出：无（印到标准输出，systemd 收进 journal）

    印的是三个池子各要几条、实际拿到几条，而不只是最后选中的清单：
    「今天只跑了一条」有好几种完全不同的原因（额度本来就是 1 条 / proven
    池空了没人接得住那 60% / 到期的 query 根本不够），而清单本身
    一个都回答不了。

    四种情况，第一种是 2026-09-02 那次回放跑的真实输出：

    一、池子空了，额度让渡（今天的实际形状：三条种子全是 candidate）

        输入   plan.per_pool = (PoolOutcome(PINNED,    target=1, available=0,
                                            chosen=0, received=0),
                                PoolOutcome(PROVEN,    target=1, available=0,
                                            chosen=0, received=0),
                                PoolOutcome(CANDIDATE, target=0, available=3,
                                            chosen=2, received=2))
               plan.chosen = (id=29 candidate 7天, id=30 candidate 1天)
               plan.unmet_budget = 0   plan.warnings = ()
        印出来
               今天跑哪几条 query
                 pinned     目标 1 条，可跑 0 条，选中 0 条，让出 1 条（这一档没那么多到期的）
                 proven     目标 1 条，可跑 0 条，选中 0 条，让出 1 条（这一档没那么多到期的）
                 candidate  目标 0 条，可跑 3 条，选中 2 条（其中 2 条是别的池子让出来的）
                 -> id=29 [candidate] cadence=7天  looking for a hardware manufacturing partner
                 -> id=30 [candidate] cadence=1天  Kickstarter AND (microphone OR audio ...)

               读法：candidate 的目标是 0 条（预算 2 x 20% 取整），但另外两档
               都空着、把 2 条全让给了它。没有让渡的话今天会跑 0 条 query，
               而报表看起来完全正常（「今天就是没轮到」）

    二、三个池子都有货，按 20/60/20 分（预算调到 10 之后的样子）

        输入   PoolOutcome(PINNED, target=2, available=2, chosen=2, received=0)
               PoolOutcome(PROVEN, target=6, available=9, chosen=6, received=0)
               PoolOutcome(CANDIDATE, target=2, available=5, chosen=2, received=0)
        印出来
                 pinned     目标 2 条，可跑 2 条，选中 2 条
                 proven     目标 6 条，可跑 9 条，选中 6 条
                 candidate  目标 2 条，可跑 5 条，选中 2 条
               然后十行 ->，没有让出也没有接收，三行都只有前半句

    三、额度用不完（到期的 query 加起来比预算还少）

        输入   三个池子 chosen 合计 3，预算 10，plan.unmet_budget = 7
        印出来 三行池子账，然后
                 额度没用完 7 条 —— 到期的 query 不够，不是配置错了
               这一句必须印：不印的话「今天只跑了 3 条」看起来像配置写错了，
               而真相是没有更多到期的 query，那是正常的一天

    四、proven 池里有 query 却查不到指标（记账不一致）

        输入   plan.warnings = ('proven query 42 没有历史指标，当 0 分排最后',)
        印出来 三行池子账，然后
                 [warn] proven query 42 没有历史指标，当 0 分排最后
               它不会让整跑挂掉（7 点的定时任务不该因为记账不一致今天颗粒
               无收），但也绝不静默 —— 一条 proven 却没有指标说明
               query_executions 那边漏了账，要有人去看
    """
    print("\n今天跑哪几条 query")
    for outcome in plan.per_pool:
        line = (
            f"  {outcome.pool:<10} 目标 {outcome.target} 条，可跑 {outcome.available} 条，"
            f"选中 {outcome.chosen} 条"
        )
        if outcome.received:
            line += f"（其中 {outcome.received} 条是别的池子让出来的）"
        if outcome.shortfall:
            line += f"，让出 {outcome.shortfall} 条（这一档没那么多到期的）"
        print(line)
    if plan.unmet_budget:
        print(f"  额度没用完 {plan.unmet_budget} 条 —— 到期的 query 不够，不是配置错了")
    for warning in plan.warnings:
        print(f"  [warn] {warning}")
    for spec in plan.chosen:
        print(f"  -> id={spec.query_id} [{spec.pool}] cadence={spec.cadence_days}天  {spec.text}")


def report_result(result: DiscoveryResult, *, run_config: RunConfig, qualified_score: int) -> None:
    """把这一跑的漏斗和线索印出来。

    输入：run_discovery 的返回值（DiscoveryResult）；这一跑的配置
          （只用它的 review_score，划「够得着但没过线」那一档）；过线分
          （从 cooldown 那份配置来，全项目只有那一个地方定义「过线」）
    输出：无（印到标准输出，systemd 收进 journal）

    印四段，顺序固定：每条 query 一行账 → 一行汇总 → 按分数从高到低列公司
    → 丢弃和告警。分数线附近那一档单独数出来：推给 Riley 的要严
    （>= qualified_score），给她标注的要有量（review_score 到 qualified_score
    之间），否则阈值永远校准不了 —— 她标不到边界样本，阈值就没有反推的原料。

    一句「今天只有 2 条」是合格的输出，不是故障 —— 配额是天花板不是地板。

    五种情况，第一种是一次回放跑的真实输出（数字会随每跑变，看形状就行）：

    一、正常一跑，有过线的

        输入   result.status=completed，per_query 两条都 completed，
               scored 十五家，discarded 空，warnings 空
        输出

            run 25  状态 completed
              query 29 [completed]  采到 40 条 / 计费 40  砍 38+1  公司 3
                （分析 3 / 冷却 0 / 终态 0）  头一回见到 3  过线 0
                各条规则砍量：company_page_author=1  no_relevance_signal=38
              query 30 [completed]  采到 31 条 / 计费 31  砍 12+6  公司 12
                （分析 12 / 冷却 0 / 终态 0）  头一回见到 12  过线 2

            公司 15 家，打了分的 15 家：过线（>= 60）2 家 · 边界（40-60）1 家
              · 其余 12 家
               66  过线  lead=1252  name:chatterly  icp=8 signal=8 senior=7 moq=8
               45  边界  lead=1253  name:brevitē  icp=5 signal=6 senior=6 moq=5
               17        lead=1254  li_id:1753   icp=1 signal=1 senior=0 moq=0

               query 那一行是一条完整的漏斗，从左到右六个数各回答一个问题：
               采回来几条 / 供应商按几条计费（含评论那一半，跟采到的数不一定
               相等）/ 两级闸门各砍了多少 / 归并成几家公司 / 这几家怎么分流的
               / 其中几家是这条 query 头一回发现的 / 几家过线。
               「头一回见到」和「过线」是评价这条 query 的那两个数，别的都是过程。

               公司那几行按分数降序，同时印四个 LLM 维度：分数是七维加权的结果，
               只印总分的话「为什么这家 45」要去翻库。lead= 是 leads 表的主键，
               照着它能直接查这家的历史；--no-persist 那跑的 id 会被回滚掉，
               所以那些号码只在当次输出里有意义。

    二、有一条 query 采集失败（Apify 超时、限流、actor 报错）

        输入   那条 outcome.status=failed 且 outcome.error 有值，
               result.status=partial
        输出   那条 query 的行后面多一行

               query 31 [failed]  采到 0 条 / 计费 0  砍 0+0  公司 0 ...
                 采集失败：Apify run 超时（900 秒）

               计数全是 0 但那一行照样印出来：库里有一行 failed 的执行记录，
               「这条 query 今天试过但没成」和「今天没轮到它」在报表上必须
               分得开。别的 query 照跑，这是一条 query 一个 Apify run 换来的。

    三、一家都没打上分（采到了，但两级闸门全砍光，或者归并出来的公司都在冷却期）

        输入   result.scored 是空字典，company_count 可能是 0 也可能不是
        输出   汇总那一行三个计数全是 0，公司清单一行都不印

            公司 0 家，打了分的 0 家：过线（>= 60）0 家 · 边界（40-60）0 家
              · 其余 0 家

               这是合格的输出不是故障。要区分「真的没有」和「配置写错了」，
               看上面 query 那一行的砍量：no_relevance_signal 独占全部砍量
               通常是这条 query 没有布尔符、被 LinkedIn 当语义匹配放宽了。

    四、有反复答不好被丢弃的公司

        输入   result.discarded 非空（键是 company_key，值是模型编的那几句引用）
        输出   最后多一段

            反复答不好被丢弃的 2 家（引用是编的，没给 Riley 看）：
              name:bvf：['Helping Brands Launch & Scale']

               只印前两句：这一段是给排查用的线头，不是完整证据。
               这里出现的公司是花了钱、重问过、最后还是没给 Riley 看的，
               所以它非空就值得看一眼 —— 真编造和「闸门取材范围漏了一个字段」
               在这段输出里长得一模一样，分辨要去看那几句话在原始数据的哪里。

    五、有告警

        输入   result.warnings 非空
        输出   最后逐行印 [warn] ...

               来源有两处：proven 池里的 query 却算不出指标（当 0 分排最后）、
               跑内两个簇撞进库里同一行（其中一个让掉了，没重复花钱）。
               刻意只告警不抛：07:00 的定时任务不该因为记账不一致今天颗粒无收。
    """
    print(f"\nrun {result.run_id}  状态 {result.status}")
    for outcome in result.per_query:
        counters = outcome.counters
        print(
            f"  query {outcome.query_id} [{outcome.status}]"
            f"  采到 {counters.raw_result_count} 条 / 计费 {counters.billed_item_count}"
            f"  砍 {counters.signal_dropped_count}+{counters.candidate_dropped_count}"
            f"  公司 {counters.unique_company_count}"
            f"（分析 {counters.researchable_lead_count} / 冷却 {counters.cooldown_skipped_count}"
            f" / 终态 {counters.excluded_skipped_count}）"
            f"  头一回见到 {counters.novel_lead_count}  过线 {counters.qualified_lead_count}"
        )
        if outcome.error:
            print(f"    采集失败：{outcome.error}")
        if counters.drop_reasons:
            reasons = "  ".join(f"{k}={v}" for k, v in sorted(counters.drop_reasons.items()))
            print(f"    各条规则砍量：{reasons}")

    ranked = sorted(result.scored.items(), key=lambda item: -item[1].score)
    over = [key for key, a in ranked if a.score >= qualified_score]
    # 边界档除了分数在区间里，还要求品类不是完全不对。
    #
    # 这一档不是「快过线了」，它是攒给 Riley 标注的样本 —— 阈值要由她标
    # 20-50 条反推，所以给她标的必须都是「品类对、只是时机或者信号强弱
    # 还说不清」的那种。品类完全不对的公司在这里出现纯属浪费她的时间，
    # 而且库里 cooldown 已经把它们判成终态、永不复查了，两边说的话
    # 不能相反。
    #
    # 打分那边已经把这一档封顶到 review_score 以下，所以正常情况下
    # 这个条件一条都不会挡到。留着它是因为那个封顶值在另一份配置里，
    # 被人改大之后这里是第二道闸 —— 而这种错不报错，症状是 Riley
    # 的标注池里慢慢混进一批不相干的公司。
    border = [
        key
        for key, a in ranked
        if run_config.review_score <= a.score < qualified_score and not a.llm.off_category
    ]
    print(
        f"\n公司 {result.company_count} 家，打了分的 {len(result.scored)} 家："
        f"过线（>= {qualified_score}）{len(over)} 家 · "
        f"边界（{run_config.review_score}-{qualified_score}）{len(border)} 家 · "
        f"其余 {len(result.scored) - len(over) - len(border)} 家"
    )
    for key, assessment in ranked:
        mark = "过线" if key in over else ("边界" if key in border else "    ")
        print(
            f"  {assessment.score:>3}  {mark}  lead={result.lead_ids.get(key)}  {key}"
            f"  icp={assessment.llm.icp_fit} signal={assessment.llm.signal_strength}"
            f" senior={assessment.llm.seniority} moq={assessment.llm.moq_fit}"
        )

    if result.discarded:
        print(f"\n反复答不好被丢弃的 {len(result.discarded)} 家（引用是编的，没给 Riley 看）：")
        for key, quotes in result.discarded.items():
            print(f"  {key}：{quotes[:2]}")
    for warning in result.warnings:
        print(f"[warn] {warning}")


def main(argv: list[str] | None = None) -> int:
    """跑一次定时 discovery。

    输入：命令行参数。argv=None 时从 sys.argv 取（systemd 那条路就是 None），
          传列表是给测试用的，比如 ['--plan-only']
    输出：进程退出码。0 是这一跑至少采成了一条 query（也含「今天没轮到
          任何 query」和 --plan-only 那两种什么都没做的）；1 只有一种
          情况 —— status=failed，选中的 query 一条都没采成

    退出码的分界是「systemd 要不要报警」。partial（有成有败）返回 0：
    今天的线索少了点，不是故障，失败那条 query 明天还到期、还会再跑。
    一条都没采成返回 1：那一跑照样有账可记（库里几行 failed 的执行记录），
    但连着几天全失败通常是 Apify 那边的账号、余额或者 actor 变了，
    要有人知道。第二段抛异常那种连退出码都轮不到：异常一路抛出去，
    systemd 收到的是堆栈。

    七种情况。前两种在花钱之前就返回，中间三种是三条正常的路，
    后两种是坏天气：

    一、只看今天跑哪几条（--plan-only）

        输入   ['--plan-only', ...]
               读完七份配置、连库、算完 plan，一分钱没花、一行没写
        输出   印计划然后 return 0。真实的一段：

               [trigger] 2026-09-02 19:21 UTC  到期的 query 3 条
               今天跑哪几条 query
                 pinned     目标 1 条，可跑 0 条，选中 0 条，让出 1 条
                 proven     目标 1 条，可跑 0 条，选中 0 条，让出 1 条
                 candidate  目标 0 条，可跑 3 条，选中 2 条
                   （其中 2 条是别的池子让出来的）
                 -> id=29 [candidate] cadence=7天  looking for a hardware ...
                 -> id=30 [candidate] cadence=1天  Kickstarter AND (...)

               额度让渡在这里看得最清楚：pinned 和 proven 今天都是空的，
               照 20/60/20 字面分配，candidate 的目标是 0 条 —— 不让渡的话
               今天一条 query 都不跑，而报表看起来完全正常。
        后果   不插 runs 行。这条路是每次改 selector 或者动 cadence 之后
               先看一眼的地方，因为它不花钱。

    二、今天没有到期的 query

        输入   []（正常真跑），但 pickable_queries 返回空、plan.chosen 也空
        输出   印「今天没有到期的 query，什么都没做」然后 return 0
        后果   刻意不插 runs 行：什么都没发生。「今天为什么没轮到」的答案在
               queries 表的 last_run_at 和 cadence 里，不在 runs 里 ——
               插一行空 run 会让 runs 表里堆着一批没有内容的行，
               而那张表以后要用来回答「哪几天出过线索」。

    三、真跑，全成（这是 systemd 每天走的那条）

        输入   []
               插一行 running 的 runs → 每条 query 起一个 Apify run 采集
               （今天选中 2 条 x 每条最多 40 条 post，约 0.15 美元）→
               归并、打分（LLM 几分钱）→ store.finish(commit=True) 提交整跑
        输出   印漏斗和公司清单，再印一行「run 25 收尾为 completed，已提交」，
               return 0
        后果   库里留下：runs 一行 completed · query_executions 每条 query
               一行、十列写满 · signals · leads 和 lead_evidence ·
               lead_discovery_hits · queries.last_run_at 往前走。
               从这一跑开始，「头一回见到」这个数才有意义 —— 空表起跑时
               它必然接近 100%。

    四、回放加不留数据（--replay FILE --no-persist）

        输入   ['--replay', 'data/harvest_2026-08-27_010047.json', '--no-persist']
               真 store、真库、真模型，只有采集是从文件里读的；
               整跑跑在一个事务里，最后 rollback
        输出   跟情况三一样的漏斗，末尾换成
               「--no-persist，整跑已回滚，runs 那行也没留（曾用 id=25）」，
               return 0
        后果   库里一行没留，连 ③ 插的那行 runs 也没了。所以 ⑤ 整段跳过 ——
               去 update 一行已经不存在的 runs 会改到 0 行然后抛。
               这两个开关合起来就是原计划里那个 verify_pipeline.py，
               改完 pipeline 拿它跑一遍，一分钱采集费不花。
               只给 --replay 不给 --no-persist 会被当场拦掉（理由见下面
               解析参数那一段）：回放的数据落库会污染 novel 指标。

    五、有成有败（partial）

        输入   []，但其中一条 query 的 Apify run 挂了
        输出   失败那条 query 印一行 error，收尾为 partial，return 0
        后果   只有成功那几条 query 的 last_run_at 往前走 —— 失败的那条
               明天还到期、还会再跑。失败那条的 signals 一行都不会落库
               （第一段只在最后一步才写库）。

    六、一条都没采成（failed）

        输入   []，两条 query 的 Apify run 全挂了
        输出   两行 failed 的账，收尾为 failed，return 1
        后果   照样提交：几行 failed 的执行记录是有价值的痕迹，
               而且零信号落库、没有污染。返回 1 是为了让 systemd 能报警 ——
               连着几天全失败通常是 Apify 那边的账号、余额或者 actor 变了。

    七、第二段抛异常（归并打分那半路炸了）

        输入   []，run_discovery 内部抛出来
        输出   不返回，异常一路抛到 sys.exit 外面，systemd 收到堆栈
        后果   pipeline 自己的外层守卫已经回滚掉整跑，连 ③ 插的那行 runs
               也没了。所以 report_result 和 ⑤ 都不会执行 —— 下面那两段
               在 run_discovery 之后没有任何兜底分支，就是这个原因。
               今天没有产出，明天重采一遍，采集的钱白花了一次。

    另外一个开关 --skip-cooldown 不改控制流，只是让所有公司都重新分析
    （包括已经判成终态的），会花很多 LLM 的钱，是排查用的。
    副作用写在 pipeline 那边：放行终态公司之后，这次 icp_fit 不为 0 的话
    status 会从 excluded 变回 new，等于翻了旧判决 —— 那是「人点名要看」
    应有的效果。
    """
    # prog 写死：python -m 跑一个包时 sys.argv[0] 是 __main__.py，不写的话 --help 第一行
    # 会印成 usage: __main__.py，照着敲是跑不起来的
    parser = argparse.ArgumentParser(
        prog="python -m triggers.scheduled_discovery", description="每天一次的 scheduled discovery"
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="只印今天会跑哪几条 query 就退出，不采集不花钱",
    )
    parser.add_argument(
        "--replay",
        metavar="FILE",
        help="用存下来的 Apify dataset JSON 回放采集，一分钱不花",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="整跑最后回滚（连 runs 那行一起），拿真库跑一遍但不留数据",
    )
    parser.add_argument(
        "--skip-cooldown",
        action="store_true",
        help="无视冷却期，所有公司都重新分析。会花很多 LLM 的钱，排查用",
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="照常采集、打分、落库，但不推 Slack。调打分的时候用，别打扰 Riley",
    )
    args = parser.parse_args(argv)

    # 回放必须同时给 --no-persist，不给就当场退出，不静默落库。
    #
    # 回放的数据是几周前采的那一批。它进了 leads 表之后，那些公司就永久算
    # 「已经见过」—— 明天真跑时它们全不算新发现，novel_lead_count 大面积
    # 归零，而那一列是评价 query 的核心、proven 靠它排名、proven 占 60% 的
    # 采集预算。也就是说：第一批评价数据是被污染的，且报表上完全看不出来。
    # 这跟当初决定「验证落库那一跑必须整跑回滚」是同一条理由。
    #
    # 写成硬拦不写成警告：这两个开关平时总是一起打，漏一个的后果又是
    # 不可逆的（要把那批 lead 从库里挑出来删，而它们跟真数据长得一样）。
    # 将来真需要拿回放数据填库（比如 step 6 调卡片样式），那时候再加一个
    # 显式的开关，别把它藏在「忘了带 --no-persist」这个动作里。
    if args.replay and not args.no_persist:
        parser.error(
            "--replay 要跟 --no-persist 一起用：回放的是几周前那批数据，"
            "落进 leads 表会让它们永久算「已经见过」，把 novel 指标污染掉"
        )

    # ── ① 读配置。全在最前面读完，缺键的话在花钱之前就炸 ──────────
    # 采集要用的那五份（外加它们之间那条跨文件的约束）在 load_harvest_configs 里一次
    # 读齐，跟 interactive 共用；这里只多读「今天跑哪几条」要用的两份。
    selection_config = load_selection_config()
    ranking_config = stats.load_ranking_config()
    configs = load_harvest_configs()

    # now 只取一次，整跑都用它：分头取的话 recency 和 cooldown 会差几分钟，
    # 而拿历史数据回归时那点差别会让两次跑不出同样的结果。
    now = dt.datetime.now(dt.UTC)
    persist = not args.no_persist

    with db.connect() as conn, conn.cursor() as cur:
        # ── ② 今天跑哪几条 ────────────────────────────────────────
        # 两个输入地位不同：pickable 是资格（不在里面，指标再好今天也不跑），
        # stats 只决定先后。所以 stats 缺项不是错 —— 从没跑过的 query
        # 本来就不在 query_executions 的聚合里。
        pickable = query_db.pickable_queries(cur, now=now)
        totals = discovery_db.execution_totals(cur)
        plan = select_queries(
            pickable, stats=stats.compute(totals, config=ranking_config), config=selection_config
        )
        print(f"[trigger] {now:%Y-%m-%d %H:%M} UTC  到期的 query {len(pickable)} 条")
        report_plan(plan)

        if args.plan_only:
            print("\n--plan-only，到这里就停了")
            return 0

        if not plan.chosen:
            # 不插 runs 行：什么都没发生。「今天为什么没轮到」的答案在
            # queries 表的 last_run_at 和 cadence 里，不在 runs 里。
            print("\n今天没有到期的 query，什么都没做")
            return 0

        # ── ③④⑤ 开 run、干活、收尾 ──────────────────────────────
        # 这三步连同它们的顺序纪律跟 interactive 共用同一个函数，不各写一份：
        # 它们管的是花钱和这一跑的账，两条路必须一个字不差。
        #
        # run_date 在那个函数里取 now 的 UTC 日期。07:00 PT 是 14:00 或 15:00 UTC，
        # 同一天，所以这个近似在正常班次下不会错位；真要按 PT 记账得先决定整个系统
        # 的时区口径，那件事还没定。
        #
        # thread_id 传 None 是一个判断不是省略：这一跑是时钟拉起来的，
        # 不属于任何一条对话。
        try:
            run_id, result = harvest_once(
                conn,
                cur,
                plan.chosen,
                run_type="scheduled",
                thread_id=None,
                now=now,
                configs=configs,
                replay=args.replay,
                persist=persist,
                skip_cooldown=args.skip_cooldown,
            )
        except DiscoveryBusy as exc:
            # 今天这一跑就没了。返回非零让 systemd 在 journal 里记一笔 ——
            # 静默跳过的话「今天怎么没线索」查不出来，而这条路一天只有一次机会。
            print()
            print(f"[trigger] {exc}。今天这一跑没有发生")
            return 1

        # 走到这里说明这一跑成立了（run_discovery 第二段抛异常的话不会返回，而且
        # store 已经把整跑回滚掉、runs 那行也没了 —— 那种情况下下面这段绝不能跑，
        # 所以它在 return 之后没有兜底分支）。
        report_result(
            result, run_config=configs.run, qualified_score=configs.cooldown.qualified_score
        )

        # ── ⑥ 找联系方式、⑦ 推 Slack 卡片 ─────────────────────────
        # 必须在 ⑤ 提交之后：卡片发出去就收不回来，而库那边还能回滚。
        # 反过来的话，事务万一回滚，Riley 手上就有一张指向不存在的 lead
        # 的卡片，她点按钮会写到一个不存在的 lead_id 上。
        #
        # --no-persist 那条路整段跳过，理由同上但更硬：那一跑的 leads
        # 已经被回滚掉了，推出去的每张卡片都指向不存在的行。
        # --no-slack 那条路也不找联系方式：找联系方式要花钱，而卡片不推的话这笔钱没人用得上
        if not persist:
            print("--no-persist，不推 Slack（那一跑的 leads 已经回滚，卡片会指向不存在的行）")
        elif args.no_slack:
            print("--no-slack，不推 Slack、不找联系方式。库里都写好了，线索在 leads 表里")
        else:
            # ⑥ 找联系方式。它自己提交（每次花钱马上记账），所以连接上不能有没提交的写 ——
            #    ⑤ 已经提交过了，这里是干净的。
            #    出错不往外抛、照样推卡：找联系方式是锦上添花，不能因为它今天的卡片推不出去。
            #    它自己已经接住了调 actor 出错、超月度上限两种，这里再接一层连库查库这类意外
            try:
                find_contacts_for_run(conn, run_id=run_id, now=now)
            except Exception as exc:
                conn.rollback()
                print(f"[contacts] 找联系方式出错，跳过，卡片照推：{type(exc).__name__}: {exc}")

            # ⑦ 推卡。卡片上的联系方式从库里读 ⑥ 刚写进去的
            push_digest(cur, run_id=run_id, now=now)
            # 记推送是 ⑤ 之后的又一个事务：runs 那行已经提交过了。
            conn.commit()

    return 1 if result.status is RunStatus.FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
