"""query 本身：它是怎么来的、算哪一档、多久跑一次。"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum

from models.signal import SourceType

# 连续空白（含换行和制表符）。归一化时折成一个空格。
_WHITESPACE = re.compile(r"\s+")


def normalize_query_text(text: str) -> str:
    """query 文本归一化。queries.text_normalized 那条唯一约束比的就是它。

    输入：一条 query 的原文，如
        '  Kickstarter AND (microphone OR audio OR "voice recorder")  '
    输出：归一化后的串
        'kickstarter and (microphone or audio or "voice recorder")'

    只做两件事：小写、把连续空白折成一个空格并去掉两端。
    标点、括号、引号、布尔符一律不动。

    这跟 content_hash 那边是同一个取舍，但结论比那边还保守：query 之间的
    差别本来就全在这些符号里。剥掉括号和引号之后，下面这两条会变成同一个键：

        Kickstarter AND (microphone OR audio)
        Kickstarter AND (microphone OR voice)

    撞了的后果是第二条 insert 时撞唯一约束、被当成「这条已经有了」而
    跳过 —— 一条本来该跑的 query 从此不存在，而且库里看不出少了什么。
    归一化不足的代价方向相反：大小写或空格不同的两条各占一行，
    看得见、也好清理。

    不认小写的 and / or 也不做任何布尔符处理：那是 matched_terms 的活
    （见 discovery/normalize.py，LinkedIn 只认大写布尔符）。这里的职责
    只有一个 —— 判断两条 query 是不是同一条。
    """
    return _WHITESPACE.sub(" ", text).strip().lower()


# 允许出现在 queries.filters 里的采集旋钮。
#
# 这张表刻意只有一个。它不是「actor 只支持这一个」—— actor 还支持
# authorsCompanies / authorsIndustryId / contentType / mentioningCompany 等等。
# 只放一个的理由有三条，都在实测数字上：
#   一、authorKeywords 直接打那条根因（按内容搜必然搜到供应商，改成筛人）
#   二、它收得很狠。实测同一条 query，不加是 20 条，加 'CTO' 只剩 4 条。
#       叠第二个旋钮会把量饿死，而我们每天只跑两条 query
#   三、每多一个旋钮就多一个身份维度（见 migration 0009：filters 参与唯一键），
#       库里的 query 会分裂成更多行，每行攒的统计更薄。现在一条 proven 都还没有
#
# 加一个新旋钮不需要 migration，在这里加一行即可 —— 但要先想清第三条。
_ALLOWED_FILTER_KEYS = ("authorKeywords",)

# authorKeywords 里绝对不能出现的分隔符。实测得来的，不是防御性编程：
# 'Founder, CTO' / 'Founder CTO' / 'Founder;CTO' 三种写法全部采回零条，
# 只有 'Founder OR CTO' 有结果。而采回零条不报错，跟「今天确实没线索」
# 在报表上长得一模一样 —— 这是这个字段最危险的用法，所以在进库之前就拦掉。
_BAD_KEYWORD_SEPARATORS = (",", ";", "|")


def validate_filters(filters: object) -> None:
    """查一遍 queries.filters 能不能用。不合格当场抛，不返回值。

    输入：一份 filters，如 {'authorKeywords': 'Founder OR CTO'}
    输出：无。有问题抛 ValueError

    参数标成 object 而不是 dict：这个函数的存在意义就是拦不合格的东西，
    而「根本不是个字典」正是其中一种。标成 dict 的话类型检查会让人以为
    这种情况不可能出现，然后 .items() 抛一个 AttributeError —— 那个报错
    说不出是 queries.yaml 里哪一条写错了。

    四样东西在这里查，症状一个比一个静默：

        根本不是字典 queries.yaml 里写成 `filters: [a, b]` 或者 `filters:`（空值）

        键名不认识   Apify 收到不认识的键会忽略它，而我们这边它已经算进身份了
                     （0009 的唯一键含 filters）-> 凭空多一条永远采不到东西的 query
        值不是字符串 actor 的 authorKeywords 是 string 不是 array，
                     传个列表过去会被判输入非法，整跑失败
        带逗号分号   采回零条，而且不报错

    键名白名单放在代码里不放 CHECK 约束：加一个旋钮不该要一条 migration。
    """
    if not isinstance(filters, dict):
        raise ValueError(
            f"filters 得是一个字典（键是旋钮名、值是字符串），收到 {type(filters).__name__}："
            f"{filters!r}"
        )

    for key, value in filters.items():
        if key not in _ALLOWED_FILTER_KEYS:
            raise ValueError(
                f"filters 里有不认识的旋钮 {key!r}，现在只认 {_ALLOWED_FILTER_KEYS}。"
                f"拼错的键 Apify 会忽略，而它已经算进这条 query 的身份了"
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"filters[{key!r}] 得是一个非空字符串，收到 {value!r}")

    keywords = filters.get("authorKeywords")
    if keywords:
        for separator in _BAD_KEYWORD_SEPARATORS:
            if separator in keywords:
                raise ValueError(
                    f"authorKeywords 里有 {separator!r}：实测这么写会采回零条而且不报错。"
                    f"多个词只能用 ' OR ' 连，例如 'Founder OR CTO'"
                )


# 采集窗口按档给的全部合法取值：只要这段时间里发的帖子。空串是「没指定，用配置里的默认」，
# 其余每一个都逐字抄自 actor 的 postedLimit 那个 enum。
#
# 这只是 actor 的一种给法。它另有一个 postedLimitDate 收任意日期，对话那条路上用户说了
# 多久就走那个（triggers/interactive_discovery/query/time_window.py）。这几档现在用在：
# discovery.yaml 的默认值（两条路没指定时都用它），和用户说「不限时间」时的 any。
# 以前 intent 那次调用拿它当 enum 让模型选，会被前缀带跑（「最近 2 个月」选成 24h），已经不这么用了
#
# 名字不叫 recency：打分那边已经有一维叫 recency（这件事发生多久了，算出来是个
# 分数），而这里问的是采集窗口。两个是不同的东西，各起各的名字。
POSTED_WITHIN_VALUES = (
    "",
    "1h",
    "24h",
    "week",
    "month",
    "3months",
    "6months",
    "year",
    "any",
)

# 每个采集窗口在方案卡上怎么说。空串（没指定）不在这里：卡片上印的永远是真正要用的那个值，
# 没指定的时候先换成配置里的默认，再来这里查。
# 输入   '24h'        输出   '最近 24 小时'
POSTED_WITHIN_TEXT = {
    "1h": "最近 1 小时",
    "24h": "最近 24 小时",
    "week": "最近一周",
    "month": "最近一个月",
    "3months": "最近三个月",
    "6months": "最近半年",
    "year": "最近一年",
    "any": "不限时间",
}
# 两张表对不上的话，卡片会在某个合法值上 KeyError。在 import 的时候就炸，不等到卡片那一刻
assert set(POSTED_WITHIN_TEXT) == set(POSTED_WITHIN_VALUES) - {""}


class QuerySource(StrEnum):
    """一条 query 是怎么来的。对应 queries.source。"""

    MANUAL = "manual"  # 人工写进库的种子 query
    INTERACTIVE = "interactive"  # Riley 在 Slack 对话里定下来的
    MEMORY_GENERATED = "memory_generated"  # memory processor 从历史表现里学出来的


class QueryPool(StrEnum):
    """scheduled discovery 组队时这条 query 算哪一档。

    每天的额度：pinned 20%、proven 60%、candidate 20%。
    candidate 的晋升淘汰规则见 query_intelligence/stats.py
    （跑满最小样本，Wilson 下界评判，进 proven 或 is_active=false）。
    """

    PINNED = "pinned"  # 人工钦定，不参与表现筛选（库里就是 pool='pinned'，没有 is_pinned 这个列）
    PROVEN = "proven"  # 硬指标达标（样本量够，Wilson 下界高）
    CANDIDATE = "candidate"  # 新生成的，样本量还不够，处于试跑期


@dataclass(frozen=True)
class QuerySpec:
    """要跑的一条 query。selector 组队的产出。"""

    query_id: int
    text: str
    source_type: SourceType
    pool: QueryPool

    # 存量类 query（同样那几个人反复出现）填 7，流量类填 1。
    # 不分开的话，存量 query 的 novel_lead_count 会长期偏低，
    # 被 proven 排名不公平地降级。
    cadence_days: int

    # 这条 query 带着哪几个采集旋钮跑。今天只可能有 authorKeywords 一个键，
    # 空字典表示不筛人（库里现有那三条种子都是空的）。
    #
    # 它跟上面几个字段地位不同：它是这条 query 身份的一部分（0009 的唯一键
    # 是 text_normalized 加 filters）。同一句检索词配不同的旋钮采回来的是两批
    # 不同的帖子，所以是两条 query、各记各的账。
    #
    # 类型是 dict 不是 tuple：它要原样进 jsonb，也要原样摊进 actor 的请求体，
    # 两头都是键值对。frozen 只挡住重新赋值，挡不住有人改里面的键 ——
    # 真改了的症状是这一跑发出去的旋钮跟库里记的不一样，所以谁都别改它。
    filters: dict[str, str]


@dataclass(frozen=True)
class QueryStats:
    """一条 query 的历史表现。stats.py 从 query_executions 聚合出来，selector 拿来排序。

    只装 selector 真正要用的两个数，外加算 rank_score 用的原始支撑。
    存原始支撑而不是只存结论，理由跟 memories.support 那一列一样：
    几周之后要能回答「这个 0.31 当时是怎么来的」，换了算法也能拿旧数据重算。

    一个例子（跑了 12 次的一条 proven query）：

        QueryStats(query_id=30, executions=12, rank_score=0.31,
                   unique_companies=418, qualified_leads=61, novel_leads=203)

    rank_score 是谁算的很要紧：只有 stats.py 算，selector 绝不自己算。
    两个地方各算一次的话，报表上的排名和实际跑的排名迟早会不一样，
    而那种不一致没有任何报错。这跟「confidence 不许 LLM 自己写、
    必须由 stats 算」是同一条纪律。

    rank_score 是 Wilson 下界一类的东西，不是裸比率 ——
    1 个 qualified / 1 个 researchable 的 100% 会排在 40/100 前面。
    """

    query_id: int
    executions: int  # 完成过的执行次数。candidate 池按它升序排（跑得最少的先跑）
    rank_score: float  # proven 池按它降序排。越大越好，含义由 stats.py 定义

    # 以下是算 rank_score 的原始支撑，selector 不读，只进报告
    unique_companies: int
    researchable_leads: int  # qualified yield 的分母：真送去分析过的那些
    qualified_leads: int
    novel_leads: int


@dataclass(frozen=True)
class QueryTotals:
    """一条 query 在 query_executions 里的原始累计。库里查出来的事实，不含判断。

    它跟 QueryStats 分开，是因为两者由不同的人负责：这个是 discovery_db
    从库里 sum 出来的，那个是 stats.py 算出来的判断。合成一个的话，
    「这个 0.31 是库里的还是算出来的」就没法一眼回答，而排查排名问题时
    第一个要问的就是这个。

    五个字段全是同类型的整数，所以刻意做成 dataclass 不用元组：
    位置传参时调换其中两个不会报错，只会安静地算出另一个 rank_score。

    一个例子（跑了 12 次的一条 query）：

        QueryTotals(executions=12, unique_companies=418,
                    researchable_leads=140, qualified_leads=61, novel_leads=203)

        读法：跑了 12 次，一共撞到 418 家公司；其中 140 家过了 cooldown
        分流、真花钱分析了；61 家过线；203 家是头一回见到的。
        418 和 140 差得远是正常的 —— 大部分公司在冷却期里，不重复花钱
        分析正是 cooldown 的目的。
    """

    executions: int
    unique_companies: int
    researchable_leads: int
    qualified_leads: int
    novel_leads: int


@dataclass(frozen=True)
class KnownQuery:
    """queries 表里的一行，拟词之前端给 planner 看的那份。

    跟 QuerySpec 的差别是问的问题不一样：QuerySpec 回答「这条今天要跑，跑的时候
    用什么参数」，所以只装 pipeline 真要用的那几样；这个回答「库里已经有这么一条词，
    它现在是什么状态」，所以多带 is_active 和 last_run_at 两样 —— 关掉的那些也要端
    上来，interactive 新建的 query 默认就是关着的，正是最该让 planner 看见的那一批。

    不把这两个字段加进 QuerySpec：那个类型 selector 和 pipeline 都在用，给它加字段
    等于让每一个构造它的地方都要多想一次这两样填什么，而它们在跑的时候一次都不读。

    一个实际值：

        KnownQuery(query_id=30,
                   text='Kickstarter AND (microphone OR audio OR "voice recorder")',
                   pool=QueryPool.CANDIDATE, cadence_days=1, is_active=True,
                   last_run_at=datetime(2026, 9, 2, 14, 3, tzinfo=UTC),
                   topics=())

    last_run_at 是「最近一次跑成功」的时刻，不是最近一次尝试，跟 queries 表那一列
    同义。从没跑成功过是 None —— 这是「外部数据真实缺失」那种允许 None 的情形。

    topics 是这条词当初为哪个题目造的，只有 interactive 建的那些才有：

        topics=('bone conduction',)   为骨传导那次对话造的
        topics=()                     种子词和 memory 生的，真的没有题目，不是不知道

    有它就能拿这次的品类词直接对，精确、不用猜；空的那些只能拿品类词去猜 text 的
    正文，而两边粒度不一样（品类词是用户说话的粒度，text 是布尔式里单词的粒度）。
    两条路都在 query_intelligence/knowledge.py 的 pick_relevant 里。
    """

    query_id: int
    text: str
    pool: QueryPool
    cadence_days: int
    is_active: bool
    last_run_at: dt.datetime | None
    topics: tuple[str, ...]
