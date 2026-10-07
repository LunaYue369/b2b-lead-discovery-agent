"""线索卡片怎么推进 Slack。scheduled 和 interactive 两条路共用这一份。

    build_cards        库里查回来的素材 -> 卡片，挡掉 in_draft。两条路共用
    push_digest        scheduled 用：查素材、拼卡片、分档截配额、发、记账
    push_interactive   interactive 用：同一套零件，差在四处（见它的 docstring）

    scheduled      triggers/scheduled_discovery/main.py，那一跑提交之后调 push_digest
    interactive    那张图里的 deliver 节点，run_harvest 之后调 push_interactive

两个都只收 run_id 是这份代码能共用的前提：卡片要的东西（分数、七维、分析、引用、证据链接）
全在库里，按 leads.last_analyzed_run_id 查得出来，不需要跑的那个进程内存里的
DiscoveryResult。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, replace
from typing import Any

from delivery import send
from delivery.cards import Digest, choose, load_card_config
from models import (
    NO_CONTACTS,
    CardContacts,
    CardQuote,
    CardSource,
    LeadCard,
    LeadStatus,
    RunStatus,
    find_quote,
)
from persistence import discovery_db, lead_db
from triggers.contacts import card_contacts


def build_cards(
    sources: dict[int, CardSource], contacts: dict[int, CardContacts]
) -> tuple[list[LeadCard], list[str]]:
    """把库里查回来的素材拼成卡片，挡掉正在写稿的那几家。纯函数，不连库不发消息。

    输入：lead_db.card_sources 查回来的那一份，如

        {1281: CardSource(display_name="Chatterly Inc.", status="new", score=67, ...,
                          quotes=("q1", "q2", "q3", "q4")),
         1290: CardSource(display_name="NoteNest", status="in_draft", score=71, ...)}

          contacts.card_contacts 查回来的每家的联系方式，如
        {1281: CardContacts(emails=(), people=(CardPerson(name="Ravi Shah", ...),), ...)}
          这一家不在里面就是 NO_CONTACTS
    输出：(拼好的卡片, 跳过的说明)，如

        ([LeadCard(lead_id=1281, display_name="Chatterly Inc.", score=67, ...,
                   evidence=(CardEvidence(author="Shah", ..., quotes=(CardQuote("q1", "解读一"), ...)),),
                   unplaced_quotes=(), unpaired_means="")],
         ["lead=1290 NoteNest 已在写稿，跳过"])

        引用怎么分进各条证据，见 _place_quotes

        一家都没有时是 ([], [])。那是合格结果不是故障，上层照样发汇总那一条
    """
    cards: list[LeadCard] = []

    # 跳过的说明单独返回、不在这里印：这一层是纯函数，谁去印、印在哪由调用方决定
    skipped: list[str] = []

    # 输入   {1281: 67 分, 1290: 71 分, 1301: 67 分}
    # 输出   [(1290, ...), (1281, ...), (1301, ...)]，分数降序，并列按 lead_id 升序兜成全序
    #        choose 还会再排一次，这里也排是为了让「跳过了哪几条」的日志顺序稳定 ——
    #        同一份数据两次跑印出来的要一样，否则对账时看不出差别
    ranked = sorted(sources.items(), key=lambda item: (-item[1].score, item[0]))

    # 这里一条分档和配额的判断都不做，那是 cards.choose 的事。混进来的话
    # 「今天为什么推了这几条」就要翻两个地方查
    for lead_id, source in ranked:
        # Riley 已经在给这家写稿了，刻意不推。in_draft 挡不住重新分析（lead_states 的分流
        # 只看 excluded 和冷却期），所以冷却期一过这家还会被分析、分数还在线上就会走到
        # 这里。再推一张卡是重复推送，卡片下面还会多出一个起草入口 —— 这一条是唯一挡得住
        # 它的地方。scheduled 和 interactive 两条路一个待遇
        if source.status == str(LeadStatus.IN_DRAFT):
            skipped.append(f"lead={lead_id} {source.display_name} 已在写稿，跳过")
            continue

        cards.append(
            LeadCard(
                lead_id=lead_id,
                display_name=source.display_name,
                score=source.score,
                icp_fit=source.icp_fit,
                signal_strength=source.signal_strength,
                seniority=source.seniority,
                moq_fit=source.moq_fit,
                recency=source.recency,
                reachability=source.reachability,
                corroboration=source.corroboration,
                analysis=source.analysis,
                **_place_quotes(source),
                contacts=contacts.get(lead_id, NO_CONTACTS),
            )
        )

    return cards, skipped


def _place_quotes(source: CardSource) -> dict:
    """把这家的原文引用分进它们各自出自的那条帖子，解读能一句配一句的配上。纯函数。

    输入：一家的 CardSource，要用的四格：
        quotes          ("In the Vault is on it's way ...", "Yesterday we dropped an episode ...")
        quotes_mean     "发言者提到Hushly正在制作音频节目，显示出他们在音频领域的活跃。"（一整段）
        evidence        (CardEvidence(author="sam hadid", ..., quotes=()),)
        evidence_texts  (("<sam 那条帖子的正文>", "ceo @ Hushly - ..."),)
    输出：LeadCard 的三格，如
        {"evidence": (CardEvidence(author="sam hadid", ...,
                                   quotes=(CardQuote("In the Vault ...", None),
                                           CardQuote("Yesterday ...", None))),),
         "unplaced_quotes": (),
         "unpaired_means": "发言者提到Hushly正在制作音频节目，显示出他们在音频领域的活跃。"}

    两步：

        一、解读能不能一句配一句
            quotes_mean 在库里是一整段。按换行拆开（模型没换行的，按句号拆），句数正好等于引用数就一句配一句；
            对不上（上面这个例子：两句引用、一句解读）就不拆，整段放进 unpaired_means，
            卡片上在这一区开头印一次。硬拆的话会把一句解读配到它说的不是的那句引用上
        二、每句引用出自哪条帖子
            拿打分时核对引用的同一个函数（models.scoring.find_quote），在每条帖子的原文里逐字找，
            找到哪条就放进哪一组；同一句在两条里都有，放进第一条（帖子新的那条）。
            哪条都找不到（出自没放上卡片的证据）放进 unplaced_quotes

    引用最多三条：跟 analyzer 的 prompt 要的「一到三条」是同一个数。模型偶尔多给一条时
    在这里截掉，库里那份不截
    """
    # 输入   ("q1", "q2", "q3", "q4")
    # 输出   ("q1", "q2", "q3")
    quotes = source.quotes[:3]

    # 一、解读按换行拆
    # 输入   "解读一\n解读二\n解读三"，库里有 3 句引用
    # 输出   ["解读一", "解读二", "解读三"]；句数跟库里的引用数对不上是 None
    lines = [line.strip() for line in source.quotes_mean.splitlines() if line.strip()]
    if len(lines) != len(source.quotes):
        # 模型有时候不换行，几句用空格连成一行。按换行拆对不上，就按句号再拆一次，
        # 拆出来正好等于引用数才用，规矩跟换行那一种一样
        # 输入   "这条信息表明Oak House Audio参与音频行业。 这条信息显示创始人将参与讨论。"，2 句引用
        # 输出   ["这条信息表明Oak House Audio参与音频行业。", "这条信息显示创始人将参与讨论。"]
        sentences = [s.strip() for s in re.split(r"(?<=[。！？])", source.quotes_mean) if s.strip()]
        if len(sentences) == len(source.quotes):
            lines = sentences
    means = lines if len(lines) == len(source.quotes) else None

    # 二、每句引用分进它出自的那一组
    # 输出   placed = {0: [CardQuote("In the Vault ...", None), ...]}，键是 evidence 的下标
    placed: dict[int, list[CardQuote]] = {}
    unplaced: list[CardQuote] = []
    for i, text in enumerate(quotes):
        quote = CardQuote(text=text, meaning=means[i] if means else None)
        # 输入   第 i 句引用，每条证据的原文（几段拼在一起找）
        # 输出   (0, ...) 出自第 0 条；None 哪条都没有
        found = find_quote(text, ["\n".join(t) for t in source.evidence_texts])
        if found is None:
            unplaced.append(quote)
        else:
            placed.setdefault(found[0], []).append(quote)

    return {
        "evidence": tuple(
            replace(item, quotes=tuple(placed.get(i, []))) for i, item in enumerate(source.evidence)
        ),
        "unplaced_quotes": tuple(unplaced),
        "unpaired_means": "" if means else source.quotes_mean,
    }


def push_digest(cur: Any, *, run_id: int, now: dt.datetime) -> int:
    """第六步：把这一跑打了分的线索推进 Slack，并给推成了的记上账。

    输入：游标（查卡片素材、查这一跑的状态、记推送）；这一跑的 run_id，如 31；
          这一跑的时刻，如 2026-09-24T14:00Z
    输出：真推出去的卡片条数，如 4

    五件事，顺序定死：

        ① 查库里的素材      lead_db.card_sources(cur, run_id)
        ② 拼卡片            build_cards（挡掉 in_draft）
        ③ 分档和配额        cards.choose
        ④ 发                send.post_digest（一条汇总加每条一张卡）
        ⑤ 给发成了的记账    lead_db.mark_pushed

    只要 run_id，不要内存里的 DiscoveryResult：卡片要的东西全在库里。所以 scheduled
    刚跑完、interactive 那张图里的推卡节点，调的都是这同一个函数。

    这一整步在那一跑提交之后才跑，顺序不能反：卡片是发给人看的、发出去就收不回来，
    而库那边还能回滚。先发后提交的话，事务万一回滚，Riley 手上就有一张指向不存在的
    lead 的卡片，她点按钮会写到一个不存在的 lead_id 上。

    四种情况：

    一、正常一跑

        输入   run_id=31，库里 last_analyzed_run_id=31 的有 21 家，其中 4 家过线
               查回 21 条 CardSource -> 拼 21 张卡 -> choose 切出
               A 0 条 B 4 条 -> 发 5 条消息 -> 给那 4 条记 pushed_at
        输出   4
        后果   频道里一条汇总加四张卡；库里那四行的 pushed_at 和 pushed_run_id=31
               写上了，七天后没人表态会被扫成 ignored

    二、一条都没过线

        输入   库里这一跑打了分的 15 家，最高 45 分
               卡片拼出来了，但 choose 切出 A 0 条 B 0 条
        输出   0
        后果   汇总照发（写明「这一跑没有过线的线索，这是合格结果不是故障」），
               一条 pushed_at 都不记

    三、有卡片发失败

        输入   四条里第二条被 Slack 拒了（msg_too_long）
               post_digest 返回 failures={1290: "msg_too_long"}
        输出   3
        后果   印出哪一条失败、错码是什么；只给成功那三条记 pushed_at。
               失败那条留在库里，冷却期过了会重新分析、重新有机会被推

    四、Slack 整个连不上（token 失效、bot 被踢出频道）

        输入   post_digest 的第一次调用就抛 SlackApiError
        输出   不返回，原样抛出去
        后果   这时候那一跑已经提交了：采集、打分、leads 全在，只是没推。
               而那几家的冷却期已经写好了（过线那档 30 天），所以它们
               三十天内不会再被分析、也不会再被推 —— 今天这批线索丢了
               一次机会。所以这个异常必须让整跑的退出码变成 1，而不是
               印一行警告就算了：连着几天推不出去要有人知道。
               补救是等 Slack 好了拿同一个 run_id 再调一次这个函数，这条命令
               还没写。写的时候要先挡掉 pushed_run_id 已经是这一跑的那几张，
               否则部分发成功之后重来会重复推
    """
    card_config = load_card_config()

    # ① 库里的素材
    # 输入   run_id=31
    # 输出   {1281: CardSource(Chatterly Inc., new, 67, ...), 1290: CardSource(NoteNest, in_draft, 71, ...)}
    sources = lead_db.card_sources(cur, run_id)

    # ② 拼。跳过的逐条印出来 —— 「今天为什么少了一条」必须能回答
    # 输出   cards=[LeadCard(1281, Chatterly Inc., 67, ...)]
    #        skipped=["lead=1290 NoteNest 已在写稿，跳过"]
    # 每家「怎么联系」和「公司概况」两区的东西，只读不花钱
    cards, skipped = build_cards(sources, card_contacts(cur, list(sources)))
    for line in skipped:
        print(f"[card] {line}")

    # ③ 分档和配额。rng 不传，默认现造一个：基线那几条是随机抽的，
    #    真跑不需要可复现（要复现的是回归跑，那边自己传种子）
    digest = choose(cards, config=card_config)

    # 输入   run_id=31
    # 输出   RunStatus.COMPLETED。只有 FAILED（选中的 query 一条都没采成）时汇总那段
    #        说明要换一套话，理由见 cards.summary_message 的情况五
    status = discovery_db.run_status(cur, run_id)

    # ④ 发。汇总那一条失败会抛，理由见情况四。
    # company_count 用 len(sources)，不是 len(cards)：cards 已经把 in_draft 剔掉了，
    # 拿它当分母会少报。汇总那一行写的是「分析了公司 N 家」，N 的口径是这一跑真花了
    # LLM 钱的家数 —— 少报的话「分析了 21 家推了 4 条」看起来比实际转化率高。
    # 两个簇在库里认成同一家时，模型分析了两次，这里只算一家：卡片按库里的行出，
    # 一家一张，分母也按家算
    pushed = send.post_digest(
        send.build_client(),
        send.read_channel(),
        digest,
        run_id=run_id,
        company_count=len(sources),
        weights=card_config.weights,
        # 定时那一跑不属于任何一条对话，汇总上不写来处
        origin=None,
        failed=status is RunStatus.FAILED,
    )
    print(
        f"\n推送：汇总 1 条 + 卡片 {len(pushed.posted)} 条"
        f"（A {len(digest.a)}、B {len(digest.b)}、C 没过线 {len(digest.c)}）"
    )
    for lead_id, code in pushed.failures.items():
        print(f"[slack] lead={lead_id} 发失败：{code}")

    # ⑤ 只给真发出去了的记账。记上发失败的那条的话，ignored 扫描七天后会把它算成
    #    「她没理」，而她根本没见过 —— 那条沉默会进 query 的分母，把那条 query 算差。
    #    提交在调用方那边，跟 runs 收尾不是同一个事务：那一个已经提交过了
    lead_db.mark_pushed(cur, pushed.pushed_lead_ids, run_id=run_id, now=now)
    return len(pushed.posted)


@dataclass(frozen=True)
class InteractivePush:
    """push_interactive 做完之后的账，deliver 节点拿它拼回给用户的那一句话。

    一个实际值（采回 40 条，分析了 12 家，3 家过线，全发出去了）：

        InteractivePush(harvested=40, analyzed=12, qualified=3, best_score=81, posted=3,
                        failures={}, summary_ts="1790126072.000100",
                        already_pushed=0, run_failed=False)

    每个字段回答的问题：

        harvested       这一跑一共采回来多少条帖子。analyzed 是 0 的时候拿它分开
                        「一条都没采回来」和「采回来了但一家都没进分析」
        analyzed        这一跑打了分的家数，含 in_draft 的。回话里「分析了 12 家」就是它
        qualified       过线的张数，不含 in_draft 的。push_below_line 打开时 posted 可以比它大
        best_score      没被 in_draft 挡掉的那几家里最高几分。一家都没过线时回话要说
                        「最高 45 分」；一家能出卡的都没有时是 None
        posted          真发出去的卡片张数
        below_line      posted 里没过线的张数（C 档），push_below_line 关着时是 0
        failures        发失败的那几张，lead_id -> Slack 错码，如 {1290: "msg_too_long"}
        summary_ts      频道里那条汇总的时间戳，deliver 拿它去取链接。没往频道发是 None
        already_pushed  这一跑之前已经推过几张。不是 0 说明这是崩溃之后的重跑，这次什么都没发
        run_failed      这一跑选中的 query 一条都没采成
    """

    harvested: int
    analyzed: int
    qualified: int
    best_score: int | None
    posted: int
    below_line: int = 0
    failures: dict[int, str] = field(default_factory=dict)
    summary_ts: str | None = None
    already_pushed: int = 0
    run_failed: bool = False


def push_interactive(
    cur: Any, client: Any, channel: str, *, run_id: int, now: dt.datetime, origin: str | None
) -> InteractivePush:
    """interactive 那一跑采完之后，把过线的卡片（push_below_line 打开时连没过线的）推到频道顶层，
    并给推成了的记上账。

    输入：
        cur       游标。查这一跑的状态、查素材、查推过没有、记推送
        client    Slack 客户端，send.build_client() 造的
        channel   卡片发到哪个频道，如 'C0000000001'。发在频道顶层，不进对话 thread：
                  Slack 的 thread 不能嵌套，而起草的入口是「线索卡下面的 thread 里 @bot」，
                  卡片发进对话 thread 的话，那个入口就没了
        run_id    这一跑，如 31
        now       这一刻，带时区，写进 pushed_at
        origin    这条对话开头那句话的链接，印在频道汇总上；拿不到是 None
    输出：InteractivePush，例子见那个类

    跟 push_digest 用的是同一套零件（card_sources / build_cards / post_digest / mark_pushed），
    差在四处：

        不截配额      过线的全推。每天的早报要配额，是怕一天几十张卡没人看得完；
                      这里是有人专门问出来的一批，一般只有几家
        不抽基线      早报的基线参考是给 Riley 校准分数线用的，跟这次问的东西无关
        没过线不发    频道里一条都不发，只在对话 thread 里回一句。早报没过线也发汇总，
                      是为了证明「今天跑过」；这里用户就在对话里等着，发了只是噪音。
                      push_below_line 打开时没过线的也推成 C 档，这一条只剩「一张能推的都没有」
        推过不再推    这一跑已经有卡片记过账，就一张都不发。这个节点崩溃之后用户回一句话
                      就会从它重跑，而卡片发出去就收不回来

    六种情况：

        一、正常          12 家打了分，3 家过线 -> 频道一条汇总加 3 张卡，记 3 行 pushed_at
                          -> InteractivePush(analyzed=12, qualified=3, posted=3, summary_ts="1790...")
        二、没过线        12 家都没到 60 分，最高 45，push_below_line 关着 -> 什么都不发
                          -> InteractivePush(analyzed=12, qualified=0, best_score=45, posted=0)
                          开着的话 12 张全推成 C 档，走情况一
                          -> InteractivePush(analyzed=12, qualified=0, best_score=45, posted=12, ...)
        三、一家都没分析  一条帖子都没采回来，或者采回来的全被过滤规则砍掉、全在冷却期里
                          -> 什么都不发
                          -> InteractivePush(harvested=0, analyzed=0, qualified=0, best_score=None, posted=0)
        四、采集全失败    runs 那行是 failed -> 什么都不发
                          -> InteractivePush(analyzed=0, ..., run_failed=True)
        五、重跑          这一跑已经有 3 张记过账 -> 什么都不发
                          -> InteractivePush(analyzed=12, qualified=3, posted=0, already_pushed=3)
        六、有卡片发失败  3 张里第二张被 Slack 拒了 -> 只给发成的 2 张记账
                          -> InteractivePush(..., posted=2, failures={1290: "msg_too_long"})

    提交在调用方那边：这里只写不提交，跟 push_digest 一样。
    还剩一条窄缝：卡片发出去了、调用方还没提交就崩溃，pushed_run_id 没写进去，重跑会再发一遍
    """
    card_config = load_card_config()

    # 使用 run_id 查找这一跑的结果，期待RunStatus.COMPLETED。
    # 若 failed 说明一条都没采成，则是情况四
    status = discovery_db.run_status(cur, run_id)
    if status is RunStatus.FAILED:
        return InteractivePush(
            harvested=0, analyzed=0, qualified=0, best_score=None, posted=0, run_failed=True
        )

    # 输入   run_id=31
    # 输出   40，这一跑几条 query 一共采回来多少条帖子。0 说明检索式太窄或者筛人筛得太严
    harvested = discovery_db.run_raw_result_count(cur, run_id)

    # 若这一跑成功，使用 run_id 查找其 CardSource
    # {1281: CardSource(Chatterly Inc., new, 81, ...), 1285: CardSource(Boomcrate, in_draft, 70, ...),
    #  1290: CardSource(NoteNest, new, 45, ...)}
    sources = lead_db.card_sources(cur, run_id)

    # 建立卡片
    # cards=[LeadCard(1281, 81), LeadCard(1290, 45)] 按分数降序，
    #        skipped=["lead=1285 Boomcrate 已在写稿，跳过"]
    # 每家「怎么联系」和「公司概况」两区的东西，只读不花钱
    cards, skipped = build_cards(sources, card_contacts(cur, list(sources)))
    for line in skipped:
        print(f"[card] {line}")

    # 筛选出分数过线的leads
    # qualified=[LeadCard(1281, 81)]，过线分跟早报和冷却期是同一个数
    #        best_score=81，cards 已经按分数降序，第一张就是最高的
    qualified = [card for card in cards if card.score >= card_config.qualified_score]
    best_score = cards[0].score if cards else None

    # 没过线的要不要也推
    # push_below_line=True  -> below=[LeadCard(1290, 45)]，一起推成 C 档
    # push_below_line=False -> below=[]，只推过线的
    below = (
        [card for card in cards if card.score < card_config.qualified_score]
        if card_config.push_below_line
        else []
    )
    if not qualified and not below:
        return InteractivePush(
            harvested=harvested, analyzed=len(sources), qualified=0, best_score=best_score, posted=0
        )

    # 查看这一跑的卡片是否有被推送过
    # [] 表示这一跑还没推过；[1281] 表示推过了，这次是崩溃之后的重跑
    already = lead_db.pushed_in_run(cur, run_id)
    if already:
        print(f"[card] run {run_id} 已经推过 {len(already)} 张，这次不再发")
        return InteractivePush(
            harvested=harvested,
            analyzed=len(sources),
            qualified=len(qualified),
            best_score=best_score,
            posted=0,
            already_pushed=len(already),
        )

    # 使用 qualified=[LeadCard(1281, 81), LeadCard(1287, 66)]，below=[LeadCard(1290, 45)]，a_score=75
    # 来建立 Digest(a=(1281,), b=(1287,), c=(1290,), held_back_a=0, held_back_b=0,
    #               review_samples=(), review_total=0)
    #        不走 choose：choose 会按配额截断、再随机抽几张基线，这两件这里都不做。
    #        分档照旧，卡片上的档位和颜色跟早报一个口径
    digest = Digest(
        a=tuple(card for card in qualified if card.score >= card_config.a_score),
        b=tuple(card for card in qualified if card.score < card_config.a_score),
        c=tuple(below),
        held_back_a=0,
        held_back_b=0,
        review_samples=(),
        review_total=0,
        review_score_range=f"{card_config.review_score}-{card_config.qualified_score - 1}",
    )

    # 正式推送卡片到slack
    # pushed = PushResult(summary_ts="1790126072.000100", posted={1281: "1790126073.1", 1287: ...},
    #                   failures={})
    #        company_count 用 len(sources)，含 in_draft 那几家，口径跟早报一样：
    #        「分析了 N 家」的 N 是这一跑真花了 LLM 钱打分的家数
    pushed = send.post_digest(
        client,
        channel,
        digest,
        run_id=run_id,
        company_count=len(sources),
        weights=card_config.weights,
        origin=origin,
    )
    for lead_id, code in pushed.failures.items():
        print(f"[slack] lead={lead_id} 发失败：{code}")

    # 只给真发出去的记账，理由同 push_digest 的 ⑤。
    # 这一步也是下次重跑时 pushed_in_run 查得到它们的原因
    lead_db.mark_pushed(cur, pushed.pushed_lead_ids, run_id=run_id, now=now)

    return InteractivePush(
        harvested=harvested,
        analyzed=len(sources),
        qualified=len(qualified),
        best_score=best_score,
        posted=len(pushed.posted),
        below_line=sum(1 for card in below if card.lead_id in pushed.posted),
        failures=dict(pushed.failures),
        summary_ts=pushed.summary_ts,
    )
