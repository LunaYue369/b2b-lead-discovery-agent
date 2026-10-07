"""leads 和 lead_evidence：公司这一侧的读写。

leads 表装所有分析过的公司，不是只有合格的。不合格的也要留记录，
否则明天同一条 query 又搜到它、又送一次 LLM —— 而按实测分布，
80-90% 属于这一类。合格与否是 score 阈值 + status，不是入表条件。

lead_evidence 唯一键 (lead_id, content_hash)：同一个帖子被多条 query 命中
就是多行，一个月后整张表都是重复。

分流那部分（lead_states）原来计划放在 discovery/dedup.py，
挪到这里是因为它要查库，而 discovery/ 那一层的闸门是纯函数、
不许连库（check_isolation.py 守着）。分流的判决跟「上一次多少分」「已经
连着几次没起色」读的是同一行 leads，所以一次查库一起拿回来。

cooldown 按结果分级，不是固定 30 天：
    PUSHED（已推给 Riley）        30 天+
    边界分数                     7-14 天
    确认是供应商/同行/词义撞车    → EXCLUDED，永不复查
固定 30 天等于把预算平摊给已知垃圾。

比的是 seen（所有分析过的），不是 confirmed（合格的）。
比 confirmed 的话，被判过的东西会天天回来。

整个模块守着两条不变式，改写法之前先确认它们还成不成立：

    一、leads 的四个身份列，建行时有什么写什么，不是只写最强那一级。
        只写一级的话，下次来一条停在别的档的引用就找不到这一行，
        同一家公司会有两行。

    二、identity_kind 指到哪一档，identity_value 就等于那一档对应的那一列。
        建行时两边一起写，之后只在升档的那一刻一起改，所以不会漂。
        find_lead 判「这个键有没有被别人占着」直接建立在这条上面。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg

from models import (
    Assessment,
    Candidate,
    CardEvidence,
    CardSource,
    CompanyCluster,
    CompanyRef,
    DraftEvidence,
    DraftSource,
    LeadHit,
    LeadState,
    LeadStatus,
    LeadType,
    SavedLead,
    TriageVerdict,
    linkedin_company_slug,
    normalize_company_name,
    normalize_domain,
)

# 逐级查 leads 用的梯度，从强到弱，跟 CompanyRef.identity_key 同一个顺序。
# 两边顺序必须一致：identity_key 决定建行时记哪一档，这张表决定查旧的时候
# 按什么顺序找。顺序漂了会出现「建的时候按 id、查的时候先按名字」那种重复行。
# 列名是这里写死的字面量，不来自外部输入，所以拼进 SQL 是安全的。
_IDENTITY_LOOKUP = ("linkedin_company_id", "linkedin_slug", "domain", "normalized_name")

# 身份档的强弱，数字越小越强。键是 CompanyRef.identity_key 那一套名字，
# 跟上面那串列名一一对应但故意不同名：一套是代码里的词汇，一套是库里的列。
_IDENTITY_RANK = {"li_id": 0, "li_slug": 1, "domain": 2, "name": 3}

# 命中一行时要取回来的列。顺序必须跟 LeadRow 的字段顺序一致，
# 下面直接把每条记录展开成 LeadRow，靠的就是这个一致。
_IDENTITY_SELECT = (
    "select id, identity_kind, identity_value, linkedin_company_id, "
    "linkedin_slug, domain, normalized_name from leads where {column} = %s"
)


@dataclass(frozen=True)
class LeadRow:
    """leads 表里跟身份有关的那几列。

    find_lead 带回整行而不是只带 id，是因为调用方要做两件必须看见旧值的事：
    比档位强弱（决定升不升档），和比身份列（决定认不认这次命中）。
    只返回 id 的话这两件事都得再查一次库。
    """

    id: int
    identity_kind: str
    identity_value: str
    linkedin_company_id: str | None
    linkedin_slug: str | None
    domain: str | None
    normalized_name: str


def _identity_columns(ref: CompanyRef) -> dict[str, str | None]:
    """把一条公司引用摊成 leads 表里那四个身份列。

    输入：一条 CompanyRef
    输出：列名到值的字典，这条引用拿不到的那几档是 None

    归一化在这里做而不是在调用处，是为了让「写进库的值」和「查库用的值」
    出自同一段代码。两边各归一化一次的话，哪天改了其中一个，症状是
    同一家公司在库里躺着两行，而且极难发现。

    两种形状的引用摊出来是这样（公司名和 id 都是编的）：

        被 @ 的公司，身份最全
            CompanyRef(name='Solara Audio', li_id='900001',
                       url='.../company/solara-audio/', domain=None)
            ->  {'linkedin_company_id': '900001',
                 'linkedin_slug':       'solara-audio',
                 'domain':              None,
                 'normalized_name':     'solara audio'}

        从职位串兜底解析出来的，只有名字
            CompanyRef(name='Solara Audio', li_id=None, url=None, domain=None)
            ->  {'linkedin_company_id': None,
                 'linkedin_slug':       None,
                 'domain':              None,
                 'normalized_name':     'solara audio'}

    两种都会把 normalized_name 填上，不管前面几档有没有值。强身份那一行
    以后能被一条只有名字的引用找回来，靠的就是这一列当初也写了。
    """
    return {
        "linkedin_company_id": ref.linkedin_company_id,
        "linkedin_slug": linkedin_company_slug(ref.linkedin_url) if ref.linkedin_url else None,
        "domain": normalize_domain(ref.domain) if ref.domain else None,
        "normalized_name": normalize_company_name(ref.name),
    }


def _conflicts_with(row: LeadRow, columns: dict[str, str | None], matched_level: int) -> bool:
    """判断撞上的这一行其实是另一家公司。

    输入：库里撞上的那行；这条引用摊出来的四个身份列；撞上发生在梯度第几级
    输出：True 表示不是同一家，这次命中要作废

    判据是「强的分歧压过弱的一致」：在比命中那一级更强的任何一级上，
    双方都有值而且不相等，那就是两家公司。一方为空不算分歧 —— 那是
    「上次只知道名字、这次才拿到 id」，正是逐级往下查要接上的情况。

    只查更强的级、不查更弱的。命中级已经是强身份的时候，名字对不上是
    正常的（同一家公司在不同数据源里写法不同），拿弱级的分歧去否掉强级的
    一致，会把最可靠的那条归并也砍掉。

    三种情况，沿用 upsert_lead 里那两家同名假公司：

        一方为空 -> 不算分歧
            在名字那一级撞上，库里那行 li_id 是 NULL，引用的是 '900001'。
            那是「上次只知道名字、这次才拿到 id」，正是要接上的情况。

        双方都有而且不等 -> 是分歧
            在名字那一级撞上，库里那行 li_id 是 '900001'，引用的是 '900002'。
            名字一样只是撞车，更强的一级说了这是两家。

        分歧在更弱的级上 -> 根本不看
            在 li_id 那一级撞上（两边都是 '900001'），但两行名字写法不同，
            'solara audio' 和 'solara audio technology'。这时 matched_level
            是 0，切出来的列是空的，循环一次都不进。
    """
    for column in _IDENTITY_LOOKUP[:matched_level]:
        # getattr 取库里那行的同名字段。LeadRow 的字段名跟列名一样，
        # 这一点由 _IDENTITY_SELECT 的写法保证。
        mine = columns[column]
        theirs = getattr(row, column)
        if mine and theirs and mine != theirs:
            return True
    return False


def find_lead(cur: psycopg.Cursor, ref: CompanyRef) -> LeadRow | None:
    """按身份键梯度逐级查，认得出来就把那一行带回来。

    输入：一个游标；一条公司引用
    输出：认下来的那一行，没见过或者不敢认就是 None

    四种结果，认不认下来由这里定，认下来之后怎么写由 upsert_lead 定：

        某一级值相同，更强的级上没有分歧      认，返回那一行
        某一级值相同，更强的级上有分歧        不认，那是另一家公司
        弱级撞上多行，排除之后还剩不止一个    分辨不了。库里已经有一行占着
                                              这条引用自己那一档的键就复用它，
                                              否则不认
        四级查完都没认下来                    不认，库里确实没有

    不在第一个非空的身份上停手，是这个函数的关键。有 LinkedIn id 但库里
    没有那个 id，要继续往下查 slug、域名、名字 —— 因为上一次可能只知道
    公司名、建了一行弱身份，这次才从别的通道拿到 id。第一级没命中就返回
    None 的话，同一家公司在库里会躺两次。

    往下查的代价由 _conflicts_with 兜住：越往下越弱，弱到名字那一级就会
    出现「两家不同的公司同名」。排除不掉的歧义则一律放弃，跟 normalize 那边
    _absorb_weak_into_strong 是同一把尺子 —— 并错一次会把两家公司的证据
    永久混在一起，不并只是多花一次分析。

    为什么不直接拿 identity_key 查一次了事：它只返回最强的那一档，
    两条指向同一家但停在不同档的引用永远接不上。逐级查能接上 ——
    第一次可能建两行，第二次相遇就并上。要一次都不重得另建 alias 表。

    四级全空的引用走不到这里：identity_key 会先抛 IdentityUnresolvable，
    而 prefilter 规则八已经把这类候选砍在花钱之前。

    逐级往下查是什么样（引用有 id 和名字，库里那行当时只解析出了名字）：

        第一级  linkedin_company_id = '900001'    查不到，接着往下
        第二级  这条引用没有主页 URL              这一档是空的，跳过
        第三级  这条引用没有域名                  跳过
        第四级  normalized_name = 'solara audio'  撞上第 12 行；更强的那几级
                                                  第 12 行都是空，不算分歧
        结果    认下来，返回第 12 行

    认下来之后各种情形下到底写了什么，见 upsert_lead 的例子，这里不重复。
    """
    columns = _identity_columns(ref)

    for level, column in enumerate(_IDENTITY_LOOKUP):
        # column 从强到弱依次是那四列，level 是它的下标 0..3。
        # _conflicts_with 拿 level 切出「比这一级更强的那几列」。
        # 这条引用拿不到这一档就跳过，接着查下一档。
        value = columns[column]
        if not value:
            continue

        # execute 不返回结果，结果留在游标里，由 fetchall 取干净。
        # 列名是模块顶上写死的字面量，值走参数化。
        cur.execute(_IDENTITY_SELECT.format(column=column), (value,))

        # 每条记录是 select 里那七列，顺序跟 LeadRow 的字段一一对应，
        # 所以能直接展开。rows 的三种形态：
        #     []          这一级没撞上
        #     [一行]       前三级只可能是这样，它们各有一个唯一索引
        #     [好几行]     只可能出现在名字那一级，同名的不同公司一起回来
        #                 （normalized_name 上只有普通索引，没有唯一约束）
        # 一行的样子：
        #     LeadRow(id=42, identity_kind='li_id', identity_value='100000001',
        #             linkedin_company_id='100000001', linkedin_slug='lumen',
        #             domain=None, normalized_name='lumen')
        rows = [LeadRow(*record) for record in cur.fetchall()]

        # 排掉「更强的级上有分歧」的，剩下的才真有可能是同一家。
        same = [row for row in rows if not _conflicts_with(row, columns, level)]

        if len(same) == 1:
            return same[0]

        if len(same) > 1:
            # 分辨不了。但先看库里有没有一行正好占着这条引用自己那一档的键 ——
            # 有的话，那就是上一次同样分辨不了时给它留下的那一行，复用它。
            # 不复用的话每跑一次就多留一行同名空壳，而它们永远分辨不出来，
            # 行数会无界增长。复用不等于猜：那一行本来就代表「这个名字，
            # 身份不明」，跟旁边那几家有强身份的仍然是分开的。
            own_kind, own_value = ref.identity_key
            mine = [
                row
                for row in same
                if row.identity_kind == own_kind and row.identity_value == own_value
            ]
            return mine[0] if len(mine) == 1 else None

        # 这一级撞上的都是别家，或者根本没撞上，接着往下查。
        # 不在这里收手，是因为更弱的级仍然可能撞上另一行真的同一家。

    return None


def upsert_lead(cur: psycopg.Cursor, ref: CompanyRef, *, run_id: int) -> tuple[int, bool]:
    """让这家公司在 leads 表里恰好有一行，并把这次新知道的身份补上去。

    输入：游标；公司引用；这次是哪个 run
    输出：(leads.id, 这一行是不是这次新建的)

    第二个返回值就是 novel_lead_count 的分子。判据是 find_lead 有没有认下来，
    不是看 created_at 是不是今天 —— 同一天里第二次跑也会命中，那时它已经不新了。

    下面是这个函数会遇到的全部情形，按「库里是什么状态」分成三支。
    每支配一个例子，公司名和 id 都是编的，只为了看清每一步到底动了什么。
    例子里统一用两家名字撞车的假公司：
        Solara Audio  LinkedIn 公司 id 900001，主页 slug solara-audio
        Solara Audio  另一家同名的，LinkedIn 公司 id 900002
    两家归一化之后的名字都是 'solara audio'。

    一、库里没有任何一行认得它 -> 建行，身份列有什么写什么，created 为 True

        1a 真的头一回见到

            来的引用   name='Solara Audio' li_id=900001 slug='solara-audio'
            查库       四档挨个查，一档都没撞上
            建出来的行 identity_kind='li_id'  identity_value='900001'
                       linkedin_company_id='900001'
                       linkedin_slug='solara-audio'
                       domain=NULL
                       normalized_name='solara audio'
                       display_name='Solara Audio'

        1b 撞上了，但更强的级上有分歧 —— 那是另一家同名公司

            库里已有   第 7 行 li_id='900001'，normalized_name='solara audio'
            来的引用   name='Solara Audio' li_id=900002
            第一级     查 li_id=900002，查不到
            第四级     按 'solara audio' 撞上第 7 行
            但         第 7 行的 li_id 是 900001，这条引用是 900002，
                       两边都有值而且不等 -> 不是同一家，这次命中作废
            结果       建第 8 行，identity_kind='li_id' identity_value='900002'

            不挡这一下的话，两家公司的证据会混进第 7 行，而且 900002 那家
            以后永远建不出自己的行 —— 第一级永远查不到它，第四级每次都撞回
            第 7 行。

        1c 撞上好几行，分辨不了

            库里已有   第 7 行 li_id='900001'、第 8 行 li_id='900002'，
                       两行的 normalized_name 都是 'solara audio'
            来的引用   name='Solara Audio'，没有 id 也没有主页
                       （职位串兜底那条通道只解析得出名字）
            第四级     两行都撞上，而这条引用没有任何强身份可以用来分辨
            又         库里还没有哪一行占着 ('name', 'solara audio')
            结果       不猜，建第 9 行，identity_kind='name'
                       identity_value='solara audio'

            下一跑再来一条一模一样的引用，就会认出第 9 行、不再新建，
            走的是下面第三支。第 9 行代表「这个名字，身份不明」，
            跟第 7、第 8 行始终是分开的三行。

        1b 和 1c 建出来的行，created 同样是 True，所以 novel_lead_count 会
        把「同名但分辨不出来的另一家」算成一条新线索。这是有意的 —— 它确实
        是一行新的、还没分析过的公司；代价是同名撞车多的时候这个数会偏高。

    二、认下来了，而且这次的档比库里那行更强 -> 升档，同时把新身份补进空列

            库里已有   第 12 行 identity_kind='name'
                       identity_value='solara audio'
                       linkedin_company_id=NULL  linkedin_slug=NULL
                       normalized_name='solara audio'
                       （上一跑只从职位串解析出了名字）
            来的引用   name='Solara Audio' li_id=900001 slug='solara-audio'
            第一级     查 li_id=900001，查不到（那一行这一列是空的）
            第四级     按名字撞上第 12 行；更强的那几级第 12 行全是空，
                       一方为空不算分歧 -> 认下来
            写进去     linkedin_company_id  NULL -> '900001'
                       linkedin_slug        NULL -> 'solara-audio'
                       identity_kind      'name' -> 'li_id'
                       identity_value 'solara audio' -> '900001'

    三、认下来了，但这次的档不比库里那行强 -> 档位不动，只补空列

            库里已有   第 12 行 identity_kind='li_id' identity_value='900001'
                       linkedin_company_id='900001'  linkedin_slug=NULL
                       normalized_name='solara audio'
            来的引用   name='Solara Audio'，没有 id 也没有主页
            第四级     按名字撞上第 12 行，这条引用更强的那几级全是空，
                       不算分歧 -> 认下来
            写进去     四个身份列一个都没变（这条引用没带来任何新东西）
                       identity_kind / identity_value 保持 'li_id' / '900001'

            这一支跟第二支是相反的处境：第二支是这次比库里知道得多，所以
            既补列又升档；这一支是这次知道得更少，一个列都补不进去，
            唯一可能变的就只剩档位，而方向只能是降。

            既然什么都没补，为什么还要走一遍 upsert：因为调用方接着要拿
            这个 lead_id 去挂证据。这条引用身份上没带来新东西，但它背后是
            一篇新帖子 —— 又有一个人独立提到了这家公司，那是 corroboration
            的分子。不走这一遍就等于把那条证据丢了。

            档位跟着降下去的话，第 12 行会变成
                identity_kind       = 'name'      声称这行只靠名字撑着
                linkedin_company_id = '900001'    可列里躺着 LinkedIn 数字 id
            身份列一个没丢，丢的是「这一行有多可靠」这个判断，两个字段对
            同一件事给出了不同答案。落库对账里「多少家停在弱身份 name 级」
            就是按 identity_kind 分组数出来的，这一行会被数进 name 档 ——
            那个数用来判断职位串兜底解析这条通道值不值得继续维护，
            虚高了会得出反向结论。

            那个数是对整张表跑一次 group by 的快照，不是累计计数器：
            一行只会被数一次，问题不在数了几遍，在这一行当下的
            identity_kind 本身就不对。

            同档是这一支的另一种来路：同一条引用第二次遇到，值本来就一样，
            什么都不会变。

    身份列一律只往上补、不往下改：已经有值的列不覆盖。数据源偶尔会给
    同一家公司两个不同的 slug（改过名），后来的那个不见得更对，
    而覆盖会让这一行跟之前存的证据对不上。

    升档不会撞唯一键。能升说明更强的那几档刚才在库里都没查到（查到了
    就在那一档认下来了），加上模块头第二条不变式，等于没有别的行占着
    这个键。这条结论依赖 find_lead 从强到弱的查询顺序。

    有几件事这个函数不负责，写在这里免得以后误以为它兜住了：

        名字那一级认下来的时候，有可能其实是同名的另一家公司。
        _conflicts_with 挡不住这一类：它要求双方在同一档上都有值才算分歧，
        而会走到名字那一级，恰恰是因为有一方在强身份上什么都没有 ——
        没有东西可以拿来反对。「同一家公司的两条不同通道」和「两家碰巧
        同名」在这种数据上长得一模一样，这是信息上限，不是判断失误。
        往哪边下注是权衡过的：不认的话，「上一跑只有名字、这一跑拿到 id」
        每次都会变成两行，而那是常态；认错只在名字撞车时发生。而且撞第二次
        就不猜了 —— 库里一旦有两行同名，歧义那一支会接管。
        两支里升档那一支伤害更重：那一行会被写上另一家公司的 LinkedIn id，
        从此两家永久焊死；档位不动那一支只是一篇帖子的证据挂错了地方。
        真正的解法是等 enrich 给两边都补上域名，之后按域名对。

        同一家公司名字写法不同、又没有共同的强身份 —— 库里会有两行，
        而且不会自愈。normalized_name 只在建行时写一次、之后不更新，
        所以只有跟首次那个写法一致的名字才撞得上。要一次都不重得另建
        alias 表，现在不值得。

        同一跑之内两条引用停在不同档 —— 那是 normalize 那边
        _absorb_weak_into_strong 的活，跑内已经并掉了。这里只管跨跑。

        display_name 同样只在建行时写一次。它是给人看的原样公司名，
        不参与任何匹配，所以后来遇到更好看的写法也不回头改。

        还有一种边角：公司名归一化之后是空的（整个名字都是法律后缀或者
        标点）。那一行的 normalized_name 列会是空字符串，从此只能靠强身份
        找回来，名字那一级永远撞不上它 —— 查的时候空值会被跳过，所以不会
        跟别的空名字撞成一家，只是找不回来而已。
    """
    columns = _identity_columns(ref)

    # identity_key 返回梯度上取到的最强那一档，形如 ("li_id", "100000001")
    # 或者 ("name", "shenzhen xx audio")。
    kind, value = ref.identity_key

    row = find_lead(cur, ref)

    if row is None:
        cur.execute(
            """
            insert into leads (identity_kind, identity_value, linkedin_company_id,
                               linkedin_slug, domain, normalized_name, display_name,
                               first_seen_run_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                kind,
                value,
                columns["linkedin_company_id"],
                columns["linkedin_slug"],
                columns["domain"],
                columns["normalized_name"],
                ref.name,
                run_id,
            ),
        )
        # returning id 把新建那一行的 id 带回来，形如 (124,)。
        return cur.fetchone()[0], True

    # 数字越小越强，所以「更强」是 <。同档也走不升那一支，值本来就一样。
    upgrading = _IDENTITY_RANK[kind] < _IDENTITY_RANK[row.identity_kind]
    next_kind = kind if upgrading else row.identity_kind
    next_value = value if upgrading else row.identity_value

    # coalesce(旧值, 新值) 的意思是「旧值不为空就用旧值」，
    # 这三列因此只往上补、不往下改。
    cur.execute(
        """
        update leads
           set linkedin_company_id = coalesce(linkedin_company_id, %s),
               linkedin_slug       = coalesce(linkedin_slug, %s),
               domain              = coalesce(domain, %s),
               identity_kind       = %s,
               identity_value      = %s,
               updated_at          = now()
         where id = %s
        """,
        (
            columns["linkedin_company_id"],
            columns["linkedin_slug"],
            columns["domain"],
            next_kind,
            next_value,
            row.id,
        ),
    )
    return row.id, False


def insert_evidence(
    cur: psycopg.Cursor,
    lead_id: int,
    candidates: Iterable[Candidate],
    *,
    signal_ids: dict[str, int],
) -> int:
    """把支持这条 lead 的证据写进去，返回真正新增了几行。

    输入：游标；这条 lead；支持它的候选；external_id 到 signals.id 的映射
    输出：新增的行数

    唯一键是 (lead_id, content_hash) 不是 (lead_id, signal_id)：同一篇正文
    被多条 query 各命中一次，signal_id 不同但证据只有一份，按 signal_id
    去重会存好几行，corroboration 跟着虚高。撞上就跳过。

    返回新增行数是为了让调用处能对账：一个簇有几个候选、最后落了几行证据，
    对不上说明摘要算错了。save_clusters 会把它一路累加出去。

    一个例子：这家公司这一跑带着三条候选

        候选甲  signal external_id='p-1'  content_hash='H1'  -> 写进去
        候选乙  signal external_id='p-2'  content_hash='H2'  -> 写进去
        候选丙  signal external_id='p-3'  content_hash='H1'  -> 撞唯一键，跳过

    丙和甲是同一篇正文被两条 query 各搜到一次：signal_id 不同，但那是
    同一份证据。返回 2 —— 三条候选落了两行，差的那一条就是被合并掉的重复。
    """
    added = 0
    for candidate in candidates:
        # 映射缺项会在这里 KeyError，那说明信号还没落库就来写证据了，
        # 顺序错了要当场知道。
        signal_id = signal_ids[candidate.signal.external_id]
        cur.execute(
            """
            insert into lead_evidence (lead_id, signal_id, content_hash, lead_type)
            values (%s, %s, %s, %s)
            on conflict (lead_id, content_hash) do nothing
            """,
            (lead_id, signal_id, candidate.signal.content_hash, candidate.lead_type),
        )
        # rowcount 是这条 insert 实际写进去几行：撞了唯一键被跳过就是 0。
        added += cur.rowcount
    return added


def save_clusters(
    cur: psycopg.Cursor,
    clusters: Sequence[CompanyCluster],
    *,
    run_id: int,
    signal_ids: dict[str, int],
) -> tuple[SavedLead, ...]:
    """把归并好的公司连同证据一起落库。

    输入：游标；第三跳出来的簇；这次 run；external_id 到 signals.id 的映射
    输出：跟传进来的簇一一对应、顺序相同的 SavedLead

    逐条返回而不是三个合计数，是因为调用方后面每一步都要用到具体是哪一家：
    lead_id 拿去分流、写分数、记命中流水，novel 拿去按 query 归因 ——
    一家公司可能被今天两条 query 同时命中，两条的账里都要记上它，
    而合计数分不回去。三个合计数在调用方随时数得出来，反过来不成立。

    novel 由 upsert_lead 顺手带出来：「库里有没有这一行」那次查库在 upsert
    里已经做过了，这里不查第二遍。

    evidence_rows 是落库对账用的：一个簇有几个候选、最后落了几行证据，
    差额就是被 (lead_id, content_hash) 唯一键合并掉的重复证据。

    一个例子：这一跑归并出三家公司

        Solara Audio  库里没见过  -> 建行，两条候选落两行证据
        Kessel Mics   库里已有    -> 补身份，两条候选里有一条撞了唯一键，
                                     落一行证据
        Orbis Labs    库里没见过  -> 建行，一条候选落一行证据

    返回

        (SavedLead(lead_id=501, novel=True,  evidence_rows=2),
         SavedLead(lead_id=502, novel=False, evidence_rows=1),
         SavedLead(lead_id=503, novel=True,  evidence_rows=1))

    五条候选对四行证据，差的那一条就是被 content_hash 合并掉的。
    """
    saved: list[SavedLead] = []
    for cluster in clusters:
        lead_id, created = upsert_lead(cur, cluster.ref, run_id=run_id)
        evidence_rows = insert_evidence(cur, lead_id, cluster.candidates, signal_ids=signal_ids)
        saved.append(SavedLead(lead_id=lead_id, novel=created, evidence_rows=evidence_rows))
    return tuple(saved)


def lead_states(cur: psycopg.Cursor, lead_ids: Sequence[int]) -> dict[int, LeadState]:
    """问库这批公司现在什么样：这次要不要花钱分析，上一次判成什么样。

    输入：游标；一批 leads.id，允许有重复
    输出：lead_id 到 LeadState 的映射，传进来的每个 id 都有一项

    三件事一次查回来，因为读的是同一行 leads：冷却分流的判决、上一次的
    分数、已有的退避次数。拆成「先问要不要分析、再问上次多少分」两个方法
    读的是同一行，还多带一条「上次的分必须在写新分之前问到」的时序约束，
    而那条约束靠注释守不如靠结构守。

    逐家返回而不是三个合计数：按 query 归因要知道是哪几家。三个数在调用方
    随时数得出来（Counter 一句），反过来分不回去。

    查全部而不只查要分析的那一批：不分析的那些也要在每条 query 的账里
    分类计数，那是 query_executions 三堆分流对账的依据。

    「还在冷却期吗」交给库去判，不要取回时刻自己比。自己比就要在客户端凑
    一个「现在」，而那台机器的时区和时钟不保证跟库一致；同一批数据在两台
    机器上分流出不同结果，这种错查起来极痛苦。

    一个例子：传进来四家

        第 12 行  status='new'      cooldown_until=NULL      score=NULL
        第 34 行  status='new'      cooldown_until 是三天后   score=34 strikes=1
        第 56 行  status='pushed'   cooldown_until 是上周     score=71
        第 78 行  status='excluded' cooldown_until=NULL      score=8

    返回

        {12: LeadState(RESEARCHABLE, previous_score=None, cooldown_strikes=0),
         34: LeadState(COOLING,      previous_score=34,   cooldown_strikes=1),
         56: LeadState(RESEARCHABLE, previous_score=71,   cooldown_strikes=0),
         78: LeadState(EXCLUDED,     previous_score=8,    cooldown_strikes=0)}

    第 12 行的 previous_score 是 None 不是 0：它被发现过、落过库，但还没轮到
    分析。0 分是判过的结论，None 是还没判过，退避那一步对两者的处理相反。
    """
    if not lead_ids:
        return {}

    cur.execute(
        """
        select id,
               status = 'excluded'                                   as is_excluded,
               cooldown_until is not null and cooldown_until > now() as still_cooling,
               score,
               cooldown_strikes
          from leads
         where id = any(%s)
        """,
        (list(lead_ids),),
    )

    states: dict[int, LeadState] = {}
    # 每一行长这样：(34, False, True, 34, 1)
    # 第 34 家不是终态、还在冷却期、上次 34 分、已经连着一次没起色。
    for lead_id, is_excluded, still_cooling, score, strikes in cur.fetchall():
        if is_excluded:
            verdict = TriageVerdict.EXCLUDED
        elif still_cooling:
            verdict = TriageVerdict.COOLING
        else:
            verdict = TriageVerdict.RESEARCHABLE
        states[lead_id] = LeadState(
            verdict=verdict, previous_score=score, cooldown_strikes=strikes
        )

    # 传进来的 id 有一个没在库里（被删过、或者根本不是 leads 的 id），
    # 它就会从结果里一起消失。调用方拿这个映射去分流和归因，缺一项会在
    # 那边表现成 KeyError，或者更糟 —— 三堆分流之和对不上 query_executions
    # 那条 CHECK，而那时只给得出一个约束名。在这里炸能说清是少了谁。
    missing = sorted(set(lead_ids) - states.keys())
    if missing:
        raise ValueError(
            f"这批 lead 有 {len(missing)} 个在库里查不到：{missing[:5]}"
            f"（传进来 {len(set(lead_ids))} 个不重复的 id，查回 {len(states)} 个）"
        )

    return states


def save_assessment(
    cur: psycopg.Cursor,
    lead_id: int,
    *,
    assessment: Assessment,
    status: LeadStatus,
    cooldown_until: datetime | None,
    strikes: int,
    analyzed_at: datetime,
    run_id: int,
) -> None:
    """把七维打分、打分用的引用和冷却决定写回 leads 那一行。

    输入：游标；哪一家；完整打分；新状态；下次可复查的时刻（终态是 None）；
          新的退避次数；这次分析的时刻；这次分析是哪一跑（如 31）
    输出：无

    分数和冷却写在同一条 UPDATE 里，是因为它们是同一件事的两半：「这次判成
    什么样」和「下次什么时候再判」。拆成两个动作的话，中间崩一下就会留下
    一行有新分数、却还带着旧冷却期的数据 —— 那一行在报表上完全正常，
    只会表现为几周后一次莫名其妙的重复分析。

    llm_ 和 det_ 分开存不合成七列，是为了事后能分别回答「模型判得准不准」
    和「确定性维度算得对不对」。库里的 CHECK 焊着每维 0-10、总分 0-100，
    以及「终态不许带冷却时刻」。

    last_analyzed_at 从参数进不用 now()：拿历史数据回归时，写死的时刻才能
    让两次跑出同样的结果，跟 analyzer 和 cooldown 同一条规矩。

    rowcount 不是 1 就抛：传进来一个不存在的 lead_id，UPDATE 会安静地什么都
    不做，而调用方以为这一家的分数已经写进去了。这条链路上「静默没写」比
    「当场炸」难查得多 —— 分数没落库的后果是下一跑重新花一次 LLM 的钱，
    而库里看起来只是「这家还没分析过」。

    两个例子，正好是 cooldown 那两支：

    一、品类对、分数没过线，上一次也是这个分（连着第二次没起色）

        输入   lead_id=1063
               assessment.score=44  llm=(icp_fit=7, signal_strength=5,
                                         seniority=4, moq_fit=8,
                                         evidence_quotes=("we are prototyping
                                             an open-ear headset",),
                                         evidence_means="还在打样，没有量产计划")
                                    det=(recency=6, reachability=6,
                                         corroboration=3)
               status=NEW  cooldown_until=2026-10-27T07:00Z
               strikes=2   analyzed_at=2026-09-01T07:00Z   run_id=31
        输出   无。leads 第 1063 行的十六列被改写，updated_at 走库的 now()。
               其中打分依据那三列写成
                   llm_evidence_quotes  = {"we are prototyping an open-ear headset"}
                   llm_evidence_means   = '还在打样，没有量产计划'
                   last_analyzed_run_id = 31

        写完之后 lead_states 问同一行，拿回的是
        LeadState(COOLING, previous_score=44, cooldown_strikes=2) ——
        下一跑靠这两个数接着算退避。

    二、品类完全不对，终态

        输入   lead_id=1062  assessment.score=12  llm.icp_fit=0
               status=EXCLUDED  cooldown_until=None  strikes=0
        输出   无

        终态的 cooldown_until 必须是 None：库里那条
        leads_excluded_has_no_cooldown 焊着，写反了当场 IntegrityError。
        这一支写完之后那一行永远不会再被分析，除非 skip_cooldown 放行。

    三、这一行已经是 in_draft（Riley 说要写稿，交给 draft agent 了）

        输入   跟例子一完全一样，status=NEW
        输出   无。分数、冷却、退避次数照写，但 status 仍然是 in_draft

        这一条是唯一一处「传进来的值不一定被采用」，因为它是唯一一处
        另有主人的列：in_draft 的含义就是「这条已经不归 discovery 管了」，
        而修正七留下这一档的理由正是「Riley 说要写稿之后 discovery 还会把
        它捞回来」。打回 new 不报错，症状是那条线索几天后又出现在给 Riley
        的卡片里，而她已经在写稿了。

        另外两档不保护：pushed 打回 new 是对的（冷却期过了、又有新证据，
        它本来就该重新排队；「推过没有」由 pushed_at 那一列记着），
        excluded 本来就由这次的判决重新决定。
    """
    # in_draft 那一档不许被这条 UPDATE 打回去，见上面 docstring 的第三个例子。
    # 写成 SQL 里的 case 而不是先查一次再判：查完再写中间隔着一个窗口，
    # 而这一跑里另一条路径（Riley 刚点了 approve）恰好可能在那个窗口里改这一行。
    cur.execute(
        """
        update leads
           set score               = %s,
               llm_icp_fit         = %s,
               llm_signal_strength = %s,
               llm_seniority       = %s,
               llm_moq_fit         = %s,
               llm_analysis        = %s,
               llm_evidence_quotes = %s,
               llm_evidence_means  = %s,
               det_recency         = %s,
               det_reachability    = %s,
               det_corroboration   = %s,
               status              = case
                                         when status = 'in_draft' then status
                                         else %s
                                     end,
               cooldown_until      = %s,
               cooldown_strikes    = %s,
               last_analyzed_at    = %s,
               last_analyzed_run_id = %s,
               updated_at          = now()
         where id = %s
        """,
        (
            assessment.score,
            assessment.llm.icp_fit,
            assessment.llm.signal_strength,
            assessment.llm.seniority,
            assessment.llm.moq_fit,
            assessment.llm.analysis,
            # 输入   ("we are prototyping an open-ear headset",)，一个 tuple
            # 输出   ['we are prototyping an open-ear headset']，一个 list
            #        psycopg 只把 list 转成 Postgres 数组（库里是 text[]）。tuple 会被当成
            #        一条记录发出去，("a", "b") 变成 '(a,b)'，Postgres 报
            #        malformed array literal: "(a,b)"。空 tuple 转出来是空数组，会撞上
            #        leads_evidence_quotes_not_empty 当场炸：能走到这里的打分都过了
            #        NO_QUOTE 那道检查，一条引用都没有说明那道检查被绕过了
            list(assessment.llm.evidence_quotes),
            # 输入   '还在打样，没有量产计划'
            assessment.llm.evidence_means,
            assessment.det.recency,
            assessment.det.reachability,
            assessment.det.corroboration,
            status,
            cooldown_until,
            strikes,
            analyzed_at,
            # 输入   31。跟 last_analyzed_at 是一对，同一条 UPDATE 写：推卡时按
            #        where last_analyzed_run_id = 31 查出这一跑打了分的那几家
            run_id,
            lead_id,
        ),
    )
    if cur.rowcount != 1:
        raise ValueError(f"写打分时 leads 第 {lead_id} 行没找到，实际改了 {cur.rowcount} 行")


def card_sources(cur: psycopg.Cursor, run_id: int, *, max_evidence: int = 3) -> dict[int, CardSource]:
    """查这一跑打了分的那几家公司，把拼卡片要的素材全部从库里取出来。

    输入：游标；哪一跑，如 31；每张卡片最多放几条证据，默认 3（一家的证据一般不超过三条，
          放全了引用才都找得到出自哪一条）
    输出：lead_id 到 CardSource 的映射，如

        {1281: CardSource(display_name="Chatterly Inc.", status="new", score=67,
                          icp_fit=8, ..., quotes=("We just wrapped our Kickstarter",),
                          evidence=(最新那两条,)),
         1290: CardSource(display_name="NoteNest", status="in_draft", score=71,
                          ..., evidence=())}

        这一跑一家都没打上分（全被冷却期挡掉，或者采集全挂了）时是 {}
    """
    # 第一条 SQL：这一跑打了分的那几行 leads
    # 输入   run_id=31
    # 输出   每家一行，如
    #        (1281, 'Chatterly Inc.', 'chatterly', 'new', 67, 8, 8, 8, 8, 6, 2, 3,
    #         'Chatterly just closed a Kickstarter ...', '创始人本人说众筹刚结束，正在找量产',
    #         ['We just wrapped our Kickstarter'])
    #        「这一跑打了分」就是 last_analyzed_run_id = 31：那一列跟分数由
    #        save_assessment 同一条 UPDATE 写，反复答不好被丢弃的那几家不写分数、
    #        也不会被写上这一列。冷却期里的公司这一跑也会被命中，但没有重新分析，
    #        不在这里 —— 它们的分数是旧的，推出去等于重推一张旧卡
    cur.execute(
        """
        select id,
               display_name,
               identity_value,
               status,
               score,
               llm_icp_fit,
               llm_signal_strength,
               llm_seniority,
               llm_moq_fit,
               det_recency,
               det_reachability,
               det_corroboration,
               llm_analysis,
               llm_evidence_means,
               llm_evidence_quotes
          from leads
         where last_analyzed_run_id = %s
        """,
        (run_id,),
    )
    leads = cur.fetchall()
    if not leads:
        return {}

    # 第二条 SQL：这几家的证据来源
    # 输入   [1281, 1290]
    # 输出   一篇证据一行，如
    #        (1281, 'Shah', 'Founder', datetime(2026, 8, 19, ...), 'https://www.linkedin.com/posts/...',
    #         '<帖子正文>', None, None)
    #        后三列是正文、原帖正文、原帖作者的职位串（不是转发的帖子后两列是 None），
    #        build_cards 拿它们认每句引用出自哪一条
    #        一行证据都没有的那家（1290）在这里不出现，所以用 join 不用 left join
    #
    #        按事件时间倒序，最新的在前：放不下的时候，该丢的是最旧那几条，
    #        Riley 判断「现在值不值得联系」看的是最近发生了什么。并列时按 signals.id
    #        兜底成全序，否则两条同一天的证据每次查回来顺序可能不同，同一份数据
    #        拼出的卡片就不一样了。
    #
    #        截到 max_evidence 条在下面的 Python 里做，不在 SQL 里做：SQL 要「每组前 N 条」
    #        得写窗口函数，而一跑也就几十行，取回来再切一刀更容易读
    cur.execute(
        """
        select e.lead_id,
               s.actor_name,
               s.actor_headline,
               s.event_at,
               s.url,
               s.text,
               s.reposted_text,
               s.reposted_actor_headline
          from lead_evidence e
          join signals s on s.id = e.signal_id
         where e.lead_id = any(%s)
         order by e.lead_id, s.event_at desc nulls last, s.id desc
        """,
        ([row[0] for row in leads],),
    )

    # 输出   evidence -> {1281: [CardEvidence(Shah, Founder, 2026-08-19, ...), 第二条], 1290: []}
    #        每家先放一个空列表，一行证据都没有的那家最后拿到空元组。它照样出卡片，
    #        只是少「出自哪几篇帖子」那一区：公司名和分数都在，没有链接只是少一块
    evidence: dict[int, list[CardEvidence]] = {row[0]: [] for row in leads}
    texts: dict[int, list[tuple[str, ...]]] = {row[0]: [] for row in leads}
    for lead_id, actor, headline, event_at, url, text, reposted_text, reposted_headline in cur.fetchall():
        # 没有链接的证据放上去也点不开，所以不收
        if not url:
            continue
        if len(evidence[lead_id]) >= max_evidence:
            continue
        evidence[lead_id].append(
            CardEvidence(
                author=actor or "",
                role=headline or "",
                # 卡片这一层不做时间计算，所以在这里就格式化成日期串，如 '2026-08-19'。
                # 只到天，不到时分：Riley 判断的是「这事新不新」，几点几分对那个判断没有影响
                when=event_at.strftime("%Y-%m-%d") if event_at else "",
                url=url,
                # 引用在 build_cards 里才分进来
                quotes=(),
            )
        )
        # 输出   ('In the Vault is on it's way ...', 'ceo @ Hushly - a sanctuary for human sound')
        #        顺序跟打分时核对引用的一样：正文、原帖正文、职位串、原帖作者的职位串，空的不要
        texts[lead_id].append(tuple(t for t in (text, reposted_text, headline, reposted_headline) if t))

    return {
        lead_id: CardSource(
            # display_name 是 not null，但空串过得了那个约束 —— adapter 那边拿不到作者名时
            # 曾经写进去过空串。空名字会让卡片标题只剩一个公司图标，认不出是哪一家。
            # 退回 identity_value（如 'chatterly'，li_id 或者归一化后的名字），那不是编的，
            # 是我们手上确实有的那个身份。
            # 用库里这一份不用簇里解析出来的名字：库里那个是归并时选定的对外称呼，
            # Riley 在卡片上看到的名字要跟她以后在库里查到的一致
            display_name=display_name or identity_value,
            status=status,
            score=score,
            icp_fit=icp_fit,
            signal_strength=signal_strength,
            seniority=seniority,
            moq_fit=moq_fit,
            recency=recency,
            reachability=reachability,
            corroboration=corroboration,
            analysis=analysis,
            quotes_mean=quotes_mean,
            # 输入   ['We just wrapped our Kickstarter']，text[] 查出来是 list
            # 输出   ('We just wrapped our Kickstarter',)
            quotes=tuple(quotes),
            evidence=tuple(evidence[lead_id]),
            evidence_texts=tuple(texts[lead_id]),
        )
        for (
            lead_id,
            display_name,
            identity_value,
            status,
            score,
            icp_fit,
            signal_strength,
            seniority,
            moq_fit,
            recency,
            reachability,
            corroboration,
            analysis,
            quotes_mean,
            quotes,
        ) in leads
    }


def draft_source(cur: psycopg.Cursor, lead_id: int, *, max_evidence: int = 3) -> DraftSource | None:
    """给一家公司写稿之前，把它最近一次打分的结论和最新几条证据的全文从库里取出来。只读。

    输入：游标；哪一家，例 1289；最多取几条证据，默认 3（跟卡片上放的一样多，稿子引的证据
          用户在卡片上都点得开）
    输出：DraftSource，例子见那个类；leads 里没有这一行是 None

    跟 card_sources 的区别：那个按「哪一跑」一次查一批、给卡片用；这个按 lead_id 查一家，
    写稿时不知道也不需要知道卡片是哪一跑推的 —— leads 那几列永远是最近一次打分的结果。
    另外这里要帖子正文和通道（lead_type），卡片不要
    """
    # 第一条 SQL：这一家最近一次打分的结论
    # 输入   lead_id=1289
    # 输出   一行，例 (72, 'Acme Audio just finished a Kickstarter ...', '创始人本人说众筹刚结束……',
    #                  ['We just wrapped our Kickstarter'])；没有这一行是 None
    cur.execute(
        """
        select score, llm_analysis, llm_evidence_means, llm_evidence_quotes
          from leads
         where id = %s
        """,
        (lead_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    score, analysis, quotes_mean, quotes = row

    # 第二条 SQL：这一家的证据，最新的在前，只取 max_evidence 条
    # 输入   lead_id=1289, max_evidence=3
    # 输出   一条证据一行，例
    #        (2720, 'author', 'Dana Lee', 'Founder @ Acme Audio', datetime(2026, 9, 20, ...),
    #         'https://www.linkedin.com/posts/...', 'We just wrapped our Kickstarter ...', None, None)
    #        排序跟 card_sources 一样（时间倒序，同一刻按 signals.id 兜底），所以取到的就是卡片上那几条。
    #        只查一家，这里直接在 SQL 里 limit，不用像 card_sources 那样取回来再切。
    #        没有链接的证据卡片上不放，这里也不要：稿子引了一条用户在卡片上找不到的帖子，他没法核对
    cur.execute(
        """
        select s.id,
               e.lead_type,
               s.actor_name,
               s.actor_headline,
               s.event_at,
               s.url,
               s.text,
               s.reposted_actor_name,
               s.reposted_text
          from lead_evidence e
          join signals s on s.id = e.signal_id
         where e.lead_id = %s
           and s.url is not null
           and s.url <> ''
         order by s.event_at desc nulls last, s.id desc
         limit %s
        """,
        (lead_id, max_evidence),
    )
    evidence = tuple(
        DraftEvidence(
            signal_id=signal_id,
            # 库里存的是取值（'author' / 'mentioned' …），转回枚举，调用方才能问 is_first_person
            lead_type=LeadType(lead_type),
            author=actor or "",
            role=headline or "",
            # 只到天，理由同 card_sources
            when=event_at.strftime("%Y-%m-%d") if event_at else "",
            url=url,
            text=text or "",
            reposted_author=reposted_author,
            reposted_text=reposted_text,
        )
        for signal_id, lead_type, actor, headline, event_at, url, text, reposted_author, reposted_text in (
            cur.fetchall()
        )
    )

    return DraftSource(
        score=score,
        analysis=analysis,
        quotes_mean=quotes_mean,
        # 输入   ['We just wrapped our Kickstarter']，text[] 查出来是 list；没打过分是 None
        # 输出   ('We just wrapped our Kickstarter',) / ()
        quotes=tuple(quotes or ()),
        evidence=evidence,
    )


def mark_pushed(
    cur: psycopg.Cursor, lead_ids: Sequence[int], *, run_id: int, now: datetime
) -> None:
    """记下「这几条今天推给 Riley 了，是哪一跑推的」。

    输入：游标；真发出去了的那几条 lead_id；这一跑的 run_id；
          这一跑的时刻（带时区）
    输出：无

    两列一起写，这一条是硬要求：pushed_at 回答「推过没有」，
    pushed_run_id 回答「哪一跑推的」，而 ignored 扫描两个都要 ——
    它找的是「推出去超过 N 天还没人表态」的卡片，然后把 run_id 记进
    那一行 feedback 里好归因。只写一列的后果：
        只有 pushed_at    -> 扫得出来，但那行 ignored 的 run_id 是空的，
                             这条沉默进不了任何一条 query 的分母
        只有 pushed_run_id -> 永远扫不到，因为筛的是 pushed_at is not null

    四种情况：

    一、正常一批

        输入   lead_ids=[1281, 1287, 1290]  run_id=27  now=2026-09-02 14:00Z
        库里   那三行的 pushed_at 和 pushed_run_id 都写上
        输出   无
        后果   七天后没人表态的话，sweep_ignored 会扫到它们，
               并且把 run_id=27 记进那一行 feedback

    二、一条都没发出去（这一跑没有过线的线索）

        输入   lead_ids=[]
        库里   一个字都不改，也不发 SQL
        输出   无
        后果   刻意不当错处理：配额是天花板不是地板，一条都没有是
               合格结果

    三、同一条第二次被推（冷却期过了重新分析、又过线）

        输入   lead_ids=[1290]，而那一行上一轮已经有 pushed_at
        库里   两列都被覆盖成这一次的
        输出   无
        后果   覆盖是对的，不是丢信息：sweep_ignored 问的是
               「自从这次把卡片推给她之后她有没有表态」（比的是
               created_at > pushed_at），所以那一列必须是最近一次推送
               的时刻。留着第一次的时刻，第二轮的沉默就永远扫不出来

    四、传进来的 id 里有库里没有的

        输入   lead_ids=[1281, 999999]
        库里   只改到一行
        输出   抛 ValueError
        后果   这些 id 是 pushed_lead_ids 来的，也就是几秒钟前刚从
               leads 里查出来、刚发出去卡片的那几条。对不上说明卡片和
               库不是同一套数据，静默放过会让后面的调查无从下手

    now 从参数进，跟这个仓库里别处一样：一次 run 里所有时刻用同一个钟，
    整跑才可复现。用 SQL 的 now() 的话，同一批数据重放两次会得到两套
    pushed_at，而 ignored 扫描是拿它跟 feedback 的时间比大小的。
    """
    if not lead_ids:
        return

    unique = sorted(set(lead_ids))
    cur.execute(
        """
        update leads
           set pushed_at = %s,
               pushed_run_id = %s,
               updated_at = now()
         where id = any(%s)
        """,
        (now, run_id, unique),
    )
    if cur.rowcount != len(unique):
        raise ValueError(
            f"记推送时期望改 {len(unique)} 行，实际改了 {cur.rowcount} 行"
            f"（lead_ids={unique}）—— 卡片和库对不上"
        )


def pushed_in_run(cur: psycopg.Cursor, run_id: int) -> list[int]:
    """这一跑已经推过卡片的是哪几家。

    输入：游标；哪一跑，如 31
    输出：lead_id 列表，按 id 排序，如 [1281, 1287]。这一跑一张都还没推过是 []

    interactive 的推卡节点靠它防重推：那个节点崩溃之后，用户回一句话就会从它重跑，
    而卡片发出去就收不回来。查的是 mark_pushed 写的 pushed_run_id —— 那一列跟卡片
    同一个事务提交，所以查得到就说明那几张真的发出去了。

    会被下一次推送覆盖：同一家后来被别的跑又推了一次，pushed_run_id 就换成那一跑，
    这里就查不到它了。对防重推没有影响，重跑的永远是刚推完的那一跑
    """
    cur.execute("select id from leads where pushed_run_id = %s order by id", (run_id,))
    # 输出   [(1281,), (1287,)] -> [1281, 1287]
    return [row[0] for row in cur.fetchall()]


def hits_by_query(cur: psycopg.Cursor, query_ids: Sequence[int]) -> list[LeadHit]:
    """这几条 query 历来命中过哪些公司，连带那些公司现在什么样。

    输入：游标；一批 queries.id，允许为空
    输出：LeadHit 列表，按 (query_id, lead_id) 升序；传进来的 id 可能一行都没有

    一个例子。query 30 在两跑里各命中过 Bonewave 一次，另外在 run 31 命中过 Sonora；
    query 31 建了但还没跑过：

        输入   query_ids=[30, 31]

        库里   lead_discovery_hits            leads
               query_id lead_id run_id        id    display_name status    score cooldown_until
               30       1301    27            1288  Sonora      pushed    67    2026-09-24
               30       1301    31            1301  Bonewave       excluded  34    null
               30       1288    31

        输出   [LeadHit(query_id=30, lead_id=1288, display_name='Sonora',
                        status=LeadStatus.PUSHED, score=67, in_cooldown=True),
                LeadHit(query_id=30, lead_id=1301, display_name='Bonewave',
                        status=LeadStatus.EXCLUDED, score=34, in_cooldown=False),
                LeadHit(query_id=30, lead_id=1301, display_name='Bonewave',
                        status=LeadStatus.EXCLUDED, score=34, in_cooldown=False)]

    这个例子要说明的四件事：

        Bonewave 两行     同一家公司被同一条 query 在两跑里都命中，就是两行，而且两行
                        逐字相同 —— 区分它们的只有 run_id，而 run_id 不取。上面那层
                        要按 lead 去重才数得出「这条词带回过几家」
        1288 排在前面   order by 是 (query_id, lead_id)，跟分数、跟哪一跑都无关。
                        排序定死是为了同样的库状态两次给出同一份输入
        query 31 不见了 传进来的 id 可以一行都没有。它不会得到一个空壳，上面那层
                        用 dict.get 取，查不到就是「这条词还没带回过谁」
        空值            score 允许为空（那家还没轮到分析），原样返回 None，不在
                        这里填 0；Bonewave 的 cooldown_until 是空的，算出来的
                        in_cooldown 是 False 不是 None —— 那个布尔值是库当场算的，
                        一定有值

    返回的是明细不是计数，而且不按 lead 去重：那张表是 append-only 的原始账本，
    「这条 query 命中过几家」和「这家公司被几条 query 命中」是两个不同的去重方向，
    在这里先替上面那层挑一个方向，另一个方向就问不出来了。去重在
    query_intelligence/knowledge.py 里做，那是纯函数、可以拿固定的行反复回归。

    走 lead_discovery_hits 而不是从 signals.matched_terms 反查：那张表就是为
    「这条 query 历来命中过哪些公司」建的（0004 那条 migration 里唯一约束和列序都按
    这个方向排），而且它自带 query_id，归因是现成的。反查那条路要扫 signals 的
    text[] 数组再经 lead_evidence 两跳，拿回来还没有归因。

    「还在冷却期吗」交给库当场算成一个布尔值，不取回 cooldown_until 自己比 ——
    自己比就要在客户端凑一个「现在」，而那台机器的时区和时钟不保证跟库一致。
    跟 lead_states 那边同一条理由。

    novel 那一列不取：它是「这家公司是不是这一跑头一回进库」，属于某一次跑的事实，
    而这里问的是历来。要「这条词带回过多少新公司」看 query_executions 聚合出来的
    novel_leads，那个数已经按跑加好了。
    """
    if not query_ids:
        return []

    cur.execute(
        """
        select h.query_id,
               l.id,
               l.display_name,
               l.status,
               l.score,
               l.cooldown_until is not null and l.cooldown_until > now() as in_cooldown
          from lead_discovery_hits h
          join leads l on l.id = h.lead_id
         where h.query_id = any(%s)
         order by h.query_id, l.id
        """,
        (list(query_ids),),
    )
    # fetchall() 返回 [(30, 1301, 'Bonewave', 'excluded', 34, False), ...]
    return [
        LeadHit(
            query_id=row[0],
            lead_id=row[1],
            display_name=row[2],
            status=LeadStatus(row[3]),
            score=row[4],
            in_cooldown=row[5],
        )
        for row in cur.fetchall()
    ]
