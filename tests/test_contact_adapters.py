"""找联系方式那三个 adapter 的翻译：公司详情、个人主页、官网首页。

喂的是真调回来存下的原始返回（data/probe_contacts_2026-09-27/），不是手写的假数据：
字段名、哪一格会是 None、链接长什么样，都以真返回为准。手写的假数据只在真返回里没出现过的
情况下用（比如 Cloudflare 混淆、坏掉的质量分），那几条单独标明。

不连网：conftest 把 apify.run_actor 和 website 用的 httpx.get 都换成了当场炸的版本。
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import httpx
import pytest

from adapters import apify, harvestapi_company, harvestapi_profile, website
from models import ContactSource, CurrentJob, FoundContact

PROBE = pathlib.Path(__file__).resolve().parent.parent / "data" / "probe_contacts_2026-09-27"

# 这些测试喂的是真调回来存下的原始返回（含真人资料、真公司官网），公开版不带这批文件，整个文件跳过
pytestmark = pytest.mark.skipif(not PROBE.exists(), reason="真实抓取样本不随公开版发布")


def _probe(name: str) -> list[dict]:
    return json.loads((PROBE / f"{name}.json").read_text(encoding="utf-8"))


# ── 公司详情 ──────────────────────────────────────────────────────────────────


def test_hushly_company_details_translate_to_a_profile():
    """Hushly 的真返回：六格按卡片要的样子出来，总部取标了 headquarter 的那一条。"""
    item = _probe("company_hushly")[0]

    profile = harvestapi_company.to_profile(item)

    assert harvestapi_company.item_company_id(item) == "100000002"
    assert profile.website == "http://www.hushly.example"
    assert (profile.employee_count, profile.employee_count_range) == (11, "2-10")
    assert profile.founded_year == 2022
    # 两条地址里总部那一条只写了国家，另一条是 Brooklyn；取总部那条
    assert profile.location == "United States"
    assert profile.industries == ("Music",)
    assert profile.phone is None
    assert profile.raw is item


def test_two_more_real_companies_translate_without_errors():
    """Bonewave、ORBO Audio 两家真返回都翻译得出来，官网是各自登记的那个。"""
    items = {harvestapi_company.item_company_id(i): i for i in _probe("company_bonewave_orbo")}

    assert harvestapi_company.to_profile(items["100000003"]).website == "https://bonewave.example/"
    assert harvestapi_company.to_profile(items["100000004"]).website == "http://orbobrand.example"


def test_missing_or_bad_company_fields_become_none_not_errors():
    """手写的：一家几乎什么都没填、而且有两个坏数。

    坏数不能原样写进库：0011 里人数有 >= 0、成立年份有 1800-2100 的 CHECK，写进去会让整步失败
    """
    profile = harvestapi_company.to_profile(
        {"id": "1", "website": "", "employeeCount": -3, "foundedOn": {"year": 12}, "locations": []}
    )

    assert (profile.website, profile.employee_count, profile.founded_year) == (None, None, None)
    assert (profile.location, profile.employee_count_range, profile.industries) == (None, None, ())


def test_the_top_size_band_has_no_upper_bound():
    """规模档最高那一档没有上界时写成「下界+」。"""
    profile = harvestapi_company.to_profile({"id": "1", "employeeCountRange": {"start": 10001, "end": None}})
    assert profile.employee_count_range == "10001+"


def test_fetch_companies_sends_numeric_id_urls(monkeypatch):
    """拼给 actor 的是数字 id 的公司页链接，一批一次 run；空的一批不调。"""
    sent = []
    monkeypatch.setattr(apify, "run_actor", lambda actor, body, **kw: sent.append((actor, body)) or [])

    # 空的一批不起 run，记账是 0 元，不是 None
    assert harvestapi_company.fetch_companies([]).cost_usd == Decimal("0")
    harvestapi_company.fetch_companies(["100000002", "100000003"])

    assert sent == [
        (
            "harvestapi~linkedin-company",
            {
                "companies": [
                    "https://www.linkedin.com/company/100000002/",
                    "https://www.linkedin.com/company/100000003/",
                ]
            },
        )
    ]


# ── 个人主页 ──────────────────────────────────────────────────────────────────


def test_a_profile_url_from_our_signals_is_cleaned_to_one_key():
    """库里存的作者链接后面带着 ?miniProfileUrn=...，收成跟 actor 返回的 linkedinUrl 一样。

    这条红了的症状：同一个人两个链接，person_lookups 的唯一约束认不出，同一个人被花两次钱
    """
    ours = (
        "https://www.linkedin.com/in/sam-hadid-a1b2c3d4?miniProfileUrn="
        "urn%3Ali%3Afsd_profile%3AACoAAAhF1l8BtzHcOI5FyDUR06aqDMwXVtCQXik"
    )
    theirs = _probe("profile_sam_hadid")[0]["linkedinUrl"]

    assert harvestapi_profile.clean_profile_url(ours) == theirs
    assert harvestapi_profile.clean_profile_url(theirs + "/") == theirs
    assert harvestapi_profile.clean_profile_url("https://WWW.LinkedIn.com/in/rshah/") == (
        "https://www.linkedin.com/in/rshah"
    )


def test_sam_hadid_translates_with_his_job_and_his_risky_email():
    """sam hadid 的真返回：现职 Hushly 带公司页，邮箱四样可信度原样带着。"""
    item = _probe("profile_sam_hadid")[0]

    person = harvestapi_profile.to_person(item)

    assert harvestapi_profile.item_request_url(item) == "https://www.linkedin.com/in/sam-hadid-a1b2c3d4"
    assert person.name == "sam hadid"
    assert person.current_jobs == (
        CurrentJob(
            company_name="Hushly",
            position="Founder",
            company_id="100000002",
            company_slug="hushly",
            company_linkedin_url="https://www.linkedin.com/company/hushly/",
        ),
    )
    assert person.emails == (
        FoundContact(
            channel=person.emails[0].channel,
            value="sam@hushly.example",
            source=ContactSource.LINKEDIN_EMAIL_SEARCH,
            source_url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
            email_status="risky",
            catch_all=True,
            found_in_google=True,
            quality_score=75,
        ),
    )
    assert person.company_websites == ("hushly.example",)


def test_ravi_shah_has_no_email_and_a_company_without_a_page():
    """Ravi Shah 的真返回：emails 是 None（不是 []），现职公司的链接是个搜索页，换成 None。

    搜索页留着的话，下游从里面切 slug 会切出 'all'，拿去跟 leads.linkedin_slug 比
    """
    person = harvestapi_profile.to_person(_probe("profile_ravi_shah")[0])

    assert person.emails == ()
    assert person.company_websites == ()
    assert person.current_jobs == (
        CurrentJob(
            company_name="Chatterly Inc.",
            position="Founder and CEO",
            company_id=None,
            company_slug=None,
            company_linkedin_url=None,
        ),
    )


def test_a_second_job_that_is_still_going_counts_as_current():
    """手写的，照 Evan Park 真返回的结构截短：currentPosition 只给最新那一份，
    另一份同时在做的在 experience 里，结束时间写着 Present。两份都算现职，已经结束的不算，
    两边都有的那一份只留一次。

    这条红了的症状：他替 Kestrel 查回来判成「现职不在这家」，卡片写他已经不在 Kestrel，邮箱不挂
    """
    rawcut = {
        "companyName": "Rawcut",
        "position": "Partner",
        "companyId": "200000017",
        "companyUniversalName": "rawcutstudio",
        "companyLinkedinUrl": "https://www.linkedin.com/company/rawcutstudio/",
        "endDate": {"text": "Present"},
    }
    item = {
        "linkedinUrl": "https://www.linkedin.com/in/evanpark",
        "originalQuery": {"url": "https://www.linkedin.com/in/evanpark"},
        "currentPosition": [rawcut],
        "experience": [
            rawcut,
            {
                "companyName": "Kestrel",
                "position": "Co-Founder",
                "companyId": "200000028",
                "companyUniversalName": "brevitē-llc",
                "companyLinkedinUrl": "https://www.linkedin.com/company/brevit%C4%93-llc/",
                "endDate": {"text": "Present"},
            },
            {
                "companyName": "Federal Reserve Bank of New York",
                "position": "Analyst, Capital Markets",
                "companyId": "2788",
                "endDate": {"text": "Jul 2018", "year": 2018, "month": "Jul"},
            },
        ],
    }

    person = harvestapi_profile.to_person(item)

    assert [(job.company_name, job.position) for job in person.current_jobs] == [
        ("Rawcut", "Partner"),
        ("Kestrel", "Co-Founder"),
    ]


def test_a_quality_score_out_of_range_becomes_none():
    """手写的：质量分不在 0-100 里。0011 那一列有 CHECK，坏数写进去会让整步失败。"""
    item = {
        "linkedinUrl": "https://www.linkedin.com/in/x",
        "originalQuery": {"url": "https://www.linkedin.com/in/x"},
        "emails": [{"email": "x@y.com", "qualityScore": 140}, {"email": ""}],
    }

    person = harvestapi_profile.to_person(item)

    # 第二个 email 是空串，跳过
    assert [(e.value, e.quality_score) for e in person.emails] == [("x@y.com", None)]


def test_fetch_profiles_asks_for_email_search_with_the_exact_mode_text(monkeypatch):
    """模式那一串要跟 actor 的枚举原文一字不差，不然 actor 判输入非法。"""
    sent = []
    monkeypatch.setattr(apify, "run_actor", lambda actor, body, **kw: sent.append((actor, body)) or [])

    harvestapi_profile.fetch_profiles(["https://www.linkedin.com/in/rshah"])

    actor, body = sent[0]
    assert actor == "harvestapi~linkedin-profile-scraper"
    assert body["profileScraperMode"] == "Profile details + email search ($10 per 1k)"
    assert body["urls"] == ["https://www.linkedin.com/in/rshah"]


# ── 官网首页 ──────────────────────────────────────────────────────────────────


def test_hushly_homepage_gives_its_contact_email():
    """Hushly 首页真 html：跳转之后在 home.hushly.example，邮箱 contact@hushly.example 的域名跟官网一样，算这家的。"""
    page = (PROBE / "homepage_www.hushly.example.html").read_text(encoding="utf-8")

    assert website.extract_emails(page, "https://home.hushly.example") == ("contact@hushly.example",)


def test_orbo_email_only_counts_against_the_site_it_redirected_to():
    """ORBO 首页真 html：拿跳转之后的 orboaudio.example 去比才找得到；拿登记的 orbobrand.example 比就没了。

    这条就是「用跳转之后的地址」那条规矩存在的理由
    """
    page = (PROBE / "homepage_www.orbobrand.example.html").read_text(encoding="utf-8")

    assert website.extract_emails(page, "https://orboaudio.example") == ("mark@orboaudio.example",)
    assert website.extract_emails(page, "http://www.orbobrand.example") == ()


def test_emails_from_other_sites_and_image_names_are_dropped():
    """手写的：页面里混着做网站的公司的邮箱、图片文件名、大小写不同的重复。"""
    page = """
        <a href="mailto:Hello@Hushly.example">Hello@Hushly.example</a>
        <img src="logo@2x.png"> <footer>built by hi@wix.com</footer>
        <a href="mailto:%20press@hushly.example">press</a> support&#64;hushly.example.
        <p>hello@hushly.example</p>
    """

    got = website.extract_emails(page, "https://home.hushly.example")

    assert got == ("Hello@Hushly.example", "press@hushly.example", "support@hushly.example")


def test_cloudflare_hidden_emails_are_decoded():
    """手写的：开了 Cloudflare 邮箱混淆的站，页面里没有邮箱原文，只有一串十六进制。

    9df5f8f1f1f2ddf8e5fcf0edf1f8b3fef2f0 是按 Cloudflare 的做法把 hello@example.com 编出来的
    """
    page = (
        '<a href="/cdn-cgi/l/email-protection#9df5f8f1f1f2ddf8e5fcf0edf1f8b3fef2f0">'
        "[email&#160;protected]</a>"
    )

    assert website.extract_emails(page, "https://www.example.com") == ("hello@example.com",)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://home.hushly.example/about", "hushly.example"),
        ("mail.hushly.example", "hushly.example"),
        ("www.example.co.uk", "example.co.uk"),
        ("shop.brand.com.cn", "brand.com.cn"),
        ("localhost", "localhost"),
    ],
)
def test_registrable_domain(given, expected):
    assert website.registrable_domain(given) == expected


def test_fetch_homepage_follows_redirects_and_reports_the_final_url(monkeypatch):
    """跟着跳转走到底，交回跳转之后的地址；没写协议的补 http://。"""
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["follow_redirects"]))
        request = httpx.Request("GET", "https://home.hushly.example")
        return httpx.Response(200, text="<p>contact@hushly.example</p>", request=request)

    monkeypatch.setattr(website.httpx, "get", fake_get)

    page = website.fetch_homepage("hushly.example")

    assert calls == [("http://hushly.example", True)]
    assert (page.final_url, page.error) == ("https://home.hushly.example", None)
    assert "contact@hushly.example" in page.html


def test_fetch_homepage_never_raises(monkeypatch):
    """连不上、对方回 403：都交回带原因的结果，不抛。一家打不开不能拖垮这一批。"""

    def refused(url, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(website.httpx, "get", refused)
    assert website.fetch_homepage("http://x.com").error == "ConnectTimeout"

    def forbidden(url, **kwargs):
        return httpx.Response(403, request=httpx.Request("GET", "https://x.com/"))

    monkeypatch.setattr(website.httpx, "get", forbidden)
    page = website.fetch_homepage("http://x.com")
    assert (page.error, page.html, page.final_url) == ("HTTP 403", None, "https://x.com/")


def test_the_conftest_gate_blocks_real_actor_calls():
    """不要 allow_real_web 的测试调真 actor：当场炸，一分钱都不花。"""
    with pytest.raises(RuntimeError, match="allow_real_web"):
        harvestapi_company.fetch_companies(["100000002"])
    with pytest.raises(RuntimeError, match="allow_real_web"):
        website.fetch_homepage("http://www.hushly.example")
