"""拟词之前从库里端上来的那份东西：别用哪种词、库里已有哪些词、它们带回过谁。

这一层只做形状，不连库。查库那几句 SQL 在 persistence/query_db.py 和
persistence/lead_db.py 里，取数和拼装分开是为了让这份纯函数能拿一批固定的行反复
回归 —— 「这次为什么给 planner 看的是这两家」要能不连库就重放出来。
跟 selector 和 stats 那两个模块住在同一层、同一条理由。

设计（为什么公司挂在每条 query 底下、为什么例子是两条、为什么排序要定死）写在
langgraph学习/graph/graph_2b.txt 第 8 节，这里不重复。
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from discovery.normalize import term_pattern
from models import KnownQuery, LeadHit, LeadStatus, QueryTotals, normalize_query_text

DEFAULT_KNOWLEDGE_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "query_knowledge.yaml"

# 一次最多端给 planner 几条已有 query。
#
# 它是 prompt 长度的上限，不是相关性的判据 —— 命中就是相关，没有「相关到几分才算」
# 这种东西。库里现在一共三条 query，碰不到它。等长到几百条、一个品类词能命中几十条
# 的时候，多出来的那些只是同一件事的重复，而 planner 那次调用的输入会跟着变长。
_MAX_KNOWN_QUERIES = 12

# 品类词里把单词分开的那一格，跟 term_pattern 认的是同一套（空白或连字符）。
# 这里要它是因为「中心词是哪一个」得先把短语切成单词才知道。
_TOPIC_GAP = re.compile(r"[\s\-]+")

# 中心词至少要这么长才拿来降级用。
#
# 拦的是 vitamin c、type c 这种以单个字母收尾的短语：一个字母的词在布尔检索式里
# 到处都是，拿它去命中等于每条 query 都相关。2 这个数跟 normalize 那边给公司名定的
# 最短长度是同一个理由 —— 单字符做匹配键会跟所有东西撞在一起。
_MIN_HEAD_CHARS = 2

# 每条 query 底下带几个公司当例子。
#
# 定死两条不做成配置项：两条各回答一个不同的问题（这条词捞回来的垃圾长什么样、
# 它捞回来最好的长什么样），少一条就少答一个，多一条不增加新信息 —— 同一条 query
# 采回来的公司高度同质，第三第四家只是在重复第二家。
# 它不是一个可以调着玩的旋钮，所以不放 yaml。
_EXAMPLES_PER_QUERY = 2

# 分数是空的时候拿它排序。leads.score 允许为空，含义是「还没轮到分析」，
# 跟「判过，0 分」是两件事。排序时当成比 0 还靠后，因为没判过的那家举出来
# 当例子说明不了任何事。
_NO_SCORE = -1


@dataclass(frozen=True)
class AvoidNote:
    """一条负面知识。字段跟 memories 表同名同义，第四个除外。

    一个实际值：

        AvoidNote(key='supplier-seo-floods-manufacturer-queries',
                  scope='query',
                  statement='query 里不要用 manufacturer / ODM / OEM / factory 这类词。...',
                  source='三跑实测。config/queries.yaml 的注释里写明 ...')

    没有 confidence 也没有 support：手写这份拿不出分母，填一个数就是造精度。
    """

    key: str
    scope: str
    statement: str
    source: str


def load_avoid(path: pathlib.Path | str = DEFAULT_KNOWLEDGE_PATH) -> tuple[AvoidNote, ...]:
    """读 config/query_knowledge.yaml 的 avoid 那一节。

    输入：配置文件路径（不给就是仓库根的 config/query_knowledge.yaml）
    输出：AvoidNote 元组，顺序就是文件里的顺序

    一个实际值：

        文件   avoid:
                 - key: supplier-seo-floods-manufacturer-queries
                   scope: query
                   statement: query 里不要用 manufacturer ...
                   source: 三跑实测 ...
        输出   (AvoidNote(key='supplier-seo-floods-manufacturer-queries', ...),)

    缺键当场 KeyError，不给默认值：四个字段每一个都会进 planner 的 prompt，
    少一个的症状是模型看到一条没有出处、或者没有正文的经验，然后照样认真照做。

    不缓存。一次对话读一遍这个几十行的文件，跟 load_intent_config 同一条理由。
    """
    # 输入   文件内容
    #        raw 是整份 yaml，我们只要 avoid 那一节
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["avoid"]

    # 输出   一串冻结的对象。这个函数不连库、不碰 state
    return tuple(
        AvoidNote(
            key=str(item["key"]),
            scope=str(item["scope"]),
            statement=" ".join(str(item["statement"]).split()),
            source=" ".join(str(item["source"]).split()),
        )
        for item in raw
    )


def _head_word(topic: str) -> str:
    """一个品类词的中心词，也就是最后那个单词。整个短语不中的时候拿它再试一次。

    输入：一个品类词
    输出：中心词；没有可用的中心词就是空串

    四个实际值：

        'conference microphone'  -> 'microphone'
        'bone conduction'        -> 'conduction'
        'microphone'             -> ''          本来就是一个词，没有退一步这回事
        'vitamin c'              -> ''          'c' 太短，见 _MIN_HEAD_CHARS

    为什么是最后那个词：英文名词短语的中心词在末尾，前面那些是修饰语 ——
    conference microphone 是一种 microphone，microphone conference 才是一种会议。
    这是语言本身的规律，不是我们拍的一个参数，所以它不进配置也没有阈值可调。

    'bone conduction' -> 'conduction' 这种看着不像品类的也照样返回，不做判断：
    判断它像不像品类需要一张词表，而我们没有词表（也不该为这件事造一张）。
    返回了也不一定命中 —— 库里没有写 conduction 的 query，那就还是 0 条。
    """
    #        words 是这个短语切开之后的各个单词，如 ['conference', 'microphone']
    words = [word for word in _TOPIC_GAP.split(topic.strip()) if word]

    # 输出   只有一个单词的短语不给中心词：它自己就是中心词，上面那一步已经找过了，
    #        再找一遍只会把同一条命中记两次
    if len(words) < 2:
        return ""

    head = words[-1]
    return head if len(head) >= _MIN_HEAD_CHARS else ""


def pick_relevant(
    queries: Sequence[KnownQuery], topics: Sequence[str]
) -> list[tuple[KnownQuery, tuple[tuple[str, str, str], ...]]]:
    """从库里现有的 query 里挑出跟这次的品类词沾边的那几条。

    输入：
        queries  库里现有的全部 query（persistence.query_db.all_queries）
        topics   这一轮读出来的品类词，小写英文名词短语

    输出：
        [(那条 query, ((品类词, 真正命中的那个词, 怎么命中的), ...)), ...]，
        最多 _MAX_KNOWN_QUERIES 条，一条都没沾边就是空列表。
        「怎么命中的」有三种，强弱就是这个顺序：

            topics  对上了这条 query 当初记下的题目。它就是为这个题目造的
            text    题目对不上（或者根本没记），但整个品类词原样出现在正文里
            head    整个品类词不在正文里，只有它的中心词在。最弱的一种

    四个实际值（库里那三条种子词的 topics 都是空的，44 是上次对话建的、带 topics）：

        ['microphone']
            -> [(30 'Kickstarter AND (microphone OR audio OR "voice recorder")',
                 (('microphone', 'microphone', 'text'),)),
                (31 'wearable AND (microphone OR audio OR voice)',
                 (('microphone', 'microphone', 'text'),))]
        ['conference microphone']
            -> [(30, (('conference microphone', 'microphone', 'head'),)), (31, 同上)]
        ['bone conduction']
            -> [(44 'bone conduction AND (headphone OR earbuds)',
                 (('bone conduction', 'bone conduction', 'topics'),))]
        []
            -> []   give_up 那条路，连要找什么都没读出来

    库里真没有就返回空列表，不退回「全给」：那样 planner 会对着一批无关的词拟方案，
    而且库一长输入就没法预测。「照这几个词查过了，库里没有」本身是要说给用户听的话
    （这次是新题目，报价没有分母）。

    种类记在每一个命中上，不记在 query 上：同一条 query 完全可能一个品类词原样命中、
    另一个只中了中心词。记在 query 上就必须在这种时候挑一个说，而挑哪个都是错的。
    """
    relevant = []
    for query in queries:
        # 先拿这条 query 当初记下的题目对一遍。对得上就是最硬的证据：这条词就是为这个
        # 题目造的，不是碰巧正文里有这几个字母。
        matched = _by_topics(query, topics)

        # 对不上再猜正文。两条路是先后不是二选一：44 那条的题目是 bone conduction，
        # 但它的正文里还有 headphone 和 earbuds —— 问 earbuds 的时候它确实相关，
        # 而那件事只有正文看得出来。互斥的话这种真命中会被白白丢掉。
        # 种子那几条 topics 是空的，_by_topics 自然返回空，直接落到这一条上
        if not matched:
            matched = _by_text(query, topics)

        if matched:
            relevant.append((query, matched))

    # 输出   按三种命中各有几个来排，强的那种先比：先比 topics 有几个，一样多再比 text，
    #        再一样比 head，最后按 id 兜底成全序。
    #        这样一条只对上一个题目的，排在三个都只中中心词的前面 —— 强度是分档的，
    #        不是把三种混在一起数个数。
    #        全定死是因为同样的库状态两次要给出同一份 knowledge，否则「今天为什么拟成
    #        这样」就没有立足点 —— 跟 selector 按 id 兜底同一条
    relevant.sort(
        key=lambda row: (
            -_count(row[1], "topics"),
            -_count(row[1], "text"),
            -_count(row[1], "head"),
            row[0].query_id,
        )
    )
    return relevant[:_MAX_KNOWN_QUERIES]


def _count(matched: Sequence[tuple[str, str, str]], kind: str) -> int:
    """这几个命中里有几个是这一种。

    输入：一条 query 的命中，如 (('voice recorder', 'voice recorder', 'text'),
                                ('conference microphone', 'microphone', 'head'))
          和要数的那一种，'text'
    输出：1

    种类是命中自己带着的第三样，不靠「前两个词一样不一样」去推：topics 和 text 那两种
    前两个词都一样，推不出来。
    """
    return sum(1 for _, _, how in matched if how == kind)


def _by_topics(query: KnownQuery, topics: Sequence[str]) -> tuple[tuple[str, str, str], ...]:
    """这条 query 当初记下的题目，跟这次的品类词有哪几个是同一个。

    输入：一条 query（topics 为空的直接就是空手而归）；这一轮的品类词
    输出：命中，前两个词永远相同（对上的就是原词），种类永远是 'topics'

    一个实际值：

        query.topics = ('bone conduction', 'bone conduction headphone')
        topics       = ['bone conduction']
        输出          = (('bone conduction', 'bone conduction', 'topics'),)

    比之前归一化一遍（小写、折叠空白），用的是 queries.text_normalized 那同一个函数：
    两边都是 LLM 写出来的自由字符串，'Bone Conduction' 和 'bone  conduction' 跟
    'bone conduction' 是同一个题目，而大小写差一格就算两个题目是查不出来的那种错。

    只认完全相同，不做包含也不退中心词：这条路存在的理由就是「精确、不用猜」。
    问得比当初窄或宽就对不上（问 microphone 对不上记着 conference microphone 的那条），
    那时候外面会落到猜正文那条路上去，不会因此丢掉这条 query。要让近义也对上就得先定
    「多近算近」，那是拍阈值，而库里还没有几条带 topics 的 query 可以拿来定。
    """
    #        stored 是这条 query 记下的题目归一化之后的集合，
    #        如 {'bone conduction', 'bone conduction headphone'}
    stored = {normalize_query_text(topic) for topic in query.topics}

    # 输出   按 topics 传进来的顺序返回，不按集合的顺序 —— 集合无序，
    #        而同样的库状态两次要给出同一份
    return tuple((topic, topic, "topics") for topic in topics if normalize_query_text(topic) in stored)


def _by_text(query: KnownQuery, topics: Sequence[str]) -> tuple[tuple[str, str, str], ...]:
    """题目对不上，只能拿品类词去这条 query 的正文里找。

    输入：一条 query；这一轮的品类词
    输出：命中，种类是 'text'（整个品类词原样在正文里）或 'head'（只有中心词在）

    两个实际值，正文都是 'Kickstarter AND (microphone OR audio OR "voice recorder")'：

        topics=['voice recorder']        -> (('voice recorder', 'voice recorder', 'text'),)
        topics=['conference microphone'] -> (('conference microphone', 'microphone', 'head'),)
    """
    exact: list[tuple[str, str, str]] = []
    via_head: list[tuple[str, str, str]] = []

    for topic in topics:
        # 第一次试：拿整个品类词去正文里搜。品类词自己可能就是两三个单词
        # （voice recorder），这一步要求它们连着出现，不是各自出现过就算：
        #     品类词 voice recorder，在这几条正文里
        #       'Kickstarter AND (microphone OR audio OR "voice recorder")'  命中
        #       'wearable AND (microphone OR audio OR voice)'   不命中，只有 voice
        #       'AND (voice-recorder OR mic)'                   命中，连字符也算
        #       'AND (voice note recorder)'                     不是原样，中间插了词
        #       'AND (recorder voice)'                          不是原样，顺序不能换
        # 唯一允许有出入的，就是那两个单词中间那一格：term_pattern 把品类词里的
        # 空格编成「一个或多个空白或连字符」，正则长这样
        #     (?<![A-Za-z0-9])voice[\s\-]+recorder(?![A-Za-z0-9])
        # 两头那两个 (?<!...) (?!...) 要求前后不能紧跟 ASCII 字母或数字：
        #     audio 在 'audiobook AND wearable' 里   不命中
        #     audio 在 'audio耳机 OR mic' 里          命中（中文不是 ASCII，算边界）
        if term_pattern(topic).search(query.text):
            exact.append((topic, topic, "text"))
            continue

        # 第二次试：整条短语不中，退一步只拿中心词试
        #     'conference microphone' 不中 -> 拿 'microphone' 再试 -> 中
        # 第二样记的是真正命中的那个词，第三样记明这是退一步来的 —— 这两种的前两个词
        # 一样不一样是能看出来，但 topics 那种前两个词也一样，所以种类必须自己写出来
        head = _head_word(topic)
        if head and term_pattern(head).search(query.text):
            via_head.append((topic, head, "head"))

    # 输出   原样的排前面。两个篮子分开装就是为了这一步
    return tuple(exact + via_head)


def build_knowledge(
    *,
    avoid: Sequence[AvoidNote],
    topics: Sequence[str],
    relevant: Sequence[tuple[KnownQuery, tuple[tuple[str, str, str], ...]]],
    totals: Mapping[int, QueryTotals],
    hits: Sequence[LeadHit],
) -> dict[str, Any]:
    """把取回来的几样拼成一份能进 state 的普通字典。

    输入（一份实际值：一个品类词，挑中一条 query，它历来命中过两家公司）：

        avoid     (AvoidNote(key='supplier-seo-floods-manufacturer-queries', scope='query',
                             statement='query 里不要用 manufacturer / ODM / OEM ...',
                             source='三跑实测。config/queries.yaml 的注释里 ...'),)
        topics    ['microphone']
        relevant  [(KnownQuery(query_id=30, text='Kickstarter AND (microphone OR audio)',
                               pool=QueryPool.CANDIDATE, cadence_days=1, is_active=True,
                               last_run_at=datetime(2026, 9, 2, 14, 3, tzinfo=UTC),
                               topics=()),
                    (('microphone', 'microphone', 'text'),))]
                  第三样 'text' 是这个命中怎么来的：整个品类词原样出现在正文里
        totals    {29: QueryTotals(executions=2, ...),
                   30: QueryTotals(executions=1, unique_companies=23, researchable_leads=23,
                                   qualified_leads=4, novel_leads=23)}
                  库里每一条跑完过的 query 都在里面，不只挑中的那条 —— 29 那份用不上，
                  下面按 query_id 取，取不到的跳过
        hits      [LeadHit(query_id=30, lead_id=1288, display_name='Sonora',
                           status=LeadStatus.PUSHED, score=67, in_cooldown=True),
                   LeadHit(query_id=30, lead_id=1301, display_name='Bonewave',
                           status=LeadStatus.EXCLUDED, score=34, in_cooldown=False),
                   LeadHit(query_id=30, lead_id=1301, ...)]
                  第三行跟第二行逐字相同：Bonewave 被这条 query 在两跑里都命中过

    输出（只有 str / int / float / bool / list / dict，四个键）：

        {"avoid": [{"key": "supplier-seo-floods-manufacturer-queries", "scope": "query",
                    "statement": "query 里不要用 manufacturer ...", "source": "三跑实测 ..."}],
         "topics_searched": ["microphone"],
         "known_queries": [
           {"id": 30, "text": "Kickstarter AND (microphone OR audio)",
            "matched_topics": [{"topic": "microphone", "matched": "microphone", "how": "text"}],
            "topics": [],
            "pool": "candidate", "cadence_days": 1, "is_active": True,
            "last_run_at": "2026-09-02",
            "executions": 1, "unique_companies": 23, "novel_leads": 23, "qualified_leads": 4,
            "leads": {"total": 2, "excluded": 1, "in_cooldown": 1,
                      "examples": [{"lead_id": 1301, "name": "Bonewave",
                                    "status": "excluded", "score": 34},
                                   {"lead_id": 1288, "name": "Sonora",
                                    "status": "pushed", "score": 67}]}}],
         "known_lead_total": 2}

    topics_searched 和 known_queries 要一起读：前者非空、后者是 []，说的是「照这几个词
    查过了，库里没有」。只给 known_queries 的话，这句话跟「还没查」长得一模一样。

    设计（为什么公司挂在每条 query 底下、为什么例子是两条）在 graph_2b.txt 第 8 节。

    整份东西要序列化进 checkpoint 躺很久，所以只出普通类型：枚举摊成字符串、时刻转成
    日期串、元组转成 list。跟 intent 那边 to_state_dict 同一条。
    """
    #        by_query 把命中按 query 分开，并且按 lead 去重。上面那三行 hits 变成：
    #
    #            {30: {1288: LeadHit(Sonora, pushed,   67, in_cooldown=True),
    #                  1301: LeadHit(Bonewave,  excluded, 34, in_cooldown=False)}}
    #
    #        外层的键是 query_id，内层的键是 lead_id。Bonewave 那两行在这里并成一个。
    #        lead_discovery_hits 是 append-only 的，同一条 query 同一家公司每跑一次
    #        就有一行，不去重的话「这条词带回过几家」会按跑的次数翻倍。
    #        并的时候不用挑哪一行：两行除了 run_id 之外逐字相同，而 run_id 根本没取
    #        回来（见 lead_db.hits_by_query），所以 setdefault 留先到的那行就够了
    by_query: dict[int, dict[int, LeadHit]] = {}
    for hit in hits:
        by_query.setdefault(hit.query_id, {}).setdefault(hit.lead_id, hit)

    #        known_queries 一条 query 一个 dict。顺序就是 relevant 的顺序，
    #        pick_relevant 已经定死（命中的品类词多的在前，一样多按 id）
    known_queries = []
    for query, matched in relevant:
        #        total 是这一条 query 的执行累计，从那份全库的 totals 里按 id 取出来：
        #
        #            query.query_id = 30
        #            totals         = {29: QueryTotals(...), 30: QueryTotals(...)}
        #            total          = QueryTotals(executions=1, unique_companies=23,
        #                                         researchable_leads=23,
        #                                         qualified_leads=4, novel_leads=23)
        #
        #        取不到（这条 query 一次都没跑完）就给一份全 0 的，不给 None：
        #        「跑过但一家都没撞到」和「从没跑过」靠 executions 这一个数就分得开，
        #        让四个字段各自表达一次，只会让读的人猜哪个 None 是什么意思
        total = totals.get(query.query_id, QueryTotals(0, 0, 0, 0, 0))

        #        found 是这条 query 历来命中过的公司，一家一条（已经去重）：
        #
        #            [LeadHit(Sonora, pushed, 67, in_cooldown=True),
        #             LeadHit(Bonewave, excluded, 34, in_cooldown=False)]
        #
        #        一家都没命中过就是空列表，下面三个计数全是 0、examples 是空的
        found = list(by_query.get(query.query_id, {}).values())
        known_queries.append(
            {
                "id": query.query_id,
                "text": query.text,
                # matched_topics=[{"topic": "conference microphone", "matched": "microphone"}]
                # 这条为什么在这份 knowledge 里。两个键相同就是整个品类词原样命中；
                # 不同就是整条短语没命中、退一步拿中心词命中的，那种弱得多（这条 query
                # 讲的是 microphone 泛类，不是会议麦），planner 该按这个分量用它。
                # 两个键永远都在，不做成「只有降级时才多一个键」——形状随内容变的话，
                # 读的人和模型都要先判断这次是哪一种
                # 每个命中三样：用户说的那个词、真正命中的那个词、怎么命中的。
                # 三种强弱不同（topics > text > head），planner 该按这个分量用它们；
                # 种类写出来不让它自己推 —— topics 和 text 那两种前两个词都一样，推不出来
                "matched_topics": [
                    {"topic": topic, "matched": word, "how": how} for topic, word, how in matched
                ],
                # 这条 query 当初为哪个题目造的。topics=[] 是种子词或 memory 生的
                "topics": list(query.topics),
                # pool="candidate"  枚举摊成库里那个字符串。checkpoint 里躺的应该是
                # 一份普通 jsonb，不是「按某个枚举类解释出来的值」
                "pool": query.pool.value,
                "cadence_days": query.cadence_days,
                "is_active": query.is_active,
                # last_run_at="2026-09-02"  只要日期不要时刻：给人看的是「上次哪天跑的」，
                # 带上时分秒会让 planner 那次调用的输入每天都长得不一样。从没跑过是 ""
                "last_run_at": query.last_run_at.date().isoformat() if query.last_run_at else "",
                # 下面四个数回答「这条词值不值」：跑过 1 次、那次撞到 23 家、23 家都是
                # 新的、其中 4 家过了线。它们是数，说不出那 23 家长什么样 —— 那是下面
                # leads 那一块的活，两块回答的不是同一个问题，所以都要
                "executions": total.executions,
                "unique_companies": total.unique_companies,
                "novel_leads": total.novel_leads,
                "qualified_leads": total.qualified_leads,
                "leads": {
                    # total=2  这条词历来带回过两家（去重之后）。它跟上面
                    # unique_companies=23 不是一个口径：那个是按跑加起来的，
                    # 同一家跨两跑算两次，两个数对不上是正常的
                    "total": len(found),
                    # excluded=1  其中判死了的有几家。excluded 除以 total 就是这条词的
                    # 垃圾率，那是 planner 拟新词时最该看的一个数
                    "excluded": sum(1 for hit in found if hit.status is LeadStatus.EXCLUDED),
                    # in_cooldown=1  还在冷却期的有几家。这是报价的一半：带回的大半都
                    # 还在冷却期，就说明现在跑多半是白花钱。注意它是计数不是过滤，
                    # 冷却中的那几家照样留在 found 里、照样可能被举成例子
                    "in_cooldown": sum(1 for hit in found if hit.in_cooldown),
                    # examples=[Bonewave(excluded 里最高分), Sonora(其余里最高分)]
                    # 先垃圾后最好，不按分数排，见 _pick_examples
                    "examples": [_as_example(hit) for hit in _pick_examples(found)],
                },
            }
        )

    # 输出   四个键。known_lead_total=2 是所有命中去重之后的家数（上面 hits 三行里只有
    #        1288 和 1301 两个 lead_id），它跟每条 query 各自的 total 加起来通常对不上
    #        —— 一家公司被两条词都命中是常态，那正是重叠
    return {
        "avoid": [
            {"key": note.key, "scope": note.scope, "statement": note.statement, "source": note.source}
            for note in avoid
        ],
        "topics_searched": list(topics),
        "known_queries": known_queries,
        "known_lead_total": len({hit.lead_id for hit in hits}),
    }


def _pick_examples(found: Sequence[LeadHit]) -> list[LeadHit]:
    """从一条 query 命中过的公司里挑两家当例子。

    输入：这条 query 命中过的公司，已经按 lead 去重
    输出：最多两家，顺序固定

    规则：

        第一家   status 是 excluded 里分数最高的
        第二家   其余里分数最高的
        某一档一家都没有   就从另一档补第二家
        同分     按 lead_id 兜底

    两家各回答一个问题：这条词捞回来的垃圾长什么样、它捞回来最好的长什么样。
    只按分数挑两家的话，两家都是好的，而「23 家里 13 家是 excluded」这件事
    一眼看不出来 —— 那恰恰是这条词最要紧的毛病。

    三个实际值：

        两档都有   excluded 的 Bonewave(34)、pushed 的 Sonora(67)
                   -> [Bonewave, Sonora]      先垃圾后最好，不按分数排
        只有一档   三家都是 excluded，分数 34 / 12 / 8
                   -> [34 那家, 12 那家]     同一档里按分数补第二家
        一家没有   -> []

    排序必须定死（同分按 lead_id），理由跟 selector 里「按 id 兜底成全序」一样：
    同样的库状态两次要给出同一份 knowledge。不定的话 planner 会对着不同的例子
    拟出不同的方案，而排查「今天为什么拟成这样」就没有立足点了。
    """
    #        两档各自按分数从高到低，同分按 lead_id
    excluded = sorted((hit for hit in found if hit.status is LeadStatus.EXCLUDED), key=_by_score_then_id)
    others = sorted((hit for hit in found if hit.status is not LeadStatus.EXCLUDED), key=_by_score_then_id)

    #        first 是两档各出一个；leftover 是剩下的，用来在某一档空了的时候补位
    first = [bucket[0] for bucket in (excluded, others) if bucket]
    leftover = sorted(excluded[1:] + others[1:], key=_by_score_then_id)

    # 输出   最多两家
    return (first + leftover)[:_EXAMPLES_PER_QUERY]


def _by_score_then_id(hit: LeadHit) -> tuple[int, int]:
    """排序键：分数高的在前，同分按 lead_id 小的在前。

    输入：一条命中
    输出：一个二元组，直接交给 sorted

    分数取负是为了让 sorted 的升序变成「分数降序」，而 lead_id 那一级仍然是升序 ——
    两级方向不同，所以不能整体 reverse=True。
    """
    return (-(hit.score if hit.score is not None else _NO_SCORE), hit.lead_id)


def _as_example(hit: LeadHit) -> dict[str, Any]:
    """把一条命中摊成给 planner 看的那四样。

    输入：一条命中
    输出：dict

    一个实际值：

        输入   LeadHit(query_id=30, lead_id=1301, display_name='Bonewave',
                       status='excluded', score=34, in_cooldown=False)
        输出   {"lead_id": 1301, "name": "Bonewave", "status": "excluded", "score": 34}

    不带 query_id：它已经是「挂在哪条 query 底下」了，再写一遍就是同一件事有两处真相。
    不带 in_cooldown：那个只进上面那层的计数，逐家列出来对拟词没有用。
    分数没判过时给 0 而不是 None，进 prompt 的东西不留 None —— 判没判过看 status。
    """
    return {
        "lead_id": hit.lead_id,
        "name": hit.display_name,
        "status": hit.status.value,
        "score": hit.score if hit.score is not None else 0,
    }
