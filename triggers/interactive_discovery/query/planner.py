"""plan_queries 那一步的全部实现：拟词那次调用，和它前后不需要模型的那几件事。

跟 intent.py 同一个摆法：一次 LLM 调用的几个面放在一个文件里 —— 配置、给模型看的
schema、解析、以及围着它的那几个纯函数。改其中一样，另外几样通常要一起看。

这个文件分两半，边界很清楚：

    确定性的那一半   check_query / screen_queries / estimate_cost_usd /
                     build_assumptions。一条 query 有没有超 500 字符、带没带 NOT、
                     中文混没混进去，都是算得出来的事实
    模型的那一半     build_planner_messages / parse_plan / draft_queries。
                     「这个题目该用哪几个词去搜」没有确定的答案，那才是模型的活

能确定算的一律代码算，这是仓库里用了四次的同一条原则（prefilter、corroboration、
打分拆两半、is_ready）。让模型去赌一件算得出来的事，等于白给它一个出错的机会，
而出错的症状全是静默的：超长和非法输入会让 Apify 整跑失败，中文混进去会采回零条、
照样计费、还不报错。

两半之间有一条约定要一起维护：prompt 里请模型把拟出来的 query 按「好到坏」排，
screen_queries 砍数量时直接留前面的。这条约定断了不会报错 —— 模型随便排，我们
照样砍后面的，只是砍掉的可能正是最好的那条。所以 prompts.PLANNER_SYSTEM 第二节
和 screen_queries 的 docstring 互相点名，改一边必须去看另一边。

assumptions 那一段为什么不让模型写：见 build_assumptions 的 docstring 最后一节。
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from models import validate_filters
from triggers.interactive_discovery.query.prompts import PLANNER_SYSTEM

# 往上四层到仓库根：query/ -> interactive_discovery/ -> triggers/ -> 仓库根
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "interactive.yaml"

# 一条 query 的长度上限，字符。actor 的 searchQueries 说明里写的数。
# 超了 Apify 会判输入非法、整跑失败 —— 失败在花钱之前是好事，但那仍然是
# 这一次探索没了，而且报错要从 Apify 那一侧一路回溯到拟词。
QUERY_MAX_CHARS = 500

# 一条 query 最多几个布尔符。同样是 actor 说明里写的数。
# 它是额度：topics 自己带 AND/OR 等于上游偷偷用掉几个。
QUERY_MAX_BOOLEANS = 5

# 大写、且独立成词的 AND / OR / NOT。LinkedIn 只认大写布尔符，所以
# research and development 里那个小写的 and 不算一个布尔符。
#
# 边界用显式 ASCII 不用转义的单词边界，理由跟 normalize.term_pattern 一样：
# 那个转义认的范围含中日韩字符，中英混排时会失效。
# NOTebook 里的 NOT 不算，major 里的 OR 不算。
#
# 刻意不复用 normalize 那边拆词的实现：那边问的是「这条 query 在找哪几个词」，
# 这边问的是「用掉了几个额度」，两个问题不一样，共用一份会让其中一边为了
# 迁就另一边而变形。
_BOOLEAN_OPERATOR = re.compile(r"(?<![A-Za-z0-9])(AND|OR|NOT)(?![A-Za-z0-9])")


# 中日韩文字和全角标点。查的是「这条 query 能不能被 LinkedIn 搜到东西」，
# 所以只挡这一类，不挡所有非 ASCII —— Kestrel 这种带重音的公司名是合法的
# 英文搜索词，一刀切成 ASCII 会把它也拦掉。
_CJK = re.compile("[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


@dataclass(frozen=True)
class PlannerConfig:
    """config/interactive.yaml 的 planner 那一段。

    实际值（仓库里那份配置）：

        PlannerConfig(model='gpt-4o-mini', max_tokens=800, temperature=0.0,
                      request_timeout_seconds=30.0, max_queries=3,
                      unit_price_usd=0.002)

    前四个是那次调用怎么发出去，后两个是围着它的两件事（拟几条、报多少钱）。
    装在一个对象里是因为它们同属 plan_queries 这一个步骤 —— 跟 IntentConfig
    把 max_clarify_rounds 和模型参数放一起是同一个理由。
    """

    # 用哪个模型。
    model: str

    # 一次回答最多几个 token。一版方案实测约 200（三条 query，每条一句检索式加
    # 一个旋钮），给到配置里那个数是留给长一点的检索式。
    # 不够的症状是输出被截断成半个 JSON，解析当场失败 —— 那一种在 read_intent
    # 那边已经单独挡过一次。
    max_tokens: int

    # 采样温度。0，理由跟 intent 那次一样：同一份需求两次拟出不一样的词，
    # 「昨天那条词是怎么来的」就没法回答了。
    temperature: float

    # 单次请求超时，秒。超时是我们的、重试是 SDK 的 —— 不显式给的话 openai
    # 默认 600 秒，而这条路上有人在 Slack 那头等着。
    request_timeout_seconds: float

    # 一次对话最多拟几条 query。它同时是两件事：报价的上限，和模型的指令
    # （prompt 里那句「最多拟几条」是从这里插值进去的，所以这个数只有一处真相）。
    #
    # 光写 prompt 不在代码里拦的症状：某次模型拟了六条，采集费直接三倍，
    # 而且不报错。光在代码里拦不告诉模型的症状：它每次都拟满然后被砍掉几条，
    # 白花输出 token，而且被砍掉的可能正是它认为最好的那几条。
    max_queries: int

    # 每个计费 item 多少美元。用来给一版方案报价。
    #
    # 这个数是实测出来的，不是供应商标价：run 36/37 是 80 个 item 花了 0.16，
    # 9-18 那次 NOT 实验是 90 个 item 花了 0.1801，两次都落在 0.002 上。
    # 标价会变，所以这里写的是「我们实际付过的单价」，改它之前先去账单上对一次。
    unit_price_usd: float


def load_planner_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> PlannerConfig:
    """读 config/interactive.yaml 的 planner 那一段。

    输入：配置文件路径（不给就是仓库根的 config/interactive.yaml）
    输出：PlannerConfig

    一个实际值：

        文件   planner:
                 model: gpt-4o-mini
                 max_tokens: 800
                 temperature: 0
                 request_timeout_seconds: 30
                 max_queries: 3
                 unit_price_usd: 0.002
        输出   PlannerConfig(model='gpt-4o-mini', max_tokens=800, temperature=0.0,
                             request_timeout_seconds=30.0, max_queries=3,
                             unit_price_usd=0.002)

    缺键当场 KeyError，不给默认值，跟 load_intent_config 同一条口径：
    退化成默认值的后果是「实际用的数跟配置说的不一样」，而它不报错。

    后两个数查一遍范围。0 或负数的 max_queries 意味着「一条都不许拟」，那不是配置
    能表达的意图，是写错了；负的单价会让报价变成负数，而报价是花钱之前那道闸门要
    拿来比大小的数。
    """
    # 输入   文件内容。raw 是 planner 那一段，整份 yaml 的其余部分不看
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))["planner"]

    #        两个数先取出来查范围，查完才装进对象
    max_queries = int(raw["max_queries"])
    if max_queries < 1:
        raise ValueError(f"planner.max_queries 至少是 1，拿到 {max_queries}")

    unit_price_usd = float(raw["unit_price_usd"])
    if unit_price_usd <= 0:
        raise ValueError(f"planner.unit_price_usd 得是正数，拿到 {unit_price_usd}")

    # 输出   摊平成一个冻结的对象
    return PlannerConfig(
        model=str(raw["model"]),
        max_tokens=int(raw["max_tokens"]),
        temperature=float(raw["temperature"]),
        request_timeout_seconds=float(raw["request_timeout_seconds"]),
        max_queries=max_queries,
        unit_price_usd=unit_price_usd,
    )


def check_query(text: str, *, filters: object, exclusions: Sequence[str] = ()) -> tuple[str, ...]:
    """查一条拟出来的 query 能不能用。返回的是问题清单，空元组表示合格。

    输入：一条 query 的正文；它自己的采集旋钮；这一轮说好要避开的词
    输出：一串中文说明，每条说清哪里不行。空元组 = 这条能用

    三个实际值。

    一、合格的那种

        输入   text='Kickstarter AND (microphone OR "voice recorder")'
               filters={'authorKeywords': 'Founder OR CTO'}
               exclusions=('pre-launch',)
        输出   ()

    二、带 NOT 的

        输入   text='audio NOT hiring'，filters={}，exclusions=()
        输出   ("这条带了 NOT：LinkedIn 的 NOT 不是过滤器是相关性罚分，"
                "排不掉跟题目同义的那批",)

    三、一条里几个毛病一起犯

        输入   text='骨传导 AND (a OR b OR c OR d OR e OR f)'
               filters={'authorKeywords': 'Founder, CTO'}
               exclusions=()
        输出   ("这条用了 6 个 AND/OR/NOT，超过 5 个的上限",
                "这条里混了中文：骨传导。LinkedIn 那边的帖子是英文的，"
                "中文词采回零条、照样计费、而且不报错",
                "旋钮不能用：authorKeywords 里有 ','：...")

    一次把所有问题都返回，不是碰到第一个就停：拟词那一步要么重来要么丢掉，
    两种都是一次性的机会，只说一个毛病等于逼着下一轮再发现一个。

    这几条全是确定性的事实，所以放在代码里不放 prompt 里。prompt 里当然也要说
    （那样模型多半就不会犯，见 prompts.PLANNER_SYSTEM 第一节），但那是请求不是
    保证 —— 模型没照做的时候，超长和非法旋钮会让 Apify 整跑失败，中文混进去会
    采回零条而且不报错。

    这里的每一句问题都是中文，因为读它的是人不是模型：它们原样进 assumptions、
    摆在 Slack 卡片上，告诉用户这一条为什么没跑。我们不会拿着这串问题回头去问
    模型第二次（理由见 screen_queries），所以模型这辈子看不到它们。
    """
    problems: list[str] = []

    #        先把最基本的那条挡掉。空白 query 发出去不会报错，
    #        它会采回一批跟这次对话完全无关的东西
    if not text.strip():
        problems.append("这条是空的")
        return tuple(problems)

    #        长度。算的是字符数不是字节数，跟 actor 那边的口径一致
    if len(text) > QUERY_MAX_CHARS:
        problems.append(f"这条有 {len(text)} 个字符，超过 {QUERY_MAX_CHARS} 个的上限")

    #        布尔符个数。operators 里是每一次出现，不是有几种
    operators = _BOOLEAN_OPERATOR.findall(text)
    if len(operators) > QUERY_MAX_BOOLEANS:
        problems.append(f"这条用了 {len(operators)} 个 AND/OR/NOT，超过 {QUERY_MAX_BOOLEANS} 个的上限")

    #        NOT 单独挡，跟上面那条数额度是两件事
    if "NOT" in operators:
        problems.append("这条带了 NOT：LinkedIn 的 NOT 不是过滤器是相关性罚分，排不掉跟题目同义的那批")

    #        cjk 是混进来的中文，去重并保持出现顺序 —— 报错里要能直接看出是哪几个字
    cjk = dict.fromkeys(_CJK.findall(text))
    if cjk:
        problems.append(
            f"这条里混了中文：{''.join(cjk)}。LinkedIn 那边的帖子是英文的，"
            "中文词采回零条、照样计费、而且不报错"
        )

    #        说好要避开的词又写进正文了。这是 exclusions 这个字段今天唯一的动作,
    #        不在这里查的话它就退回成一个没人读的装饰品
    for word in exclusions:
        if word.strip() and _contains_word(text, word):
            problems.append(f"这条里出现了说好要避开的词：{word}")

    #        旋钮走进库那一关的同一个校验。在这里先查一遍是为了早一步说清楚 ——
    #        等到 upsert_query 才炸的话，这一轮对话已经走到花钱的边上了
    try:
        validate_filters(filters)
    except ValueError as problem:
        problems.append(f"旋钮不能用：{problem}")

    # 输出   问题清单。空元组表示这条能用
    return tuple(problems)


def _contains_word(text: str, word: str) -> bool:
    """这个词有没有作为一个独立的词出现在这段文字里。

    输入：一段文字；一个词（可以是词组，如 voice recorder）
    输出：布尔

    一个实际值：

        输入   text='Kickstarter AND audio'，word='audio'   输出 True
        输入   text='audiobook launch'，word='audio'        输出 False

    边界用显式 ASCII 不用 \\b，理由同 _BOOLEAN_OPERATOR。
    词组里的空白容忍换行和连字符，跟 normalize.term_pattern 一个口径。
    """
    middle = r"[\s\-]+".join(re.escape(part) for part in word.split())
    return re.search(rf"(?<![A-Za-z0-9]){middle}(?![A-Za-z0-9])", text, re.I) is not None


def estimate_cost_usd(query_count: int, *, max_posts_per_query: int, unit_price_usd: float) -> float:
    """这一版方案跑下去大概花多少采集的钱。

    输入：这版方案有几条 query；每条最多采几条 post；每个计费 item 的单价
    输出：美元，保留到分

    两个实际值：

        输入   query_count=2，max_posts_per_query=40，unit_price_usd=0.002
               计费 item 上限 = 2 x 40 = 80
        输出   0.16

        输入   query_count=0，max_posts_per_query=40，unit_price_usd=0.002
        输出   0.0

    算的是上限不是预期：max_posts_per_query 是天花板，供应商只有 12 条就返回
    12 条。所以真实花费只会比这个数小。报价往高了说是故意的 —— 报低了的那次，
    用户是在账单上发现的。

    评论没算在里面：开了评论抓取的话计费 item 会翻倍（实测 60 个 post 加
    60 条评论 = 120 个计费单位），而我们今天一直关着它。哪天开了，这个函数
    要跟着改，否则报价会低一半。
    """
    #        billed_items 是计费 item 的上限，也就是最多采回多少条
    billed_items = max(0, query_count) * max_posts_per_query

    # 输出   保留到分。多出来的小数位在报价这件事上没有意义
    return round(billed_items * unit_price_usd, 2)


def screen_queries(
    raw: Sequence[Mapping[str, object]],
    *,
    exclusions: Sequence[str] = (),
    max_queries: int,
) -> tuple[list[dict], list[tuple[str, tuple[str, ...]]]]:
    """把 planner 拟出来的那一批过一遍：哪几条留下，哪几条丢掉、为什么。

    输入：
        raw          拟出来的那一批，每条是 {'text': ..., 'filters': {...}}。
                     顺序是模型自己排的，见下面「顺序是有含义的」那一节
        exclusions   这一轮说好要避开的词，逐条交给 check_query
        max_queries  一次最多留几条（配置里那个数）

    输出：
        (kept, dropped)
        kept      留下的那几条，元素是 {'text': ..., 'filters': {...}}，
                  直接就是 plan['queries'] 的形状，一个字都不用再转
        dropped   丢掉的那几条，元素是 (正文, 问题清单)。问题清单是中文句子，
                  原样进 build_assumptions 摆到卡片上

    四个实际值，覆盖四种走法。

    一、一条被质量砍掉，剩下的正好不超上限

        输入   raw=[{'text': 'Kickstarter AND bone conduction', 'filters': {}},
                    {'text': 'audio NOT hiring', 'filters': {}},
                    {'text': 'wearable AND microphone', 'filters': {}}]
               exclusions=()，max_queries=3
               第二条带 NOT，被 check_query 挑出来；剩两条合格，没到上限
        输出   kept    = [{'text': 'Kickstarter AND bone conduction', 'filters': {}},
                          {'text': 'wearable AND microphone', 'filters': {}}]
               dropped = [('audio NOT hiring',
                           ('这条带了 NOT：LinkedIn 的 NOT 不是过滤器是相关性罚分，'
                            '排不掉跟题目同义的那批',))]

    二、全合格，但比上限多一条

        输入   raw=[四条全合格的]，max_queries=3
               质量这一关一条都没掉；第四条输在位置上
        输出   kept    = 前三条
               dropped = [('earbuds AND startup', ('超过一次最多 3 条，这条没跑',))]

    三、质量和数量同时发生，而且顺序很要紧

        输入   raw=[{'text': 'Kickstarter AND bone conduction', 'filters': {}},
                    {'text': 'audio NOT hiring', 'filters': {}},
                    {'text': 'wearable AND microphone', 'filters': {}},
                    {'text': 'earbuds AND (startup OR launch)', 'filters': {}}]
               max_queries=3
               先查质量：第二条出局，还剩三条
               再砍数量：三条正好等于上限，一条都不用再砍
        输出   kept    = 第一、三、四条
               dropped = [('audio NOT hiring', (NOT 那句,))]

               这就是「先查质量再砍数量」买到的东西。反过来先砍数量的话，
               前三条里那条带 NOT 的会先占掉一个名额、然后在质量这一关掉下去，
               最后只跑两条 —— 而第四条明明是合格的。用户付了同样的等待，
               拿到的线索少三分之一，而报表上看不出任何异常。有测试焊着这个顺序。

    四、一条都没留下

        输入   raw=[{'text': '骨传导耳机', 'filters': {}}]，max_queries=3
        输出   kept    = []
               dropped = [('骨传导耳机', ('这条里混了中文：骨传导耳机。...',))]

               不在这里报错也不在这里重试。空的 kept 照样往下走到 await_go，
               卡片第一行会是「一条能用的 query 都没拟出来，换个说法…」
               （build_assumptions 负责这句）。让用户看见比当场炸有用 ——
               炸了他只知道出错了，看见了他知道该换个说法。

    顺序是有含义的，而这一点靠两个文件的一条约定撑着：

        prompts.PLANNER_SYSTEM 第二节里请模型「按这一条能带回多少真买方，
        从好到坏排」，并且告诉它超出上限的会被直接砍掉、不会再问它一次。
        所以这里砍数量用的是截断 —— 留前面的，等于用模型自己的判断。

        这条约定断了不报错：模型哪天随便排，我们照样砍后面的，只是砍掉的可能
        正是它认为最好的那条，而用户只会觉得「今天拟得不太行」。所以改这两处
        任意一边，都要去看另一边。

    砍数量为什么不回头重问模型：重问一次是多花一次钱去问同一个问题，而它刚刚
    已经把自己认为最好的排在前面了 —— 我们手上已经有它的答案。带着问题清单回去
    让它重拟也是同一个理由：那几条问题全是确定性的规矩，prompt 里已经写着了，
    它这次没照做，再说一遍多半还是那样，而每说一遍都要花一次钱。

    丢掉的不静默：每一条都带着原因进 build_assumptions，摆在方案卡片上。
    """
    kept: list[dict] = []
    dropped: list[tuple[str, tuple[str, ...]]] = []

    #        先按质量分两堆。这一步不看数量，所以一条合格的 query 不会因为
    #        排在后面就被当成有毛病
    for item in raw:
        text = str(item.get("text") or "")
        filters = item.get("filters")
        problems = check_query(text, filters=filters, exclusions=exclusions)
        if problems:
            dropped.append((text, problems))
        else:
            kept.append({"text": text, "filters": dict(filters or {})})

    #        合格的里面超出上限的那几条，按原顺序留前面的
    if len(kept) > max_queries:
        for item in kept[max_queries:]:
            dropped.append((str(item["text"]), (f"超过一次最多 {max_queries} 条，这条没跑",)))
        kept = kept[:max_queries]

    # 输出   两堆。留下的那堆直接就是 plan['queries'] 的形状
    return kept, dropped


def build_assumptions(
    intent: Mapping[str, object],
    *,
    kept: Sequence[Mapping[str, object]],
    dropped: Sequence[tuple[str, Sequence[str]]] = (),
) -> str:
    """把这一版方案替用户做的猜测写成给人看的一段话。

    输入：understand_intent 读出来的那份 intent；留下来的那几条 query；
          被丢掉的那几条（每条一个 (正文, 问题清单)）
    输出：一段中文，原样进 plan 的 assumptions，也就是方案卡片上那几行

    它在整条流水线上的位置是最后一步：先拟词（模型），再 screen_queries 分出
    kept 和 dropped，然后才轮到这里。顺序不能换 —— 它要说的是「这次真的会搜什么」，
    而那件事只有筛完才知道。

    一个实际值。用户说的是「帮我找找骨传导耳机的初创，最好是刚做完众筹的，
    别带那种预热帖，只要加州的」：

        输入   intent={'goal': '找刚做完众筹的骨传导耳机团队',
                       'topics': ['bone conduction'], 'events': ['刚做完众筹'],
                       'exclusions': ['pre-launch'], 'time_said': '这周',
                       'unsupported': ['只要加州的']}
               kept=[{'text': 'Kickstarter AND bone conduction',
                      'filters': {'authorKeywords': 'Founder OR CTO'}}]
               dropped=[('audio NOT hiring', ['这条带了 NOT：...'])]
        输出   我理解的需求：找刚做完众筹的骨传导耳机团队
               要搜的词：bone conduction
               要找的时刻：刚做完众筹
               拟词时避开：pre-launch
               只看这些人发的帖：Founder OR CTO
               做不到的：只要加州的
               丢掉了 1 条：audio NOT hiring（这条带了 NOT：...）

    对照一条都没留下的样子：

        输入   kept=[]，其余同上
        输出   开头多一行「一条能用的 query 都没拟出来 ...」

    为什么这四样必须逐条摆出来，尤其是 topics：

        模型有可能把用户不想要的词填进 topics（他说「别带众筹的」，模型填了
        kickstarter）。形状检查查不出这种错 —— kickstarter 是一个合法的名词
        短语。一路走下去就是花钱搜了一个他明确排除的题目，全程不报错。
        摆在卡片上，他在点「跑」之前一眼就能抓住。这才是这一层真正的保险，
        指望模型不出错不是。

    空的字段不出现，不写成「无」：一行「拟词时避开：无」占掉一行视线，
    而它没有回答任何问题。

    为什么这段话是拼出来的，不让模型写（这件事讨论过，结论写在这里免得又被提一次）：

        它存在的全部价值是「逐字可信」。用户拿它跟自己刚才说的话对，看模型有没有
        把某个词理解反了。让模型来写或者再润色一遍，等于让被检查的人自己誊抄
        检查表 —— 它把 exclusions 里的 pre-launch 写成「会避开预热类的内容」是
        好文笔，但用户再也看不出我们真正拿去比对的那个词是哪几个字母；它顺手漏掉
        一条、或者把「丢掉了 1 条」说成「已按你的要求优化」，没有任何症状。
        这跟话术那边「引用的原文必须是 post 正文的子串」是同一条纪律。

        润色还要多花一次调用、多一次延迟，而用户正等着。

        中间一度让拟词那次调用顺带写一句中文思路（叫 rationale）摆在这份清单前面，
        想让卡片读起来不那么硬。后来拿掉了：这份清单本身就是卡片上唯一要读的东西，
        再加一段模型写的话只是多一处没人能核对的文字，而它一旦跟清单说得不一样，
        用户先信哪一份反倒成了新问题。
    """
    #        lines 是最后要拼起来的那几行，按「先说理解、再说做法、最后说没做到的」排
    lines: list[str] = []

    # 输入   一条都没留下时，这句话要排在最前面 —— 它是这版方案唯一的内容
    if not kept:
        lines.append(
            "一条能用的 query 都没拟出来。换一个说法告诉我你想找什么，"
            "或者把范围放宽一点（比如别限定得太细，或者换一个品类词）。"
        )

    #        逐项取值。取不到的一律当成空，不让缺键炸在拼字符串这一步 ——
    #        这段话的用途是给人看，缺一行远好过整条对话挂掉
    goal = str(intent.get("goal") or "").strip()
    if goal:
        lines.append(f"我理解的需求：{goal}")

    for label, key in (
        ("要搜的词", "topics"),
        ("要找的时刻", "events"),
        ("拟词时避开", "exclusions"),
    ):
        values = [str(v).strip() for v in intent.get(key) or () if str(v).strip()]
        if values:
            lines.append(f"{label}：{'、'.join(values)}")

    # 采集窗口不在这份清单里：它在卡片最上面那一栏（query/messages.plan_message），
    # 那里印的是真正要用的值，没说时间的也印（换成默认），比放在清单最末一行显眼

    #        旋钮取自留下来的那几条 query，不是 intent —— 它是 planner 拟的，
    #        不是用户说的。同一个值只报一次，几条 query 用同一套旋钮是常态
    keywords = dict.fromkeys(
        str((item.get("filters") or {}).get("authorKeywords") or "").strip() for item in kept
    )
    for value in keywords:
        if value:
            lines.append(f"只看这些人发的帖：{value}")

    unsupported = [str(v).strip() for v in intent.get("unsupported") or () if str(v).strip()]
    if unsupported:
        lines.append(f"做不到的：{'、'.join(unsupported)}")

    #        丢掉的那几条逐条说清楚。丢了而不说等于静默丢弃，
    #        而用户会以为方案就是他要的那个样子
    if dropped:
        lines.append(f"丢掉了 {len(dropped)} 条：")
        for text, problems in dropped:
            lines.append(f"    {text}（{'；'.join(problems)}）")

    # 输出   一段话。一行一件事，卡片上按行渲染
    return "\n".join(lines)


@dataclass(frozen=True)
class PlanDraft:
    """拟词那一次 LLM 调用的完整输出，还没经过任何筛选。

    一个实际值：

        PlanDraft(queries=({"text": "Kickstarter AND bone conduction",
                            "filters": {"authorKeywords": "Founder OR CTO"}},
                           {"text": "wearable AND (microphone OR audio)", "filters": {}}))

    它跟 plan['queries'] 长得几乎一样，但不是同一个东西：这里这份是模型说的，
    有没有超长、带没带 NOT、中文混没混进去都还没查过，条数也可能超上限。
    查过筛过之后才是 plan['queries']（见 screen_queries）。两者用不同的类型装着，
    是为了让「这份还没检查过」这件事在调用方那边看得见。

    只有一个字段，看起来不如直接返回一个元组省事。留着这个类是因为它是那次调用的
    出口：哪天那次调用多返回一样东西，加在这里，调用方的签名不用动。

    queries 里的顺序是模型自己排的好坏顺序，原样保留 —— 砍数量的时候要用它。
    """

    # 拟出来的那几条，每条 {'text': ..., 'filters': {...}}。
    # filters 今天最多只有 authorKeywords 一个键，没填的那几条是空字典。
    queries: tuple[dict[str, Any], ...]


# 交给 OpenAI 的 json_schema。两条硬要求跟 INTENT_SCHEMA 一样：每一层都要
# additionalProperties: false，required 必须列全 properties 里的每一个键。
#
# 只要 queries 一样东西。模型不需要额外写一段话解释它为什么这么拟 —— 用户要看的
# 那份东西（这次真的会搜什么、真的会避开什么、哪几条被丢掉了）全是确定性的，
# 由 build_assumptions 拼出来，理由写在那个函数的 docstring 最后一节。
#
# authorKeywords 摊平放在每条 query 旁边，没有包一层 filters：
# 今天白名单里只有这一个旋钮（models 里那个 _ALLOWED_FILTER_KEYS），摊平能让模型
# 一眼看清它只有这一个选择。包一层的话 strict 模式还是要求那一层的 required 列全，
# 等于同样要给一个值，什么都没省下。加第二个旋钮那天再包也不迟。
#
# 不填就给空串 —— strict 下没有「不给这个键」这个选项。空串在 parse_plan 里被剔掉，
# 剔不干净的后果见那个函数里的注释。
PLAN_SCHEMA: dict[str, Any] = {
    "name": "query_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["queries"],
        "properties": {
            "queries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "authorKeywords"],
                    "properties": {
                        "text": {"type": "string"},
                        "authorKeywords": {"type": "string"},
                    },
                },
            },
        },
    },
}


class PlannerCallFailed(RuntimeError):
    """这一次拟词调用没拿到能用的回答。

    跟 IntentCallFailed 同构，六种情况共用一个类型：SDK 抛的异常、输出被 max_tokens
    截断、模型拒答、没有内容、返回的不是合法 JSON、解析时发现字段不对。共用是因为
    这一层对它们做的事一样 —— 补上上下文再抛。自己重试没有意义：temperature 是 0，
    同样的输入拿回同样的坏回答。

    抛到节点会让这一次 invoke 整个失败，用户看到的是「出错了」而不是一版方案。
    这是对的：宁可显式失败，也不要编一版方案出来接着往下走 —— 下一步就是让他点
    「跑」，而那一下是真花钱。

    注意它跟「模型拟出来的 query 不合格」是两件事，别混。那一种不抛异常，走的是
    screen_queries 那条路：丢掉、记下原因、摆到卡片上给用户看。分界是「谁的问题」——
    这个类型装的是我们跟模型之间的接口出了事，那一种装的是模型没听话。
    """


def build_planner_messages(
    intent: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    *,
    lines: Sequence[str],
    feedback: str,
    previous_plan: Mapping[str, Any] | None,
    max_queries: int,
) -> list[dict[str, str]]:
    """把这一轮要给拟词那次调用看的五样东西，拼成喂给模型的那两条消息。

    输入：understand_intent 读出来的那份 intent；retrieve_knowledge 端上来的那份
          knowledge；这条对话说过的每一句原话；用户看完上一版方案回的那一句
          （空串表示第一次拟）；上一版方案（第一次拟时是 None）；一次最多拟几条
    输出：OpenAI chat 格式的消息列表，永远是两条（system + user）

    一个实际值（第一次拟）：

        输入   intent={'goal': '找刚做完众筹的骨传导耳机团队',
                       'topics': ['bone conduction'], 'events': ['刚做完众筹'],
                       'exclusions': ['pre-launch'], 'time_said': '这周',
                       'max_spend_usd': 0.2, 'unsupported': ['只要加州的']}
               knowledge={'avoid': [三条], 'topics_searched': ['bone conduction'],
                          'known_queries': [], 'known_lead_total': 0}
               lines=['用户: 帮我找找骨传导耳机的初创，最好是刚做完众筹的，只要加州的']
               feedback=''，previous_plan=None，max_queries=3
        输出   [{'role': 'system', 'content': '你在帮一家做音频...最多 3 条...'},
                {'role': 'user', 'content': 一份 JSON，见下}]

               那份 JSON 摊开是：
                   {"intent": {"goal": "找刚做完众筹的骨传导耳机团队",
                               "topics": ["bone conduction"],
                               "events": ["刚做完众筹"],
                               "exclusions": ["pre-launch"]},
                    "knowledge": {"avoid": [三条], "topics_searched": ["bone conduction"],
                                  "known_queries": [], "known_lead_total": 0},
                    "conversation": ["用户: 帮我找找骨传导耳机的初创，..."],
                    "plan_feedback": "",
                    "previous_plan": None}

        改第二版的时候，五样都可能不一样，只是变法不同：

            conversation   一定变。多出两行（我们那一版方案，和他回的那一句）
            plan_feedback  一定变。从空串变成他回的那一句
            previous_plan  一定变。从 None 变成他刚看过的那一版
            intent         可能变。他说「别带众筹的」的话，exclusions 会多一条；
                           他只说「第二条不要了」的话，七个字段一个都不动
            knowledge      可能变。topics 变了才会变（它是按 topics 查的），
                           topics 没变就是原样再查一次、拿回同一份

        前两样有机会变，是因为「改」那条边绕回的是 understand_intent 不是这里 ——
        它重读一遍全部原话、重填 intent，再经过 retrieve_knowledge 重查一次库，
        然后才到这儿。绕回这里的话那两样永远是旧的，这次拟词就没有新东西可用。

        第二版的那三项长这样：
                    "conversation": [..., "AI: 方案 v1，2 条 query，预计 $0.16。...",
                                     "用户: 第二条不要了"],
                    "plan_feedback": "第二条不要了",
                    "previous_plan": {"version": 1,
                                      "queries": ["Kickstarter AND bone conduction",
                                                  "bone conduction AND manufacturer"]}

    五个键各是 state 里的哪一格：

        intent         state['intent_result']['intent'] 里的四个字段（不是七个，见下）
        knowledge      state['knowledge']，整份
        conversation   state['messages']，一个字不改
        plan_feedback  state['plan_feedback']
        previous_plan  state['plan']

    三个照抄了 state 的字段名，另外两个刻意不照抄，各有各的原因，别顺手改齐：

        conversation   messages 这个词在这个文件里已经被占了 —— 这个函数的名字、
                       它的返回值、draft_queries 里那个局部变量，说的都是 OpenAI
                       那份 system/user 列表。同一个词两个意思会出事
        previous_plan  这次调用的产出就是一版新 plan，叫 plan 分不清是哪一版

    照抄那三个是有用处的：出事的时候第一件要做的事就是拿日志里那份 payload 对着
    state 看，名字一样就不用中间再翻译一道。

    为什么非给 conversation 不可：温度是 0，同样的输入拿回同样的输出。用户说「改」
    之后图绕回 understand_intent 再到这里，要是喂进来的东西一个字没变，拟出来的就是
    逐字相同的一版 —— 他改一次看见同一版，再改一次还是同一版，而全程不报错。
    「别带 Kickstarter」还有 intent 那条路（会进 exclusions，再被 check_query 硬拦），
    但「第二条不要了」「再加一条」改的是方案本身，intent 七个字段没有一个装得下它。

    plan_feedback 干的事就一件：把 conversation 那一长串历史里最新的一句指出来。
    conversation 绕三轮之后长这样：

        用户: 帮我找找骨传导耳机的初创
        AI: 方案 v1，2 条 query，预计 $0.16。我理解的需求：… 要搜的词：… 做不到的：…
        用户: 别带众筹的
        AI: 方案 v2，1 条 query，预计 $0.08。我理解的需求：… 要搜的词：… 做不到的：…
        用户: 第二条不要了          <- 只有这一句是新的

    不指出来的话，模型得自己从这一堆里认出最后那一句。而那件事越绕越难：每出一版
    方案就往里写一整份清单（那几行是我们说的，不是他说的），绕三轮之后我们写进去
    的字数比他说的多好几倍。

    两样都给不是二选一 —— 「第二条不要了」里那个「第二条」指的是上面那几行里列的
    那几条，只有完整的 conversation 读得出来。

    intent 只给四个字段，判据是仓库里那条「每个字段必须指名消费者」反过来用在输入上：
    喂进去而没人要它做什么的字段，模型会认真对待它，然后做出我们没要求的事。

        time_said       采集窗口是 harvest 那一步传给 actor 的参数，跟词长什么样无关。
                        给了它可能把 last week 写进检索式，占布尔符额度又没有用
        max_spend_usd   花多少钱是代码那道闸门的事。给了它只会让它自我审查条数，
                        而条数已经由 max_queries 管着
        unsupported     装的是我们做不到的那几件，用途是给人看

        挡不干净：用户那句「只要加州的」原话就在 conversation 里。所以 prompt 第六节
        明写了「需求里没有而他嘴上说了的，说明整理那一步判定我们做不到，别自作主张
        塞进检索词」。不从原话里删是因为删不干净 —— 要删就得做子串匹配，那会误伤，
        而且误伤了不报错。

    previous_plan 只给版本号和几条正文，不给 assumptions 也不给报价：那两样是我们
    写给用户看的话，喂回去只会让它复述我们的措辞。

    两条机械上的。system 那条里的条数是从配置插值进去的，不是写死的 —— 配置写 3、
    prompt 写 2 那种不一致没有任何症状（模型按它看到的数拟，代码按配置那个数截断）；
    用 replace 不用 str.format，理由在 prompts.PLANNER_SYSTEM 上面那段注释里。
    user 那条是一份 JSON 不是一段散文：拼散文要我们自己定一套排版，而那套排版一改，
    模型读到的东西就变了、却没有任何地方记着它变过。
    """

    # 输入   system 那条：把条数插进去
    system = PLANNER_SYSTEM.replace("__MAX_QUERIES__", str(max_queries))

    #        payload 是喂给模型的那份 JSON。键名全是英文；哪个键对应 state 里的
    #        哪一格、以及两个对不上的键为什么对不上，写在上面 docstring 里
    payload = {
        "intent": {
            "goal": intent.get("goal", ""),
            "topics": list(intent.get("topics") or ()),
            "events": list(intent.get("events") or ()),
            "exclusions": list(intent.get("exclusions") or ()),
        },
        "knowledge": knowledge,
        "conversation": list(lines),
        "plan_feedback": feedback.strip(),
        "previous_plan": None
        if not previous_plan
        else {
            "version": previous_plan.get("version", 0),
            "queries": [str(item.get("text", "")) for item in previous_plan.get("queries") or ()],
        },
    }

    # 输出   两条消息。ensure_ascii 关掉，中文原样进去 —— 开着的话 goal 和整段对话
    #        会变成一串转义码，模型读得懂但没有任何好处，而人在日志里读不懂
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_plan(payload: Mapping[str, Any]) -> PlanDraft:
    """把模型返回的那份 JSON 解析成 PlanDraft。

    输入：json.loads 出来的那个字典
    输出：PlanDraft。字段不对当场抛 PlannerCallFailed

    一个实际值：

        输入   {"queries": [{"text": "Kickstarter AND bone conduction",
                             "authorKeywords": "Founder OR CTO"},
                            {"text": "wearable AND (microphone OR audio)",
                             "authorKeywords": ""}]}
        输出   PlanDraft(queries=({"text": "Kickstarter AND bone conduction",
                                   "filters": {"authorKeywords": "Founder OR CTO"}},
                                  {"text": "wearable AND (microphone OR audio)",
                                   "filters": {}}))

    这一步只做形状转换，不做质量判断：超长、带 NOT、混中文的那几条原样带出去，
    交给 check_query 说清楚、交给卡片给用户看。在这里拒收等于为一条词毙掉整条对话。

    空的 authorKeywords 必须剔干净，不能留成一个值是空串的键：

        0009 那条唯一索引是 (text_normalized, filters)，filters 整份参与身份。
        所以空字典和「有这个键但值是空串」在库里是两条不同的 query —— 同一句词
        会凭空多出一行，而它跟原来那条采回来的东西一模一样。
        顺带，validate_filters 也不收空串，所以留着的话这条会在 check_query 那里
        被判不合格、被丢掉，用户看见的是一条他看不懂的错。

    缺键当场抛，不给默认值：strict 的 json_schema 下这几个键必然在，缺了只能是
    PLAN_SCHEMA 和这个函数漂了 = 我们的 bug，正确反应是改代码不是兜底。
    """
    try:
        raw_queries = payload["queries"]
    except (KeyError, TypeError) as err:
        raise PlannerCallFailed(f"返回里少了字段：{err}。整份回答前 300 字：{str(payload)[:300]}") from err

    if not isinstance(raw_queries, list):
        raise PlannerCallFailed(
            f"queries 得是一个列表，拿到 {type(raw_queries).__name__}：{str(raw_queries)[:200]}"
        )

    #        queries 逐条转成 plan 里那个形状：authorKeywords 收进 filters，
    #        空的整个键都不放进去
    queries: list[dict[str, Any]] = []
    for item in raw_queries:
        if not isinstance(item, dict):
            raise PlannerCallFailed(f"queries 里有一条不是对象，是 {type(item).__name__}：{str(item)[:200]}")
        try:
            text = str(item["text"]).strip()
            keywords = str(item["authorKeywords"]).strip()
        except KeyError as err:
            raise PlannerCallFailed(f"queries 里有一条少了字段 {err}：{str(item)[:200]}") from err
        filters = {"authorKeywords": keywords} if keywords else {}
        queries.append({"text": text, "filters": filters})

    # 输出   一份冻结的 PlanDraft。顺序原样保留，砍数量时要用它
    return PlanDraft(queries=tuple(queries))


def draft_queries(
    client: Any,
    *,
    intent: Mapping[str, Any],
    knowledge: Mapping[str, Any],
    lines: Sequence[str],
    feedback: str,
    previous_plan: Mapping[str, Any] | None,
    config: PlannerConfig,
) -> PlanDraft:
    """调一次模型，拟出这一版方案的几条检索词。这一步花钱（很少，但是花）。

    输入：
        client      一个能 chat 的对象。真跑时是 openai.OpenAI，测试里是假客户端 ——
                    这一层不 import openai，也不认它的异常类型，理由跟 analyzer
                    那边一样：认死一个供应商的类型，换实现或者写测试都得跟着改
        intent      understand_intent 读出来的那七个字段（只有四个会被喂进去）
        knowledge   retrieve_knowledge 从库里端上来的那一份
        lines       这条对话说过的每一句原话。用户说「改」之后，他要改什么只有
                    这里读得到 —— 理由见 build_planner_messages
        feedback    lines 里最新的那一句（用户看完上一版方案回的话），单独再给一遍，
                    好让模型不用自己从一长串历史里找它。空串表示第一次拟。
                    必填，不给默认值：漏传的后果是那一句不再被指出来，没有任何症状
        previous_plan   上一版方案，第一次拟时是 None
        config      模型、max_tokens、温度、超时、最多拟几条

    输出：PlanDraft。还没经过任何检查，下一步是 screen_queries

    一个实际值：

        输入   intent 是骨传导那一份，knowledge 里 known_queries 是空的，feedback 是空串
        输出   PlanDraft(queries=(两条,))

    对库的写：一笔都没有。真正写 state 的是调它的那个节点 return 的时候
    （见 nodes.plan_queries）。

    六种拿不到好回答的情况全部抛 PlannerCallFailed，每一种都把上下文补上。
    不在这一层重试：temperature 是 0，同样的输入拿回同样的坏回答。

    这个函数跟 intent.read_intent 是一个模子刻出来的，逐句同构。没有抽成一个共用的
    call_model()，是因为两次调用要说的话不一样：那边截断了要提示「一份 intent 约
    200 tokens」，这边要提示的是条数和方案长度。抽出来之后那几句提示要么变通用
    （等于没有），要么靠参数传进去（等于把函数体搬到调用方）。
    """
    #        messages 拼在 try 外面：拼的时候抛异常会被下面报成「请求失败」，
    #        把我们自己的 bug 伪装成网络故障
    messages = build_planner_messages(
        intent,
        knowledge,
        lines=lines,
        feedback=feedback,
        previous_plan=previous_plan,
        max_queries=config.max_queries,
    )

    try:
        # 输出   一次 HTTP 请求。超时是我们的、重试是 SDK 的
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": PLAN_SCHEMA},
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout_seconds,
        )
    except Exception as err:
        raise PlannerCallFailed(f"请求失败：{type(err).__name__}: {err}") from err

    #        choice 是唯一那个候选回答（没要 n>1）
    choice = response.choices[0]

    if choice.finish_reason == "length":
        raise PlannerCallFailed(
            f"输出被 max_tokens={config.max_tokens} 截断。一版方案约 200 tokens，"
            f"截断多半是它拟了太多条或者某一条写得太长，"
            f"调 config/interactive.yaml 的 planner.max_tokens"
        )

    # refusal 是结构化输出下模型拒答时才有值的字段，正常是 None
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise PlannerCallFailed(f"模型拒答：{refusal}")

    content = choice.message.content
    if content is None:
        raise PlannerCallFailed(f"模型没有返回内容，finish_reason={choice.finish_reason}")

    #        payload 是那份 JSON。strict schema 下它应该永远合法，
    #        真不合法的时候要把原文带上 —— 光说「JSON 解析失败」查不了
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as err:
        raise PlannerCallFailed(f"返回的不是合法 JSON：{err}。原文前 200 字：{content[:200]}") from err

    # 合法 JSON 不等于一个对象：null、[]、3 都能解析成功。strict schema 下不该发生，
    # 但真发生时得在这儿炸 —— 不然下一句取 queries 抛的是 TypeError，
    # 那种报错读起来像我们自己的 bug，查的方向完全反了
    if not isinstance(payload, dict):
        raise PlannerCallFailed(f"返回的 JSON 不是一个对象，是 {type(payload).__name__}：{content[:200]}")

    # 输出   解析成 PlanDraft。字段不对在这一步炸
    return parse_plan(payload)
