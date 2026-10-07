"""P1 到 P9 那套判断的测试。输入全是库里的真行（leads 和 signals），不是编的。

只有 P9（职位串写着这家、现职不在这家）库里还没有真例子，那两条单独标明是手写的。
"""

from __future__ import annotations

import datetime as dt

import pytest

from discovery.authorship import classify_author, job_matches_lead, name_too_common, pick_current_job
from models import AuthorKind, AuthorRef, CurrentJob, LeadForContacts

COMMON = frozenset({"air", "fever", "nothing"})
WHEN = dt.datetime(2026, 9, 21, tzinfo=dt.UTC)


def _lead(lead_id, display, normalized, company_id, slug, kind="li_id"):
    return LeadForContacts(
        lead_id=lead_id,
        display_name=display,
        normalized_name=normalized,
        identity_kind=kind,
        linkedin_company_id=company_id,
        linkedin_slug=slug,
        domain=None,
    )


def _author(signal_id, name, headline, url, *, company=False, role="poster"):
    return AuthorRef(
        signal_id=signal_id,
        role=role,
        name=name,
        headline=headline,
        url=url,
        is_company_page=company,
        post_url="https://www.linkedin.com/posts/x",
        event_at=WHEN,
    )


# 库里的真行
HUSHLY = _lead(1388, "Hushly", "hushly", "100000002", "hushly")
CHATTERLY = _lead(1281, "Chatterly Inc.", "chatterly", None, None, kind="name")
BRANDS = _lead(1334, "Makers&Co", "makers&co", "200000011", "makers&co")
EARMARK = _lead(1341, "Earmark", "earmark", "100000005", "earmarkaudio")
BOOMCRATE = _lead(1285, "Boomcrate", "boomcrate", "200000014", "boomcrate")
BVF_GEORGIA = _lead(1286, "BVF Georgia", "bvf georgia", "200000010", "bvf-georgia-company")
NEW_TOWN = _lead(1289, "North Field Technologies", "north field technologies", "100000001", "northfield-inc")
AIR = _lead(1354, "Air", "air", "200000015", "airhq")

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
ARTHUR = _author(
    2718,
    "Arthur Mann",
    "CRO @ BVF | Helping Brands Launch & Scale | Crowdfunding & eCommerce Expert | Advisor | "
    "Kickstarter/Indiegogo",
    "https://www.linkedin.com/in/arthur-mann?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAAAXOLbM",
)
ROB_OWENS = _author(
    2718,
    "Rob Olsen",
    "CEO & Founder, Managing Partner & Celebrity Relations at Boomcrate",
    "https://www.linkedin.com/in/rob-olsen-100000001?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAACGCcm4",
    role="original_poster",
)
MARC_ANDRE = _author(
    2720,
    "Marc-Andre Lavoie",
    "Founder & CEO, North Field Technologies | Seratone is a plug-and-play karaoke that turns any speaker "
    "into a studio",
    "https://www.linkedin.com/in/malavoie?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAABJk2n4",
)
SINGALONG = _author(
    2720,
    "SingAlong Group",
    "2,244 followers",
    "https://www.linkedin.com/company/singalong-group/posts",
    company=True,
    role="original_poster",
)
BRANDS_PAGE = _author(
    2784, "Makers&Co", None, "https://www.linkedin.com/company/makers&co/posts", company=True
)
AUDIOXPRESS = _author(
    2789, "audioXpress", None, "https://www.linkedin.com/company/audioxpress/posts", company=True
)
LUZ = _author(
    2801,
    "Luz Cortez",
    "Executive Director of Content, The Clios",
    "https://www.linkedin.com/in/luzcortez?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAAAW2Zgk",
)


def _kind(lead, author):
    return classify_author(lead, author, common_words=COMMON, max_short_length=3)


@pytest.mark.parametrize(
    ("case", "lead", "author", "expected"),
    [
        ("P1 这家自己的主页", BRANDS, BRANDS_PAGE, AuthorKind.OWN_PAGE),
        ("P2 别的机构的主页", EARMARK, AUDIOXPRESS, AuthorKind.OTHER_PAGE),
        ("P3 职位串里有这家，headline 通道", CHATTERLY, RAVI, AuthorKind.NAMED_IN_HEADLINE),
        ("P4 职位串里有这家，mentioned 通道", HUSHLY, SAM, AuthorKind.NAMED_IN_HEADLINE),
        ("P5 职位串里没有这家", BOOMCRATE, ARTHUR, AuthorKind.NOT_NAMED),
        ("P6 转发的人", NEW_TOWN, MARC_ANDRE, AuthorKind.NAMED_IN_HEADLINE),
        ("P6 原帖作者是别的机构的主页", NEW_TOWN, SINGALONG, AuthorKind.OTHER_PAGE),
        ("P6 原帖作者是这家的创始人", BOOMCRATE, ROB_OWENS, AuthorKind.NAMED_IN_HEADLINE),
        ("P7 只对上一部分", BVF_GEORGIA, ARTHUR, AuthorKind.PARTIAL_NAME),
        ("P8 名字是常用词", AIR, LUZ, AuthorKind.NAME_TOO_COMMON),
    ],
)
def test_every_case_from_the_design_with_real_rows(case, lead, author, expected):
    assert _kind(lead, author) is expected, case


def test_boomcrate_repost_splits_into_a_third_party_and_the_founder():
    """signal 2718：Arthur 转发了 Rob Olsen 的帖子。同一条证据，两个人两种判法（P6）。

    转发的 Arthur 是众筹代运营公司的人，不查；原帖作者 Rob Olsen 职位串写着 Boomcrate 的
    CEO & Founder，要查。只看转发的人的话，这家的创始人就漏了
    """
    assert (_kind(BOOMCRATE, ARTHUR), _kind(BOOMCRATE, ROB_OWENS)) == (
        AuthorKind.NOT_NAMED,
        AuthorKind.NAMED_IN_HEADLINE,
    )


def test_a_name_only_matches_as_a_whole_word():
    """整词比：'hushly' 不许撞上 'hushlyic'。"""
    fake = _author(1, "x", "Head of hushlyic content", "https://www.linkedin.com/in/x")
    assert _kind(HUSHLY, fake) is AuthorKind.NOT_NAMED


def test_a_company_page_of_a_name_only_lead_is_compared_by_name():
    """只有名字的公司（没有 slug）：拿主页名字规范化之后比。"""
    page = _author(1, "Chatterly Inc.", None, "https://www.linkedin.com/company/chatterly/posts", company=True)
    assert _kind(CHATTERLY, page) is AuthorKind.OWN_PAGE


@pytest.mark.parametrize(
    ("name", "expected"),
    [("air", True), ("bbc", True), ("fever", True), ("hushly", False), ("bvf georgia", False)],
)
def test_name_too_common(name, expected):
    assert name_too_common(name, common_words=COMMON, max_short_length=3) is expected


# ── 花钱查过之后：现职对不对得上 ──────────────────────────────────────────────

SAM_JOB = CurrentJob(
    company_name="Hushly",
    position="Founder",
    company_id="100000002",
    company_slug="hushly",
    company_linkedin_url="https://www.linkedin.com/company/hushly/",
)
RAVI_JOB = CurrentJob(
    company_name="Chatterly Inc.",
    position="Founder and CEO",
    company_id=None,
    company_slug=None,
    company_linkedin_url=None,
)


def test_real_jobs_match_by_id_and_by_name():
    """真查回来的：sam 按公司 id 对上 Hushly；Ravi 的公司没有公司页，按名字对上 Chatterly。"""
    assert job_matches_lead(HUSHLY, SAM_JOB) is True
    assert job_matches_lead(CHATTERLY, RAVI_JOB) is True


def test_when_both_sides_have_an_id_a_same_name_does_not_rescue_it():
    """手写的：id 两边都有但不一样，名字碰巧一样也算对不上。LinkedIn 上同名公司很多。"""
    namesake = CurrentJob(
        company_name="Hushly",
        position=None,
        company_id="99999999",
        company_slug="hushly-llc",
        company_linkedin_url=None,
    )
    assert job_matches_lead(HUSHLY, namesake) is False


def test_p9_he_left_so_the_lookup_does_not_match():
    """手写的 P9：职位串写着 Hushly，查回来现职在 Spotify。挑出来的是那一份现职，对不上。"""
    spotify = CurrentJob(
        company_name="Spotify",
        position=None,
        company_id="207470",
        company_slug="spotify",
        company_linkedin_url="https://www.linkedin.com/company/spotify/",
    )
    assert pick_current_job(HUSHLY, (spotify,)) == (spotify, False)


def test_the_matching_job_is_picked_even_when_it_is_not_first():
    """几份现职里有一份在这家：记那一份，不管排第几。"""
    other = CurrentJob(
        company_name="Some Advisory",
        position=None,
        company_id="1",
        company_slug="adv",
        company_linkedin_url=None,
    )
    assert pick_current_job(HUSHLY, (other, SAM_JOB)) == (SAM_JOB, True)
    assert pick_current_job(HUSHLY, ()) == (None, False)
