"""写稿那张图上每个节点的实现。

节点只做编排：读 state、调下面那几层、返回一个字典。查库在 persistence / triggers.contacts 里，
拼给人看的第一问在这里的纯函数里（request_question），读需求、定计划在 planner.py，
节点自己不写业务规则。

interrupt 写在节点第一行、一个节点最多一个，tests/test_interrupt_discipline.py 用 ast 扫整个
interactive_discovery 包焊着这条纪律，这个文件也在里面。
"""

from __future__ import annotations

import dataclasses
import pathlib

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from delivery.cards import EMAIL_STATUS_TEXT
from discovery.analyzer import load_facts
from models import DraftEvidence
from models.contact import CardPerson, CardRelation, CompanyProfile, ContactSource
from persistence import contacts_db, db, draft_db, lead_db
from query_intelligence.knowledge import load_avoid
from triggers.contacts import card_contacts
from triggers.harvest import build_llm_client
from triggers.interactive_discovery.drafting.feedback import (
    apply_feedback,
    load_feedback_config,
    read_feedback,
)
from triggers.interactive_discovery.drafting.messages import drafts_summary, drafts_text, plan_text
from triggers.interactive_discovery.drafting.planner import (
    COMPANY_NUMBER,
    TO_WRITE,
    WRITTEN,
    ai_plan_to_state,
    company_line,
    copy_entry,
    drafts_after_plan,
    email_numbers,
    finalize_plan,
    listed_people,
    load_plan_config,
    person_line,
    plan_from_state,
    plan_to_state,
    previous_drafts,
    read_ai_plan,
    untouched_changes,
)
from triggers.interactive_discovery.drafting.reply import load_reply_config, read_decision
from triggers.interactive_discovery.drafting.revisions import revision_row
from triggers.interactive_discovery.drafting.state import DraftingState
from triggers.interactive_discovery.drafting.writer import load_write_config, write_one

# 写稿的负面知识（哪类写法别用），格式同 config/query_knowledge.yaml
# 往上四层到仓库根：drafting/ -> interactive_discovery/ -> triggers/ -> 仓库根
DRAFTING_KNOWLEDGE_PATH = pathlib.Path(__file__).resolve().parents[3] / "config" / "drafting_knowledge.yaml"


def retrieve_knowledge(state: DraftingState) -> dict:
    """开对话时把写稿要的东西一次从库里和配置里取齐。只读，不花钱，不调模型。

    取两份，各进 state 的一个字段：

        lead        发给谁、发到哪：公司名、这家自己的邮箱（带可信度）、卡片上的人。
                    第一问和 plan_drafts 要用它（名单编号、有没有邮箱）
        knowledge   写什么：公司概况、最近一次打分的结论、几条证据的全文、我们能提供什么（facts.json）、
                    哪类写法别用（drafting_knowledge.yaml）。plan_drafts 挑卖点和开场用它，
                    以后 write_draft 也用

    「对这家该推我们哪项业务」不在这里判断：这里只取，判断是 plan_drafts 的事。
    跟 query 图的 retrieve_knowledge 同一条规矩：取哪几样是代码写死的，不让模型决定。

    输入：state["lead_id"]，例 1289
    输出：{"lead": {"display_name": "North Field Technologies",
                    "emails": [{"value": "contact@northfield-inc.example", "source": "website",
                                "person_name": None, "email_status": None, "catch_all": None,
                                "quality_score": None},
                               {"value": "marcandre@northfield-inc.example", "source": "linkedin_email_search",
                                "person_name": "Marc-Andre Lavoie", "email_status": "risky",
                                "catch_all": True, "quality_score": 60}],
                    "people": [{"name": "Zach T.", "relation": "not_member", "position": None,
                                "profile_url": "https://www.linkedin.com/in/zachtrent"},
                               {"name": "Marc-Andre Lavoie", "relation": "member",
                                "position": "Founder & CEO", "profile_url": "https://www.linkedin.com/in/malavoie"},
                               {"name": "SingAlong Group", "relation": "other_page", "position": None,
                                "profile_url": "https://www.linkedin.com/company/singalong-group/"}]},
          "knowledge": {"profile": {"website": "https://northfield-inc.example", ...},
                        "assessment": {"score": 72, "analysis": "...", ...},
                        "evidence": [三条],
                        "facts": {"moq": {...}, "services": {...}, ...},
                        "avoid": [三条]}}
          knowledge 每一格长什么样，见 state.py 的 knowledge 字段
          另外一格 "round_start": 0（这一轮从 messages 第几句开始，见下面最后那段）

    单开一个节点、不写进 ask_request：ask_request 第一行是 interrupt，用户每回一次话它就从第一行
    重跑一次，查库放在它里面就是每回一次话查一次。放在这里，整条对话只查这一次。
    放在 plan_drafts 前面、而不是像 query 图那样放在读需求后面：query 要先知道用户想找哪个品类
    才知道查什么；这边要取的东西跟用户说了什么无关，都是这一家的，开头取齐就行
    """
    lead_id = state["lead_id"]

    # 四样一次查齐，共用一个连接一个事务：分几次连的话几份数据可能来自几个时刻
    with db.connect() as conn, conn.cursor() as cur:
        # 输入   [1289]
        # 输出   inputs -> {1289: (LeadForContacts(lead_id=1289, display_name="North Field Technologies",
        #                                          linkedin_slug="northfield-inc", ...),
        #                          [AuthorRef(signal_id=2720, name="Marc-Andre Lavoie", ...),
        #                           另外两条])}
        #        只用它的 display_name。card_contacts 里面也查了一遍这个，但它返回的 CardContacts 不带公司名
        inputs = contacts_db.contact_inputs(cur, [lead_id])
        # 输出   contacts -> CardContacts(website_url="https://northfield-inc.example", ...,
        #                     emails=(CardEmail(value="contact@northfield-inc.example", source=WEBSITE,
        #                                       other_company=None, ...),
        #                             CardEmail(value="marcandre@northfield-inc.example",
        #                                       source=LINKEDIN_EMAIL_SEARCH,
        #                                       person_name="Marc-Andre Lavoie",
        #                                       email_status="risky", other_company=None, ...)),
        #                     people=(...))
        #        跟卡片上「联系方式」那一区是同一个函数拼的，所以 bot 问的时候说的邮箱就是卡片上那几个
        contacts = card_contacts(cur, [lead_id])[lead_id]
        # 输出   profile -> CompanyProfile(website="https://northfield-inc.example", employee_count_range="2-10",
        #                                  founded_year=2021, ..., raw={...})
        #        没抓过公司详情的（只有公司名、没有 LinkedIn 公司 id 的那种）是 None
        profile = contacts_db.latest_profiles(cur, [lead_id]).get(lead_id)
        # 输出   source -> DraftSource(score=72, analysis="...", quotes=(...), evidence=(三条,))
        source = lead_db.draft_source(cur, lead_id)

    # 卡片是从这一行拼出来的，库里一定有它；没有说明 lead_id 读错了，当场停，不去给一家不存在的公司写稿
    if lead_id not in inputs or source is None:
        raise ValueError(f"leads 表里没有 id={lead_id} 这一家")
    # lead 是 LeadForContacts，_author是[AuthorRef,...]
    lead, _authors = inputs[lead_id]

    # 输入   contacts.emails，最新的在前
    # 输出   emails -> [{"value": "contact@northfield-inc.example", "source": "website", ...},
    #                   {"value": "marcandre@northfield-inc.example", "source": "linkedin_email_search",
    #                    "person_name": "Marc-Andre Lavoie", "email_status": "risky", ...}]
    #        每个邮箱带上来源和可信度那几项，第一问要把可信度直接写给用户看（request_question）。
    #        存成 dict 不存 CardEmail，理由见 state.py 的 lead 那个字段；source 存枚举的取值
    #        （"website" / "linkedin_email_search"），不存枚举对象本身，同一个理由
    #        other_company 不是 None 的不要：那是这个人在另一家公司的邮箱。
    #        例 Kestrel（lead 1518）库里只有 evan.park@rawcutstudio.example，other_company="Rawcut"
    #        -> emails=[]，bot 会说这家没有邮箱，不会让人往 Rawcut 发开发信
    emails = [
        {
            "value": e.value,
            "source": e.source.value,
            "person_name": e.person_name,
            "email_status": e.email_status,
            "catch_all": e.catch_all,
            "quality_score": e.quality_score,
        }
        for e in contacts.emails
        if e.other_company is None
    ]

    # 输入   contacts.people，卡片「联系人」那一区的每个人，顺序跟卡片一样
    # 输出   people -> [{"name": "Zach T.", "relation": "not_member", "position": None, "profile_url": …},
    #                   {"name": "Marc-Andre Lavoie", "relation": "member",
    #                    "position": "Founder & CEO", …},
    #                   {"name": "SingAlong Group", "relation": "other_page", "position": None, …}]
    #        全都留着，第三方也留：用户可能就是想发给那个发帖的人，名单上得有他才挑得到。
    #        写给第三方的，结束时列计划会提醒一句（planner._finalize_slot）。relation 存取值，理由同 source
    people = [
        {
            "name": p.name,
            "relation": p.relation.value,
            "position": p.position,
            "profile_url": p.profile_url,
        }
        for p in contacts.people
    ]

    # 下面拼 knowledge。库里的对象都摊成普通字典、列表再进 state，理由同上面的 emails
    knowledge = {
        "profile": _profile_dict(profile),
        # 输出   {"score": 72, "analysis": "North Field is building a karaoke mic ...",
        #         "quotes_mean": "创始人说在找能做小批量的工厂",
        #         "quotes": ["we need a partner for 1,000 units"]}
        "assessment": {
            "score": source.score,
            "analysis": source.analysis,
            "quotes_mean": source.quotes_mean,
            "quotes": list(source.quotes),
        },
        # 输出   [{"signal_id": 2984, "first_person": False, "author": "Zach T.", ...},
        #         {"signal_id": 2720, "first_person": True, "author": "Marc-Andre Lavoie", ...}]
        #        是不是本人说的要对着卡片上的人算，所以把 contacts.people 一起交进去（见 _evidence_dict）
        "evidence": [_evidence_dict(e, contacts.people) for e in source.evidence],
        # 输出   facts.json 整份，例 {"moq": {"value": 500, ...}, "services": {...}, "_todo": [...]}
        #        下划线开头的也留着（理由见 analyzer.load_facts）：让模型看见「交期这几项我们没有数」
        "facts": load_facts(),
        # 输出   [{"key": "third-party-evidence-no-fake-coincidence", "scope": "draft",
        #          "statement": "证据是从第三方口中得知的……", "source": "models/signal.py ……"}, 另外两条]
        #        不按渠道筛，全给：三条都是写任何一种稿子都要守的
        "avoid": [dataclasses.asdict(note) for note in load_avoid(DRAFTING_KNOWLEDGE_PATH)],
    }

    # 这一轮从 messages 第几句开始。retrieve_knowledge 是每一轮第一个节点，所以在这里记。
    # 这时 messages 里已经有 START 带进来的那句（@bot 开的、或者重开时说的那句），它属于这一轮
    #   按钮开的第一轮     messages=[]                                   -> 0
    #   @bot 开的第一轮    messages=["用户: 帮我写封邮件给他们"]          -> 0
    #   结束之后又 @ 重开  messages=["AI: 请问…", "用户: 写三份…", "AI: 好，给…写这 3 份：…", "用户: 写吧",
    #                               "用户: 给 3 号写条附言…"]                     -> 4
    #   只有 messages 往后接，别的字段每轮整份换掉，所以这一轮从哪句开始只能在这里记（见 state.py）
    round_start = len(state["messages"]) - (1 if state["request"] else 0)

    return {
        "lead": {"display_name": lead.display_name, "emails": emails, "people": people},
        "knowledge": knowledge,
        "round_start": round_start,
    }


def _profile_dict(profile: CompanyProfile | None) -> dict:
    """公司概况摊成进 state 的字典。没抓过是 {}。

    输入 -> 输出（编的例子）：
        CompanyProfile(website="https://acme-audio.example", employee_count=8, employee_count_range="2-10",
                       founded_year=2023, location="Austin, US", industries=("Consumer Electronics",),
                       phone=None,
                       raw={"tagline": "Clip-on mics for creators", "description": "Acme Audio makes ...",
                            "specialities": ["wireless audio"], ...})
            -> {"website": "https://acme-audio.example", "employee_count_range": "2-10", "founded_year": 2023,
                "location": "Austin, US", "industries": ["Consumer Electronics"],
                "tagline": "Clip-on mics for creators", "description": "Acme Audio makes ...",
                "specialities": ["wireless audio"]}
        None -> {}

    tagline / description / specialities 表里没有单独的列，从 raw（公司详情 actor 返回的整条）里取：
    写稿最用得上的就是公司自己怎么介绍自己。actor 没给的是 None / []，不猜
    employee_count（精确人数）和 phone 不要：前者跟 employee_count_range 重复，后者写稿用不到
    """
    if profile is None:
        return {}
    raw = profile.raw
    return {
        "website": profile.website,
        "employee_count_range": profile.employee_count_range,
        "founded_year": profile.founded_year,
        "location": profile.location,
        "industries": list(profile.industries),
        "tagline": raw.get("tagline") or None,
        "description": raw.get("description") or None,
        "specialities": [str(s) for s in raw.get("specialities") or []],
    }


# 发帖人是这几类，他发的帖子就算「这家的人自己说的」：确认是这家的人、这家自己的公司主页、
# 职位串写着这家还没核对的人（卡片上也是按这家的人列的）
_OWN_VOICE = (CardRelation.MEMBER, CardRelation.OWN_PAGE, CardRelation.NOT_LOOKED_UP)


def _is_own_voice(evidence: DraftEvidence, people: tuple[CardPerson, ...]) -> bool:
    """这条证据是不是这家的人自己说的。四种情况：

        通道本身就是本人（lead_type.is_first_person，例 author）         -> True
        发帖人在卡片上、关系是 _OWN_VOICE 之一                          -> True
            例 North Field 的 [2720]：通道是 repost_mentioned，发帖人 Marc-Andre Lavoie 在卡片上是 member
            例 Hushly 的 [2820]：通道是 mentioned，发帖人 sam hadid（ceo @ Hushly）在卡片上是 not_looked_up
        发帖人在卡片上、关系是别的（not_member / other_page / left / unclear）  -> False
            例 North Field 的 [2984]：Zach T. 是 not_member
        发帖人不在卡片上                                               -> False，不猜

    为什么不能只看通道：通道说的是「这条帖子是怎么挂到这家公司上的」（被 @、转发里提到），
    说不出发帖人是不是这家的人。创始人自己发帖 @ 了自家公司，通道照样是 mentioned。
    卡片上的人认发帖人要名字和帖子链接都对上：转发的帖子，转发的人和原帖的机构在卡片上共用同一个帖子链接
    （North Field 的 Marc-Andre 和 SingAlong Group），只看链接会把 SingAlong 的关系套到 Marc-Andre 头上
    """
    if evidence.lead_type.is_first_person:
        return True
    author = evidence.author.casefold()
    return any(
        p.relation in _OWN_VOICE and p.name.casefold() == author and p.post_url == evidence.url
        for p in people
    )


def _evidence_dict(evidence: DraftEvidence, people: tuple[CardPerson, ...]) -> dict:
    """一条证据摊成进 state 的字典。

    输入：一条证据，和卡片上的人（card_contacts 那份 people，拿来认发帖人是不是这家的人）
    输入 -> 输出（编的例子）：
        DraftEvidence(signal_id=2720, lead_type=LeadType.MENTIONED, author="Audio Weekly", role="",
                      when="2026-09-18", url="https://www.linkedin.com/posts/...",
                      text="Congrats to @Acme Audio on a successful campaign!",
                      reposted_author=None, reposted_text=None)
            -> {"signal_id": 2720, "lead_type": "mentioned", "first_person": False,
                "author": "Audio Weekly", "role": "", "when": "2026-09-18",
                "url": "https://www.linkedin.com/posts/...",
                "text": "Congrats to @Acme Audio on a successful campaign!",
                "reposted_author": None, "reposted_text": None}

    first_person 在这里就算好存进去：以后写开场白的模型和自审的代码都要问这件事，
    存成现成的 True / False，两边不会各算各的。怎么算见 _is_own_voice
    """
    return {
        "signal_id": evidence.signal_id,
        "lead_type": evidence.lead_type.value,
        "first_person": _is_own_voice(evidence, people),
        "author": evidence.author,
        "role": evidence.role,
        "when": evidence.when,
        "url": evidence.url,
        "text": evidence.text,
        "reposted_author": evidence.reposted_author,
        "reposted_text": evidence.reposted_text,
    }


def _email_brief(email: dict, *, with_owner: bool = True) -> str:
    """第一问里的一个邮箱，一行：地址、是谁的、可信度。说法跟卡片「联系方式」那一区一样，只是压成一行。

    输入 -> 输出（编的例子）：
        {"value": "hello@acme-audio.example", "source": "website", "person_name": None, ...}
            -> "hello@acme-audio.example：官网上公开写着的，没有另外验证过"
        {"value": "dana@acme-audio.example", "source": "linkedin_email_search", "person_name": "Dana Lee",
         "email_status": "risky", "catch_all": True, "quality_score": 60}
            -> "dana@acme-audio.example（Dana Lee 的）：不确定能不能收到；
                对方邮箱什么地址都收，没法事先确认这个地址有人在用；质量分 60 / 100"
               （真的那句是一行，这里为了排版折了）
        同一个，with_owner=False（已经列在 Dana Lee 名下，不用再说是谁的）
            -> "dana@acme-audio.example：不确定能不能收到；…"

    服务商给的状态换人话用的是卡片那张表（cards.EMAIL_STATUS_TEXT）；表里没有的值原样印，不猜意思
    """
    if email["source"] == ContactSource.WEBSITE.value:
        return f"{email['value']}：官网上公开写着的，没有另外验证过"

    whose = f"（{email['person_name']} 的）" if with_owner and email["person_name"] else ""
    # 输出   parts -> ["不确定能不能收到", "对方邮箱什么地址都收，没法事先确认这个地址有人在用",
    #                  "质量分 60 / 100"]
    #        服务商一项都没给的是 []，退成一句「从 LinkedIn 主页找到的」
    parts = []
    if email["email_status"]:
        status = email["email_status"]
        parts.append(EMAIL_STATUS_TEXT.get(status, f"服务商标的是「{status}」"))
    if email["catch_all"]:
        parts.append("对方邮箱什么地址都收，没法事先确认这个地址有人在用")
    if email["quality_score"] is not None:
        parts.append(f"质量分 {email['quality_score']} / 100")
    trust = "；".join(parts) if parts else "从 LinkedIn 主页找到的，服务商没给可信度"
    return f"{email['value']}{whose}：{trust}"


# 第一问里四种写法的说明，写清楚用户才知道该选哪种
_FORMS_TEXT = (
    "可以写四种：邮件、LinkedIn 私信、LinkedIn 加好友附言"
    "（附言 300 字符以内；对方还不是好友时私信可能收不到，附言总能送到），"
    "还有文章（不写给具体的人，把我们的卖点套到这家身上写一篇）。"
)


def request_question(lead: dict, *, max_drafts: int) -> str:
    """在用户通过点击按钮来drafting的情况下，agent第一句应该怎么表达。

    输入：state["lead"]，该公司的信息；每轮最多几份（config 里的 max_drafts_per_round）
    输出：一个例子（编的，Acme Audio：Dana Lee 有自己的邮箱，Sam Park 没有，官网上还有一个公开邮箱。
          下面每一行就是真的一行，只有第一句、写法那句和最后那句为了排版折了）

        请问你想给 Acme Audio 写哪些 outreach 信息？一次可以写几份（最多 4 份），
        每份说多少都行，没说的我来定。

        名单：
        1. Acme Audio 这家公司本身（称呼他们的团队）
           邮箱 1-1 hello@acme-audio.example：官网上公开写着的，没有另外验证过
        2. Dana Lee（Founder，这家公司的人）
           邮箱 2-1 dana@acme-audio.example：不确定能不能收到；质量分 60 / 100
        3. Sam Park（发帖的人不是这家公司的，只是在帖子里提到了它，仍可联络）

        可以写四种：邮件、LinkedIn 私信、LinkedIn 加好友附言（附言 300 字符以内；…），
        还有文章（不写给具体的人，把我们的卖点套到这家身上写一篇）。

        你可以这样回复：「给 2 号写封邮件，强调 MOQ 500，再写一篇文章讲我们能做小批量」。
        要发到哪个邮箱，说它的号就行，例如「发到 1-1」。
        没说写给谁的，写给 1 号这家公司；也可以回「看你」，让我来定。

    这家一个邮箱都没有时，写法那段后面加一句「这家公司没有找到邮箱：邮件照样能写，发之前要你自己找地址。」，
    也不说「要发到哪个邮箱」那句。卡片上一个人都没有时，例子里的「2 号」换成「1 号」。
    编号跟 plan_drafts 给模型看的是同一套：名单是 planner.target_listing（1 号永远是这家公司本身，
    这家自己的 LinkedIn 主页并进 1 号，不单独占号），邮箱是 planner.email_numbers（主人的号-第几个）。
    邮箱挂在各自主人名下、带可信度：用户不用回去翻卡片就能决定写不写邮件、发哪一个
    """
    name = lead["display_name"]
    people, emails = listed_people(lead), lead["emails"]
    lines = [
        f"请问你想给 {name} 写哪些 outreach 信息？一次可以写几份（最多 {max_drafts} 份），"
        "每份说多少都行，没说的我来定。",
        "",
        "名单：",
    ]

    # 输入   email_numbers -> [("1-1", 1, hello@ 那个), ("2-1", 2, dana@ 那个)]
    # 中间   by_owner -> {1: [("1-1", hello@ 那个)], 2: [("2-1", dana@ 那个)]}
    by_owner: dict[int, list[tuple[str, dict]]] = {}
    for number, owner, email in email_numbers(lead):
        by_owner.setdefault(owner, []).append((number, email))

    # 一、1 号这家公司本身，下面挂官网上的邮箱（和主人不在名单上的邮箱，带上是谁的）
    # 二、2 号起卡片上的人，谁有自己的邮箱就挂在他下面（不用再说是谁的）
    # 输出   "1. Acme Audio 这家公司本身（称呼他们的团队）\n   邮箱 1-1 hello@acme-audio.example：…\n
    #         2. Dana Lee（Founder，这家公司的人）\n   邮箱 2-1 dana@acme-audio.example：…"
    targets = [(COMPANY_NUMBER, company_line(lead))]
    targets += [(i, person_line(p)) for i, p in enumerate(people, start=COMPANY_NUMBER + 1)]
    for i, line in targets:
        lines.append(f"{i}. {line}")
        for number, email in by_owner.get(i, []):
            lines.append(f"   邮箱 {number} {_email_brief(email, with_owner=i == COMPANY_NUMBER)}")
    lines.append("")

    # 三、能写哪几种，和怎么回。没有邮箱也照样能写邮件，只是提一句地址要他自己找
    lines.append(_FORMS_TEXT)
    if not emails:
        lines.append("这家公司没有找到邮箱：邮件照样能写，发之前要你自己找地址。")
    who = f"{COMPANY_NUMBER + 1} 号" if people else f"{COMPANY_NUMBER} 号"
    lines.append("")
    lines.append(
        f"你可以这样回复：「给 {who}写封邮件，强调 MOQ 500，再写一篇文章讲我们能做小批量」。"
        + (f"要发到哪个邮箱，说它的号就行，例如「发到 {email_numbers(lead)[0][0]}」。" if emails else "")
        + f"没说写给谁的，写给 {COMPANY_NUMBER} 号这家公司；也可以回「看你」，让我来定。"
    )
    return "\n".join(lines)


def ask_request(state: DraftingState) -> dict:
    """问第一句「给这家写什么」，停下来等用户回答。整张图第一个 interrupt。

    输入（编的例子）：
        {"messages": [], "lead_id": 1289,
         "lead": {"display_name": "Acme Audio", "emails": [...], "people": [Dana Lee, Sam Park]},
         "request": "", ...}
    交出去的东西（外面 result["__interrupt__"][0].value）：
        {"kind": "drafting_request",
         "question": "请问你想给 Acme Audio 写哪些 outreach 信息？一次可以写几份（最多 4 份），…"}
    输出（用户回了「给 1 号写封邮件，再给 2 号写一条加好友附言」之后）：
        {"messages": ["AI: 请问你想给 Acme Audio 写哪些 outreach 信息？…",
                      "用户: 给 1 号写封邮件，再给 2 号写一条加好友附言"],
         "request": "给 1 号写封邮件，再给 2 号写一条加好友附言"}

    问和答一起写进 messages，理由同 query 的 ask_clarification：答话脱离了问题没法读，
    「1 号」只有跟问题里那张编号名单放在一起才知道是谁
    """
    # 第一行，前面不许有任何东西（理由见 langgraph学习/nodes/nodes_1a.txt 2.5 第一条）
    # 输入   state["lead"]，只读
    # 输出   挂起时这一句抛 GraphInterrupt，节点断在这里；恢复时它返回用户那句原话，
    #        例 "给 1 号写封邮件，再给 2 号写一条加好友附言"
    #
    # kind 给渲染那一头认这次该发什么；取值 ASCII，理由同 query 的 ask_clarification
    # （要拿它做 if，还会随 interrupt 的值落进 checkpoint_writes）。
    # 带 drafting_ 前缀：两张图的存档在同一套表里，直接翻 checkpoint_writes 时一眼分得出是哪张图停的；
    # 以后这张图还会有别的停处（给用户看计划、看稿），也都带这个前缀，不会跟 query 的撞名
    # _first_question 在这一行里、恢复时会跟着重跑一遍：纯函数加读一个小 yaml，不查库不调模型，重跑没有代价
    reply = interrupt({"kind": "drafting_request", "question": _first_question(state)})

    # 下面这些只有用户回话之后才会执行，每次恢复只执行一次
    # 输出   两笔写：messages 追加问和答两句，request 存 interrupt 收到的原值（不带「用户: 」前缀）
    #        问的那句重新拼一次：state["lead"] 和配置都没变，拼出来跟交出去的那句一字不差
    return {
        "messages": [f"AI: {_first_question(state)}", f"用户: {reply}"],
        "request": str(reply),
    }


def plan_drafts(state: DraftingState) -> dict:
    """定这一轮的计划：调一次模型读用户的话、替每份定下没说的，再用代码把它落到卡片上（finalize_plan）。
    这个节点不问、不停。

    输入（编的例子，按钮开的对话，用户答完第一问）：
        {"messages": ["AI: 请问你想给 Acme Audio 写哪些 outreach 信息？…",
                      "用户: 给 2 号写封邮件，强调 MOQ 500，再写一篇文章"],
         "lead": {"display_name": "Acme Audio", "emails": [...], "people": [Dana Lee, Sam Park]},
         "knowledge": {"profile": {...}, "assessment": {...}, "evidence": [[2720] Dana 本人的帖子, ...],
                       "facts": {...}, "avoid": [...]}, ...}
    输出：
        {"ai_plan": {"count_user_requested": 0, "count_user_words": "",
                         "drafts": [{"user_words": "给 2 号写封邮件，强调 MOQ 500",
                                     "recipient_user_requested": "2 号", "recipient_ai_understood": 2,
                                     "email_user_requested": "", "email_ai_understood": "",
                                     "form_user_requested": "email", "emphasis_user_requested": ["MOQ 500"],
                                     "form_ai_planned": "", "selling_points_ai_planned": ["moq"],
                                     "opening_ai_planned": 2720, "reason_ai_planned": "…"},
                                    {"user_words": "再写一篇文章", ..., "form_user_requested": "article",
                                     "selling_points_ai_planned": ["moq", "product_families"], ...}],
                         "language_user_requested": "", "tone_user_requested": "", "unsupported": []},
         "plan": {"slots": [{"number": 1, "user_words": "给 2 号写封邮件，强调 MOQ 500",
                             "recipient": {"name": "Dana Lee", ...}, "form": "email",
                             "address": "dana@acme-audio.example", "emphasis": ["MOQ 500"],
                             "selling_points": ["moq"], "opening": 2720, ...},
                            {"number": 2, "user_words": "再写一篇文章", "recipient": None,
                             "form": "article", ...}],
                  "language": "英文", "tone": "简短、专业，不用推销腔", "unsupported": [],
                  "notes": ["语言你没说，按默认用英文", ...], "ai_plans_all": False}}

    两格都存：ai_plan 是模型的原话（事后查「模型当时答的是什么」），plan 是代码落到卡片上的那一份
    （每份写给谁、发哪个地址、合并了哪几份），计划展示和以后的 write_draft 只读 plan。

    两种进来的方式，看 state["ai_plan"] 空不空：
        第一次定计划   ai_plan={}（这一轮还没定过）          -> 只读这一轮的话
        要改计划       ai_plan 是上一版（classify_plan_reply 判了 revise 绕回来的）
                       -> 另外把上一版（每项标好是计划里的第几份）和他那句修改意见（plan_feedback）
                          明写交给模型，让它在上一版上改；交回来的仍是完整的一轮，不是只交改了的那几份。
                          修改意见常常一句管好几份、还管整轮（「第 1 份改私信，再加一篇文章，都用中文」），
                          所以不按份拆开问，整句交给模型自己对到每一份
                       改完之后，他没点名的那几份要是变了，打一行日志（planner.untouched_changes），不拦

    每次都把这一轮的 messages（从 round_start 起）整段重读一遍：以后用户看了计划再改（「第 2 份写给 3 号」），
    要写什么会散在好几句里；前一轮（结束之后又 @ 重开的）说过的不算。
    它没有 interrupt，所以不会被重放，一轮只付一次模型的钱（理由同 query 的 understand_intent）。
    说了但认不出的人和邮箱、份数对不上都不问：按固定办法定下来，计划里写一句（planner.finalize_plan）。
    模型没给出好回答时 read_ai_plan 抛 AiPlanCallFailed，这一层不接：整次 invoke 失败，
    用户看到「出错了，回任意一句重试」，重试从这个节点第一行重跑（runner._recover 情况三）
    """
    # 输入   配置：模型、max_tokens、温度、超时。每次读一遍那个小 yaml，不缓存
    config = load_plan_config()
    # 输入   一个能 chat 的客户端，跟 analyzer、query 那张图同一个构造函数。
    #        tests/conftest.py 挡着它：测试里忘了换桩就当场炸，不会悄悄花钱
    client = build_llm_client()

    # 输出   AiPlan。这一句是这个节点里唯一花钱的地方。
    #        lead 和 knowledge 都给：lead 用来对编号，knowledge 用来挑卖点和开场
    #        只读这一轮的话（messages[round_start:]）：重开之后前一轮的要求不算这一轮的
    #        replan时，previous -> 见planner
    #        feedback -> "第 2 份改成私信"；第一次定计划两个都是空的
    previous = previous_drafts(state["ai_plan"], state["plan"])
    feedback = state["plan_feedback"] if previous else ""
    # 用ai做意图理解+计划
    result = read_ai_plan(
        client,
        state["messages"][state["round_start"] :],
        state["lead"],
        state["knowledge"],
        feedback=feedback,
        previous=previous,
        drafts=state["drafts"],
        plan=state["plan"],
        config=config,
    )
    # 输出   一行日志，例 [节点] plan_drafts 定了 2 份：email->2 [moq], article->0 [moq,product_families]
    parts = ", ".join(
        f"{d.form_user_requested or d.form_ai_planned or '?'}->{d.recipient_ai_understood} "
        f"[{','.join(d.selling_points_ai_planned)}]"
        for d in result.drafts
    )
    print(f"[节点] plan_drafts 定了 {len(result.drafts)} 份：{parts}")

    # 输入   模型那份摊平的字典、这家公司、配置（每轮上限、默认语言语气）
    # 输出   plan -> Plan(slots=(PlanSlot(number=1, ..., address="dana@acme-audio.example"), ...), ...)
    #        当场算、当场存：用户看到的计划和以后照着写的是同一份（理由见 planner.Plan）
    ai_plan = ai_plan_to_state(result)
    # 输出   要改计划时：他没点名的那几份被改掉了，一份一行日志，例
    #        [节点] plan_drafts 第 1 份他没点名，这一版却找不到原样的那一项（给 2 号写封邮件…）
    for note in untouched_changes(previous, ai_plan, feedback, state["drafts"]):
        print(f"[节点] plan_drafts {note}")
    # 用代码来限制规范ai给出的计划：号沿用上一版、已定稿的原样留、新的接着最大号发、算 plan_version
    # 输出   finalized -> Finalized(plan=Plan(slots=(#1, #3, #4, #5), ...), dropped=(2,))
    finalized = finalize_plan(ai_plan, state["lead"], config, previous=state["plan"], drafts=state["drafts"])
    # 输出   账本跟着改：新号建 planned、计划变了的改 planned、没交回来的记 dropped
    drafts = drafts_after_plan(state["drafts"], finalized)

    return {"ai_plan": ai_plan, "plan": plan_to_state(finalized.plan), "drafts": drafts}


def await_plan(state: DraftingState) -> dict:
    """把这一轮的计划发给用户，停下来等他回话。整张图第二个 interrupt。

    输入（编的例子，plan_drafts 刚定完）：
        {"messages": ["用户: 给 3 号写封邮件，强调 MOQ 500"], "round_start": 0,
         "lead": {"display_name": "North Field Technologies", ...}, "knowledge": {"evidence": [...], ...},
         "plan": {"slots": [{"number": 1, "form": "email", "address": "marcandre@northfield-inc.example", ...}],
                  ...}, ...}
    交出去的东西（外面 result["__interrupt__"][0].value）：
        {"kind": "drafting_plan",
         "question": "好，给 North Field Technologies 写这 1 份：\\n第 1 份：邮件，发到 marcandre@… → …\\n
                      …\\n\\n要改就直接说，例如「第 2 份写给 3 号」…；没问题回「写吧」；不写了回「算了」。"}
    输出（用户回了「第 1 份改成私信」之后）：
        {"messages": ["AI: 好，给 North Field Technologies 写这 1 份：…", "用户: 第 1 份改成私信"],
         "plan_feedback": "第 1 份改成私信"}

    计划和回话一起进 messages：判回话的 classify_plan_reply 要知道他是对着哪份计划说的；
    要改时绕回 plan_drafts，它重读这一轮的话，看得到上一版计划和他怎么改的（「第 1 份」指的是哪一份）。
    plan_feedback 另存一份原话，理由见 state.py 那个字段
    """
    # 第一行，前面不许有任何东西（理由见 langgraph学习/nodes/nodes_1a.txt 2.5 第一条）
    # 输入   state["plan"] / state["lead"] / state["knowledge"]，只读
    # 输出   挂起时抛 GraphInterrupt 断在这里；恢复时返回用户那句原话，例 "第 1 份改成私信"
    #        plan_text 恢复时跟着重跑：纯函数，不查库不调模型，拼出来一字不差
    # 把最终决定的计划给用户看，并且在plan_feedback里记录用户提回的意见
    reply = interrupt({"kind": "drafting_plan", "question": plan_text(state)})

    # 下面这些只有用户回话之后才执行，每次恢复只执行一次
    return {
        "messages": [f"AI: {plan_text(state)}", f"用户: {reply}"],
        "plan_feedback": str(reply),
    }


def classify_plan_reply(state: DraftingState) -> dict:
    """读用户对着计划回的那句话，判成改 / 同意 / 不写了三选一，写进 decision。

    输入（编的例子，用户回了「写吧」）：
        {"messages": ["用户: 给 3 号写封邮件", "AI: 好，给 North Field Technologies 写这 1 份：…", "用户: 写吧"],
         "round_start": 0, "plan_feedback": "写吧", "plan": {"slots": [{...}], ...}, ...}
    输出，三种之一：
        {"decision": "approve", "plan_feedback": ""}    同意：照这份写，并清空plan_feedback
        {"decision": "cancel", "plan_feedback": "", "verdicts": [...]}
                                                       不写了，并清空plan_feedback；之前写过、还在等他表态的
                                                       那几份各一项 cancelled，record_round 写进库
        {"decision": "revise"}                         要改：plan_feedback 已存在state里

    判成 approve / cancel 时把 plan_feedback 清掉：那句话不是修改意见，留在存档里会在下一次冒充成一条。
    计划一份都没有却判成 approve 的，改判 revise：没有东西可写，这是确定的事，在代码里挡，不靠模型。
    模型那次调用返回的 reason 只进日志，不进 state
    """
    # 输入   这一轮的话（messages[round_start:]，里面有他对着的那份计划）、以及用户resume回来的plan_feedback
    # 输出   judgement，例 ReplyJudgement(decision="approve", reason="「写吧」是同意")。这一句花钱（很少）
    judgement = read_decision(
        build_llm_client(),
        state["messages"][state["round_start"] :],
        feedback=state["plan_feedback"],
        config=load_reply_config(),
    )
    decision = judgement.decision

    # 计划是空的：没有可写的，同意也写不了，当成要改（让他说要写什么）
    if decision == "approve" and not state["plan"]["slots"]:
        print(
            f"[节点] classify_plan_reply 判成 approve，但计划一份都没有，改判 revise。"
            f"理由：{judgement.reason}"
        )
        return {"decision": "revise"}

    print(f"[节点] classify_plan_reply 判成 {decision}。理由：{judgement.reason}")
    if decision == "revise":
        return {"decision": decision}
    if decision == "cancel":
        # 不写了：之前写过、还在等他表态的那几份，交给 record_round 各记一行 cancelled（第一次定计划时没有）
        cancelled = [
            {"number": int(k), "verdict": "cancelled", "feedback": ""}
            for k in sorted(state["drafts"], key=int)
            if state["drafts"][k]["status"] == WRITTEN
        ]
        return {"decision": decision, "plan_feedback": "", "verdicts": cancelled}
    return {"decision": decision, "plan_feedback": ""}


def _first_question(state: DraftingState) -> str:
    """第一问那句话：request_question 加上配置里的每轮上限。ask_request 交出去和写进 messages 的都是它。"""
    return request_question(state["lead"], max_drafts=load_plan_config().max_drafts_per_round)


def write_draft(state: DraftingState) -> dict:
    """照计划写稿：账本里状态是 planned / rewrite 的每一份各调一次模型，写完各过一遍代码检查。

    输入：state["plan"]（照着写）、state["drafts"]（挑哪几份要写、上一版正文和意见）、lead、knowledge
    输出：{"drafts": 新账本（写了的那几份变 written、version +1）, "written_now": [3, 4, 5]}
          例（接 replan_rewrite_1a.txt 第 8 步）：#3 planned -> written v2、#4 rewrite -> written v2、
          #5 planned -> written v1；#1 approved、#2 dropped 不碰

    planned 照计划从头写（旧正文不给模型），rewrite 在上一版上改（writer.write_one）。
    一份失败（WriteCallFailed）整个节点失败，重试时整节点重跑：已经写好的那几份也重写一遍，
    多花一点钱，不会写错；节点没返回之前什么都没进存档。不写库：用户还没看过，判决凑不齐
    """
    config = load_write_config()
    client = build_llm_client()
    plan = plan_from_state(state["plan"])
    # 复制一份再改：原地改 state 里的字典，存档里看不见
    drafts = {key: copy_entry(entry) for key, entry in state["drafts"].items()}
    written: list[int] = []
    for slot in plan.slots:
        key = str(slot.number)
        if drafts[key]["status"] not in TO_WRITE:
            continue
        # 这一句花钱：一份一次，代码检查不过关的那份会多调几次
        drafts[key] = write_one(client, slot, plan, state["lead"], state["knowledge"], drafts[key], config)
        written.append(slot.number)
    print(f"[节点] write_draft 写了 {written}")
    # 加上 classify_feedback 留下的：要回来、原样再给他看一遍的那几份（平时是 []）
    return {"drafts": drafts, "written_now": sorted(set(state["written_now"]) | set(written))}


def await_write(state: DraftingState) -> dict:
    """把这一次写好的稿子发给用户，停下来等他说。整张图第三个 interrupt。

    交出去的东西：{"kind": "drafting_write", "question": messages.drafts_text(state)}
        written_now 里那几份的正文全文，加一行总览（几份已定稿、几份不要了、几份还在等）
    输出（用户回了「第 4 份短一点，其他都行」之后）：
        {"messages": ["AI: [稿子] 第 3 份 私信→Dana 第 2 版 / …", "用户: 第 4 份短一点，其他都行"],
         "draft_feedback": "第 4 份短一点，其他都行"}

    messages 里只记一句摘要，不记正文：plan_drafts 读这一轮的 messages，正文混进去等于把稿子
    塞进了改计划的 prompt（改计划不看正文）。正文在账本里
    """
    # 第一行，前面不许有任何东西（理由见 langgraph学习/nodes/nodes_1a.txt 2.5 第一条）
    reply = interrupt({"kind": "drafting_write", "question": drafts_text(state)})

    return {
        "messages": [f"AI: {drafts_summary(state)}", f"用户: {reply}"],
        "draft_feedback": str(reply),
    }


def classify_feedback(state: DraftingState) -> dict:
    """把用户看完稿子回的那句话拆到每一份，落到账本上，推出下一步。

    输入：state["draft_feedback"]、这一轮的 messages、账本、计划、lead
    输出：{"drafts": 新账本, "plan": 计划（有要回来的那份时放回去了）,
           "plan_feedback": 计划那一半（按份一行，没有是 ""）, "verdicts": 这一句判了哪几份,
           "decision": replan / rewrite / wait / done / cancel,
           "written_now": 要回来、原样再给他看的那几份（平时是 []）, "notice": 下一条消息开头那句}
          例见 feedback.Applied

    拆是模型做的（feedback.read_feedback，这一句花钱），落账本和推去向是代码做的（feedback.apply_feedback）。
    written_now 清空：再回到 await_write（wait）时只问一句，不重发正文
    """
    judgement = read_feedback(
        build_llm_client(),
        state["messages"][state["round_start"] :],
        feedback=state["draft_feedback"],
        drafts=state["drafts"],
        plan=state["plan"],
        lead=state["lead"],
        config=load_feedback_config(),
    )
    applied = apply_feedback(
        state["drafts"], state["plan"], judgement, limit=load_plan_config().max_drafts_per_round
    )
    print(f"[节点] classify_feedback 判成 {applied.decision}：{applied.verdicts}。理由：{judgement.reason}")
    return {
        "drafts": applied.drafts,
        "plan": applied.plan,
        "plan_feedback": applied.plan_feedback,
        "verdicts": applied.verdicts,
        "decision": applied.decision,
        "written_now": applied.written_now,
        "notice": applied.notice,
    }


def record_round(state: DraftingState, config: RunnableConfig) -> dict:
    """把用户刚才那句话下的判决写进 draft_revisions，一份一行。整张图唯一写我们自己表的节点。

    输入：state["verdicts"]（classify_feedback 判的；看计划时说不写了是 classify_plan_reply 给的）、
          账本（被判的那一版正文）、计划（那一份的快照）、
          messages（那句话的下标就是最后一句）、config 里的 thread_id
    输出：{}（只写库，不改 state）
          库里多的行例（第 4 步那句）：
              reply_index=7 slot=1 version=1 verdict=approved
              reply_index=7 slot=3 version=1 verdict=replan feedback="改成给 Dana 发 LinkedIn 私信"

    跟 classify_feedback 分开：那一步调模型，写完库还没返回就崩了的话，重跑会再判一次、可能判得不一样，
    库里和存档里就对不上了。这里只照存档里的判决抄，崩了重跑靠唯一键（thread_id, reply_index, slot_number）
    挡重复。被判成 dropped 的那份，计划里那一份这时还在（plan_drafts 还没跑），快照取得到
    """
    if not state["verdicts"]:
        return {}
    thread_id = config["configurable"]["thread_id"]
    # 那句话就是 await_write 刚接上的最后一句
    # 那句话就是 messages 最后一句（看稿或看计划时回的都一样），去掉「用户: 」
    rows = [
        revision_row(
            state,
            thread_id,
            number=verdict["number"],
            verdict=verdict["verdict"],
            feedback=verdict["feedback"],
            reply_index=len(state["messages"]) - 1,
            user_reply=state["messages"][-1].removeprefix("用户: "),
        )
        for verdict in state["verdicts"]
    ]
    with db.connect() as conn, conn.cursor() as cur:
        written = [draft_db.insert_revision(cur, row) for row in rows]
    print(f"[节点] record_round 写了 {sum(written)} 行（共 {len(rows)} 行，其余重跑时已经写过）")
    return {}
