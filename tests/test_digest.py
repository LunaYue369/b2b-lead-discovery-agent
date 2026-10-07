"""推卡片那一段的测试：triggers/digest.py 的 build_cards 和 push_digest。

两条路（scheduled 和 interactive）推卡调的都是这一份，所以测试也单独放，不挂在
scheduled 名下。库和 Slack 全换成桩：card_sources 自己的测试在 test_lead_db.py。
"""

from __future__ import annotations

import pytest

from models import CardQuote


def _source(name: str, *, score: int = 80, status: str = "new", evidence_count: int = 1):
    """造一份 card_sources 查回来的素材，只有公司名、分数、状态、证据条数可变。

    四句引用 q1 到 q4，四句解读（一句配一句）；第 i 条证据的原文里有 q(i+1)，
    所以一条证据时只有 q1 认得出出处
    """
    from models import CardEvidence, CardSource

    return CardSource(
        display_name=name,
        status=status,
        score=score,
        icp_fit=8,
        signal_strength=8,
        seniority=7,
        moq_fit=8,
        recency=6,
        reachability=6,
        corroboration=3,
        analysis="品类：做耳机。\n结论：值得联系。",
        quotes_mean="解读一\n解读二\n解读三\n解读四",
        quotes=("q1", "q2", "q3", "q4"),
        evidence=tuple(
            CardEvidence(f"作者{i}", "身份", "2026-09-02", f"https://x/{i}", ())
            for i in range(evidence_count)
        ),
        evidence_texts=tuple((f"正文里有 q{i + 1} 这句",) for i in range(evidence_count)),
    )


def test_build_cards_copies_everything_from_the_database_row():
    """卡片上的每一格都来自库里那一行，lead_id 来自字典的键。

    输入   {1281: 80 分、四条引用（带四句解读）、一条证据，那条证据的原文里有 q1}
    输出   一张 LeadCard，七维、分析、证据原样搬过来；引用截到三条，
           q1 分进那条证据（配上解读一），q2、q3 认不出出处，进 unplaced_quotes
    """
    from triggers.digest import build_cards

    cards, skipped = build_cards({1281: _source("Chatterly Inc.")}, {})

    assert skipped == []
    assert len(cards) == 1
    card = cards[0]
    assert card.lead_id == 1281
    assert card.display_name == "Chatterly Inc."
    assert card.score == 80 and card.icp_fit == 8 and card.corroboration == 3
    assert card.analysis == "品类：做耳机。\n结论：值得联系。"
    assert len(card.evidence) == 1
    # 引用最多三条，跟 prompt 要的「一到三条」是同一个数。模型多给的那条（q4）在卡片上截掉
    assert card.evidence[0].quotes == (CardQuote("q1", "解读一"),)
    assert card.unplaced_quotes == (CardQuote("q2", "解读二"), CardQuote("q3", "解读三"))
    assert card.unpaired_means == ""


def test_means_that_do_not_pair_one_to_one_are_kept_whole():
    """两句引用、一句解读（库里真有这种）：解读不拆，整段进 unpaired_means，各句引用不配解读。

    硬拆的话会把一句解读配到它说的不是的那句引用上
    """
    from dataclasses import replace

    from triggers.digest import build_cards

    source = replace(_source("Hushly", evidence_count=2), quotes=("q1", "q2"), quotes_mean="一句整体的解读。")

    card = build_cards({1388: source}, {})[0][0]

    assert card.unpaired_means == "一句整体的解读。"
    assert card.evidence[0].quotes == (CardQuote("q1", None),)
    assert card.evidence[1].quotes == (CardQuote("q2", None),)
    assert card.unplaced_quotes == ()


def test_means_written_on_one_line_are_split_by_sentence_when_the_count_matches():
    """Oak House Audio 那张的真形状：两句引用，两句解读用空格连成一行。按句号拆开正好两句，一句配一句。"""
    from dataclasses import replace

    from triggers.digest import build_cards

    source = replace(
        _source("Oak House Audio", evidence_count=2),
        quotes=("q1", "q2"),
        quotes_mean="这条信息表明Oak House Audio参与音频行业。 这条信息显示创始人将参与讨论。",
    )

    card = build_cards({1521: source}, {})[0][0]

    assert card.unpaired_means == ""
    assert card.evidence[0].quotes == (CardQuote("q1", "这条信息表明Oak House Audio参与音频行业。"),)
    assert card.evidence[1].quotes == (CardQuote("q2", "这条信息显示创始人将参与讨论。"),)


def test_a_lead_already_being_drafted_is_not_pushed_again():
    """Riley 已经在写稿的那条不许再推一张卡。

    in_draft 挡不住重新分析（lead_states 的分流只看 excluded 和冷却期），
    所以冷却期一过这家还会被分析、分数还在线上就会走到这里。
    再推一张卡是重复推送，而她正在给那家写稿 —— 这里是唯一挡得住它的地方。
    """
    from triggers.digest import build_cards

    cards, skipped = build_cards({1281: _source("Chatterly Inc.", status="in_draft")}, {})

    assert cards == []
    assert skipped == ["lead=1281 Chatterly Inc. 已在写稿，跳过"]


def test_build_cards_sorts_by_score_so_the_log_is_stable():
    """按分数降序、并列按 lead_id 兜成全序。

    choose 还会再排一次，这里也排是为了让「跳过了哪几条」的日志顺序
    稳定 —— 同一份数据两次跑印出来的要一样，否则对账时看不出差别。
    """
    from triggers.digest import build_cards

    sources = {3: _source("A", score=60), 1: _source("B", score=80), 2: _source("C", score=60)}
    cards, _ = build_cards(sources, {})
    assert [c.score for c in cards] == [80, 60, 60]
    # 两个 60 分的按 lead_id 定序，不是按字典里的先后
    assert [c.lead_id for c in cards] == [1, 2, 3]


def test_nothing_scored_gives_no_cards_and_no_complaints():
    """一条都没打上分是合格结果，不是故障。"""
    from triggers.digest import build_cards

    cards, skipped = build_cards({}, {})
    assert cards == [] and skipped == []


def test_push_digest_reads_everything_by_run_id_and_marks_only_what_was_sent(monkeypatch):
    """push_digest 只拿 run_id，库和 Slack 都换成桩，看它把哪几样东西递给了谁。

    输入   run_id=31，库里这一跑打了分的三家：1281（80 分）、1290（71 分，in_draft）、
           1301（30 分）；这一跑的状态是 failed；Slack 只发成了 1281 那张
    输出   返回 1
    焊住的四件
        company_count=3    分母是这一跑打了分的家数，含 in_draft 那家，不是卡片张数
        failed=True        从 runs 那一行读出来的，不是调用方传进来的
        发出去的卡片里没有 1290
        mark_pushed 只收到 [1281]，run_id=31
    """
    import datetime as dt

    from delivery import send
    from models import RunStatus
    from persistence import discovery_db, lead_db
    from triggers import digest as digest_module
    from triggers.digest import push_digest

    sources = {
        1281: _source("Chatterly Inc.", score=80),
        1290: _source("NoteNest", score=71, status="in_draft"),
        1301: _source("Low", score=30),
    }
    got: dict = {}

    monkeypatch.setattr(lead_db, "card_sources", lambda cur, run_id: sources)
    # 联系方式那一区不在这里测（见 tests/test_contacts.py 的 card_contacts），换成什么都没有
    monkeypatch.setattr(digest_module, "card_contacts", lambda cur, ids: {})
    monkeypatch.setattr(discovery_db, "run_status", lambda cur, run_id: RunStatus.FAILED)
    monkeypatch.setattr(send, "build_client", lambda: "client")
    monkeypatch.setattr(send, "read_channel", lambda: "C0TEST")

    def fake_post(client, channel, digest, **kwargs):
        got["card_ids"] = [card.lead_id for card in [*digest.a, *digest.b]]
        got.update(kwargs)
        return send.PushResult(summary_ts="1.0", posted={1281: "2.0"}, failures={})

    def fake_mark(cur, lead_ids, *, run_id, now):
        got["marked"] = (list(lead_ids), run_id)

    monkeypatch.setattr(send, "post_digest", fake_post)
    monkeypatch.setattr(lead_db, "mark_pushed", fake_mark)

    sent = push_digest(object(), run_id=31, now=dt.datetime(2026, 9, 24, 14, tzinfo=dt.UTC))

    assert sent == 1
    assert got["company_count"] == 3
    assert got["failed"] is True
    assert got["run_id"] == 31
    assert 1290 not in got["card_ids"]
    assert got["marked"] == ([1281], 31)


# ── push_interactive ─────────────────────────────────────────────────────────


@pytest.fixture
def interactive_env(monkeypatch):
    """push_interactive 碰到的库和 Slack 全换成桩，每一步往 env 里记一笔。

    输出：一个 dict，测试可以在调用前改它
        sources     card_sources 桩返回的素材，lead_id -> CardSource
        status      run_status 桩返回的状态，默认 RunStatus.COMPLETED
        already     pushed_in_run 桩返回的 lead_id 列表，默认 []（这一跑还没推过）
        failures    post_digest 桩要报失败的那几张，lead_id -> 错码
        posted      post_digest 收到的 Digest 里三档的 lead_id，例 {"a": [1], "b": [2, 3], "c": []}
        post_kwargs post_digest 收到的关键字参数
        marked      mark_pushed 收到的 (lead_ids, run_id)；没被调用是 None
        asked       card_sources 被调过几次
        harvested   run_raw_result_count 桩返回的采回条数，默认 40
        push_below_line  没过线的推不推，默认 False；仓库那份配置里的值不算数，
                         不然改一下配置这里一半的测试就换了意思
    """
    import dataclasses

    from delivery import send
    from models import RunStatus
    from persistence import discovery_db, lead_db
    from triggers import digest

    env = {
        "sources": {},
        "status": RunStatus.COMPLETED,
        "already": [],
        "failures": {},
        "posted": None,
        "post_kwargs": None,
        "marked": None,
        "asked": 0,
        "harvested": 40,
        "push_below_line": False,
    }
    real_config = digest.load_card_config()

    def fake_sources(cur, run_id):
        env["asked"] += 1
        return env["sources"]

    def fake_post(client, channel, digest, **kwargs):
        env["posted"] = {band: [c.lead_id for c in getattr(digest, band)] for band in ("a", "b", "c")}
        env["post_kwargs"] = kwargs
        ok = {
            c.lead_id: f"ts{c.lead_id}"
            for c in (*digest.a, *digest.b, *digest.c)
            if c.lead_id not in env["failures"]
        }
        return send.PushResult(summary_ts="1790126080.000200", posted=ok, failures=dict(env["failures"]))

    def fake_mark(cur, lead_ids, *, run_id, now):
        env["marked"] = (list(lead_ids), run_id)

    monkeypatch.setattr(lead_db, "card_sources", fake_sources)
    monkeypatch.setattr(digest, "card_contacts", lambda cur, ids: {})
    monkeypatch.setattr(discovery_db, "run_status", lambda cur, run_id: env["status"])
    monkeypatch.setattr(lead_db, "pushed_in_run", lambda cur, run_id: env["already"])
    monkeypatch.setattr(discovery_db, "run_raw_result_count", lambda cur, run_id: env["harvested"])
    monkeypatch.setattr(send, "post_digest", fake_post)
    monkeypatch.setattr(lead_db, "mark_pushed", fake_mark)
    monkeypatch.setattr(
        digest,
        "load_card_config",
        lambda: dataclasses.replace(real_config, push_below_line=env["push_below_line"]),
    )
    return env


def _push(**overrides):
    import datetime as dt

    from triggers.digest import push_interactive

    kwargs = {"run_id": 31, "now": dt.datetime(2026, 9, 24, 14, tzinfo=dt.UTC), "origin": "https://x/root"}
    kwargs.update(overrides)
    return push_interactive(object(), "client", "C0LEADS", **kwargs)


def test_every_card_over_the_line_goes_out_with_no_quota(interactive_env):
    """过线的全推，不截配额：B 档 12 张全发，早报那边 b_quota=10 会砍掉两张。

    输入   1 张 80 分（A 档）、12 张 65 分（B 档）、1 张 45 分（没过线）、1 张 90 分但在写稿
    输出   A 档 [1]、B 档 12 张全在，没过线和在写稿的不在；汇总带着来处的链接；
           analyzed 数的是库里打了分的全部 15 家（含在写稿那家）
    """
    sources = {1: _source("A", score=80), 99: _source("写稿中", score=90, status="in_draft")}
    sources.update({lead_id: _source(f"B{lead_id}", score=65) for lead_id in range(10, 22)})
    sources[50] = _source("低分", score=45)
    interactive_env["sources"] = sources

    result = _push()

    assert interactive_env["posted"] == {"a": [1], "b": list(range(10, 22)), "c": []}
    assert interactive_env["post_kwargs"]["origin"] == "https://x/root"
    assert interactive_env["post_kwargs"]["company_count"] == 15
    assert interactive_env["marked"] == ([1, *range(10, 22)], 31)
    assert (result.analyzed, result.qualified, result.posted) == (15, 13, 13)
    assert result.summary_ts == "1790126080.000200"


def test_only_the_cards_that_went_out_are_marked(interactive_env):
    """一张被 Slack 拒了：只给发成的记 pushed_at，失败那张带着错码回到结果里。"""
    interactive_env["sources"] = {1: _source("A", score=80), 2: _source("B", score=70)}
    interactive_env["failures"] = {2: "msg_too_long"}

    result = _push()

    assert interactive_env["marked"] == ([1], 31)
    assert result.posted == 1 and result.failures == {2: "msg_too_long"}


def test_nothing_over_the_line_posts_nothing_to_the_channel(interactive_env):
    """一家都没过线：频道里一条都不发，只把最高分交回去给 deliver 说。"""
    interactive_env["sources"] = {1: _source("A", score=45), 2: _source("B", score=30)}

    result = _push()

    assert interactive_env["posted"] is None and interactive_env["marked"] is None
    assert (result.analyzed, result.qualified, result.best_score) == (2, 0, 45)


def test_a_run_already_pushed_is_not_pushed_again(interactive_env):
    """崩溃之后的重跑：这一跑已经有卡片记过账，就一张都不发。

    这条红了的症状：deliver 在记完账之后崩溃，用户回一句话，频道里同一批卡片又出现一遍。
    """
    interactive_env["sources"] = {1: _source("A", score=80), 2: _source("B", score=70)}
    interactive_env["already"] = [1, 2]

    result = _push()

    assert interactive_env["posted"] is None and interactive_env["marked"] is None
    assert result.already_pushed == 2 and result.posted == 0


def test_a_failed_harvest_stops_before_reading_any_cards(interactive_env):
    """采集全失败：先查状态，失败就直接返回，连素材都不查，频道里什么都不发。"""
    from models import RunStatus

    interactive_env["status"] = RunStatus.FAILED

    result = _push()

    assert result.run_failed is True
    assert interactive_env["asked"] == 0 and interactive_env["posted"] is None


def test_all_analyzed_leads_in_draft_leave_no_best_score(interactive_env):
    """打了分的全是在写稿的：没有能出卡的，best_score 是 None，不拿在写稿那家的分数去报。"""
    interactive_env["sources"] = {1: _source("A", score=90, status="in_draft")}

    result = _push()

    assert (result.analyzed, result.qualified, result.best_score) == (1, 0, None)
    assert interactive_env["posted"] is None


def test_push_below_line_sends_the_cards_under_the_line_as_band_c(interactive_env):
    """开关打开：没过线的也推，排进 C 档，跟过线的一起记账；在写稿的照样挡掉。

    输入   1 张 80 分、1 张 45 分、1 张 12 分、1 张 30 分但在写稿
    输出   A [1]、C [2, 3]；三张都记 pushed_at；qualified 仍只数过线的 1 张
    """
    interactive_env["push_below_line"] = True
    interactive_env["sources"] = {
        1: _source("A", score=80),
        2: _source("低", score=45),
        3: _source("更低", score=12),
        4: _source("写稿中", score=30, status="in_draft"),
    }

    result = _push()

    assert interactive_env["posted"] == {"a": [1], "b": [], "c": [2, 3]}
    assert interactive_env["marked"] == ([1, 2, 3], 31)
    assert (result.analyzed, result.qualified, result.posted, result.below_line) == (4, 1, 3, 2)


def test_push_below_line_still_posts_when_nothing_is_over_the_line(interactive_env):
    """开关打开、一家都没过线：不再提前返回，全部推成 C 档。

    这条红了的症状：打开开关之后「0 家过线」那几跑仍然一张卡都没有，开关白开
    """
    interactive_env["push_below_line"] = True
    interactive_env["sources"] = {1: _source("A", score=45), 2: _source("B", score=30)}

    result = _push()

    assert interactive_env["posted"] == {"a": [], "b": [], "c": [1, 2]}
    assert (result.qualified, result.posted, result.best_score) == (0, 2, 45)
    assert result.summary_ts == "1790126080.000200"
