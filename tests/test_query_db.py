"""upsert_query 和 query 文本归一化的测试。

这里焊住的同样是「不报错但结果是错的」那一类，两个方向各一半：

    归一化过度   两条不同的 query 撞成一条，第二条从此不存在，库里看不出少了什么
    重跑覆盖     再跑一次 seed 把 stats 学出来的 pool 打回种子值，学习成果无声消失

不连真库。这个函数发出的 SQL 只有两条（带 on conflict 的 insert、按唯一键
回查），下面那个假游标把 text_normalized 那条唯一约束照库里的语义实现一遍
就够了 —— 要验的是「撞上之后还改不改字段」这类判断，不是 psycopg 会不会用。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from models import QueryPool, QuerySource, SourceType, normalize_query_text
from persistence.query_db import mark_ran, pickable_queries, upsert_query


class FakeCursor:
    """够 upsert_query 用的最小游标，只认它自己发出的那两条 SQL。

    输入：无
    输出：rows 里是一张 queries 表，每行一个字典

    认不出来的 SQL 直接炸：哪天 query_db 多发一条语句，要在这里当场知道，
    而不是让测试继续绿着跑一张假的表。
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._result: list[tuple] = []
        self._next_id = 1

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(sql.split())

        if text.startswith("insert into queries"):
            (
                query_text,
                text_normalized,
                source_type,
                source,
                memory_key,
                pool,
                cadence_days,
                topics,
                filters_json,
            ) = params
            # 库里那一列是 jsonb：存进去就解析了，键顺序不影响相等。
            # 假游标照做，否则测试比的是发出去的字节而不是库里的值。
            filters = json.loads(filters_json)
            # 这就是那条唯一索引。0009 之后它是两列一起比：同一句词配不同
            # 旋钮是两条 query，各建一行。撞上时 do nothing，returning 什么
            # 都不返回 —— 库里的行为就是这样。
            if any(
                r["text_normalized"] == text_normalized and r["filters"] == filters
                for r in self.rows
            ):
                self._result = []
                return
            row = {
                "id": self._next_id,
                "text": query_text,
                "text_normalized": text_normalized,
                "source_type": str(source_type),
                "source": str(source),
                "generated_by_memory_key": memory_key,
                "pool": str(pool),
                "cadence_days": cadence_days,
                "filters": filters,
                "is_active": True,
                "last_run_at": None,
            }
            self.rows.append(row)
            self._next_id += 1
            self._result = [(row["id"],)]
            return

        if text.startswith("select id from queries where text_normalized"):
            # 回查也按两列，跟上面那条唯一索引同一个口径
            self._result = [
                (r["id"],)
                for r in self.rows
                if r["text_normalized"] == params[0]
                and r["filters"] == json.loads(params[1])
            ]
            return

        raise AssertionError(f"假游标不认识这条 SQL：{text[:60]}")

    def fetchone(self):
        return self._result[0] if self._result else None


KICKSTARTER = 'Kickstarter AND (microphone OR audio OR "voice recorder")'


def seed(cur: FakeCursor, text: str = KICKSTARTER, **overrides):
    """按种子 query 的默认参数调一次 upsert_query，省得每条测试重写七个参数。"""
    kwargs = dict(
        text=text,
        source=QuerySource.MANUAL,
        source_type=SourceType.LINKEDIN_POST,
        pool=QueryPool.PINNED,
        cadence_days=1,
        topics=[],
        filters={},
    )
    kwargs.update(overrides)
    return upsert_query(cur, **kwargs)


# ---------- 归一化口径 ----------


def test_case_and_whitespace_fold():
    assert normalize_query_text("  Kickstarter   AND\n(audio)  ") == "kickstarter and (audio)"


def test_punctuation_is_not_stripped():
    """括号和引号必须留着 —— query 之间的差别全在这些符号里。

    这两条只差一个词。剥掉括号引号再比的话它们不会撞，但同类的写法差异
    （引号内外、括号分组）会让本来不同的 query 归一化成同一个键，
    第二条 insert 时被当成「已经有了」跳过，而库里看不出少了什么。
    """
    a = normalize_query_text("Kickstarter AND (microphone OR audio)")
    b = normalize_query_text("Kickstarter AND (microphone OR voice)")
    assert a != b
    assert "(" in a and '"' in normalize_query_text('x AND "voice recorder"')


# ---------- 建行与幂等 ----------


def test_first_insert_creates_row():
    cur = FakeCursor()
    query_id, created = seed(cur)
    assert (query_id, created) == (1, True)
    assert cur.rows[0]["text"] == KICKSTARTER  # 原文照原样留着，卡片要显示它
    assert cur.rows[0]["text_normalized"] == KICKSTARTER.lower()


def test_second_call_returns_same_id_and_does_not_create():
    cur = FakeCursor()
    first_id, _ = seed(cur)
    second_id, created = seed(cur)
    assert second_id == first_id
    assert created is False
    assert len(cur.rows) == 1


def test_case_difference_is_the_same_query():
    """大小写不同不是新 query。不认下来的话 seed 每跑一次多一行近似重复。"""
    cur = FakeCursor()
    seed(cur)
    _, created = seed(cur, text=KICKSTARTER.upper())
    assert created is False
    assert len(cur.rows) == 1


def test_rerun_does_not_overwrite_learned_state():
    """这条是整个函数存在的理由：重跑 seed 不许动库里已经变过的字段。

    模拟的是真实处境 —— 这条 query 被 stats 升成 proven、被 memory processor
    调慢到 3 天、已经跑过 40 次。有人又跑了一遍 seed 脚本。
    写成「顺手更新一遍字段」的话，这三样会被种子值悄悄打回去。
    """
    cur = FakeCursor()
    seed(cur)
    row = cur.rows[0]
    row["pool"] = "proven"
    row["cadence_days"] = 3
    row["last_run_at"] = "2026-08-30T07:00:00Z"

    seed(cur)  # 参数跟第一次一模一样：pool=pinned、cadence_days=1

    assert row["pool"] == "proven"
    assert row["cadence_days"] == 3
    assert row["last_run_at"] == "2026-08-30T07:00:00Z"


# ---------- 拒绝写进去的输入 ----------


def test_blank_text_is_refused():
    """空串过得了 not null，那个约束在这件事上是假安心。

    全空白的 query 归一化之后是空串，会占住 text_normalized 那个唯一键，
    之后任何一条空白 query 都被认成「已经有了」。
    """
    cur = FakeCursor()
    with pytest.raises(ValueError):
        seed(cur, text="   \n  ")
    assert cur.rows == []


def test_cadence_below_one_is_refused():
    cur = FakeCursor()
    with pytest.raises(ValueError):
        seed(cur, cadence_days=0)
    assert cur.rows == []


def test_memory_generated_needs_a_memory_key():
    cur = FakeCursor()
    with pytest.raises(ValueError):
        seed(cur, source=QuerySource.MEMORY_GENERATED, pool=QueryPool.CANDIDATE)
    assert cur.rows == []


def test_manual_query_must_not_carry_a_memory_key():
    """反方向也要挡：手写的 query 挂一条经验的名字，会让那条经验的功劳虚高。"""
    cur = FakeCursor()
    with pytest.raises(ValueError):
        seed(cur, generated_by_memory_key="crowdfunding-audio-beats-baseline")
    assert cur.rows == []


def test_typo_in_pool_fails_at_the_call_not_in_the_database():
    """裸字符串拼错要当场炸，而不是带着它一路走到库里去撞 CHECK。"""
    cur = FakeCursor()
    with pytest.raises(ValueError):
        seed(cur, pool="pined")
    assert cur.rows == []


# ---------- pickable_queries ----------
#
# 到期规则由 SQL 算（last_run_at + make_interval(days => cadence_days)），
# 假游标验不了它 —— 在 Python 里把日期算法再实现一遍，等于拿自己的实现
# 验自己的实现。那一半在 scripts/verify_persistence.py 里拿真库跑，
# 五条 fixture 覆盖到期/未到期/从没跑过/停用/差一秒。
#
# 这里只验假游标验得了的：库里的字符串有没有还原成词汇表里的枚举，
# 以及空结果不会炸。


class CannedCursor:
    """按固定行数返回结果，不解释 SQL。只用来验取回来之后那一步。"""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.seen_now = None

    def execute(self, sql: str, params: tuple = ()) -> None:
        assert "from queries" in " ".join(sql.split())
        # now 必须真的被当成参数传下去，而不是在 SQL 里写 now()。
        # 写成 now() 的话回归跑每天选出的 query 都不一样。
        (self.seen_now,) = params

    def fetchall(self):
        return self._rows


def test_rows_come_back_as_vocabulary_types():
    cur = CannedCursor(
        [
            (3, "wearable AND (microphone OR audio OR voice)", "linkedin_post",
             "proven", 1, {"authorKeywords": "Founder OR CTO"}),
            (1, "looking for a hardware manufacturing partner", "linkedin_post",
             "pinned", 7, {}),
        ]
    )
    now = dt.datetime(2026, 9, 1, 14, 0, tzinfo=dt.UTC)
    specs = pickable_queries(cur, now=now)

    assert [s.query_id for s in specs] == [3, 1]  # 顺序照库里给的来，这一层不重排
    assert specs[0].pool is QueryPool.PROVEN
    assert specs[1].pool is QueryPool.PINNED
    assert specs[0].source_type is SourceType.LINKEDIN_POST
    assert specs[1].cadence_days == 7
    # 旋钮原样跟着 spec 走：它是这条 query 身份的一部分，adapter 靠它发请求
    assert specs[0].filters == {"authorKeywords": "Founder OR CTO"}
    assert specs[1].filters == {}
    assert cur.seen_now == now


def test_nothing_due_is_an_empty_list_not_an_error():
    """一条都没到期是正常的一天，不是故障。"""
    cur = CannedCursor([])
    assert pickable_queries(cur, now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)) == []


# ---------- mark_ran ----------
#
# 不倒退那一下是 SQL 里的 greatest 做的，跟到期规则一样，验它得连真库
# （在 verify_persistence 里：标记后退出候选、拿更早的时刻再标一次不倒退）。
# 这里只验 Python 那一侧的守卫。


class RecordingCursor:
    """记下发出去的 SQL 和参数，rowcount 由外面指定。"""

    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount
        self.sql = ""
        self.params: tuple = ()

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.sql = " ".join(sql.split())
        self.params = params


def test_mark_ran_passes_the_moment_in_as_a_parameter():
    """时刻从参数进，不在 SQL 里写 now()。

    一次 run 里所有时刻用同一个钟，整跑才可复现；用 SQL 的 now() 的话，
    同一批数据重放两次会得到两套 last_run_at，之后选出的 query 也就不一样。
    """
    cur = RecordingCursor()
    moment = dt.datetime(2026, 9, 1, 14, 0, tzinfo=dt.UTC)
    mark_ran(cur, 2, ran_at=moment)

    assert "now()" not in cur.sql.replace("updated_at = now()", "")
    assert cur.params == (moment, moment, 2)  # greatest 两个位置各一次，最后是 id


def test_mark_ran_on_a_missing_query_raises():
    """update 影响 0 行 = 这个 id 不存在。

    放过去的话这条 query 永远停在上一次的 last_run_at，于是每天都被选中、
    每天都跑 —— 花的是真钱，而报表上它看起来只是「这条 query 很勤快」。
    """
    cur = RecordingCursor(rowcount=0)
    with pytest.raises(RuntimeError):
        mark_ran(cur, 999, ran_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))


# ---------- filters（采集旋钮）----------


def test_same_text_different_filters_are_two_queries():
    """0009 的核心：同一句词配不同旋钮采回来的是两批帖子，就是两条 query。

    不分开的症状全在账上：第二条永远进不了库，它采回的公司和花掉的钱记到
    第一条头上，而 query_executions 是 proven 排名的唯一依据，proven 占
    每天 60% 的额度。报表上完全正常。
    """
    cur = FakeCursor()

    first, created_first = seed(cur, filters={})
    second, created_second = seed(cur, filters={"authorKeywords": "CTO"})

    assert (created_first, created_second) == (True, True)
    assert first != second
    assert len(cur.rows) == 2


def test_same_text_same_filters_is_still_one_query():
    """旋钮一样就还是同一条，照旧一个字段都不改。"""
    cur = FakeCursor()

    first, _ = seed(cur, filters={"authorKeywords": "CTO"})
    second, created = seed(cur, filters={"authorKeywords": "CTO"})

    assert (first, created) == (second, False)
    assert len(cur.rows) == 1


def test_a_comma_in_author_keywords_is_refused():
    """实测过：逗号、空格、分号三种写法都采回零条，而且不报错。

    同一条 query 不加旋钮回来 20 条，写成 'Founder, CTO' 回来 0 条，
    只有 'Founder OR CTO' 有结果。采回零条跟「今天确实没线索」在报表上
    长得一模一样，所以这道闸必须在进库之前。
    """
    cur = FakeCursor()

    with pytest.raises(ValueError, match="OR"):
        seed(cur, filters={"authorKeywords": "Founder, CTO"})

    assert cur.rows == []


def test_or_separated_author_keywords_are_accepted():
    """闸门不能把唯一正确的写法也拦下来。"""
    cur = FakeCursor()

    query_id, created = seed(cur, filters={"authorKeywords": "Founder OR CTO"})

    assert created is True
    assert cur.rows[0]["filters"] == {"authorKeywords": "Founder OR CTO"}


def test_an_unknown_filter_key_is_refused():
    """拼错的键 Apify 会直接忽略，而我们这边它已经算进这条 query 的身份了。

    结果是凭空多出一条永远采不到东西的 query，没有任何报错。
    """
    cur = FakeCursor()

    with pytest.raises(ValueError, match="authorKeyword"):
        seed(cur, filters={"authorKeyword": "CTO"})

    assert cur.rows == []
