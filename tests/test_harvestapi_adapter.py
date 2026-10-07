"""HarvestAPI adapter 的字段口径测试。

adapter 只做翻译，所以这里焊的都是「供应商少给一个字段时我们怎么办」——
答案统一是炸，不是兜一个空值。兜出来的空值会一路流到库里，
症状出现在好几天之后的另一层，那时候已经没人记得它是从哪来的了。
"""

from __future__ import annotations

import json

import pytest

from adapters.harvestapi_posts import (
    HarvestApiPostsAdapter,
    RecordedPostsAdapter,
    _actor_company,
    build_input,
    read_dataset,
    to_raw_signal,
    write_dataset,
)
from models import QueryPool, QuerySpec, SourceType


def test_a_profile_author_has_no_company_here():
    """个人作者一律返回 None，公司身份留给 normalize 从职位串解析。

    author.id 对个人来说是个人 URN（ACoAA 开头）不是公司 id，
    往 linkedin_company_id 里塞会造出一堆假身份键。
    """
    author = {"type": "profile", "id": "ACoAAxxxx", "name": "Someone"}
    assert _actor_company(author, "ext-1") is None


def test_a_company_author_becomes_a_strong_ref():
    """公司主页作者：author.id 就是 LinkedIn 的公司数字 id，身份键梯度最高一级。"""
    author = {
        "type": "company",
        "id": 100000001,
        "name": "Lumen",
        "linkedinUrl": "https://www.linkedin.com/company/lumen/",
    }
    ref = _actor_company(author, "ext-1")

    assert ref is not None
    assert ref.name == "Lumen"
    assert ref.linkedin_company_id == "100000001"
    assert ref.identity_key == ("li_id", "100000001")


def test_a_company_author_without_a_name_blows_up():
    """公司主页作者缺 name 时炸，不拿空串兜底。

    以前那句 `author.get("name") or ""` 编出来的空串会一路流进库里：
    normalized_name 写成空字符串（空串过得了 not null），于是那一行以后
    只能靠 id 找回来 —— 同一家公司再从职位串被解析出来时接不上，库里多一行；
    display_name 同样是空的，Slack 卡片上公司名一片空白。
    """
    author = {
        "type": "company",
        "id": 100000001,
        "linkedinUrl": "https://www.linkedin.com/company/lumen/",
    }
    with pytest.raises(ValueError, match="没有 name"):
        _actor_company(author, "ext-1")


def test_the_error_says_which_post_it_came_from():
    """炸的时候要能定位到是哪一条 post，不然 191 条里找不出是谁。"""
    author = {"type": "company", "id": 1, "name": ""}
    with pytest.raises(ValueError, match="ext-42"):
        _actor_company(author, "ext-42")


# ─────────────────────────────────────────────── adapter 类的编排


def _spec(text: str = "wearable AND audio", source_type: SourceType = SourceType.LINKEDIN_POST):
    return QuerySpec(
        query_id=30,
        text=text,
        source_type=source_type,
        pool=QueryPool.CANDIDATE,
        cadence_days=1,
        filters={},
    )


def test_one_query_one_apify_run(monkeypatch):
    """一次 fetch 只把这一条 query 交给 actor。

    一个 run 传多条的话，Apify 只给一个 dataset、一个计费总数，而
    billed_item_count 是 per-query 的列 —— 摊出来的数在「一条 query 命中
    大量评论、另一条几乎没有」时是错的，而且看不出来。
    """
    seen: dict = {}

    def fake_from_apify(queries, **kwargs):
        seen["queries"] = queries
        seen.update(kwargs)
        # 三个返回值：翻译好的信号、计费条数、供应商原样返回的 dataset
        return [], 120, []

    monkeypatch.setattr("adapters.harvestapi_posts.from_apify", fake_from_apify)

    adapter = HarvestApiPostsAdapter()
    adapter.fetch(_spec(), max_items=40)

    assert seen["queries"] == ["wearable AND audio"]
    assert seen["max_posts_per_query"] == 40
    assert adapter.billed_items() == 120


def test_a_query_from_another_source_is_refused():
    """source_type 对不上当场炸。

    一条公司搜索的 query 交到这里会被原样塞进 LinkedIn 的帖子搜索框 ——
    那不报错，只拿回一批不相干的帖子，而报表上这条 query 跑成功了。
    """
    adapter = HarvestApiPostsAdapter()
    with pytest.raises(ValueError, match="linkedin_post"):
        adapter.fetch(_spec(source_type=SourceType.LINKEDIN_COMPANY), max_items=40)


def test_billed_items_before_any_fetch_raises():
    """没采过就问计费数要炸，不能给 0。

    0 会被原样写进 billed_item_count，然后这条 query 在「每块钱几个合格
    线索」上显得免费，而那个分母决定预算怎么分。
    """
    with pytest.raises(RuntimeError, match="还没跑过 fetch"):
        HarvestApiPostsAdapter().billed_items()


def test_recorded_adapter_replays_only_the_matching_query(tmp_path):
    """回放按 query 文本分，同一份文件同时服务当初那几条 query。"""
    dataset = [
        _recorded_item("p-1", "wearable AND audio"),
        _recorded_item("p-2", "wearable AND audio"),
        _recorded_item("p-3", "Kickstarter AND audio"),
    ]
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")

    adapter = RecordedPostsAdapter(path)

    got = adapter.fetch(_spec("wearable AND audio"), max_items=40)
    assert [s.external_id for s in got] == ["p-1", "p-2"]
    assert adapter.billed_items() == 2

    # max_items 是天花板：只要一条就只回放一条。
    assert len(adapter.fetch(_spec("wearable AND audio"), max_items=1)) == 1

    # 文件里没有这条 query。空列表跟真采集「今天没搜到东西」是同一种结果，
    # 不该炸 —— 炸的是采集失败，那一条由真 adapter 抛。
    assert adapter.fetch(_spec("nothing recorded"), max_items=40) == []


def _recorded_item(external_id: str, query_text: str) -> dict:
    """拼一条够 to_raw_signal 用的最小 dataset item。"""
    return {
        "id": external_id,
        "type": "post",
        "linkedinUrl": f"https://www.linkedin.com/posts/{external_id}",
        "content": "we are looking for a manufacturing partner",
        "postedAt": {"date": "2026-08-26 10:00:00"},
        "query": {"search": query_text},
        "author": {
            "type": "profile",
            "id": "ACoAAxxxx",
            "name": "Someone",
            "info": "Founder at Solara Audio",
            "linkedinUrl": "https://www.linkedin.com/in/someone",
        },
    }


def test_the_raw_dataset_is_written_verbatim_before_any_filtering(monkeypatch, tmp_path):
    """真跑要把供应商原样返回的 dataset 存下来，包括不是 post 的那些。

    存的必须是闸门之前的原料。库里存的是闸门之后的结果 —— 被 prefilter
    砍掉的那批根本不落库，而那批恰恰是以后验「这条规则有没有误砍真买方」
    要用的样本。所以这份文件不能从 RawSignal 反推（那样只剩 post），
    要拿 from_apify 还回来的原始 items。

    输入   两条 query 各采一批，dataset 里混着一条 comment
           adapter 把两次的 items 攒在一起，每次 fetch 之后整份重写
    输出   文件里是两批的全部 item，一条没少；评论那条也在
    """
    batches = [
        [_recorded_item("p-1", "wearable AND audio"), {"type": "comment", "id": "c-1"}],
        [_recorded_item("p-2", "Kickstarter AND audio")],
    ]

    def fake_from_apify(queries, **kwargs):
        items = batches.pop(0)
        posts = [i for i in items if i.get("type") == "post"]
        return [to_raw_signal(i) for i in posts], len(items), items

    monkeypatch.setattr("adapters.harvestapi_posts.from_apify", fake_from_apify)

    dump = tmp_path / "harvest_run1_2026-09-02.json.gz"
    adapter = HarvestApiPostsAdapter(dump_path=dump)

    adapter.fetch(_spec("wearable AND audio"), max_items=40)
    # 第一条 query 采完就该已经落盘了：中途崩掉的话钱已经花了，
    # 已经采到的那些不能跟着丢。
    assert [i["id"] for i in read_dataset(dump)] == ["p-1", "c-1"]

    adapter.fetch(_spec("Kickstarter AND audio"), max_items=40)
    assert [i["id"] for i in read_dataset(dump)] == ["p-1", "c-1", "p-2"]

    # 存的是 gzip，不是纯文本 —— 这些文件要长期留在 git 里
    assert dump.read_bytes()[:2] == b"\x1f\x8b"


def test_no_dump_path_means_no_file(monkeypatch, tmp_path):
    """没给落盘路径就不写文件（回放和测试走的是这一支）。"""

    def fake_from_apify(queries, **kwargs):
        return [], 0, [{"type": "post", "id": "x"}]

    monkeypatch.setattr("adapters.harvestapi_posts.from_apify", fake_from_apify)

    HarvestApiPostsAdapter().fetch(_spec(), max_items=40)
    assert list(tmp_path.iterdir()) == []


def test_read_dataset_accepts_both_shapes(tmp_path):
    """压缩的和没压缩的都读得出来。

    没压缩那一支不是为了将来，是为了现有的回归集：早期手工采的那几份
    是纯 JSON，而它们是唯一有标注的数据。
    """
    items = [_recorded_item("p-1", "q")]

    plain = tmp_path / "a.json"
    plain.write_text(json.dumps(items), encoding="utf-8")
    assert read_dataset(plain) == items

    packed = tmp_path / "b.json.gz"
    write_dataset(packed, items)
    assert read_dataset(packed) == items

    # 写完不许留下临时文件
    assert sorted(f.name for f in tmp_path.iterdir()) == ["a.json", "b.json.gz"]


# ---------- build_input 怎么拼请求 ----------


def test_empty_filters_changes_nothing_about_the_request():
    """加了 filters 这个参数之后，不带旋钮的那条路必须跟以前一模一样。

    这条路今天是全部的真实流量（库里三条 query 的 filters 都是空的），
    多出或少掉一个键都会改变采回来的东西，而我们不会看见。
    """
    assert build_input(["x"], max_posts_per_query=40) == build_input(
        ["x"], max_posts_per_query=40, filters={}
    )


def test_a_filter_cannot_override_a_fixed_parameter():
    """旋钮摊平的位置决定了谁盖谁，而盖反了没有任何症状。

    写在最后的话，filters 里一个叫 maxPosts 的键就能把这一跑的上限改成 9999，
    采集费跟着翻几百倍，而库里记的还是 40。validate_filters 的白名单不让这种
    键进库，但那是另一个模块的事 —— 这条测试焊的是这一层自己站得住。
    """
    request = build_input(["x"], max_posts_per_query=40, filters={"maxPosts": 9999})

    assert request["maxPosts"] == 40


def test_a_cutoff_date_is_sent_instead_of_the_level():
    """用户在对话里说了多久：传 postedLimitDate，不再传 postedLimit。

    两个一起传的话 actor 按哪个筛没核过，只传一个就没有这个问题。
    不给日期（定时那一跑、没说时间）的那条路要跟以前一模一样：只有 postedLimit
    """
    dated = build_input(["x"], max_posts_per_query=40, posted_since="2026-07-28")
    level = build_input(["x"], max_posts_per_query=40, posted_limit="month")

    assert dated["postedLimitDate"] == "2026-07-28" and "postedLimit" not in dated
    assert level["postedLimit"] == "month" and "postedLimitDate" not in level


def test_a_known_filter_reaches_the_request():
    """闸门不能把唯一要用的旋钮也挡掉。"""
    request = build_input(
        ["x"], max_posts_per_query=40, filters={"authorKeywords": "Founder OR CTO"}
    )

    assert request["authorKeywords"] == "Founder OR CTO"
