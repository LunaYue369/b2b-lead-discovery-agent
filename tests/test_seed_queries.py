"""seed 脚本里两个纯函数的测试：读文件和比对。

这个脚本的价值不在「灌库」——upsert_query 撞了就什么都不做，灌库天然幂等——
而在把文件和库对不上的地方报出来。所以焊住的都是「不报就会变成安静的错」：

    yaml 里键拼错     忽略它等于静默用了默认频率，那条 query 从此以错的频率跑
    yaml 里两条撞车   第二条被当成「已经有了」跳过，那条 query 从此不存在
    库里字段不同      不报的话「我明明改了文件」查不出所以然
    孤儿              改过文字的旧行还在跑、还在花钱，而报告里看不见

连库那部分不在这里：fetch_manual_queries 只有一条 select，
真正要验的到期和归并行为在 verify_persistence 里拿真库跑。
"""

from __future__ import annotations

import pytest

from models import QueryPool, SourceType
from scripts.seed_queries import DbQuery, Seed, load_seeds, plan_seed

YAML_HEAD = "queries:\n"
ONE = """\
  - text: Kickstarter AND (microphone OR audio)
    source_type: linkedin_post
    cadence_days: 1
"""


def write(tmp_path, body: str):
    path = tmp_path / "queries.yaml"
    path.write_text(YAML_HEAD + body, encoding="utf-8")
    return path


def seed_of(
    text: str, *, pool=QueryPool.CANDIDATE, cadence_days=1, filters=None
) -> Seed:
    """造一条 Seed。归一化文本按真规则算，别在测试里手写。"""
    from models import normalize_query_text

    return Seed(
        text=text,
        text_normalized=normalize_query_text(text),
        source_type=SourceType.LINKEDIN_POST,
        pool=pool,
        cadence_days=cadence_days,
        filters=filters or {},
    )


def row_of(query_id: int, seed: Seed, *, text=None, pool=None, cadence_days=None) -> DbQuery:
    """造库里的一行，默认跟这条种子一致，只覆盖要制造差异的那个字段。"""
    return DbQuery(
        query_id=query_id,
        text=text if text is not None else seed.text,
        text_normalized=seed.text_normalized,
        pool=pool or seed.pool,
        cadence_days=cadence_days or seed.cadence_days,
        is_active=True,
        filters=seed.filters,
    )


# ---------- load_seeds ----------


def test_pool_defaults_to_candidate(tmp_path):
    """没写 pool 就是 candidate：种子是没验证过的猜测，该待在会被评价的池子里。"""
    seeds = load_seeds(write(tmp_path, ONE))
    assert len(seeds) == 1
    assert seeds[0].pool is QueryPool.CANDIDATE
    assert seeds[0].cadence_days == 1
    assert seeds[0].text_normalized == "kickstarter and (microphone or audio)"


def test_pool_can_be_pinned_explicitly(tmp_path):
    seeds = load_seeds(write(tmp_path, ONE + "    pool: pinned\n"))
    assert seeds[0].pool is QueryPool.PINNED


def test_unknown_key_is_refused(tmp_path):
    """cadance_days 拼错了。忽略它等于静默用了默认频率，而那条 query
    会以错的频率一直跑下去，报表上完全看不出来。"""
    body = ONE.replace("cadence_days: 1", "cadance_days: 1")
    with pytest.raises(ValueError, match="不认识的键"):
        load_seeds(write(tmp_path, body))


def test_missing_required_key_is_refused(tmp_path):
    body = "  - text: x\n    source_type: linkedin_post\n"
    with pytest.raises(KeyError):
        load_seeds(write(tmp_path, body))


def test_bad_pool_value_is_refused(tmp_path):
    with pytest.raises(ValueError):
        load_seeds(write(tmp_path, ONE + "    pool: pined\n"))


def test_two_entries_that_normalize_the_same_are_refused(tmp_path):
    """只差大小写和空格的两条。不拦的话第二条进库时被当成「已经有了」跳过，
    库里只看到「新增 1 条」，没人看得出少了一条。"""
    twin = ONE.replace("Kickstarter AND", "KICKSTARTER   AND")
    with pytest.raises(ValueError, match="身份一样"):
        load_seeds(write(tmp_path, ONE + twin))


# ---------- plan_seed ----------


def test_empty_database_means_everything_is_new():
    seeds = [seed_of("a AND b"), seed_of("c AND d")]
    plan = plan_seed(seeds, [])
    assert plan.to_create == seeds
    assert plan.unchanged == [] and plan.differing == [] and plan.orphans == []


def test_same_row_is_unchanged():
    seed = seed_of("a AND b")
    plan = plan_seed([seed], [row_of(1, seed)])
    assert plan.to_create == []
    assert [row.query_id for _, row in plan.unchanged] == [1]


def test_learned_cadence_shows_up_as_a_difference_not_a_rewrite():
    """库里 cadence 是 3（多半是 memory processor 调慢的），文件里还是 1。
    这条不会被改回去，但必须报出来 —— 否则「我明明改了文件」没处查。"""
    seed = seed_of("a AND b", cadence_days=1)
    plan = plan_seed([seed], [row_of(1, seed, cadence_days=3)])
    assert plan.unchanged == []
    (_, row, diffs) = plan.differing[0]
    assert row.query_id == 1
    assert diffs == ["cadence_days 文件 1、库里 3"]


def test_promoted_pool_shows_up_as_a_difference():
    """同理，stats 把它升成 proven 之后，重跑 seed 不该把它打回 candidate。"""
    seed = seed_of("a AND b")
    plan = plan_seed([seed], [row_of(1, seed, pool=QueryPool.PROVEN)])
    (_, _, diffs) = plan.differing[0]
    assert diffs == [f"pool 文件 {QueryPool.CANDIDATE}、库里 {QueryPool.PROVEN}"]


def test_case_only_edit_is_reported_because_the_stored_text_stays_old():
    """把 yaml 里的写法改了大小写：归一化后一样，库里那行不会变，
    而卡片上显示的是库里那个旧写法。不报就只能靠肉眼发现。"""
    seed = seed_of("Kickstarter AND (audio)")
    plan = plan_seed([seed], [row_of(1, seed, text="kickstarter and (audio)")])
    (_, _, diffs) = plan.differing[0]
    assert diffs == ["原文写法不同，库里是 'kickstarter and (audio)'"]


def test_reworded_query_leaves_the_old_row_behind_as_an_orphan():
    """这是改 yaml 文字的真实后果：新写法建新行，旧行还在库里、还在跑。

    归一化文本是身份，所以「改一条 query 的文字」在库里等于「新增一条」，
    旧那条不会消失，两条都会花钱。
    """
    old = seed_of("wearable AND (microphone OR audio)")
    new = seed_of("wearable AND (microphone OR audio OR voice)")
    plan = plan_seed([new], [row_of(7, old)])
    assert plan.to_create == [new]
    assert [row.query_id for row in plan.orphans] == [7]
