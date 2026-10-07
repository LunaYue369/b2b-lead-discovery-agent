"""write_draft 那一步的测试：代码检查挡不挡得住、不过关会不会带着问题重写、改正文时给没给上一版。

不调模型：用照剧本回答的假 client，一次调用回一份 {"subject", "body"}。

焊住的几类：

    数字是编的            稿子里写了一个 facts.json 和证据里都没有的数（交期、产量）
    假装偶遇              证据是第三方说的，稿子却写「saw your post」
    附言超长              加好友附言超过 300 字符，LinkedIn 发不出去
    称呼错人              写给 Dana 的信叫了 Sam；写给公司的信叫了名单上某个人
    计划变了还给旧稿      照新计划从头写时把上一版塞给模型，新稿被带回旧的收件人、旧的写法
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from triggers.interactive_discovery.drafting.planner import Plan, PlanSlot
from triggers.interactive_discovery.drafting.writer import (
    WriteConfig,
    build_write_messages,
    check_draft,
    compose_text,
    load_write_config,
    write_one,
)

DANA = {"name": "Dana Lee", "relation": "member", "position": "Founder", "profile_url": "https://x/dana"}
SAM = {"name": "Sam Park", "relation": "not_member", "position": None, "profile_url": "https://x/sam"}
LEAD = {
    "display_name": "Acme Audio",
    "emails": [{"value": "hello@acme-audio.example", "source": "website", "person_name": None,
                "email_status": None, "catch_all": None, "quality_score": None}],
    "people": [DANA, SAM],
}  # fmt: skip
KNOWLEDGE = {
    "profile": {"website": "https://acme-audio.example", "founded_year": 2023},
    "assessment": {"score": 72,
                   "analysis": "Acme Audio just finished a Kickstarter and needs 800 units built.",
                   "quotes_mean": "", "quotes": []},
    "evidence": [
        {"signal_id": 2720, "lead_type": "author", "first_person": True, "author": "Dana Lee",
         "role": "Founder @ Acme Audio", "when": "2026-09-20", "url": "https://x/1",
         "text": "We just wrapped our Kickstarter and are looking for a factory.",
         "reposted_author": None, "reposted_text": None},
        {"signal_id": 2741, "lead_type": "mentioned", "first_person": False, "author": "Sam Park",
         "role": "Creator gear reviewer", "when": "2026-09-18", "url": "https://x/2",
         "text": "Tried the Acme Audio clip-on this week.", "reposted_author": None, "reposted_text": None},
    ],
    "facts": {
        "moq": {"value": 500, "unit": "units", "why_it_matters": ["Most ODMs start at 1000-5000 units."]},
        "services": {"claim": "ODM / OEM / branding", "source": "acme/main/odm.html"},
    },
    "avoid": [{"key": "k", "scope": "draft", "statement": "第三方的消息不要装作偶然看到", "source": "s"}],
}  # fmt: skip
PLAN = Plan(slots=(), language="英文", tone="简短、专业", unsupported=(), notes=(), ai_plans_all=False)
CONFIG = WriteConfig(model="gpt-4.1", max_tokens=1500, temperature=0.3, request_timeout_seconds=60.0,
                     max_retries=2, note_limit=300)  # fmt: skip


def _slot(form="email", recipient=DANA, opening=2720, **rest) -> PlanSlot:
    return PlanSlot(
        number=rest.get("number", 1),
        plan_version=rest.get("plan_version", 1),
        user_words="给 Dana 写封邮件",
        recipient=recipient,
        recipient_user_requested="",
        email_user_requested="",
        form=form,
        address="dana@acme-audio.example" if form == "email" else None,
        emphasis=tuple(rest.get("emphasis", ("MOQ 500",))),
        selling_points=("moq",),
        opening=opening,
        reason="",
        ai_decides=(),
        notes=(),
        sources=(0,),
    )


def _check(slot, body, subject="Building your mic"):
    return check_draft(slot, LEAD, KNOWLEDGE, subject, body, note_limit=300)


GOOD = (
    "Hi Dana,\n\nCongrats on wrapping the Kickstarter. We build audio devices from 500 units.\n\n"
    "Best regards,"
)


def test_a_good_email_passes():
    assert _check(_slot(), GOOD) == []


def test_a_number_without_a_source_is_caught():
    """「ship in 30 days」：30 在 facts.json、证据、公司情况里都没有。800（证据里有）、500（facts 里有）、
    1,000 写法的逗号不算数。"""
    body = GOOD + "\nWe can ship 800 units in 30 days."

    assert _check(_slot(), body) == ["数字 30 在 facts.json、证据和公司情况里都找不到，不许写"]


def test_a_third_party_opening_cannot_pretend_to_be_theirs():
    """开场引的是 Sam 说的，写给 Dana 却说「saw your post」：挡。引 Dana 自己的帖子就可以这么说。"""
    body = "Hi Dana,\n\nI saw your post about the clip-on mic. We build from 500 units."

    (problem,) = _check(_slot(opening=2741), body)
    assert problem.startswith("开场那条证据不是收件人本人说的（是 Sam Park 说的），不许写「your post」")
    assert _check(_slot(opening=2720), body) == []


def test_a_note_over_the_limit_is_caught():
    body = "Hi Dana, " + "x" * 300

    assert _check(_slot(form="linkedin_note"), body, subject="") == [
        "加好友附言 309 个字符，超过 300，要压短"
    ]


def test_the_greeting_must_name_the_recipient():
    """写给 Dana 的第一行要有 Dana；写给公司的第一行不许出现名单上的人名；文章不查称呼。"""
    assert _check(_slot(), "Hi Sam,\n\nWe build from 500 units.") == [
        "第一行的称呼要写给 Dana Lee（用名 dana）"
    ]
    company = _slot(recipient=None, opening=0)
    assert _check(company, "Hi Acme Audio team,\n\nWe build from 500 units.") == []
    assert _check(company, "Hi Dana,\n\nWe build from 500 units.") == [
        "这份写给 Acme Audio 的团队，第一行的称呼里不该出现 Dana"
    ]
    assert (
        _check(_slot(form="article", recipient=None, opening=0), "Small batches matter.", subject="Why") == []
    )


def test_empty_body_and_missing_subject():
    assert _check(_slot(), "  ") == ["正文是空的"]
    assert _check(_slot(), GOOD, subject="") == ["邮件要有标题（subject）"]


def test_compose_text_per_form():
    assert compose_text("email", "Hi", "Body") == "Subject: Hi\n\nBody"
    assert compose_text("article", "Title", "Body") == "Title\n\nBody"
    assert compose_text("linkedin_note", "", "Body") == "Body"


class _Client:
    """照剧本回答：每调一次回下一份 (subject, body)，记下每次收到的 messages。"""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        subject, body = self._answers.pop(0)
        message = SimpleNamespace(content=json.dumps({"subject": subject, "body": body}), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=message)])


PLANNED = {"status": "planned", "version": 0, "plan_version": 0, "text": "", "feedback": "", "problems": []}


def test_write_one_rewrites_until_the_checks_pass():
    """第一次写了编的数字：带着那条问题重写，第二次过了。版本 +1、记下照的计划版本、意见清空。"""
    client = _Client(("Hi", GOOD + " Lead time 30 days."), ("Hi", GOOD))

    entry = write_one(client, _slot(plan_version=3), PLAN, LEAD, KNOWLEDGE, PLANNED, CONFIG)

    assert entry == {"status": "written", "version": 1, "plan_version": 3, "text": f"Subject: Hi\n\n{GOOD}",
                     "feedback": "", "problems": []}  # fmt: skip
    assert len(client.calls) == 2
    assert "数字 30" in client.calls[1][-1]["content"]


def test_write_one_gives_up_after_the_retries_and_keeps_the_problems():
    """重写了 max_retries 次还是不过：照样交出去，最后一次的问题挂在 problems 上，发稿时提醒用户。"""
    bad = ("Hi", "Hi Sam,\n\nWe build from 500 units.")
    client = _Client(bad, bad, bad)

    entry = write_one(client, _slot(), PLAN, LEAD, KNOWLEDGE, PLANNED, CONFIG)

    assert len(client.calls) == 3
    assert entry["problems"] == ["第一行的称呼要写给 Dana Lee（用名 dana）"]
    assert entry["status"] == "written"


def test_a_rewrite_gets_the_old_text_and_a_replanned_one_does_not():
    """rewrite：上一版 + 意见给模型，在原稿上改。planned（计划变了）：不给上一版，有正文意见照样带上。"""
    old = {"status": "rewrite", "version": 1, "plan_version": 1, "text": "Subject: Old\n\nHi Dana, old text",
           "feedback": "短一点", "problems": []}  # fmt: skip
    rewrite = build_write_messages(
        _slot(), PLAN, LEAD, KNOWLEDGE, previous_text=old["text"], feedback="短一点", problems=()
    )
    replanned = build_write_messages(
        _slot(), PLAN, LEAD, KNOWLEDGE, previous_text="", feedback="语气随意点", problems=()
    )

    assert (
        rewrite[-1]["content"]
        == "上一版：\nSubject: Old\n\nHi Dana, old text\n\n他的意见：短一点\n\n在上一版上改，只改他说的。"
    )
    assert all("old text" not in m["content"] for m in replanned)
    assert replanned[-1]["content"] == "他对这一份的意见（写的时候照着做）：语气随意点"

    client = _Client(("Hi", GOOD))
    write_one(client, _slot(), PLAN, LEAD, KNOWLEDGE, old, CONFIG)
    assert "old text" in client.calls[0][-1]["content"]
    client = _Client(("Hi", GOOD))
    write_one(client, _slot(), PLAN, LEAD, KNOWLEDGE, old | {"status": "planned"}, CONFIG)
    assert all("old text" not in m["content"] for m in client.calls[0])


def test_the_brief_says_whose_words_the_opening_is():
    """开场那条证据：Dana 本人说的标「可以说看到了他的帖子」；Sam 说的标「不许装作看到」。只给挑中的卖点。"""
    own = build_write_messages(_slot(), PLAN, LEAD, KNOWLEDGE, previous_text="", feedback="", problems=())[1]
    other = build_write_messages(
        _slot(opening=2741), PLAN, LEAD, KNOWLEDGE, previous_text="", feedback="", problems=()
    )[1]

    assert "开场引这条证据（收件人本人说的，可以说看到了他的帖子）" in own["content"]
    assert (
        "开场引这条证据（不是收件人本人说的，要说清消息从哪来，不许装作看到了收件人的帖子）"
        in other["content"]
    )
    assert "要强调（他说的，一定要写进去）：MOQ 500" in own["content"]
    assert '- 起订量低（MOQ）：{"value": 500, "unit": "units"}' in own["content"]
    assert "ODM / OEM" not in own["content"]


def test_the_repo_config_loads():
    config = load_write_config()
    assert config.note_limit == 300 and config.max_retries == 2


def test_numbers_in_names_and_addresses_are_not_made_up():
    """公司名、人名、职位、收件地址里的数字有出处：称呼写「Hi 3M team」不算编数字。"""
    lead = LEAD | {"display_name": "3M Audio"}
    company = _slot(recipient=None, opening=0)

    assert (
        check_draft(
            company, lead, KNOWLEDGE, "Hi", "Hi 3M Audio team,\n\nWe build from 500 units.", note_limit=300
        )
        == []
    )


def test_writing_to_the_author_of_a_third_party_post_can_say_your_post():
    """写给 Sam 本人、开场引的是 Sam 自己发的帖子：他不是这家的人（first_person=False），
    但「saw your post」是真话，不拦；同一条帖子写给 Dana 就拦。
    """
    body = "Hi Sam,\n\nI saw your post about the Acme Audio clip-on. We build from 500 units."
    to_sam = _slot(recipient=SAM, opening=2741, form="linkedin_message")

    assert _check(to_sam, body, subject="") == []
    brief = build_write_messages(to_sam, PLAN, LEAD, KNOWLEDGE, previous_text="", feedback="", problems=())[1]
    assert "开场引这条证据（收件人本人说的，可以说看到了他的帖子）" in brief["content"]


def test_internal_reasons_in_facts_are_neither_shown_nor_a_source():
    """why_it_matters 是内部理由：不给写稿模型看，里面的数字（1000-5000）也不算出处，写出来就挡。"""
    brief = build_write_messages(_slot(), PLAN, LEAD, KNOWLEDGE, previous_text="", feedback="", problems=())[
        1
    ]

    assert "why_it_matters" not in brief["content"] and "1000-5000" not in brief["content"]
    assert _check(_slot(), GOOD + " Most ODMs start at 1000-5000 units.") == [
        "数字 1000、5000 在 facts.json、证据和公司情况里都找不到，不许写"
    ]


def test_a_draft_to_an_outsider_asks_for_introductions():
    """写给外人（Sam 发帖提到了 Acme Audio，不是这家的人）：告诉模型这一份的目的是引荐——
    先引荐给这家负责产品或采购的人，再问认不认识别的要找代工厂的团队。写给这家的人不加这一句。
    """
    to_sam = build_write_messages(
        _slot(recipient=SAM, opening=2741, form="linkedin_message"), PLAN, LEAD, KNOWLEDGE,
        previous_text="", feedback="", problems=(),
    )[1]["content"]  # fmt: skip
    to_dana = build_write_messages(
        _slot(), PLAN, LEAD, KNOWLEDGE, previous_text="", feedback="", problems=()
    )[1]["content"]

    assert "这一份的目的：他不是 Acme Audio 的人，是发帖提到了这家。不要把他当买家推销。" in to_sam
    assert "引荐给 Acme Audio 负责产品或采购的人" in to_sam
    assert "认不认识别的正在找音频或语音设备代工厂的品牌、团队" in to_sam
    assert "这一份的目的" not in to_dana
