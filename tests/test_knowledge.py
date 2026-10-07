"""拟词之前那份 knowledge 的形状层：读配置、去重、挑例子、算计数。

焊住的五类，全是「不报错，但会让 planner 拿着一份假账去报价」：

    命中没按 lead 去重      lead_discovery_hits 是 append-only 的，同一条 query 同一家
                            公司每跑一次就有一行。不去重的话「这条词带回过多少家」
                            会按跑次翻倍，而那个数是报价的分母
    例子挑得不确定          同样的库状态两次给出不同的例子 -> planner 拟出不同的方案，
                            而排查「今天为什么拟成这样」就没有立足点
    例子两家都是好的        只按分数挑，「23 家里 13 家是 excluded」这件事一眼看不出来，
                            而那恰恰是这条词最要紧的毛病
    从没跑过的 query 缺数    totals 里查不到就让四个计数变成 None 的话，读的人要判断
                            哪个 None 是什么意思
    进 state 的不是普通类型  枚举和时刻直接塞进 checkpoint，几周后按新类解释旧数据

不连库：这一层本来就不连，行是测试自己造的。
"""

from __future__ import annotations

import datetime as dt
import json
import random

import pytest

from models import KnownQuery, LeadHit, LeadStatus, QueryPool, QueryTotals
from query_intelligence.knowledge import build_knowledge, load_avoid, pick_relevant

RAN_AT = dt.datetime(2026, 9, 2, 14, 3, tzinfo=dt.UTC)


def _query(
    query_id: int,
    *,
    text: str | None = None,
    topics: tuple[str, ...] = (),
    is_active: bool = True,
    last_run_at: dt.datetime | None = RAN_AT,
):
    """库里的一条 query。只有测试真的要区分的字段才传，其余给一个固定值。

    topics 默认空元组，也就是默认走「猜正文」那条路 —— 库里现在三条种子词都是
    这样，而带 topics 的那条路由它自己那几条测试盖。
    """
    return KnownQuery(
        query_id=query_id,
        text=text if text is not None else f"query-{query_id}",
        pool=QueryPool.CANDIDATE,
        cadence_days=1,
        is_active=is_active,
        last_run_at=last_run_at,
        topics=topics,
    )


def _hit(query_id: int, lead_id: int, status: LeadStatus, score: int | None, *, cooling=False):
    """一条命中。display_name 按 lead_id 生成，好在断言里认出是哪一家。"""
    return LeadHit(
        query_id=query_id,
        lead_id=lead_id,
        display_name=f"lead-{lead_id}",
        status=status,
        score=score,
        in_cooldown=cooling,
    )


def _build(queries, totals, hits):
    """把 queries 当成「已经挑出来的相关 query」交给 build_knowledge。

    挑的那一步（词面命中、排序、上限）由 pick_relevant 自己那组测试焊，这里测的是
    拼装，所以每条一律配一个固定的命中词。
    """
    relevant = [(query, (("mic", "mic", "text"),)) for query in queries]
    return build_knowledge(avoid=(), topics=["mic"], relevant=relevant, totals=totals, hits=hits)


# ───────────────────────────────────────────── 手写的那份负面知识


def test_load_avoid_reads_the_real_config():
    """读仓库里那份真配置，四个字段一个都不许空。

    用真文件不用假数据：这份东西整份会进 planner 的 prompt，而它是手写的 ——
    少一个 source 的症状是模型看到一条没有出处的经验，然后照样认真照做。
    """
    notes = load_avoid()

    assert notes, "配置里一条 avoid 都没有"
    for note in notes:
        assert note.key and note.key == note.key.lower(), note
        assert note.scope == "query", note
        assert len(note.statement) > 20, note
        assert note.source, note
        # 折过行的 yaml 块会带换行，读出来必须是一整行，否则塞进 prompt 会断开
        assert "\n" not in note.statement and "\n" not in note.source, note


def test_avoid_keys_are_unique():
    """key 是这条经验的名字，重了说明两条讲的是一回事，该合并不该并存。

    memories 表那边 key 有全局唯一约束，手写这份没有库帮忙，所以在这里焊。
    """
    keys = [note.key for note in load_avoid()]

    assert len(keys) == len(set(keys)), keys


def test_avoid_goes_into_knowledge_whole():
    """avoid 跟这次问什么无关，整份带，不筛也不截断。"""
    notes = load_avoid()

    knowledge = build_knowledge(avoid=notes, topics=[], relevant=(), totals={}, hits=())

    assert len(knowledge["avoid"]) == len(notes)
    assert knowledge["avoid"][0]["key"] == notes[0].key


# ───────────────────────────────────────────── 去重和计数


def test_hits_are_deduped_per_lead():
    """同一条 query 同一家公司在两跑里各有一行，只能算一家。

    不去重的话「这条词带回过多少家」按跑次翻倍，而那个数是报价的分母。
    """
    hits = [
        _hit(30, 1301, LeadStatus.EXCLUDED, 34),
        _hit(30, 1301, LeadStatus.EXCLUDED, 34),
        _hit(30, 1288, LeadStatus.PUSHED, 67),
    ]

    knowledge = _build([_query(30)], {}, hits)

    assert knowledge["known_queries"][0]["leads"]["total"] == 2


def test_counts_split_excluded_and_cooling():
    """三个计数各数各的，一家公司可以同时算进 total 和 in_cooldown。"""
    hits = [
        _hit(30, 1, LeadStatus.EXCLUDED, 8),
        _hit(30, 2, LeadStatus.PUSHED, 71, cooling=True),
        _hit(30, 3, LeadStatus.NEW, None),
    ]

    leads = _build([_query(30)], {}, hits)["known_queries"][0]["leads"]

    assert (leads["total"], leads["excluded"], leads["in_cooldown"]) == (3, 1, 1)


def test_known_lead_total_dedups_across_queries():
    """同一家公司被两条 query 命中，全局只算一次；每条 query 底下仍各算一次。

    两个数回答两个不同的问题：每条底下问「这条词带回多少」，全局问「这个题目
    我们一共已经有多少家」。重叠命中是常态，所以这两个数加起来对不上是对的。
    """
    hits = [
        _hit(30, 1301, LeadStatus.EXCLUDED, 34),
        _hit(31, 1301, LeadStatus.EXCLUDED, 34),
        _hit(31, 1288, LeadStatus.PUSHED, 67),
    ]

    knowledge = _build([_query(30), _query(31)], {}, hits)

    per_query = [item["leads"]["total"] for item in knowledge["known_queries"]]
    assert per_query == [1, 2]
    assert knowledge["known_lead_total"] == 2


def test_a_query_that_never_ran_gets_zeros_not_none():
    """从没跑完过的 query 在 totals 里查不到，四个计数给 0。

    给 None 的话读的人要逐个判断哪个 None 是什么意思。「跑过但一家都没撞到」和
    「从没跑过」由 executions 这一个数区分就够了。
    """
    item = _build([_query(44, is_active=False, last_run_at=None)], {}, ())["known_queries"][0]

    assert item["executions"] == 0
    assert item["unique_companies"] == 0
    assert item["novel_leads"] == 0
    assert item["qualified_leads"] == 0
    assert item["last_run_at"] == ""
    assert item["is_active"] is False


def test_totals_are_matched_by_query_id():
    """按 query_id 对，不按下标 —— 两条 query 的账不能安到对方头上。"""
    totals = {
        31: QueryTotals(
            executions=12, unique_companies=418, researchable_leads=140, qualified_leads=61, novel_leads=203
        )
    }

    knowledge = _build([_query(30), _query(31)], totals, ())

    assert knowledge["known_queries"][0]["executions"] == 0
    assert knowledge["known_queries"][1]["executions"] == 12
    assert knowledge["known_queries"][1]["novel_leads"] == 203


# ───────────────────────────────────────────── 挑哪两家当例子


def test_examples_take_one_from_each_bucket():
    """第一家是 excluded 里分数最高的，第二家是其余里分数最高的。

    两家各回答一个问题：这条词捞回来的垃圾长什么样、它捞回来最好的长什么样。
    顺序是「先垃圾后最好」，不是按分数排 —— 按分数排的话 67 那家会跑到前面，
    而第一眼该看见的是这条词的毛病。
    """
    hits = [
        _hit(30, 1, LeadStatus.EXCLUDED, 12),
        _hit(30, 2, LeadStatus.EXCLUDED, 34),
        _hit(30, 3, LeadStatus.PUSHED, 67),
        _hit(30, 4, LeadStatus.NEW, 22),
    ]

    examples = _build([_query(30)], {}, hits)["known_queries"][0]["leads"]["examples"]

    assert [item["lead_id"] for item in examples] == [2, 3]


def test_examples_fall_back_to_the_other_bucket():
    """某一档一家都没有，就从另一档补第二家。"""
    hits = [
        _hit(30, 1, LeadStatus.EXCLUDED, 34),
        _hit(30, 2, LeadStatus.EXCLUDED, 12),
        _hit(30, 3, LeadStatus.EXCLUDED, 8),
    ]

    examples = _build([_query(30)], {}, hits)["known_queries"][0]["leads"]["examples"]

    assert [item["lead_id"] for item in examples] == [1, 2]


def test_examples_are_capped_at_two():
    """定死两家。多一家不增加新信息，同一条 query 采回来的公司高度同质。"""
    hits = [_hit(30, i, LeadStatus.PUSHED, 90 - i) for i in range(1, 9)]

    examples = _build([_query(30)], {}, hits)["known_queries"][0]["leads"]["examples"]

    assert len(examples) == 2


def test_examples_do_not_depend_on_row_order():
    """库返回的先后不许影响挑出来是哪两家。

    这一条是「同样的库状态两次要给出同一份 knowledge」的落点。不定的话 planner
    会对着不同的例子拟出不同的方案，而那种不一致没有任何报错。
    """
    hits = [
        _hit(30, 5, LeadStatus.EXCLUDED, 34),
        _hit(30, 9, LeadStatus.EXCLUDED, 34),
        _hit(30, 2, LeadStatus.PUSHED, 67),
        _hit(30, 7, LeadStatus.PUSHED, 67),
    ]
    first = _build([_query(30)], {}, hits)["known_queries"][0]["leads"]["examples"]

    shuffled = list(hits)
    random.Random(0).shuffle(shuffled)
    again = _build([_query(30)], {}, shuffled)["known_queries"][0]["leads"]["examples"]

    # 两个 34 分、两个 67 分，同分按 lead_id 小的在前
    assert [item["lead_id"] for item in first] == [5, 2]
    assert again == first


def test_a_lead_that_was_never_scored_ranks_last():
    """分数是空的那家排在最后，而且摊出去是 0 不是 None。

    score 为空的含义是「发现过、落过库，但还没轮到分析」。举它当例子说明不了
    任何事，所以排在判过分的后面。
    """
    hits = [
        _hit(30, 1, LeadStatus.NEW, None),
        _hit(30, 2, LeadStatus.NEW, 3),
    ]

    examples = _build([_query(30)], {}, hits)["known_queries"][0]["leads"]["examples"]

    assert [item["lead_id"] for item in examples] == [2, 1]
    assert examples[1]["score"] == 0


# ───────────────────────────────────────────── 进 state 的形状


def test_knowledge_is_plain_json():
    """整份东西要被序列化进 checkpoint 躺很久，不许带枚举、时刻、元组。

    存对象的话，哪天枚举改名或者 schema 改一个字段，旧那条 thread 的快照读出来
    就是「按新类解释的旧数据」。
    """
    knowledge = _build([_query(30)], {}, [_hit(30, 1, LeadStatus.EXCLUDED, 34)])

    dumped = json.dumps(knowledge, ensure_ascii=False)

    assert '"pool": "candidate"' in dumped
    assert '"status": "excluded"' in dumped
    assert '"last_run_at": "2026-09-02"' in dumped


def test_knowledge_has_exactly_four_keys():
    """四个键各对着一个问题。加第五个之前先说得出它的消费者是谁。"""
    knowledge = _build((), {}, ())

    assert set(knowledge) == {"avoid", "topics_searched", "known_queries", "known_lead_total"}


def test_hits_for_an_unknown_query_are_not_invented():
    """命中里出现了不在 queries 列表里的 query_id，不许凭空多出一条 known_query。

    真会发生：那条 query 被删了、或者 all_queries 和 hits 之间隔了一次写入。
    多出来的话 planner 会看到一条没有正文的词。
    """
    knowledge = _build([_query(30)], {}, [_hit(99, 1, LeadStatus.NEW, 5)])

    assert [item["id"] for item in knowledge["known_queries"]] == [30]
    assert knowledge["known_queries"][0]["leads"]["total"] == 0
    # 但它仍然算进全局那个数：那问的是「库里已经有多少家」，跟哪条词带回来的无关
    assert knowledge["known_lead_total"] == 1


@pytest.mark.parametrize("missing", ["key", "scope", "statement", "source"])
def test_load_avoid_refuses_a_note_with_a_missing_field(tmp_path, missing):
    """缺一个字段当场 KeyError，不给默认值。"""
    note = {"key": "k", "scope": "query", "statement": "s", "source": "src"}
    note.pop(missing)
    path = tmp_path / "query_knowledge.yaml"
    path.write_text(
        "avoid:\n"
        + "".join(f"  {'- ' if i == 0 else '  '}{k}: {v}\n" for i, (k, v) in enumerate(note.items())),
        encoding="utf-8",
    )

    with pytest.raises(KeyError):
        load_avoid(path)


# ───────────────────────────────────────────── 按 topics 挑相关的 query

# 库里那三条种子词的原文。拿真的写，是因为「引号挡不挡路」「整句的那条能不能被
# 一个词命中」这两件事只有对着真串才说明问题。
SEED_29 = "looking for a hardware manufacturing partner"
SEED_30 = 'Kickstarter AND (microphone OR audio OR "voice recorder")'
SEED_31 = "wearable AND (microphone OR audio OR voice)"


def _seeds():
    return [_query(29, text=SEED_29), _query(30, text=SEED_30), _query(31, text=SEED_31)]


def _one(queries, topics):
    """挑出来应该只有一条时，把那一条的命中对拿出来。多于一条就当场炸。"""
    (picked,) = pick_relevant(queries, topics)
    return picked[1]


def test_only_the_queries_whose_text_contains_the_topic_come_back():
    """一个品类词挑出的是正文里真写了这个词的那几条，别的一条都不带。"""
    picked = pick_relevant(_seeds(), ["microphone"])

    assert [query.query_id for query, _ in picked] == [30, 31]
    assert all(matched == (("microphone", "microphone", "text"),) for _, matched in picked)


def test_a_topic_nobody_wrote_about_returns_nothing():
    """库里没有的题目返回空列表，不许退回「全给」。

    bone conduction 整条不中，中心词 conduction 也不中 —— 库里真的没有这个题目的经验。
    退回全给的话，planner 会对着一批跟这次无关的词拟方案，而且一旦库长起来，输入长度
    和内容都变得没法预测。空列表才是那句该说的话：这次是新题目，报价没有分母。
    """
    assert pick_relevant(_seeds(), ["bone conduction"]) == []


def test_no_topics_means_no_relevant_queries():
    """topics 是空的（give_up 那条路）就挑不出任何一条，不是「没筛所以全给」。"""
    assert pick_relevant(_seeds(), []) == []


def test_quotes_and_parens_do_not_block_a_match():
    """词被引号或括号包着照样算命中：边界只看前后有没有 ASCII 字母数字。"""
    picked = pick_relevant(_seeds(), ["voice recorder"])

    assert [query.query_id for query, _ in picked] == [30]


def test_a_topic_inside_a_longer_word_is_not_a_match():
    """audiobook 不算命中 audio。

    没有边界的话，随便一个品类词都能命中一堆不相干的 query，而这一层挑出来的东西
    会直接进 planner 的 prompt —— 症状是它照着一条根本不相干的词拟方案。
    """
    assert pick_relevant([_query(40, text="audiobook AND wearable")], ["audio"]) == []


def test_a_multi_word_topic_matches_as_one_piece_or_not_at_all():
    """原样命中要求那两个单词连着出现：中间插词、顺序颠倒都不算原样。

    两边相同的那一对才是原样命中。中间插了词的那条走的是中心词那条降级路
    （见下一条），所以这里断言的是命中对长什么样，不是有没有命中。
    """
    assert _one([_query(30, text=SEED_30)], ["voice recorder"]) == (
        ("voice recorder", "voice recorder", "text"),
    )
    assert _one([_query(42, text="AND (voice-recorder OR mic)")], ["voice recorder"]) == (
        ("voice recorder", "voice recorder", "text"),
    )
    assert _one([_query(40, text="AND (voice note recorder)")], ["voice recorder"]) == (
        ("voice recorder", "recorder", "head"),
    )


def test_the_gap_inside_a_topic_is_the_only_thing_that_may_differ():
    """品类词里那一格空格，对上空白或连字符都算 —— 只有这一处允许有出入。"""
    assert pick_relevant([_query(42, text="AND (voice-recorder OR mic)")], ["voice recorder"]) != []
    assert pick_relevant([_query(43, text="AND (voice  recorder)")], ["voice recorder"]) != []


def test_the_head_word_catches_what_the_whole_phrase_misses():
    """整条短语不中就退一步用中心词再试一次，而且要看得出来是降级来的。

    这是真实提问的形状：用户说 conference microphone，而库里写的是 microphone。
    只认整条短语的话这种提问永远 0 条 —— 不是库小，是两边粒度不一样。
    """
    picked = pick_relevant(_seeds(), ["conference microphone"])

    assert [query.query_id for query, _ in picked] == [30, 31]
    assert all(matched == (("conference microphone", "microphone", "head"),) for _, matched in picked)


def test_a_one_word_topic_does_not_get_a_second_try():
    """本来就是一个单词的品类词没有降级这回事，它自己就是中心词。

    给它第二次机会的话，同一条命中会被记两次，matched 里出现两个一模一样的对，
    而排序是按命中个数排的 —— 这条 query 会凭空插到别人前面。
    """
    assert _one([_query(30, text=SEED_30)], ["microphone"]) == (("microphone", "microphone", "text"),)


def test_a_head_word_that_is_too_short_is_not_used():
    """单个字母收尾的短语不降级：一个字母在布尔检索式里到处都是，拿它命中等于全都相关。"""
    assert pick_relevant([_query(44, text="AND (vitamin OR c OR mic)")], ["vitamin c"]) == []


def test_an_exact_match_ranks_above_a_head_word_match():
    """原样命中的排在降级命中的前面：后者只说明这条 query 沾了中心词那个泛类。"""
    queries = [
        _query(50, text="AND (conference microphone OR mic)"),
        _query(49, text="AND (microphone OR audio)"),
    ]

    picked = pick_relevant(queries, ["conference microphone"])

    assert [query.query_id for query, _ in picked] == [50, 49]
    assert picked[0][1] == (("conference microphone", "conference microphone", "text"),)
    assert picked[1][1] == (("conference microphone", "microphone", "head"),)


def test_more_matched_topics_ranks_first_then_id():
    """命中的品类词多的排前面，一样多的按 id —— 同样的库状态两次要给出同一份。"""
    queries = [
        _query(31, text=SEED_31),
        _query(30, text=SEED_30),
        _query(50, text="microphone AND audio AND kickstarter"),
    ]

    picked = pick_relevant(queries, ["microphone", "audio"])

    assert [query.query_id for query, _ in picked] == [30, 31, 50]
    assert all(
        matched == (("microphone", "microphone", "text"), ("audio", "audio", "text"))
        for _, matched in picked
    )


def test_the_number_of_known_queries_is_capped():
    """再多也只端 12 条上去。上限是 prompt 长度的上限，不是相关性的判据。"""
    many = [_query(i, text=f"microphone AND topic-{i}") for i in range(100, 130)]

    picked = pick_relevant(many, ["microphone"])

    assert len(picked) == 12
    assert [query.query_id for query, _ in picked] == list(range(100, 112))


def test_a_query_that_remembers_its_topics_matches_word_for_word():
    """interactive 建的 query 记着当初的题目，拿这次的品类词直接对，不用去猜正文。

    这是这条路存在的全部理由：44 那条的正文是布尔式，而题目是用户当初说的那句话，
    两者粒度不一样。对题目是精确的，猜正文是猜的。
    """
    query = _query(44, text="bone conduction AND (headphone OR earbuds)", topics=("bone conduction",))

    (picked_query, matched) = pick_relevant([query], ["bone conduction"])[0]

    assert picked_query.query_id == 44
    assert matched == (("bone conduction", "bone conduction", "topics"),)


def test_topics_are_compared_after_the_same_normalisation_as_query_text():
    """大小写和多余空白不算两个题目，用的是 queries.text_normalized 那同一个函数。

    两边都是 LLM 写出来的自由字符串，差一个大写字母就算两个题目的话，对不上这件事
    永远查不出来 —— 症状只是「库里明明有，却说没有」。
    """
    query = _query(44, text="whatever", topics=("Bone  Conduction",))

    assert pick_relevant([query], ["bone conduction"]) != []


def test_a_query_with_topics_still_falls_back_to_its_text():
    """题目对不上的时候照样去猜正文，两条路是先后不是二选一。

    44 的题目是 bone conduction，但它的正文里还有 earbuds —— 有人问 earbuds 的时候
    它确实相关，而那件事只有正文看得出来。二选一的话这种真命中会被白白丢掉。
    """
    query = _query(44, text="bone conduction AND (headphone OR earbuds)", topics=("bone conduction",))

    (_, matched) = pick_relevant([query], ["earbuds"])[0]

    assert matched == (("earbuds", "earbuds", "text"),)


def test_matching_the_topics_ranks_above_guessing_the_text():
    """对上题目的排在猜正文的前面：前者是这条词就为这个题目造的，后者只是字面沾边。"""
    remembered = _query(44, text="whatever AND (mic)", topics=("bone conduction",))
    guessed = _query(30, text="bone conduction AND (headphone OR earbuds)")

    picked = pick_relevant([guessed, remembered], ["bone conduction"])

    assert [(query.query_id, matched[0][2]) for query, matched in picked] == [
        (44, "topics"),
        (30, "text"),
    ]


def test_the_topics_that_were_searched_are_recorded():
    """查过哪几个词要写进 knowledge：不写的话「查过但库里没有」和「还没查」长得一样。"""
    knowledge = build_knowledge(avoid=(), topics=["bone conduction"], relevant=(), totals={}, hits=())

    assert knowledge["topics_searched"] == ["bone conduction"]
    assert knowledge["known_queries"] == []
    assert knowledge["known_lead_total"] == 0


def test_matched_topics_always_has_all_three_keys():
    """三个键永远都在，形状不随内容变：用户说的词、真正命中的词、怎么命中的。

    只有某一种才多一个键的话，读的人和模型都得先判断这次是哪一种。而「怎么命中的」
    不能靠前两个词推：topics 和 text 那两种前两个词都一样。
    """
    relevant = [(_query(30, text=SEED_30), (("conference microphone", "microphone", "head"),))]

    knowledge = build_knowledge(
        avoid=(), topics=["conference microphone"], relevant=relevant, totals={}, hits=()
    )

    assert knowledge["known_queries"][0]["matched_topics"] == [
        {"topic": "conference microphone", "matched": "microphone", "how": "head"}
    ]


def test_knowledge_says_how_each_topic_was_matched():
    """每个命中要写清它是对上题目来的、原样在正文里、还是只中了中心词。

    三种证据强度不一样，而不写的话读的人得自己去推，推错了没有任何症状。
    """
    remembered = _query(44, text="whatever", topics=("bone conduction",))
    relevant = [(remembered, (("bone conduction", "bone conduction", "topics"),))]

    knowledge = build_knowledge(
        avoid=(), topics=["bone conduction"], relevant=relevant, totals={}, hits=()
    )

    assert knowledge["known_queries"][0]["matched_topics"][0]["how"] == "topics"
    assert knowledge["known_queries"][0]["topics"] == ["bone conduction"]
