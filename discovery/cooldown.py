"""分析完一家公司之后，下次什么时候才值得再花一次 LLM 的钱。

纯函数，不连库不调 LLM：同样的输入永远给同样的输出，所以「这家公司为什么
三个月都没再被看过」可以拿一批固定的行重放出来。

这一层存在的理由是成本。leads 表装的是所有分析过的公司而不只是合格的，
绝大多数属于不合格那一类；没有冷却记录的话，明天同一条 query 又搜到它们、
又送一次 LLM，而那是整个系统最大的一笔冤枉钱。

判据只有一句话：

    冷却期回答的是「这家公司多久之后，世界可能变得不一样」，
    不是「这家公司现在有多差」。

同样是低分，品类完全不对的理财顾问和「做录音笔但今天只是发了句愿景」
应该差很远 —— 后者下个月可能就众筹上线了。按分数深浅排冷却期会把这两类
排在一起，那正是要避免的。

四档加一个退避：

    off_category  品类完全不对          终态，永不复查
    qualified     过线，已经看过了       中等冷却
    in_range      品类对、时机没到       最短冷却，这一档最值得等
    marginal      品类沾边、分数低       长冷却，但不判终态

    退避          复查后分数没提升 -> 冷却翻倍，到上限为止
                  分数提升了 -> 重置回本档基础值

从 analyzer 的输出走到 leads 那一行
──────────────────────────────────

看着像好几套标准，其实是一条链。按顺序走一遍，每一步用到哪个 dataclass 的
哪几个字段都写出来。

第一步 输入：analyzer 已经打好的一个 Assessment（models/scoring.py）

    Assessment 里装三样东西：

        llm    一个 LlmAssessment，LLM 判的四维，每维 0-10
                 icp_fit          品类对不对
                 signal_strength  事件有多硬
                 seniority        说话的人能不能拍板
                 moq_fit          量级谈不谈得成
               它还带 analysis 和 evidence_quotes 两个文字字段
               （给 Riley 看的理由和原文引用），这个模块一个都不用

        det    一个 DeterministicScores，代码算的三维，每维 0-10
                 recency          事件发生到现在多久
                 reachability     够不够得着
                 corroboration    几件互相独立的事指向同一家

        score  上面七维按 config/scoring.yaml 里的权重加起来，0-100。
               加权是 analyzer 那一层做的，这里只拿结果

    所谓「七维」就是 llm 那四维加 det 那三维，score 是它们的加权和。
    这个模块从这三样里只取两个数：llm.icp_fit 和 score。其余六维已经
    折进 score 里了，再单独拿出来看一遍等于给它们加权两次。

第二步 分档：_tier_and_base 拿那两个数四选一

    档位是一个字符串，四个取值，只在这个模块内部用 —— 落库落的是天数和
    状态，tier 只进 CooldownDecision 给报表和排查看。

        tier          命中条件                       基础天数
        off_category  icp_fit == 0                   无（终态）
        qualified     score >= qualified_score       qualified_days
        in_range      icp_fit >= in_range_icp_fit    in_range_days
        marginal      以上都不是                      marginal_days

    右边这三个天数、左边这两条线，全来自 CooldownConfig，也就是
    config/discovery.yaml 里那五个数。四条判据从上往下试、第一条命中就
    返回，为什么必须是这个顺序见 _tier_and_base。

第三步 退避：strikes 用的是另外两个数，跟档位无关

    strikes 的意思是「连续几次复查没有起色」，一次没起色记一次。它不是
    分数也不是等级，就是个计数器，存在 leads 那一行上，每次分析读出来、
    加一或者清零、再写回去。

        previous_score 是 None   第一次分析，没有上一次可比   strikes = 0
        score > previous_score   有起色                      strikes = 0
        其余                     没起色                       strikes 加 1

    它只有一个作用：把第二步那个基础天数翻 2 的 strikes 次方倍，
    再跟 max_days 取小。档位回答「这家公司
    属于哪一类」，strikes 回答「这一类里它已经连续几次没起色」，
    两者相乘才是最终的冷却天数。

第四步 输出：CooldownDecision 四个字段各自从哪来

        tier            第二步直接给的
        status          由 tier 定，只有两种取值：off_category 那一档是
                        EXCLUDED，其余三档都是 NEW。所以 status 不是另一套
                        标准，它是档位的一个函数
        cooldown_until  now 加上第三步算出来的天数。终态那一档是 None
        strikes         第三步算出来的新值，下次分析把它读回来

第五步 落库：谁把这个决定写进 leads

    这个模块不碰数据库。pipeline 拿到 CooldownDecision 之后，连同
    Assessment 一起交给 store.save_assessment，由它把七维分数、status、
    cooldown_until、strikes 写进 leads 的同一行 —— 分数和冷却写在一个
    动作里，拆开的话中间崩一下会留下一行新分数配旧冷却期。

    leads.status 有四个取值，这个模块只写得出其中两个：

        NEW        分析过了，还没推出去           这个模块写
        EXCLUDED   终态，永不复查                 这个模块写
        PUSHED     卡片真推出去之后才写            delivery 写
        IN_DRAFT   Riley 点了 approve 之后才写      draft agent 写

    过线只说明够格被推，真推出去是 delivery 的事，那一步会失败也会被
    配额截断。所以 qualified 那一档给的 status 仍然是 NEW，不是 PUSHED。

跟另外两个「多久一次」的区别
────────────────────────────

系统里有三个时间闸门，主语完全不同，别混：

    queries.cadence_days   一条 query 多久跑一次采集    主语是 query
    seen_days              一段正文多久之内算见过        主语是正文
    leads.cooldown_until   一家公司多久之后再分析一次     主语是公司

三套记忆各管各的：cadence 管采集花的钱，cooldown 管 LLM 花的钱，
seen 管的是同一件事被联播成好几条时别重复算。它们互不知道对方存在 ——
一条 query 今天轮到了、也真跑了，采回来的公司却可能一家都不进 LLM
（全在冷却期），那是正常的一天，不是哪里错了。

关于「有没有新证据」，有一个反直觉的结论，写在这里免得以后又想改：
用「这家公司又发新帖了」当放行判据是错的。供应商发帖比买方勤得多
（发广告本来就是他们的工作），按新证据放行等于把预算精确地送给
发帖最勤的那批，成本问题换个形式回来。所以分档一律时间驱动。
唯一可能的例外是 qualified 那一档 —— 它数量少、已经过了品类和事件两道闸门，
不存在被供应商刷屏的问题，将来可以让新证据缩短它的冷却。那要改
triage 的签名（得先知道这一跑哪些公司拿到了新证据），现在不做。

关于终态的判据，有一件现在做不到的事必须说清楚：供应商认不出来。
analyzer 只出四个维度，没有「这是不是供应商」这一项，而供应商的品类
恰恰是对的（他们就是做耳机的），分数上跟「品类对但今天没事发生的真买方」
完全重叠。按分数判终态一定会误杀真买方，所以 EXCLUDED 这一档只认
icp_fit == 0 这一个硬判据 —— 模型说这家公司做的东西跟我们的品类完全无关。
供应商交给 prefilter 的 headline 规则和 Riley 的 reject 按钮，退避是它现在的兜底。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import datetime, timedelta

import yaml

from models import Assessment, LeadStatus

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "discovery.yaml"

# 退避最多把基础天数翻多少次。冷却天数最后还要跟 max_days 取小，
# 所以这个数只是防止算一个天文数字出来（Python 的整数不会溢出，
# 但 2 的几百次方既没意义又难在报表里读）。
MAX_BACKOFF_SHIFTS = 8


@dataclass(frozen=True)
class CooldownConfig:
    """四档的天数和分档的两条线，来自 config/discovery.yaml。"""

    qualified_score: int  # 过线分。跟 pipeline 数 qualified_lead_count 用的是同一个数
    qualified_days: int
    in_range_days: int
    marginal_days: int
    in_range_icp_fit: int  # icp_fit 到几分算「品类对」
    max_days: int  # 退避上限


@dataclass(frozen=True)
class CooldownDecision:
    """一家公司这次分析完之后该怎么记。

    一个例子（品类对、分数没过线、上一次分析也是这个分数）：

        CooldownDecision(status=LeadStatus.NEW,
                         cooldown_until=datetime(2026, 9, 29, 7, 0, tzinfo=UTC),
                         strikes=1,
                         tier="in_range")

        读法：不是终态，四周后可以再看，这是它连续第一次复查没有起色，
        所以基础的两周被翻成了四周。

    status 只会是 NEW 或 EXCLUDED，不会是 PUSHED。分数过线只说明它够格被推，
    真的推出去是 delivery 那一层的事，那一步可能失败也可能被配额截断 ——
    在这里就把状态写成 PUSHED，等于替一件还没发生的事记账。
    """

    status: LeadStatus
    cooldown_until: datetime | None  # EXCLUDED 那一档是 None，库里有约束焊着
    strikes: int  # 连续几次复查没起色，下一次算退避要用
    tier: str  # 走的哪一档，报表和排查用


def load_cooldown_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> CooldownConfig:
    """读 config/discovery.yaml 里的分数线和 cooldown 那一段。

    输入：配置文件路径，默认是仓库里那份
    输出：一个 CooldownConfig

    一个例子：

        文件   qualified_score: 60
               cooldown: {qualified_days: 30, in_range_days: 14,
                          marginal_days: 60, in_range_icp_fit: 5, max_days: 180}
        输出   CooldownConfig(qualified_score=60, qualified_days=30,
                              in_range_days=14, marginal_days=60,
                              in_range_icp_fit=5, max_days=180)

    全程用下标不用 get，跟 selector 的 load_selection_config、prefilter 的
    load_rules 同一条口径：键缺了当场 KeyError，不退化成一个默认值。
    这里退化的后果是「某一档的冷却期悄悄变成了别的数」，而它不报错。

    qualified_score 取的是 yaml 的顶层键，不是 cooldown 段里的 —— 它同时被
    pipeline 用来数 qualified_lead_count，同一个数在配置里放两份，
    早晚会改了一处漏了另一处。
    """
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    qualified_score = int(raw["qualified_score"])
    block = raw["cooldown"]
    config = CooldownConfig(
        qualified_score=qualified_score,
        qualified_days=int(block["qualified_days"]),
        in_range_days=int(block["in_range_days"]),
        marginal_days=int(block["marginal_days"]),
        in_range_icp_fit=int(block["in_range_icp_fit"]),
        max_days=int(block["max_days"]),
    )

    # 配置读进来了，但还没确认这几个数互相讲得通。
    # 下面几条都是「填得进去但会安静地做错事」的组合，不是打字错误。
    if not 0 <= config.qualified_score <= 100:
        raise ValueError(f"qualified_score 是 0-100 的分，拿到 {config.qualified_score}")
    if not 1 <= config.in_range_icp_fit <= 10:
        raise ValueError(f"in_range_icp_fit 是 1-10 的分，拿到 {config.in_range_icp_fit}")
    for name, days in (
        ("qualified_days", config.qualified_days),
        ("in_range_days", config.in_range_days),
        ("marginal_days", config.marginal_days),
    ):
        if days < 1:
            raise ValueError(f"{name} 至少是 1 天，拿到 {days}")
        # 上限比某一档的基础值还小的话，那一档一进来就被截短，
        # 「这一档最该短」之类的设计意图会静默失效。
        if days > config.max_days:
            raise ValueError(f"{name}({days}) 超过了退避上限 max_days({config.max_days})")

    return config


def _tier_and_base(assessment: Assessment, config: CooldownConfig) -> tuple[str, int | None]:
    """分档：这家公司走四档里的哪一档，基础冷却几天。

    输入：这次的完整打分；配置
    输出：(档位名, 基础天数)。终态那一档天数是 None

    四条判据从上往下试，第一条命中就返回，后面的不再看：

        icp_fit == 0          off_category  品类完全不对，天数 None
        score >= 过线分        qualified
        icp_fit >= 品类线      in_range
        其余                   marginal

    所以优先级是 off_category > qualified > in_range > marginal。
    这个顺序是判据不是排版 —— 四档不是互斥的，有三处会真的同时成立，
    换个顺序就是另一个结果：

        品类为零、总分却高      off_category 赢。分数是七维加权来的，
                               品类为零而其余六维漂亮时总分未必低，
                               而那种公司再高分也不该联系
        品类对、又过了线        qualified 赢。两条都成立，走中等冷却
                               而不是最短的那一档 —— 它已经被看过了
        品类沾边、但过了线      qualified 赢。要推给 Riley 的东西
                               不该被判进噪音档

    只有 in_range 和 marginal 是真互斥的（icp_fit 在不在品类线上），
    它们俩之间谁先谁后无所谓。
    """
    # 判据从 LlmAssessment.off_category 来，不在这里再写一次 icp_fit == 0：
    # 用这个判据的有三处（打分封顶、这里判终态、报告分档），分家之后会出现
    # 「打分层不封顶、这里判终态」这种自相矛盾的组合，而两边各自看都是对的。
    if assessment.llm.off_category:
        return "off_category", None
    if assessment.score >= config.qualified_score:
        return "qualified", config.qualified_days
    if assessment.llm.icp_fit >= config.in_range_icp_fit:
        return "in_range", config.in_range_days
    return "marginal", config.marginal_days


def next_cooldown(
    assessment: Assessment,
    *,
    previous_score: int | None,
    strikes: int,
    config: CooldownConfig,
    now: datetime,
) -> CooldownDecision:
    """这家公司这次分析完之后，下次什么时候再看，还是永远不看了。

    输入：这次的完整打分；库里那一行上一次的分数（第一次分析是 None）；
          之前连续几次复查没起色；配置；当前时刻（必须带时区）
    输出：一个 CooldownDecision

    签名里那个单独的星号：它后面的参数只能按名字传。这里有两个相邻的
    int 参数（previous_score 和 strikes），允许按位置传的话调换它们
    不会报错，只会静默算出另一个冷却期。

    now 从参数进不用 datetime.now()，跟 analyzer 同一条规矩：拿历史数据
    回归时，写死的当前时刻才能让两次跑出同样的结果。

    三个例子：

    一、第一次分析，品类对但分数没过线

        输入   assessment.llm.icp_fit=7  assessment.score=34
               previous_score=None  strikes=0
               分档 in_range，基础 14 天；第一次分析没有「有没有起色」
               可言，退避次数保持 0，14 天不翻倍
        输出   status=NEW  cooldown_until=now+14 天  strikes=0  tier="in_range"

    二、两周后复查，分数一点没动

        输入   assessment.score=34  previous_score=34  strikes=0
               分档还是 in_range；分数没提升，退避次数加到 1，
               14 天翻成 28 天
        输出   status=NEW  cooldown_until=now+28 天  strikes=1  tier="in_range"

    三、又过了四周，这次众筹上线了，分数跳上来

        输入   assessment.score=71  previous_score=34  strikes=1
               分档变成 qualified，基础 30 天；分数提升了，退避次数
               重置成 0，30 天不翻倍
        输出   status=NEW  cooldown_until=now+30 天  strikes=0  tier="qualified"

    第三个例子里 status 仍然是 NEW 不是 PUSHED，理由见 CooldownDecision。

    退避判的是「有没有起色」不是「分数高不高」，这一条是专门给认不出来的
    供应商准备的：它们品类对、会落进最短的那一档，但分数不会自己变好，
    于是冷却期一次次翻倍，最后自己沉到底。而真买方一旦有新事件分数就上去了，
    退避会被重置，走不到深处。
    """
    if now.tzinfo is None:
        # 库里 cooldown_until 是 timestamptz。写一个不带时区的时刻进去，
        # 库会按服务器时区解释它，同一份数据在两台机器上算出不同的冷却期，
        # 而这种错查起来极痛苦。
        raise ValueError("now 必须带时区，cooldown_until 落库的是 timestamptz")

    tier, base_days = _tier_and_base(assessment, config)

    # 终态：不记冷却时刻，库里那条约束要求 EXCLUDED 的 cooldown_until 为空。
    # strikes 原样带回去，它对终态没有意义，但也没有理由在这里清掉。
    if base_days is None:
        return CooldownDecision(
            status=LeadStatus.EXCLUDED, cooldown_until=None, strikes=strikes, tier=tier
        )

    # 档位和基础天数都有了，还差退避倍数。
    # 第一次分析（previous_score 是 None）不算「没起色」—— 没有上一次可比。
    improved = previous_score is None or assessment.score > previous_score
    next_strikes = 0 if improved else strikes + 1

    days = min(base_days * 2 ** min(next_strikes, MAX_BACKOFF_SHIFTS), config.max_days)
    return CooldownDecision(
        status=LeadStatus.NEW,
        cooldown_until=now + timedelta(days=days),
        strikes=next_strikes,
        tier=tier,
    )
