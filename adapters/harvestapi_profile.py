"""HarvestAPI LinkedIn 个人主页 + 找邮箱（Apify actor: harvestapi/linkedin-profile-scraper）。

只做两件事：拿个人主页链接去查（花钱）、把返回的一条翻译成 ScrapedPerson。
不判断这个人是不是某家公司的人、不算钱、不写库，那些在 triggers/contacts.py。

两种模式，枚举原文照抄（actor 的 input schema 里 profileScraperMode 的 enum）：

    'Profile details no email ($4 per 1k)'         $0.004 一个人
    'Profile details + email search ($10 per 1k)'  $0.01 一个人，我们用这个

这个 actor 没有启动费这一项（pricingInfos 里只有 profile 和 profile_with_email 两个事件）。

字段（例子是 sam hadid 的原始返回；他和 Ravi Shah 两个人的原始返回存在
data/probe_contacts_2026-09-27/，测试拿它们当输入）：

    linkedinUrl       'https://www.linkedin.com/in/sam-hadid-a1b2c3d4'，干净的，不带问号后面那段
    firstName / lastName   'sam' / 'hadid'
    headline          'ceo @ Hushly -  a sanctuary for human sound'。注意中间是两个空格，
                      跟我们库里 signals.actor_headline 存的（一个空格）不完全一样
    currentPosition   列表，经历里标着「至今」的每一份工作。每一份有 companyName /
                      companyId / companyUniversalName / companyLinkedinUrl
    emails            找到了是列表，每个 {email, status, catchAllDomain, foundInGoogleSearch,
                      qualityScore, validEmailServer, free}；注意找不到时是 None，不是 []
    companyWebsites   [{"url": "http://www.hushly.example", "domain": "hushly.example", ...}]，没有是 []
    originalQuery     {"url": <我们传进去的那个链接>}，拿它把返回对回请求
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlsplit

# 引模块不引函数：tests/conftest.py 要把 apify.run_actor 换成当场炸的版本，from 引进来的名字
# import 那一刻就绑死了，换不到
from adapters import apify
from models import ContactChannel, ContactSource, CurrentJob, FoundContact, ScrapedPerson

ACTOR_ID = "harvestapi~linkedin-profile-scraper"

# 枚举原文照抄，一个字都不能改：actor 按字符串全等认模式，改了会被判成输入非法
MODE_WITH_EMAIL = "Profile details + email search ($10 per 1k)"


def clean_profile_url(url: str) -> str:
    """把个人主页链接收成一种写法，当「同一个人」的键用。

    输入：'https://www.linkedin.com/in/sam-hadid-a1b2c3d4?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAA...'
    输出：'https://www.linkedin.com/in/sam-hadid-a1b2c3d4'

    为什么必须收：我们库里 signals.actor_url 存的链接后面带着 ?miniProfileUrn=...，那一段每次
    采集可能不一样。不收的话同一个人会有两个链接，person_lookups 那个「同一个人只查一次」的
    唯一约束就认不出来，同一个人会被花两次钱。

    三步：去掉问号和井号后面的、去掉结尾的斜杠、host 改小写（路径不改大小写，slug 里可能
    有大写，LinkedIn 那边区不区分没核过，不动它最稳）
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return f"{parts.scheme or 'https'}://{parts.netloc.lower()}{path}"


def fetch_profiles(profile_urls: list[str]) -> apify.ActorRun:
    """一次 run 查一批人（带找邮箱），返回原始 item 和这一次真扣的钱。这一步花钱。

    多少钱要看找没找到邮箱：

        sam hadid    找到了 sam@hushly.example   按 profile_with_email 收 $0.01
        Ravi Shah  没找到                   只按 profile 收 $0.004

    所以「每人 $0.01」只是上限，真扣多少看 ActorRun.cost_usd

    输入：['https://www.linkedin.com/in/sam-hadid-a1b2c3d4', 'https://www.linkedin.com/in/rshah']
          要先过 clean_profile_url
    输出：ActorRun(items=[原始 item, ...], cost_usd=Decimal("0.01400"),
                   charged_events={"profile": 1, "profile_with_email": 1})

    返回的条数可能比传进去的少（主页被删了、链接不对），顺序也不保证，调用方用
    item_request_url 对回去
    """
    if not profile_urls:
        # 一个人都不用查：不起 run，一分钱不花
        return apify.ActorRun(items=[], cost_usd=Decimal("0"), charged_events={})

    # 输入   两个链接
    # 输出   {"urls": [两个链接], "profileScraperMode": "Profile details + email search ($10 per 1k)"}
    return apify.run_actor(ACTOR_ID, {"urls": profile_urls, "profileScraperMode": MODE_WITH_EMAIL})


def item_request_url(item: dict) -> str:
    """一条返回是查哪个链接查回来的。

    输入：{"originalQuery": {"url": "https://www.linkedin.com/in/rshah"}, "linkedinUrl": ...}
    输出：'https://www.linkedin.com/in/rshah'

    用 originalQuery 不用 linkedinUrl：后者是 LinkedIn 那边的规范写法，个人改过 slug 时
    会跟我们传进去的不一样，就对不回去了。两个都过一遍 clean_profile_url
    """
    return clean_profile_url(item["originalQuery"]["url"])


def to_person(item: dict) -> ScrapedPerson:
    """把一条返回翻译成 ScrapedPerson。纯函数，不连网。

    输入（sam hadid 的原始返回，只列用到的键）：
        {"linkedinUrl": "https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
         "firstName": "sam", "lastName": "hadid",
         "headline": "ceo @ Hushly -  a sanctuary for human sound",
         "currentPosition": [{"companyName": "Hushly", "companyId": "100000002",
                              "companyUniversalName": "hushly",
                              "companyLinkedinUrl": "https://www.linkedin.com/company/hushly/", ...}],
         "emails": [{"email": "sam@hushly.example", "status": "risky", "catchAllDomain": True,
                     "foundInGoogleSearch": True, "qualityScore": 75, ...}],
         "companyWebsites": [{"domain": "hushly.example", ...}], ...}
    输出：
        ScrapedPerson(linkedin_url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
                      name="sam hadid", headline="ceo @ Hushly -  a sanctuary for human sound",
                      current_jobs=(CurrentJob(company_name="Hushly", company_id="100000002",
                                               company_slug="hushly",
                                               company_linkedin_url="https://www.linkedin.com/company/hushly/"),),
                      emails=(FoundContact(value="sam@hushly.example", email_status="risky", ...),),
                      company_websites=("hushly.example",), raw=<整条原样>)
    """
    linkedin_url = clean_profile_url(item.get("linkedinUrl") or item["originalQuery"]["url"])

    # 输入   firstName="sam"  lastName="hadid"
    # 输出   "sam hadid"。两个都没有是 None；只有一个就只用那一个
    name = " ".join(part for part in (item.get("firstName"), item.get("lastName")) if part) or None

    return ScrapedPerson(
        linkedin_url=linkedin_url,
        name=name,
        headline=item.get("headline") or None,
        current_jobs=tuple(job for job, _ in current_jobs_with_websites(item)),
        # emails 找不到时是 None 不是 []，`or ()` 两种都接住
        emails=tuple(
            _email(entry, linkedin_url) for entry in item.get("emails") or () if entry.get("email")
        ),
        # 输入   [{"url": "http://www.hushly.example", "domain": "hushly.example", "validEmailServer": True}]
        # 输出   ("hushly.example",)
        company_websites=tuple(
            site["domain"] for site in item.get("companyWebsites") or () if site.get("domain")
        ),
        raw=item,
    )


def current_jobs_with_websites(item: dict) -> tuple[tuple[CurrentJob, str | None], ...]:
    """这个人所有的现职：currentPosition 那几份，加上经历里结束时间写着 Present 的，同一份只留一次。
    每份连带那家公司在 LinkedIn 上登记的官网。

    为什么不能只看 currentPosition：它只给最新开始的那一份。Evan Park 的实际返回：
        currentPosition   [Rawcut / Partner（2026 年 4 月起）]
        experience        Rawcut / Partner          2026 年 4 月 -> Present
                          Kestrel / Co-Founder          2018 年 7 月 -> Present
                          Federal Reserve Bank of New York / Analyst   2016 -> 2018
    只看 currentPosition 的话，他替 Kestrel 查回来是「现职不在这家」，卡片写他已经不在 Kestrel，
    他的邮箱也不挂 —— 其实他两份工作同时在做。创始人常常这样：一边做自己的公司，一边还有份正职

    输入：一条原始返回，只用 currentPosition 和 experience 两个键（两边每一项的字段是一样的）
    输出：((CurrentJob("Rawcut", "Partner", "200000017", ...), "https://rawcutstudio.example/"),
           (CurrentJob("Kestrel", "Co-Founder", "200000028", ...), None))
          currentPosition 的排在前面。官网取那一项里 company.website，没登记是 None

    同一份怎么认：公司 id 加职位；没有公司 id 的用公司名加职位。
        Rawcut / Partner 两边都有，只留一次。
        Moss Media Group 的 Jamie Cole 在经历里有两行一模一样的现职，也只留一次

    官网是拿来认邮箱是哪一家的：找邮箱的服务商给 Evan Park 找到的是 evan.park@rawcutstudio.example，
    域名跟他 Rawcut 那份现职的官网一样，卡片上要写明这是他在 Rawcut 的邮箱（triggers/contacts.py）
    """
    jobs: list[tuple[CurrentJob, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()

    # 输入   experience 的一项 {"companyName": "Kestrel", "endDate": {"text": "Present"}, ...}
    # 输出   留下；endDate 是 {"text": "Jul 2018", "year": 2018, ...} 的是已经结束的，不要
    present = [
        job for job in item.get("experience") or () if (job.get("endDate") or {}).get("text") == "Present"
    ]
    for raw_job in [*(item.get("currentPosition") or ()), *present]:
        job = _current_job(raw_job)
        key = (job.company_id or job.company_name, job.position)
        if key in seen:
            continue
        seen.add(key)
        # 输入   raw_job["company"]["website"] = "https://rawcutstudio.example/"
        # 输出   "https://rawcutstudio.example/"；没有 company 或没登记官网是 None
        jobs.append((job, (raw_job.get("company") or {}).get("website") or None))
    return tuple(jobs)


def _current_job(job: dict) -> CurrentJob:
    """currentPosition（或 experience）里的一份工作翻译成 CurrentJob。

    两种：

        公司有 LinkedIn 公司页
        输入   {"companyName": "Hushly", "position": "Founder", "companyId": "100000002",
                "companyUniversalName": "hushly", "companyLinkedinUrl": "https://www.linkedin.com/company/hushly/"}
        输出   CurrentJob("Hushly", "Founder", "100000002", "hushly", "https://www.linkedin.com/company/hushly/")

        公司没有公司页，LinkedIn 给的是一个搜索页链接
        输入   {"companyName": "Chatterly Inc.", "position": "Founder and CEO", "companyId": None,
                "companyUniversalName": None,
                "companyLinkedinUrl": "https://www.linkedin.com/search/results/all/?keywords=Chatterly+Inc%2E"}
        输出   CurrentJob("Chatterly Inc.", "Founder and CEO", None, None, None)
               搜索页不是公司页，换成 None。留着的话下游从里面切 slug 会切出 'all' 这种东西
    """
    url = job.get("companyLinkedinUrl") or None
    if url and "/company/" not in urlsplit(url).path:
        url = None
    return CurrentJob(
        company_name=job.get("companyName") or None,
        position=job.get("position") or None,
        company_id=str(job["companyId"]) if job.get("companyId") else None,
        company_slug=job.get("companyUniversalName") or None,
        company_linkedin_url=url,
    )


def _email(entry: dict, profile_url: str) -> FoundContact:
    """emails 里的一个翻译成 FoundContact，四样可信度原样带着。

    输入：{"email": "sam@hushly.example", "status": "risky", "catchAllDomain": True,
           "foundInGoogleSearch": True, "qualityScore": 75, "validEmailServer": True, "free": False}
          以及这个人的主页链接
    输出：FoundContact(channel=EMAIL, value="sam@hushly.example", source=LINKEDIN_EMAIL_SEARCH,
                       source_url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
                       email_status="risky", catch_all=True, found_in_google=True, quality_score=75)

    validEmailServer 和 free（是不是 gmail 这种免费邮箱）不单独成列，在 raw 里
    """
    # 输入   75；不是整数或者不在 0 到 100 之间
    # 输出   75；None。0011 里这一列有 0-100 的 CHECK，坏数写进去会让整步失败
    score = entry.get("qualityScore")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        score = None

    return FoundContact(
        channel=ContactChannel.EMAIL,
        value=entry["email"].strip(),
        source=ContactSource.LINKEDIN_EMAIL_SEARCH,
        source_url=profile_url,
        email_status=entry.get("status") or None,
        catch_all=entry.get("catchAllDomain"),
        found_in_google=entry.get("foundInGoogleSearch"),
        quality_score=score,
    )
