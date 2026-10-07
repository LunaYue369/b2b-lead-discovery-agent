"""queries 表的读写。「该搜什么」这件事在库里的落点。

这张表是 query 的唯一权威。config/queries.yaml 只是种子，灌进来之后就不再是
依据了 —— 因为 pool 会被 stats 改（candidate 跑满样本升 proven）、cadence_days
会被 memory processor 调、is_active 会被淘汰规则关掉，这些都发生在库里，
文件不知道。

这一层只回答客观问题（有哪些、哪些今天到期），不做「今天该跑哪五条」那种
决策 —— 额度分配和排名在 query_intelligence/selector.py，那一层不连库，
拿一批固定的行就能反复回归。

所有会改字段的入口都是独立函数，一个只干一件事：last_run_at 走 mark_ran，
pool 走 set_pool，is_active 走 deactivate（后两个等 step 8 stats 落地时再写）。
upsert_query 一个字段都不改，就是为了不让这些更新顺着 seed 悄悄发生。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence

import psycopg

from models import (
    KnownQuery,
    QueryPool,
    QuerySource,
    QuerySpec,
    SourceType,
    normalize_query_text,
    validate_filters,
)


def upsert_query(
    cur: psycopg.Cursor,
    *,
    text: str,
    source: QuerySource,
    source_type: SourceType,
    pool: QueryPool,
    cadence_days: int,
    topics: Sequence[str],
    filters: dict[str, str],
    generated_by_memory_key: str | None = None,
) -> tuple[int, bool]:
    """让这条 query 在库里恰好有一行，已经有了就一个字段都不动。

    输入：游标；query 原文；怎么来的；搜哪个源；算哪一档；几天跑一次；
          这条词是为哪几个品类词造的（['bone conduction']，种子和 memory 那两路给 []）；
          带哪几个采集旋钮（{'authorKeywords': 'Founder OR CTO'}，不筛人就给 {}）；
          是哪条 memory 学出来的（只有 source=memory_generated 才给）
    输出：(queries.id, 这一行是不是这次新建的)

    filters 跟别的参数地位不同：它参与身份。0009 之后唯一键是
    (text_normalized, filters)，所以同一句检索词配不同旋钮是两条 query，
    各记各的账。这正是加它的理由 —— 不参与身份的话，第二条永远进不了库，
    它采回的公司和花掉的钱会全记到第一条头上，而 query_executions 是
    评价 query 好坏的唯一依据，proven 靠它排名、proven 占 60% 的每日额度。

    两种情形，各配一个真实例子。

    一、库里没有这条 -> 建行，created 为 True

            输入   text='Kickstarter AND (microphone OR audio OR "voice recorder")'
                   source=QuerySource.MANUAL
                   source_type=SourceType.LINKEDIN_POST
                   pool=QueryPool.PINNED
                   cadence_days=1
                   generated_by_memory_key=None
                   归一化成
                   'kickstarter and (microphone or audio or "voice recorder")'
                   insert 成功，returning 拿回新行的 id
            建出来 id=2  text 是原文（大小写照原样留着，卡片上要显示它）
                   text_normalized 是上面那个串，唯一约束比的是它
                   pool='pinned'  is_active=true  last_run_at=NULL
            输出   (2, True)

    二、库里已经有了 -> 什么都不改，回查拿 id，created 为 False

            库里已有 id=2 的那一行，但它已经不是当初种下去的样子了：
                   pool     'pinned' -> 'proven'   （stats 跑满样本升上去的）
                   cadence_days  1 -> 3            （memory processor 调慢的）
                   last_run_at   已经跑过 40 次

            输入   跟上面那次一模一样（有人又跑了一遍 seed 脚本）
                   insert 撞 (text_normalized, filters) 唯一索引，on conflict do nothing
                   一行都没写，returning 什么也没返回
                   接着按同一对键回查，拿到 id=2
            输出   (2, False)，库里那一行原封不动

    三、同一句词、不同旋钮 -> 这是两条 query，会各建一行

            输入   text 跟上面完全一样，但 filters={'authorKeywords': 'CTO'}
                   (text_normalized, filters) 这一对在库里没有，insert 成功
            输出   (7, True)

    第二种情形是这个函数最重要的性质，它直接来自 upsert_lead 的降档教训。
    那次的 bug 是 identity_kind 无条件跟随这次的引用，于是一行已经升到
    li_id 的记录会被一条只有名字的引用打回 name 档 —— 身份列一个没丢，
    丢的是「这一行有多可靠」。这里的对应物是 pool 和 cadence_days：
    写成「顺手把字段都更新一遍」的话，重跑一次 seed 就会把 stats 学了
    四十次执行才升上去的 proven 打回 pinned，而且库里看不出发生过什么。
    所以策略是「只保证存在，不负责更新」，改字段一律走意图明确的独立函数
    （set_pool / mark_ran / deactivate），一个函数只干一件事。

    代价是文件改了库里不会跟着变：把 queries.yaml 里的 cadence_days 从 1 改成 7
    再跑 seed，库里还是 1。这个代价不能靠静默吞掉 —— seed 脚本负责把
    文件和库的差异逐条报出来，让人自己决定改不改。

    不用「on conflict do nothing returning id」一步了事，是 insert_signals 那条
    教训的同款：returning 对被跳过的行不返回任何东西，一步写法在撞上的时候
    会返回 None，而调用方接着要拿这个 id 去挂执行记录，那时候才炸就离出错的
    地方很远了。分两步之后，这个函数对两种情形都保证返回一个真实的 id。

    created 不只是给日志看的。它是两个观测点：seed 报「新建几条、已存在几条」；
    以后 memory processor 生成 candidate query 时，created=False 意味着这条经验
    产出了一条库里早就有的 query —— 那是 candidate 池自我污染的最早信号，
    而那个池子一旦只进不出，每条都跑不满最小样本，谁好谁坏就永远评价不了。
    """
    # 三个枚举一律用值构造一遍。传进来的要是拼错的裸字符串（'pined'），
    # 这一行当场 ValueError，而不是带着它一路走到库里去撞 CHECK 约束 ——
    # 那时候拿到的报错只有一个约束名，看不出是哪个字段拼错了。
    # 这是 AssessmentProblem 那次的同款：裸 str 拼错会静默走很远。
    source = QuerySource(source)
    source_type = SourceType(source_type)
    pool = QueryPool(pool)

    # 空串防御。库里 text 和 text_normalized 都是 not null，但空字符串过得了
    # not null —— 那个约束在这件事上是假安心（adapter 那次 author.get("name")
    # or "" 就是这么把空串写进 normalized_name 的）。
    # 全空白的 query 归一化之后是空串，它会占住 text_normalized 那个唯一键，
    # 之后任何一条空白 query 都被认成「已经有了」。
    if not text.strip():
        raise ValueError("query 文本是空的（或者全是空白），不建行")

    text_normalized = normalize_query_text(text)
    if not text_normalized:
        raise ValueError(f"归一化之后成了空串，不建行：{text!r}")

    # cadence_days 库里有 CHECK (>= 1)。这里再查一遍不是重复：0 或负数的
    # 后果是「这条 query 每天都到期」或者「永远不到期」，两种都是安静地
    # 跑偏，而在这里炸能说清是哪条 query 的哪个字段。库那层挡的是绕过这个
    # 函数直接写库的路径。两边都查这条口径跟 insert_execution 一致。
    if cadence_days < 1:
        raise ValueError(f"cadence_days 至少是 1，拿到 {cadence_days}：{text!r}")

    # 库里那条 CHECK 叫 queries_memory_key_required。同样是两边都查：
    # 少了这个键，以后「这条 memory 到底带来了什么」永远查不出来，
    # 而那正是 memory processor 唯一的验收方式。
    if source is QuerySource.MEMORY_GENERATED and not generated_by_memory_key:
        raise ValueError(f"memory_generated 的 query 必须说明是哪条经验生的：{text!r}")
    if source is not QuerySource.MEMORY_GENERATED and generated_by_memory_key:
        raise ValueError(
            f"只有 memory_generated 才该带 generated_by_memory_key，"
            f"这条是 {source}：{text!r}"
        )

    # 旋钮查一遍再进库。放在这里而不是等库那层拦：库里只有一条
    # 「它得是个对象」的 CHECK，键名和分隔符它管不了，而那两样的症状
    # 恰恰是最静默的（不认识的键被 Apify 忽略、带逗号采回零条）。
    validate_filters(filters)

    cur.execute(
        """
        insert into queries (
            text, text_normalized, source_type, source,
            generated_by_memory_key, pool, cadence_days, topics, filters
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (text_normalized, filters) do nothing
        returning id
        """,
        (
            text,
            text_normalized,
            source_type,
            source,
            generated_by_memory_key,
            pool,
            cadence_days,
            # 库里那一列是 text[]，psycopg 把 Python 的 list 直接映成数组。
            # 这里统一转成 list：传元组进去也能存，但两个调用方一个给元组一个给
            # 列表的话，出问题时要先分辨是不是类型带来的
            list(topics),
            # jsonb 那一列要一个 JSON 串。psycopg 不会自动把 dict 当 jsonb，
            # 所以显式 dumps；sort_keys 不用加 —— jsonb 存进去自己会排序，
            # 而唯一键比的是库里那个规范化之后的值，不是我们发过去的字节。
            json.dumps(filters),
        ),
    )

    # 插成功了 fetchone() 是 (2,)，撞上被跳过则是 None —— 这一步的
    # 有无就是 created 的判据，不用去比 created_at 是不是今天
    # （同一天里第二次跑也会撞上，那时它已经不新了；upsert_lead 同款）。
    inserted = cur.fetchone()
    if inserted is not None:
        return inserted[0], True

    # 撞上了才走这一步：按唯一键把既有那一行的 id 回查出来。
    # 查得到是必然的 —— 刚刚就是被这一行挡下来的。查不到只可能是有人
    # 在这中间把它删了，那属于不该发生的事，直接炸掉而不是返回 None，
    # 免得一个 None 被当成 id 挂到 query_executions 上去。
    #
    # 回查的条件必须跟唯一索引逐字一致（两列一起比）。只按 text_normalized 查的话，
    # 同一句词的另一套旋钮那一行会被查出来，于是这一跑的执行记录挂到别的 query 上。
    cur.execute(
        "select id from queries where text_normalized = %s and filters = %s::jsonb",
        (text_normalized, json.dumps(filters)),
    )
    existing = cur.fetchone()
    if existing is None:
        raise RuntimeError(
            f"撞了唯一键却查不到那一行，库被并发改过？{text_normalized!r} {filters!r}"
        )
    return existing[0], False


def pickable_queries(cur: psycopg.Cursor, *, now: dt.datetime) -> list[QuerySpec]:
    """今天客观上可以跑的 query。只筛「启用且到期」，不分额度。

    输入：游标；判定到期用的时刻（带时区）
    输出：QuerySpec 列表，从没跑过的排在最前，顺序确定

    一个例子。库里四条，now 是 2026-09-01 07:00 PT：

        id  text                              pool       cadence  last_run_at
        1   looking for a hardware manufa...  pinned     7        2026-08-30  还没到期
        2   Kickstarter AND (microphone ...   pinned     1        2026-08-31  到期了
        3   wearable AND (microphone OR ...   proven     1        NULL        从没跑过
        4   our manufacturer missed           candidate  1        2026-08-31  但 is_active=false

               SQL 里逐行算 last_run_at + cadence_days 天 <= now：
               第 1 条 08-30 + 7 天 = 09-06，还没到 -> 不选
               第 2 条 08-31 + 1 天 = 09-01，到了   -> 选
               第 3 条 last_run_at 是空 -> 选（从没跑过的一律算到期）
               第 4 条 is_active=false -> 不选，连到期都不用算

        输出   [QuerySpec(query_id=3, ...), QuerySpec(query_id=2, ...)]
               第 3 条排前面：last_run_at 为空的排最前

    这个函数刻意不做 20/60/20 的额度分配，也不按 pool 筛。那是 selector 的活，
    而 selector 在 query_intelligence/ 那一层、不许连库 —— 它要能拿一批固定的
    行反复回归，「今天为什么只选了这五条」才有立足点。这里只回答一个客观问题：
    哪些 query 今天允许被考虑。返回全部候选，怎么挑是上面那层的事。

    now 从参数进而不是用 SQL 里的 now()：同一批库状态在任何时候重放都该选出
    同一批 query，否则回归跑的结果每天都不一样。跟 analyzer 里 now 从参数进
    是同一条规矩。

    排序是「从没跑过的最前，其次是最久没跑的，最后按 id 兜底」。id 那一级不是
    装饰 —— 没有它，两条 last_run_at 相同的 query 顺序由 Postgres 随意决定，
    额度截断时今天留甲、明天留乙，而两次都「没报错」。上面那层可以随意重排，
    这里只保证顺序是确定的。

    对 last_run_at 的语义有一条硬约定：它是「最近一次跑成功的时刻」，不是
    「最近一次尝试的时刻」。所以 mark_ran 只能在这条 query 真的跑完之后调 ——
    采集挂了（Apify 502）还去更新它的话，这条 query 会白白吃掉一整个 cadence
    的冷却期，明天不会被选中。那是静默减少覆盖：报表上它看起来只是「今天没轮到」。
    """
    cur.execute(
        # 到期判断逐行算，因为 cadence_days 是每条 query 自己的：存量类 7 天、
        # 流量类 1 天。make_interval(days => ...) 而不是拼字符串，免得 SQL 注入
        # 和整数溢出两件事都要自己操心。
        #
        # 这条查询用不上 queries_pickable_idx —— 那条索引以 pool 打头，而这里
        # 不按 pool 筛。表大小是几十行到几百行，全表扫比走索引还快，等它长到
        # 几千行再说（真要优化就把索引改成 (is_active, last_run_at)）。
        """
        select id, text, source_type, pool, cadence_days, filters
        from queries
        where is_active
          and (
              last_run_at is null
              or last_run_at + make_interval(days => cadence_days) <= %s
          )
        order by last_run_at asc nulls first, id
        """,
        (now,),
    )
    # fetchall() 返回 [(3, 'wearable AND ...', 'linkedin_post', 'proven', 1, {}), ...]，
    # 列顺序就是 select 里的顺序。枚举在这里构造一次，让库里的字符串到这一层
    # 就变回词汇表里的类型，上面那层不用再认识库的取值。
    #
    # filters 那一列 psycopg 已经把 jsonb 解成 dict 了，不用自己 loads。
    # dict(row[5]) 再包一层是为了让每个 QuerySpec 拿到自己的那份 ——
    # 共用一个对象的话，谁改了一处，别的 query 发出去的旋钮跟着变。
    return [
        QuerySpec(
            query_id=row[0],
            text=row[1],
            source_type=SourceType(row[2]),
            pool=QueryPool(row[3]),
            cadence_days=row[4],
            filters=dict(row[5]),
        )
        for row in cur.fetchall()
    ]


def mark_ran(cur: psycopg.Cursor, query_id: int, *, ran_at: dt.datetime) -> None:
    """记下这条 query 最近一次跑成功的时刻。只有真跑成了才调。

    输入：游标；哪条 query；这一跑的时刻（带时区）
    输出：无

    一个例子：

        库里已有 id=2  cadence_days=1  last_run_at=2026-08-31 14:00

        输入   query_id=2  ran_at=2026-09-01 14:00
               取「库里那个」和「这次这个」里较晚的一个写回去
        库里   last_run_at 2026-08-31 14:00 -> 2026-09-01 14:00
        结果   下次 pickable_queries 要到 2026-09-02 14:00 之后才会再选中它

    一条一条调，不做批量。因为跑成没跑成是每条 query 各自的事：五条里
    两条超时三条成功，那三条要记、两条不能记。批量接口会诱导调用方
    在跑完一轮之后把整批都标一遍，而那正是下面这条约定最怕的事。

    对应 pickable_queries 里那条硬约定：last_run_at 是「最近一次跑成功」，
    不是「最近一次尝试」。采集挂了（Apify 502）也来调这个函数的话，
    这条 query 会白白吃掉一整个 cadence 的冷却期，明天不会被选中 ——
    而报表上它看起来只是「今天没轮到」，是静默减少覆盖。所以这个函数的
    位置是「确认这一跑 completed 之后」，不是「发起采集之后」。

    ran_at 从参数进，而且该传这一跑开头那个 now —— 跟喂给
    pickable_queries 的是同一个。一次 run 里所有时刻用同一个钟，
    整跑才可复现；用 SQL 里的 now() 的话，同一批数据重放两次
    会得到两套 last_run_at，之后选出来的 query 也就不一样了。

    greatest 那一下是防倒退：两次 run 时间上重叠时（比如 scheduled 那跑
    07:00 开始、09:05 才收尾，而 interactive 在 09:00 单独跑过同一条），
    后收尾的那个会把 last_run_at 往回拨，于是这条 query 提前到期、
    多跑一次。倒退不会报错，只会让 cadence 悄悄变短，所以在 SQL 里
    一次性挡掉，而不是要求每个调用方自己记得比一下。
    """
    cur.execute(
        # coalesce 处理「从没跑过」那一支：last_run_at 是空时
        # greatest(ran_at, ran_at) 就等于 ran_at。
        # 不写成 coalesce 的话 greatest(null, ran_at) 在 Postgres 里
        # 确实也返回 ran_at（它会跳过 null），但那是靠记住一个函数的
        # 边角行为，写出来更省事的是让两支长得一样。
        "update queries set "
        "last_run_at = greatest(coalesce(last_run_at, %s), %s), updated_at = now() "
        "where id = %s",
        (ran_at, ran_at, query_id),
    )
    # 一行都没更新 = 这个 id 不存在。放过去的话这条 query 永远停在
    # 上一次的 last_run_at，于是每天都被选中、每天都跑 —— 花的是真钱，
    # 而报表上它看起来只是「这条 query 很勤快」。
    if cur.rowcount != 1:
        raise RuntimeError(f"要标记的 query 不在库里：query_id={query_id}")

def set_active(cur, query_id: int, *, active: bool) -> None:
    """开关一条 query 参不参与每天的组队。

    输入：游标；哪条 query；True 进轮转，False 退出轮转
    输出：无

    一个例子：

        库里已有 id=45  pool='candidate'  is_active=true

        输入   query_id=45  active=False
        库里   is_active true -> false，updated_at 刷新
        结果   pickable_queries 从此不再选中它。它的历史执行记录一条不动，
               query_executions 上的账还在，以后重新打开就接着算

    单独一个函数、不塞进 upsert_query，跟 set_pool / mark_ran 是同一条口径：
    upsert_query 只保证「这一行存在」，谁要改字段就得明写出改的是哪个字段。
    合在一起的话，重跑一次 seed 会顺手把人工关掉的 query 又打开，而库里
    看不出发生过什么。
    """
    cur.execute(
        "update queries set is_active = %s, updated_at = now() where id = %s",
        (active, query_id),
    )
    # 一行都没更新 = 这个 id 不存在。放过去的话调用方会以为自己关掉了一条
    # query，而它明天照跑 —— 花的是真钱，报表上完全正常。
    if cur.rowcount != 1:
        raise RuntimeError(f"要改的 query 不在库里：query_id={query_id}")


def all_queries(cur: psycopg.Cursor) -> list[KnownQuery]:
    """库里现有的全部 query，包括关掉的。拟词之前端给 planner 看。

    输入：游标
    输出：KnownQuery 列表，按 id 升序

    一个例子。库里四条：

        id  text                              pool       是否启用  last_run_at
        29  looking for a hardware manufa...  candidate  是        2026-09-02
        30  Kickstarter AND (microphone ...   candidate  是        2026-09-02
        31  wearable AND (microphone OR ...   candidate  是        NULL
        44  bone conduction AND headphone     candidate  否        NULL

        输出   四条都返回，包括第 44 条

    跟 pickable_queries 的差别不是过滤条件松一点，是问的问题不一样：那个问
    「今天客观上哪几条可以跑」，所以要筛启用和到期；这个问「库里已经有哪些词」，
    关掉的和没到期的都得端上来。

    关掉的那些尤其要有：对话里新建的 query 默认就是 is_active=false（一次探索不等于
    从此每天跑），它们正是最该让 planner 看见的一批 —— 上一次对话已经探过同一个
    题目这件事，只有从这里看得出来。

    按 id 排不按 last_run_at 排：这份东西要进 planner 的 prompt，同样的库状态两次
    必须给出同一份，而 id 是唯一不会随时间变的那一级。pickable_queries 那边按
    last_run_at 排是因为它的输出要被额度截断，先后决定谁今天跑得上。

    不分页也不筛：库里现在一共三条，几百 token。等它长到几百条再谈怎么挑，那时候
    也才知道该按什么挑（见 langgraph学习/graph/graph_2b.txt 7.3）。
    """
    cur.execute(
        """
        select id, text, pool, cadence_days, is_active, last_run_at, topics
        from queries
        order by id
        """
    )
    # fetchall() 返回
    #   [(30, 'Kickstarter AND ...', 'candidate', 1, True, datetime(...), []),
    #    (44, 'bone conduction AND ...', 'candidate', 1, False, None, ['bone conduction'])]
    # 列顺序就是 select 里的顺序。枚举在这一层构造，上面那层不用认识库里的字符串
    return [
        KnownQuery(
            query_id=row[0],
            text=row[1],
            pool=QueryPool(row[2]),
            cadence_days=row[3],
            is_active=row[4],
            last_run_at=row[5],
            # 库里是 text[]，取回来是 Python 的 list。转成元组是因为 KnownQuery 是
            # frozen dataclass，装一个可变的东西进去，等于让拿到它的人能改库里那行的
            # 副本而不自知
            topics=tuple(row[6]),
        )
        for row in cur.fetchall()
    ]
