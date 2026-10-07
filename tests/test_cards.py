"""早报那一层：分档、配额截断、折叠区抽样、块的形状。

这一层能脱离 Slack 完整测，因为它只拼字典、不发送。发送在 buttons.py，
那一半要 token 和真频道，测不了。
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from delivery.cards import (
    ACTION_APPROVE,
    ACTION_REJECT,
    ACTION_SKIP,
    BAND_COLORS,
    COMPANY_MARK,
    OUTER_RULE,
    SUMMARY_COLOR,
    SUMMARY_MARK,
    CardConfig,
    choose,
    lead_message,
    load_card_config,
    summary_message,
)
from models import (
    NO_CONTACTS,
    CardContacts,
    CardEmail,
    CardEvidence,
    CardPerson,
    CardQuote,
    CardRelation,
    CompanyProfile,
    ContactSource,
    LeadCard,
)

WEIGHTS = {
    "icp_fit": 25,
    "signal_strength": 20,
    "seniority": 10,
    "moq_fit": 15,
    "recency": 10,
    "reachability": 10,
    "corroboration": 10,
}

CONFIG = CardConfig(
    a_score=75,
    qualified_score=60,
    review_score=40,
    a_quota=5,
    b_quota=10,
    review_sample_size=5,
    push_below_line=False,
    weights=WEIGHTS,
)


def _card(score: int, lead_id: int = 0, name: str = "") -> LeadCard:
    return LeadCard(
        lead_id=lead_id or score,
        display_name=name or f"公司 {lead_id or score}",
        score=score,
        icp_fit=8,
        signal_strength=8,
        seniority=8,
        moq_fit=8,
        recency=6,
        reachability=2,
        corroboration=3,
        analysis="他们刚做完众筹，正在找代工厂。",
        evidence=(
            CardEvidence(
                author="Shah",
                role="Founder",
                when="2026-08-19",
                url="https://www.linkedin.com/posts/aaa",
                quotes=(
                    CardQuote(
                        text="We just wrapped our Kickstarter",
                        meaning="这句话是创始人本人说的，指的就是这家公司。",
                    ),
                ),
            ),
        ),
        unplaced_quotes=(),
        unpaired_means="",
        contacts=NO_CONTACTS,
    )


def test_the_real_config_file_loads():
    """仓库里那份配置要读得出来，而且三条大小关系是对的。"""
    config = load_card_config()
    assert config.a_score > config.qualified_score > config.review_score
    assert config.a_quota >= 1
    assert config.b_quota >= 1
    # 权重从 scoring.yaml 读，七维一个不少 —— 卡片上要印它，
    # 抄一份到这边的话改了 scoring.yaml 卡片会印旧数字
    assert sum(config.weights.values()) == 100
    assert set(config.weights) == set(WEIGHTS)


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ({"a_score": 60}, "B 档永远是空的"),
        ({"review_score": 60}, "边界档会跟推送区重叠"),
        ({"a_quota": 0}, "配额要至少 1"),
    ],
)
def test_broken_thresholds_blow_up_at_load_time(tmp_path, bad, match):
    """三条大小关系错了要当场炸，不许悄悄做错事。

    每一条错了的症状都是「早报看起来正常但内容是错的」：
    A 档下界不高于过线分 -> B 档永远空，推送量悄悄减半；
    边界档上界不低于过线分 -> 同一条线索既在卡片区又在标注区；
    配额 0 -> 那一档关掉了，而报表上像是「今天这档没线索」。
    """
    import yaml

    raw = {
        "qualified_score": 60,
        "review_score": 40,
        "delivery": {"a_score": 75, "a_quota": 5, "b_quota": 10, "review_sample_size": 5,
                     "push_below_line": False},
    }
    raw.update({k: v for k, v in bad.items() if k in raw})
    raw["delivery"].update({k: v for k, v in bad.items() if k not in raw})

    path = tmp_path / "discovery.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    # 权重在同目录的 scoring.yaml 里，读配置要一起读到
    (tmp_path / "scoring.yaml").write_text(
        yaml.safe_dump({"weights": {"llm": {"icp_fit": 100}, "det": {}}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=match):
        load_card_config(path)


def test_bands_split_on_score():
    """三档按分数切，C 档一条都不进早报。"""
    digest = choose(
        [_card(80), _card(76), _card(70), _card(60), _card(59), _card(40), _card(39)],
        config=CONFIG,
        rng=random.Random(0),
    )
    assert [c.score for c in digest.a] == [80, 76]
    assert [c.score for c in digest.b] == [70, 60]
    # 59 和 40 是边界档，39 低于 review_score，一档都不进
    assert digest.review_total == 2
    assert {c.score for c in digest.review_samples} == {59, 40}


def test_the_quota_cuts_the_lowest_scores_and_says_so():
    """超配额按分数截断，而且被挡住的条数要能数出来。

    配额是天花板不是地板。而天花板挡住了什么如果不说，那就跟本来
    没有线索完全一样 —— 静默丢掉是这套设计里最不能接受的一种失败。
    """
    cards = [_card(60 + i, lead_id=i) for i in range(14)]  # 14 条落在 B 档
    digest = choose(cards, config=CONFIG, rng=random.Random(0))

    assert len(digest.b) == CONFIG.b_quota
    assert digest.held_back_b == 4
    # 留下的是分数最高的那批，砍掉的是最低的
    assert min(c.score for c in digest.b) > max(
        c.score for c in cards if c not in digest.b
    )

    summary = summary_message(digest, run_id=27, company_count=20, origin=None)
    tally = str(summary["attachments"][0]["blocks"])
    assert "*配额挡住*" in tally and "4 条" in tally


def test_ties_break_on_lead_id_so_truncation_is_stable():
    """分数相同的两条按 lead_id 定序。

    不兜成全序的话排序不稳定，配额刚好卡在中间时今天留甲明天留乙，
    而两次都不报错。
    """
    cards = [_card(65, lead_id=i) for i in (7, 3, 9, 1)]
    config = CardConfig(**{**CONFIG.__dict__, "b_quota": 2})

    first = choose(cards, config=config, rng=random.Random(0))
    second = choose(list(reversed(cards)), config=config, rng=random.Random(0))
    assert [c.lead_id for c in first.b] == [1, 3]
    assert [c.lead_id for c in second.b] == [1, 3]


def test_the_review_sample_is_reproducible_for_a_given_seed():
    """折叠区是随机抽的，但同一个种子必须抽出同一批。

    rng 从参数进跟 now 从参数进是同一条理由：同一份数据两次跑不出
    同样的早报，回归对照就没有立足点。
    """
    cards = [_card(40 + i, lead_id=i) for i in range(19)]  # 全落在边界档
    a = choose(cards, config=CONFIG, rng=random.Random(42))
    b = choose(cards, config=CONFIG, rng=random.Random(42))
    c = choose(cards, config=CONFIG, rng=random.Random(7))

    assert [x.lead_id for x in a.review_samples] == [x.lead_id for x in b.review_samples]
    assert len(a.review_samples) == CONFIG.review_sample_size
    # 换个种子应该抽到不一样的（19 条里抽 5 条，撞上同一批的概率极低）
    assert [x.lead_id for x in a.review_samples] != [x.lead_id for x in c.review_samples]
    # 抽完仍按分数排：随机决定的是抽谁，不是怎么摆
    assert [x.score for x in a.review_samples] == sorted(
        (x.score for x in a.review_samples), reverse=True
    )


def test_a_day_with_nothing_over_the_line_still_gets_a_message():
    """一条都没过线也要发消息，而且要说清这是合格结果。

    不发的话，「系统挂了」和「今天真没有」在 Slack 里长得一模一样。
    而连着几天没有线索是关键词或者 ICP 该调的信号，那件事只有 Riley
    判断得了。
    """
    digest = choose([_card(30), _card(20)], config=CONFIG, rng=random.Random(0))
    assert digest.pushed_count == 0

    summary = summary_message(digest, run_id=27, company_count=15, origin=None)
    text = str(summary)
    assert "这一跑没有过线的线索" in text
    assert "不是故障" in text
    # 汇总那条的记号跟线索卡不一样：一屏扫下来要能分出总账和线索
    blocks = summary["attachments"][0]["blocks"]
    assert summary["attachments"][0]["color"] == SUMMARY_COLOR
    # run 号在第二行，不在标题里：标题该是这条消息是什么，编号是细节
    assert blocks[0]["text"]["text"] == f"{SUMMARY_MARK} 线索报告"
    # 写「采集编号」不写「第 N 次采集」：那个号是 bigserial，回滚掉的
    # 事务也会吃掉号，所以它不等于跑过几次 —— 实测第一次真跑拿到的是
    # 27，而那时候 runs 表里只有一行
    assert blocks[1]["text"]["text"] == "采集编号 27"


def test_a_lead_shows_seven_dimensions_quotes_and_three_buttons():
    """一条线索要给分项、原话和三个按钮。

    只给总分的话 Riley 没法校准（她的标注是唯一能反推阈值的东西），
    而原话是卡片上唯一的事实 —— 分数和理由都是判断。
    """
    message = lead_message(
        _card(80, lead_id=1281, name="Chatterly Inc."), band="A", run_id=27, weights=WEIGHTS
    )
    attachment = message["attachments"][0]
    blocks = attachment["blocks"]
    text = str(blocks)

    # 色条走 attachment，那是 Slack 唯一能给消息上色的机制；A 和 B 不同色
    assert attachment["color"] == BAND_COLORS["A"]
    assert BAND_COLORS["A"] != BAND_COLORS["B"]
    assert SUMMARY_COLOR not in BAND_COLORS.values()

    # 卡片上下各一条重线。外框用重线字符自己画、内部分区用原生 divider，
    # 靠两种线分出「这是卡片边界」和「这是卡片里的一个分区」
    assert blocks[0]["text"]["text"] == OUTER_RULE
    assert blocks[-1]["text"]["text"] == OUTER_RULE
    assert any(b["type"] == "divider" for b in blocks)

    # 公司名用 header 块（Slack 最大字号），前面一个记号
    assert blocks[1] == {
        "type": "header",
        "text": {"type": "plain_text", "text": f"{COMPANY_MARK} Chatterly Inc."},
    }
    # 总分标成百分制，分项标成十分制 —— 两套刻度同时出现，不标一定被读成一套
    assert "80 / 100" in blocks[2]["text"]["text"]
    # 分数不加粗：一列里七个数全是粗体等于一个都不突出
    assert "8 / 10" in blocks[3]["fields"][1]["text"]

    # 七维显示成一个区（小标题只挂第一段），实现上两段 fields ——
    # 因为 Slack 的 fields 上限是 10 项而七维要 14 项
    # 七维是一整块：两个多行 field（左列全部项目名、右列全部分数）。
    # 拆成两段 section 的话中间会多一道空行，把一张表切成两半。
    detail = blocks[3]
    assert detail["text"]["text"] == ":bar_chart: *分数明细*"
    assert len(detail["fields"]) == 2
    assert len(detail["fields"][0]["text"].splitlines()) == 7
    assert len(detail["fields"][1]["text"].splitlines()) == 7
    # fields 上限 10 项，超了 Slack 拒收整条消息
    assert all(len(b.get("fields", [])) <= 10 for b in blocks)

    # 维度名换成能读懂的一句话，后面带权重 —— 不带的话「品类 8 分」和
    # 「时效 8 分」看起来一样重，而实际差两倍半
    assert "*做的是不是音频设备*  (权重 25)" in text
    assert "*这件事有多新*  (权重 10)" in text
    assert "*几件事互相印证*" in text  # 「互证」那个词不解释看不懂

    sections = [b["text"]["text"] for b in blocks if b["type"] == "section" and "text" in b]
    assert any(t.startswith(":brain: *分数分析*") for t in sections)
    assert any(t.startswith(":mag: *帖子证据*") for t in sections)

    # 每条帖子一组、标号、纵向排：发帖人、身份、时间、链接，再证据分析（判断），再原文引用（事实）。
    # 分析和原文各有小标题，读的人分得开哪句是判断、哪句是原话
    group = next(t for t in sections if t.startswith("*1.*") and "发帖人" in t)
    assert group.splitlines() == [
        "*1.*",
        "发帖人：Shah",
        "发帖人身份：Founder",
        "时间：2026-08-19",
        "帖子链接：<https://www.linkedin.com/posts/aaa>",
        "证据分析：这句话是创始人本人说的，指的就是这家公司。",
        "原文引用：",
        "> We just wrapped our Kickstarter",
    ]

    # 降级文本必须有（不然手机推送是一条空消息），而且要写在 attachment
    # 内部：写在顶层的话它会显示在色条上方，把通知栏那句再印一遍
    assert attachment["fallback"] == "A 档 80 分 Chatterly Inc."
    assert "text" not in message

    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    assert actions[0]["block_id"] == "lead:1281:run:27"
    assert [e["action_id"] for e in actions[0]["elements"]] == [
        ACTION_APPROVE,
        ACTION_SKIP,
        ACTION_REJECT,
    ]
    # lead_id 和 run_id 在 block_id 和 value 里各带一份：Slack 两种回调
    # 载荷带回来的字段不完全一样。
    # 刻意不带 query_id —— 一条线索可能被同一跑两条 query 同时命中，
    # 塞一个进去等于任选一条、静默错归因；归因去查 lead_discovery_hits。
    values = {e["value"] for e in actions[0]["elements"]}
    assert values == {'{"lead_id":1281,"run_id":27}'}
    assert "query_id" not in "".join(values)


def test_a_lead_without_quotes_or_links_still_renders():
    """没有引用、没有链接也要拼得出块来。

    引用为空走不到这里（防幻觉闸门会把整家丢掉），但块的拼装不该
    依赖那个保证 —— 上游改一次闸门就崩掉一层 UI 是不可接受的耦合。
    """
    bare = LeadCard(
        **{
            **_card(80).__dict__,
            "evidence": (),
            "unplaced_quotes": (),
            "unpaired_means": "",
            "analysis": "",
        }
    )
    blocks = lead_message(bare, band="A", run_id=27, weights=WEIGHTS)["attachments"][0]["blocks"]
    # 按钮永远在：Riley 的判断不能因为少一个链接就没法表达
    assert any(b["type"] == "actions" for b in blocks)
    assert not any("帖子证据" in str(b) for b in blocks)
    assert not any("分数分析" in str(b) for b in blocks)


def test_means_that_do_not_pair_are_shown_once_and_unplaced_quotes_last():
    """解读没法一句配一句：整段放在「帖子证据」标题下面标「整体」，各组里只有原文引用；
    找不到出处的引用放在最后「其他原文引用」。"""
    base = _card(80)
    card = LeadCard(
        **{
            **base.__dict__,
            "evidence": (replace(base.evidence[0], quotes=(CardQuote("q1", None), CardQuote("q2", None))),),
            "unplaced_quotes": (CardQuote("q3", None),),
            "unpaired_means": "三句合起来说明他们刚众筹完。",
        }
    )
    sections = _texts(lead_message(card, band="A", run_id=27, weights=WEIGHTS))

    assert ":mag: *帖子证据*\n证据分析（整体）：三句合起来说明他们刚众筹完。" in sections
    group = next(t for t in sections if t.startswith("*1.*") and "发帖人" in t)
    assert "原文引用 1：\n> q1\n原文引用 2：\n> q2" in group
    assert "证据分析" not in group
    assert any(t.startswith("*其他原文引用*") and "> q3" in t for t in sections)


def test_the_baseline_area_shows_only_the_verdict_line():
    """基线区每条只放结论那一行，不放整段分析。

    整段五行贴进去，Slack 会自己把长文本折起来加一个「Show more」——
    那个折叠是客户端对超长 section 的处理，控制不了。而基线区的用途是
    让她快速扫一遍决定标什么，每条都要点一次展开就白设计了。
    """
    long_analysis = "\n".join(
        [
            "品类：ORBO Audio 生产音频设备，特别是耳机。",
            "事件：提到耳机在众筹期间出现了质量问题。",
            "层级：没有来自 ORBO Audio 的发言人。",
            "起订量：没有相关的需求量。",
            "结论：值得观察，暂时不联系。",
        ]
    )
    card = LeadCard(**{**_card(52, lead_id=9, name="ORBO Audio").__dict__,
                       "analysis": long_analysis})
    digest = choose([card], config=CONFIG, rng=random.Random(0))
    text = str(summary_message(digest, run_id=27, company_count=1, origin=None)["attachments"][0]["blocks"])

    assert "结论：值得观察，暂时不联系。" in text
    assert "品类：ORBO Audio" not in text
    assert "层级：" not in text


def test_a_free_form_analysis_falls_back_to_the_first_line():
    """模型没照固定格式写的时候，退回第一句而不是留空。

    留空会让基线区那一条只剩分数和公司名，正好回到「只给公司名判断不了」
    那个问题上。
    """
    card = LeadCard(**{**_card(52, lead_id=9).__dict__,
                       "analysis": "这家公司做耳机，刚做完众筹。"})
    digest = choose([card], config=CONFIG, rng=random.Random(0))
    text = str(summary_message(digest, run_id=27, company_count=1, origin=None)["attachments"][0]["blocks"])
    assert "这家公司做耳机，刚做完众筹。" in text


def test_a_failed_harvest_does_not_claim_the_empty_result_is_fine():
    """采集全失败的时候，那段说明必须换一套话。

    采集失败时也是一条线索都没有，跟「今天真的没有」共用同一段话的话，
    卡片上写着「这是合格的结果，不是故障」而实际是故障 —— 两种情况长得
    一模一样，是这套设计里最不能出的那种错。

    仍然发这一条而不是沉默：沉默的话 Riley 那天在频道里什么都看不到，
    而「系统挂了」和「今天没线索」又混成一件事了。
    """
    digest = choose([], config=CONFIG, rng=random.Random(0))

    ok = str(summary_message(digest, run_id=27, company_count=0, origin=None)["attachments"][0]["blocks"])
    assert "这是合格的结果，不是故障" in ok
    assert "采集全失败" not in ok

    bad = str(
        summary_message(digest, run_id=27, company_count=0, origin=None, failed=True)["attachments"][0][
            "blocks"
        ]
    )
    assert "采集全失败" in bad
    assert "这是合格的结果，不是故障" not in bad
    # 照样发出去，不是沉默
    assert "线索报告" in bad


def test_a_conversation_run_says_where_it_came_from():
    """对话里采的那一跑，汇总上多一行「来自这条对话」，点开回到用户 @ bot 那一句。
    定时那一跑 origin=None，这一行不出现。

    不写来处的话，两种卡片混在同一个频道里，Riley 分不清这批是每天自动来的，还是有人专门问的。
    """
    digest = choose([_card(80, 1)], config=CONFIG, rng=random.Random(0))

    with_origin = str(summary_message(digest, run_id=31, company_count=1, origin="https://x/root"))
    assert "<https://x/root|这条对话>" in with_origin

    without = str(summary_message(digest, run_id=31, company_count=1, origin=None))
    assert "这条对话" not in without


def test_push_below_line_puts_everything_under_the_line_in_band_c():
    """开关打开：没过线的全进 C 档，不截配额，不再抽基线样本；A、B 照旧。

    C 里要连低于 review_score 的也有（39 分那张）：要看的是「这一跑抓到了什么」，
    不是只看边界档
    """
    config = CardConfig(**{**CONFIG.__dict__, "push_below_line": True})
    digest = choose(
        [_card(80), _card(70), _card(59), _card(40), _card(39), _card(5)],
        config=config,
        rng=random.Random(0),
    )
    assert [c.score for c in digest.a] == [80]
    assert [c.score for c in digest.b] == [70]
    assert [c.score for c in digest.c] == [59, 40, 39, 5]
    assert digest.review_samples == ()
    assert digest.review_total == 2
    assert (digest.pushed_count, digest.qualified_count) == (6, 2)


def test_switch_off_leaves_band_c_empty():
    """开关关着：C 档是空的，行为跟加开关之前一样。"""
    digest = choose([_card(80), _card(30)], config=CONFIG, rng=random.Random(0))
    assert digest.c == ()


def test_a_band_c_card_is_grey_and_says_it_missed_the_line():
    """C 档卡片：灰色条，档位那一行写明「没过线」，按钮照样在（起草不看分数）。"""
    message = lead_message(_card(45), band="C", run_id=27, weights=WEIGHTS)
    attachment = message["attachments"][0]
    assert attachment["color"] == BAND_COLORS["C"]
    assert "C 档 · 没过线" in str(attachment["blocks"][2])
    assert attachment["fallback"].startswith("C 档 · 没过线 45 分")
    actions = next(b for b in attachment["blocks"] if b["type"] == "actions")
    assert [e["action_id"] for e in actions["elements"]] == [ACTION_APPROVE, ACTION_SKIP, ACTION_REJECT]


def test_summary_with_only_band_c_says_none_passed_but_cards_follow():
    """一家都没过线、但推了 C 档：汇总要说清下面的都是没过线的，不能说「不是故障、没有卡」。"""
    config = CardConfig(**{**CONFIG.__dict__, "push_below_line": True})
    digest = choose([_card(45), _card(20)], config=config, rng=random.Random(0))
    text = str(summary_message(digest, run_id=27, company_count=2, origin=None))
    assert "下面的卡片全是没过线的" in text
    assert "不是故障" not in text
    assert "C 没过线 2" in text

# ── 怎么联系、公司概况两区 ──────────────────────────────────────────────────


def _with_contacts(contacts):
    return LeadCard(**{**_card(74, lead_id=1388, name="Hushly").__dict__, "contacts": contacts})


def _texts(message):
    return [b["text"]["text"] for b in message["attachments"][0]["blocks"] if b.get("text")]


def test_the_contact_and_profile_sections_for_hushly():
    """公司概况是第二块；联系方式三组标号、每项一行带小标题；邮箱的可信度写成人话。"""
    contacts = CardContacts(
        website_url="https://hushly.example",
        company_page_url="https://www.linkedin.com/company/hushly/",
        profile=CompanyProfile(
            "http://www.hushly.example", 11, "2-10", 2022, "United States", ("Music",), None, {}
        ),
        emails=(
            CardEmail(
                "sam@hushly.example",
                ContactSource.LINKEDIN_EMAIL_SEARCH,
                "https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
                "sam hadid",
                "risky",
                True,
                True,
                75,
                None,
            ),
            CardEmail(
                "contact@hushly.example",
                ContactSource.WEBSITE,
                "https://home.hushly.example",
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        ),
        people=(
            CardPerson(
                "sam hadid",
                CardRelation.MEMBER,
                "Founder",
                None,
                "https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
                "https://www.linkedin.com/posts/x",
            ),
        ),
    )
    message = lead_message(_with_contacts(contacts), band="B", run_id=45, weights=WEIGHTS)
    texts = _texts(message)

    # 公司概况紧跟在分数明细后面（第二块），每项一行
    titles = [t.splitlines()[0] for t in texts]
    assert titles.index(":information_source: *公司概况*") == titles.index(":bar_chart: *分数明细*") + 1
    profile = next(t for t in texts if t.startswith(":information_source:"))
    assert profile.splitlines()[1:] == [
        "人数：11 人（公司自己选的规模档 2-10）",
        "成立：2022 年",
        "总部：United States",
        "行业：Music",
    ]

    # 三组不标号，组里每一个标 1. 2.；链接显示网址本身
    emails = next(t for t in texts if t.startswith("*邮箱*"))
    assert emails.splitlines() == [
        "*邮箱*",
        "*1.*",
        "地址：sam@hushly.example",
        "来源：sam hadid 的 LinkedIn 主页（<https://www.linkedin.com/in/sam-hadid-a1b2c3d4>）",
        "能不能收到：不确定能不能收到（服务商原话 risky）",
        "能不能事先验证：不能。对方邮箱什么地址都收，填错了也不会退信，所以没法事先确认这个地址真的有人在用",
        "公开出现过：在谷歌搜索结果里公开出现过",
        "质量分：75 / 100（找邮箱的服务商打的分）",
        "*2.*",
        "地址：contact@hushly.example",
        "来源：官网首页（<https://home.hushly.example>）",
        "可靠程度：官网上公开写着的，没有另外验证过",
    ]
    assert next(t for t in texts if t.startswith("*网站*")).splitlines() == [
        "*网站*",
        "官网：<https://hushly.example>",
        "LinkedIn 公司页：<https://www.linkedin.com/company/hushly/>",
    ]
    assert next(t for t in texts if t.startswith("*联系人*")).splitlines() == [
        "*联系人*",
        "*1.*",
        "名字：sam hadid",
        "跟这家的关系：这家公司的人",
        "职位：Founder",
        "LinkedIn 主页：<https://www.linkedin.com/in/sam-hadid-a1b2c3d4>",
        "相关帖子：<https://www.linkedin.com/posts/x>",
    ]


def test_nothing_found_yet_still_shows_the_email_group_and_no_profile():
    """还没找过：邮箱那一组写还没找到，没有网站、联系人两组，也没有公司概况那一块。"""
    texts = _texts(lead_message(_with_contacts(NO_CONTACTS), band="B", run_id=45, weights=WEIGHTS))
    assert "*邮箱*\n还没找到" in texts
    assert not any(t.startswith(("*网站*", "*联系人*", ":information_source:")) for t in texts)


def test_names_with_ampersands_are_escaped_for_slack():
    """Makers&Co 这种名字要转义成 &amp;，不然 Slack 会把 & 后面当成它的语法。"""
    person = CardPerson(
        "Makers&Co",
        CardRelation.OWN_PAGE,
        None,
        None,
        "https://www.linkedin.com/company/makers&co/posts",
        None,
    )
    contacts = CardContacts(
        website_url=None, company_page_url=None, profile=None, emails=(), people=(person,)
    )
    texts = _texts(lead_message(_with_contacts(contacts), band="B", run_id=45, weights=WEIGHTS))
    people = next(t for t in texts if t.startswith("*联系人*"))
    assert "名字：Makers&amp;Co" in people
    assert "跟这家的关系：这家公司自己的主页发的帖" in people
    # 公司主页那一项叫「公司页」，不叫「主页」
    assert "LinkedIn 公司页：<https://www.linkedin.com/company/makers&co/posts>" in people
    assert "LinkedIn 主页" not in people


def test_a_valid_email_is_translated_not_printed_raw():
    """GizmoCrowd 那张的真值：服务商标 valid。要翻成人话，不能印「服务商标的是「valid」」。"""
    email = CardEmail(
        value="michael@gadgetfund.example",
        source=ContactSource.LINKEDIN_EMAIL_SEARCH,
        source_url="https://www.linkedin.com/in/michael-gray01",
        person_name="Michael Gray",
        email_status="valid",
        catch_all=False,
        found_in_google=False,
        quality_score=80,
        other_company=None,
    )
    contacts = CardContacts(website_url=None, company_page_url=None, profile=None, emails=(email,), people=())
    texts = _texts(lead_message(_with_contacts(contacts), band="B", run_id=45, weights=WEIGHTS))
    emails = next(t for t in texts if t.startswith("*邮箱*"))
    assert "能不能收到：能收到，服务商验证过这个地址（服务商原话 valid）" in emails
    assert "注意：" not in emails


def test_an_email_from_his_other_job_says_whose_it_is():
    """Evan Park 替 Kestrel 查出来的真邮箱：是他在 Rawcut 的，卡片上紧跟来源写明，不是这家的。"""
    email = CardEmail(
        value="evan.park@rawcutstudio.example",
        source=ContactSource.LINKEDIN_EMAIL_SEARCH,
        source_url="https://www.linkedin.com/in/evanpark",
        person_name="Evan Park",
        email_status="risky",
        catch_all=True,
        found_in_google=False,
        quality_score=60,
        other_company="Rawcut",
    )
    contacts = CardContacts(website_url=None, company_page_url=None, profile=None, emails=(email,), people=())
    texts = _texts(lead_message(_with_contacts(contacts), band="B", run_id=46, weights=WEIGHTS))
    lines = next(t for t in texts if t.startswith("*邮箱*")).splitlines()
    assert lines[:5] == [
        "*邮箱*",
        "*1.*",
        "地址：evan.park@rawcutstudio.example",
        "来源：Evan Park 的 LinkedIn 主页（<https://www.linkedin.com/in/evanpark>）",
        "注意：这是他在 Rawcut 的邮箱，不是这家的",
    ]


def test_an_analysis_written_on_one_line_is_split_into_one_item_per_line():
    """Draftex Legal 那张的真值（截短）：五项用空格连成一行，卡片上要一项一行。"""
    card = replace(
        _card(15),
        analysis=(
            "品类：Draftex Legal并不涉及音频或语音设备的制造。 事件：没有任何证据。"
            "层级：发言者是该公司的联合创始人。 起订量：没有证据。 结论：不值得联系。"
        ),
    )
    texts = _texts(lead_message(card, band="B", run_id=46, weights=WEIGHTS))
    analysis = next(t for t in texts if t.startswith(":brain:"))
    assert analysis.splitlines() == [
        ":brain: *分数分析*",
        "品类：Draftex Legal并不涉及音频或语音设备的制造。",
        "事件：没有任何证据。",
        "层级：发言者是该公司的联合创始人。",
        "起订量：没有证据。",
        "结论：不值得联系。",
    ]
