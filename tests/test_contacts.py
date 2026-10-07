"""triggers/contacts.py 的测试：定计划、报价、整个流程的顺序和几种情况。

不连库不连网：contacts_db 的函数全换成往一本流水账里记一笔的假函数，两个 actor 换成交回
存下来的真返回（data/probe_contacts_2026-09-27/）的假函数，官网换成交回存下来的真首页。
流水账按发生的先后记，所以「先记账提交、再写结果」这种顺序能直接断言。
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from decimal import Decimal

import pytest

from adapters import apify, harvestapi_company, harvestapi_profile, website
from discovery.authorship import classify_author
from models import (
    NO_CONTACTS,
    AuthorKind,
    AuthorRef,
    CardRelation,
    CompanyProfile,
    ContactChannel,
    ContactSource,
    DomainWrite,
    FoundContact,
    LeadForContacts,
    PersonLookup,
    SavedContact,
    SavedPersonLookup,
)
from persistence import contacts_db
from triggers import contacts
from triggers.interactive_discovery.budget import MonthlyCapReached

PROBE = pathlib.Path(__file__).resolve().parent.parent / "data" / "probe_contacts_2026-09-27"

# 这些测试喂的是真调回来存下的原始返回（含真人资料、真公司官网），公开版不带这批文件，整个文件跳过
pytestmark = pytest.mark.skipif(not PROBE.exists(), reason="真实抓取样本不随公开版发布")
NOW = dt.datetime(2026, 9, 27, 18, tzinfo=dt.UTC)
CONFIG = contacts.load_contacts_config()


def _probe(name):
    return json.loads((PROBE / f"{name}.json").read_text(encoding="utf-8"))


def _lead(lead_id, display, normalized, company_id, slug):
    return LeadForContacts(
        lead_id, display, normalized, "li_id" if company_id else "name", company_id, slug, None
    )


def _author(signal_id, name, headline, url, *, company=False):
    return AuthorRef(
        signal_id, "poster", name, headline, url, company, "https://www.linkedin.com/posts/x", NOW
    )


HUSHLY = _lead(1388, "Hushly", "hushly", "100000002", "hushly")
CHATTERLY = _lead(1281, "Chatterly Inc.", "chatterly", None, None)
EARMARK = _lead(1341, "Earmark", "earmark", "100000005", "earmarkaudio")

SAM = _author(
    2820,
    "sam hadid",
    "ceo @ Hushly - a sanctuary for human sound",
    "https://www.linkedin.com/in/sam-hadid-a1b2c3d4?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAAAhF1l8",
)
RAVI = _author(
    2714,
    "Ravi Shah",
    "Founder/CEO Chatterly Inc. | Safe-listening headphones | Offset Audioᵀᴹ",
    "https://www.linkedin.com/in/rshah?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAAAAK0BM",
)
AUDIOXPRESS = _author(
    2789, "audioXpress", None, "https://www.linkedin.com/company/audioxpress/posts", company=True
)

SAM_URL = "https://www.linkedin.com/in/sam-hadid-a1b2c3d4"
RAVI_URL = "https://www.linkedin.com/in/rshah"

INPUTS = {
    1388: (HUSHLY, [SAM]),
    1281: (CHATTERLY, [RAVI]),
    1341: (EARMARK, [AUDIOXPRESS]),
}


def _saved_lookup(url):
    lookup = PersonLookup(url, "x", None, None, None, True, {})
    return SavedPersonLookup(id=1, signal_id=None, lookup=lookup, fetched_at=NOW)


# ── 定计划、报价 ──────────────────────────────────────────────────────────────


def test_the_plan_for_hushly_chatterly_earmark():
    """两家有公司 id 的抓公司详情，只有名字的 Chatterly 不抓；职位串里有自家名字的两个人查，媒体主页不查。

    主页链接去掉了 ?miniProfileUrn= 那段：拿它当「同一个人」的键
    """
    plan = contacts.plan_lookups(INPUTS, profiles={}, lookups={}, config=CONFIG)

    assert plan.companies == {"100000002": 1388, "100000005": 1341}
    assert set(plan.people) == {SAM_URL, RAVI_URL}
    assert plan.people[SAM_URL] == [(1388, SAM)]


def test_a_company_fetched_before_is_not_fetched_again():
    profile = CompanyProfile(None, None, None, None, None, (), None, {})
    plan = contacts.plan_lookups(INPUTS, profiles={1388: profile}, lookups={}, config=CONFIG)
    assert "100000002" not in plan.companies


def test_people_looked_up_before_count_toward_the_two_per_company():
    """每家最多 2 个：以前查过 2 个就一个都不再查；同一个人两条证据只占一个名额。"""
    another = _author(9, "Other Founder", "Co-founder at Hushly", "https://www.linkedin.com/in/other")
    third = _author(10, "Third", "Hushly engineer", "https://www.linkedin.com/in/third")
    inputs = {1388: (HUSHLY, [SAM, SAM, another, third])}

    fresh = contacts.plan_lookups(inputs, profiles={}, lookups={}, config=CONFIG)
    assert set(fresh.people) == {SAM_URL, "https://www.linkedin.com/in/other"}

    one_done = contacts.plan_lookups(
        inputs, profiles={}, lookups={1388: [_saved_lookup(SAM_URL)]}, config=CONFIG
    )
    # sam 查过了、不占这次的名额也不再查；剩一个名额给 another
    assert set(one_done.people) == {"https://www.linkedin.com/in/other"}

    two_done = contacts.plan_lookups(
        inputs,
        profiles={},
        lookups={1388: [_saved_lookup(SAM_URL), _saved_lookup("https://www.linkedin.com/in/zzz")]},
        config=CONFIG,
    )
    assert two_done.people == {}


def test_one_person_in_two_companies_is_paid_for_once():
    """同一个人出现在两家的证据里：只查一次，两家各记一对。"""
    both = _author(11, "Pat", "Founder, Hushly | Advisor, Chatterly", "https://www.linkedin.com/in/pat")
    plan = contacts.plan_lookups(
        {1388: (HUSHLY, [both]), 1281: (CHATTERLY, [both])}, profiles={}, lookups={}, config=CONFIG
    )
    assert list(plan.people) == ["https://www.linkedin.com/in/pat"]
    assert [lead_id for lead_id, _ in plan.people["https://www.linkedin.com/in/pat"]] == [1388, 1281]


def test_quote():
    plan = contacts.plan_lookups(INPUTS, profiles={}, lookups={}, config=CONFIG)
    # 2 家 x 0.004 + 启动费 0.00005 + 2 人 x 0.01
    assert contacts.quote_usd(plan, CONFIG) == Decimal("0.02805")
    assert contacts.quote_usd(contacts.LookupPlan(companies={}, people={}), CONFIG) == Decimal("0")


def test_the_real_config_file_loads():
    assert CONFIG.max_people_per_lead == 2
    assert CONFIG.person_with_email_usd == Decimal("0.01")
    assert "air" in CONFIG.common_words


def test_a_generic_word_in_the_company_name_does_not_count_as_a_partial_match():
    """库里的真行：Spark Creative Group（lead 1527），发帖人 James Diaz 是 Bright Idea Lab 的人。

    职位串里的 Creative 撞上公司名里的 creative。用真配置的常用词表，creative 和 group 不算数，
    只剩 bolt 拿去比，职位串里没有 -> P5「不是这家的」，不是 P7「看不出」
    """
    bolt = _lead(1527, "Spark Creative Group", "spark creative group", None, "bolt-creative-group")
    james = _author(
        3100,
        "James Diaz",
        "Creative Director at Bright Idea Lab",
        "https://www.linkedin.com/in/jamesdiaz",
    )

    kind = classify_author(
        bolt, james, common_words=CONFIG.common_words, max_short_length=CONFIG.max_short_length
    )

    assert kind is AuthorKind.NOT_NAMED


# ── 整个流程 ──────────────────────────────────────────────────────────────────


class FakeConn:
    """记下每一次提交的假连接。游标是一个什么都不做的占位：真正的读写都换成了假函数。"""

    def __init__(self, log):
        self.log = log

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))


@pytest.fixture
def world(monkeypatch):
    """库、两个 actor、官网、月度上限全换成假的，每一步往 log 里记一笔。

    输出：一个 dict，测试可以在调用前改它
        inputs / profiles / lookups / existing   四个读函数交回什么
        company_cost / people_cost                两次 actor 调用 Apify 回的真扣费（None 是没回）
        cap_error                                 过闸那一步要抛的异常，None 是放行
        homepages                                 官网地址 -> 交回的 Homepage
        domain_result                             补 leads.domain 交回什么
        log                                       按先后记下的每一步
    """
    env = {
        "inputs": dict(INPUTS),
        "profiles": {},
        "lookups": {},
        "existing": {},
        "company_cost": Decimal("0.00805"),
        "people_cost": Decimal("0.01400"),
        "cap_error": None,
        "homepages": {},
        "domain_result": DomainWrite(outcome="written", other_lead_id=None),
        "log": [],
    }
    log = env["log"]

    monkeypatch.setattr(contacts_db, "contact_inputs", lambda cur, ids: env["inputs"])
    monkeypatch.setattr(contacts_db, "latest_profiles", lambda cur, ids: dict(env["profiles"]))
    monkeypatch.setattr(contacts_db, "person_lookups_for", lambda cur, ids: env["lookups"])
    monkeypatch.setattr(contacts_db, "contacts_for", lambda cur, ids: env["existing"])

    def record_spend(cur, **kw):
        log.append(("spend", kw["actor"], kw["cost_usd"], kw["item_count"]))

    def insert_company_profile(cur, lead_id, profile, **kw):
        log.append(("profile", lead_id, profile.website))
        return 1

    def insert_person_lookup(cur, lead_id, lookup, **kw):
        log.append(("lookup", lead_id, lookup.person_linkedin_url, lookup.matches_lead))
        return 7, True

    def insert_contact(cur, lead_id, contact, **kw):
        log.append(("contact", lead_id, contact.value, str(contact.source), kw["person_lookup_id"]))
        return True

    def set_domain(cur, lead_id, domain):
        log.append(("domain", lead_id, domain))
        return env["domain_result"]

    monkeypatch.setattr(contacts_db, "record_spend", record_spend)
    monkeypatch.setattr(contacts_db, "insert_company_profile", insert_company_profile)
    monkeypatch.setattr(contacts_db, "insert_person_lookup", insert_person_lookup)
    monkeypatch.setattr(contacts_db, "insert_contact", insert_contact)
    monkeypatch.setattr(contacts_db, "set_domain_if_empty", set_domain)

    # 两个 actor：交回存下来的真返回。Earmark 那家没存真返回，actor 就只「返回」Hushly 一家
    def fetch_companies(ids):
        log.append(("fetch_companies", sorted(ids)))
        items = [i for i in _probe("company_hushly") if i["id"] in ids]
        return apify.ActorRun(items=items, cost_usd=env["company_cost"], charged_events={})

    def fetch_profiles(urls):
        log.append(("fetch_profiles", sorted(urls)))
        items = [*_probe("profile_sam_hadid"), *_probe("profile_ravi_shah")]
        items = [i for i in items if harvestapi_profile.item_request_url(i) in urls]
        return apify.ActorRun(items=items, cost_usd=env["people_cost"], charged_events={})

    monkeypatch.setattr(harvestapi_company, "fetch_companies", fetch_companies)
    monkeypatch.setattr(harvestapi_profile, "fetch_profiles", fetch_profiles)

    hushly_html = (PROBE / "homepage_www.hushly.example.html").read_text(encoding="utf-8")
    env["homepages"]["http://www.hushly.example"] = website.Homepage(
        "http://www.hushly.example", "https://home.hushly.example", hushly_html, None
    )

    def fetch_homepage(url):
        log.append(("homepage", url))
        return env["homepages"].get(url, website.Homepage(url, None, None, "ConnectTimeout"))

    monkeypatch.setattr(website, "fetch_homepage", fetch_homepage)

    def check_cap(cur, **kw):
        log.append(("cap", Decimal(str(kw["estimate_usd"]))))
        if env["cap_error"]:
            raise env["cap_error"]
        return 0.5

    monkeypatch.setattr(contacts, "check_monthly_cap", check_cap)
    return env


def _run(env):
    return contacts.find_contacts(FakeConn(env["log"]), [1388, 1281, 1341], run_id=45, now=NOW)


def test_the_whole_flow_for_hushly_chatterly_earmark(world):
    """正常一批：过闸、公司详情、查人、官网，每一步花钱的都是先记账提交、再写结果提交。"""
    report = _run(world)
    log = world["log"]

    steps = [entry[0] for entry in log]
    assert steps == [
        "cap",
        "fetch_companies", "spend", "commit", "profile", "commit",
        "fetch_profiles", "spend", "commit", "lookup", "contact", "lookup", "commit",
        "homepage", "contact", "domain", "commit",
    ]  # fmt: skip

    assert ("cap", Decimal("0.02805")) in log
    # 记账用 Apify 回的真值，不是报价
    assert ("spend", "harvestapi/linkedin-company", Decimal("0.00805"), 1) in log
    assert ("spend", "harvestapi/linkedin-profile-scraper", Decimal("0.01400"), 2) in log
    # sam 对上 Hushly，他的邮箱挂上，指回查人那一行；Ravi 对上 Chatterly，没找到邮箱
    assert ("lookup", 1388, SAM_URL, True) in log
    assert ("contact", 1388, "sam@hushly.example", "linkedin_email_search", 7) in log
    assert ("lookup", 1281, RAVI_URL, True) in log
    # 官网：Hushly 跳到 home.hushly.example，比邮箱、取域名都按跳转之后落到的那个地址
    assert ("contact", 1388, "contact@hushly.example", "website", None) in log
    assert ("domain", 1388, "hushly.example") in log

    assert report.spent_usd == Decimal("0.02205")
    # 三家以前都没有邮箱：这一次是第一次找到，不算「邮箱变了」
    assert report.changed_emails == {}
    assert (report.companies_fetched, report.people_looked_up, report.people_matched) == (1, 2, 2)
    assert (report.emails_from_people, report.emails_from_websites) == (1, 1)
    # Earmark 付了钱但 actor 没返回（测试里没存它的真返回），记进 missing
    assert report.missing == {"100000005": 1341}


def test_p9_a_person_who_left_gets_a_lookup_row_but_no_email(world):
    """sam 的职位串写着 Hushly，但这家换成一个别的公司 id：现职对不上，邮箱不挂。"""
    other = _lead(1388, "Hushly", "hushly", "55555555", "hushly-other")
    world["inputs"] = {1388: (other, [SAM])}

    report = _run(world)

    assert ("lookup", 1388, SAM_URL, False) in world["log"]
    assert not any(e[0] == "contact" and e[2] == "sam@hushly.example" for e in world["log"])
    assert (report.people_looked_up, report.people_matched, report.emails_from_people) == (1, 0, 0)


def test_over_the_cap_spends_nothing_but_still_reads_known_homepages(world):
    """超了月度上限：两个 actor 一个都不调，报告里写明原因；以前抓过概况的那家照抓官网（不花钱）。"""
    world["cap_error"] = MonthlyCapReached(
        spent_usd=9.99,
        estimate_usd=0.03,
        cap_usd=10.0,
        since=dt.datetime(2026, 9, 21, tzinfo=dt.UTC),
        resets_at=dt.datetime(2026, 10, 21, tzinfo=dt.UTC),
    )
    world["profiles"] = {
        1388: CompanyProfile("http://www.hushly.example", 11, "2-10", 2022, "United States", ("Music",), None, {})
    }

    report = _run(world)
    steps = [e[0] for e in world["log"]]

    assert "fetch_companies" not in steps and "fetch_profiles" not in steps and "spend" not in steps
    assert "超过上限" in report.blocked_by_cap
    assert report.spent_usd == Decimal("0")
    assert ("homepage", "http://www.hushly.example") in world["log"]


def test_missing_charge_from_apify_is_booked_at_list_price(world):
    """Apify 没回扣费：按价格表记一笔，不是不记。不记的话月度上限就漏了这一次。"""
    world["company_cost"] = None

    _run(world)

    # 2 家 x 0.004 + 启动费 0.00005
    assert ("spend", "harvestapi/linkedin-company", Decimal("0.00805"), 1) in world["log"]


def test_a_homepage_already_mined_is_not_fetched_again(world):
    """以前从官网找到过邮箱的那家不再抓官网。"""
    found = FoundContact(
        "email",
        "contact@hushly.example",
        ContactSource.WEBSITE,
        "https://home.hushly.example",
        None,
        None,
        None,
        None,
    )
    world["existing"] = {1388: [SavedContact(id=12, contact=found, person_lookup_id=None, found_at=NOW)]}

    _run(world)

    assert not any(e[0] == "homepage" for e in world["log"])


def test_a_broken_homepage_and_a_taken_domain_are_reported(world):
    """官网打不开记进 homepage_errors；域名被别家占了记进 domain_conflicts。两者都不让整步失败。"""
    world["homepages"]["http://www.hushly.example"] = website.Homepage(
        "http://www.hushly.example", None, None, "ConnectTimeout"
    )
    report = _run(world)
    assert report.homepage_errors == {1388: "ConnectTimeout"}

    world["log"].clear()
    hushly_html = (PROBE / "homepage_www.hushly.example.html").read_text(encoding="utf-8")
    world["homepages"]["http://www.hushly.example"] = website.Homepage(
        "http://www.hushly.example", "https://home.hushly.example", hushly_html, None
    )
    world["domain_result"] = DomainWrite(outcome="taken", other_lead_id=1288)
    report = _run(world)
    assert report.domain_conflicts == {1388: 1288}


def test_nothing_to_do_does_not_touch_the_cap_or_the_actors(world):
    """一批里没有要花钱查的：不过闸、不调 actor。"""
    world["inputs"] = {1341: (EARMARK, [AUDIOXPRESS])}
    world["profiles"] = {1341: CompanyProfile(None, None, None, None, None, (), None, {})}

    report = _run(world)

    assert [e[0] for e in world["log"]] == []
    assert report.spent_usd == Decimal("0")


def _website_email(value):
    found = FoundContact(
        ContactChannel.EMAIL, value, ContactSource.WEBSITE, "https://home.hushly.example", None, None, None, None
    )
    return SavedContact(id=12, contact=found, person_lookup_id=None, found_at=NOW)


def test_a_new_address_for_a_lead_that_already_had_one_is_reported(world):
    """Hushly 以前从官网找到过 contact@hushly.example，这次查 sam hadid 又找到 sam@hushly.example：
    新旧都存，报告里记一笔等人看。"""
    world["inputs"] = {1388: (HUSHLY, [SAM])}
    world["existing"] = {1388: [_website_email("contact@hushly.example")]}

    report = _run(world)

    assert ("contact", 1388, "sam@hushly.example", "linkedin_email_search", 7) in world["log"]
    assert report.changed_emails == {1388: (["sam@hushly.example"], ["contact@hushly.example"])}


def test_the_same_address_from_another_source_is_not_a_change(world):
    """以前官网上就有 SAM@hushly.example，这次个人主页上找到 sam@hushly.example：地址没变（不分大小写），不记。"""
    world["inputs"] = {1388: (HUSHLY, [SAM])}
    world["existing"] = {1388: [_website_email("SAM@hushly.example")]}

    report = _run(world)

    assert report.changed_emails == {}


def test_an_actor_failure_skips_that_step_but_the_rest_still_runs(world, monkeypatch):
    """公司详情那一次 run 失败：不往外抛，记进 actor_errors；查人照查，官网照抓（以前有概况的那家）。

    这条红了的症状：Apify 一挂，推卡那一步跟着崩，这一批一张卡都发不出去
    """

    def failing(ids):
        raise RuntimeError("run abc 结束状态为 FAILED")

    monkeypatch.setattr(harvestapi_company, "fetch_companies", failing)
    world["profiles"] = {
        1388: CompanyProfile("http://www.hushly.example", 11, "2-10", 2022, "United States", ("Music",), None, {})
    }

    report = _run(world)
    steps = [e[0] for e in world["log"]]

    assert report.actor_errors == {"company_profile": "RuntimeError: run abc 结束状态为 FAILED"}
    assert "rollback" in steps
    assert "fetch_profiles" in steps
    assert ("homepage", "http://www.hushly.example") in world["log"]


def test_a_website_on_a_shared_platform_is_not_taken_as_the_company_domain(world):
    """官网跳到 linktr.ee：不找邮箱、不写 leads.domain，记进 shared_hosts。

    这条红了的症状：leads.domain 写成 linktr.ee，第二家也填 linktr.ee 的公司被误报成同一家
    """
    world["inputs"] = {1388: (HUSHLY, [])}
    world["homepages"]["http://www.hushly.example"] = website.Homepage(
        "http://www.hushly.example", "https://linktr.ee/hushly", "<a href='mailto:help@linktr.ee'>help</a>", None
    )

    report = _run(world)

    assert report.shared_hosts == {1388: "linktr.ee"}
    assert not any(e[0] in ("contact", "domain") for e in world["log"])


# ── 拼卡片：card_contacts ─────────────────────────────────────────────────────


def _saved_sam(matches=True, company="Hushly"):
    """sam hadid 那一行查人记录，raw 用存下来的真返回（职位从里面取）。"""
    lookup = PersonLookup(
        person_linkedin_url=SAM_URL,
        person_name="sam hadid",
        headline="ceo @ Hushly - a sanctuary for human sound",
        current_company_name=company,
        current_company_linkedin_url="https://www.linkedin.com/company/hushly/",
        matches_lead=matches,
        raw=_probe("profile_sam_hadid")[0],
    )
    return SavedPersonLookup(id=7, signal_id=2820, lookup=lookup, fetched_at=NOW)


def _card_world(monkeypatch, *, inputs, profiles=None, lookups=None, saved=None):
    monkeypatch.setattr(contacts_db, "contact_inputs", lambda cur, ids: inputs)
    monkeypatch.setattr(contacts_db, "latest_profiles", lambda cur, ids: profiles or {})
    monkeypatch.setattr(contacts_db, "person_lookups_for", lambda cur, ids: lookups or {})
    monkeypatch.setattr(contacts_db, "contacts_for", lambda cur, ids: saved or {})


def test_card_contacts_for_hushly_after_one_round(monkeypatch):
    """Hushly 找过一次之后：官网用 leads.domain，两个邮箱（官网的、sam 的），sam 是这家的人、职位 Founder。"""
    hushly = LeadForContacts(1388, "Hushly", "hushly", "li_id", "100000002", "hushly", "hushly.example")
    website_email = _website_email("contact@hushly.example")
    sam_email = SavedContact(
        id=13,
        contact=FoundContact(
            ContactChannel.EMAIL,
            "sam@hushly.example",
            ContactSource.LINKEDIN_EMAIL_SEARCH,
            SAM_URL,
            "risky",
            True,
            True,
            75,
        ),
        person_lookup_id=7,
        found_at=NOW,
    )
    profile = CompanyProfile("http://www.hushly.example", 11, "2-10", 2022, "United States", ("Music",), None, {})
    _card_world(
        monkeypatch,
        inputs={1388: (hushly, [SAM, SAM])},
        profiles={1388: profile},
        lookups={1388: [_saved_sam()]},
        saved={1388: [sam_email, website_email]},
    )

    got = contacts.card_contacts(object(), [1388])[1388]

    assert got.website_url == "https://hushly.example"
    assert got.company_page_url == "https://www.linkedin.com/company/hushly/"
    assert got.profile == profile
    assert [(e.value, e.person_name) for e in got.emails] == [
        ("sam@hushly.example", "sam hadid"),
        ("contact@hushly.example", None),
    ]
    # 同一个人两条证据只留一行；职位从查人那一行的原始返回里取
    assert [(p.name, p.relation, p.position) for p in got.people] == [
        ("sam hadid", CardRelation.MEMBER, "Founder")
    ]
    assert got.people[0].profile_url == SAM_URL


def test_card_contacts_relations_before_and_after_looking_up(monkeypatch):
    """没查过是 NOT_LOOKED_UP；查了对不上是 LEFT（带他现在那家）；别的机构主页是 OTHER_PAGE。"""
    _card_world(monkeypatch, inputs={1388: (HUSHLY, [SAM])})
    assert contacts.card_contacts(object(), [1388])[1388].people[0].relation is CardRelation.NOT_LOOKED_UP

    _card_world(monkeypatch, inputs={1388: (HUSHLY, [SAM])}, lookups={1388: [_saved_sam(False, "Spotify")]})
    left = contacts.card_contacts(object(), [1388])[1388].people[0]
    assert (left.relation, left.current_company) == (CardRelation.LEFT, "Spotify")

    _card_world(monkeypatch, inputs={1341: (EARMARK, [AUDIOXPRESS])})
    page = contacts.card_contacts(object(), [1341])[1341]
    assert page.people[0].relation is CardRelation.OTHER_PAGE
    assert page.emails == () and page.profile is None
    # 作者链接是 audioXpress 的帖子列表 .../company/audioxpress/posts，卡片上印公司页本身
    assert page.people[0].profile_url == "https://www.linkedin.com/company/audioxpress/"


def _saved_evan():
    """Evan Park 替 Kestrel 那一行查人记录。raw 照他的真返回截短：两份现职，只有 Rawcut 登记了官网。"""
    rawcut = {
        "companyName": "Rawcut",
        "position": "Partner",
        "companyId": "200000017",
        "companyUniversalName": "rawcutstudio",
        "companyLinkedinUrl": "https://www.linkedin.com/company/rawcutstudio/",
        "company": {"name": "Rawcut", "website": "https://rawcutstudio.example/"},
        "endDate": {"text": "Present"},
    }
    kestrel = {
        "companyName": "Kestrel",
        "position": "Co-Founder",
        "companyId": "200000028",
        "companyUniversalName": "brevitē-llc",
        "companyLinkedinUrl": "https://www.linkedin.com/company/brevit%C4%93-llc/",
        "endDate": {"text": "Present"},
    }
    raw = {
        "linkedinUrl": "https://www.linkedin.com/in/evanpark",
        "originalQuery": {"url": "https://www.linkedin.com/in/evanpark"},
        "currentPosition": [rawcut],
        "experience": [rawcut, kestrel],
    }
    lookup = PersonLookup(
        person_linkedin_url="https://www.linkedin.com/in/evanpark",
        person_name="Evan Park",
        headline="Co-Founder @ Kestrel",
        current_company_name="Kestrel",
        current_company_linkedin_url="https://www.linkedin.com/company/brevit%C4%93-llc/",
        matches_lead=True,
        raw=raw,
    )
    return SavedPersonLookup(id=5, signal_id=3000, lookup=lookup, fetched_at=NOW)


def test_an_email_on_his_other_jobs_domain_names_that_company():
    """邮箱域名跟这家对不上、跟他另一份现职的官网对得上：给那一家的名字；其余都是 None。"""
    kestrel = _lead(1518, "Kestrel", "brevitē", None, None)
    evan = _saved_evan()

    # 一、Kestrel 不知道官网，邮箱是 Rawcut 的域名 -> 'Rawcut'
    assert contacts._other_company("evan.park@rawcutstudio.example", evan, kestrel, None) == "Rawcut"
    # 二、邮箱就是这家官网的域名 -> None（哪怕他另一份现职也是这个域名，也以这家为准）
    assert contacts._other_company("evan@kestrel.example", evan, kestrel, "kestrel.example") is None
    # 三、哪一家的官网都对不上 -> None，不猜
    assert contacts._other_company("evan@gmail.com", evan, kestrel, None) is None
    # 官网来的邮箱没有查人那一行 -> None
    assert contacts._other_company("info@rawcutstudio.example", None, kestrel, None) is None


def test_card_contacts_for_a_lead_not_in_the_database_is_empty(monkeypatch):
    """库里没有这一家：给 NO_CONTACTS，不缺键。"""
    _card_world(monkeypatch, inputs={})
    assert contacts.card_contacts(object(), [9999]) == {9999: NO_CONTACTS}


# ── 这一跑要找哪几家：find_contacts_for_run ──────────────────────────────────


@pytest.mark.parametrize(("push_below_line", "min_score"), [(True, None), (False, 60)])
def test_the_leads_to_contact_follow_the_push_below_line_switch(monkeypatch, push_below_line, min_score):
    """没过线的也推卡时，这一跑分析过的全找（不看分数）；只推过线的时，只找过线的。

    口径跟推卡对不上的后果：找了不推卡的是白花钱，推了卡没找的卡片上联系方式是空的
    """
    import dataclasses

    from delivery.cards import load_card_config

    config = dataclasses.replace(load_card_config(), push_below_line=push_below_line)
    asked = {}

    def fake_leads(cur, run_id, *, min_score):
        asked["args"] = (run_id, min_score)
        return [1285, 1388]

    def fake_find(conn, lead_ids, *, run_id, now):
        asked["found"] = (list(lead_ids), run_id)
        return contacts.ContactsReport(lead_count=len(lead_ids))

    monkeypatch.setattr(contacts, "load_card_config", lambda: config)
    monkeypatch.setattr(contacts_db, "leads_to_contact", fake_leads)
    monkeypatch.setattr(contacts, "find_contacts", fake_find)

    report = contacts.find_contacts_for_run(FakeConn([]), run_id=45, now=NOW)

    assert asked["args"] == (45, min_score)
    assert asked["found"] == ([1285, 1388], 45)
    assert report.lead_count == 2
