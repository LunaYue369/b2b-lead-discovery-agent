"""lead_db 的身份归并测试。

这里每一条焊住的都是「不报错但结果是错的」那一类：同一家公司在库里躺两行，
或者两家不同的公司被并进同一行。前者只是多花一次分析，后者会把两家的证据
永久混在一起、corroboration 虚高，而且从库里看不出来。

不连真库。跑批的 SQL 只有三条（按身份列查、建行、补身份），
下面那个假游标把它们照着 leads 表的语义实现一遍就够了 ——
要验的是「查到了就不该再建一行」这类判断，不是 psycopg 会不会用。
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from models import (
    Assessment,
    CompanyRef,
    DeterministicScores,
    LeadStatus,
    LeadType,
    LlmAssessment,
    TriageVerdict,
)
from persistence.lead_db import (
    _IDENTITY_SELECT,
    LeadRow,
    _conflicts_with,
    card_sources,
    draft_source,
    find_lead,
    lead_states,
    pushed_in_run,
    save_assessment,
    upsert_lead,
)


class FakeCursor:
    """够 lead_db 用的最小游标，只认它自己发出的那三条 SQL。

    输入：无
    输出：rows 里是一张 leads 表，每行一个字典

    认不出来的 SQL 直接炸，不静默吞掉 —— 哪天 lead_db 多发一条语句，
    要在这里当场知道，而不是让测试继续绿着跑一个假的表。
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._result: list[tuple] = []
        self._next_id = 1
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:
        # 把多行 SQL 压成一行再判前缀，免得受缩进影响
        text = " ".join(sql.split())

        if text.startswith("select id, identity_kind"):
            # "... from leads where linkedin_company_id = %s" -> 取出列名
            column = text.split(" where ")[1].split(" = ")[0]
            value = params[0]
            self._result = [
                (
                    row["id"],
                    row["identity_kind"],
                    row["identity_value"],
                    row["linkedin_company_id"],
                    row["linkedin_slug"],
                    row["domain"],
                    row["normalized_name"],
                )
                for row in self.rows
                if row[column] == value
            ]
            return

        if text.startswith("insert into leads"):
            kind, value, li_id, slug, domain, normalized, display, run_id = params
            row = {
                "id": self._next_id,
                "identity_kind": kind,
                "identity_value": value,
                "linkedin_company_id": li_id,
                "linkedin_slug": slug,
                "domain": domain,
                "normalized_name": normalized,
                "display_name": display,
                "first_seen_run_id": run_id,
            }
            self._next_id += 1
            self.rows.append(row)
            self._result = [(row["id"],)]
            return

        if text.startswith("update leads"):
            li_id, slug, domain, kind, value, lead_id = params
            row = next(r for r in self.rows if r["id"] == lead_id)
            # coalesce(旧, 新)：旧的不为空就留着
            row["linkedin_company_id"] = row["linkedin_company_id"] or li_id
            row["linkedin_slug"] = row["linkedin_slug"] or slug
            row["domain"] = row["domain"] or domain
            row["identity_kind"] = kind
            row["identity_value"] = value
            self._result = []
            return

        raise AssertionError(f"假游标没预料到这条 SQL：{text[:90]}")

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple]:
        return self._result


def _ref(name: str, li_id: str | None = None, slug: str | None = None) -> CompanyRef:
    """造一条公司引用。slug 给的是短名，这里拼成公司页 URL。"""
    return CompanyRef(
        name=name,
        linkedin_company_id=li_id,
        linkedin_url=f"https://www.linkedin.com/company/{slug}/" if slug else None,
        domain=None,
    )


# ─────────────────────────────────────────────── 同一家公司不该有两行


def test_a_name_only_ref_finds_the_row_that_was_created_from_a_strong_ref():
    """先用强身份建行，再来一条只有名字的引用，要认出是同一家。

    这一条守的是「新建时四个身份列全写」。只写 identity_key 那一级的话，
    第二条引用的前三级都是空、第四级查不到东西，同一家公司会有两行。
    """
    cur = FakeCursor()

    first, created = upsert_lead(cur, _ref("Lumen", li_id="100000001", slug="lumen"), run_id=1)
    assert created is True

    second, created = upsert_lead(cur, _ref("Lumen"), run_id=1)
    assert created is False
    assert second == first
    assert len(cur.rows) == 1


def test_a_weak_row_gets_upgraded_when_the_id_finally_shows_up():
    """先只知道名字，后来从别的通道拿到 id，那一行要就地升级，不是新建。"""
    cur = FakeCursor()

    first, _ = upsert_lead(cur, _ref("Lumen"), run_id=1)
    second, created = upsert_lead(cur, _ref("Lumen", li_id="100000001"), run_id=2)

    assert created is False
    assert second == first
    assert len(cur.rows) == 1
    assert cur.rows[0]["linkedin_company_id"] == "100000001"
    assert cur.rows[0]["identity_kind"] == "li_id"
    assert cur.rows[0]["identity_value"] == "100000001"


# ─────────────────────────────────────────────── 身份档只升不降


def test_a_strong_row_is_not_downgraded_by_a_name_only_ref():
    """已经升到 li_id 的行，被一条只有名字的引用撞上之后不许退回 name 档。

    降档不会撞唯一键，也不会丢数据（linkedin_company_id 那一列还在），
    所以它是纯静默的 —— 症状只出现在落库对账里「多少家停在弱身份 name 级」
    那个数上，而那个数是用来评估职位串兜底解析成色的。虚高了会得出反向结论。
    """
    cur = FakeCursor()

    upsert_lead(cur, _ref("Lumen", li_id="100000001"), run_id=1)
    assert cur.rows[0]["identity_kind"] == "li_id"

    upsert_lead(cur, _ref("Lumen"), run_id=2)

    assert cur.rows[0]["identity_kind"] == "li_id"
    assert cur.rows[0]["identity_value"] == "100000001"


# ─────────────────────────────────────────────── 两家公司不该并成一行


def test_two_different_companies_sharing_a_name_each_get_a_row():
    """名字归一化后相同、但 LinkedIn id 不同，那是两家公司，各建各的行。

    不挡的话第二家会在名字那一级撞进第一家的行：证据混在一起，
    而且第二家以后永远建不出自己的行 —— 第一级查不到它的 id，
    第四级每次都撞回第一家。
    """
    cur = FakeCursor()

    first, _ = upsert_lead(cur, _ref("Lumen", li_id="106"), run_id=1)
    second, created = upsert_lead(cur, _ref("Lumen", li_id="777"), run_id=1)

    assert created is True
    assert second != first
    assert len(cur.rows) == 2
    # 两行各自的身份键跟自己的 id 列一致，没有串
    assert {(r["identity_value"], r["linkedin_company_id"]) for r in cur.rows} == {
        ("106", "106"),
        ("777", "777"),
    }


def test_an_ambiguous_name_is_not_guessed():
    """一个名字对上好几行、又没有强身份可以分辨时，不猜，单独建一行。

    跟 normalize 那边 _absorb_weak_into_strong 的「歧义就放弃」同一把尺子：
    并错一次是永久的，不并只是多花一次分析。
    """
    cur = FakeCursor()

    upsert_lead(cur, _ref("Lumen", li_id="106"), run_id=1)
    upsert_lead(cur, _ref("Lumen", li_id="777"), run_id=1)

    lead_id, created = upsert_lead(cur, _ref("Lumen"), run_id=1)

    assert created is True
    assert len(cur.rows) == 3
    assert cur.rows[-1]["id"] == lead_id
    assert cur.rows[-1]["identity_kind"] == "name"


def test_an_ambiguous_name_does_not_grow_a_new_row_every_run():
    """分辨不了的名字，第二次再来要复用上次留下的那一行，不是再建一行。

    不复用的话每跑一次就多一行同名空壳，而它们永远分辨不出来 ——
    行数无界增长。复用不等于猜：那一行代表「这个名字，身份不明」，
    跟旁边两家有强身份的仍然是分开的三行。
    """
    cur = FakeCursor()
    upsert_lead(cur, _ref("Lumen", li_id="106"), run_id=1)
    upsert_lead(cur, _ref("Lumen", li_id="777"), run_id=1)

    first, created_first = upsert_lead(cur, _ref("Lumen"), run_id=1)
    again, created_again = upsert_lead(cur, _ref("Lumen"), run_id=2)

    assert created_first is True
    assert created_again is False
    assert again == first
    assert len(cur.rows) == 3


def test_a_stronger_level_keeps_looking_after_it_misses():
    """强的那级查不到不算数，还要继续往下查 —— 否则弱身份行永远接不上。

    这是 find_lead 不在第一个非空身份上停手的理由，也是上面那条
    「两家同名不能并」必须靠 _conflicts_with 而不是靠提前收手来挡的原因。
    """
    cur = FakeCursor()

    upsert_lead(cur, _ref("Pinecrest"), run_id=1)
    row = find_lead(cur, _ref("Pinecrest", li_id="100000006"))

    assert row is not None
    assert row.identity_kind == "name"


# ─────────────────────────────────────────────── 冲突判定本身


def _row(**kwargs) -> LeadRow:
    base = LeadRow(
        id=1,
        identity_kind="li_id",
        identity_value="106",
        linkedin_company_id="106",
        linkedin_slug=None,
        domain=None,
        normalized_name="lumen",
    )
    return replace(base, **kwargs)


def test_conflict_only_counts_when_both_sides_have_a_value():
    """一方为空不算分歧 —— 那正是「上次只知道名字、这次拿到 id」要接上的情况。"""
    columns = {
        "linkedin_company_id": "777",
        "linkedin_slug": None,
        "domain": None,
        "normalized_name": "lumen",
    }
    # 库里那行没有 id：不是分歧，是待补
    assert _conflicts_with(_row(linkedin_company_id=None), columns, matched_level=3) is False
    # 库里那行 id 是别家的：是分歧
    assert _conflicts_with(_row(linkedin_company_id="106"), columns, matched_level=3) is True


def test_a_weaker_disagreement_does_not_veto_a_stronger_match():
    """id 对上了就是同一家，名字写法不同是正常的，不许拿它否掉。

    'Lumen' 和 'Lumen Technology Co., Ltd.' 是同一家公司在两个数据源里的
    两种写法。拿名字的分歧去否掉 id 的一致，会把最可靠的那条归并砍掉。
    """
    columns = {
        "linkedin_company_id": "106",
        "linkedin_slug": None,
        "domain": None,
        "normalized_name": "lumen technology",
    }
    assert _conflicts_with(_row(normalized_name="lumen"), columns, matched_level=0) is False


def test_upsert_reports_whether_the_row_is_new():
    """created 就是 novel_lead_count 的分子，第二次遇到同一家必须是 False。"""
    cur = FakeCursor()

    _, first = upsert_lead(cur, _ref("Lumen", li_id="106"), run_id=1)
    _, again = upsert_lead(cur, _ref("Lumen", li_id="106"), run_id=2)

    assert first is True
    assert again is False


def test_fake_cursor_refuses_sql_it_does_not_model():
    """假游标只认三条 SQL。多出来的语句要炸，不然测试会绿着跑一张假表。"""
    cur = FakeCursor()
    with pytest.raises(AssertionError):
        cur.execute("delete from leads")


def test_the_select_column_order_matches_leadrow():
    """_IDENTITY_SELECT 的列顺序必须跟 LeadRow 的字段顺序逐一对上。

    find_lead 用 LeadRow(*record) 直接展开，靠的就是这个一致。哪天在
    select 里插一列或者调个顺序，不会报错，只会把 slug 装进 domain 字段 ——
    然后冲突判定拿着错位的值去比，症状是归并时对时不对。
    """
    columns = _IDENTITY_SELECT.split("select ")[1].split(" from ")[0]
    names = [c.strip() for c in columns.split(",")]
    fields = [f.name for f in dataclasses.fields(LeadRow)]
    assert names == fields


# ─────────────────────────────────────────────── 分流和写分数


class FakeStateCursor:
    """够 lead_states 和 save_assessment 用的最小游标。

    输入：无
    输出：rows 里是一张 leads 表，每行一个字典

    跟上面那个假游标分开写，是因为两者假装的东西不一样：那个照着身份列
    的语义实现三条 SQL，这个只负责把行还回来、把更新记下来。

    「还在冷却期吗」这两个布尔值直接存在行上，不由这个假游标去算 ——
    真库里它们是 SQL 拿库的 now() 比出来的，那正是刻意交给库做的事
    （客户端自己凑一个「现在」会让两台机器分流出不同结果）。在假游标里
    重新实现一遍时间比较，等于用我们自己写的第二份实现去证明第一份对。
    这里要验的是拿到这两个布尔值之后的判决、缺行检测和写回。
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._result: list[tuple] = []
        self.rowcount = 0
        self.updates: list[tuple] = []
        self.last_sql = ""

    def add(
        self,
        lead_id: int,
        *,
        is_excluded: bool = False,
        still_cooling: bool = False,
        score: int | None = None,
        strikes: int = 0,
    ) -> None:
        self.rows.append(
            {
                "id": lead_id,
                "is_excluded": is_excluded,
                "still_cooling": still_cooling,
                "score": score,
                "cooldown_strikes": strikes,
            }
        )

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(sql.split())

        if text.startswith("select id, status = 'excluded'"):
            wanted = set(params[0])
            self._result = [
                (r["id"], r["is_excluded"], r["still_cooling"], r["score"], r["cooldown_strikes"])
                for r in self.rows
                if r["id"] in wanted
            ]
            return

        if text.startswith("update leads set score"):
            self.updates.append(params)
            self.last_sql = text
            lead_id = params[-1]
            self.rowcount = sum(1 for r in self.rows if r["id"] == lead_id)
            self._result = []
            return

        raise AssertionError(f"假游标没预料到这条 SQL：{text[:90]}")

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple]:
        return self._result


def test_lead_states_gives_every_lead_its_own_verdict():
    """三种判决逐家给，而且带回上一次的分数和退避次数。"""
    cur = FakeStateCursor()
    cur.add(12)  # 发现过、还没分析过
    cur.add(34, still_cooling=True, score=34, strikes=1)
    cur.add(56, score=71)  # 冷却期已过
    cur.add(78, is_excluded=True, score=8)

    states = lead_states(cur, [12, 34, 56, 78])

    assert states[12].verdict is TriageVerdict.RESEARCHABLE
    assert states[34].verdict is TriageVerdict.COOLING
    assert states[56].verdict is TriageVerdict.RESEARCHABLE
    assert states[78].verdict is TriageVerdict.EXCLUDED

    # 0 分和「还没判过」不是一回事，退避那一步对两者的处理相反。
    assert states[12].previous_score is None
    assert states[34].previous_score == 34
    assert states[34].cooldown_strikes == 1
    assert states[12].cooldown_strikes == 0


def test_lead_states_raises_when_a_lead_is_not_in_the_table():
    """传进来的 id 有一个查不到就当场炸，不要静默少一项。

    少的那一项在调用方表现成 KeyError，或者更糟 —— 三堆分流之和对不上
    query_executions 那条 CHECK，而那时只给得出一个约束名。
    """
    cur = FakeStateCursor()
    cur.add(12)

    with pytest.raises(ValueError, match="查不到"):
        lead_states(cur, [12, 99])


def test_lead_states_takes_repeated_ids_once():
    """同一个 id 传两次只占一项，不算缺行。

    一家公司被今天两条 query 同时命中时，调用方拼出来的那个列表里它会
    出现两次。那不是错误，是这一层要吸收掉的正常情况。
    """
    cur = FakeStateCursor()
    cur.add(12)

    states = lead_states(cur, [12, 12])

    assert list(states) == [12]


def test_lead_states_does_not_query_for_an_empty_list():
    """一家都没有的时候不发 SQL。空列表拼进 any(%s) 是白跑一次库。"""
    cur = FakeStateCursor()
    assert lead_states(cur, []) == {}


def test_save_assessment_writes_the_seven_dimensions_and_the_cooldown_together():
    """七维分、引用、状态、冷却时刻、退避次数、哪一跑一条 UPDATE 写完。

    分开写的话中间崩一下会留下一行有新分数、却还带着旧冷却期的数据，
    而那一行在报表上完全正常。引用要换成 list 才进得了 text[]。
    """
    cur = FakeStateCursor()
    cur.add(12, score=34, strikes=1)
    now = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
    until = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)

    save_assessment(
        cur,
        12,
        assessment=Assessment(
            llm=LlmAssessment(
                icp_fit=7,
                signal_strength=6,
                seniority=4,
                moq_fit=8,
                analysis="reason",
                evidence_quotes=("quote",),
                evidence_means="means",
            ),
            det=DeterministicScores(recency=10, reachability=6, corroboration=3),
            score=58,
        ),
        status=LeadStatus.NEW,
        cooldown_until=until,
        strikes=2,
        analyzed_at=now,
        run_id=31,
    )

    (params,) = cur.updates
    # 参数顺序就是 SQL 里的 set 顺序，最后一个是 where 的 lead_id。
    # ["quote"] 是 list 不是 tuple：tuple 进不了 text[]
    # 末尾的 fmt: skip 是让 ruff format 别动这一句：它会把十七个值拆成一行一个，
    # 那样看不出第一行是 llm 那半、第二行是 det 往后那半
    assert params == (
        58, 7, 6, 4, 8, "reason", ["quote"], "means",
        10, 6, 3, LeadStatus.NEW, until, 2, now, 31, 12,
    )  # fmt: skip


def test_save_assessment_raises_when_the_row_is_gone():
    """改不到行就抛。UPDATE 找不到行不报错，只会安静地什么都不做。"""
    cur = FakeStateCursor()
    cur.add(12)

    with pytest.raises(ValueError, match="没找到"):
        save_assessment(
            cur,
            99,
            assessment=Assessment(
                llm=LlmAssessment(
                    icp_fit=0,
                    signal_strength=0,
                    seniority=0,
                    moq_fit=0,
                    analysis="",
                    evidence_quotes=(),
                ),
                det=DeterministicScores(recency=0, reachability=0, corroboration=0),
                score=0,
            ),
            status=LeadStatus.EXCLUDED,
            cooldown_until=None,
            strikes=0,
            analyzed_at=datetime(2026, 9, 1, 7, 0, tzinfo=UTC),
            run_id=31,
        )


def test_save_assessment_does_not_steal_a_lead_back_from_the_draft_agent():
    """in_draft 那一档不许被打回 new。

    冷却期一过，一条已经交给 draft agent 的线索会被重新分析，而 cooldown
    的判决只会给 NEW 或 EXCLUDED。照直写的话 status 被打回 new，不报错，
    症状是那条线索几天后又出现在给 Riley 的卡片里，而她已经在写稿了。
    修正七留下 in_draft 这一档，要防的就是这件事。
    """
    sql = _save_assessment_sql()
    assert "case" in sql and "in_draft" in sql, "status 那一列必须挡住 in_draft"


def _save_assessment_sql() -> str:
    """把 save_assessment 发出去的那条 SQL 抓回来。"""
    cur = FakeStateCursor()
    cur.add(12)
    save_assessment(
        cur,
        12,
        assessment=Assessment(
            llm=LlmAssessment(
                icp_fit=7,
                signal_strength=6,
                seniority=4,
                moq_fit=8,
                analysis="reason",
                evidence_quotes=("quote",),
            ),
            det=DeterministicScores(recency=10, reachability=6, corroboration=3),
            score=58,
        ),
        status=LeadStatus.NEW,
        cooldown_until=datetime(2026, 9, 29, 7, 0, tzinfo=UTC),
        strikes=1,
        analyzed_at=datetime(2026, 9, 1, 7, 0, tzinfo=UTC),
        run_id=31,
    )
    return cur.last_sql


# ─────────────────────────────────────────────── 卡片素材


class FakeCardCursor:
    """够 card_sources 用的最小游标：按顺序把事先排好的几批行还回去。

    输入：results，每条 SQL 依次拿走一批，如 [第一条查回的 leads 行, 第二条查回的证据行]
    输出：sqls 里记着发出去的每条 SQL 和参数，测试拿它看发了几条、带了什么参数

    不去解析 SQL：card_sources 的判断全在拿到行之后（分组、截断、退回 identity_value），
    这里要验的是那一段。SQL 本身对不对只有真库验得出来。
    """

    def __init__(self, results: list[list[tuple]]) -> None:
        self._results = list(results)
        self._rows: list[tuple] = []
        self.sqls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.sqls.append((sql, params))
        self._rows = self._results.pop(0)

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        # draft_source 第一条 SQL 用它：那批行是空的就是库里没有这一行
        return self._rows[0] if self._rows else None


def _lead_row(lead_id: int, name: str, *, status: str = "new", score: int = 67) -> tuple:
    """card_sources 第一条 SQL 查回的一行，列序跟那条 select 一样。"""
    return (
        lead_id, name, f"id-{lead_id}", status, score,
        8, 8, 8, 8, 6, 2, 3,
        "analysis", "means", ["quote one", "quote two"],
    )  # fmt: skip


def test_card_sources_groups_evidence_by_lead_and_keeps_the_newest_three():
    """两家公司：一家五篇证据（其中一篇没链接），一家一篇都没有。

    输入   第一条 SQL 查回 1281、1290 两行；第二条查回 1281 的五行证据（已按时间倒序），
           第二行的链接是空的；第四行是转发的帖子（有原帖正文和原帖作者的职位串）
    输出   1281 拿到最新的三条有链接的证据，和它们各自的原文（拿来认引用出自哪一条）；
           1290 照样在结果里，evidence 是空元组。
           引用从 list 变成 tuple，公司名是空串的那家退回 identity_value
    """
    newest = datetime(2026, 9, 20, tzinfo=UTC)
    cur = FakeCardCursor(
        [
            [_lead_row(1281, "Chatterly Inc."), _lead_row(1290, "", status="in_draft", score=71)],
            [
                (1281, "Shah", "Founder", newest, "https://x/1", "正文一", None, None),
                (1281, "Lee", "CTO", datetime(2026, 9, 18, tzinfo=UTC), None, "正文二", None, None),
                (1281, "Park", "PM", datetime(2026, 9, 10, tzinfo=UTC), "https://x/3", "正文三", None, None),
                (1281, "Ann", "Ops", datetime(2026, 9, 5, tzinfo=UTC), "https://x/4",
                 "转发的话", "原帖正文", "原帖职位"),
                (1281, "Old", "Intern", datetime(2026, 8, 1, tzinfo=UTC), "https://x/5",
                 "正文五", None, None),
            ],
        ]
    )

    got = card_sources(cur, 31)

    assert set(got) == {1281, 1290}
    # 没有链接那篇不收，所以最新的三条是第一、三、四篇
    assert [e.url for e in got[1281].evidence] == ["https://x/1", "https://x/3", "https://x/4"]
    assert got[1281].evidence[0].when == "2026-09-20"
    assert got[1281].evidence[0].quotes == ()
    # 每条证据的原文，顺序：正文、原帖正文、职位串、原帖作者的职位串，空的不要
    assert got[1281].evidence_texts == (
        ("正文一", "Founder"),
        ("正文三", "PM"),
        ("转发的话", "原帖正文", "Ops", "原帖职位"),
    )
    assert got[1281].quotes == ("quote one", "quote two")
    assert got[1281].score == 67 and got[1281].corroboration == 3
    # 一篇证据都没有照样出卡片，只是少链接那一块
    assert got[1290].evidence == () and got[1290].evidence_texts == ()
    # in_draft 原样带出来，挡不挡是 build_cards 的事
    assert got[1290].status == "in_draft"
    # 空名字退回 identity_value，不给一张没有标题的卡
    assert got[1290].display_name == "id-1290"
    # 第一条 SQL 按 run_id 查，第二条按查回来的那几家查
    assert cur.sqls[0][1] == (31,)
    assert "last_analyzed_run_id" in cur.sqls[0][0]
    assert cur.sqls[1][1] == ([1281, 1290],)


def test_card_sources_stops_after_one_query_when_the_run_scored_nobody():
    """这一跑一家都没打上分：返回空字典，第二条 SQL 不发（空列表拼进 any(%s) 是白跑一次库）。"""
    cur = FakeCardCursor([[]])

    assert card_sources(cur, 31) == {}
    assert len(cur.sqls) == 1


def test_draft_source_reads_the_latest_assessment_and_full_evidence():
    """一家公司：最近一次打分的结论，加两条证据的全文和通道。

    输入   第一条 SQL 查回那一行 leads（分数、分析、解读、引用）；
           第二条查回两条证据（已按时间倒序）：一条本人发的，一条转发（带原帖作者和原帖正文）、
           通道是 mentioned（从第三方口中得知）
    输出   引用从 list 变成 tuple，通道转回 LeadType，日期只到天，没有职位串的给空串
    """
    cur = FakeCardCursor(
        [
            [(72, "Acme Audio just finished a Kickstarter.", "众筹刚结束",
              ["We just wrapped our Kickstarter"])],
            [
                (2720, "author", "Dana Lee", "Founder @ Acme Audio", datetime(2026, 9, 20, 8, 30, tzinfo=UTC),
                 "https://x/1", "We just wrapped our Kickstarter.", None, None),
                (2733, "repost_mentioned", "Sam Park", None, datetime(2026, 9, 18, tzinfo=UTC),
                 "https://x/2", "Look at this", "Audio Weekly", "Congrats to @Acme Audio!"),
            ],
        ]
    )  # fmt: skip

    got = draft_source(cur, 1289)

    assert got is not None
    assert got.score == 72
    assert got.quotes == ("We just wrapped our Kickstarter",)
    first, second = got.evidence
    assert (first.signal_id, first.lead_type, first.when) == (2720, LeadType.AUTHOR, "2026-09-20")
    assert first.lead_type.is_first_person
    assert second.lead_type == LeadType.REPOST_MENTIONED and not second.lead_type.is_first_person
    assert second.role == ""
    assert (second.reposted_author, second.reposted_text) == ("Audio Weekly", "Congrats to @Acme Audio!")
    # 两条 SQL 都按 lead_id 查，第二条带上条数上限
    assert cur.sqls[0][1] == (1289,)
    assert cur.sqls[1][1] == (1289, 3)


def test_draft_source_is_none_when_the_lead_does_not_exist():
    """leads 里没有这一行：返回 None，第二条 SQL 不发。"""
    cur = FakeCardCursor([[]])

    assert draft_source(cur, 9999) is None
    assert len(cur.sqls) == 1


def test_draft_source_of_a_lead_never_scored_has_empty_quotes():
    """打分那几列是空的（没被打过分）：引用给空元组不是 None，证据照样查。"""
    cur = FakeCardCursor([[(None, None, None, None)], []])

    got = draft_source(cur, 1289)

    assert got is not None
    assert got.score is None and got.quotes == () and got.evidence == ()


def test_pushed_in_run_asks_by_pushed_run_id():
    """查的是 mark_pushed 写的那一列 pushed_run_id，不是 pushed_at。

    按 pushed_at 查的话分不出是哪一跑推的：同一家上个月被另一跑推过，这一跑的重跑就会以为
    自己已经推过了，一张都不发。
    """
    cur = FakeCardCursor([[(1281,), (1287,)]])

    assert pushed_in_run(cur, 31) == [1281, 1287]
    sql, params = cur.sqls[0]
    assert "pushed_run_id = %s" in sql and params == (31,)
