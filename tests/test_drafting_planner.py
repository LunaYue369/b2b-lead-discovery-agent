"""plan_drafts 那次调用和「这一轮写哪几份、每份怎么写」那个纯函数的测试。不调模型、不连库。

用两家公司的 state["lead"]（retrieve_knowledge 读出来的那种形状），名单按统一编号：

    NEW_TOWN   两个邮箱（官网的 contact@、Marc-Andre Lavoie 的 marcandre@），
               名单：1. 这家公司本身 / 2. Zach T.（提到这家的人）/ 3. Marc-Andre（这家的人）/
               4. SingAlong Group（别的机构）；两条证据：[2733] Zach 第三方说的、[2720] Marc-Andre 本人说的
    CHATTERLY    没有邮箱，名单：1. 这家公司本身 / 2. Ravi Shah（职位串写着这家，还没核对）

焊住的几类：

    写错人        模型编了名单里没有的编号、编了一份用户没提过的稿、编了一个用户没说过的邮箱
    编我们的本事  卖点不是 facts.json 的键、开场引了一条不存在的证据
    两半混了      AI 补的那份填了「用户说的」那几格；用户没说写法 AI 也没定
    停下来问      说了但认不出的人和邮箱、份数对不上，都该按固定办法定下来并写一句说明，不再有「要问的」
    多份对不上    说了三份只说清两份没补、同一份说了两次没合并、AI 定的写法碰巧一样被合并
    计划看不懂    「你说的」和「我定的」分不清，卖点印成英文键
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from discovery.analyzer import load_facts
from triggers.interactive_discovery.drafting import messages
from triggers.interactive_discovery.drafting.planner import (
    AI_PLAN_SCHEMA,
    SELLING_POINT_TEXT,
    AiPlan,
    AiPlanCallFailed,
    AiPlanDraft,
    Finalized,
    PlanConfig,
    _revision_text,
    ai_plan_to_state,
    build_messages,
    drafts_after_plan,
    email_listing,
    email_numbers,
    evidence_line,
    finalize_plan,
    listed_people,
    load_plan_config,
    new_entry,
    parse_ai_plan,
    person_line,
    plan_from_state,
    plan_to_state,
    previous_drafts,
    read_ai_plan,
    selling_point_keys,
    target_listing,
    untouched_changes,
)

CONFIG = PlanConfig(
    model="gpt-4.1",
    max_tokens=2000,
    temperature=0.0,
    request_timeout_seconds=30.0,
    max_drafts_per_round=4,
    default_language="英文",
    default_tone="简短、专业，不用推销腔",
)

MARC_ANDRE = {
    "name": "Marc-Andre Lavoie",
    "relation": "member",
    "position": "Founder & CEO",
    "profile_url": "https://www.linkedin.com/in/malavoie",
}
ZACH = {
    "name": "Zach T.",
    "relation": "not_member",
    "position": None,
    "profile_url": "https://www.linkedin.com/in/zachtrent",
}
SINGALONG = {
    "name": "SingAlong Group",
    "relation": "other_page",
    "position": None,
    "profile_url": "https://www.linkedin.com/company/singalong-group/",
}
RAVI = {
    "name": "Ravi Shah",
    "relation": "not_looked_up",
    "position": None,
    "profile_url": "https://www.linkedin.com/in/rshah",
}

NEW_TOWN = {
    "display_name": "North Field Technologies",
    "emails": [
        {
            "value": "contact@northfield-inc.example",
            "source": "website",
            "person_name": None,
            "email_status": None,
            "catch_all": None,
            "quality_score": None,
        },
        {
            "value": "marcandre@northfield-inc.example",
            "source": "linkedin_email_search",
            "person_name": "Marc-Andre Lavoie",
            "email_status": "risky",
            "catch_all": True,
            "quality_score": 60,
        },
    ],
    "people": [ZACH, MARC_ANDRE, SINGALONG],
}
CHATTERLY = {"display_name": "Chatterly Inc.", "emails": [], "people": [RAVI]}

# retrieve_knowledge 取的那份（编的值），facts 是仓库里那份真的 facts.json
KNOWLEDGE = {
    "profile": {"website": "https://northfield-inc.example", "tagline": "Karaoke at home, done right"},
    "assessment": {
        "score": 72,
        "analysis": "North Field is building a karaoke mic and asked for a partner to make 1,000 units.",
        "quotes_mean": "创始人说在找能做小批量的工厂",
        "quotes": ["we need a partner for 1,000 units"],
    },
    "evidence": [
        {
            "signal_id": 2733,
            "lead_type": "mentioned",
            "first_person": False,
            "author": "Zach T.",
            "role": "Producer",
            "when": "2026-09-21",
            "url": "https://www.linkedin.com/posts/zachtrent_1",
            "text": "Had fun testing the @North Field Technologies mic last night.",
            "reposted_author": None,
            "reposted_text": None,
        },
        {
            "signal_id": 2720,
            "lead_type": "author",
            "first_person": True,
            "author": "Marc-Andre Lavoie",
            "role": "Founder & CEO @ North Field Technologies",
            "when": "2026-09-20",
            "url": "https://www.linkedin.com/posts/malavoie_1",
            "text": "Our karaoke mic is ready; we need a partner for 1,000 units.",
            "reposted_author": None,
            "reposted_text": None,
        },
    ],
    "facts": load_facts(),
    "avoid": [],
}
# 没抓过概况、一条证据都没有的那家
BARE_KNOWLEDGE = KNOWLEDGE | {
    "profile": {},
    "evidence": [],
    "assessment": KNOWLEDGE["assessment"] | {"quotes": []},
}

COMPANY_LINE = "North Field Technologies 这家公司本身（称呼他们的团队）"
ZACH_LINE = "Zach T.（发帖的人不是这家公司的，只是在帖子里提到了它，仍可联络）"
JP_LINE = "Marc-Andre Lavoie（Founder & CEO，这家公司的人）"
SINGALONG_LINE = "SingAlong Group（发帖的是别的机构，不是这家公司的，但仍可联络）"
DEFAULT_NOTES = ("语言你没说，按默认用英文", "语气你没说，按默认：简短、专业，不用推销腔")


def _ask(**overrides) -> dict:
    """ai_plan["drafts"] 里的一份。默认：用户除了原话什么都没说；AI 定了写邮件、挑了 moq、
    开场不引帖子。
    """
    base = {
        "plan_number": 0,
        "user_words": "写一份",
        "recipient_user_requested": "",
        "recipient_ai_understood": 0,
        "email_user_requested": "",
        "email_ai_understood": "",
        "form_user_requested": "",
        "emphasis_user_requested": [],
        "form_ai_planned": "email",
        "selling_points_ai_planned": ["moq"],
        "opening_ai_planned": 0,
        "reason_ai_planned": "",
    }
    return base | overrides


def _added(**overrides) -> dict:
    """AI 补的一份：user_words 是空的，用户那一半全空。默认 AI 定了一篇文章。"""
    return _ask(user_words="", form_ai_planned="article") | overrides


def _ai_plan(*drafts: dict, **overrides) -> dict:
    """一份 state["ai_plan"]。"""
    base = {
        "count_user_requested": 0,
        "count_user_words": "",
        "drafts": list(drafts),
        "language_user_requested": "",
        "tone_user_requested": "",
        "unsupported": [],
    }
    return base | overrides


def _finalize(*drafts: dict, lead=NEW_TOWN, **overrides):
    return finalize_plan(_ai_plan(*drafts, **overrides), lead, CONFIG, previous={}, drafts={}).plan


JP_EMAIL = _ask(
    user_words="给 3 号写封邮件，强调 MOQ 500",
    recipient_user_requested="3 号",
    recipient_ai_understood=3,
    form_user_requested="email",
    emphasis_user_requested=["MOQ 500"],
    form_ai_planned="",
    opening_ai_planned=2720,
    reason_ai_planned="创始人说要找工厂做 1,000 台，开场引他这条",
)
ZACH_NOTE = _ask(
    user_words="再给 Zach 写一条加好友附言",
    recipient_user_requested="Zach",
    recipient_ai_understood=2,
    form_user_requested="linkedin_note",
    form_ai_planned="",
    selling_points_ai_planned=["categories"],
)
MIKE = _ask(user_words="再给 Mike 写一份", recipient_user_requested="Mike", recipient_ai_understood=-1)


# ── 配置和 schema ─────────────────────────────────────────────────────────


def test_the_repo_config_loads():
    """仓库里那份 config/drafting.yaml 读得出来，数跟这里测试用的那份一样。"""
    assert load_plan_config() == CONFIG


def test_every_property_is_listed_in_required():
    """strict 模式的硬要求：外层和每份稿两层，required 都必须列全 properties 的每一个键。"""
    outer = AI_PLAN_SCHEMA["schema"]
    item = outer["properties"]["drafts"]["items"]
    assert set(outer["required"]) == set(outer["properties"])
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False


def test_every_selling_point_has_a_chinese_name():
    """facts.json 里每个能挑的键在计划里都有中文叫法。这条红了：facts.json 加了键，SELLING_POINT_TEXT 没跟上，
    计划里会印出英文键给用户看。
    """
    assert set(selling_point_keys(KNOWLEDGE)) == set(SELLING_POINT_TEXT)


# ── 统一编号 ────────────────────────────────────────────────────────────


def test_target_listing_puts_the_company_first():
    """1 号永远是这家公司本身，人从 2 号起，顺序跟卡片一样。"""
    assert target_listing(NEW_TOWN) == (f"1. {COMPANY_LINE}\n2. {ZACH_LINE}\n3. {JP_LINE}\n4. {SINGALONG_LINE}")


def test_the_companys_own_page_does_not_take_a_number():
    """这家自己的 LinkedIn 主页就是 1 号，不再单独占一个号，后面的人往前挪。"""
    own_page = {"name": "North Field Technologies", "relation": "own_page", "position": None, "profile_url": "u"}
    lead = NEW_TOWN | {"people": [own_page, MARC_ANDRE]}

    assert listed_people(lead) == [MARC_ANDRE]
    assert target_listing(lead) == f"1. {COMPANY_LINE}\n2. {JP_LINE}"


# ── 解析：用户那一半 ────────────────────────────────────────────────────

LINES = [
    "AI: 请问你想给 North Field Technologies 写哪些 outreach 信息？…",
    "用户: 写三份，给 3 号写封邮件，强调 MOQ 500，发到 jp@northfield-inc.example",
]


def _parse(payload: dict, lines=LINES, lead=NEW_TOWN, knowledge=KNOWLEDGE, revising=False) -> AiPlan:
    return parse_ai_plan(payload, lines, lead, knowledge, revising=revising)


def test_parse_a_good_answer():
    """一份正常的回答：原话去空白、空串扔掉，编号原样留下，两半各进各的格。"""
    payload = _ai_plan(
        JP_EMAIL | {"recipient_user_requested": " 3 号 ", "emphasis_user_requested": ["MOQ 500", " "]},
        count_user_requested=3,
        count_user_words="写三份",
    )

    assert _parse(payload) == AiPlan(
        count_user_requested=3,
        count_user_words="写三份",
        drafts=(
            AiPlanDraft(
                plan_number=0,
                user_words="给 3 号写封邮件，强调 MOQ 500",
                recipient_user_requested="3 号",
                recipient_ai_understood=3,
                email_user_requested="",
                email_ai_understood="",
                form_user_requested="email",
                emphasis_user_requested=("MOQ 500",),
                form_ai_planned="",
                selling_points_ai_planned=("moq",),
                opening_ai_planned=2720,
                reason_ai_planned="创始人说要找工厂做 1,000 台，开场引他这条",
            ),
        ),
        language_user_requested="",
        tone_user_requested="",
        unsupported=(),
    )


def test_user_words_survive_spaces_punctuation_and_case():
    """模型抄原话时多一个空格、少一个标点、大小写不同，都认得出是那句话，不当成编的。"""
    payload = _ai_plan(JP_EMAIL | {"user_words": "给3号写封邮件 强调moq500。"})

    assert _parse(payload).drafts[0].recipient_ai_understood == 3


def test_an_email_the_user_said_is_kept():
    payload = _ai_plan(JP_EMAIL | {"email_user_requested": "jp@northfield-inc.example"})

    assert _parse(payload).drafts[0].email_user_requested == "jp@northfield-inc.example"


@pytest.mark.parametrize(
    ("draft", "match"),
    [
        ({"recipient_ai_understood": 5}, "名单只有 4 号"),  # 模型编了一个名单里没有的号
        ({"recipient_ai_understood": -2}, "名单只有 4 号"),
        ({"recipient_ai_understood": True}, "名单只有 4 号"),  # bool 是 int 的子类，不能当成 1
        ({"form_user_requested": "wechat"}, "写法返回了"),
        ({"user_words": "给 Mike 写一封"}, "找不到"),  # 用户从没说过这句
        ({"email_user_requested": "contact@northfield-inc.example"}, "用户没说过"),  # 模型从卡片上抄的
    ],
    ids=["pick-past-the-list", "pick-below-minus-one", "pick-is-a-bool", "unknown-form", "made-up-draft",
         "made-up-email"],
)  # fmt: skip
def test_parse_refuses_a_draft_that_would_go_wrong(draft, match):
    """编号出了名单、写法不认识、编出一份用户没提过的稿、编一个用户没说过的邮箱：当场报错，不兜底往下写。"""
    with pytest.raises(AiPlanCallFailed, match=match):
        _parse(_ai_plan(JP_EMAIL | draft))


def test_user_words_only_count_what_the_user_said():
    """user_words 只在用户的话里找：抄的是 bot 自己问的那句，也算编的。"""
    payload = _ai_plan(JP_EMAIL | {"user_words": "请问你想给 North Field Technologies 写哪些"})

    with pytest.raises(AiPlanCallFailed, match="找不到"):
        _parse(payload)


@pytest.mark.parametrize("words", ["", "写五份"], ids=["no-words", "words-never-said"])
def test_a_count_without_the_users_words_is_dropped(words):
    """份数的原话没给、或者在对话里找不到：这个数多半是模型自己数的，当作没说份数，不报错。"""
    parsed = _parse(_ai_plan(JP_EMAIL, count_user_requested=5, count_user_words=words))

    assert (parsed.count_user_requested, parsed.count_user_words) == (0, "")


def test_parse_refuses_a_negative_count():
    with pytest.raises(AiPlanCallFailed, match="count_user_requested"):
        _parse(_ai_plan(JP_EMAIL, count_user_requested=-1))


@pytest.mark.parametrize("where", ["outer", "draft"])
def test_parse_refuses_a_missing_key(where):
    """外层少一个键、某份稿少一个键，都当场报错，报出是哪个键。"""
    payload = _ai_plan(dict(JP_EMAIL))
    if where == "outer":
        del payload["count_user_words"]
        key = "count_user_words"
    else:
        del payload["drafts"][0]["selling_points_ai_planned"]
        key = "selling_points_ai_planned"

    with pytest.raises(AiPlanCallFailed, match=key):
        _parse(payload)


def test_to_state_dict_round_trips():
    """摊平再解析回来一模一样：state 里存的那份，下一轮拿出来算的就是同一份。"""
    parsed = _parse(_ai_plan(JP_EMAIL, _added(), count_user_requested=3, count_user_words="写三份"))

    assert _parse(ai_plan_to_state(parsed)) == parsed


def test_parse_treats_minus_one_without_words_as_not_said():
    """模型填了 -1 却没抄是谁：计划里没法说「你说的「」没认出」，当作没说写给谁。"""
    payload = _ai_plan(JP_EMAIL | {"recipient_user_requested": "", "recipient_ai_understood": -1})

    assert _parse(payload).drafts[0].recipient_ai_understood == 0


def test_parse_treats_zero_with_words_as_unknown():
    """抄了「Mike」编号却填 0：当作名单上认不出（-1），计划里点出 Mike，不说成「没说写给谁」。
    Slack 实测里 gpt-4.1 就这么填过一次（reason 里自己写着「名单中没有 Mike」）
    """
    lines = ["用户: 写三份：给 Marc-Andre 写封邮件，强调 MOQ 500，再给 Mike 写一份"]
    payload = _ai_plan(MIKE | {"recipient_ai_understood": 0})

    (draft,) = _parse(payload, lines=lines).drafts
    assert draft.recipient_ai_understood == -1
    (slot,) = finalize_plan(
        ai_plan_to_state(_parse(payload, lines=lines)), NEW_TOWN, CONFIG, previous={}, drafts={}
    ).plan.slots
    assert slot.notes[0].startswith("你说的「Mike」名单上没认出")


def test_what_the_plan_said_is_not_what_the_user_said():
    """对话里有一版计划时，模型把计划里的「语言：英文」「语气：…」「卖点：起订量低」抄成他说的：一律当没说。

    这条红了的症状（Slack 实测真这样过）：改完一版之后，计划上「语言你没说，按默认用英文」那两句说明没了，
    用户以为是他自己要求的英文
    """
    lines = [
        "用户: 给 3 号写封邮件，强调 MOQ 500",
        "AI: 好，给 North Field Technologies 写这 1 份：\n第 1 份：邮件 → …\n  卖点（我定的）：起订量低（MOQ）\n"
        "语言：英文\n语气：简短、专业，不用推销腔",
        "用户: 第 1 份改成私信",
    ]
    payload = _ai_plan(
        JP_EMAIL | {"emphasis_user_requested": ["MOQ 500", "起订量低（MOQ）"]},
        language_user_requested="英文",
        tone_user_requested="简短、专业，不用推销腔",
    )
    payload["drafts"][0]["user_words"] = "给 3 号写封邮件，强调 MOQ 500"

    parsed = _parse(payload, lines=lines)

    assert (parsed.language_user_requested, parsed.tone_user_requested) == ("", "")
    assert parsed.drafts[0].emphasis_user_requested == ("MOQ 500",)


def test_parse_refuses_an_email_number_not_on_the_card():
    """模型填了卡片上没有的号：当场报错，不去猜是哪个。"""
    with pytest.raises(AiPlanCallFailed, match="卡片上的邮箱号只有"):
        _parse(_ai_plan(JP_EMAIL | {"email_ai_understood": "1-2"}))


def test_parse_refuses_an_email_number_without_the_users_words():
    """填了号却没抄用户说邮箱的原话：多半是模型自己挑的，报错。"""
    with pytest.raises(AiPlanCallFailed, match="却没抄用户说邮箱的原话"):
        _parse(_ai_plan(JP_EMAIL | {"email_ai_understood": "1-1"}))


# ── 解析：AI 那一半 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("draft", "match"),
    [
        ({"selling_points_ai_planned": ["cheap_price"]}, "不是 facts.json 的键"),  # 编了一样我们没有的本事
        ({"selling_points_ai_planned": ["_todo"]}, "不是 facts.json 的键"),  # 下划线开头的是待办，不是卖点
        ({"selling_points_ai_planned": []}, "一个卖点都没挑"),
        ({"opening_ai_planned": 9999}, "这家的证据只有"),  # 引了一条不存在的帖子
        ({"opening_ai_planned": True}, "这家的证据只有"),
        ({"form_user_requested": "", "form_ai_planned": ""}, "form_ai_planned 却是空的"),
    ],
    ids=["unknown-selling-point", "underscore-key", "no-selling-point", "unknown-evidence",
         "evidence-is-a-bool", "nobody-picked-a-form"],
)  # fmt: skip
def test_parse_refuses_what_ai_made_up(draft, match):
    """AI 那一半编了东西（卖点不在 facts.json、开场引了不存在的证据）或者该定的没定：当场报错。"""
    with pytest.raises(AiPlanCallFailed, match=match):
        _parse(_ai_plan(JP_EMAIL | draft))


def test_no_form_is_fine_when_the_user_named_an_email():
    """用户没说写法、但说了邮箱（那就是写邮件）：AI 不用再定写法，不报错。"""
    ask = JP_EMAIL | {"form_user_requested": "", "email_user_requested": "jp@northfield-inc.example"}

    assert _parse(_ai_plan(ask)).drafts[0].form_ai_planned == ""


def test_an_evidence_free_company_only_takes_zero():
    """这家一条证据都没有：开场只能填 0；填了任何编号都是编的。"""
    (draft,) = _parse(_ai_plan(_ask(user_words="写三份")), knowledge=BARE_KNOWLEDGE).drafts
    assert draft.opening_ai_planned == 0
    with pytest.raises(AiPlanCallFailed, match="这家的证据只有"):
        _parse(_ai_plan(_ask(user_words="写三份", opening_ai_planned=2720)), knowledge=BARE_KNOWLEDGE)


def test_an_added_draft_ignores_what_the_user_half_says():
    """AI 补的那份（user_words 是空的）填了「用户说的」那几格：一律不看，当没说（写给这家公司）。"""
    payload = _ai_plan(
        _added(recipient_user_requested="Zach", recipient_ai_understood=2, form_user_requested="email")
    )

    (draft,) = _parse(payload, lines=["用户: 写吧"]).drafts
    assert (draft.recipient_user_requested, draft.recipient_ai_understood, draft.form_user_requested) == (
        "",
        0,
        "",
    )
    assert draft.form_ai_planned == "article"


def test_added_drafts_go_after_what_the_user_said():
    """模型把补的那份排在前面：解析后用户说的排前，补的接在后面。"""
    parsed = _parse(_ai_plan(_added(), JP_EMAIL))

    assert [d.user_words for d in parsed.drafts] == ["给 3 号写封邮件，强调 MOQ 500", ""]


def test_nothing_said_and_nothing_added_is_refused():
    """用户只回了「写吧」，模型却一份都没补：这一轮没东西可写，报错，不发一份空计划。"""
    with pytest.raises(AiPlanCallFailed, match="一份都没说清"):
        _parse(_ai_plan(), lines=["用户: 写吧"])


# ── 调模型 ──────────────────────────────────────────────────────────────


class _FakeClient:
    """照剧本回答的 client，形状跟 tests/test_intent.py 那个一样。"""

    def __init__(self, content, *, finish_reason="stop"):
        self._content = content
        self._finish_reason = finish_reason
        self.kwargs: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs.append(kwargs)
        message = SimpleNamespace(content=self._content, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=self._finish_reason, message=message)])


def test_read_plan_sends_the_card_the_knowledge_and_the_conversation():
    """喂给模型的：system 一条、这家公司一条（邮箱、统一编号的名单）、这家的情况和我们能提供的一条、对话一条；
    拿回来解析成 AiPlan。
    """
    client = _FakeClient(json.dumps(_ai_plan(JP_EMAIL | {"recipient_ai_understood": 4})))

    got = read_ai_plan(
        client, LINES, NEW_TOWN, KNOWLEDGE, feedback="", previous=[], drafts={}, plan={}, config=CONFIG
    )

    assert got.drafts[0].recipient_ai_understood == 4
    sent = client.kwargs[0]
    assert sent["response_format"]["json_schema"] is AI_PLAN_SCHEMA
    assert (sent["timeout"], sent["max_tokens"]) == (30.0, 2000)
    assert sent["messages"][1]["content"] == (
        "这家公司：North Field Technologies\n"
        f"名单：\n1. {COMPANY_LINE}\n2. {ZACH_LINE}\n3. {JP_LINE}\n4. {SINGALONG_LINE}\n"
        "卡片上的邮箱：\n1-1 contact@northfield-inc.example\n"
        "3-1 marcandre@northfield-inc.example（Marc-Andre Lavoie 的）"
    )
    knowledge = sent["messages"][2]["content"]
    assert "为什么推这家（打分 72）：North Field is building a karaoke mic" in knowledge
    assert "打分时引的原话：we need a partner for 1,000 units" in knowledge
    # 证据带编号、带是不是本人说的：模型挑开场时要看这两样
    assert (
        "[2720] Marc-Andre Lavoie（Founder & CEO @ North Field Technologies） 2026-09-20 的帖子，本人说的："
        "Our karaoke mic is ready; we need a partner for 1,000 units."
    ) in knowledge
    assert "[2733] Zach T.（Producer） 2026-09-21 的帖子，第三方说的：" in knowledge
    assert (
        "卖点只能从这几个键里挑：moq, categories, services, certifications, company, product_families"
    ) in knowledge
    assert sent["messages"][3]["content"] == "\n".join(LINES)


def test_build_messages_for_a_bare_company():
    """卡片上一个人都没有、没有邮箱、没抓过概况、没有证据：每一样都写明「没有」，不留空。"""
    lead = {"display_name": "Chatterly Inc.", "emails": [], "people": []}

    sent = build_messages(["用户: 写吧"], lead, BARE_KNOWLEDGE, feedback="", previous=[], drafts={}, plan={})

    assert sent[1]["content"] == (
        "这家公司：Chatterly Inc.\n名单：\n1. Chatterly Inc. 这家公司本身（称呼他们的团队）\n"
        "卡片上的邮箱：\n（没有）"
    )
    assert sent[2]["content"].startswith("这家公司的概况：（没抓过）\n")
    assert "证据（开场要引就填前面的编号）：\n（没有）" in sent[2]["content"]
    assert "打分时引的原话" not in sent[2]["content"]


@pytest.mark.parametrize(
    ("client", "match"),
    [
        (_FakeClient("{}", finish_reason="length"), "截断"),
        (_FakeClient("not json"), "不是合法 JSON"),
        (_FakeClient("[]"), "不是一个对象"),
        (_FakeClient(None), "没有返回内容"),
    ],
    ids=["truncated", "not-json", "not-an-object", "no-content"],
)
def test_read_plan_fails_loudly(client, match):
    """拿不到好回答一律抛 AiPlanCallFailed：宁可让用户看到「出错了」，也不编一份计划往下写。"""
    with pytest.raises(AiPlanCallFailed, match=match):
        read_ai_plan(
            client,
            ["用户: 写吧"],
            NEW_TOWN,
            KNOWLEDGE,
            feedback="",
            previous=[],
            drafts={},
            plan={},
            config=CONFIG,
        )


# ── finalize_plan：每份写给谁、写哪一种、发到哪 ────────────────────────


def test_two_clear_drafts():
    """两份都说清了：邮件发到 Marc-Andre 自己的邮箱；附言写给 Zach（提到这家的人），提醒一句写给了外人。
    AI 那一半原样带过来。
    """
    plan = _finalize(JP_EMAIL, ZACH_NOTE)

    first, second = plan.slots
    assert (first.number, first.form, first.address) == (1, "email", "marcandre@northfield-inc.example")
    assert first.recipient == MARC_ANDRE and first.emphasis == ("MOQ 500",) and first.notes == ()
    assert (first.selling_points, first.opening, first.ai_decides) == (("moq",), 2720, ())
    assert (second.number, second.form, second.address) == (2, "linkedin_note", None)
    assert second.notes == (f"{ZACH_LINE}，照样给他写",)
    assert plan.notes == DEFAULT_NOTES
    assert plan.ai_plans_all is False


def test_email_to_someone_without_their_own_address_goes_to_the_public_one():
    """给 Zach 写邮件，Zach 没有自己的邮箱：发到官网公开邮箱，并说清换了地址。"""
    (slot,) = _finalize(ZACH_NOTE | {"form_user_requested": "email"}).slots

    assert slot.address == "contact@northfield-inc.example"
    assert "Zach T. 没有找到邮箱，发到官网上的公开邮箱 contact@northfield-inc.example" in slot.notes


def test_email_to_number_one_goes_to_the_public_address():
    """「发官网邮箱，写给他们团队」读成 1 号：不指定人，发官网那个，不用提醒。"""
    ask = _ask(user_words="发官网邮箱", recipient_ai_understood=1, form_user_requested="email")

    (slot,) = _finalize(ask).slots

    assert (slot.recipient, slot.address, slot.notes) == (None, "contact@northfield-inc.example", ())


def test_no_recipient_means_the_company():
    """「写封邮件」没说写给谁：写给这家公司（AI 不替他挑人），计划里写一句。"""
    (slot,) = _finalize(_ask(user_words="写封邮件", form_user_requested="email")).slots

    assert (slot.recipient, slot.address) == (None, "contact@northfield-inc.example")
    assert slot.notes == (f"没说写给谁，写给 {COMPANY_LINE}",)


def test_linkedin_to_the_company_is_written_with_a_note():
    """LinkedIn 写给这家公司：照写，提醒一句发给谁由他定。"""
    ask = _ask(user_words="给他们发条私信", recipient_ai_understood=1, form_user_requested="linkedin_message")

    (slot,) = _finalize(ask).slots

    assert slot.recipient is None
    assert slot.notes == ("LinkedIn 消息只能发给一个人或公司主页，称呼写这家公司，发给谁由你定",)


def test_no_form_uses_what_ai_planned():
    """「给 3 号写一份」没说写哪一种：用 AI 定的那种，标出是 AI 定的。"""
    ask = _ask(user_words="给 3 号写一份", recipient_ai_understood=3, form_ai_planned="linkedin_message")

    (slot,) = _finalize(ask).slots

    assert (slot.form, slot.ai_decides, slot.recipient) == ("linkedin_message", ("form",), MARC_ANDRE)


def test_the_users_form_wins_over_what_ai_planned():
    """用户说了邮件、AI 那格也填了附言：用用户说的，不算 AI 定的。"""
    (slot,) = _finalize(JP_EMAIL | {"form_ai_planned": "linkedin_note"}).slots

    assert (slot.form, slot.ai_decides) == ("email", ())


def test_an_article_is_written_to_the_company():
    """文章不写给具体的人：写给这家公司，没有地址；点了人的，说一句按写给公司写。"""
    plain, named = _finalize(
        _ask(
            user_words="写一篇讲小批量的文章",
            form_user_requested="article",
            emphasis_user_requested=["小批量"],
        ),
        _ask(user_words="给 3 号写一篇文章", recipient_ai_understood=3, form_user_requested="article"),
    ).slots

    assert (plain.recipient, plain.address, plain.notes) == (None, None, ())
    assert named.recipient is None
    assert named.notes == ("文章不写给具体的人，按写给 North Field Technologies 这家公司写",)


def test_no_email_at_all_still_writes_the_email():
    """Chatterly 一个邮箱都没有，用户要邮件：照写，提醒一句地址要他自己找。"""
    ask = _ask(user_words="给 Ravi 写封邮件", recipient_ai_understood=2, form_user_requested="email")

    (slot,) = _finalize(ask, lead=CHATTERLY).slots

    assert (slot.form, slot.address, slot.recipient) == ("email", None, RAVI)
    assert slot.notes == ("这家没有找到邮箱，邮件照样写，发之前要你自己找地址",)


# ── 说了但认不出：不问，按固定办法定下来，写一句 ──────────────────────


def test_an_unknown_person_goes_to_the_company_with_a_note():
    """「再给 Mike 写一份」，名单里没有 Mike：不问，这份先写给这家公司，说明里点出他说的原话和怎么改。"""
    (_jp, slot) = _finalize(JP_EMAIL, MIKE).slots

    assert (slot.recipient, slot.address) == (None, "contact@northfield-inc.example")
    assert slot.notes == (f"你说的「Mike」名单上没认出，这份先写给 {COMPANY_LINE}；是名单上哪一号告诉我就改",)


def test_an_unknown_person_with_a_picked_email_goes_to_the_emails_owner():
    """「给 Mike 发到 3-1」：Mike 名单上没有，但邮箱选了 -> 写给邮箱主人 Marc-Andre，说一句。"""
    ask = MIKE | {"form_user_requested": "email", "email_user_requested": "3-1", "email_ai_understood": "3-1"}

    (slot,) = _finalize(ask).slots

    assert slot.recipient == MARC_ANDRE
    assert "你说的「Mike」名单上没认出，按这个邮箱的主人写给 Marc-Andre Lavoie" in slot.notes


def test_an_email_not_on_the_card_is_sent_as_said():
    """说了一个卡片上没有的地址：照他说的发，写一句不是卡片上的。"""
    (slot,) = _finalize(
        JP_EMAIL | {"email_user_requested": "jp@northfield-inc.example", "email_ai_understood": "-1"}
    ).slots

    assert (slot.address, slot.recipient) == ("jp@northfield-inc.example", MARC_ANDRE)
    assert "jp@northfield-inc.example 不是卡片上的邮箱，照你说的发到这个地址" in slot.notes


def test_a_described_email_falls_back_to_the_default():
    """「发 sales 那个」对不上：写邮件，按默认顺序挑（写给公司 -> 官网的第一个），说一句怎么换。"""
    ask = _ask(user_words="发 sales 那个", email_user_requested="sales 那个", email_ai_understood="-1",
               form_ai_planned="")  # fmt: skip

    (slot,) = _finalize(ask).slots

    assert (slot.form, slot.ai_decides, slot.address) == ("email", (), "contact@northfield-inc.example")
    assert slot.notes == (
        f"没说写给谁，写给 {COMPANY_LINE}",
        "你说的「sales 那个」没认出是哪个邮箱，按默认发到 contact@northfield-inc.example；要换说邮箱的号",
    )


# ── 邮箱单独编号 ────────────────────────────────────────────────────────

TWO_SITE = NEW_TOWN | {
    "emails": [
        NEW_TOWN["emails"][0],
        NEW_TOWN["emails"][0] | {"value": "sales@northfield-inc.example"},
        NEW_TOWN["emails"][1],
    ]
}


def test_email_numbers_group_by_owner():
    """号 = 主人的编号-在他名下第几个。两个官网邮箱都挂 1 号下，是 1-1、1-2；Marc-Andre 是 3 号，他的是 3-1。
    名单编号不因为邮箱多而变。
    """
    assert [(n, o, e["value"]) for n, o, e in email_numbers(TWO_SITE)] == [
        ("1-1", 1, "contact@northfield-inc.example"),
        ("1-2", 1, "sales@northfield-inc.example"),
        ("3-1", 3, "marcandre@northfield-inc.example"),
    ]
    assert target_listing(TWO_SITE) == target_listing(NEW_TOWN)


def test_an_email_whose_owner_is_not_listed_hangs_under_the_company():
    """主人不在名单上的个人邮箱：挂 1 号下，列出来带上是谁的。"""
    lead = NEW_TOWN | {"people": [ZACH]}

    assert email_listing(lead) == (
        "1-1 contact@northfield-inc.example\n1-2 marcandre@northfield-inc.example（Marc-Andre Lavoie 的）"
    )


def test_the_second_site_email_is_used_when_picked():
    """「用第二个官网邮箱给他们写一封」-> 1-2：发到 sales@，写给这家公司。"""
    ask = _ask(
        user_words="用第二个官网邮箱给他们写一封",
        recipient_ai_understood=1,
        email_user_requested="第二个官网邮箱",
        email_ai_understood="1-2",
        form_user_requested="email",
    )

    (slot,) = _finalize(ask, lead=TWO_SITE).slots

    assert (slot.address, slot.recipient, slot.notes) == ("sales@northfield-inc.example", None, ())


def test_picking_a_persons_email_picks_the_person_and_the_form():
    """只说「发到 3-1」，没点人也没说写哪一种：写给这个邮箱的主人 Marc-Andre，写邮件，不算 AI 定的。"""
    ask = _ask(
        user_words="发到 3-1", email_user_requested="3-1", email_ai_understood="3-1", form_ai_planned=""
    )

    (slot,) = _finalize(ask).slots

    assert (slot.recipient, slot.form, slot.address, slot.ai_decides) == (
        MARC_ANDRE,
        "email",
        "marcandre@northfield-inc.example",
        (),
    )


def test_an_email_on_the_card_picks_its_owner():
    """没说写给谁、只说「发到 marcandre@…」（模型没填号）：地址在卡片上，是 Marc-Andre 的，就写给他。"""
    ask = _ask(
        user_words="发到 marcandre@northfield-inc.example",
        email_user_requested="MarcAndre@northfield-inc.example",
        form_user_requested="email",
    )

    (slot,) = _finalize(ask).slots

    assert (slot.recipient, slot.address) == (MARC_ANDRE, "marcandre@northfield-inc.example")


def test_two_public_emails_to_the_company_are_two_drafts():
    """给这家公司的两个不同邮箱各写一封：地址不同，是两份，不合并。"""
    first = _ask(
        user_words="发 1-1",
        email_user_requested="1-1",
        email_ai_understood="1-1",
        form_user_requested="email",
    )
    second = _ask(user_words="再发 1-2", email_user_requested="1-2", email_ai_understood="1-2",
                  form_user_requested="email")  # fmt: skip

    plan = _finalize(first, second, lead=TWO_SITE)

    assert [s.address for s in plan.slots] == ["contact@northfield-inc.example", "sales@northfield-inc.example"]


def test_an_email_of_someone_else_gets_a_note():
    """「给 Zach 写，发到 3-1」：称呼 Zach、信进 Marc-Andre 的邮箱，照发，但说一句。"""
    ask = ZACH_NOTE | {
        "form_user_requested": "email",
        "email_user_requested": "3-1",
        "email_ai_understood": "3-1",
    }

    (slot,) = _finalize(ask).slots

    assert (slot.recipient, slot.address) == (ZACH, "marcandre@northfield-inc.example")
    assert (
        "marcandre@northfield-inc.example 是 Marc-Andre Lavoie 的邮箱，称呼写的是 Zach T.，照你说的发"
    ) in slot.notes


def test_a_persons_email_for_the_company_gets_a_note():
    """「给他们团队写，发到 3-1」：称呼团队、信进 Marc-Andre 本人的邮箱，说一句。"""
    ask = _ask(
        user_words="给他们团队写，发到 3-1",
        recipient_ai_understood=1,
        email_user_requested="3-1",
        email_ai_understood="3-1",
        form_user_requested="email",
    )

    (slot,) = _finalize(ask).slots

    assert (slot.recipient, slot.address) == (None, "marcandre@northfield-inc.example")
    assert (
        "marcandre@northfield-inc.example 是 Marc-Andre Lavoie 的邮箱，"
        "称呼写的是 North Field Technologies 这家公司，照你说的发"
    ) in slot.notes


# ── 整轮：份数、AI 补的份、合并、上限 ─────────────────────────────────


def test_nothing_said_takes_what_ai_added():
    """只回了「写吧」：AI 补的那几份就是这一轮，都写给这家公司，AI 补的份不说「没说写给谁」。"""
    plan = _finalize(_added(), _added(form_ai_planned="email"))

    assert plan.ai_plans_all
    assert [(s.number, s.form, s.recipient, s.user_words) for s in plan.slots] == [
        (1, "article", None, ""),
        (2, "email", None, ""),
    ]
    assert plan.slots[1].notes == ()
    assert plan.notes == DEFAULT_NOTES


def test_nothing_said_still_caps_what_ai_added():
    """AI 补多了（5 份）：只留每轮上限那么多。"""
    plan = _finalize(*[_added() for _ in range(5)])

    assert len(plan.slots) == 4


def _five_asks() -> list[dict]:
    """五份各不相同的：三个人的邮件 + 两个人的附言，写法都是用户说的。"""
    return [
        JP_EMAIL,
        ZACH_NOTE,
        _ask(user_words="给 Zach 写封邮件", recipient_ai_understood=2, form_user_requested="email"),
        _ask(
            user_words="给 SingAlong 写条私信",
            recipient_ai_understood=4,
            form_user_requested="linkedin_message",
        ),
        _ask(user_words="给 3 号写条附言", recipient_ai_understood=3, form_user_requested="linkedin_note"),
    ]


def test_too_many_drafts_keep_the_first_four():
    """一次说了 5 份，超过每轮上限 4：按说的顺序留前 4 份，说一句后面的下一轮再说。"""
    plan = _finalize(*_five_asks())

    assert [s.number for s in plan.slots] == [1, 2, 3, 4]
    assert plan.slots[3].recipient == SINGALONG
    assert "一轮最多写 4 份，先写前 4 份，后面的下一轮再说" in plan.notes


def test_a_count_above_the_limit_is_capped():
    """说了「写 6 份」只列了 4 份：份数压到上限 4，跟说清的对得上，不补。"""
    plan = _finalize(*_five_asks()[:4], _added(), count_user_requested=6)

    assert len(plan.slots) == 4
    assert "你说要 6 份，一轮最多写 4 份，这一轮按 4 份写" in plan.notes


def test_said_three_heard_two_takes_one_added():
    """说了三份只说清两份：AI 补的接在后面当第 3 份，多补的不要，说一句。"""
    plan = _finalize(JP_EMAIL, ZACH_NOTE, _added(), _added(form_ai_planned="email"), count_user_requested=3)

    third = plan.slots[2]
    assert len(plan.slots) == 3
    assert (third.number, third.user_words, third.form, third.recipient) == (3, "", "article", None)
    assert "你说要 3 份，说清的是 2 份，另外 1 份由我来定" in plan.notes


def test_said_three_heard_two_but_ai_added_none():
    """说了三份只说清两份，AI 却一份都没补：按两份写，说明里写实际几份。"""
    plan = _finalize(JP_EMAIL, ZACH_NOTE, count_user_requested=3)

    assert len(plan.slots) == 2
    assert "你说要 3 份，说清的是 2 份，我补了 0 份，一共 2 份" in plan.notes


def test_said_one_heard_two_writes_both():
    """说了一份却听出两份：按听出来的两份写，不随便扔掉一份。"""
    plan = _finalize(JP_EMAIL, ZACH_NOTE, count_user_requested=1)

    assert len(plan.slots) == 2
    assert "你说要 1 份，我听出了 2 份，按 2 份写" in plan.notes


def test_added_drafts_are_dropped_when_the_user_was_clear():
    """用户说清了要什么、没说份数，AI 还补了一份：不要，不替他加。"""
    plan = _finalize(JP_EMAIL, _added())

    assert [s.user_words for s in plan.slots] == ["给 3 号写封邮件，强调 MOQ 500"]


def test_the_same_draft_said_twice_is_merged():
    """先说「给 3 号写封邮件强调 MOQ 500」，后面又说「3 号那封邮件再提一下固件」：合成一份。
    强调的点、卖点取并集，开场留前面那份的。
    """
    again = JP_EMAIL | {
        "user_words": "3 号那封邮件再提一下固件",
        "emphasis_user_requested": ["固件"],
        "selling_points_ai_planned": ["product_families"],
        "opening_ai_planned": 2733,
        "reason_ai_planned": "",
    }

    plan = _finalize(JP_EMAIL, ZACH_NOTE, again)

    assert [s.number for s in plan.slots] == [1, 2]
    merged = plan.slots[0]
    assert merged.emphasis == ("MOQ 500", "固件")
    assert merged.selling_points == ("moq", "product_families")
    assert (merged.opening, merged.reason) == (2720, "创始人说要找工厂做 1,000 台，开场引他这条")
    assert merged.user_words == "给 3 号写封邮件，强调 MOQ 500；3 号那封邮件再提一下固件"
    assert "第 3 份跟第 1 份都是给 Marc-Andre Lavoie 的邮件，合成了一份" in plan.notes


def test_forms_ai_picked_the_same_are_not_merged():
    """「给 3 号写一份」「3 号再写一份」都没说写法，AI 碰巧都定了邮件：用户没说是同一种，是两份。"""
    first = _ask(user_words="给 3 号写一份", recipient_ai_understood=3)
    second = _ask(user_words="3 号再写一份", recipient_ai_understood=3)

    assert len(_finalize(first, second).slots) == 2


def test_the_count_is_compared_after_merging():
    """说「写两份」，两句其实是同一封（合成一份）：是 2 对 1，AI 补的那份要用上。"""
    again = JP_EMAIL | {"user_words": "3 号那封邮件再提一下固件", "emphasis_user_requested": ["固件"]}

    plan = _finalize(JP_EMAIL, again, _added(), count_user_requested=2)

    assert [s.user_words for s in plan.slots] == [
        "给 3 号写封邮件，强调 MOQ 500；3 号那封邮件再提一下固件",
        "",
    ]


def test_duplicates_are_merged_before_the_limit_cuts():
    """5 份里两份是同一封：合并后是 4 份，正好不超上限，一份都不截。"""
    again = JP_EMAIL | {"user_words": "3 号那封邮件再提一下固件", "emphasis_user_requested": ["固件"]}

    plan = _finalize(*_five_asks()[:4], again)

    assert len(plan.slots) == 4
    assert not any("一轮最多写" in note for note in plan.notes)


def test_same_person_different_forms_are_two_drafts():
    """同一个人，一封邮件一条附言：是两份，不合并。"""
    note = _ask(user_words="再给他写条附言", recipient_ai_understood=3, form_user_requested="linkedin_note")

    assert len(_finalize(JP_EMAIL, note).slots) == 2


def test_what_he_said_about_language_and_tone_wins():
    """说了语言语气就用他的原话，不再记「按默认」。"""
    plan = _finalize(JP_EMAIL, language_user_requested="中文", tone_user_requested="随意一点")

    assert (plan.language, plan.tone, plan.notes) == ("中文", "随意一点", ())


# ── 结束时列的计划 ──────────────────────────────────────────────────────


def test_round_lines_for_two_drafts():
    """两份：每份一行，你说的、我定的、开场、理由、提醒缩进挂在那一份下面；整轮的默认值放最后。"""
    assert messages.round_lines(_finalize(JP_EMAIL, ZACH_NOTE), NEW_TOWN, KNOWLEDGE) == [
        f"第 1 份：邮件，发到 marcandre@northfield-inc.example → {JP_LINE}",
        "  要强调（你说的）：MOQ 500",
        "  卖点（我定的）：起订量低（MOQ）",
        "  开场引：Marc-Andre Lavoie（Founder & CEO @ North Field Technologies） 2026-09-20 的帖子，"
        "本人说的",
        "  为什么这样定：创始人说要找工厂做 1,000 台，开场引他这条",
        f"第 2 份：LinkedIn 加好友附言 → {ZACH_LINE}",
        "  卖点（我定的）：我们专做的品类（音频和语音设备）",
        "  开场不引帖子",
        f"  {ZACH_LINE}，照样给他写",
        "语言：英文",
        "语气：简短、专业，不用推销腔",
        "说明：",
        "- 语言你没说，按默认用英文",
        "- 语气你没说，按默认：简短、专业，不用推销腔",
    ]


def test_round_lines_show_what_ai_decides_and_what_cannot_be_done():
    """写哪一种是 AI 定的标出来并附原话；做不到的要求单独列出来并说清为什么。"""
    ask = _ask(user_words="给 3 号写一份", recipient_ai_understood=3, form_ai_planned="linkedin_message")

    lines = messages.round_lines(_finalize(ask, unsupported=["帮我直接发出去"]), NEW_TOWN, KNOWLEDGE)

    assert lines[:2] == [
        f"第 1 份：LinkedIn 私信（写哪一种是我定的） → {JP_LINE}",
        "  你说的是「给 3 号写一份」",
    ]
    assert "做不到的：帮我直接发出去（稿子只写不发，要你自己复制粘贴发出去）" in lines


def test_round_lines_for_a_draft_added_by_ai():
    """AI 补的那份标「我加的」，不带「你说的是」那一行。"""
    plan = _finalize(JP_EMAIL, ZACH_NOTE, _added(opening_ai_planned=2733), count_user_requested=3)

    lines = messages.round_lines(plan, NEW_TOWN, KNOWLEDGE)

    assert f"第 3 份（我加的）：文章 → {COMPANY_LINE}" in lines
    assert "  开场引：Zach T.（Producer） 2026-09-21 的帖子，第三方说的" in lines
    assert not any(line.startswith("  你说的是") for line in lines)


def test_round_lines_for_an_article():
    ask = _ask(
        user_words="写一篇讲小批量的文章", form_user_requested="article", emphasis_user_requested=["小批量"]
    )

    lines = messages.round_lines(_finalize(ask), NEW_TOWN, KNOWLEDGE)

    assert lines[:2] == [f"第 1 份：文章 → {COMPANY_LINE}", "  要强调（你说的）：小批量"]


def test_plan_text_heads_and_ends_the_plan():
    """await_plan 交出去的计划：用户说了的开头「好，给 X 写这 N 份」，一份都没说的开头「你没说要写什么」；
    最后一段告诉他三种回法，对着判回话那一步的三个取值。
    """
    said = messages.plan_text(
        {"plan": plan_to_state(_finalize(JP_EMAIL)), "lead": NEW_TOWN, "knowledge": KNOWLEDGE, "drafts": {}}
    )
    nothing = messages.plan_text(
        {"plan": plan_to_state(_finalize(_added())), "lead": NEW_TOWN, "knowledge": KNOWLEDGE, "drafts": {}}
    )

    assert said.startswith("好，给 North Field Technologies 写这 1 份：\n第 1 份：邮件")
    assert nothing.startswith(f"你没说要写什么，我定了这 1 份：\n第 1 份（我加的）：文章 → {COMPANY_LINE}")
    assert said.endswith(
        "\n\n要改就直接说，例如「第 2 份写给 3 号」「第 1 份改成私信」「再加一篇文章」；"
        "没问题回「写吧」；不写了回「算了」。"
    )


def test_render_ended_for_a_cancel():
    """不写了（对着计划或对着稿子说的）：回同一句。"""
    assert messages.render_ended({"decision": "cancel"}) == {"text": messages.CANCELLED_TEXT}


def test_render_ended_refuses_an_unknown_decision():
    """走到 END 时 decision 是别的值（例如 approve：同意计划之后去写稿，不会走到 END）：
    图上多了一条通到 END 的路、这里没跟上，当场停，不猜一句发出去。
    """
    with pytest.raises(ValueError, match="只认 cancel / done"):
        messages.render_ended({"decision": "approve"})


def test_person_line_without_a_position():
    assert person_line(ZACH) == ZACH_LINE


def test_the_plan_shortens_a_long_headline():
    """计划那一行只留简介第一个「|」前面那段，太长再截；给模型的那份原样全给。

    这条红了的症状：计划里「开场引」那一行印出发帖人整条 LinkedIn 简介，一行变三行
    """
    long_role = "Getting things done in Japan: Fintech | GTM | Strategy | Partnerships | Innovation"
    evidence = KNOWLEDGE["evidence"][0] | {"role": long_role}
    very_long = evidence | {"role": "ceo @ Hushly - a sanctuary for human sound and more words"}

    assert evidence_line(evidence, short=True) == (
        "Zach T.（Getting things done in Japan: Fintech） 2026-09-21 的帖子，第三方说的"
    )
    shortened = evidence_line(very_long, short=True)
    assert shortened.startswith("Zach T.（ceo @ Hushly - a sanctuary for human sou…）")
    assert long_role in evidence_line(evidence)


def test_the_plan_round_trips_through_state():
    """计划摊成字典存进 state、再读回来，跟存之前一模一样：用户看到的和以后照着写的是同一份。

    用一份什么都有的：写给人、写给公司、AI 补的、有说明、有做不到的
    """
    plan = _finalize(JP_EMAIL, MIKE, _added(), count_user_requested=3, unsupported=["帮我直接发出去"])

    assert plan_from_state(plan_to_state(plan)) == plan


def test_the_plan_in_state_is_plain_json():
    """存进 state 的那份只有字典、列表、字符串、数字、None：存档要在库里躺很久，不放元组和对象。"""
    plan = plan_to_state(_finalize(JP_EMAIL, ZACH_NOTE))

    assert json.loads(json.dumps(plan)) == plan


# ── 改计划：上一版和修改意见 ──────────────────────────────────────────


def test_every_slot_remembers_where_it_came_from():
    """每份记着自己出自 ai_plan 的哪几项：合并过的记两项，AI 补的记它自己那一项，编号重排也不丢。"""
    again = JP_EMAIL | {"user_words": "3 号那封邮件再提一下固件", "emphasis_user_requested": ["固件"]}

    plan = _finalize(JP_EMAIL, ZACH_NOTE, again, _added(), count_user_requested=3)

    assert [(s.number, s.sources) for s in plan.slots] == [(1, (0, 2)), (2, (1,)), (3, (3,))]


def test_previous_drafts_are_numbered_as_the_user_saw_them():
    """上一版每一项标上用户看到的「第几份」：合并的两项都是第 1 份，没进计划的（多补的）是 None。"""
    again = JP_EMAIL | {"user_words": "3 号那封邮件再提一下固件", "emphasis_user_requested": ["固件"]}
    ai_plan = _ai_plan(JP_EMAIL, ZACH_NOTE, again, _added())
    plan = plan_to_state(finalize_plan(ai_plan, NEW_TOWN, CONFIG, previous={}, drafts={}).plan)

    previous = previous_drafts(ai_plan, plan)

    assert [d["plan_number"] for d in previous] == [1, 2, 1, None]
    # 每一项自己那格 plan_number（模型填的 0）被按 sources 算的号盖掉
    assert previous[1] == {**ZACH_NOTE, "plan_number": 2}
    assert previous_drafts({}, {}) == []


def test_the_revision_block_is_only_there_when_revising():
    """要改时多一条消息：上一版原样（JSON）和他那句修改意见，排在对话前面；第一次定计划没有这一条。"""
    ai_plan = _ai_plan(JP_EMAIL, ZACH_NOTE)
    previous = previous_drafts(
        ai_plan, plan_to_state(finalize_plan(ai_plan, NEW_TOWN, CONFIG, previous={}, drafts={}).plan)
    )

    first = build_messages(LINES, NEW_TOWN, KNOWLEDGE, feedback="", previous=[], drafts={}, plan={})
    revising = build_messages(
        LINES,
        NEW_TOWN,
        KNOWLEDGE,
        feedback="第 2 份改成私信",
        previous=previous,
        drafts={},
        plan={"slots": []},
    )

    assert len(first) == 4 and len(revising) == 5
    block = revising[3]["content"]
    assert block.startswith("这一轮是在改上一版计划。")
    assert "他说的：\n第 2 份改成私信" in block
    assert "他看到的第 2 份，就是你上一版的 [1]：" in block
    assert '  [1] {"user_words": "再给 Zach 写一条加好友附言"' in block
    # 每一项原样印出来时不带 plan_number 那一格（分组已经说了是第几份）
    assert '"plan_number"' not in block
    assert revising[4]["content"] == "\n".join(LINES)


def test_untouched_changes_flags_a_slot_nobody_asked_to_change():
    """他只点了第 2 份，模型却把第 1 份的写法也改了：打一行提醒；点名改的那份变了不提醒。"""
    ai_plan = _ai_plan(JP_EMAIL, ZACH_NOTE)
    previous = previous_drafts(
        ai_plan, plan_to_state(finalize_plan(ai_plan, NEW_TOWN, CONFIG, previous={}, drafts={}).plan)
    )
    changed_both = _ai_plan(
        JP_EMAIL | {"form_user_requested": "linkedin_message"},
        ZACH_NOTE | {"form_user_requested": "linkedin_message"},
    )
    only_second = _ai_plan(JP_EMAIL, ZACH_NOTE | {"form_user_requested": "linkedin_message"})

    assert untouched_changes(previous, changed_both, "第 2 份改成私信", {}) == [
        "第 1 份他没点名，这一版却找不到原样的那一项（给 3 号写封邮件，强调 MOQ 500）"
    ]
    assert untouched_changes(previous, only_second, "第二份改成私信", {}) == []
    assert untouched_changes([], only_second, "", {}) == []
    # 看稿时说了不要的那份（账本 dropped），没交回来是对的，不提醒
    dropped_first = {"1": new_entry() | {"status": "dropped"}}
    assert untouched_changes(previous, _ai_plan(ZACH_NOTE), "新加一份：再写一篇文章", dropped_first) == []


def test_a_revision_keeps_what_ai_added_before():
    """用户第一句说「写吧」，AI 补了两份；他看完只改第 1 份（模型把修改那句抄成了第 1 份的原话）：
    第 2 份照留。第一次定计划时同样的输入，AI 补的那份会被丢掉（用户说清了要写什么）。

    这条红了的症状：用户只改了一份，另一份 AI 补的从计划里消失了，而他从没说过不要
    """
    said_nothing = finalize_plan(
        _ai_plan(_added(form_ai_planned="email"), _added()), NEW_TOWN, CONFIG, previous={}, drafts={}
    )
    previous = plan_to_state(said_nothing.plan)
    drafts = drafts_after_plan({}, said_nothing)
    first = _ask(
        plan_number=1,
        user_words="第 1 份改成私信",
        form_user_requested="linkedin_message",
        form_ai_planned="",
    )
    ai_plan = _ai_plan(first, _added(plan_number=2))

    revised = finalize_plan(ai_plan, NEW_TOWN, CONFIG, previous=previous, drafts=drafts).plan
    fresh = finalize_plan(
        _ai_plan(first | {"plan_number": 0}, _added()), NEW_TOWN, CONFIG, previous={}, drafts={}
    ).plan

    assert [(s.number, s.form, s.sources) for s in revised.slots] == [
        (1, "linkedin_message", (0,)),
        (2, "article", (1,)),
    ]
    assert len(fresh.slots) == 1


def test_the_revision_block_groups_by_what_the_user_saw():
    """改计划那一段按他看到的计划分组：合成的一份列出几项、没进计划的单独一组，每一项原样一行。

    用 previous_drafts 那个例子：[0] [2] 合成第 1 份，[1] 是第 2 份，[3] 是模型多补、没要的
    """
    previous = [
        JP_EMAIL | {"plan_number": 1},
        ZACH_NOTE | {"plan_number": 2},
        JP_EMAIL | {"user_words": "3 号那封邮件再提一下固件", "plan_number": 1},
        _added() | {"plan_number": None},
    ]

    lines = _revision_text("第 2 份改成私信", previous, {}, {"slots": []}).split("\n")

    assert lines[4] == (
        "他看到的第 1 份，是你上一版的 [0] 和 [2] 合成的"
        "（几项写给同一个对象、同一种写法、同一个地址，程序合成了一份）："
    )
    assert lines[5].startswith('  [0] {"user_words": "给 3 号写封邮件，强调 MOQ 500"')
    assert lines[6].startswith('  [2] {"user_words": "3 号那封邮件再提一下固件"')
    assert lines[7] == "他看到的第 2 份，就是你上一版的 [1]："
    assert lines[9] == "没进计划的（你多补的、程序没要；或者超了每轮上限被截掉的）：他没看到这几项"
    assert lines[10].startswith('  [3] {"user_words": ""')
    assert lines[lines.index("他说的：") + 1] == "第 2 份改成私信"


def test_a_revised_form_on_the_wrong_half_is_moved_back():
    """改计划时，他说「第 1 份改成私信」，模型把私信填进 form_ai_planned、form_user_requested 还是邮件：
    挪回用户那一半，写私信、不标「我定的」。第一次定计划时同样的两格照信用户说的（邮件）。

    这条红了的症状（Slack 实测真这样过）：他明说了改成私信，计划上第 1 份还是邮件
    """
    lines = LINES + ["用户: 第 1 份改成私信"]
    payload = _ai_plan(JP_EMAIL | {"form_ai_planned": "linkedin_message"})

    revised = _parse(payload, lines=lines, revising=True).drafts[0]
    fresh = _parse(payload, lines=lines, revising=False).drafts[0]

    assert (revised.form_user_requested, revised.form_ai_planned) == ("linkedin_message", "")
    assert (fresh.form_user_requested, fresh.form_ai_planned) == ("email", "linkedin_message")


# ── 改计划：号是身份证，账本跟着走 ────────────────────────────────────────

COMPANY_EMAIL = _ask(
    user_words="给官网发一封邮件",
    recipient_user_requested="官网",
    recipient_ai_understood=1,
    form_user_requested="email",
    form_ai_planned="",
)
SMALL_BATCH_ARTICLE = _ask(
    user_words="再写一篇讲小批量的文章",
    form_user_requested="article",
    form_ai_planned="",
    emphasis_user_requested=["小批量"],
)


def _first_round(*drafts: dict) -> tuple[dict, dict]:
    """第一次定计划，返回 (state["plan"], state["drafts"])。"""
    fin = finalize_plan(_ai_plan(*drafts), NEW_TOWN, CONFIG, previous={}, drafts={})
    return plan_to_state(fin.plan), drafts_after_plan({}, fin)


def _revise(previous: dict, drafts: dict, *items: dict, **overrides) -> tuple[Finalized, dict]:
    """改一次计划，返回 (Finalized, 新账本)。items 里每项要带 plan_number（接着第几份，0 = 新的）。"""
    fin = finalize_plan(_ai_plan(*items, **overrides), NEW_TOWN, CONFIG, previous=previous, drafts=drafts)
    return fin, drafts_after_plan(drafts, fin)


def _as(draft: dict, number: int) -> dict:
    """模型交回的一项接着第 number 份。"""
    return draft | {"plan_number": number}


def _ledger(drafts: dict, **statuses: str) -> dict:
    """把账本里几份改成写过一版、处在某个状态。例 _ledger(d, **{"1": "approved", "2": "written"})"""
    out = {k: dict(v) for k, v in drafts.items()}
    for key, status in statuses.items():
        out[key].update(status=status, version=1, plan_version=1, text=f"第 {key} 份正文")
    return out


def test_the_first_plan_numbers_from_one_and_opens_the_ledger():
    """第一次定计划：从 1 发号，每份 plan_version 1，账本里每份一条 planned、还没写过。"""
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE)

    assert [(s["number"], s["plan_version"]) for s in plan["slots"]] == [(1, 1), (2, 1)]
    assert {k: (e["status"], e["version"], e["slot"]["number"]) for k, e in drafts.items()} == {
        "1": ("planned", 0, 1),
        "2": ("planned", 0, 2),
    }


def test_a_dropped_number_is_never_given_out_again():
    """他说第 2 份不要了（模型没交回 2 号），又加了一份：新的叫第 4 份，不是第 2 份；2 号在账本里记 dropped。

    这条红了的症状：用户在 Slack 上见过的「第 2 份」换成了另一封信，他翻聊天记录就对不上了
    """
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE, COMPANY_EMAIL)

    fin, after = _revise(plan, drafts, _as(JP_EMAIL, 1), _as(COMPANY_EMAIL, 3), SMALL_BATCH_ARTICLE)

    assert [s.number for s in fin.plan.slots] == [1, 3, 4]
    assert fin.dropped == (2,)
    assert after["2"]["status"] == "dropped"
    assert (after["4"]["status"], after["4"]["version"], after["4"]["slot"]["form"]) == (
        "planned",
        0,
        "article",
    )
    # 不要了的那份在账本里留着它最后那版计划，要回来时照它复活
    assert after["2"]["slot"]["form"] == "linkedin_note"

    fin2, _after2 = _revise(
        plan_to_state(fin.plan),
        after,
        _as(JP_EMAIL, 1),
        _as(COMPANY_EMAIL, 3),
        _as(SMALL_BATCH_ARTICLE, 4),
        ZACH_NOTE,
    )
    assert [s.number for s in fin2.plan.slots] == [1, 3, 4, 5]


def test_an_approved_draft_stays_exactly_as_it_was():
    """已定稿的那份，模型交回来时改了写法：不认，计划和账本都原样。"""
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE)
    drafts = _ledger(drafts, **{"1": "approved", "2": "written"})

    fin, after = _revise(
        plan, drafts, _as(JP_EMAIL | {"form_user_requested": "linkedin_message"}, 1), _as(ZACH_NOTE, 2)
    )

    first = fin.plan.slots[0]
    assert (first.number, first.form, first.plan_version, first.sources) == (1, "email", 1, (0,))
    assert after["1"] == drafts["1"]


def test_plan_version_moves_only_for_the_slot_that_changed():
    """只改了第 2 份的写法：第 2 份 plan_version 2、正文过时（planned）；
    第 1 份不动，还在等他说（written）。
    """
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE)
    drafts = _ledger(drafts, **{"1": "written", "2": "replan"})

    fin, after = _revise(
        plan, drafts, _as(JP_EMAIL, 1), _as(ZACH_NOTE | {"form_user_requested": "linkedin_message"}, 2)
    )

    assert [(s.number, s.plan_version) for s in fin.plan.slots] == [(1, 1), (2, 2)]
    assert (after["1"]["status"], after["2"]["status"]) == ("written", "planned")


def test_a_language_change_moves_every_slot_but_the_approved_one():
    """「都改成中文」：语言是整轮的，没定稿的每份 plan_version 都 +1，定稿的不动。"""
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE, COMPANY_EMAIL)
    drafts = _ledger(drafts, **{"1": "approved", "2": "written", "3": "written"})

    fin, after = _revise(
        plan,
        drafts,
        _as(JP_EMAIL, 1),
        _as(ZACH_NOTE, 2),
        _as(COMPANY_EMAIL, 3),
        language_user_requested="中文",
    )

    assert [s.plan_version for s in fin.plan.slots] == [1, 2, 2]
    assert [after[k]["status"] for k in ("1", "2", "3")] == ["approved", "planned", "planned"]


def test_the_limit_counts_live_drafts_including_approved_ones():
    """上限数活着的份：#1 定稿 + #3 #4 = 3 份，再加两份只进得去一份；不要了的 #2 不算。"""
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE, COMPANY_EMAIL, SMALL_BATCH_ARTICLE)
    drafts = _ledger(drafts, **{"1": "approved", "2": "dropped", "3": "written", "4": "written"})
    zach_message = ZACH_NOTE | {"user_words": "再给 Zach 发条私信", "form_user_requested": "linkedin_message"}
    singalong = _ask(
        user_words="再给 4 号写条附言", recipient_ai_understood=4, form_user_requested="linkedin_note"
    )
    plan["slots"] = [s for s in plan["slots"] if s["number"] != 2]

    fin, _after = _revise(
        plan,
        drafts,
        _as(JP_EMAIL, 1),
        _as(COMPANY_EMAIL, 3),
        _as(SMALL_BATCH_ARTICLE, 4),
        zach_message,
        singalong,
    )

    assert [s.number for s in fin.plan.slots] == [1, 3, 4, 5]
    assert fin.plan.slots[-1].form == "linkedin_message"
    assert "一轮最多 4 份（已定稿的也算），新加的 1 份没加上；要加可以先不要一份" in fin.plan.notes


def test_a_new_draft_same_as_an_old_one_merges_into_the_old_number():
    """新加的跟第 1 份一样（同一个人、同一种、同一个地址）：并进第 1 份，不另发号。"""
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE)
    again = JP_EMAIL | {"user_words": "3 号那封再提一下固件", "emphasis_user_requested": ["固件"]}

    fin, after = _revise(plan, drafts, _as(JP_EMAIL, 1), _as(ZACH_NOTE, 2), again)

    assert [s.number for s in fin.plan.slots] == [1, 2]
    assert fin.plan.slots[0].emphasis == ("MOQ 500", "固件")
    assert fin.plan.slots[0].plan_version == 2
    assert set(after) == {"1", "2"}


def test_two_old_drafts_made_the_same_drop_the_later_number():
    """他把第 2 份改成跟第 1 份一样：并进第 1 份，第 2 份记 dropped，说明里写一句。"""
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE)
    same_as_first = JP_EMAIL | {"user_words": "再给 Zach 写一条加好友附言"}

    fin, after = _revise(plan, drafts, _as(JP_EMAIL, 1), _as(same_as_first, 2))

    assert [s.number for s in fin.plan.slots] == [1]
    assert fin.dropped == (2,)
    assert after["2"]["status"] == "dropped"
    assert "第 2 份跟第 1 份都是给 Marc-Andre Lavoie 的邮件，合成了一份" in fin.plan.notes


def test_nothing_said_stays_nothing_said_while_revising():
    """第一句「写吧」，AI 定了两份；看计划时只说「换个卖点」（哪份都没有原话）：
    开头照旧是「你没说要写什么」。
    """
    plan, drafts = _first_round(_added(form_ai_planned="email"), _added())

    fin, _after = _revise(plan, drafts, _as(_added(form_ai_planned="email"), 1), _as(_added(), 2))

    assert fin.plan.ai_plans_all is True
    assert [s.number for s in fin.plan.slots] == [1, 2]


def test_the_ledger_after_a_new_plan():
    """账本怎么跟着计划变：正文过时的改 planned，计划没变的 rewrite 不动，
    要改计划却没改的看有没有正文意见。
    """
    base = _finalize(JP_EMAIL).slots[0]
    slots = (
        replace(base, number=1, plan_version=2),  # written，计划变了
        replace(base, number=2, plan_version=1),  # rewrite，计划没变
        replace(base, number=3, plan_version=1),  # replan 带正文意见，计划却没变
        replace(base, number=4, plan_version=1),  # replan 没有正文意见，计划却没变
        replace(base, number=5, plan_version=1),  # approved
    )
    plan = replace(_finalize(JP_EMAIL), slots=slots)
    drafts = _ledger(
        {str(n): new_entry() for n in range(1, 6)},
        **{"1": "written", "2": "rewrite", "3": "replan", "4": "replan", "5": "approved"},
    )
    drafts["2"]["feedback"] = "短一点"
    drafts["3"]["feedback"] = "语气随意点"

    after = drafts_after_plan(drafts, Finalized(plan=plan, dropped=()))

    assert [after[k]["status"] for k in ("1", "2", "3", "4", "5")] == [
        "planned", "rewrite", "rewrite", "planned", "approved",
    ]  # fmt: skip
    assert after["2"]["feedback"] == "短一点"
    # 传进来的那份没被改
    assert drafts["1"]["status"] == "written"


def test_the_revision_block_marks_each_slot_by_its_status():
    """改计划那一段：已定稿、只改正文、要改计划的那几份标题后面各带一句；不要了的单独一行。"""
    previous = [
        JP_EMAIL | {"plan_number": 1},
        ZACH_NOTE | {"plan_number": 2},
        COMPANY_EMAIL | {"plan_number": 3},
    ]
    drafts = _ledger(
        {str(n): new_entry() for n in range(1, 5)},
        **{"1": "approved", "2": "rewrite", "3": "replan", "4": "dropped"},
    )
    drafts["4"]["slot"] = plan_to_state(_finalize(SMALL_BATCH_ARTICLE))["slots"][0]

    text = _revision_text("第 3 份：改成私信", previous, drafts, {"slots": []})

    assert "他看到的第 1 份（已定稿，不能改，原样交回），就是你上一版的 [0]：" in text
    assert "他看到的第 2 份（他只要改正文，计划原样交回），就是你上一版的 [1]：" in text
    assert "他看到的第 3 份（他要改这一份的计划），就是你上一版的 [2]：" in text
    assert (
        "已经不要的：第 4 份（文章 → 这家公司本身（1 号）；要强调 小批量；卖点 moq；开场不引帖子）。"
        "他没说要回来就不要交这几个号；他要哪一份回来，就交一项、plan_number 填那个号，"
        "照它原来的计划填，再改上他说要改的"
    ) in text
    assert "接着上面第 N 份的就填 N（那一份是几项合成的，几项都填 N），新加的填 0。" in text


def test_parse_only_takes_numbers_from_the_last_plan():
    """plan_number 只能是 0 或上一版计划里还在的号；第一次定计划填了号当作 0。"""
    lines = LINES + ["用户: 第 2 份不要了"]
    payload = _ai_plan(JP_EMAIL | {"plan_number": 2})

    with pytest.raises(AiPlanCallFailed, match="上一版计划里只有 \\[1\\]"):
        parse_ai_plan(payload, lines, NEW_TOWN, KNOWLEDGE, revising=True, numbers={1})
    assert (
        parse_ai_plan(payload, lines, NEW_TOWN, KNOWLEDGE, revising=True, numbers={1, 2})
        .drafts[0]
        .plan_number
        == 2
    )
    assert parse_ai_plan(payload, lines, NEW_TOWN, KNOWLEDGE, revising=False).drafts[0].plan_number == 0
    with pytest.raises(AiPlanCallFailed, match="plan_number 返回了 -1"):
        parse_ai_plan(_ai_plan(JP_EMAIL | {"plan_number": -1}), lines, NEW_TOWN, KNOWLEDGE, revising=True)


def test_read_plan_takes_the_numbers_from_the_previous_plan():
    """read_ai_plan 自己从上一版算出能填的号交给解析：上一版只有 1 号时，模型填 2 号就报错。"""
    previous = [JP_EMAIL | {"plan_number": 1}]
    answer = json.dumps(_ai_plan(JP_EMAIL | {"plan_number": 2}), ensure_ascii=False)

    with pytest.raises(AiPlanCallFailed, match="上一版计划里只有"):
        read_ai_plan(
            _FakeClient(answer), LINES, NEW_TOWN, KNOWLEDGE, feedback="第 1 份改成私信", previous=previous,
            drafts={"1": new_entry()}, plan={"slots": [{"number": 1}]}, config=CONFIG,
        )  # fmt: skip


def test_a_dropped_draft_comes_back_with_its_number_while_revising():
    """计划阶段：第 2 份不要了，之后他又说要回来，模型交回 plan_number 2：原号复活，照这一版计划从头写，
    plan_version 跟它不要时存的那版计划比。
    """
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE, COMPANY_EMAIL)
    fin, drafts = _revise(plan, drafts, _as(JP_EMAIL, 1), _as(COMPANY_EMAIL, 3))
    assert drafts["2"]["status"] == "dropped"

    back, after = _revise(
        plan_to_state(fin.plan), drafts, _as(JP_EMAIL, 1), _as(ZACH_NOTE, 2), _as(COMPANY_EMAIL, 3)
    )

    assert [(s.number, s.plan_version) for s in back.plan.slots] == [(1, 1), (2, 1), (3, 1)]
    assert after["2"]["status"] == "planned"


def test_a_slot_without_its_old_items_is_shown_by_its_plan():
    """上一版 drafts 里没有原样的那份（刚要回来的）：按计划写成一句给模型，让它照着填一项、填原号。"""
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE)
    previous = [JP_EMAIL | {"plan_number": 1}]
    plan["slots"][1]["sources"] = []

    text = _revision_text("第 1 份改成私信", previous, drafts, plan)

    assert (
        "他看到的第 2 份，你上一版的 drafts 里没有它的原样，它现在的计划是："
        "LinkedIn 加好友附言 → Zach T.；卖点 categories；开场不引帖子。"
        "交回时照这个计划填一项，plan_number 填 2"
    ) in text


def test_read_plan_lets_a_dropped_number_come_back():
    """能填的号：上一版还活着的，加上不要了、账本里存着计划的（可以要回来）。"""
    plan, drafts = _first_round(JP_EMAIL, ZACH_NOTE)
    fin, drafts = _revise(plan, drafts, _as(JP_EMAIL, 1))
    previous = [JP_EMAIL | {"plan_number": 1}]
    answer = json.dumps(
        _ai_plan(JP_EMAIL | {"plan_number": 1}, ZACH_NOTE | {"plan_number": 2}), ensure_ascii=False
    )

    got = read_ai_plan(
        _FakeClient(answer), LINES + ["用户: 再给 Zach 写一条加好友附言"], NEW_TOWN, KNOWLEDGE,
        feedback="第 2 份还是要", previous=previous, drafts=drafts, plan=plan_to_state(fin.plan),
        config=CONFIG,
    )  # fmt: skip

    assert [d.plan_number for d in got.drafts] == [1, 2]
