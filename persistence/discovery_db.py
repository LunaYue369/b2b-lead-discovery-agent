"""runs、query_executions、signals：一次 discovery 跑批自己的账。

这三张表记的是「这一跑发生了什么」，不是「发现了哪些公司」——
后者在 lead_db.py。分开是因为两边的生命周期不同：跑批的账写完就不再动，
公司那边会被后面每一跑反复更新。

写入顺序是固定的，外键指着上一层：

    runs -> query_executions
         -> signals -> (lead_db 的 leads -> lead_evidence)
                                         -> lead_discovery_hits

lead_discovery_hits 记的是「哪条 query 命中了哪家公司」，虽然指着 leads，
但它属于这一边：写一次就不再动，是 query 表现的账本，不是公司的现状。

runs 和 query_executions 都是「先插一行 running，跑完再收尾」两步走：

    insert_run       -> finish_run
    start_execution  -> finish_execution

两步不是啰嗦。一步写法（跑完了才插一行）有两个后果，而且都是安静的：
崩在半路的那一跑在库里什么都不留，「今天为什么只有两条线索」没处查；
started_at 会等于插入时刻而不是开始时刻，于是耗时永远是 0，
而那两列看起来填得好好的。

收尾前调 ExecutionCounters.check()。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

import psycopg

from models import DiscoveryHit, ExecutionCounters, ExecutionStatus, QueryTotals, RunStatus, Signal

# 「同一时刻只允许跑一个 discovery」这件事的锁号。这个数字本身没有含义，
# 它只需要满足两条：整个库里独一份、永远不改。advisory lock 的锁号是全库共享
# 的一个数字空间，改了它等于换了一把锁，老进程和新进程会各拿各的、互相拦不住。
_DISCOVERY_LOCK_KEY = 20260902


def try_lock_discovery(cur: psycopg.Cursor) -> bool:
    """问一句「现在可以跑 discovery 吗」。拿到返回 True，已经有人在跑返回 False。

    输入：游标。锁跟着这个游标所在的事务走
    输出：True 可以跑，False 现在有别人在跑

    advisory lock 是什么：它是一把跟任何数据都无关的锁，本质就是一个数字。
    我们要防的「同一时刻只跑一个 discovery」没有任何一行数据能代表它，所以
    普通的行锁表锁用不上 —— 没有那一行可锁。

    叫 advisory（建议性）是因为它是进程之间的君子协定：没拿锁的人硬要写库照样
    写得进去。所以它有效的前提是「所有会跑 discovery 的地方都记得先问这一句」，
    这也是这句话只写在这一个函数里、不许散到各个 trigger 里去的原因。

    用 _xact_（事务级）不用 session 级：进程被 kill -9、断电、Neon 把连接掐了，
    事务都会没，锁跟着一起放。session 级那个版本会在连接泄漏时留下一把谁也拿不到
    的锁，而症状是「从某天起 discovery 再也不跑了」，没有任何报错。

    try_ 那半截是「拿不到就立刻返回 False」，不是排队等。排队等在这里是错的：
    interactive 那条路上用户还在 Slack 那头看着，等几分钟不如当场说一句
    「现在有另一跑在采集」。

    要防的是什么（四个撞点里最硬的那个）：两跑同时发现同一家新公司时，
    upsert_lead 是 select-then-insert、不是原子的，第二个会撞上 leads 的唯一索引
    抛出来 —— 而我们整跑一个事务，抛了就是整跑回滚，那一跑的采集钱白花。

    代价是「同一时刻只允许一个 discovery」。我们一天跑两条 query，碰不到这个边。
    """
    cur.execute("select pg_try_advisory_xact_lock(%s)", (_DISCOVERY_LOCK_KEY,))
    return bool(cur.fetchone()[0])


def insert_run(cur: psycopg.Cursor, *, run_type: str, run_date: str, thread_id: str | None) -> int:
    """开一次 run，状态先记 running。

    输入：游标；run 类型（scheduled / interactive）；跑的是哪一天；
          是哪条对话让它跑的（scheduled 传 None，interactive 传 thread_id）
    输出：runs.id

    先插 running 再在结尾改成 completed，中间炸了库里留下的是一条
    running 的记录 —— 那正是我们想要的信息。等跑完再一次性插入的话，
    崩掉的那次在库里什么都不会留下，而「今天为什么没数据」就查不出来了。

    一个例子：

        输入   run_type='scheduled'  run_date='2026-08-27'
        库里   runs 多一行，status='running'，started_at 是现在，
               finished_at 和 error 都是空
        输出   那一行的 id。这一跑的 signals 和 query_executions 都挂在它上面

    同一天允许跑好几次，run_date 上没有唯一约束 —— 重试和补跑都是正常的。

    thread_id 没有默认值，两条路都得显式传：scheduled 传 None 是一个判断
    （「这一跑不属于任何对话」），漏传是一个疏忽，两者不该长得一样。
    """
    cur.execute(
        "insert into runs (run_type, status, run_date, thread_id)"
        " values (%s, 'running', %s, %s) returning id",
        (run_type, run_date, thread_id),
    )
    # fetchone() 拿回新建那一行，形如 (7,)，取 [0] 得到 run_id = 7。
    return cur.fetchone()[0]


def finish_run(cur: psycopg.Cursor, run_id: int, *, status: str, error: str | None = None) -> None:
    """给 run 收尾。

    输入：游标；哪次 run；最终状态；失败原因
    输出：无

    status 取 completed / failed / partial。partial 是「五条 query 里两条
    超时三条成功」——这个状态只在 run 这一层有意义，每条 execution
    自己要么成功要么失败。

    三种收尾：

        五条 query 全成      status='completed'  error=None
        全挂了               status='failed'     error='Apify 502'
        两条超时三条成功      status='partial'    error='2/5 query 超时'

    没被收尾的行会一直停在 running。那不是脏数据，是「这一跑崩在半路」
    留下的唯一痕迹，查「今天为什么没数据」就靠它。
    """
    cur.execute(
        "update runs set status = %s, finished_at = now(), error = %s where id = %s",
        (status, error, run_id),
    )


def run_status(cur: psycopg.Cursor, run_id: int) -> RunStatus:
    """查一跑现在是什么状态。

    输入：游标；哪一跑，如 31
    输出：RunStatus.COMPLETED / FAILED / PARTIAL / RUNNING 之一，如 RunStatus.COMPLETED

    推卡那一步要它：汇总消息在「采集全失败」时换一套说法。
    runs 里没有这一跑就抛 ValueError —— 推卡时 run_id 是几秒钟前刚建的那一行，
    查不到说明 run_id 传错了，这时候继续推会推出一条「这一跑没有过线的线索」，
    而那句话是假的
    """
    cur.execute("select status from runs where id = %s", (run_id,))
    # 输出   ('completed',)，没有这一跑是 None
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"runs 里没有第 {run_id} 跑")
    # 输入   'completed'
    # 输出   RunStatus.COMPLETED。库里的 CHECK 跟 RunStatus 是同一组四个值
    return RunStatus(row[0])


def interactive_billed_items(cur: psycopg.Cursor, *, since: datetime) -> int:
    """从 since 起，对话里说「跑」采回来的一共计费多少条。月度上限拿它算已花。

    输入：游标；账期的起点，带时区，如 2026-09-21T00:00Z
    输出：一个整数，如 615。这个账期一跑都没有是 0

    只算 run_type='interactive' 的跑：定时那一跑不归这道闸管。
    按 query_executions.started_at 切，不按 runs.started_at：两者差几秒，用哪个都行，
    用执行这一级是因为计费的数就记在这一级，少 join 一次。
    跑到一半崩溃、整跑回滚的那几跑不在这里 —— 那几行连同 runs 一起没了，
    而 Apify 已经扣了钱。所以这个数只会比 Apify 账单少，不会多
    """
    cur.execute(
        """
        select coalesce(sum(e.billed_item_count), 0)
          from query_executions e
          join runs r on r.id = e.run_id
         where r.run_type = 'interactive'
           and e.started_at >= %s
        """,
        (since,),
    )
    # 输出   (615,)。coalesce 让一行都没有时拿到 0 而不是 None
    return int(cur.fetchone()[0])


def run_raw_result_count(cur: psycopg.Cursor, run_id: int) -> int:
    """这一跑一共采回来多少条（供应商返回的原始条数，几条 query 加起来）。

    输入：游标；哪一跑，如 38
    输出：一个整数，如 0（一条都没采回来）或 40

    推卡那一步拿它分开两种「一家都没分析」：一条帖子都没采回来（检索式太窄、筛人筛得太严），
    和采回来了但一家都没进分析（被过滤规则砍掉，或者全在冷却期里）。回给用户的话不一样，
    前一种该改的是检索式，后一种改检索式未必有用
    """
    cur.execute(
        "select coalesce(sum(raw_result_count), 0) from query_executions where run_id = %s",
        (run_id,),
    )
    # 输出   (40,)
    return int(cur.fetchone()[0])


def start_execution(cur: psycopg.Cursor, *, run_id: int, query_id: int) -> int:
    """开一条 query 的执行记录，状态先记 running。跑之前调。

    输入：游标；属于哪次 run、哪条 query
    输出：query_executions.id，跑完拿它调 finish_execution

    一个例子：

        输入   run_id=7  query_id=2
        库里   query_executions 多一行
               status='running'
               started_at 是现在，也就是这条 query 真正开始跑的时刻
               finished_at / error / 全部计数都还空着（计数列有 default 0）
        输出   那一行的 id，比如 31

    跟 insert_run 是同一个道理：先插 running 再收尾，中间炸了库里留下的
    是一条 running 的记录，那正是我们想要的信息。跑完才插一行的话，
    崩掉的那条 query 在库里什么都不留 —— 而「今天为什么只有两条线索」
    要靠它回答。run 那一层本来就是这么做的，execution 这一层原先没有，
    是这次补上的。

    另一件原先做不到的事：started_at 用的是库里的 default now()，
    而原来那个函数是在这条 query 跑完之后才被调用的，于是 started_at 和
    finished_at 只差几毫秒 —— 「这条 query 跑了多久」永远是 0，
    两列都填得好好的，看起来完全正常。Apify 一条 query 实测要跑几十秒到
    几分钟，那是成本判断和超时策略的原料。拆开之后这个数才是真的。
    """
    cur.execute(
        "insert into query_executions (run_id, query_id, status) "
        "values (%s, %s, 'running') returning id",
        (run_id, query_id),
    )
    # fetchone() 拿回新建那一行，形如 (31,)，取 [0] 得到 execution_id = 31。
    return cur.fetchone()[0]


def finish_execution(
    cur: psycopg.Cursor,
    execution_id: int,
    *,
    status: ExecutionStatus,
    counters: ExecutionCounters,
    error: str | None = None,
) -> None:
    """给一条 query 的执行收尾：写最终状态和全部计数。

    输入：游标；start_execution 返回的那个 id；最终状态；全部计数；失败原因
    输出：无

    落库前按 status 跑对应的那一半自查，跟库里的 CHECK 一一对齐：

        completed   counters.check()         四条全查
        failed      counters.check_always()  只查无条件那两条

    库里三条约束里，triage 那条自带「status 不是 completed 就免检」的前提，
    另外两条无条件。两边都有不是重复：这里炸能给出「哪个数对不上」的中文
    说明，库那层炸只给一个约束名，但它挡得住绕过这个函数直接写库的路径。
    早先这里不分 status、一律只在 completed 时查，于是一条 failed 的执行
    会在 Python 这边放行、到库里被拒 —— 正是这段注释想避免的那种报错。

    两个例子：

        跑成了
            输入   execution_id=31  status=ExecutionStatus.COMPLETED
                   counters 是这一跑各节点累加出来的
                   check() 四条全过；drop_reasons 那个字典
                   {"no_relevance_signal": 61, "company_page_author": 28}
                   转成 JSON 写进 jsonb 列
            库里   第 31 行 status 从 running 变成 completed，
                   finished_at 记 now()，十个计数列一次写满

        采集挂了
            输入   execution_id=31  status=ExecutionStatus.FAILED
                   counters 是崩之前累加到一半的  error='Apify 502'
                   只跑 check_always()：崩在半路时分流三堆本来就凑不齐，
                   拿 check() 去查它会把「采集失败」报成「计数对不上」
            库里   第 31 行留下 failed + 原因 + 半截计数

    status 不接受 running：这个函数是收尾用的，传 running 进来说明调用方
    把开始和结束搞混了。库里那条 CHECK 允许 running（start_execution 要用），
    所以这一下只能在这里挡。
    """
    # 拼错的裸字符串（'complete' 少个 d）在这里当场炸，而不是先静默走进
    # else 分支少查两条自查、再到库里撞 CHECK —— 那时候拿到的报错只有一个
    # 约束名，看不出是状态拼错了。
    status = ExecutionStatus(status)
    if status is ExecutionStatus.RUNNING:
        raise ValueError(f"finish_execution 是收尾用的，不该传 running：execution_id={execution_id}")

    if status is ExecutionStatus.COMPLETED:
        counters.check()
    else:
        counters.check_always()

    cur.execute(
        """
        update query_executions set
            status = %s,
            finished_at = now(),
            error = %s,
            raw_result_count = %s,
            billed_item_count = %s,
            signal_dropped_count = %s,
            candidate_dropped_count = %s,
            drop_reasons = %s,
            unique_company_count = %s,
            researchable_lead_count = %s,
            cooldown_skipped_count = %s,
            excluded_skipped_count = %s,
            novel_lead_count = %s,
            qualified_lead_count = %s
        where id = %s
        """,
        (
            status,
            error,
            counters.raw_result_count,
            counters.billed_item_count,
            counters.signal_dropped_count,
            counters.candidate_dropped_count,
            # drop_reasons 形如 {"no_relevance_signal": 61, "company_page_author": 28}，
            # 转成 JSON 字符串写进 jsonb 列。
            json.dumps(counters.drop_reasons),
            counters.unique_company_count,
            counters.researchable_lead_count,
            counters.cooldown_skipped_count,
            counters.excluded_skipped_count,
            counters.novel_lead_count,
            counters.qualified_lead_count,
            execution_id,
        ),
    )
    # 一行都没更新 = 这个 id 不存在。放过去的话这条执行的账就永远是
    # 那行 running 的空壳，而报表上它看起来只是「跑了很久还没结束」。
    if cur.rowcount != 1:
        raise RuntimeError(f"要收尾的执行不在库里：execution_id={execution_id}")


def _repost_columns(signal: Signal) -> tuple[str | None, ...]:
    """把转发那一半摊成五列。不是转发就全是 None。

    输入：一条信号
    输出：(原帖 id, 原帖正文, 原帖作者名, 原帖职位串, 原帖主页 URL)

    五列一起给、不单独给某一列：库里那条 CHECK 要求要么全空、要么 id 和
    正文都有值。半填的行意味着 adapter 漏翻译了一个字段，而那种行几周之后
    表现为「这条线索的证据原文是空的」，那时候已经查不回来了。

    为什么要存：repost_author / repost_headline / repost_mentioned 三条通道的
    候选，证据原文在原帖里。不存的话渲染卡片时回读 signals.text 拿到的是
    转发者自己写的那段，而真正有内容的那段丢了。

    两种情形：

        不是转发   (None, None, None, None, None)
        是转发     ('200000026...', '原帖正文...', 'Women in Cloud',
                    '28,470 followers', 'https://www.linkedin.com/company/...')
    """
    if signal.reposted_from is None:
        return (None, None, None, None, None)

    original = signal.reposted_from
    return (
        original.external_id,
        original.text,
        original.actor_name,
        original.actor_headline,
        original.actor_url,
    )


def insert_signals(cur: psycopg.Cursor, signals: Sequence[Signal], *, run_id: int) -> dict[str, int]:
    """把这一跑留下来的信号写进库。

    输入：游标；过完闸门留下的信号；属于哪次 run
    输出：external_id 到 signals.id 的映射，给 lead_db 写证据那一步用

    分两步而不是用 on conflict returning：后者对「跳过」的那些行不返回 id，
    映射会缺项，而缺项要到写证据那一步才炸，那时候已经离出错的地方很远了。

    撞 external_id 说明这条帖之前的 run 已经存过。这是正常的：
    同一条帖过几天又被搜到，而它作为证据只算一份。

    撞上跳过的时候，那一行的 run_id / query_search / matched_terms 保持
    首次写入的值，不会被这一跑覆盖。带来三件事，用这几列之前先想清楚：

        run_id         回答不了「这一跑看到了哪些信号」——
                       重见的那些还挂在当初那个 run 上
        query_search   只记第一个搜到它的 query，不是所有搜到过它的
        matched_terms  存的是当初那条 query 的命中词，不是这次这条的

    没做成「每次都覆盖」，是因为这三列记的都是首次发现时的事实，覆盖掉
    就丢了。真要回答「哪几条 query 都搜到过这条帖」得另开一张关联表，
    现在没有这个需求。per-query 的账不依赖这几列 —— 它在 query_executions
    上，按每次执行独立记，重复发现照样算进 raw_result_count。

    一个例子：这一跑留下三条信号，其中一条上周就存过

        输入   三条 Signal，external_id 是 p-1 / p-2 / p-3，p-2 库里已有
               insert 写进两行，p-2 撞唯一键被跳过
        输出   {'p-1': 318, 'p-2': 205, 'p-3': 319}
               p-2 拿回的是它上周那一行的 id，证据要挂到那一行上去
    """
    if not signals:
        return {}

    # rows 是一批位置参数元组，一条信号一个。最后五个是转发那一半，
    # 不是转发就全是 None。比如一条不是转发的：
    #     (7, "harvestapi_posts", "linkedin_post", "200000026...",
    #      "https://www.linkedin.com/posts/...", "正文...",
    #      "9f86d0818...", "9f86d0818...", "audio AND manufacturer",
    #      ["audio"], "Jane Doe", "Founder at Lumen", "https://...",
    #      "profile", datetime(...), datetime(...),
    #      None, None, None, None, None)
    # 第七个和第八个这里相同，是因为不是转发时事实摘要就等于内容摘要；
    # 转发时两者会分岔，取哪一段由 normalize 判，见 Signal.fact_hash。
    rows = [
        (
            run_id,
            s.source,
            s.source_type,
            s.external_id,
            s.url,
            s.text,
            s.content_hash,
            s.fact_hash,
            s.query_search,
            list(s.matched_terms),
            s.actor_name,
            s.actor_headline,
            s.actor_url,
            s.actor_type,
            s.event_at,
            s.observed_at,
            *_repost_columns(s),
        )
        for s in signals
    ]
    cur.executemany(
        """
        insert into signals (
            run_id, source, source_type, external_id, url, text,
            content_hash, fact_hash, query_search, matched_terms,
            actor_name, actor_headline, actor_url, actor_type,
            event_at, observed_at,
            reposted_external_id, reposted_text, reposted_actor_name,
            reposted_actor_headline, reposted_actor_url
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        on conflict (external_id) do nothing
        """,
        rows,
    )

    # 回头把这批 external_id 全查一遍拿 id，跳过的那些也能查到。
    # fetchall() 返回 [("200000026...", 317), ("200000027...", 318), ...]，
    # dict() 直接把它变成 {external_id: signals.id}。
    cur.execute(
        "select external_id, id from signals where external_id = any(%s)",
        ([s.external_id for s in signals],),
    )
    return dict(cur.fetchall())


def seen_content_hashes(cur: psycopg.Cursor, *, days: int) -> frozenset[str]:
    """取最近这些天见过的正文摘要，给 prefilter 当跨天去重的依据。

    输入：游标；往回看多少天
    输出：content_hash 的集合，直接喂给 drop_signals 的 seen_content_hashes

    天数从参数进、不写死在这里：它是个业务判断，该由调用方从配置读。
    定下来的值是 10 天。理由不是存储 —— 一天 200 条、一年 7 万行加索引也就
    几 MB，而免费层给 0.5 GB，这个维度几年都不是约束。理由是语义：超过十天
    还一模一样的正文再出现，更可能是一件新事，而不是同一轮联播的尾巴，
    公告转发几天就结束了。

    一个例子：

        输入   days=10
               库里 signals 有 7 万行，落在最近十天的是 2000 行
        输出   那 2000 行的 content_hash 去重之后的集合

    窗口短反而更干净。hash 只回答「这段正文见没见过」，而「这家公司要不要
    再花钱分析」是 cooldown 的事 —— 两套记忆各管各的，位置也不同，一个在
    prefilter，一个在 analyzer 之前。跨过窗口的重复交给下游按公司判。
    同一家公司不同 hash 一律放行，那是 corroboration 的分子，
    挡掉等于掐死最强的免费信号。
    """
    cur.execute(
        # signals_seen_window_idx 是 (created_at desc, content_hash)，
        # 这条查询只碰这两列，走 index-only scan 不回表。
        "select distinct content_hash from signals "
        "where created_at > now() - (%s * interval '1 day')",
        (days,),
    )
    return frozenset(row[0] for row in cur.fetchall())


def record_discovery_hits(
    cur: psycopg.Cursor, hits: Sequence[DiscoveryHit], *, run_id: int
) -> None:
    """记下这一跑里哪条 query 命中了哪家公司。

    输入：游标；一批命中流水；这次 run
    输出：无

    append-only：不去重、不更新、没有 on conflict do nothing。这是 query
    表现的原始账本，改写它等于改写历史。库里那条唯一约束不是拿来去重的，
    是拿来炸的 —— 同一跑里同一条 query 对同一家公司只该有一行（_attribute
    按 lead_id 去重之后才生成流水），出现第二行说明归因那一步重复计数了，
    而那种错落库之后表现为「这条 query 的独占发现莫名其妙地多」，
    要几周后才会被怀疑。让它在写的时候就炸。

    一家公司被今天两条 query 同时命中时是两行，两行的 novel 都是 true ——
    「头一回见到」是公司的属性，跟哪条 query 在问它无关。归给「先跑的
    那条」是错的：那取决于 selector 今天把谁排在前面，同样的库状态会因为
    排序不同给出不同的归因，而这一列决定 proven 池的排名。

    一个例子：

        输入   hits=(DiscoveryHit(query_id=30, lead_id=501, novel=True),
                     DiscoveryHit(query_id=30, lead_id=502, novel=True),
                     DiscoveryHit(query_id=31, lead_id=502, novel=True))
               run_id=77
        输出   无。库里多三行，502 那家在两条 query 的账里各占一行

    存明细不存计数列，是因为「独占发现」的算法还没定死（按跑算还是按时间
    窗算、要不要看分数）。有明细，换一种算法是改一句 SQL；只有计数列，
    换算法要重新跑历史。
    """
    if not hits:
        return

    cur.executemany(
        """
        insert into lead_discovery_hits (query_id, lead_id, run_id, novel)
        values (%s, %s, %s, %s)
        """,
        [(hit.query_id, hit.lead_id, run_id, hit.novel) for hit in hits],
    )



def execution_totals(cur: psycopg.Cursor) -> dict[int, QueryTotals]:
    """按 query 把跑完的执行累加起来，给 stats 算排序分用。

    输入：游标
    输出：query_id 到 QueryTotals 的映射，只含至少完成过一次的 query

    只数 completed 的执行。failed 那些没有分流和归因的数（它们停在采集
    那一步，unique/researchable/qualified/novel 全是 0），把它们算进来
    等于让「采集老是超时」表现成「这条 query 找不到好线索」——
    前者要去查网络和额度，后者要把 query 降级，两件事的处理完全相反。

    库里只出原始计数，判断留给 stats.compute 那个纯函数。分开是为了让
    「今天为什么选这几条 query」能拿一份固定的数字重放出来，不必连库。

    一个例子：

        库里   query 30 有 12 条 completed、1 条 failed 的执行，
               12 条加起来 unique 418 家、researchable 140 家、
               qualified 61 家、novel 203 家；
               query 31 只有 1 条 completed，那次一家公司都没撞到；
               query 29 只有 2 条 failed 的执行
        输出   {30: QueryTotals(executions=12, unique_companies=418,
                                researchable_leads=140, qualified_leads=61,
                                novel_leads=203),
                31: QueryTotals(executions=1, unique_companies=0,
                                researchable_leads=0, qualified_leads=0,
                                novel_leads=0)}

        query 29 不在结果里 —— 它一次都没跑完。而 31 在结果里、数全是 0，
        那是「跑过，但那次什么都没找到」，是它的真实表现。

    从没跑成功过的 query 不给一行 0，理由就在上面那个区别：selector 那边
    「在不在这个映射里」是有意义的。candidate 池把不在里面的当 0 次执行
    排最前（它就是该先跑），proven 池把不在里面的当记账不一致、记一条
    warning。补一行 0 会把后面那条告警静默掉。
    """
    cur.execute(
        """
        select query_id,
               count(*),
               coalesce(sum(unique_company_count), 0),
               coalesce(sum(researchable_lead_count), 0),
               coalesce(sum(qualified_lead_count), 0),
               coalesce(sum(novel_lead_count), 0)
          from query_executions
         where status = 'completed'
         group by query_id
        """
    )
    # 每一行是 (query_id, 次数, 公司数, 分析过的, 过线的, 头一回见到的)。
    # 展开成关键字参数不用位置传：五个同类型的整数，位置传时调换两个
    # 不报错，只会安静地算出另一个 rank_score。
    return {
        row[0]: QueryTotals(
            executions=row[1],
            unique_companies=row[2],
            researchable_leads=row[3],
            qualified_leads=row[4],
            novel_leads=row[5],
        )
        for row in cur.fetchall()
    }
