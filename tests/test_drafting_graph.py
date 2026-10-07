"""写稿那张图的测试：开对话 -> 读这家公司 -> 停在第一问 -> 用户回话 -> 定计划 -> 停下来给他看 -> 判他的回话。

跑的是真的 drafting/graph.build_graph，存档器换成 InMemorySaver。查库那两个函数换成桩，
桩里放的是库里这几家的真值；调模型的两步都换成照剧本回答的桩：定计划（read_ai_plan，script 那个 fixture）、
判回话（read_decision：「就这样」判同意，「算了」判不写了，其余一律判要改）：

    1289 North Field Technologies   两个这家的邮箱（官网一个，Marc-Andre Lavoie 一个）
    1281 Chatterly Inc.            一个邮箱都没有
    1518 Kestrel                 只有一个 Evan Park 在别家（Rawcut）的邮箱

焊住的几类：

    问的邮箱跟卡片上不一样     把别家的邮箱当成这家的，稿子会发到别的公司去
    编号对不上                 第一问的名单编号跟给模型看的不是同一套（1 号是这家公司本身）
    问答没记全                 下一个节点读 messages 时，「2 号」两个字脱离了问题
    第一份 state 缺字段        节点里读那个字段 KeyError
    停下来追问                 用户什么都不想说、说了名单上没有的人，图也该直接定计划、停在计划上，不另外追问
    计划那一停没接好           要改没绕回去重定、同意或不写了没走到 END、
                               计划没进 messages（重定时看不到上一版）
    定计划看不到 knowledge     挑卖点和开场的那次调用没拿到证据和 facts.json
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import typing

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from delivery import conversation
from models import DraftEvidence, DraftSource, LeadType
from models.contact import (
    NO_CONTACTS,
    CardEmail,
    CardPerson,
    CardRelation,
    CompanyProfile,
    ContactSource,
    LeadForContacts,
)
from triggers.interactive_discovery import runner
from triggers.interactive_discovery.drafting import messages, nodes, revisions, slack_conversation
from triggers.interactive_discovery.drafting.feedback import DECISIONS as FEEDBACK_DECISIONS
from triggers.interactive_discovery.drafting.feedback import FeedbackJudgement, SlotVerdict
from triggers.interactive_discovery.drafting.graph import build_graph
from triggers.interactive_discovery.drafting.planner import WRITTEN, AiPlan, AiPlanDraft
from triggers.interactive_discovery.drafting.reply import DECISIONS, ReplyJudgement
from triggers.interactive_discovery.drafting.state import DraftingState, initial_state

NEW_TOWN = LeadForContacts(
    lead_id=1289,
    display_name="North Field Technologies",
    normalized_name="north field technologies",
    identity_kind="li_id",
    linkedin_company_id="100000001",
    linkedin_slug="northfield-inc",
    domain="northfield-inc.example",
)
CHATTERLY = LeadForContacts(
    lead_id=1281,
    display_name="Chatterly Inc.",
    normalized_name="chatterly",
    identity_kind="name",
    linkedin_company_id=None,
    linkedin_slug=None,
    domain=None,
)
KESTREL = LeadForContacts(
    lead_id=1518,
    display_name="Kestrel",
    normalized_name="brevitē",
    identity_kind="name",
    linkedin_company_id=None,
    linkedin_slug=None,
    domain=None,
)

# card_contacts 返回的 emails，最新的在前。只填 retrieve_knowledge 用到的那一格，其余照库里的样子
EMAILS = {
    1289: (
        CardEmail(
            value="contact@northfield-inc.example",
            source=ContactSource.WEBSITE,
            source_url="https://northfield-inc.example/",
            person_name=None,
            email_status=None,
            catch_all=None,
            found_in_google=None,
            quality_score=None,
            other_company=None,
        ),
        CardEmail(
            value="marcandre@northfield-inc.example",
            source=ContactSource.LINKEDIN_EMAIL_SEARCH,
            source_url="https://www.linkedin.com/in/malavoie",
            person_name="Marc-Andre Lavoie",
            email_status="risky",
            catch_all=True,
            found_in_google=None,
            quality_score=60,
            other_company=None,
        ),
    ),
    1281: (),
    1518: (
        CardEmail(
            value="evan.park@rawcutstudio.example",
            source=ContactSource.LINKEDIN_EMAIL_SEARCH,
            source_url=None,
            person_name="Evan Park",
            email_status="risky",
            catch_all=None,
            found_in_google=None,
            quality_score=None,
            other_company="Rawcut",
        ),
    ),
}


# card_contacts 返回的 people，卡片「联系人」那一区的顺序。post_url 用不到，留 None
PEOPLE = {
    1289: (
        CardPerson(
            "Zach T.", CardRelation.NOT_MEMBER, None, None, "https://www.linkedin.com/in/zachtrent", None
        ),
        CardPerson(
            "Marc-Andre Lavoie",
            CardRelation.MEMBER,
            "Founder & CEO",
            None,
            "https://www.linkedin.com/in/malavoie",
            None,
        ),
        CardPerson(
            "SingAlong Group",
            CardRelation.OTHER_PAGE,
            None,
            None,
            "https://www.linkedin.com/company/singalong-group/",
            None,
        ),
    ),
    1281: (
        CardPerson(
            "Ravi Shah", CardRelation.NOT_LOOKED_UP, None, None, "https://www.linkedin.com/in/rshah", None
        ),
    ),
    1518: (
        CardPerson(
            "Evan Park",
            CardRelation.MEMBER,
            "Co-Founder",
            None,
            "https://www.linkedin.com/in/evanpark",
            None,
        ),
    ),
}

# latest_profiles 返回的公司概况：只有 1289 抓过（编的值），另外两家只有名字、没抓过
PROFILES = {
    1289: CompanyProfile(
        website="https://northfield-inc.example",
        employee_count=6,
        employee_count_range="2-10",
        founded_year=2021,
        location="Montreal, CA",
        industries=("Consumer Electronics",),
        phone=None,
        raw={
            "tagline": "Karaoke at home, done right",
            "description": "North Field builds a karaoke microphone that pairs with any TV.",
            "specialities": ["karaoke", "wireless microphones"],
            "employeeCount": 6,
        },
    ),
}

# lead_db.draft_source 返回的打分结论和证据（编的值）。1289 两条：一条第三方 @ 了它，一条本人发的
SOURCES = {
    1289: DraftSource(
        score=72,
        analysis="North Field is building a karaoke mic and asked for a partner to make 1,000 units.",
        quotes_mean="创始人说在找能做小批量的工厂",
        quotes=("we need a partner for 1,000 units",),
        evidence=(
            DraftEvidence(
                signal_id=2733,
                lead_type=LeadType.MENTIONED,
                author="Zach T.",
                role="Producer",
                when="2026-09-21",
                url="https://www.linkedin.com/posts/zachtrent_1",
                text="Had fun testing the @North Field Technologies mic last night.",
                reposted_author=None,
                reposted_text=None,
            ),
            DraftEvidence(
                signal_id=2720,
                lead_type=LeadType.AUTHOR,
                author="Marc-Andre Lavoie",
                role="Founder & CEO @ North Field Technologies",
                when="2026-09-20",
                url="https://www.linkedin.com/posts/malavoie_1",
                text="Our karaoke mic is ready; we need a partner for 1,000 units.",
                reposted_author=None,
                reposted_text=None,
            ),
        ),
    ),
    1281: DraftSource(score=67, analysis="Chatterly wrapped a Kickstarter.", quotes_mean="众筹刚结束",
                      quotes=(), evidence=()),
    1518: DraftSource(score=61, analysis="Kestrel is prototyping.", quotes_mean="在做原型",
                      quotes=(), evidence=()),
}  # fmt: skip


def _ask(user_words: str, pick: int = 0, form: str = "", **rest) -> AiPlanDraft:
    """剧本里模型给出的一份稿。用户那一半没给的是空的（没说）；AI 那一半默认挑了 moq、开场不引帖子，
    用户没说写法时 AI 定成邮件。

    例：_ask("给 3 号写封邮件", 3, "email") -> 写给名单上 3 号（Marc-Andre）的一封邮件
        （1 号是这家公司本身，人从 2 号起）
        _ask("", form_ai="linkedin_note") -> AI 补的一份附言
    """
    return AiPlanDraft(
        plan_number=rest.get("plan_number", 0),
        user_words=user_words,
        recipient_user_requested=rest.get("said_to", ""),
        recipient_ai_understood=pick,
        email_user_requested=rest.get("email", ""),
        email_ai_understood=rest.get("email_pick", ""),
        form_user_requested=form,
        emphasis_user_requested=tuple(rest.get("emphasis", ())),
        form_ai_planned=rest.get("form_ai", "" if form else "email"),
        selling_points_ai_planned=tuple(rest.get("selling_points", ("moq",))),
        opening_ai_planned=rest.get("opening", 0),
        reason_ai_planned=rest.get("reason", ""),
    )


def _said(*drafts: AiPlanDraft, count: int = 0, count_words: str = "") -> AiPlan:
    """剧本里模型给出的一整份：几份稿，加上份数。语言语气都没说。"""
    return AiPlan(
        count_user_requested=count,
        count_user_words=count_words,
        drafts=drafts,
        language_user_requested="",
        tone_user_requested="",
        unsupported=(),
    )


# 用户什么都没说清的时候（「写吧」「看你」）模型给出的那一份：用户那一半全空，AI 补了一份写给公司的邮件
NOTHING_SAID = _said(_ask(""))


@pytest.fixture
def script():
    """调模型那一步的剧本：用户最后一句（去掉「用户: 」）-> 模型给出的 AiPlan。

    输出：一个 dict，测试往里填；剧本里没有的那一句按 NOTHING_SAID 回答（什么都没说清）。
    另有三个键："_reads" 记着每次被调时的那段 messages，测试拿它数调了几次模型；
    "_knowledge" 记着每次被调时交进去的 knowledge；"_judged" 记着判回话那一步每次判的是哪一句；
    "_revisions" 记着每次被调时交进去的 (feedback, previous)；"_ledgers" 记着每次定计划时交进去的账本；
    "_written" 记着写稿桩每次写的 (第几份, 写之前的状态, 意见, 上一版正文)；
    "_feedback" 是看稿回话的剧本（那句话 -> FeedbackJudgement，剧本里没有的当成什么都没判）；
    "_rows" 记着 record_round 写进 draft_revisions 的每一行
    """
    return {
        "_reads": [],
        "_knowledge": [],
        "_judged": [],
        "_revisions": [],
        "_ledgers": [],
        "_written": [],
        "_feedback": {},
        "_rows": [],
    }


@pytest.fixture
def app(monkeypatch, script):
    """一张真的写稿图（内存存档器），查库那两个函数、调模型那一步都换成桩。

    输出：编译好的图。每个测试一张，存档互不相干
    """

    class FakeConn:
        # 桩函数不碰游标，这里只要让 `with db.connect() as conn, conn.cursor() as cur` 走得通
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return contextlib.nullcontext(None)

    leads = {lead.lead_id: lead for lead in (NEW_TOWN, CHATTERLY, KESTREL)}

    def fake_contact_inputs(cur, lead_ids):
        # 真函数的规矩：库里没有的 id 不在输出里
        return {i: (leads[i], []) for i in lead_ids if i in leads}

    def fake_card_contacts(cur, lead_ids):
        # 真函数的规矩：传进来的每一家都有，库里没有的是 NO_CONTACTS
        return {
            i: dataclasses.replace(NO_CONTACTS, emails=EMAILS.get(i, ()), people=PEOPLE.get(i, ()))
            for i in lead_ids
        }

    def fake_read_plan(client, lines, lead, knowledge, *, feedback, previous, drafts, plan, config):
        # 按用户最后一句查剧本；剧本里没有就当什么都没说清
        script["_reads"].append(list(lines))
        script["_knowledge"].append(knowledge)
        script["_revisions"].append((feedback, [dict(d) for d in previous]))
        script["_ledgers"].append({k: v["status"] for k, v in drafts.items()})
        return script.get(lines[-1].removeprefix("用户: "), NOTHING_SAID)

    monkeypatch.setattr(nodes.db, "connect", lambda *a, **k: FakeConn())
    monkeypatch.setattr(nodes.contacts_db, "contact_inputs", fake_contact_inputs)
    # 真函数的规矩：没抓过概况的那家不在输出里；leads 里没有这一行，draft_source 返回 None
    monkeypatch.setattr(
        nodes.contacts_db, "latest_profiles", lambda cur, ids: {i: PROFILES[i] for i in ids if i in PROFILES}
    )
    monkeypatch.setattr(nodes.lead_db, "draft_source", lambda cur, lead_id: SOURCES.get(lead_id))
    # card_contacts 是 from 引进 nodes 的，要换 nodes 上的这个名字
    monkeypatch.setattr(nodes, "card_contacts", fake_card_contacts)

    def fake_read_decision(client, lines, *, feedback, config):
        # 判回话的剧本：「就这样」同意，「算了」不写了，其余都是要改
        script["_judged"].append(feedback)
        decision = {"就这样": "approve", "算了": "cancel"}.get(feedback, "revise")
        return ReplyJudgement(decision=decision, reason="测试剧本")

    # read_ai_plan / read_decision / build_llm_client 也是 from 引进 nodes 的，同样换 nodes 上的名字
    monkeypatch.setattr(nodes, "read_ai_plan", fake_read_plan)
    monkeypatch.setattr(nodes, "read_decision", fake_read_decision)
    monkeypatch.setattr(nodes, "build_llm_client", lambda: None)

    def fake_write_one(client, slot, plan, lead, knowledge, entry, config):
        # 写稿的桩：正文写明第几份、第几版、按的什么意见，测试据此看出哪份被重写了、照什么写的
        script["_written"].append((slot.number, entry["status"], entry["feedback"], entry["text"]))
        version = entry["version"] + 1
        text = f"第 {slot.number} 份第 {version} 版（{slot.form}）" + (
            f"按意见改：{entry['feedback']}" if entry["feedback"] else ""
        )
        return {**entry, "status": WRITTEN, "version": version, "plan_version": slot.plan_version,
                "text": text, "feedback": "", "problems": []}  # fmt: skip

    def fake_read_feedback(client, lines, *, feedback, drafts, plan, lead, config):
        return script["_feedback"].get(feedback, _judged())

    def fake_insert(cur, row):
        # 唯一键同库里那张表：(thread_id, reply_index, slot_number)，撞了不写
        key = (row.thread_id, row.reply_index, row.slot_number)
        if any((r.thread_id, r.reply_index, r.slot_number) == key for r in script["_rows"]):
            return False
        script["_rows"].append(row)
        return True

    monkeypatch.setattr(nodes, "write_one", fake_write_one)
    monkeypatch.setattr(nodes, "read_feedback", fake_read_feedback)
    monkeypatch.setattr(nodes.draft_db, "insert_revision", fake_insert)
    return build_graph(InMemorySaver())


def _judged(*slots: SlotVerdict, add=(), cancel=False, round_plan="", round_content="") -> FeedbackJudgement:
    """看稿回话的剧本里一份判决。例：_judged(SlotVerdict(1, "approve", "", ""), add=("再加一封",))"""
    return FeedbackJudgement(
        cancel=cancel,
        slots=slots,
        add=tuple(add),
        round_plan_change=round_plan,
        round_content_change=round_content,
        reason="测试剧本",
    )


def _config(lead_id: int) -> dict:
    return {"configurable": {"thread_id": f"draft:C0000000001:lead{lead_id}"}}


def test_initial_state_covers_every_field():
    """第一份 state 的键跟 DraftingState 的字段表一模一样，一个不多一个不少。"""
    assert set(initial_state(1289, "")) == set(typing.get_type_hints(DraftingState))


def test_initial_state_for_a_mention_keeps_the_line_twice():
    """@bot 开的：那句话既进 messages（对话记录），也进 request（下一个节点读的就是它）；按钮开的两处都空。"""
    assert initial_state(1289, "帮我写封邮件给他们") == {
        "messages": ["用户: 帮我写封邮件给他们"],
        "lead_id": 1289,
        "lead": {},
        "knowledge": {},
        "request": "帮我写封邮件给他们",
        "round_start": 0,
        "ai_plan": {},
        "plan": {},
        "plan_feedback": "",
        "decision": "",
        "drafts": {},
        "draft_feedback": "",
        "written_now": [],
        "verdicts": [],
        "notice": "",
    }
    assert initial_state(1289, "") == {
        "messages": [],
        "lead_id": 1289,
        "lead": {},
        "knowledge": {},
        "request": "",
        "round_start": 0,
        "ai_plan": {},
        "plan": {},
        "plan_feedback": "",
        "decision": "",
        "drafts": {},
        "draft_feedback": "",
        "written_now": [],
        "verdicts": [],
        "notice": "",
    }


def test_a_given_request_skips_the_first_question(app, script):
    """@bot 开的对话已经说了要写什么：读完这家直接去定计划，不停在 ask_request 上问第一句，停在计划上。"""
    # 「给他们」读成 1 号这家公司：发官网邮箱，不用再问
    script["帮我写封邮件给他们"] = _said(_ask("帮我写封邮件给他们", 1, "email"))

    result = app.invoke(initial_state(1289, "帮我写封邮件给他们"), _config(1289))

    assert result["__interrupt__"][0].value["kind"] == "drafting_plan"
    assert result["lead"]["display_name"] == "North Field Technologies"
    assert result["messages"] == ["用户: 帮我写封邮件给他们"]
    assert result["ai_plan"]["drafts"][0]["form_user_requested"] == "email"
    # 计划当场用代码算好存下：「给他们」是 1 号这家公司，邮件发官网那个
    (slot,) = result["plan"]["slots"]
    assert (slot["recipient"], slot["form"], slot["address"]) == (None, "email", "contact@northfield-inc.example")
    assert script["_reads"] == [["用户: 帮我写封邮件给他们"]]
    assert app.get_state(_config(1289)).next == ("await_plan",)


def test_start_stops_at_the_first_question_with_this_companys_emails(app):
    """开对话：读完这家，停在第一问；1 号是这家公司、官网邮箱挂在它下面，人从 2 号起，
    Marc-Andre 的邮箱挂在他名下。
    """
    result = app.invoke(initial_state(1289, ""), _config(1289))

    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "drafting_request"
    # 整句逐字对：这句话原样发给用户，也原样进 messages
    assert payload["question"] == (
        "请问你想给 North Field Technologies 写哪些 outreach 信息？一次可以写几份（最多 4 份），"
        "每份说多少都行，没说的我来定。\n"
        "\n"
        "名单：\n"
        "1. North Field Technologies 这家公司本身（称呼他们的团队）\n"
        "   邮箱 1-1 contact@northfield-inc.example：官网上公开写着的，没有另外验证过\n"
        "2. Zach T.（发帖的人不是这家公司的，只是在帖子里提到了它，仍可联络）\n"
        "3. Marc-Andre Lavoie（Founder & CEO，这家公司的人）\n"
        "   邮箱 3-1 marcandre@northfield-inc.example：不确定能不能收到；"
        "对方邮箱什么地址都收，没法事先确认这个地址有人在用；质量分 60 / 100\n"
        "4. SingAlong Group（发帖的是别的机构，不是这家公司的，但仍可联络）\n"
        "\n"
        "可以写四种：邮件、LinkedIn 私信、LinkedIn 加好友附言"
        "（附言 300 字符以内；对方还不是好友时私信可能收不到，附言总能送到），"
        "还有文章（不写给具体的人，把我们的卖点套到这家身上写一篇）。\n"
        "\n"
        "你可以这样回复：「给 2 号写封邮件，强调 MOQ 500，再写一篇文章讲我们能做小批量」。"
        "要发到哪个邮箱，说它的号就行，例如「发到 1-1」。"
        "没说写给谁的，写给 1 号这家公司；也可以回「看你」，让我来定。"
    )

    snapshot = app.get_state(_config(1289))
    assert runner.conversation_status(snapshot) == conversation.WAITING
    assert snapshot.next == ("ask_request",)
    assert snapshot.values["lead"] == {
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
        "people": [
            {
                "name": "Zach T.",
                "relation": "not_member",
                "position": None,
                "profile_url": "https://www.linkedin.com/in/zachtrent",
            },
            {
                "name": "Marc-Andre Lavoie",
                "relation": "member",
                "position": "Founder & CEO",
                "profile_url": "https://www.linkedin.com/in/malavoie",
            },
            {
                "name": "SingAlong Group",
                "relation": "other_page",
                "position": None,
                "profile_url": "https://www.linkedin.com/company/singalong-group/",
            },
        ],
    }


def test_reply_is_recorded_with_its_question(app, script):
    """用户回话之后：问和答两句进 messages，原话进 request，定完计划，停在计划上等他看。"""
    script["给 3 号写封邮件，强调 MOQ 500"] = _said(
        _ask("给 3 号写封邮件，强调 MOQ 500", 3, "email", emphasis=("MOQ 500",))
    )
    first = app.invoke(initial_state(1289, ""), _config(1289))
    question = first["__interrupt__"][0].value["question"]

    result = app.invoke(Command(resume="给 3 号写封邮件，强调 MOQ 500"), _config(1289))

    assert result["__interrupt__"][0].value["kind"] == "drafting_plan"
    assert result["messages"] == [f"AI: {question}", "用户: 给 3 号写封邮件，强调 MOQ 500"]
    assert result["request"] == "给 3 号写封邮件，强调 MOQ 500"
    assert runner.conversation_status(app.get_state(_config(1289))) == conversation.WAITING


def test_no_email_still_offers_all_four_forms(app):
    """一个邮箱都没有：四种照样都能写（不管能不能发，照写），只加一句地址要他自己找。"""
    result = app.invoke(initial_state(1281, ""), _config(1281))

    question = result["__interrupt__"][0].value["question"]
    assert question == (
        "请问你想给 Chatterly Inc. 写哪些 outreach 信息？一次可以写几份（最多 4 份），"
        "每份说多少都行，没说的我来定。\n"
        "\n"
        "名单：\n"
        "1. Chatterly Inc. 这家公司本身（称呼他们的团队）\n"
        "2. Ravi Shah（职位串里写着这家公司，还没核对他现在是不是在这家）\n"
        "\n"
        "可以写四种：邮件、LinkedIn 私信、LinkedIn 加好友附言"
        "（附言 300 字符以内；对方还不是好友时私信可能收不到，附言总能送到），"
        "还有文章（不写给具体的人，把我们的卖点套到这家身上写一篇）。\n"
        "这家公司没有找到邮箱：邮件照样能写，发之前要你自己找地址。\n"
        "\n"
        "你可以这样回复：「给 2 号写封邮件，强调 MOQ 500，再写一篇文章讲我们能做小批量」。"
        "没说写给谁的，写给 1 号这家公司；也可以回「看你」，让我来定。"
    )


def test_another_companys_email_is_not_offered(app):
    """Kestrel 唯一的邮箱是 Evan Park 在 Rawcut 的：不算这家的，按没邮箱问。

    这条红了的症状：bot 说「这家的邮箱：evan.park@rawcutstudio.example」，开发信发到别家去
    """
    result = app.invoke(initial_state(1518, ""), _config(1518))

    question = result["__interrupt__"][0].value["question"]
    assert "rawcutstudio" not in question
    assert "这家公司没有找到邮箱" in question
    assert app.get_state(_config(1518)).values["lead"]["emails"] == []


def test_retrieve_knowledge_collects_what_drafting_needs(app):
    """开对话时 knowledge 一次取齐：概况、打分结论、证据全文、facts.json、写稿的 avoid。

    这条红了的症状：以后写稿的模型拿不到公司自我介绍、看不出哪条证据是第三方说的，
    或者看到的 facts / avoid 是空的，开始自己编我们的数字
    """
    app.invoke(initial_state(1289, ""), _config(1289))

    knowledge = app.get_state(_config(1289)).values["knowledge"]
    # 概况：表里的几列 + raw 里的自我介绍；精确人数和电话不要
    assert knowledge["profile"] == {
        "website": "https://northfield-inc.example",
        "employee_count_range": "2-10",
        "founded_year": 2021,
        "location": "Montreal, CA",
        "industries": ["Consumer Electronics"],
        "tagline": "Karaoke at home, done right",
        "description": "North Field builds a karaoke microphone that pairs with any TV.",
        "specialities": ["karaoke", "wireless microphones"],
    }
    assert knowledge["assessment"] == {
        "score": 72,
        "analysis": "North Field is building a karaoke mic and asked for a partner to make 1,000 units.",
        "quotes_mean": "创始人说在找能做小批量的工厂",
        "quotes": ["we need a partner for 1,000 units"],
    }
    # 证据顺序照库里的（最新在前），通道存取值，是不是本人说的已经算好
    assert [(e["signal_id"], e["lead_type"], e["first_person"]) for e in knowledge["evidence"]] == [
        (2733, "mentioned", False),
        (2720, "author", True),
    ]
    assert knowledge["evidence"][1]["text"] == "Our karaoke mic is ready; we need a partner for 1,000 units."
    # 两份配置读的是仓库里的真文件
    assert knowledge["facts"]["moq"]["value"] == 500
    assert [note["key"] for note in knowledge["avoid"]] == [
        "third-party-evidence-no-fake-coincidence",
        "our-numbers-only-from-facts",
        "no-lead-time-or-sample-cost-promises",
    ]
    assert {note["scope"] for note in knowledge["avoid"]} == {"draft"}


def test_a_company_never_profiled_gets_an_empty_profile(app):
    """只有公司名、没抓过公司详情的那家：profile 是 {}，不编；没证据的 evidence 是 []。"""
    app.invoke(initial_state(1281, ""), _config(1281))

    knowledge = app.get_state(_config(1281)).values["knowledge"]
    assert knowledge["profile"] == {}
    assert knowledge["evidence"] == []
    assert knowledge["assessment"]["score"] == 67


def test_retrieve_knowledge_runs_once_even_after_the_reply(app, monkeypatch, script):
    """查库只在开对话那一轮查一次：用户回话恢复时，重跑的是 ask_request，不是 retrieve_knowledge。"""
    calls = []
    real = nodes.contacts_db.contact_inputs

    def counting(cur, lead_ids):
        calls.append(list(lead_ids))
        return real(cur, lead_ids)

    monkeypatch.setattr(nodes.contacts_db, "contact_inputs", counting)

    script["给 3 号写条私信"] = _said(_ask("给 3 号写条私信", 3, "linkedin_message"))

    app.invoke(initial_state(1289, ""), _config(1289))
    app.invoke(Command(resume="给 3 号写条私信"), _config(1289))

    assert calls == [[1289]]


@pytest.mark.parametrize("reply", ["写吧", "看你"])
def test_a_vague_reply_goes_on_without_asking(app, reply):
    """点了按钮只回「写吧」「看你」、一份都没说：不追问，AI 补的份就是这一轮的计划，停在计划上。

    这条红了的症状：用户什么都不想说，bot 却一直问「写给谁、写哪一种」
    """
    app.invoke(initial_state(1289, ""), _config(1289))

    result = app.invoke(Command(resume=reply), _config(1289))

    assert result["__interrupt__"][0].value["kind"] == "drafting_plan"
    assert [d["user_words"] for d in result["ai_plan"]["drafts"]] == [""]


def test_an_unknown_person_does_not_stop_the_graph(app, script):
    """说了一个名单上没有的人（Mike）：不另外追问，照样定完计划、停在计划上，只调一次模型。

    这份写给谁、要不要改，是看计划时用户说的事（planner.finalize_plan 会在计划里点出「Mike」没认出）
    """
    first = _ask("给 3 号写封邮件", 3, "email")
    mike = _ask("再给 Mike 写条私信", -1, "linkedin_message", said_to="Mike")
    script["给 3 号写封邮件，再给 Mike 写条私信"] = _said(first, mike)
    app.invoke(initial_state(1289, ""), _config(1289))

    result = app.invoke(Command(resume="给 3 号写封邮件，再给 Mike 写条私信"), _config(1289))

    assert result["__interrupt__"][0].value["kind"] == "drafting_plan"
    assert [d["recipient_ai_understood"] for d in result["ai_plan"]["drafts"]] == [3, -1]
    assert len(script["_reads"]) == 1


def test_plan_drafts_sees_the_knowledge(app, script):
    """定计划那次调用拿到的是 retrieve_knowledge 取齐的那份：证据和 facts.json 都在。

    这条红了的症状：模型挑卖点、挑开场时看不到证据，只能瞎挑，或者 parse_ai_plan 把它挑的证据编号全判成编的
    """
    app.invoke(initial_state(1289, "写吧"), _config(1289))

    (knowledge,) = script["_knowledge"]
    assert [e["signal_id"] for e in knowledge["evidence"]] == [2733, 2720]
    assert knowledge["facts"]["moq"]["value"] == 500


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        # 服务商给了一个表里没有的状态：原样印，不猜它是什么意思
        (
            {
                "value": "a@x.com",
                "source": "linkedin_email_search",
                "person_name": None,
                "email_status": "unknown",
                "catch_all": None,
                "quality_score": None,
            },
            "a@x.com：服务商标的是「unknown」",
        ),
        # 服务商一项可信度都没给：说清是从 LinkedIn 主页找到的，不留一个空的冒号
        (
            {
                "value": "a@x.com",
                "source": "linkedin_email_search",
                "person_name": "Ravi Shah",
                "email_status": None,
                "catch_all": None,
                "quality_score": None,
            },
            "a@x.com（Ravi Shah 的）：从 LinkedIn 主页找到的，服务商没给可信度",
        ),
    ],
    ids=["unseen-status", "no-trust-at-all"],
)
def test_email_brief_rare_cases(email, expected):
    assert nodes._email_brief(email) == expected


def test_unknown_lead_stops_before_asking(app):
    """lead_id 在库里没有：retrieve_knowledge 当场报错，不去问「给这家写什么」。"""
    with pytest.raises(ValueError, match="id=999999"):
        app.invoke(initial_state(999999, ""), _config(999999))


# 一条转发帖：Marc-Andre 转发了 SingAlong Group 的帖子，卡片上两人共用这条帖子的链接（库里 lead 1289 的真形状）
_REPOST_URL = "https://www.linkedin.com/posts/activity-7493771291343237120-wIy1"
_CARD_PEOPLE = (
    CardPerson("Zach T.", CardRelation.NOT_MEMBER, None, None, "https://www.linkedin.com/in/zachtrent",
               "https://www.linkedin.com/posts/zachtrent_1"),
    CardPerson("Marc-Andre Lavoie", CardRelation.MEMBER, "Founder & CEO", None,
               "https://www.linkedin.com/in/malavoie", _REPOST_URL),
    CardPerson("SingAlong Group", CardRelation.OTHER_PAGE, None, None,
               "https://www.linkedin.com/company/singalong-group/", _REPOST_URL),
)  # fmt: skip


def _post(author: str, lead_type: LeadType, url: str) -> DraftEvidence:
    return DraftEvidence(
        signal_id=1, lead_type=lead_type, author=author, role="", when="2026-09-20", url=url,
        text="…", reposted_author=None, reposted_text=None,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("post", "own"),
    [
        (_post("Dana Lee", LeadType.AUTHOR, "u"), True),  # 通道本身就是本人
        (_post("Marc-Andre Lavoie", LeadType.REPOST_MENTIONED, _REPOST_URL), True),  # member 转发的
        (_post("Zach T.", LeadType.MENTIONED, "https://www.linkedin.com/posts/zachtrent_1"), False),  # 外人
        (_post("SingAlong Group", LeadType.MENTIONED, _REPOST_URL), False),  # 共用链接的别的机构
        (_post("Someone Else", LeadType.MENTIONED, _REPOST_URL), False),  # 不在卡片上，不猜
    ],
    ids=["author-channel", "member-repost", "not-member", "other-page-same-url", "not-on-card"],
)
def test_own_voice_is_judged_against_the_card(post, own):
    """是不是本人说的，看通道之外还要看发帖人在卡片上是什么关系。

    这条红了的症状：Hushly 的 CEO、North Field 的创始人自己发的帖子被标成「第三方说的」，
    写稿时不敢说「看到你发的帖子」；或者反过来，把别的机构的帖子当成本人的
    """
    assert nodes._is_own_voice(post, _CARD_PEOPLE) is own


def test_a_restarted_round_only_reads_its_own_words(app, script):
    """对话走到 END 之后，在同一份存档上用新的一句从头再跑：定计划只读这一轮的话，不读上一轮的。

    这条红了的症状：重开之后模型把上一轮的「写三份…Mike」当成这一轮的又定一遍，新说的那句被挤掉
    （Slack 实测里真这样过）
    """
    first = app.invoke(initial_state(1289, "写三份：给 3 号写封邮件，再给 Mike 写一份"), _config(1289))
    first_plan = first["__interrupt__"][0].value["question"]
    app.invoke(Command(resume="算了"), _config(1289))
    assert runner.conversation_status(app.get_state(_config(1289))) == conversation.ENDED

    result = app.invoke(initial_state(1289, "给 3 号写条加好友附言"), _config(1289))

    assert result["messages"][:3] == [
        "用户: 写三份：给 3 号写封邮件，再给 Mike 写一份",
        f"AI: {first_plan}",
        "用户: 算了",
    ]
    # 这一轮只有新说的那句：新计划要等他回话时才跟回话一起进 messages
    assert result["messages"][3:] == ["用户: 给 3 号写条加好友附言"]
    assert result["round_start"] == 3
    assert script["_reads"][-1] == ["用户: 给 3 号写条加好友附言"]
    # 上一轮的 decision 被新一轮的初始 state 清掉了：这一轮还没判过
    assert result["decision"] == ""


def _to_the_plan(app, script) -> str:
    """开一条 @bot 起草对话，停在计划上。返回那份计划（交出去的那句话）。"""
    script["给 3 号写封邮件"] = _said(_ask("给 3 号写封邮件", 3, "email"))
    result = app.invoke(initial_state(1289, "给 3 号写封邮件"), _config(1289))
    return result["__interrupt__"][0].value["question"]


def test_the_plan_is_sent_and_recorded(app, script):
    """停在计划上：交出去的是那份计划；他回话之后，计划和回话一起进 messages，原话进 plan_feedback。

    这条红了的症状：要改时绕回 plan_drafts，模型看不到上一版计划，「第 1 份」不知道指的是哪一份
    """
    plan = _to_the_plan(app, script)
    assert plan.startswith(
        "好，给 North Field Technologies 写这 1 份：\n第 1 份：邮件，发到 marcandre@northfield-inc.example"
    )
    assert plan.endswith("没问题回「写吧」；不写了回「算了」。")

    result = app.invoke(Command(resume="算了"), _config(1289))

    assert result["messages"] == ["用户: 给 3 号写封邮件", f"AI: {plan}", "用户: 算了"]
    assert script["_judged"] == ["算了"]


def test_cancel_at_the_plan_ends_the_round(app, script):
    """对着计划说不写了：走到 END，decision 留着（END 那句话按它挑），plan_feedback 清掉。"""
    _to_the_plan(app, script)

    result = app.invoke(Command(resume="算了"), _config(1289))

    assert "__interrupt__" not in result
    assert (result["decision"], result["plan_feedback"]) == ("cancel", "")
    assert runner.conversation_status(app.get_state(_config(1289))) == conversation.ENDED


def test_approve_writes_and_stops_at_the_drafts(app, script):
    """对着计划说同意：去写稿，写完停在看稿那一停，交出去的是这一次写的正文。"""
    _to_the_plan(app, script)

    result = app.invoke(Command(resume="就这样"), _config(1289))

    assert app.get_state(_config(1289)).next == ("await_write",)
    assert result["written_now"] == [1]
    assert result["drafts"]["1"]["status"] == "written"
    question = result["__interrupt__"][0].value
    assert question["kind"] == "drafting_write"
    assert question["question"].startswith("第 1 份 · 邮件，发到 marcandre@northfield-inc.example → ")
    assert "第 1 份第 1 版（email）" in question["question"]


def test_a_revision_goes_back_and_plans_again(app, script):
    """要改：绕回 plan_drafts 重定一版，重定时读得到上一版计划和他怎么改的，再停在新计划上。"""
    plan = _to_the_plan(app, script)
    script["第 1 份改成私信"] = _said(_ask("给 3 号写封邮件", 3, "linkedin_message", plan_number=1))

    result = app.invoke(Command(resume="第 1 份改成私信"), _config(1289))

    assert script["_reads"][-1] == ["用户: 给 3 号写封邮件", f"AI: {plan}", "用户: 第 1 份改成私信"]
    new_plan = result["__interrupt__"][0].value["question"]
    assert "第 1 份：LinkedIn 私信" in new_plan
    assert (result["decision"], result["plan_feedback"]) == ("revise", "第 1 份改成私信")
    assert app.get_state(_config(1289)).next == ("await_plan",)
    # 第一次定计划没有上一版；要改时，修改意见和标好编号的上一版明写交进去
    (first_feedback, first_previous), (feedback, previous) = script["_revisions"]
    assert (first_feedback, first_previous) == ("", [])
    assert feedback == "第 1 份改成私信"
    assert [(d["plan_number"], d["user_words"], d["form_user_requested"]) for d in previous] == [
        (1, "给 3 号写封邮件", "email")
    ]


def test_an_empty_plan_cannot_be_approved(monkeypatch):
    """计划一份都没有却判成同意：改判要改，不去照一份空计划写稿。"""
    monkeypatch.setattr(nodes, "build_llm_client", lambda: None)
    monkeypatch.setattr(
        nodes, "read_decision", lambda *a, **k: ReplyJudgement(decision="approve", reason="测试剧本")
    )

    out = nodes.classify_plan_reply(
        {"messages": ["用户: 写吧"], "round_start": 0, "plan_feedback": "写吧", "plan": {"slots": []}}
    )

    assert out == {"decision": "revise"}


def test_the_decision_values_and_the_branch_table_are_one_set():
    """reply.DECISIONS 和图上那张表的键逐字相同。多一个少一个都不报错，要等用户真回话才发作。"""
    app = build_graph(InMemorySaver())
    ends = app.builder.branches["classify_plan_reply"]["route_plan_reply"].ends

    assert set(DECISIONS) == set(ends)


# ── 看稿之后：改计划、改正文、重新打开、写库（langgraph学习/drafting/replan_rewrite_1a.txt 第四节） ──

FOUR = "写四份：给 3 号写封邮件，给 2 号写条加好友附言，给官网发一封邮件，再写一篇讲小批量的文章"
JP_MAIL = _ask("给 3 号写封邮件", 3, "email")
ZACH_NOTE = _ask("给 2 号写条加好友附言", 2, "linkedin_note")
SITE_MAIL = _ask("给官网发一封邮件", 1, "email")
ARTICLE = _ask("再写一篇讲小批量的文章", 0, "article", emphasis=("小批量",))
FIVE_THINGS = "第 1 份可以了。第 2 份不要了。第 3 份改成给 3 号发私信。第 4 份短一点。再给 4 号写条附言"


def _numbered(draft: AiPlanDraft, number: int) -> AiPlanDraft:
    return dataclasses.replace(draft, plan_number=number)


def _to_the_first_drafts(app, script) -> dict:
    """点按钮开对话 -> 要四份 -> 计划阶段把第 2 份改成私信 -> 同意 -> 停在第一批稿子上。"""
    script[FOUR] = _said(JP_MAIL, ZACH_NOTE, SITE_MAIL, ARTICLE)
    script["第 2 份改成私信"] = _said(
        _numbered(JP_MAIL, 1),
        _numbered(dataclasses.replace(ZACH_NOTE, form_user_requested="linkedin_message"), 2),
        _numbered(SITE_MAIL, 3),
        _numbered(ARTICLE, 4),
    )
    app.invoke(initial_state(1289, ""), _config(1289))
    app.invoke(Command(resume=FOUR), _config(1289))
    app.invoke(Command(resume="第 2 份改成私信"), _config(1289))
    return app.invoke(Command(resume="就这样"), _config(1289))


def test_the_whole_round_from_plan_to_final_drafts(app, script):
    """笔记第四节那个例子在 North Field 上从头跑到尾：号不变、两个版本号各数各的、改计划的先回 plan_drafts、
    只改正文的直接写、已定稿的能重新打开、库里每句判决一行。
    """
    first = _to_the_first_drafts(app, script)

    # 计划阶段改了第 2 份：它的 plan_version 是 2，号没变；四份都写了第 1 版
    assert [(s["number"], s["plan_version"]) for s in first["plan"]["slots"]] == [
        (1, 1),
        (2, 2),
        (3, 1),
        (4, 1),
    ]
    assert first["written_now"] == [1, 2, 3, 4]
    assert {k: (e["status"], e["version"]) for k, e in first["drafts"].items()} == {
        str(n): ("written", 1) for n in range(1, 5)
    }
    assert script["_rows"] == []

    # 一句话五件事：#3 要改计划、要加一份 -> 整轮先回 plan_drafts，#4 的正文意见在账本里等着
    script["_feedback"][FIVE_THINGS] = _judged(
        SlotVerdict(1, "approve", "", ""),
        SlotVerdict(2, "drop", "", ""),
        SlotVerdict(3, "change", "改成给 3 号发私信", ""),
        SlotVerdict(4, "change", "", "短一点"),
        add=("再给 4 号写条附言",),
    )
    script[FIVE_THINGS] = _said(
        _numbered(JP_MAIL, 1),
        _numbered(_ask("第 3 份改成给 3 号发私信", 3, "linkedin_message"), 3),
        _numbered(ARTICLE, 4),
        _ask("再给 4 号写条附言", 4, "linkedin_note"),
    )
    replanned = app.invoke(Command(resume=FIVE_THINGS), _config(1289))

    assert app.get_state(_config(1289)).next == ("await_plan",)
    feedback, previous = script["_revisions"][-1]
    assert feedback == "第 3 份：改成给 3 号发私信\n新加一份：再给 4 号写条附言"
    assert [d["plan_number"] for d in previous] == [1, 2, 3, 4]
    assert script["_ledgers"][-1] == {"1": "approved", "2": "dropped", "3": "replan", "4": "rewrite"}
    assert [(s["number"], s["plan_version"]) for s in replanned["plan"]["slots"]] == [
        (1, 1), (3, 2), (4, 1), (5, 1),
    ]  # fmt: skip
    assert {k: e["status"] for k, e in replanned["drafts"].items()} == {
        "1": "approved", "2": "dropped", "3": "planned", "4": "rewrite", "5": "planned",
    }  # fmt: skip
    assert replanned["drafts"]["4"]["feedback"] == "短一点"
    plan = replanned["__interrupt__"][0].value["question"]
    assert plan.startswith(
        "改好了，这一轮的计划：\n第 1 份（已定稿，不动）：邮件，发到 marcandre@northfield-inc.example"
    )
    for mark in (
        "第 3 份（计划改了）",
        "第 4 份（计划不变，按你说的改正文）",
        "第 5 份（新加的）",
        "已不要：第 2 份",
    ):
        assert mark in plan

    # 同意新计划：只写 #3（照新计划从头写）、#4（按意见改）、#5（新的）；#1 #2 不碰
    second = app.invoke(Command(resume="就这样"), _config(1289))

    assert second["written_now"] == [3, 4, 5]
    assert [w[:3] for w in script["_written"][-3:]] == [
        (3, "planned", ""), (4, "rewrite", "短一点"), (5, "planned", ""),
    ]  # fmt: skip
    assert {k: (e["status"], e["version"], e["plan_version"]) for k, e in second["drafts"].items()} == {
        "1": ("approved", 1, 1), "2": ("dropped", 1, 2), "3": ("written", 2, 2),
        "4": ("written", 2, 1), "5": ("written", 1, 1),
    }  # fmt: skip
    drafts_text = second["__interrupt__"][0].value["question"]
    assert drafts_text.startswith("第 3 份 · LinkedIn 私信 → Marc-Andre Lavoie")
    assert "总览：已定稿 第 1 份；已不要 第 2 份" in drafts_text

    # 只改正文：不经过 plan_drafts、不停下来看计划，直接只写 #4
    script["_feedback"]["第 4 份还是太长。其他都行"] = _judged(
        SlotVerdict(3, "approve", "", ""),
        SlotVerdict(4, "change", "", "还是太长"),
        SlotVerdict(5, "approve", "", ""),
    )
    plans_before = len(script["_reads"])
    third = app.invoke(Command(resume="第 4 份还是太长。其他都行"), _config(1289))

    assert len(script["_reads"]) == plans_before
    assert third["written_now"] == [4]
    assert third["drafts"]["4"]["version"] == 3

    # 回头改已定稿的 #1：重新打开，在原稿上改；#4 没点名，还在等
    script["_feedback"]["第 1 份也加一句固件可以定制"] = _judged(
        SlotVerdict(1, "change", "", "加一句固件可以定制")
    )
    fourth = app.invoke(Command(resume="第 1 份也加一句固件可以定制"), _config(1289))

    assert script["_written"][-1][:3] == (1, "rewrite", "加一句固件可以定制")
    assert fourth["written_now"] == [1]
    assert (fourth["drafts"]["1"]["status"], fourth["drafts"]["1"]["version"]) == ("written", 2)
    assert (fourth["drafts"]["4"]["status"], fourth["drafts"]["4"]["version"]) == ("written", 3)
    assert "还在等你说 第 4 份" in fourth["__interrupt__"][0].value["question"]

    # 都可以了：走到 END，列出终稿
    script["_feedback"]["都可以了"] = _judged(
        SlotVerdict(1, "approve", "", ""), SlotVerdict(4, "approve", "", "")
    )
    done = app.invoke(Command(resume="都可以了"), _config(1289))

    assert "__interrupt__" not in done
    assert done["decision"] == "done"
    assert runner.conversation_status(app.get_state(_config(1289))) == conversation.ENDED
    assert messages.render_ended(done)["text"] == (
        "定稿 4 份：\n"
        "第 1 份 邮件 → Marc-Andre Lavoie（第 2 版）\n"
        "第 3 份 LinkedIn 私信 → Marc-Andre Lavoie（第 2 版）\n"
        "第 4 份 文章 → North Field Technologies（第 3 版）\n"
        "第 5 份 LinkedIn 加好友附言 → SingAlong Group（第 1 版）\n"
        "正文都在上面的消息里，复制发出去就行。要再写，在这个 thread 里 @ 我。"
    )

    # 库里一共 10 行：每句看稿回话对它判了的每一份一行，只追加
    assert [
        (r.reply_index, r.slot_number, r.draft_version, r.plan_version, r.verdict) for r in script["_rows"]
    ] == [
        (7, 1, 1, 1, "approved"),
        (7, 2, 1, 2, "dropped"),
        (7, 3, 1, 1, "replan"),
        (7, 4, 1, 1, "rewrite"),
        (11, 3, 2, 2, "approved"),
        (11, 4, 2, 1, "rewrite"),
        (11, 5, 1, 1, "approved"),
        (13, 1, 1, 1, "rewrite"),
        (15, 1, 2, 1, "approved"),
        (15, 4, 3, 1, "approved"),
    ]
    row = script["_rows"][2]
    assert (row.thread_id, row.lead_id, row.form, row.recipient_name, row.address) == (
        "draft:C0000000001:lead1289",
        1289,
        "email",
        None,
        "contact@northfield-inc.example",
    )
    assert (row.feedback, row.user_reply) == ("改成给 3 号发私信", FIVE_THINGS)
    assert row.text == "第 3 份第 1 版（email）"
    assert done["messages"][7] == f"用户: {FIVE_THINGS}"


def test_unmentioned_drafts_are_asked_about_again(app, script):
    """只说了「第 1 份可以了」：没有要写的，回看稿那一停再问一次剩下的，不重发正文、不再写稿。"""
    _to_the_first_drafts(app, script)
    script["_feedback"]["第 1 份可以了"] = _judged(SlotVerdict(1, "approve", "", ""))
    written_before = len(script["_written"])

    result = app.invoke(Command(resume="第 1 份可以了"), _config(1289))

    assert app.get_state(_config(1289)).next == ("await_write",)
    assert len(script["_written"]) == written_before
    question = result["__interrupt__"][0].value["question"]
    assert question.startswith("还在等你说：第 2 份、第 3 份、第 4 份（正文在上面）。")
    assert "第 1 份第 1 版" not in question


def test_cancel_after_seeing_the_drafts_records_them(app, script):
    """看完稿说不写了：还在等的每份记一行 cancelled，走到 END 回不写了那句。"""
    _to_the_first_drafts(app, script)
    script["_feedback"]["算了"] = _judged(cancel=True)

    result = app.invoke(Command(resume="算了"), _config(1289))

    assert result["decision"] == "cancel"
    assert messages.render_ended(result) == {"text": messages.CANCELLED_TEXT}
    assert [(r.slot_number, r.verdict) for r in script["_rows"]] == [(n, "cancelled") for n in range(1, 5)]


def test_record_round_twice_writes_once(app, script):
    """record_round 崩了重跑：同一句话对同一份只写一次（唯一键 thread_id + reply_index + slot_number）。"""
    _to_the_first_drafts(app, script)
    state = dict(app.get_state(_config(1289)).values)
    state.update(
        messages=state["messages"] + ["用户: 第 1 份可以了"],
        draft_feedback="第 1 份可以了",
        verdicts=[{"number": 1, "verdict": "approved", "feedback": ""}],
    )

    nodes.record_round(state, _config(1289))
    nodes.record_round(state, _config(1289))

    # 手拼的 state 里稿子摘要那句还没进 messages，接上的回话是第 6 句
    assert [(r.reply_index, r.slot_number) for r in script["_rows"]] == [(6, 1)]


def test_the_feedback_decisions_and_the_branch_table_are_one_set():
    """feedback.DECISIONS 和 record_round 后面那张表的键逐字相同；
    await_write 退回时假装 write_draft 刚跑完。
    """
    app = build_graph(InMemorySaver())
    ends = app.builder.branches["record_round"]["route_feedback"].ends

    assert set(FEEDBACK_DECISIONS) == set(ends)
    assert slack_conversation._BEFORE_INTERRUPT["await_write"] == "write_draft"


def test_nothing_left_to_write_says_so():
    """同意的那版计划里只剩已定稿的：看稿那一停说「这一轮没有要写的稿子了」，
    不发一句空的「还在等你说」。
    """
    empty_plan = {"slots": [], "language": "英文", "tone": "", "unsupported": [], "notes": []}
    approved = {
        "status": "approved",
        "version": 1,
        "plan_version": 1,
        "text": "x",
        "feedback": "",
        "problems": [],
    }
    text = messages.drafts_text(
        {
            "plan": empty_plan | {"ai_plans_all": False},
            "drafts": {"1": approved},
            "written_now": [],
            "notice": "",
            "lead": {"display_name": "North Field Technologies"},
        }
    )  # fmt: skip

    assert text.startswith("这一轮没有要写的稿子了。\n\n总览：已定稿 第 1 份")


def test_a_dropped_draft_comes_back_with_its_number_after_seeing_the_drafts(app, script):
    """看稿时说第 2 份不要了，下一句又说还是要：原号复活，那版正文原样再发一遍，不重写、不经过计划；
    库里只记「不要了」那一行（要回来没改什么，不算一次判决）。
    """
    _to_the_first_drafts(app, script)
    script["_feedback"]["第 2 份不要了"] = _judged(SlotVerdict(2, "drop", "", ""))
    script["_feedback"]["第 2 份还是要吧"] = _judged(SlotVerdict(2, "change", "", ""))
    app.invoke(Command(resume="第 2 份不要了"), _config(1289))
    written_before = len(script["_written"])

    result = app.invoke(Command(resume="第 2 份还是要吧"), _config(1289))

    assert result["drafts"]["2"]["status"] == "written"
    assert [s["number"] for s in result["plan"]["slots"]] == [1, 2, 3, 4]
    assert result["written_now"] == [2]
    assert len(script["_written"]) == written_before
    question = result["__interrupt__"][0].value["question"]
    assert question.startswith("第 2 份 · LinkedIn 私信 → Zach T.")
    assert "第 2 份第 1 版（linkedin_message）" in question
    assert [(r.slot_number, r.verdict) for r in script["_rows"]] == [(2, "dropped")]


def test_cancel_at_a_replanned_plan_records_the_drafts_still_waiting(app, script):
    """看稿时只改了第 1 份的计划（第 2、3、4 份没表态，还在等），看新计划时说不写了：
    那三份各记一行 cancelled，user_reply 是「算了」这句；第 1 份已经记过 replan，不重复记。
    """
    _to_the_first_drafts(app, script)
    script["_feedback"]["第 1 份改成私信"] = _judged(SlotVerdict(1, "change", "改成私信", ""))
    script["第 1 份改成私信"] = _said(
        _numbered(dataclasses.replace(JP_MAIL, form_user_requested="linkedin_message"), 1),
        _numbered(dataclasses.replace(ZACH_NOTE, form_user_requested="linkedin_message"), 2),
        _numbered(SITE_MAIL, 3),
        _numbered(ARTICLE, 4),
    )
    app.invoke(Command(resume="第 1 份改成私信"), _config(1289))
    assert app.get_state(_config(1289)).next == ("await_plan",)

    result = app.invoke(Command(resume="算了"), _config(1289))

    assert result["decision"] == "cancel"
    assert runner.conversation_status(app.get_state(_config(1289))) == conversation.ENDED
    assert [(r.slot_number, r.verdict, r.user_reply) for r in script["_rows"]] == [
        (1, "replan", "第 1 份改成私信"),
        (2, "cancelled", "算了"),
        (3, "cancelled", "算了"),
        (4, "cancelled", "算了"),
    ]


def test_drafts_left_waiting_too_long_are_marked_abandoned(app, script):
    """停在看稿那一停 8 天没人回：还在等的 4 份各一行 abandoned，reply_index 是下一句的位置，user_reply 空；
    不到 7 天、或者对话已经结束，都不记；扫两次得到的是同样的行（写库时撞唯一键，不重复）。
    """
    _to_the_first_drafts(app, script)
    snapshot = app.get_state(_config(1289))
    stopped = dt.datetime.fromisoformat(snapshot.created_at)
    thread = _config(1289)["configurable"]["thread_id"]

    rows = revisions.sweep(app, [thread], now=stopped + dt.timedelta(days=8), after_days=7)
    soon = revisions.sweep(app, [thread], now=stopped + dt.timedelta(days=6), after_days=7)
    again = revisions.sweep(app, [thread], now=stopped + dt.timedelta(days=9), after_days=7)

    assert [(r.slot_number, r.draft_version, r.verdict, r.reply_index, r.user_reply) for r in rows] == [
        (n, 1, "abandoned", 6, "") for n in range(1, 5)
    ]
    assert soon == []
    assert [(r.reply_index, r.slot_number) for r in again] == [(r.reply_index, r.slot_number) for r in rows]

    # 他后来回了话：那句的下标（7）在 abandoned 那几行（6）后面，不撞
    script["_feedback"]["都可以了"] = _judged(*(SlotVerdict(n, "approve", "", "") for n in range(1, 5)))
    app.invoke(Command(resume="都可以了"), _config(1289))
    assert {r.reply_index for r in script["_rows"]} == {7}
    later = app.get_state(_config(1289))
    assert revisions.sweep(app, [thread], now=stopped + dt.timedelta(days=30), after_days=7) == []
    assert later.next == ()
