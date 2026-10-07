"""plan_queries 那一步里确定性的那一半：查一条 query 能不能用、报价、写猜测。

焊住的几类，全是「不报错，但要到花钱那一刻才发作」：

    超长和非法旋钮   Apify 判输入非法、整跑失败。失败在花钱之前是好事，但那仍然是
                     这一次探索没了，而且报错要从 Apify 那侧一路回溯到拟词
    中文混进 query   采回零条、照样计费、而且不报错 —— 在报表上跟「今天确实没线索」
                     长得一模一样
    带 NOT           实测过它排不掉跟题目同义的那批（工厂广告），留着它等于给用户
                     一个「我已经排掉了」的错觉
    说好避开的词又写进正文  exclusions 今天唯一的动作就是这个，不查它就退回成一个
                     没人读的装饰品
    丢掉了却不说      用户会以为方案就是他要的那个样子
    报价算错          报低了的那次，用户是在账单上发现的

模型那一半（schema / 拼消息 / 解析 / 那一次调用）也在这个文件里，焊的是另外几类：

    schema 和 required 漂了  strict 模式要求 required 列全每一个 property。漏一个，
                     模型可以整个不返回那个键，解析当场炸
    prompt 里的条数写死      配置写 3、prompt 写 2 没有任何症状：模型按它看到的数拟，
                     代码按配置那个数截断，被砍掉的几条白花了输出 token
    喂了没人用的字段  模型会认真对待它然后做出我们没要求的事。最具体的是 unsupported：
                     它会把「只要加州的」写成 California 塞进检索式
    对话没喂进去      温度是 0，输入一个字没变的话重拟出来的是逐字相同的一版 ——
                     用户改一次看见同一版方案，全程不报错
    空旋钮没剔干净    0009 的唯一键含 filters，空字典和「有键值是空串」是两条不同的
                     query，同一句词会凭空多出一行
    坏回答被当成好回答 截断、拒答、空内容、非法 JSON 四种，任意一种被吞掉，
                     下一步就是拿着半版方案去问用户「跑不跑」

不连库、不调模型：确定性那一半本来就不需要，模型那一半用一个照剧本回答的假 client。
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from triggers.interactive_discovery.query.planner import (
    PLAN_SCHEMA,
    QUERY_MAX_BOOLEANS,
    QUERY_MAX_CHARS,
    PlannerCallFailed,
    PlannerConfig,
    build_assumptions,
    build_planner_messages,
    check_query,
    draft_queries,
    estimate_cost_usd,
    load_planner_config,
    parse_plan,
    screen_queries,
)

GOOD = 'Kickstarter AND (microphone OR "voice recorder")'


# ---------- check_query ----------


def test_a_well_formed_query_has_no_problems():
    """闸门不能把正常的词也拦下来。这条用了三个布尔符，正好在上限之内。"""
    assert check_query(GOOD, filters={"authorKeywords": "Founder OR CTO"}) == ()


def test_blank_query_is_refused_and_stops_there():
    """空白 query 发出去不报错，它会采回一批跟这次对话完全无关的东西。

    这一条查完就返回，不再往下查：一条空串的长度、布尔符、中文都没有意义，
    报出来只会让用户在四条噪音里找那一条真的。
    """
    assert check_query("   ", filters={}) == ("这条是空的",)


def test_a_too_long_query_is_refused():
    long_query = "audio OR " * 60
    problems = check_query(long_query, filters={})

    assert any(str(QUERY_MAX_CHARS) in p for p in problems)


def test_too_many_boolean_operators_are_counted():
    """布尔符是额度。数的是出现次数不是种类。"""
    problems = check_query("a AND (b OR c OR d OR e OR f OR g)", filters={})

    assert any("6 个 AND/OR/NOT" in p for p in problems)
    assert any(str(QUERY_MAX_BOOLEANS) in p for p in problems)


def test_lowercase_and_is_not_a_boolean_operator():
    """LinkedIn 只认大写布尔符，research and development 里那个 and 不算。

    算进去的话，一条正常的英文句子会被误判成超了额度然后被丢掉。
    """
    assert check_query("research and development and design and test", filters={}) == ()


def test_an_operator_inside_a_word_is_not_counted():
    """NOTebook 里的 NOT、major 里的 OR、ANDroid 里的 AND 都不算。

    用 \\b 就会把这几个算进去（\\w 含中日韩字符，边界在中英混排时会失效），
    所以边界是显式 ASCII。
    """
    assert check_query("NOTebook major ANDroid", filters={}) == ()


def test_not_is_refused_on_its_own():
    """带 NOT 的单独报，跟「超了几个额度」是两件事。

    一条只用了一个 NOT 的 query 根本不会超额度，但它照样不该用。
    """
    problems = check_query("audio NOT hiring", filters={})

    assert len(problems) == 1
    assert "NOT" in problems[0]


def test_chinese_in_the_query_is_refused():
    """中文词采回零条、照样计费、而且不报错。报错里要指出是哪几个字。"""
    problems = check_query("骨传导 AND headphone", filters={})

    assert any("骨传导" in p for p in problems)


def test_an_accented_latin_name_is_not_chinese():
    """挡的是中日韩文字，不是所有非 ASCII。

    Kestrel 是一家真实公司（带隐藏麦克风口袋的卫衣，标注里出现过），
    一刀切成 ASCII 会把这种合法的英文搜索词也拦掉。
    """
    assert check_query("Kestrel AND microphone", filters={}) == ()


def test_an_excluded_word_in_the_query_is_refused():
    """说好要避开的词又写进正文，是 exclusions 今天唯一要查的事。"""
    problems = check_query("Kickstarter AND pre-launch", filters={}, exclusions=("pre-launch",))

    assert problems == ("这条里出现了说好要避开的词：pre-launch",)


def test_an_excluded_word_only_matches_whole_words():
    """避开 audio 不该连 audiobook 一起拦掉。"""
    assert check_query("audiobook launch", filters={}, exclusions=("audio",)) == ()


def test_bad_filters_are_reported_not_raised():
    """旋钮不合格是一条要讲给用户听的问题，不是一个要炸掉整条对话的异常。

    分隔符写错的症状是采回零条而且不报错，所以必须查；但一条 query 的旋钮
    写坏了不该让另外两条也跟着没了。
    """
    problems = check_query(GOOD, filters={"authorKeywords": "Founder, CTO"})

    assert len(problems) == 1
    assert problems[0].startswith("旋钮不能用：")


def test_all_problems_come_back_at_once():
    """一次把所有毛病都返回，不是碰到第一个就停。

    拟词要么重来要么丢掉，两种都是一次性的机会 —— 只说一个毛病等于逼着
    下一轮再发现一个。
    """
    problems = check_query(
        "骨传导 AND (a OR b OR c OR d OR e OR f)",
        filters={"authorKeywords": "Founder, CTO"},
    )

    assert len(problems) == 3


# ---------- 报价 ----------


def test_cost_is_queries_times_posts_times_unit_price():
    """两条 query 各采 40 条 = 80 个计费 item，单价 0.002 -> 0.16 美元。

    这个数跟 run 36/37 实际付的 0.16 对得上。
    """
    assert estimate_cost_usd(2, max_posts_per_query=40, unit_price_usd=0.002) == 0.16


def test_no_queries_costs_nothing():
    """一条都没拟出来的时候报价是 0，不是某个默认值。

    报一个非零的数等于告诉用户「点跑会花这些钱」，而点下去什么都不会发生。
    """
    assert estimate_cost_usd(0, max_posts_per_query=40, unit_price_usd=0.002) == 0.0


def test_cost_is_rounded_to_cents():
    """多出来的小数位在报价这件事上没有意义。"""
    assert estimate_cost_usd(3, max_posts_per_query=37, unit_price_usd=0.002) == 0.22


# ---------- assumptions ----------


FULL_INTENT = {
    "goal": "找刚做完众筹的骨传导耳机团队",
    "topics": ["bone conduction"],
    "events": ["刚做完众筹"],
    "exclusions": ["pre-launch"],
    "time_said": "这周",
    "max_spend_usd": 0.2,
    "unsupported": ["只要加州的"],
}
KEPT = [
    {
        "text": "Kickstarter AND bone conduction",
        "filters": {"authorKeywords": "Founder OR CTO"},
    }
]


def test_assumptions_list_the_four_things_the_user_must_be_able_to_check():
    """goal / topics / exclusions / unsupported 四样一个都不能漏。

    topics 是最要紧的那个：模型有可能把用户不想要的词填进去（他说「别带众筹的」，
    模型填了 kickstarter），而形状检查查不出这种错 —— 那是一个合法的名词短语。
    摆在卡片上，他点「跑」之前一眼就能抓住。
    """
    text = build_assumptions(FULL_INTENT, kept=KEPT)

    assert "找刚做完众筹的骨传导耳机团队" in text
    assert "bone conduction" in text
    assert "pre-launch" in text
    assert "只要加州的" in text


def test_assumptions_show_the_author_filter_because_it_changes_the_results():
    """「只看 Founder 和 CTO 发的帖」是一个会显著改变结果的决定，不该是隐形的。

    实测同一条 query 不加旋钮回来 20 条，加了 CTO 只剩 4 条。
    """
    assert "Founder OR CTO" in build_assumptions(FULL_INTENT, kept=KEPT)


def test_the_author_filter_is_reported_once_even_when_every_query_uses_it():
    """几条 query 共用一套旋钮是常态，重复报三遍只会挤掉别的行。"""
    kept = [
        {"text": "a", "filters": {"authorKeywords": "Founder OR CTO"}},
        {"text": "b", "filters": {"authorKeywords": "Founder OR CTO"}},
    ]

    assert build_assumptions(FULL_INTENT, kept=kept).count("Founder OR CTO") == 1


def test_dropped_queries_are_named_with_their_reason():
    """丢了而不说等于静默丢弃，而用户会以为方案就是他要的那个样子。"""
    dropped = [("audio NOT hiring", ["这条带了 NOT：排不掉跟题目同义的那批"])]

    text = build_assumptions(FULL_INTENT, kept=KEPT, dropped=dropped)

    assert "丢掉了 1 条" in text
    assert "audio NOT hiring" in text
    assert "排不掉跟题目同义的那批" in text


def test_nothing_survived_says_so_first_and_asks_for_a_new_direction():
    """一条都没留下时，这句话要排在最前面 —— 它是这版方案唯一的内容。

    只说「丢掉了 3 条」而不说「接下来怎么办」的话，用户看着一张空方案卡片，
    不知道是该改说法还是该再等等。
    """
    text = build_assumptions(FULL_INTENT, kept=[], dropped=[("x", ["坏"])])

    assert text.splitlines()[0].startswith("一条能用的 query 都没拟出来")


def test_empty_fields_do_not_produce_empty_lines():
    """空的字段不出现，不写成「无」。

    一行「拟词时避开：无」占掉一行视线，而它没有回答任何问题。
    """
    text = build_assumptions({"goal": "找潜在客户公司"}, kept=[{"text": "x", "filters": {}}])

    assert text == "我理解的需求：找潜在客户公司"


def test_assumptions_do_not_crash_on_a_half_filled_intent():
    """缺键不该炸在拼字符串这一步。

    这段话的用途是给人看，缺一行远好过整条对话挂掉。
    """
    assert build_assumptions({}, kept=[]) != ""


# ---------- 配置 ----------


def test_config_reads_the_repo_file():
    config = load_planner_config()

    assert config.max_queries >= 1
    assert config.unit_price_usd > 0


def test_a_missing_key_is_not_papered_over():
    """缺键当场 KeyError，跟 load_intent_config 同一条口径。

    退化成默认值的后果是「实际用的数跟配置说的不一样」，而它不报错。
    """
    import tempfile

    bad = pathlib.Path(tempfile.mkdtemp()) / "interactive.yaml"
    bad.write_text("planner:\n  max_queries: 3\n", encoding="utf-8")

    with pytest.raises(KeyError):
        load_planner_config(bad)


def test_a_zero_max_queries_is_refused():
    """0 不是「不限」也不是「一条都不许拟」，它是写错了。"""
    import tempfile

    bad = pathlib.Path(tempfile.mkdtemp()) / "interactive.yaml"
    bad.write_text("planner:\n  max_queries: 0\n  unit_price_usd: 0.002\n", encoding="utf-8")

    with pytest.raises(ValueError, match="max_queries"):
        load_planner_config(bad)


def test_a_negative_unit_price_is_refused():
    """负的单价会让报价变成负数，而报价是花钱之前那道闸门要拿来比大小的数。"""
    import tempfile

    bad = pathlib.Path(tempfile.mkdtemp()) / "interactive.yaml"
    bad.write_text("planner:\n  max_queries: 3\n  unit_price_usd: -1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unit_price_usd"):
        load_planner_config(bad)


# ---------- screen_queries ----------


def test_bad_queries_are_dropped_and_good_ones_kept():
    """一条坏的不该带走另外几条好的。单条丢弃是这条路上的处置方式。"""
    raw = [
        {"text": "Kickstarter AND bone conduction", "filters": {}},
        {"text": "audio NOT hiring", "filters": {}},
        {"text": "wearable AND microphone", "filters": {}},
    ]

    kept, dropped = screen_queries(raw, max_queries=3)

    assert [item["text"] for item in kept] == [
        "Kickstarter AND bone conduction",
        "wearable AND microphone",
    ]
    assert [text for text, _ in dropped] == ["audio NOT hiring"]


def test_quality_is_checked_before_the_count_is_capped():
    """顺序反过来的话，坏的那条会占掉一个名额然后被丢掉，最后少跑一条。

    用户付的是同样的等待，拿到的线索少了三分之一，而报表上看不出来。
    这里第二条是坏的：先查质量的话，第三第四条能补上两个名额。
    """
    raw = [
        {"text": "a AND b", "filters": {}},
        {"text": "audio NOT hiring", "filters": {}},
        {"text": "c AND d", "filters": {}},
        {"text": "e AND f", "filters": {}},
    ]

    kept, _dropped = screen_queries(raw, max_queries=3)

    assert [item["text"] for item in kept] == ["a AND b", "c AND d", "e AND f"]


def test_extra_queries_beyond_the_cap_are_dropped_with_a_reason():
    """超出上限的那几条也要说清楚，不能静默截断。

    用户说「我要五个方向」而我们只跑三条，他得知道另外两条没跑。
    """
    raw = [{"text": f"q{i} AND x", "filters": {}} for i in range(5)]

    kept, dropped = screen_queries(raw, max_queries=2)

    assert len(kept) == 2
    assert len(dropped) == 3
    assert all("超过一次最多 2 条" in problems[0] for _text, problems in dropped)


def test_screening_passes_the_exclusions_down():
    """说好避开的词写进正文的那条要被挑出来，而这件事只有 screen 这一层知道。"""
    raw = [{"text": "Kickstarter AND pre-launch", "filters": {}}]

    kept, dropped = screen_queries(raw, exclusions=("pre-launch",), max_queries=3)

    assert kept == []
    assert "pre-launch" in dropped[0][1][0]


def test_kept_queries_come_back_in_the_plan_shape():
    """留下的那堆直接就是 plan["queries"] 的形状，调用处不用再拼一次。

    再拼一次的话就有两个地方决定这个形状，而形状变了不会有任何测试红
    （run_harvest 在测试里是桩）。
    """
    raw = [{"text": "a AND b", "filters": {"authorKeywords": "Founder OR CTO"}}]

    kept, _dropped = screen_queries(raw, max_queries=3)

    assert kept == [{"text": "a AND b", "filters": {"authorKeywords": "Founder OR CTO"}}]


def test_screening_copies_the_filters_instead_of_sharing_them():
    """每条 query 拿到自己的那份旋钮。共用一个字典的话，谁改了一处，
    别的 query 发出去的旋钮跟着变，而库里记的还是原来那个。
    """
    shared = {"authorKeywords": "Founder OR CTO"}
    raw = [{"text": "a AND b", "filters": shared}]

    kept, _dropped = screen_queries(raw, max_queries=3)
    kept[0]["filters"]["authorKeywords"] = "CEO"

    assert shared == {"authorKeywords": "Founder OR CTO"}


# ---------- 模型那一半共用的几样 ----------

# 一份 intent，用户说的是「帮我找找骨传导耳机的初创，最好是刚做完众筹的，
# 别带那种预热帖，只要加州的，别超过两毛钱」。七个字段全写出来，不图省事 ——
# build_planner_messages 只挑四个喂进去，那正是要焊的事。
INTENT = {
    "goal": "找刚做完众筹的骨传导耳机团队",
    "topics": ["bone conduction"],
    "events": ["刚做完众筹"],
    "exclusions": ["pre-launch"],
    "time_said": "这周",
    "max_spend_usd": 0.2,
    "unsupported": ["只要加州的"],
}

KNOWLEDGE = {"avoid": [], "topics_searched": ["bone conduction"], "known_queries": [], "known_lead_total": 0}

LINES = ("用户: 帮我找找骨传导耳机的初创", "AI: 方案 v1，…", "用户: 第二条不要了")

CONFIG = PlannerConfig(
    model="gpt-4o-mini",
    max_tokens=800,
    temperature=0.0,
    request_timeout_seconds=30.0,
    max_queries=3,
    unit_price_usd=0.002,
)

# 「这个参数没给」和「这个参数给的就是 None」是两件事：后者正是要测的那种失败
# （模型没返回内容）。用 None 当默认值的话，这两种情况在函数里长得一模一样。
_KEEP = object()


class _FakeClient:
    """一个只会照着剧本回答的 client，形状跟 tests/test_intent.py 那个一样。

    输入：构造时给这一次要返回的原文，外加 finish_reason / refusal / 要不要当场抛
    输出：一个长得像 openai 返回值的对象（choices[0].message.content / refusal）

    真 client 的形状里我们只用到这几层，所以只造这几层。这一层本来就不认 openai
    的类型（draft_queries 收的是「一个能 chat 的对象」），假的真的都一样。
    """

    def __init__(self, *, content=_KEEP, finish_reason="stop", refusal=None, boom=None):
        self._content = None if content is _KEEP else content
        self._finish_reason = finish_reason
        self._refusal = refusal
        self._boom = boom
        self.seen: dict = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.seen = kwargs
        if self._boom is not None:
            raise self._boom
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self._finish_reason,
                    message=SimpleNamespace(content=self._content, refusal=self._refusal),
                )
            ]
        )


# ---------- 模型那一半：schema / 拼消息 ----------


def test_the_schema_requires_every_property_it_declares():
    """strict 模式的硬要求：每一层的 required 必须列全 properties 里的每一个键。

    漏一个，模型可以整个不返回那个键，parse_plan 当场 KeyError —— 而那要等到
    某次它正好省掉那个字段才发生。additionalProperties 同理：少写一处，
    多出来的键会被悄悄收下。
    """

    def walk(node):
        if node.get("type") != "object":
            if node.get("type") == "array":
                walk(node["items"])
            return
        assert node["additionalProperties"] is False, node
        assert set(node["required"]) == set(node["properties"]), node
        for child in node["properties"].values():
            walk(child)

    assert PLAN_SCHEMA["strict"] is True
    walk(PLAN_SCHEMA["schema"])


def test_the_prompt_carries_the_cap_from_the_config_not_a_hardcoded_number():
    """配置写 3、prompt 写 2 那种不一致没有任何症状：模型按它看到的数拟，
    代码按配置那个数截断，中间被砍掉的几条白花了输出 token。

    所以这个数只有配置里那一处真相，prompt 里是插值进去的。
    """
    messages = build_planner_messages(
        INTENT, KNOWLEDGE, lines=LINES, feedback="", previous_plan=None, max_queries=7
    )

    assert "最多 7 条" in messages[0]["content"]
    # 占位符必须被换干净，留着的话模型看到的是一句它读不懂的话
    assert "__MAX_QUERIES__" not in messages[0]["content"]


def test_only_the_four_fields_with_a_consumer_are_fed_to_the_model():
    """喂进去而没人要它做什么的字段，模型会认真对待它，然后做出我们没要求的事。

    最具体的一条是 unsupported：它装的是「我们做不到的」，喂了它模型可能会认真
    尝试把「只要加州的」写成 California 塞进检索式 —— 那正是我们判定做不到的事。
    """
    messages = build_planner_messages(
        INTENT, KNOWLEDGE, lines=(), feedback="", previous_plan=None, max_queries=3
    )
    payload = json.loads(messages[1]["content"])

    assert set(payload["intent"]) == {"goal", "topics", "events", "exclusions"}


def test_the_conversation_is_fed_so_that_replanning_can_change_anything():
    """温度是 0。用户说「改」之后，喂进来的东西一个字都没变的话，拟出来的就是
    逐字相同的一版 —— 他改一次看见同一版方案，再改一次还是同一版，全程不报错。

    「别带 Kickstarter」还有 understand_intent 那条路（会进 exclusions），
    但「第二条不要了」这种改的是方案本身，intent 里没有任何字段装得下它。
    """
    messages = build_planner_messages(
        INTENT,
        KNOWLEDGE,
        lines=LINES,
        feedback="第二条不要了",
        previous_plan={"version": 1, "queries": [{"text": "a", "filters": {}}]},
        max_queries=3,
    )
    payload = json.loads(messages[1]["content"])

    assert payload["conversation"] == list(LINES)
    assert payload["previous_plan"] == {"version": 1, "queries": ["a"]}


def test_the_first_draft_says_there_is_no_previous_plan():
    """第一次拟的时候这一格必须是 None，不能是空字典。

    空字典会让 prompt 第六节那句「有 previous_plan 就是在改不是在拟」判错边，
    而症状是它以为用户提过意见，于是小心翼翼地不敢动。
    """
    messages = build_planner_messages(
        INTENT, KNOWLEDGE, lines=LINES, feedback="", previous_plan={}, max_queries=3
    )

    assert json.loads(messages[1]["content"])["previous_plan"] is None


def test_the_payload_keeps_chinese_readable():
    """ensure_ascii 开着的话 goal 和整段对话会变成一串转义码。

    模型读得懂，但日志里的人读不懂，而这两样正是出事时第一个要看的东西。
    """
    messages = build_planner_messages(
        INTENT, KNOWLEDGE, lines=LINES, feedback="", previous_plan=None, max_queries=3
    )

    assert "找刚做完众筹的骨传导耳机团队" in messages[1]["content"]


# ---------- 模型那一半：解析 ----------


def test_a_full_answer_becomes_the_plan_shape():
    draft = parse_plan(
        {"queries": [{"text": " Kickstarter AND bone conduction ", "authorKeywords": "Founder OR CTO"}]}
    )

    assert draft.queries == (
        {"text": "Kickstarter AND bone conduction", "filters": {"authorKeywords": "Founder OR CTO"}},
    )


def test_an_empty_author_filter_is_dropped_not_kept_as_a_blank():
    """0009 那条唯一索引是 (text_normalized, filters)，filters 整份参与身份。

    所以空字典和「有这个键但值是空串」在库里是两条不同的 query —— 同一句词会凭空
    多出一行，而它跟原来那条采回来的东西一模一样。
    顺带：validate_filters 也不收空串，所以留着的话这条会在 check_query 那里被判
    不合格、被丢掉，用户看见的是一条他看不懂的错。
    """
    draft = parse_plan({"queries": [{"text": "a", "authorKeywords": "   "}]})

    assert draft.queries[0]["filters"] == {}
    # 反过来验一次：留着空串的话，那一条真的会被判不合格
    assert check_query("a", filters={"authorKeywords": ""}) != ()


def test_the_model_order_is_kept_because_the_cap_relies_on_it():
    """砍数量用的是截断，留前面的 —— 那等于用模型自己排的好坏顺序。

    这里重排一下的话不报错，只是砍掉的可能正是它认为最好的那条。
    """
    draft = parse_plan(
        {
            "queries": [
                {"text": "first", "authorKeywords": ""},
                {"text": "second", "authorKeywords": ""},
                {"text": "third", "authorKeywords": ""},
            ],
        }
    )

    assert [q["text"] for q in draft.queries] == ["first", "second", "third"]


def test_a_bad_query_is_carried_out_not_refused_here():
    """这一步只做形状转换，不做质量判断。

    在这里拒收等于为一条词毙掉整条对话；带出去的话，check_query 会把它说清楚，
    而用户在卡片上看得见它为什么没跑。
    """
    draft = parse_plan({"queries": [{"text": "骨传导 NOT supplier", "authorKeywords": ""}]})

    assert draft.queries[0]["text"] == "骨传导 NOT supplier"
    assert check_query(draft.queries[0]["text"], filters={}) != ()


def test_a_missing_field_blows_up_with_the_answer_attached():
    """strict 的 json_schema 下这几个键必然在，缺了只能是 schema 和这个函数漂了。

    那是我们的 bug，正确反应是改代码不是兜底。报错要带上原文 —— 光说「少了字段」
    查不了。
    """
    with pytest.raises(PlannerCallFailed, match="少了字段"):
        parse_plan({})

    with pytest.raises(PlannerCallFailed, match="少了字段"):
        parse_plan({"queries": [{"text": "a"}]})


def test_queries_that_is_not_a_list_blows_up():
    with pytest.raises(PlannerCallFailed, match="得是一个列表"):
        parse_plan({"queries": "a"})


def test_a_query_that_is_not_an_object_blows_up():
    with pytest.raises(PlannerCallFailed, match="不是对象"):
        parse_plan({"queries": ["a"]})


# ---------- 模型那一半：那一次调用 ----------


def test_a_good_call_comes_back_parsed():
    client = _FakeClient(content=json.dumps({"queries": [{"text": "a", "authorKeywords": ""}]}))

    draft = draft_queries(
        client,
        intent=INTENT,
        knowledge=KNOWLEDGE,
        lines=LINES,
        feedback="",
        previous_plan=None,
        config=CONFIG,
    )

    assert draft.queries == ({"text": "a", "filters": {}},)
    # 发出去的那几个参数照配置走，不是各处写死的
    assert client.seen["model"] == CONFIG.model
    assert client.seen["max_tokens"] == CONFIG.max_tokens
    assert client.seen["temperature"] == CONFIG.temperature
    assert client.seen["timeout"] == CONFIG.request_timeout_seconds
    assert client.seen["response_format"]["json_schema"] is PLAN_SCHEMA


def test_a_truncated_answer_is_not_treated_as_an_answer():
    """被 max_tokens 截断的话拿到的是半个 JSON。吞掉它的下一步就是拿着半版方案
    去问用户「跑不跑」，而他点了跑就真花钱。
    """
    client = _FakeClient(content='{"queries": [{"text": "Kick', finish_reason="length")

    with pytest.raises(PlannerCallFailed, match="截断"):
        draft_queries(
            client,
            intent=INTENT,
            knowledge=KNOWLEDGE,
            lines=LINES,
            feedback="",
            previous_plan=None,
            config=CONFIG,
        )


def test_a_refusal_is_not_treated_as_an_answer():
    client = _FakeClient(content=None, refusal="我不能帮你做这个")

    with pytest.raises(PlannerCallFailed, match="拒答"):
        draft_queries(
            client,
            intent=INTENT,
            knowledge=KNOWLEDGE,
            lines=LINES,
            feedback="",
            previous_plan=None,
            config=CONFIG,
        )


def test_no_content_is_not_treated_as_an_answer():
    client = _FakeClient(content=None)

    with pytest.raises(PlannerCallFailed, match="没有返回内容"):
        draft_queries(
            client,
            intent=INTENT,
            knowledge=KNOWLEDGE,
            lines=LINES,
            feedback="",
            previous_plan=None,
            config=CONFIG,
        )


def test_a_non_json_answer_brings_the_original_text_along():
    """光说「JSON 解析失败」查不了，要看见它到底返回了什么。"""
    client = _FakeClient(content="抱歉，我来帮你拟几个检索词：")

    with pytest.raises(PlannerCallFailed, match="抱歉"):
        draft_queries(
            client,
            intent=INTENT,
            knowledge=KNOWLEDGE,
            lines=LINES,
            feedback="",
            previous_plan=None,
            config=CONFIG,
        )


def test_a_json_that_is_not_an_object_blows_up_here_not_later():
    """null、[]、3 都能被 json.loads 解析成功。

    不在这儿炸的话，下一句取 queries 抛的是 TypeError，那种报错读起来像我们
    自己的 bug，查的方向完全反了。
    """
    client = _FakeClient(content="[]")

    with pytest.raises(PlannerCallFailed, match="不是一个对象"):
        draft_queries(
            client,
            intent=INTENT,
            knowledge=KNOWLEDGE,
            lines=LINES,
            feedback="",
            previous_plan=None,
            config=CONFIG,
        )


def test_a_transport_error_is_wrapped_not_leaked():
    """这一层不 import openai 也不认它的异常类型：认死一个供应商的类型，
    换实现或者写测试都得跟着改。
    """
    client = _FakeClient(boom=TimeoutError("read timeout"))

    with pytest.raises(PlannerCallFailed, match="请求失败"):
        draft_queries(
            client,
            intent=INTENT,
            knowledge=KNOWLEDGE,
            lines=LINES,
            feedback="",
            previous_plan=None,
            config=CONFIG,
        )


def test_the_latest_feedback_is_handed_over_separately():
    """跟 understand_intent 那边同一个理由：对话里我们自己复述的字数会越来越多，
    而这一句才是这一轮的新消息。两样都给不是二选一 —— 「第二条不要了」里那个
    「第二条」指的是我们上一句列的那几条，只有完整对话读得出来。
    """
    messages = build_planner_messages(
        INTENT, KNOWLEDGE, lines=LINES, feedback="  第二条不要了  ", previous_plan=None, max_queries=3
    )
    payload = json.loads(messages[1]["content"])

    # 去掉首尾空白，别的一个字不改
    assert payload["plan_feedback"] == "第二条不要了"
    assert payload["conversation"] == list(LINES)


def test_the_first_draft_has_no_feedback():
    """第一次拟的时候这一格是空串。模型靠它跟 previous_plan 一起判断「这是在改还是在拟」。"""
    messages = build_planner_messages(
        INTENT, KNOWLEDGE, lines=LINES, feedback="", previous_plan=None, max_queries=3
    )

    assert json.loads(messages[1]["content"])["plan_feedback"] == ""


def test_the_payload_keys_line_up_with_the_state_fields():
    """喂给模型的那份 JSON，每个键对应 state 里哪一格，这条对应关系要焊住。

    漂了没有症状：模型照样答得出东西，只是我们在日志里看见一份 payload 的时候，
    没法一眼说出这一格是从哪儿来的。而出事的时候第一件要做的事就是拿它对着 state 看。

    三个键跟 state 的字段名一模一样，两个刻意不一样（理由写在 build_planner_messages
    的 docstring 里）：conversation 装的是 state['messages']，名字让给了 OpenAI 那份
    消息列表；previous_plan 装的是 state['plan']，那次调用的产出就是一版新 plan，
    两版摆在一起得分得出哪版是哪版。

    那两条是写明的例外，不是漏改 —— 所以这里逐个键写死，不写成「跟 state 的字段名
    一样」那种自动比对：自动比对会把例外也判成错，然后逼人把例外改掉。
    """
    state_plan = {"version": 1, "queries": [{"text": "a", "filters": {}}], "assumptions": "…"}
    messages = build_planner_messages(
        INTENT,
        KNOWLEDGE,
        lines=LINES,
        feedback="第二条不要了",
        previous_plan=state_plan,
        max_queries=3,
    )
    payload = json.loads(messages[1]["content"])

    assert set(payload) == {"intent", "knowledge", "conversation", "plan_feedback", "previous_plan"}
    assert payload["knowledge"] == KNOWLEDGE  # state['knowledge']
    assert payload["conversation"] == list(LINES)  # state['messages']
    assert payload["plan_feedback"] == "第二条不要了"  # state['plan_feedback']
    assert payload["previous_plan"]["version"] == 1  # state['plan']
