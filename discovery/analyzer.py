"""LLM 打分。整条链路上唯一贵的一步，所以前面三道闸门都是为它服务的。

LLM 只填需要判断的四维（icp_fit / signal_strength / seniority / moq_fit），
recency / reachability / corroboration 一律代码算。
同一条原则的第三次应用：把能确定算的交给确定性层，别让 LLM 去赌。

━━━━━━━━━━━━━━━━━━ 一、会出什么错（全谱）━━━━━━━━━━━━━━━━━━

从最外面往里数六层。前两层是「回答根本没拿到、或者拿到了但不成形」，
中间三层是「回答拿到了但不对」，最后一层是我们判不出来的。

① 传输    网络断 / 读超时 / 429 限流 / 5xx
          谁查得出  SDK 抛异常，我们只能捕
          现状      SDK 自己退避重试（openai 3.6 默认 max_retries=2，覆盖
                    连接错误 / 408 / 409 / 429 / 5xx）。传到我们手上说明
                    它已经重试过并放弃了
             超时是我们的、重试是 SDK 的。SDK 默认 read timeout 是 600 秒，
             14 批最坏能挂两个多小时，而这是每天跑一次的定时任务 ——
             挂在那儿比失败更难发现。所以每次请求显式带上
             config.request_timeout_seconds

② 成形    finish_reason == "length"    输出被 max_tokens 截断成半个 JSON
          refusal 有值                 结构化输出下模型改成了拒答
          content is None              没有内容，也没有 refusal
          JSON 解析失败                走 strict schema 时基本只由截断引起
          谁查得出  _call_model
          缺字段 / 多字段 / 字段类型错这一类不在这儿查，靠 OpenAI 的
             strict schema 在服务端保证。这是一条依赖假设：换供应商、或者
             哪天用了不支持 strict 的模型，这一层会凭空消失，要自己补回来。
             写在这里是为了那天能想起来它本来是谁在管

③ 身份    漏答一家       丢的那家永远不出现在报表里，而漏斗数字看起来正常
          多答一家       多半是它自己编了个公司，那条会带着编的引用一路走下去
          同一家答两次   静默取最后一行 —— 两个不同的分数，报表上完全看不出来
          谁查得出  parse_assessments

④ 值      分数越界（不在 0-10）   0-10 是库里 CHECK 的范围，放过去要到落库
                                  那一步才炸，离出错的地方太远
          analysis 是空串         Riley 要的是分项加理由，没有理由的分数她
                                  没法校准，而分数本身看不出这条缺了理由
          谁查得出  parse_assessments

⑤ 内容    引用在这家公司的原文里找不回来
          一条有效引用都没给（空数组）
          引用是空串或纯空白（`"" in text` 恒为真，混得过子串比对）
          谁查得出  _check_content
          「找得回来」比「逐字子串」松一点：忽略大小写、空白多少和
             两端标点。实测一跑 20 家有 4 家因为模型顺手补了个句号被判成
             幻觉丢掉，相似度全在 0.989 以上。放宽的只是怎么找，找到之后
             存进 Assessment 的是原文那一段，不是模型写的那一段

⑥ 语义    分数都在 0-10、引用也都是原文，但把供应商判成了买方
          谁查得出   谁都查不出，重问也解决不了 —— 模型不是没遵守格式，
                    是判断错了，再问一遍它还这么判
          归谁      Riley 的标注 + golden set + eval，不在这个文件里


━━━━━━━━━━━━━━━━━━ 二、为什么切成这几层 ━━━━━━━━━━━━━━━━━━

两条分界线，上面那张表就是被它们切出来的。

第一条：这个错跟「模型说了什么」有没有关系。

    ① 层的错跟内容完全无关（网线断了、被限流了），所以原样重发是对的，
    改 prompt 反而是错的。
    ③④⑤ 层是模型答得不对，不改 prompt 的重发一定拿回同样的坏回答 ——
    temperature 是 0，同样的输入给同样的输出。所以那几层重问时一定要附上
    「你上一轮错在哪」，补的是信息不是随机性（这也是为什么重问不去动
    temperature：温度设成 0 是为了同一批数据重跑给同样的分，回归对照
    才有立足点）。

    这两种「重试」名字一样、性质相反，必须是两套预算、两套代码。混在一起
    的话，要么给网络错误加了没用的 prompt，要么给内容错误做了注定白跑的
    重发 —— 而两种都不会报错。

第二条：这个错能不能定位到某一家。

    ④⑤ 能 —— 就是那一家的那一维、那一句。别人家的判断没受影响，照用。
    ③ 不能 —— 公司集合都对不上了，「哪句话是关于谁的」整体存疑，
    剩下那些看起来正常的也不能挑着用。
    ② 是第三种情况：整批连回答都没有，无所谓定位。

    定位得到某一家，也不能再往下切。看起来可以只让它重写引用、保住
    分数，但那是给幻觉洗白：引用是编的，说明那个分数是从一段不存在的话里
    读出来的，配一句真引用之后它反而看起来干净了 —— 比两者单独存在都坏。
    分数越界同理：一家连 0-10 都没遵守，它同一次生成的 analysis 也不值得信。
    所以重问的最小单位是「一家公司的整条答案」。

    也不按错误类型切。一家公司可以同时既编了引用又分数越界，按类型拆成
    两次请求的话，这家会在两次里各出现一次、拿回两份冲突的答案，然后需要
    一条合并规则 —— 一个纯粹由「按类型切」这个决定凭空造出来的问题。


━━━━━━━━━━━━━━━━━━ 三、我们怎么处理 ━━━━━━━━━━━━━━━━━━

    ①②  -> _call_model 抛 LlmCallFailed，带上是哪一批的哪几家。
            五种情况共用一个类型，因为它们对我们的意义相同：我们自己重试
            没有意义。① 是 SDK 已经重试过并放弃（再叠一层只会让重试次数
            相乘、让定时任务拖更久），② 是重发一模一样的请求只会得到
            一模一样的结果，要改的是 batch_size 或 prompt。
            这一批算失败；要不要记成 partial 是 pipeline 那一层的事
            （runs 表有这个状态），不在这里判。

    ③   -> parse_assessments 抛 BatchIdentityMismatch，带漏 / 多 / 重三份键。
            assess_batch 接住，整批重问。说明里三种分三节写，因为它们要模型
            做的事不一样：漏了要补、多了要删、重了要合。

    ④⑤ -> 攒成 Complaint(kind, detail)，只重问不合格的那几家，已经过关的
            不再动它。kind 是 models.AssessmentProblem，四个值。
            ④ 在 parse_assessments 查、⑤ 在 _check_content 查，分界线是
            「查它需要什么数据」不是「这错有多严重」：④ 只看回答本身就够，
            ⑤ 要拿 cluster 的原文来比。

    ⑥   -> 不处理，也处理不了。

    ③ 和 ④⑤ 各有各的预算（identity_retries / content_retries），不共用。
    共用一个数的话，一次身份错会把本该留给引用的机会一起吃掉 —— 身份重问
    成功之后才发现引用是编的，这时候预算已经空了，而那两件事根本没关系。
    分开也不会让次数相乘：两个计数器各自单调递减、都不重置，所以一批最多
    调用 1 + identity_retries + content_retries 次，是相加。
    （会相乘的是「每进一个新阶段就把预算重置一遍」那种写法，不是这种。）

    重问用完还不合格的那几家不静默丢，交给调用方 —— 数量涨起来说明
    prompt 或者证据材料出了问题。

关于我们自己的硬数字（MOQ、认证、产能）只能引 config/facts.json，
那是另一道闸门，管的是「关于我们的话不许编」；上面 ⑤ 管的是
「关于对方的话不许编」。两半合起来才是完整的防幻觉。

批量调用，不是一家一个请求：扇出要多几十倍请求 + 一个 reducer +
一堆难看的 trace。分批在 analyze 里做，batch_size 在 config/scoring.yaml。
卡住批大小的是输出不是输入：实测 133 家的 user 消息约 53k tokens，
gpt-4o-mini 的窗口装得下；但每家的回答约 110 tokens，max_tokens=1500
只装得下十几家，超了就是 JSON 被截断成半个、整批白跑。

重问的最小单位是「一家公司的整条答案」，往上按公司挑、往下不再切。
三条理由（止损、不矛盾、省钱）和为什么不能只重写引用，都在 assess_batch。

这一层不连库、不认识供应商字段，也不 import slack（check_isolation 守着）。
OpenAI 客户端从参数传进来，不在这里构造：构造它要读密钥，而读密钥的模块
没法在没有密钥的机器上跑测试，纯计算的那几个函数会跟着一起测不了。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yaml

from discovery.prompts import (
    ANALYZER_SYSTEM,
    IDENTITY_DUPLICATED,
    IDENTITY_EXTRA,
    IDENTITY_INTRO,
    IDENTITY_MISSING,
    PROBLEM_HEADINGS,
    RETRY_INTRO,
)
from models import (
    Assessment,
    AssessmentProblem,
    CompanyCluster,
    DeterministicScores,
    LeadType,
    LlmAssessment,
    locate_quote,
)

DEFAULT_SCORING_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "scoring.yaml"
DEFAULT_FACTS_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "facts.json"


@dataclass(frozen=True)
class ScoringConfig:
    """config/scoring.yaml 摊平之后的样子。

    摊平是为了让调用处不用记哪个数在 yaml 的第几层。权重和映射表分开放，
    因为它们改动的频率不一样：权重要等 Riley 标够了才反推，映射表现在就能调。

    现在这份配置的实际值：

        llm_weights          {"icp_fit": 25, "signal_strength": 20,
                              "seniority": 10, "moq_fit": 15}
        det_weights          {"recency": 10, "reachability": 10,
                              "corroboration": 10}
        recency_days         ((3, 10), (7, 8), (14, 6), (30, 4), (90, 2), (3650, 0))
        corroboration_count  ((4, 10), (3, 8), (2, 6), (1, 3))
        reachability         {"has_domain": 10, "has_linkedin_url": 6,
                              "name_only": 2}
        off_category_cap     30
        model                "gpt-4o-mini"
        max_tokens           1500
        temperature          0.0
        content_retries      1
        identity_retries     1
        batch_size           10
        request_timeout_seconds  60

    两组权重加起来必须是 100，weighted_score 会当场查（不是在这里查，
    因为配置读进来时还不知道调用方要不要用它）。
    """

    llm_weights: dict[str, int]
    det_weights: dict[str, int]
    recency_days: tuple[tuple[int, int], ...]  # (max_age_days, score)，从新到旧
    corroboration_count: tuple[tuple[int, int], ...]  # (min_count, score)，从多到少
    reachability: dict[str, int]

    # 品类完全不对（icp_fit == 0）那一档的总分上限。它是闸门不是权重，
    # 理由见 weighted_score 结尾那一段。
    off_category_cap: int

    model: str
    max_tokens: int
    temperature: float
    content_retries: int
    identity_retries: int
    batch_size: int
    request_timeout_seconds: float


def load_scoring(path: pathlib.Path | str = DEFAULT_SCORING_PATH) -> ScoringConfig:
    """读 config/scoring.yaml。

    输入：配置文件路径
    输出：一个 ScoringConfig

    raw 是三层嵌套的字典（weights / deterministic / llm），
    映射表在 yaml 里是一串小字典，这里转成元组对，顺序原样保留 ——
    下面按顺序取第一个满足的档，顺序就是语义。

    用下标不用 get，跟 prefilter.load_rules 同一条理由：键缺了当场 KeyError，
    而不是退化成「这一维恒等于 0」那种不报错的错。
    """
    # raw 是整个 yaml，三个嵌套的顶层键加一个平的：
    #     {"weights": {...}, "deterministic": {...}, "llm": {...},
    #      "off_category_cap": 30}
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))

    # weights 再分两组，两组的键名分别对应 LlmAssessment 和
    # DeterministicScores 的字段名：
    #     {"llm": {"icp_fit": 25, "signal_strength": 20,
    #              "seniority": 10, "moq_fit": 15},
    #      "det": {"recency": 10, "reachability": 10, "corroboration": 10}}
    weights = raw["weights"]

    # det 是三张映射表。前两张在 yaml 里是一串小字典，
    # 第三张是平的三个键：
    #     {"recency_days": [{"max_age_days": 3, "score": 10}, ...],
    #      "corroboration_count": [{"min_count": 4, "score": 10}, ...],
    #      "reachability": {"has_domain": 10, "has_linkedin_url": 6,
    #                       "name_only": 2}}
    det = raw["deterministic"]

    # llm 是调模型的那几个参数：
    #     {"model": "gpt-4o-mini", "max_tokens": 1500, "temperature": 0,
    #      "content_retries": 1, "identity_retries": 1,
    #      "request_timeout_seconds": 60, "batch_size": 10}
    llm = raw["llm"]

    # 下面把两张小字典的表转成元组对，顺序原样保留：
    #     recency_days        ((3, 10), (7, 8), (14, 6), (30, 4), (90, 2), (3650, 0))
    #     corroboration_count ((4, 10), (3, 8), (2, 6), (1, 3))
    # 转元组是为了它能进 frozen dataclass，顺序就是取档顺序。

    return ScoringConfig(
        llm_weights=weights["llm"],
        det_weights=weights["det"],
        recency_days=tuple((row["max_age_days"], row["score"]) for row in det["recency_days"]),
        corroboration_count=tuple(
            (row["min_count"], row["score"]) for row in det["corroboration_count"]
        ),
        reachability=det["reachability"],
        off_category_cap=int(raw["off_category_cap"]),
        model=llm["model"],
        max_tokens=llm["max_tokens"],
        temperature=llm["temperature"],
        content_retries=llm["content_retries"],
        identity_retries=llm["identity_retries"],
        batch_size=llm["batch_size"],
        request_timeout_seconds=llm["request_timeout_seconds"],
    )


def load_facts(path: pathlib.Path | str = DEFAULT_FACTS_PATH) -> dict[str, Any]:
    """读 config/facts.json —— 关于我们自己的、可核实的事实。

    输入：文件路径
    输出：解析出来的字典，下划线开头的键（注释和待办）原样留着

    留着下划线那几条是故意的：它们会跟着进 prompt。`_todo` 那条列的是
    「交期、打样费这些我们还没有准确数字」，让模型看见「这几项没有值」，
    比让它自己补一个像样的数要好。
    """
    # 返回的字典长这样（省略了大部分）：
    #     {"_comment": [...],
    #      "moq": {"value": 500, "unit": "units",
    #              "claim": "minimum order quantity of 500 units", ...},
    #      "categories": {"items": ["microphones", "headsets", ...], ...},
    #      "certifications": {"items": ["FCC", "CE", "UKCA", ...], ...},
    #      "company": {"years_of_engineering": "15+", ...},
    #      "_todo": ["Lead time, tooling and sample cost ...", ...]}
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


# ─────────────────────────────────────────────── 确定性那三维


def _table_lookup(table: Sequence[tuple[int, int]], value: int, *, ascending: bool) -> int:
    """在映射表里取第一个满足的档。

    输入：(阈值, 分数) 的有序表；要查的值；阈值是「不超过」还是「不小于」
    输出：那一档的分数

    ascending=True  表是「不超过多少天」，从小到大排，取第一个 value <= 阈值
    ascending=False 表是「至少几件事」，从大到小排，取第一个 value >= 阈值

    两种表合用一个函数，是因为它们的形状一样、只有比较方向不同。
    分开写两份的话，哪天改了其中一份的取档规则（比如从「第一个满足」
    改成「最接近」），另一份会静静地留在旧规则上。

    表兜不住就炸，不返回 0。返回 0 的话，一个配错的表会表现成
    「这一维所有线索都是 0 分」，而那看起来跟「确实都不新」一模一样。
    """
    # 一轮一轮往下走，threshold / score 例如先是 (3, 10) 再是 (7, 8)。
    # 升序表查 5 天：3 不满足（5 <= 3 假），7 满足，返回 8。
    # 降序表查 2 件事：4 不满足（2 >= 4 假），3 不满足，2 满足，返回 6。
    for threshold, score in table:
        if (value <= threshold) if ascending else (value >= threshold):
            return score

    # 走到这里说明每一档都不满足，例如升序表最大只到 90 天而 value 是 200。
    raise ValueError(f"映射表兜不住 {value}，表是 {table}")


def deterministic_scores(
    cluster: CompanyCluster, *, config: ScoringConfig, now: datetime
) -> DeterministicScores:
    """代码算的那三维。纯函数，同样的输入永远给同样的分。

    输入：一个公司簇；配置；当前时刻（从参数进，不在函数里取）
    输出：DeterministicScores

    now 从参数进是为了可回归：拿三个月前那批数据重跑，recency 要能算出
    当时的分数，而不是「现在离那时候多久」。函数里调 datetime.now() 的话，
    同一批数据每天跑出来的分都不一样，回归对照就没有立足点。

    三维各举一个例子（时刻按 event_at 算，转发取原帖发布时刻）：

        某公司昨天刚宣布众筹成功，有 LinkedIn 公司 id 和官网，
        两条互不相干的帖子都提到它
            recency 10（1 天，落在 <=3 那档）
            reachability 10（有 LinkedIn url和domain）
            corroboration 6（2 件事）

        某公司名字是从职位串里解析出来的，只有名字，
        帖子是两个月前的，只有一条
            recency 2（60 天，落在 <=90 那档）
            reachability 2（name_only）
            corroboration 3（1 件事）
    """
    # newest 是这一簇里最近发生的那件事，例如
    #     datetime(2026, 8, 27, 3, 14, tzinfo=UTC)
    # 取最大不取平均：一家公司上个月发过一条、昨天又发一条，时效按昨天算 ——
    # 平均会让老信号把新信号拖下去，而我们要找的正是「现在正在发生的事」。
    newest = max(signal.event_at for signal in cluster.signals)

    # age_days 例如 1（now 是 2026-08-28、newest 是 08-27）或 60（两个月前）。
    # 负数说明数据源给了未来时刻，那是脏数据不是「特别新」，
    # 钳到 0 让它落进最新那一档，别让它绕过表。
    age_days = max(0, (now - newest).days)

    # recency 例如 age_days=1 -> 10，age_days=60 -> 2。
    recency = _table_lookup(config.recency_days, age_days, ascending=True)

    # cluster.corroboration 是按 fact_hash 去重之后的独立事实数，最小是 1，
    # 例如 2（两条互不相干的帖子都提到这家公司）。
    # 映射之后 corroboration 是 6；只有一条证据时是 1 -> 3 分。
    corroboration = _table_lookup(
        config.corroboration_count, cluster.corroboration, ascending=False
    )

    # 够不够得着，三档从强到弱，各举一个：
    #     ref.domain="lumen.example"                        -> 10
    #     只有 linkedin_url=".../company/lumen/"         -> 6
    #     只有 name="shenzhen xx audio"（职位串解析出来的）-> 2
    # domain 排在 LinkedIn URL 前面，是因为有官网就能找到邮箱和具体的人，
    # 而公司页只能证明这家公司存在。现在 domain 那一档实际拿不到值 ——
    # post search 不给官网，等 enrich。
    if cluster.ref.domain:
        reachability = config.reachability["has_domain"]
    elif cluster.ref.linkedin_url:
        reachability = config.reachability["has_linkedin_url"]
    else:
        reachability = config.reachability["name_only"]

    return DeterministicScores(
        recency=recency, reachability=reachability, corroboration=corroboration
    )


def weighted_score(llm: LlmAssessment, det: DeterministicScores, config: ScoringConfig) -> int:
    """七维加权成 0-100。

    输入：LLM 那四维；代码算的那三维；配置里的权重
    输出：0-100 的整数

    每一维是 0-10，乘以权重之后除以 10，所以权重之和就是满分。
    权重和恰好是 100 时满分正好是 100 —— 不在这里强制它等于 100，
    而是下面 assert 它，因为「权重加起来不是 100」是配置写错，
    悄悄按比例缩放会让人以为分数还是那个分数。

    品类完全不对的那一档不参与加权，直接封顶，理由见下面那一段。

    三个例子：

        各维都拿满
            输入   llm icp10 signal10 seniority10 moq10 / det 各 10
                   llm 700 + det 300 = 1000
            输出   100

        中等线索
            输入   llm icp8 signal8 seniority0 moq8 / det rec6 reach6 corr3
                   llm 200+160+0+120 = 480，det 60+60+30 = 150，合计 630
            输出   63

        品类完全不对，但别的维度都很高
            输入   llm icp0 signal8 seniority8 moq8 / det rec4 reach2 corr6
                   加权算出来是 48，超过封顶值
            输出   封顶值（配置里那个数）
    """
    # total_weight 现在是 25+20+10+15 + 10+10+10 = 100。
    # 不等于 100 就是配置写错，例如有人把 icp_fit 从 25 改成 24 忘了补别处。
    total_weight = sum(config.llm_weights.values()) + sum(config.det_weights.values())
    if total_weight != 100:
        raise ValueError(f"权重之和是 {total_weight}，不是 100。改 config/scoring.yaml")

    # points 是加权前的总点数，满分 1000。
    # 拿 Chatterly 那条真数据（icp10 sig8 sen10 moq8 / rec10 rea6 cor3）算：
    #     10*25 + 8*20 + 10*10 + 8*15 = 630
    #     10*10 +  6*10 +  3*10       = 190
    #     points = 820 -> 返回 82
    points = (
        llm.icp_fit * config.llm_weights["icp_fit"]
        + llm.signal_strength * config.llm_weights["signal_strength"]
        + llm.seniority * config.llm_weights["seniority"]
        + llm.moq_fit * config.llm_weights["moq_fit"]
        + det.recency * config.det_weights["recency"]
        + det.reachability * config.det_weights["reachability"]
        + det.corroboration * config.det_weights["corroboration"]
    )
    # 整除 10 把 0-1000 收回 0-100。余数直接丢：分数是给人看的排序依据，
    # 保留小数只会让「79.4 和 79.6 差在哪」变成一个没人答得上来的问题。
    score = points // 10

    # 品类完全不对的直接封顶。
    #
    # 为什么不能只靠权重压：icp_fit 的权重是它自己那一维的上限，不是别人的
    # 天花板。icp_fit 拿 0 分时，剩下六维仍然能挣到「100 减去 icp_fit 权重」
    # 那么多分 —— 而事件很硬、说话人是老板、MOQ 也合得上的公司到处都有，
    # 它们只是做的东西跟我们无关。也就是说品类不对的公司照样能过线，
    # 而「不合格的绝对不放上来」是这个项目的头一条规矩。
    #
    # 加权改成乘法（拿 icp_fit 当系数）也能压住，但那样每一维都不再有
    # 「这一维值多少分」这个可以对着表回答的含义，而映射表刻意写成表
    # 不写成公式就是为了这个。所以这里只做一件事：判据成立就封顶，
    # 别的分数原样算完再比 —— 报表上仍然看得出这家「除了品类之外有多硬」。
    #
    # 封顶值要低于 review_score（那一档是攒给 Riley 标注的边界样本），否则
    # 品类不对的会挤进她要看的那批。这两个数在两份配置里（封顶在
    # scoring.yaml，review_score 在 discovery.yaml），所以这个约束不在这里
    # 检查 —— 检查放在 trigger 读完全部配置那一步，那里两个数都在手上。
    # 不把 review_score 抄进 scoring.yaml：一个数抄两份，改了一处之后
    # 「过线」和「按过线分档」会用两个数，那个坑在 qualified_score 上避过一次。
    if llm.off_category:
        return min(score, config.off_category_cap)

    return score


# ─────────────────────────────────────────────── 喂给模型的那份材料


def company_key(cluster: CompanyCluster) -> str:
    """这一批里指代一家公司用的键。

    输入：一个公司簇
    输出：形如 "li_id:100000001" 或 "name:shenzhen xx audio" 的串

    要一个字符串而不是直接用下标，是因为模型偶尔会漏答一家或者把顺序打乱。
    用下标的话漏答会静静地错位，把甲公司的判断安到乙公司头上；用键的话
    对不上就是对不上，当场能发现。
    """
    # identity_key 是梯度上取到的最强那一档，例如
    #     ("li_id", "100000001")        被 @ 出来的公司，身份最硬
    #     ("name", "shenzhen xx audio") 职位串解析出来的，身份最弱
    kind, value = cluster.ref.identity_key

    # 拼成 "li_id:100000001" / "name:shenzhen xx audio"。
    return f"{kind}:{value}"


def _evidence_item(
    *, posted_at: datetime, speaker: str, headline: str, text: str, channels: set[LeadType]
) -> dict[str, Any]:
    """把一段正文摊成模型看到的一条证据。

    输入：这段话的发布时刻；说话的人和他的职位串；正文；这一段命中的通道
    输出：一条证据字典

    一个例子（发帖人自己就是这家公司的人）：

        输入 posted_at=datetime(2026, 8, 26, 3, 14, tzinfo=UTC),
             speaker="Jane Doe", headline="Founder at Chatterly",
             text="We just wrapped our Kickstarter for the offset-driver ...",
             channels={LeadType.AUTHOR}
        输出 {"posted_at": "2026-08-26",
              "speaker": "Jane Doe",
              "speaker_headline": "Founder at Chatterly",
              "speaker_is_this_company": True,
              "text": "We just wrapped our Kickstarter for the offset-driver ..."}

    另一个例子（第三方在谈这家公司，channels 里没有第一人称通道）：

        输入 posted_at=datetime(2026, 8, 24, 11, 2, tzinfo=UTC),
             speaker="Care Tech Social", headline="1,277 followers",
             text="Samsung Electronics's new watches score your heart ...",
             channels={LeadType.MENTIONED}
        输出 {"posted_at": "2026-08-24",
              "speaker": "Care Tech Social",
              "speaker_headline": "1,277 followers",
              "speaker_is_this_company": False,
              "text": "Samsung Electronics's new watches score your heart ..."}

    speaker_is_this_company 是这条数据里最要紧的一个字段，它决定 seniority
    那一维该看谁。没有它的话「1,277 followers 这个聚合号发帖提到了 Samsung」
    会被读成「Samsung 有人在说话」，而那条信息量是零。实测这一跑 133 家里
    有 96 家（72%）只有第三方通道，所以这不是边角情况，是主流情况。

    判断用的是 LeadType.is_first_person，不在这里自己列一个集合 ——
    draft 那一期要用同一个分组分开场句，两处各列一份的话哪天漂了，
    就是两层对同一条证据做出不同判断，而且不报错。

    正文截断到 1200 字符：LinkedIn 长帖后半段基本是话题标签和口号，
    而一批十家的话前面的部分才是预算所在。截断点写在这里不写进配置，
    因为它跟 prompt 的形状绑在一起，单独调没有意义。
    截断只影响给模型看的部分，验引用时用的是完整原文（models.unverified_quotes），
    方向是安全的：模型只引得到它看得见的那一截，那是完整原文的子集。
    """
    return {
        "posted_at": posted_at.date().isoformat(),
        "speaker": speaker,
        "speaker_headline": headline,
        "speaker_is_this_company": any(channel.is_first_person for channel in channels),
        "text": text[:1200],
    }


def _brief(cluster: CompanyCluster) -> dict[str, Any]:
    """把一家公司摊成喂给模型的那份材料。

    输入：一个公司簇
    输出：一个字典，进 prompt 的 JSON

    一个例子。这家公司有两条证据，一条是自己发的、一条是别人提到的：

        输入  CompanyCluster(ref=CompanyRef("Chatterly", "100000001", url, None),
                             candidates=[AUTHOR 的候选, MENTIONED 的候选])
        输出  {"company_key": "li_id:100000001",
               "company_name": "Chatterly",
               "found_via": ["author", "mentioned"],
               "independent_facts": 2,
               "evidence": [
                 {"posted_at": "2026-08-26", "speaker": "Jane Doe",
                  "speaker_headline": "Founder at Chatterly",
                  "speaker_is_this_company": True,
                  "text": "We just wrapped our Kickstarter for the ..."},
                 {"posted_at": "2026-08-24", "speaker": "Hardware Weekly",
                  "speaker_headline": "3,102 followers",
                  "speaker_is_this_company": False,
                  "text": "Five audio campaigns worth watching, including ..."}]}

    channels 是「证据行 -> 这个簇从这一行拿到的通道集合」，例如
        {("harvestapi_posts", "7231..."): {LeadType.AUTHOR, LeadType.MENTIONED}}
    通道挂在 candidates 上、正文挂在 signals 上，两边要在这里配回去 ——
    只给一个簇级的 found_via 集合，等于告诉模型「这里面有第一人称的证据」
    却不说是哪一条，那正是 CompanyCluster 存 Candidate 而不存两个字段
    要避免的事（见它的 docstring：存了通道却对不上证据，等于没存）。

    转发的信号出两条证据，各自记各自的说话人：外层那段是转发者写的，
    里面那段是原帖作者写的。两段共用转发者的名字是错的 —— 加速器转发
    被投公司的帖子时，原帖里「我们刚做完众筹」会被记在加速器头上，
    seniority 就判到了另一家公司的人身上。

    时刻也分两段：转发者那句话是 observed_at 说的，原帖是 event_at 发的。
    """
    # channels 从空字典开始，一条候选往里加一个通道。
    # 循环结束时它是 {证据行的 row_key -> 通道集合}，例如
    #     {("harvestapi_posts", "7231..."): {LeadType.MENTIONED}}
    channels: dict[tuple[str, str], set[LeadType]] = {}
    for candidate in cluster.candidates:
        channels.setdefault(candidate.signal.row_key, set()).add(candidate.lead_type)

    # evidence 从空列表开始，一条信号往里放一到两项。
    # 不转发放一项，转发放两项（转发者那句 + 原帖正文）。
    evidence: list[dict[str, Any]] = []
    for signal in cluster.signals:
        # here 是这个簇从这一行拿到的全部通道，例如 {LeadType.REPOST_MENTIONED}。
        here = channels[signal.row_key]

        # 外层那段：转发的话是转发者自己写的那句，不转发就是正文本身。
        # 通道要去掉描述原帖的那三条 —— 它们说的是里面那一段。
        evidence.append(
            _evidence_item(
                posted_at=signal.observed_at,
                speaker=signal.actor_name,
                headline=signal.actor_headline,
                text=signal.text,
                channels={c for c in here if not c.is_from_original_post},
            )
        )

        if signal.reposted_from is not None:
            # 里面那段：原帖，说话的人是原帖作者。
            # 内容通常在这一段里，转发者常常只写一句「So cool to see this」。
            evidence.append(
                _evidence_item(
                    posted_at=signal.event_at,
                    speaker=signal.reposted_from.actor_name,
                    headline=signal.reposted_from.actor_headline,
                    text=signal.reposted_from.text,
                    channels={c for c in here if c.is_from_original_post},
                )
            )

    return {
        "company_key": company_key(cluster),
        "company_name": cluster.ref.name,
        # 通道告诉模型这家公司是怎么被发现的。headline 那几条是从职位串里
        # 猜出来的，身份弱，模型该在 analysis 里说明，而不是当成确证。
        # 具体哪一条证据走的哪条通道，看 evidence 里的 speaker_is_this_company。
        "found_via": sorted({str(c.lead_type) for c in cluster.candidates}),
        "independent_facts": cluster.corroboration,
        "evidence": evidence,
    }


def build_messages(
    clusters: Sequence[CompanyCluster], facts: dict[str, Any], *, retry_note: str = ""
) -> list[dict[str, str]]:
    """拼出这一批的请求消息。纯函数，方便离线看 prompt 长什么样。

    输入：这一批公司簇；facts.json 的内容；重问时附加的说明（第一轮是空串）
    输出：OpenAI chat 的 messages 列表，两条

    一个例子（一家公司、不是重问那一轮）：

        输出 [{"role": "system",
               "content": "You score inbound-lead candidates for a hardware ..."},
              {"role": "user",
               "content": '{\n "COMPANY FACTS (the only hard numbers ...": {...},\n'
                          ' "candidates": [{"company_key": "li_id:100000001", ...}]\n}'}]

    user 那条是一个 JSON 串，两块 —— COMPANY FACTS 是关于我们自己的
    白名单，candidates 是这一批要判的公司。facts 整块给进去而不是挑几个字段，
    是因为 `_todo` 那种条目（「交期打样费我们还没有准确数字」）本身就是
    要让模型看见的信息：它得知道哪几项是没有值的，才不会自己补一个。

    三段话术全在 discovery/prompts.py，这个函数只负责拼装 —— 改措辞和改算法
    是两件事，不该出现在同一段代码里。
    """
    # payload 两块。第一块是 facts.json 整块，第二块是这一批要判的公司，
    # 一家一个 _brief。10 家的话 candidates 就是 10 项。
    payload = {
        "COMPANY FACTS (the only hard numbers you may cite about the ODM company)": facts,
        "candidates": [_brief(c) for c in clusters],
    }

    # user 是那份 JSON 的字符串。实测 10 家约 18k 字符、约 4.5k tokens。
    # indent=1 不是好看，是让模型少数几个空格 —— 压成一行反而更费 token。
    user = json.dumps(payload, ensure_ascii=False, indent=1)

    # 重问那一轮才会走这里，说明附在最后，例如开头是
    #     "Your previous answer had problems. Redo the answer for the ..."
    if retry_note:
        user = f"{user}\n\n{retry_note}"

    return [
        {"role": "system", "content": ANALYZER_SYSTEM},
        {"role": "user", "content": user},
    ]


# 显式 schema，不靠 prompt 求它返回 JSON。strict 模式要求每个属性都在
# required 里、且不许有额外字段，所以下面没有可选字段。
# strict 管不了的两件事在 parse_assessments 里查：0-10 的范围（不支持
# minimum/maximum），以及「答的是不是给它的那几家」（它根本不知道有哪几家）。
RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "lead_assessments",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["assessments"],
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "company_key",
                        "icp_fit",
                        "signal_strength",
                        "seniority",
                        "moq_fit",
                        "analysis",
                        "evidence_means",
                        "evidence_quotes",
                    ],
                    "properties": {
                        "company_key": {"type": "string"},
                        "icp_fit": {"type": "integer"},
                        "signal_strength": {"type": "integer"},
                        "seniority": {"type": "integer"},
                        "moq_fit": {"type": "integer"},
                        "analysis": {"type": "string"},
                        "evidence_means": {"type": "string"},
                        "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
        },
    },
}

_DIMENSIONS = ("icp_fit", "signal_strength", "seniority", "moq_fit")


# ─────────────────────────────────────────────── 三类错误


@dataclass(frozen=True)
class Complaint:
    """模型对某一家答得不合格，具体是哪一处。会原样写进重问说明给模型看。

    输入端：由 parse_assessments 和 _check_content 产生
    输出端：进 _retry_note 拼成给模型看的说明，或者进 assess_batch 的
            第二个返回值当丢弃理由

    kind 决定它进重问说明的哪一节，detail 是那一节里具体的一行。四种各举一个：

        Complaint(AssessmentProblem.SCORE_OUT_OF_RANGE, "icp_fit=11")
        Complaint(AssessmentProblem.EMPTY_ANALYSIS,     "analysis was empty")
        Complaint(AssessmentProblem.FABRICATED_QUOTE,   "they are raising a Series A")
        Complaint(AssessmentProblem.NO_QUOTE,           "no verbatim quote survived the check")

    做成两个字段而不是一句现成的话，是因为同一种问题要按 kind 归组 ——
    模型看「这三句都不是原文」比看三条各说各的更容易一次改对。

    四种共用一个类、不做成四个子类，判据是「代码拿它做什么」：归组、
    格式化成一行、最后当丢弃理由交出去 —— 三件事对每种 kind 都一模一样，
    差的只有文案，而文案在 prompts.PROBLEM_HEADINGS 里查表。
    子类是用来承载不同行为的，这里没有不同行为。

    留在 analyzer 不进 models：它是这一层重问时的记账。被计数、要进报表的
    是 kind，那个已经在 models.AssessmentProblem 里了。
    """

    kind: AssessmentProblem
    detail: str


class BatchIdentityMismatch(ValueError):
    """模型答的公司集合跟给它的那批对不上。整批的身份映射不可信。

    三个字段各举一个例子（都是相对于「给了 A 和 B 两家」而言）：

        missing     ["li_id:222"]      只答了 A
        extra       ["li_id:999999"]   答了 A、B 和一个不存在的公司
        duplicated  ["li_id:111"]      A 答了两遍，分数还不一样

    跟内容问题分开成一个异常类型，是因为两者该走的路不一样：
    某一家的引用编了，只重问那一家就行；而答错了公司集合说明这一轮
    「哪句话是关于谁的」整体存疑，剩下那些看起来正常的也不能挑着用。

    duplicated 是后来补的一条，堵一个静默的洞：判重如果只比集合，
    [A, A, B] 对 {A, B} 是相等的，然后解析时后一行覆盖前一行 ——
    两个不同的分数，静默取最后一个，报表上完全看不出来。

    三个都存成字段而不是只留一句话，是为了让重问说明能用英文分三节写给
    模型看（漏了要补、多了要删、重了要合，三件事不一样），
    同时 str(self) 保持中文写给人和日志看。
    """

    def __init__(self, missing: list[str], extra: list[str], duplicated: list[str]) -> None:
        """输入：三份键，各自可以是空列表，但不会三个都空（都空就不该抛）。

        例如给了 A 和 B 两家、模型只答了 A 还多编了一个：
            BatchIdentityMismatch(missing=["li_id:222"],
                                  extra=["li_id:999999"], duplicated=[])
        构造出来的 str(self) 是
            "模型答的公司对不上：漏了 ['li_id:222']，多出 ['li_id:999999']，重复 []"
        """
        self.missing = missing
        self.extra = extra
        self.duplicated = duplicated
        super().__init__(
            f"模型答的公司对不上：漏了 {missing[:5]}，多出 {extra[:5]}，重复 {duplicated[:5]}"
        )


class LlmCallFailed(RuntimeError):
    """这一批根本没拿回可用的回答。跟「回答拿到了但不合格」是两回事。

    company_keys 是这一批问的是哪几家，reason 是一句人话。各举一个：

        LlmCallFailed(["li_id:111", "li_id:222"],
                      "输出被 max_tokens=1500 截断，这一批 10 家 ...")
        LlmCallFailed(["li_id:111"], "请求失败：RateLimitError ...", status_code=429)

    所有「拿不到回答」的情况共用这一个类型，是为了让 pipeline 那一层
    能一句 except 就把这一批记成 partial（runs 表有这个状态）。而它们
    共同的性质是：**我们自己重问是没有意义的**。

        截断 / 拒答 / 空内容    重发一模一样的请求只会得到一模一样的结果，
                               要改的是 batch_size 或 prompt
        网络 / 限流 / 5xx      OpenAI SDK 自己已经退避重试过了（默认
                               max_retries=2，覆盖连接错误 / 408 / 409 /
                               429 / 5xx）。传到我们手上说明它已经放弃，
                               我们再叠一层退避只会让重试次数相乘、
                               让一次定时任务拖更久

    所以这一层的活不是重试，是**把上下文补上**：SDK 抛的异常里没有
    「这是哪一批的哪几家」，而排查「今天早报为什么少了 20 家」要从那儿开始。

    status_code 用 getattr 取，不 import openai：这一层拿到的 client 是从
    参数进来的任意对象（测试里就是个假客户端），认死一个供应商的异常类型，
    换实现或者测试时就都得跟着改。
    """

    def __init__(
        self, company_keys: list[str], reason: str, *, status_code: int | None = None
    ) -> None:
        """输入：这一批问的是哪几家；一句人话；HTTP 状态码（没有就是 None）。

        例如被限流：
            LlmCallFailed(["li_id:111", "li_id:222"],
                          "请求失败：RateLimitError: Rate limit reached",
                          status_code=429)
        构造出来的 str(self) 是
            "请求失败：RateLimitError: Rate limit reached（HTTP 429）。
             这一批 2 家：['li_id:111', 'li_id:222']"
        """
        self.company_keys = company_keys
        self.status_code = status_code
        where = f"（HTTP {status_code}）" if status_code is not None else ""
        super().__init__(f"{reason}{where}。这一批 {len(company_keys)} 家：{company_keys[:5]}")


# ─────────────────────────────────────────────── 查错：只看回答本身那一组


def parse_assessments(
    payload: dict[str, Any], clusters: Sequence[CompanyCluster]
) -> tuple[dict[str, LlmAssessment], dict[str, list[Complaint]]]:
    """把模型返回的 JSON 转成 LlmAssessment，顺便查它有没有乱答。

    输入：解析好的响应；这一批公司簇
    输出：(解析出来的 company_key 到 LlmAssessment, 不合格的那几家)

    输入长这样（三家的一批，其中一家分数越界）：

        payload  {"assessments": [
                    {"company_key": "li_id:100000001", "icp_fit": 10,
                     "signal_strength": 8, "seniority": 10, "moq_fit": 8,
                     "analysis": "Chatterly just closed a Kickstarter ...",
                     "evidence_quotes": ["We just wrapped our Kickstarter"]},
                    {"company_key": "li_id:97531", "icp_fit": 11, ...},
                    {"company_key": "name:shenzhen xx audio", ...}]}
        clusters 那三家的簇

    输出：

        ({"li_id:100000001": LlmAssessment(
              icp_fit=10, signal_strength=8, seniority=10, moq_fit=8,
              analysis="Chatterly just closed a Kickstarter for offset-driver "
                       "headphones and the founder is the one posting.",
              evidence_quotes=("We just wrapped our Kickstarter",)),
          "name:shenzhen xx audio": LlmAssessment(
              icp_fit=3, signal_strength=1, seniority=2, moq_fit=1,
              analysis="Contract manufacturer advertising its own OEM service.",
              evidence_quotes=("we help brands build their own earbuds",))},

         {"li_id:97531": [Complaint(AssessmentProblem.SCORE_OUT_OF_RANGE, "icp_fit=11")]})

    越界那一家不在第一项里 —— 它拼不出 LlmAssessment（0-10 是库里 CHECK 的
    范围），调用方会拿第二项去重问它。两项加起来一定等于这一批的家数。

    这一步查的是「只看回答本身就能判的」两种：分数越界、analysis 是空的。
    要拿原文比才能判的那两种（引用是编的、一条引用都没给）在
    _check_content 里，因为它需要 cluster 的 quotable_texts，而这里
    只需要键的集合。分界线是「查它需要什么数据」，不是「这错有多严重」。

    keys 是模型答的键的列表（不是集合，重复要数得出来），
    wanted 是这一批应该有的键的集合。模块开头 ③ 那三种（漏答、多答、
    同一家答两次）任意一种成立就抛 BatchIdentityMismatch、整批作废；
    每种各自的危害见那张表，为什么不能只重问几家见 BatchIdentityMismatch。

    用 Counter 不用 set 是这里唯一容易写错的一步：判重只比集合的话，
    [A, A, B] 对 {A, B} 是相等的，检查会通过。
    """
    # keys 是模型答的键，按它给的顺序，例如
    #     ["li_id:100000001", "li_id:97531", "li_id:97531"]
    # 用列表不用集合：重复要数得出来。
    keys = [row["company_key"] for row in payload["assessments"]]

    # wanted 是这一批应该有的键，例如
    #     {"li_id:100000001", "li_id:97531", "name:shenzhen xx audio"}
    wanted = {company_key(c) for c in clusters}

    # 三种身份问题，例如 missing=["name:shenzhen xx audio"]、
    # extra=["li_id:999999"]、duplicated=["li_id:97531"]。
    counts = Counter(keys)
    missing = sorted(wanted - counts.keys())
    extra = sorted(counts.keys() - wanted)
    duplicated = sorted(key for key, n in counts.items() if n > 1)

    if missing or extra or duplicated:
        raise BatchIdentityMismatch(missing, extra, duplicated)

    # out 装解析成功的那些，complaints 装不合格的那几家。
    # 一家不会同时进两边，两边加起来等于这一批的家数。
    out: dict[str, LlmAssessment] = {}
    complaints: dict[str, list[Complaint]] = {}

    # row 是模型给的一家，例如
    #     {"company_key": "li_id:100000001", "icp_fit": 10, ...,
    #      "evidence_quotes": ["We just wrapped our Kickstarter"]}
    for row in payload["assessments"]:
        key = row["company_key"]

        # bad 装这一家的问题，例如
        #     [Complaint(AssessmentProblem.SCORE_OUT_OF_RANGE, "icp_fit=11")]
        # 四维都查完再说，不只报第一个 —— 模型一次改一维会来回重问好几轮。
        bad = [
            Complaint(AssessmentProblem.SCORE_OUT_OF_RANGE, f"{dim}={row[dim]}")
            for dim in _DIMENSIONS
            if not 0 <= row[dim] <= 10
        ]

        # 空 analysis 也算不合格：Riley 要的是分项加理由，一条没有理由的线索
        # 她没法校准，而分数本身看不出这条缺了理由。
        if not row["analysis"].strip():
            bad.append(Complaint(AssessmentProblem.EMPTY_ANALYSIS, "analysis was empty"))

        # evidence_means 空的跟 analysis 空的同一档：都是「给了分数但没给
        # 理由」。它俩共用一个 problem kind，因为退回重问的说明是同一句话 ——
        # 为一种文案差别新建一个 kind，那是子类承载不同行为的用法，
        # 这里没有不同行为。
        if not row["evidence_means"].strip():
            bad.append(
                Complaint(AssessmentProblem.EMPTY_ANALYSIS, "evidence_means was empty")
            )

        if bad:
            complaints[key] = bad
            continue

        out[key] = LlmAssessment(
            icp_fit=row["icp_fit"],
            signal_strength=row["signal_strength"],
            seniority=row["seniority"],
            moq_fit=row["moq_fit"],
            analysis=row["analysis"],
            evidence_quotes=tuple(row["evidence_quotes"]),
            evidence_means=row["evidence_means"],
        )
    return out, complaints


# ─────────────────────────────────────────────── 查错：要拿原文比那一组


def _check_content(
    assessment: LlmAssessment, cluster: CompanyCluster
) -> tuple[LlmAssessment, list[Complaint]]:
    """查一家公司的引用真不真，顺便把引用换成原文那一段。防幻觉闸门的判断部分。

    输入：模型对这一家的判断；这一家的簇（拿它的原文来比）
    输出：(引用已经换成逐字原文的判断, 这一家的问题列表)
          问题列表是空的就表示这一家过了

    四种走向各举一个（这家公司的原文是 "We just wrapped our Kickstarter"）：

        逐字引对     输入 evidence_quotes=("We just wrapped our Kickstarter",)
                    输出 引用不变，问题 []
        顺手改了一点 输入 evidence_quotes=("we just wrapped our kickstarter.",)
                    输出 引用被换回 ("We just wrapped our Kickstarter",)，问题 []
        引用是编的   输入 evidence_quotes=("they are raising a Series A",)
                    输出 引用不变，问题 [Complaint(FABRICATED_QUOTE,
                                                "they are raising a Series A")]
        一条都没给   输入 evidence_quotes=()
                    输出 引用不变，问题 [Complaint(NO_QUOTE,
                                                "no verbatim quote survived the check")]

    「换回原文那一段」是实测逼出来的：一跑 20 家里 4 家（20%）被判成幻觉
    丢掉，而那四句跟原文的相似度都在 0.989 以上 —— 差的是模型顺手补的一个
    句号、或者一个大小写（"Linkwave" vs 原文的 "Linkwave"）。那不是编造。
    按幻觉丢掉它们，等于为了防编造扔掉五分之一的真线索。

    放宽的是「怎么找」，不是「找到算什么」：找的时候忽略大小写、空白多少和
    两端标点（见 models.locate_quote），存进 Assessment 的仍然是逐字原文。
    这比原先还严一点 —— 原先存的是模型写的版本，它可能带着那个多出来的
    句号，而那一句以后会被抄进给客户的邮件里。

    「至少一条」那条也是后来补的。原先空数组能合法穿过闸门 ——
    unverified_quotes 对空元组返回空列表，看起来跟「引用全对」一模一样。
    于是一条 74 分、零条原文引用的线索照样能推给 Riley，而分项加原文引用
    正是她校准判断的全部依据。而且旧的重问说明里那句「实在不行就留空数组」
    恰恰把它变成了模型最省事的合规路径。

    要求「至少一条」是永远做得到的：每个簇至少有一条信号、每条信号至少有
    一段正文，所以证据块不可能是空的。模型返回空数组只意味着它不肯落笔，
    不意味着没有东西可引。

    顺带堵掉空串：`"" in text` 恒为真，所以 evidence_quotes=("",) 过得了
    子串比对。这里直接跳过纯空白的引用 —— 它既不是编造（没声称任何事）
    也不是证据。跳过之后只给了空串就会走到 NO_QUOTE，而「一条空串加一条
    真引用」不会因为那条空串被打回重问。
    """
    # verified 装找回来的原文段，fabricated 装找不回来的那几句。
    # 一轮下来例如 verified=["We just wrapped our Kickstarter"]、fabricated=[]。
    verified: list[str] = []
    fabricated: list[str] = []
    for quote in assessment.evidence_quotes:
        # 纯空白的直接跳过，两边都不进。它不是编造（没声称任何事），
        # 也不是证据。跳过之后：只给了空串就会走到下面的 NO_QUOTE，
        # 而「一条空串加一条真引用」不会因为那条空串被打回重问。
        if not quote.strip():
            continue

        # found 是原文里对应的那一段（逐字），找不到就是 None。
        # 比对范围是这一簇所有信号的 quotable_texts（转发的两段都算），
        # 而且只跟这一家自己的原文比 —— 拿甲公司的话当乙公司的证据会被抓出来。
        found = locate_quote(quote, cluster)
        (fabricated if found is None else verified).append(quote if found is None else found)

    out = [Complaint(AssessmentProblem.FABRICATED_QUOTE, quote) for quote in fabricated]

    # 编了的话上面那几条已经在说「请引用原文」了，不用再补一条 NO_QUOTE。
    if not fabricated and not verified:
        out.append(
            Complaint(AssessmentProblem.NO_QUOTE, "no verbatim quote survived the check")
        )

    # 引用换成找回来的原文段。用 replace 生成新对象，因为 LlmAssessment 是
    # frozen —— 原地改会当场炸，而那正是我们要的：这一层不该偷偷动别的字段。
    return dataclasses.replace(assessment, evidence_quotes=tuple(verified)), out


# ─────────────────────────────────────────────── 把错误写回给模型


def _retry_note(complaints: dict[str, list[Complaint]]) -> str:
    """把上一轮的问题原样写回去，让模型看见自己错在哪一句上。

    输入：company_key 到这一家问题列表的字典
    输出：附在 user 消息后面的一段说明，按问题类型分节

    输入是两家、三个问题：

        {"li_id:100000001": [
             Complaint(AssessmentProblem.FABRICATED_QUOTE, "they are raising a Series A"),
             Complaint(AssessmentProblem.FABRICATED_QUOTE, "shipping to backers in Q4")],
         "name:shenzhen xx audio": [
             Complaint(AssessmentProblem.NO_QUOTE, "no verbatim quote survived the check")]}

    输出就是这一整段文本，原样接在 user 那个 JSON 串后面：

        Your previous answer had problems. Redo the answer for the companies below.

        These evidence_quotes are NOT verbatim substrings of that company's own
        evidence. Copy a shorter fragment character for character instead:
          li_id:100000001: 'shipping to backers in Q4'
          li_id:100000001: 'they are raising a Series A'

        These companies came back without a single usable quote. Every company
        needs at least one string copied character for character from its own
        evidence text - the evidence block is never empty, so there is always
        something to copy:
          name:shenzhen xx audio: 'no verbatim quote survived the check'

    四个细节都是故意的：

        编的话原样带回去   模型看得见自己编的是哪一句，比笼统说一句
                          「请引用原文」有用得多
        用 repr 加引号     引号和换行原样可见，模型能看出自己是不是多写了
                          一个空格、或者把换行换成了空格 —— 而子串比对
                          恰恰是一个字符都不能差
        按 kind 分节       同一种毛病归在一起，改一次就能改对一批。
                          节的顺序是 AssessmentProblem 的声明顺序，不是
                          字典顺序，所以同样的问题永远排在同样的位置
        排序输出           同样的 complaints 永远生成同样的文本。temperature
                          是 0，同样的 prompt 会拿到同样的回答，所以重问
                          必须改 prompt 才有意义 —— 而这段就是改动的那部分。
                          （这也是为什么重问不去动 temperature：温度是为了
                          可复现才设成 0 的，真正补上的信息是这段文字，
                          不是随机性）

    抬头的措辞在 prompts.PROBLEM_HEADINGS，这里只负责拼装。
    """
    # lines 从抬头开始，一节一节往下拼。
    lines = [RETRY_INTRO]

    # 按 AssessmentProblem 的声明顺序走，节的顺序因此是固定的。
    for kind, heading in PROBLEM_HEADINGS.items():
        # rows 是这一节的所有行，例如
        #     [("li_id:100000001", "shipping to backers in Q4"),
        #      ("li_id:100000001", "they are raising a Series A")]
        rows = sorted(
            (key, complaint.detail)
            for key, items in complaints.items()
            for complaint in items
            if complaint.kind == kind
        )
        if not rows:
            continue

        lines.append("")
        lines.append(heading)
        lines.extend(f"  {key}: {detail!r}" for key, detail in rows)

    return "\n".join(lines)


def _identity_note(err: BatchIdentityMismatch) -> str:
    """答错了公司集合时写回去的说明。整批重问用这一段。

    输入：抛出来的那个异常，带着漏答、多答、重复三份键
    输出：附在 user 消息后面的一段说明，漏 / 多 / 重各一节

    输入 BatchIdentityMismatch(missing=["name:shenzhen xx audio"],
                               extra=["li_id:999999"],
                               duplicated=["li_id:100000001"])
    输出：

        Your previous answer did not cover exactly the companies you were given.
        Missing - you must return an entry for each of these:
          'name:shenzhen xx audio'
        Not in the input - do not invent companies:
          'li_id:999999'
        Answered more than once - return exactly one entry per company:
          'li_id:100000001'

    三种错分开列，是因为它们要模型做的事不一样：漏了要补、多了要删、
    重了要合。合成一句「你答错了」的话，模型多半只改一种。

    措辞在 prompts.IDENTITY_*，这里只负责拼装。
    """
    lines = [IDENTITY_INTRO]
    for heading, keys in (
        (IDENTITY_MISSING, err.missing),
        (IDENTITY_EXTRA, err.extra),
        (IDENTITY_DUPLICATED, err.duplicated),
    ):
        if not keys:
            continue
        lines.append(heading)
        lines.extend(f"  {key!r}" for key in keys)
    return "\n".join(lines)


# ─────────────────────────────────────────────── 调模型


def _call_model(
    clusters: Sequence[CompanyCluster],
    *,
    facts: dict[str, Any],
    config: ScoringConfig,
    client: Any,
    note: str,
) -> dict[str, Any]:
    """发一次请求，把回来的 JSON 解出来。所有跟 SDK 打交道的事都在这里。

    输入：这一轮要问的公司簇；facts；配置；OpenAI 客户端；重问说明（第一轮空串）
    输出：解析好的响应字典，例如

        {"assessments": [
            {"company_key": "li_id:100000001", "icp_fit": 10,
             "signal_strength": 8, "seniority": 10, "moq_fit": 8,
             "analysis": "Chatterly just closed a Kickstarter for offset-driver "
                         "headphones and the founder is the one posting.",
             "evidence_quotes": ["We just wrapped our Kickstarter"]},
            {"company_key": "li_id:97531", "icp_fit": 0, "signal_strength": 2,
             "seniority": 4, "moq_fit": 0,
             "analysis": "Industrial coatings supplier. Wrong category.",
             "evidence_quotes": ["powder coating line"]}]}

    模块开头 ① 和 ② 那两层全部在这里查，五种情况一律抛 LlmCallFailed
    （SDK 抛的异常、输出被截断、模型拒答、没有内容、返回的不是合法 JSON）。
    共用一个类型的理由见那张表下面第三节，一句话是：这一批我们自己重试
    没有意义。

    所以这一层对传输错误做的事不是重试，是**把上下文补上** ——
    SDK 抛的异常里没有「这是哪一批的哪几家」，而排查「今天早报为什么少了
    20 家」正是要从那儿开始。下面每个 raise 的消息都带着该改哪个配置。

    超时是我们的，重试是 SDK 的。SDK 默认 read timeout 是 600 秒，
    14 批最坏能挂两小时以上，而这是个每天跑一次的定时任务 ——
    所以每次请求显式带上 config.request_timeout_seconds。
    重试次数和退避策略在构造 client 时定，那在这一层之外（客户端从参数进来）。

    捕 Exception 而不是认 openai 的异常类型，是因为这一层拿到的 client 是
    任意对象（测试里就是个假客户端）。认死一个供应商的类型，换实现或者
    写测试时都得跟着改，而这一层的全部价值只是补上下文，不需要知道类型。
    """
    # keys 只用来在出错时说清是哪一批，例如 ["li_id:100000001", "li_id:97531"]。
    keys = [company_key(c) for c in clusters]

    # 消息在 try 外面拼。圈进去的话，_brief 里抛个 KeyError 会被报成
    # 「请求失败：KeyError」—— 把我们自己的 bug 伪装成传输故障，
    # 而这两种的排查方向完全相反（一个查代码，一个查网络和额度）。
    messages = build_messages(clusters, facts, retry_note=note)

    try:
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout_seconds,
        )
    except Exception as err:
        # status_code 用 getattr 取：openai 的异常有这个属性，别的没有就是 None。
        raise LlmCallFailed(
            keys,
            f"请求失败：{type(err).__name__}: {err}",
            status_code=getattr(err, "status_code", None),
        ) from err

    # choice 是唯一那个候选回答（没要 n>1）。
    choice = response.choices[0]

    if choice.finish_reason == "length":
        raise LlmCallFailed(
            keys,
            f"输出被 max_tokens={config.max_tokens} 截断。每家的回答约 110 tokens，"
            f"改小 config/scoring.yaml 的 batch_size 或者调大 max_tokens",
        )

    # refusal 是结构化输出下模型拒答时才有值的字段，正常是 None。
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise LlmCallFailed(keys, f"模型拒答：{refusal}")

    content = choice.message.content
    if content is None:
        raise LlmCallFailed(keys, f"模型没有返回内容，finish_reason={choice.finish_reason}")

    try:
        return json.loads(content)
    except json.JSONDecodeError as err:
        # 走 strict schema 还解析不了，基本只剩「截断但 finish_reason 没报」
        # 这一种。原文截一段带上，不然只有一个「第 8 行第 12 列」没法查。
        raise LlmCallFailed(keys, f"返回的不是合法 JSON：{err}。开头是 {content[:120]!r}") from err


def assess_batch(
    clusters: Sequence[CompanyCluster],
    *,
    facts: dict[str, Any],
    config: ScoringConfig,
    client: Any,
) -> tuple[dict[str, LlmAssessment], dict[str, list[str]]]:
    """调模型给这一批打分，不合格的重问，只重问不合格的那几家。

    输入：这一批公司簇（不超过 config.batch_size 家）；facts；配置；客户端
    输出：(过了全部闸门的 assessment, 重问完还是不合格的那几家)

    传三家、其中一家怎么问都在编，返回长这样：

        ({"li_id:100000001": LlmAssessment(
              icp_fit=10, signal_strength=8, seniority=10, moq_fit=8,
              analysis="Chatterly just closed a Kickstarter for offset-driver "
                       "headphones and the founder is the one posting.",
              evidence_quotes=("We just wrapped our Kickstarter",)),
          "name:shenzhen xx audio": LlmAssessment(
              icp_fit=3, signal_strength=1, seniority=2, moq_fit=1,
              analysis="Contract manufacturer advertising its own OEM service.",
              evidence_quotes=("we help brands build their own earbuds",))},

         {"li_id:97531": ["they are raising a Series A",
                          "they closed a $12M round"]})

    两项不重叠，加起来正好是传进来的家数。第二项那一家的两条是它两轮各编
    了一次，两条都留着不是 bug —— 它记录的是「问了两次都在编」这件事本身。

    两条路，判据是「这次回答的身份映射还可信吗」（③ 走一条、④⑤ 走另一条，
    分层和分界线见模块开头）：

        身份映射不可信   BatchIdentityMismatch -> 整批重问，pending 不变
        身份映射可信     只是某几家内容不合格   -> pending 缩成那几家

    除了模块开头讲的那两条边界，只重问坏的那几家还有两个独立的好处：

        止损     已经过关的再问一遍等于重新掷一次骰子，一家本来引用也对的
                 公司可能第二轮就编了一句。重问只可能让它变差，不可能变好
        省钱     每多一次请求就多付一遍固定开销：system 约 0.5k tokens
                 加 facts 整块约 1.1k

    跨家之间没有需要一起判断的东西（每家的分只跟它自己的证据有关），
    所以拆开重问在语义上是安全的。

    有五样东西一轮一轮变：

        settled        已经过关的，只增不减
        pending        这一轮要问的。整批重问时不变，只重问那几家时变短
        note           附在 prompt 后面的说明，第一轮是空串
        identity_left  答错公司集合还能重问几次
        content_left   某几家内容不合格还能重问几次

    两个预算分开、都只减不加，这是这个循环唯一容易写错的地方。
    共用一个数的话会出现这条路：第一轮答错了公司集合、整批重问，第二轮
    公司对上了但引用是编的 —— 这时候预算已经被身份那次吃光，引用那一关
    一次机会都没得到。而这两件事根本没关系。

    一条走满两个预算的实际路径（identity_retries=1、content_retries=1）：

        第 1 次调用  10 家   漏答了 1 家 -> identity_left 1->0，整批重问
        第 2 次调用  10 家   公司对上了，其中 2 家引用是编的
                            -> 8 家进 settled，content_left 1->0，
                               pending 缩成那 2 家
        第 3 次调用   2 家   1 家改对了进 settled，另 1 家还在编
                            -> content_left 已经是 0，把它交给调用方丢弃
        返回          settled 9 家，discarded 1 家

    最多 1 + identity_retries + content_retries 次调用，是相加不是相乘 ——
    每一轮要么返回、要么抛、要么花掉两个预算里的一个，两个都不重置。

    重问次数用完还不合格的那几家进第二个返回值，调用方丢弃、不给 Riley 看。
    不静默丢：丢的是「模型对这家反复答不好」，那本身是关于模型和这条线索的
    信息。数量涨起来说明 prompt 或者证据材料出了问题。
    """
    if len(clusters) > config.batch_size:
        # 分批是 analyze 的活。这里只拦一下，因为「一次请求」这个前提
        # 一旦破了，症状是输出被截断、整批白跑，而那看起来像模型抽风。
        raise ValueError(
            f"一次给了 {len(clusters)} 家，超过 batch_size={config.batch_size}。"
            f"分批应该在 analyze 里做"
        )
    if not clusters:
        return {}, {}

    # by_key 是键到簇的反查表，验引用和挑出要重问的那几家都要用它，例如
    #     {"li_id:100000001": CompanyCluster(ref=Chatterly, candidates=[...])}
    by_key = {company_key(c): c for c in clusters}

    settled: dict[str, LlmAssessment] = {}
    pending: list[CompanyCluster] = list(clusters)
    note = ""

    # 两个预算各管各的，都只减不加、不重置。
    # identity_left  答错公司集合时整批重问还剩几次
    # content_left   某几家内容不合格时只重问那几家还剩几次
    identity_left = config.identity_retries
    content_left = config.content_retries

    # 每一轮要么返回、要么抛、要么花掉两个预算里的一个，所以调用次数的
    # 上界就是「一次成功的 + 两个预算」，相加不相乘。写成 range 而不是
    # while True，是为了这个上界在代码里看得见。
    max_calls = 1 + config.identity_retries + config.content_retries

    for _ in range(max_calls):
        payload = _call_model(pending, facts=facts, config=config, client=client, note=note)

        try:
            # parsed 是解析出来的那些，complaints 先只装「只看回答就能判」的
            # 那两种（分数越界、analysis 空）。
            parsed, complaints = parse_assessments(payload, pending)
        except BatchIdentityMismatch as err:
            # 身份映射不可信，整批重问，pending 不变。
            # 花的是 identity 的预算，不动 content 的 —— 引用那一关
            # 还一次都没考过，不该因为模型点错了名就失去机会。
            if identity_left == 0:
                raise
            identity_left -= 1
            # note 必须换掉：temperature 是 0，一模一样的 prompt 会拿到
            # 一模一样的坏回答，不改 prompt 的重问是纯粹浪费一次请求。
            note = _identity_note(err)
            continue

        # 第二组检查：逐家验引用（要拿原文比，所以在这里不在上面）。
        # 过了的进 settled，没过的并进 complaints。
        for key, assessment in parsed.items():
            # checked 是引用已经换成逐字原文的那一份，进 settled 的是它 ——
            # 模型顺手加的句号不该跟着走到给客户的邮件里。
            checked, problems = _check_content(assessment, by_key[key])
            if problems:
                complaints.setdefault(key, []).extend(problems)
            else:
                settled[key] = checked

        # 这一轮全过。settled 此时装着历轮累计的全部结果。
        if not complaints:
            return settled, {}

        # 内容预算用完，把还不合格的那几家交出去，调用方丢弃。
        if content_left == 0:
            return settled, {
                key: [c.detail for c in items] for key, items in sorted(complaints.items())
            }
        content_left -= 1

        # 还有机会：下一轮只问不合格的那几家，说明带着它们错在哪。
        # 排序是为了同样的输入每次跑出同样的顺序。
        pending = [by_key[key] for key in sorted(complaints)]
        note = _retry_note(complaints)

    raise AssertionError(
        f"跑满了 {max_calls} 次还没收手。每一轮都该返回、抛出、或者花掉一个预算，"
        f"走到这里说明有一条路忘了动预算"
    )


def analyze(
    clusters: Sequence[CompanyCluster],
    *,
    facts: dict[str, Any],
    config: ScoringConfig,
    client: Any,
    now: datetime | None = None,
) -> tuple[dict[str, Assessment], dict[str, list[str]]]:
    """给这一批公司打完整的七维分。这一层的入口，分批在这里做。

    输入：这一跑全部公司簇（多少家都行，里面会分批）；facts；配置；
          OpenAI 客户端；当前时刻
    输出：(company_key 到 Assessment, 反复答不好被丢弃的那几家)

    传进去 3 家、其中 1 家反复编引用的话，返回的两项长这样：

        ({"li_id:100000001": Assessment(
              llm=LlmAssessment(icp_fit=10, signal_strength=8, seniority=10,
                                moq_fit=8,
                                analysis="Chatterly just closed a Kickstarter ...",
                                evidence_quotes=("We just wrapped our Kickstarter",)),
              det=DeterministicScores(recency=10, reachability=6, corroboration=3),
              score=82),
          "name:shenzhen xx audio": Assessment(
              llm=LlmAssessment(icp_fit=3, signal_strength=1, seniority=2, moq_fit=1,
                                analysis="Contract manufacturer advertising ...",
                                evidence_quotes=("we help brands build their own "
                                                 "earbuds",)),
              det=DeterministicScores(recency=6, reachability=2, corroboration=3),
              score=25)},

         {"li_id:97531": ["they are raising a Series A"]})

    第二项里那家不在第一项里，两处不重叠：拿到 Assessment 的都是引用验过的，
    进第二项的一条 Assessment 都没有。调用方对两项的处理也不一样 ——
    第一项按分数排序推给 Riley，第二项只进日志和计数。

    按 config.batch_size 切段，一段一次请求；每段先拿四维判断，
    再对每家算三维事实，最后加权。分批放在这里不放 pipeline，是因为批大小
    是「一次请求装得下多少」这件事的属性，跟业务无关 —— 让 pipeline 去记
    这个数，等于把一个模型参数漏进了上一层。

    确定性那三维对被丢弃的公司不算 —— 没有 LLM 那半，Assessment 拼不出来。
    为什么不拿假分凑一个，见下面那个 continue 分支上的注释。

    两种异常会一路抛出去、这一跑失败，都是故意的：

        LlmCallFailed           这一批根本没拿回回答（限流、截断、拒答）
        BatchIdentityMismatch   重问完还是答错公司集合

    不吞掉是因为这两种基本都是系统性的：prompt 或 schema 出了问题、
    key 额度用光了、batch_size 配大了 —— 第一批犯了后面每批都会犯，
    早点炸比跑完十四批再看报表省钱。哪一批失败了要记成 partial，
    那是 pipeline 那一层的事（runs 表有这个状态），不在这里判。

    now 默认取当前时刻，但拿历史数据回归时要显式传当时的时刻，
    理由见 deterministic_scores。
    """
    # 不传就取当前时刻。拿历史数据回归时要显式传当时的时刻，
    # 否则同一批数据每天算出来的 recency 都不一样。
    now = now or datetime.now(UTC)

    # out 装最终的七维分，discarded 装反复答不好的那几家。
    # 循环结束时两者的家数加起来等于 len(clusters)。
    out: dict[str, Assessment] = {}
    discarded: dict[str, list[str]] = {}

    # 一段一次请求。133 家、batch_size=10 的话是 14 次。
    for start in range(0, len(clusters), config.batch_size):
        # batch 例如第一段是 clusters[0:10]。
        batch = clusters[start : start + config.batch_size]

        # llm_by_key 是过了全部闸门的那些，bad 是反复答不好的那几家，
        # 例如 llm_by_key 有 9 家、bad 是 {"li_id:97531": ["编的那句"]}。
        llm_by_key, bad = assess_batch(batch, facts=facts, config=config, client=client)

        # 这一批的每一家恰好落在一边：要么有判断，要么被丢弃。
        # 焊这一条是因为漏掉一家不会报错，只会让漏斗数字悄悄少一个 ——
        # 而「今天为什么只有 2 条」的排查就是从漏斗数字开始的。
        # 比集合不比个数：只比个数的话，「一家两头都沾、另一家两头都不沾」
        # 会正好抵消，而那恰恰是这条 assert 要防的那种构造 bug。
        wanted = {company_key(c) for c in batch}
        assert set(llm_by_key) | set(bad) == wanted and not set(llm_by_key) & set(bad), (
            f"这一批 {sorted(wanted)}，判断 {sorted(llm_by_key)}、丢弃 {sorted(bad)}，对不上"
        )
        discarded.update(bad)

        for cluster in batch:
            # key 例如 "li_id:100000001"。
            key = company_key(cluster)

            # 被丢弃的那几家直接跳过，不算确定性三维也不拼 Assessment ——
            # 拼一个「四维全 0」的假分，会让它在报表里看起来只是分低，不是被丢了。
            if key not in llm_by_key:
                continue

            # llm 是模型判的四维，det 是代码算的三维，例如
            #     llm  icp_fit=10 signal_strength=8 seniority=10 moq_fit=8
            #     det  recency=10 reachability=6 corroboration=3
            #     score 82
            llm = llm_by_key[key]
            det = deterministic_scores(cluster, config=config, now=now)
            out[key] = Assessment(llm=llm, det=det, score=weighted_score(llm, det, config))

    return out, discarded
