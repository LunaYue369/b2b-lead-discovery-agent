"""lead_feedback 表 —— Riley 对 lead 的态度。

append-only，同一个 lead 允许多行，取最新一行算当前状态。
真实会发生的情况：Riley approve 了，改稿改到一半发现这家其实不该联系
（"他们那个众筹项目失败了"）。那是一条延迟的负反馈，
应该新写一行 reject，不是去 update 原来那条 approve。
设计成 1 lead 1 行，这个信号就丢了 —— 而「看起来对但实际不对」
正是 analyzer 最需要学的一类。

这张表只装 lead 级 feedback（这条线索值不值得联系 -> 归因到 query）。
改稿意见是 draft 级（话术写得好不好 -> 归因到 draft prompt），
等写 draft agent 时另开 draft_revisions 表。混在一起，
memory processor 算 query 的 approval yield 时会把改稿意见算进分母，
而那个指标决定每天 60% 的预算。

写入的三个入口，author 那一列就是用来分辨它们的：

    Slack 按钮      approve / skip / reject，author 是 Slack user id
    ignored 扫描    ignored，author 是 'system'
    以后的 interactive  可能不带 run_id（那不是从卡片来的）

run_id 记的是「这一下点的是哪一跑推的那张卡」。归因要靠它：哪条 query
找到这家公司记在 lead_discovery_hits (query_id, lead_id, run_id) 上，
键里有 run_id。不带的话，一条 approve 会算给历来所有找到过这家公司的
query，包括三周前那次推送之后被忽略掉的那条。
按钮里刻意不塞 query_id：一条 lead 可能被同一跑里两条 query 同时命中
（那正是 corroboration 的分子），塞一个等于任选一条、静默错归因。
"""

from __future__ import annotations

import datetime as dt

import psycopg

from models import FeedbackType, LeadStatus

# ignored 那些行的 author。不是人，所以不写 Slack user id。
SYSTEM_AUTHOR = "system"


def record_feedback(
    cur: psycopg.Cursor,
    lead_id: int,
    *,
    feedback_type: FeedbackType,
    author: str,
    run_id: int | None = None,
    note: str | None = None,
) -> int:
    """写一行 feedback。只插不改。

    输入：游标；哪条 lead；哪种态度；谁写的；这一下点的是哪一跑推的卡
          （不是从卡片来的就不传）；理由（只有 reject 会有）
    输出：新插入那行的 id

    这个函数不碰 leads 表。approve 要顺手把 lead 推进 in_draft，那是
    另一个函数（advance_to_draft），两件事分开的理由见那个函数。

    四种情况：

    一、Riley 点「要联系」

        输入   lead_id=1281  feedback_type=APPROVE  author='U04ABCD'
               run_id=27  note=None
        库里   lead_feedback 多一行
               (lead_id=1281, feedback_type='approve', author='U04ABCD',
                run_id=27, note=null, created_at=now())
        输出   那行的 id，比如 41
        后果   调用方紧接着调 advance_to_draft，discovery 从此不再捞它

    二、Riley 点「不该联系」并在 modal 里写了理由

        输入   lead_id=1283  feedback_type=REJECT  author='U04ABCD'
               run_id=27  note='这家是做众筹营销的代理，不做硬件'
        库里   多一行，note 有值
        输出   那行的 id
        后果   note 是最值钱的一种数据 —— 「品类不对」和「这家太大」
               指向完全不同的修法，一个改 query 一个改打分权重，
               而这个区别只有她说得出来

    三、同一条 lead 第二次写反馈（她 approve 之后反悔）

        输入   lead_id=1281  feedback_type=REJECT  author='U04ABCD'
               run_id=27  note='他们那个众筹失败了'
        库里   还是多一行，第一行那条 approve 原样留着
        输出   新那行的 id
        后果   两行都在。current_feedback 取最新一行，所以当前态度是
               reject；而「她当时是 approve 的」这件事没丢 ——
               那正是 analyzer 最该学的一类样本

    四、ignored 扫描写的

        输入   lead_id=1290  feedback_type=IGNORED  author='system'
               run_id=27  note=None
        输出   那行的 id
        后果   一般不直接这么调，走 sweep_ignored 批量写

    这个函数不检查「这条 lead 是不是真的推给她过」。原因是三个入口里有
    一个（以后的 interactive）本来就不是从卡片来的，而在这里加一道
    「必须先 pushed」的闸门，会让那条路要么绕过这个函数、要么伪造一个
    pushed_at。表是 append-only 的账本，账本不该拒绝记账。
    """
    cur.execute(
        """
        insert into lead_feedback (lead_id, feedback_type, note, author, run_id)
        values (%s, %s, %s, %s, %s)
        returning id
        """,
        (lead_id, str(feedback_type), note, author, run_id),
    )
    row = cur.fetchone()
    if row is None:
        # returning 一定有一行，除非 insert 根本没执行。
        # 静默返回 0 会让调用方拿着一个假 id 往下走。
        raise RuntimeError(f"insert 没有返回 id，lead_id={lead_id}")
    return int(row[0])


def advance_to_draft(cur: psycopg.Cursor, lead_id: int) -> None:
    """approve 之后把 lead 推进 in_draft。

    输入：游标；哪条 lead
    输出：无

    这一步现在只做一件事：改 status。它挡住的是「重新分析之后 status 被
    打回 new，几天后又出现在给 Riley 的卡片里、而她已经在写稿」——
    save_assessment 里那句 case when status='in_draft' 就是为它写的。

    它挡不住「重新分析」本身。lead_states 的分流只看两件事：是不是
    excluded、冷却期过没过。in_draft 不在里面，所以冷却期一过这家公司
    照样会被送去分析、照样花一次 LLM 的钱。要真正不捞它，得给
    TriageVerdict 加第四档，而 query_executions 有一条 CHECK 焊着
    「三堆分流之和 = 公司数」，第四档算进哪一堆要拍一次 + 一条 migration。
    那件事等 Draft Agent 那期再定，现在触发不了（Draft Agent 不做，
    没有别的路径会把 lead 推进 in_draft）。

    跟 record_feedback 分成两个函数，不是为了「职责单一」这种说法，
    是因为它们的失败含义不同：feedback 那一行是账本，写进去就不该回退；
    而状态推进是可以重试的。合成一个函数的话，状态那句失败会连带把
    账本那一行回滚掉 —— 而 Riley 已经点过按钮了，那次点击不该消失。
    调用方把两个放在同一个事务里，顺序是先记账后改状态。

    三种情况：

    一、正常情况

        库里   leads.status = 'pushed'，cooldown_until 是 30 天后
        输入   lead_id=1281
        库里   status -> 'in_draft'，cooldown_until 原样不动
        输出   无

    二、这条 lead 已经在 in_draft 了（她点了两次「要联系」）

        库里   status 已经是 'in_draft'
        输入   lead_id=1281
        库里   不变（where 里排掉了 in_draft）
        输出   无，不抛。重复点按钮是很正常的操作，
               而 Slack 的按钮回调本身也可能重投

    三、lead_id 根本不存在

        输入   lead_id=999999
        输出   抛 RuntimeError。这不是「她点了两次」，是 payload 里的
               lead_id 对不上库 —— 那说明卡片和库不是同一套数据，
               静默放过会让后面的调查无从下手

    刻意不动 cooldown_until。这里踩过一次：原先写的是「清空它」，理由是
    「in_draft 的公司不该再被分析，留个未来的时刻没意义」—— 那句推理
    反了。lead_states 判的是「cooldown_until 不为空且大于现在」，清空
    之后这家公司立刻变成可分析，**明天那一跑就会花一次 LLM 的钱重新
    分析它**，正好跟想要的效果相反。
    留着不动的话，save_assessment 在推它之前写的那个未来时刻（过线那档
    是 30 天）本身就是保护 —— 那段时间里它不会被再分析，而 30 天足够
    Riley 把稿子写完。

    status 用 in 而不是 = 'pushed'：真跑里她可能在卡片发出去之前就看到
    了（interactive 那条路），那时候 status 还是 new。列出允许的前态而
    不是允许一切，是为了让 excluded 那种终态不会被一次点击复活。
    """
    cur.execute(
        """
        update leads
           set status = %s,
               updated_at = now()
         where id = %s
           and status in (%s, %s)
        """,
        (
            str(LeadStatus.IN_DRAFT),
            lead_id,
            str(LeadStatus.NEW),
            str(LeadStatus.PUSHED),
        ),
    )
    if cur.rowcount == 1:
        return

    # 改到 0 行有两种可能，要分开：已经是 in_draft（正常，重复点击）
    # 和这条 lead 不存在或处在别的状态（不正常）。
    cur.execute("select status from leads where id = %s", (lead_id,))
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"leads 里没有 id={lead_id}，卡片和库对不上")
    if row[0] != str(LeadStatus.IN_DRAFT):
        raise RuntimeError(
            f"lead {lead_id} 的状态是 {row[0]}，不能推进到 in_draft"
            f"（只允许从 new 或 pushed 推进）"
        )


def current_feedback(cur: psycopg.Cursor, lead_id: int) -> FeedbackType | None:
    """这条 lead 现在的态度，取最新一行。

    输入：游标；哪条 lead
    输出：一个 FeedbackType，一行都没有就是 None

    三种情况：

    一、有过反馈

        库里   两行：先 approve（09:00）后 reject（11:00）
        输入   lead_id=1281
        输出   FeedbackType.REJECT

    二、一行都没有

        输入   lead_id=1290
        输出   None
        后果   None 和 IGNORED 不是一回事：None 是「还在等她」，
               IGNORED 是「等超时了，已经记成弱负信号」。
               sweep_ignored 靠这个区别决定扫谁

    三、同一秒里两行（按钮连点两下）

        输出   id 大的那一行。order by 里 created_at 之后跟着 id，
               不兜的话同一时刻的两行顺序随机，而「她最后点的是哪个」
               会在两次查询之间变
    """
    cur.execute(
        """
        select feedback_type
          from lead_feedback
         where lead_id = %s
         order by created_at desc, id desc
         limit 1
        """,
        (lead_id,),
    )
    row = cur.fetchone()
    return FeedbackType(row[0]) if row else None


def sweep_ignored(
    cur: psycopg.Cursor, *, now: dt.datetime, after_days: int, run_id: int | None = None
) -> list[int]:
    """把推出去太久还没人动的卡片记成 ignored。

    输入：游标；现在几点（带时区）；推出去几天算超时；只扫某一跑
          （不传就扫全部）
    输出：这次被扫成 ignored 的 lead_id 列表，按 id 排序

    为什么必须有这个函数：Riley 大部分卡片是不回应的。只记 approve 和
    reject，approval yield 的分母就只剩「她动过的那些」，而那个比率会
    虚高得离谱 —— 一条 query 推了 50 条她只点了 2 条 approve，
    真实 yield 是 4%，不记 ignored 算出来是 100%。而这个指标决定每天
    60% 的采集预算怎么分。沉默是弱负信号，不是没有信号。

    四种情况：

    一、正常扫一批

        库里   lead 1290 pushed_at=8-20，lead 1291 pushed_at=8-21，
               两条都没有任何 feedback 行
        输入   now=2026-09-02  after_days=7
               找 pushed_at 早于 8-26 且一行 feedback 都没有的
        库里   lead_feedback 多两行，author='system'，note=null，
               run_id 各自取 leads.pushed_run_id（推那张卡的那一跑）
        输出   [1290, 1291]

    二、这次推送之后已经有反馈了（她点过，或者上次已经扫过）

        库里   lead 1281 在 pushed_at 之后有一行 approve；
               lead 1284 在 pushed_at 之后有一行 ignored
        输出   两条都不在返回值里，也不会新写行
        后果   幂等 —— 每天扫一次不会给同一条重复写 ignored。
               这条很重要：重复写会让同一条 lead 在分母里被数好几次

    五、同一条 lead 第二轮又被推、又被忽略

        库里   lead 1290 三个月前推过、被扫成 ignored（那行 created_at
               是三个月前），冷却期过后重新分析又过线，昨天再推了一次，
               pushed_at 已经更新成昨天
        输入   now 是九天后  after_days=7
        库里   再写一行 ignored，run_id 是第二次推送那一跑
        输出   [1290]
        后果   这一条是「not exists 要不要看时间」的分水岭。不看时间的话
               第二轮的沉默永远扫不出来（库里已经有一行 ignored 了），
               而第二轮那几条 query 的分母就少算了一条 —— 报表上完全
               看不出来。所以那个 not exists 比的是 created_at > pushed_at：
               问的是「自从这次把卡片推给她之后，她有没有表过态」

    三、还没到时间

        库里   lead 1292 pushed_at=9-01
        输入   now=2026-09-02  after_days=7
        输出   []，不写任何行

    四、从来没推给她过（pushed_at 是空的）

        库里   lead 1293 status='new'，pushed_at=null
        输出   不在返回值里
        后果   没推给她的东西谈不上她忽略了。这一条是这个函数最容易
               写错的地方：不筛 pushed_at 的话，C 档那些从来没上卡片的
               公司会被大批扫成 ignored，而它们会进 approval yield 的
               分母 —— 那个比率就彻底没意义了

    一条 SQL 干完（insert ... select），不是先查再逐条写。因为这两步之间
    Riley 完全可能正好点了按钮：先查出 20 条、她在这中间 approve 了一条、
    再逐条写就会给那条既写 approve 又写 ignored。一条语句里做，
    「有没有 feedback」和「写进去」在同一个快照上。

    写进 ignored 那一行的 run_id 取 leads.pushed_run_id，也就是「推这张卡
    的那一跑」。不拿 first_seen_run_id 顶：一家公司可能头一回进库那天分数
    不够没上卡片，冷却期过了重新分析涨过线才被推，两个 run 不是一回事。
    拿 first_seen 归因等于把一条 ignored 算给一条当天没推过这家公司的
    query。pushed_run_id 是空的话这一行的 run_id 就留空 —— 归因时会被
    join 掉、看得见，而填一个看着合理的值是看不见的错。

    run_id 那个参数是给「只扫某一跑」用的（比如补扫某天漏掉的）。
    默认不传就是按时间扫全部，那是每天的定时任务要的。
    """
    cur.execute(
        """
        insert into lead_feedback (lead_id, feedback_type, note, author, run_id)
        select l.id, %s, null, %s, l.pushed_run_id
          from leads l
         where l.pushed_at is not null
           and l.pushed_at < %s - make_interval(days => %s)
           and (%s::bigint is null or l.pushed_run_id = %s)
           and not exists (
               select 1
                 from lead_feedback f
                where f.lead_id = l.id
                  and f.created_at > l.pushed_at
           )
        returning lead_id
        """,
        (
            str(FeedbackType.IGNORED),
            SYSTEM_AUTHOR,
            now,
            after_days,
            run_id,
            run_id,
        ),
    )
    return sorted(int(row[0]) for row in cur.fetchall())
