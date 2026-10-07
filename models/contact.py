"""找联系方式那一步的词汇：公司概况、查过的人、找到的联系方式、花的钱。

四张表（0011）各对应一个类型，写进去和读出来用同一套：

    CompanyProfile   company_profiles 一行     公司详情 actor 抓回来的公司概况
    PersonLookup     person_lookups 一行       花钱查过的帖子作者，现职对不对得上这家
    FoundContact     lead_contacts 一行        一个邮箱或电话，带出处和可信度
    SpendPurpose     actor_spend.purpose       调付费 actor 是为了哪件事

读回来的时候库里多出几样写的时候没有的东西（行 id、什么时候找到的），
包成 SavedPersonLookup / SavedContact，不往上面那几个类型里塞：写的时候那几格没有值，
塞进去就只能先填一个假的。

「这个作者是不是这家公司的人」（P1 到 P9）不在这里，它是一条规则，不是库里的一行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class ContactChannel(StrEnum):
    """一条联系方式是哪一种。取值跟 lead_contacts.channel 的 CHECK 一一对应。"""

    EMAIL = "email"
    PHONE = "phone"


class ContactSource(StrEnum):
    """一条联系方式是从哪儿找来的。取值跟 lead_contacts.source 的 CHECK 一一对应。

    WEBSITE                 我们自己抓官网首页找到的，不花钱，没有可信度这一说
    LINKEDIN_EMAIL_SEARCH   个人主页 actor 顺带找的邮箱，$0.01 一个人，带四样可信度
    """

    WEBSITE = "website"
    LINKEDIN_EMAIL_SEARCH = "linkedin_email_search"


class SpendPurpose(StrEnum):
    """调一次付费 actor 是为了什么。取值跟 actor_spend.purpose 的 CHECK 一一对应。"""

    COMPANY_PROFILE = "company_profile"
    PERSON_EMAIL = "person_email"


class AuthorKind(StrEnum):
    """一条证据的作者跟这家公司是什么关系。只看免费的东西（主页链接、职位串）判出来的。

    跟 triggers/contacts.py 开头「十一种情况」里的 P1 到 P9 对应（每种的条件、处理、落什么库、
    卡片怎么写都在那里）：

        OWN_PAGE            P1  这家公司自己的主页发的
        OTHER_PAGE          P2  别的机构（媒体、投资方、展会）的主页发的
        NAMED_IN_HEADLINE   P3 / P4  个人，职位串里有这家的名字（整词）。只有这一种会花钱去查他
        NOT_NAMED           P5  个人，职位串里没有这家的名字
        PARTIAL_NAME        P7  个人，职位串里只对上这家名字的一部分（'bvf georgia' 只出现了 'bvf'）
        NAME_TOO_COMMON     P8  这家的名字是一个很短的词或者常用英文单词（'air'），不做比对

    P6（转发的帖子）不是一种关系：转发的人和原帖作者各判一次，各得上面一种。
    P9（职位串写着这家、查回来现职不在这家）要花钱查过才知道，记在 PersonLookup.matches_lead 上
    """

    OWN_PAGE = "own_page"
    OTHER_PAGE = "other_page"
    NAMED_IN_HEADLINE = "named_in_headline"
    NOT_NAMED = "not_named"
    PARTIAL_NAME = "partial_name"
    NAME_TOO_COMMON = "name_too_common"


@dataclass(frozen=True)
class LeadForContacts:
    """找联系方式要知道的这家公司的几样身份信息。leads 表里的一行，只取这几列。

    两个真例子：

        C1，手上有 LinkedIn 公司 id 的
        LeadForContacts(lead_id=1388, display_name="Hushly", normalized_name="hushly",
                        identity_kind="li_id", linkedin_company_id="100000002",
                        linkedin_slug="hushly", domain=None)

        C2，只有名字的
        LeadForContacts(lead_id=1281, display_name="Chatterly Inc.", normalized_name="chatterly",
                        identity_kind="name", linkedin_company_id=None, linkedin_slug=None, domain=None)
    """

    lead_id: int
    display_name: str
    normalized_name: str
    identity_kind: str
    linkedin_company_id: str | None
    linkedin_slug: str | None
    domain: str | None


@dataclass(frozen=True)
class AuthorRef:
    """一条证据里的一个作者：发帖的人，或者被转发的那条原帖的作者。

    一条转发的帖子出两个 AuthorRef（P6）。三个真例子（库里的 signals 行）：

        signal 2820，sam hadid 自己发的帖子
        AuthorRef(signal_id=2820, role="poster", name="sam hadid",
                  headline="ceo @ Hushly - a sanctuary for human sound",
                  url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4?miniProfileUrn=urn%3Ali%3A...",
                  is_company_page=False,
                  post_url="https://www.linkedin.com/posts/sam-hadid-a1b2c3d4_in-the-vault-...-pmIJ",
                  event_at=datetime(2026, 9, 21, 19, 56, 31, tzinfo=UTC))

        signal 2720，Marc-Andre Lavoie 转发了 SingAlong Group 的帖子，出两个：
        AuthorRef(signal_id=2720, role="poster", name="Marc-Andre Lavoie",
                  headline="Founder & CEO, North Field Technologies | Seratone is a plug-and-play ...",
                  url="https://www.linkedin.com/in/...", is_company_page=False,
                  post_url="https://www.linkedin.com/posts/activity-7493771291343237120-wIy1",
                  event_at=datetime(2026, 8, 13, 13, 30, 8, tzinfo=UTC))
        AuthorRef(signal_id=2720, role="original_poster", name="SingAlong Group",
                  headline="2,244 followers",
                  url="https://www.linkedin.com/company/singalong-group/posts", is_company_page=True,
                  post_url="https://www.linkedin.com/posts/activity-7493771291343237120-wIy1",
                  event_at=datetime(2026, 8, 13, 13, 30, 8, tzinfo=UTC))

    公司主页的 headline 是粉丝数（'2,244 followers'），不是职位，所以公司主页不看 headline

    url 是库里存的原样，个人主页后面带着 ?miniProfileUrn=...。拿它当「同一个人」的键之前
    要先过 harvestapi_profile.clean_profile_url。

    is_company_page：发帖的人那一半看 signals.actor_type；原帖作者那一半库里没有这一列，
    看链接里是 /company/ 还是 /in/
    """

    signal_id: int
    role: str  # 'poster' 发帖的人；'original_poster' 被转发的原帖的作者
    name: str
    headline: str | None
    url: str
    is_company_page: bool
    post_url: str | None
    event_at: datetime | None


@dataclass(frozen=True)
class CompanyProfile:
    """公司详情 actor 抓回来的一家公司的概况。company_profiles 的一行。

    一个实际值（Hushly）：

        CompanyProfile(website="http://www.hushly.example", employee_count=11,
                       employee_count_range="2-10", founded_year=2022, location="United States",
                       industries=("Music",), phone=None, raw={...actor 返回的整条...})

    除了 raw，每一格都允许为 None：actor 对小公司经常只给一部分，这是外部数据真实缺失，
    不是我们没填。卡片上缺哪样就不印哪样。电话这一格公司详情 actor 从来不给，永远是 None

    raw 是 actor 返回的那一整条，一个字不改。以后要加一样卡片上的东西、或者发现
    某一格解析错了，从这里重新取，不用再花钱重抓
    """

    website: str | None
    employee_count: int | None
    employee_count_range: str | None  # 例 "2-10"，公司自己填的规模档
    founded_year: int | None
    location: str | None  # 例 "Euless, US"
    industries: tuple[str, ...]  # 一个都没有是 ()，不用 None：「没有行业」不需要跟「空」分开
    phone: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class PersonLookup:
    """花钱查过的一个帖子作者。person_lookups 的一行（不含 id 和时间）。

    一个实际值（sam hadid，查回来现职就是 Hushly）：

        PersonLookup(person_linkedin_url="https://www.linkedin.com/in/samhadid/",
                     person_name="sam hadid",
                     headline="ceo @ Hushly - a sanctuary for human sound",
                     current_company_name="Hushly",
                     current_company_linkedin_url="https://www.linkedin.com/company/hushly/",
                     matches_lead=True, raw={...})

    headline 是查之前帖子上写的那行（名字下面那行，本人手写、常过时）；
    current_company_* 是查回来经历里标着「至今」的那份工作。两者对不上时
    matches_lead 是 False（P9），找到的邮箱不上卡片。

    current_company_* 允许为 None：这个人经历里可能一份「至今」的工作都没有
    """

    person_linkedin_url: str
    person_name: str | None
    headline: str | None
    current_company_name: str | None
    current_company_linkedin_url: str | None
    matches_lead: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class CurrentJob:
    """一个人经历里标着「至今」的一份工作。一个人可以同时有几份（actor 给的 currentPosition 是一个列表）。

    两个真例子：

        sam hadid，这份工作的公司有 LinkedIn 公司页
        CurrentJob(company_name="Hushly", position="Founder", company_id="100000002",
                   company_slug="hushly", company_linkedin_url="https://www.linkedin.com/company/hushly/")

        Ravi Shah，他填的公司在 LinkedIn 上没有公司页
        CurrentJob(company_name="Chatterly Inc.", position="Founder and CEO", company_id=None,
                   company_slug=None, company_linkedin_url=None)

    第二种里 LinkedIn 给的链接其实是一个搜索页
    （'https://www.linkedin.com/search/results/all/?keywords=Chatterly+Inc%2E'），
    那不是公司页，翻译的时候已经换成 None：拿它去跟 leads.linkedin_slug 比只会比出错来。
    这种只剩名字能比
    """

    company_name: str | None
    position: str | None  # 例 'Founder'，他在那一份工作里的职位，卡片上印在名字后面
    company_id: str | None  # LinkedIn 公司数字 id，跟 leads.linkedin_company_id 同一种东西
    company_slug: str | None  # 公司页链接里 /company/ 后面那一段，跟 leads.linkedin_slug 同一种东西
    company_linkedin_url: str | None


@dataclass(frozen=True)
class ScrapedPerson:
    """个人主页 actor 查回来的一个人，还没判断他是不是某家公司的人。

    一个真例子（sam hadid）：

        ScrapedPerson(linkedin_url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
                      name="sam hadid",
                      headline="ceo @ Hushly -  a sanctuary for human sound",
                      current_jobs=(CurrentJob(company_name="Hushly", ...),),
                      emails=(FoundContact(value="sam@hushly.example", ...),),
                      company_websites=("hushly.example",),
                      raw={...})

    跟 PersonLookup 的差别：这里没有 matches_lead。「对不对得上这家」要拿 leads 那一行来比，
    那是 triggers/contacts.py 的事，这一层只翻译。

    company_websites 是 actor 顺带给的「他现职公司的官网域名」。只有名字、没有 LinkedIn 公司页
    的那一档公司（抓不了公司详情）以后可能靠它拿到官网，现在还没用上
    """

    linkedin_url: str
    name: str | None
    headline: str | None
    current_jobs: tuple[CurrentJob, ...]
    emails: tuple[FoundContact, ...]
    company_websites: tuple[str, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class SavedPersonLookup:
    """从库里读回来的一次查人：PersonLookup 再加上库里才有的几格。

    一个实际值：

        SavedPersonLookup(id=7, signal_id=5120, lookup=PersonLookup(...),
                          fetched_at=datetime(2026, 9, 27, 14, 0, tzinfo=UTC))

    signal_id 是「因为哪一条帖子才去查他」，那条帖子后来不在了是 None
    """

    id: int
    signal_id: int | None
    lookup: PersonLookup
    fetched_at: datetime


@dataclass(frozen=True)
class FoundContact:
    """找到的一个邮箱或电话。lead_contacts 的一行（不含 id、是谁、什么时候）。

    两个真例子：

        官网上找到的，没有可信度
        FoundContact(channel=EMAIL, value="contact@hushly.example", source=WEBSITE,
                     source_url="https://home.hushly.example/",
                     email_status=None, catch_all=None, found_in_google=None, quality_score=None)

        个人主页顺带找的，四样可信度原样带着
        FoundContact(channel=EMAIL, value="sam@hushly.example", source=LINKEDIN_EMAIL_SEARCH,
                     source_url="https://www.linkedin.com/in/samhadid/",
                     email_status="risky", catch_all=True, found_in_google=True, quality_score=75)

    后四格只有 LINKEDIN_EMAIL_SEARCH 有，官网来的全是 None。
    email_status 用字符串不用枚举：那是找邮箱那边给的值，它哪天多一种我们不该当场炸
    """

    channel: ContactChannel
    value: str
    source: ContactSource
    source_url: str | None
    email_status: str | None
    catch_all: bool | None
    found_in_google: bool | None
    quality_score: int | None


@dataclass(frozen=True)
class SavedContact:
    """从库里读回来的一条联系方式：FoundContact 再加上库里才有的几格。

    一个实际值：

        SavedContact(id=12, contact=FoundContact(...sam@hushly.example...),
                     person_lookup_id=7, found_at=datetime(2026, 9, 27, 14, 0, tzinfo=UTC))

    person_lookup_id 指回是哪一次查人找到的，卡片拿它印「这是谁的邮箱」；
    官网来的是 None
    """

    id: int
    contact: FoundContact
    person_lookup_id: int | None
    found_at: datetime


@dataclass(frozen=True)
class DomainWrite:
    """往 leads.domain 写官网域名的结果。contacts_db.set_domain_if_empty 返回它。

    三种：

        DomainWrite(outcome="written", other_lead_id=None)   原来是空的，写进去了
        DomainWrite(outcome="kept", other_lead_id=None)      原来就有值，没动
        DomainWrite(outcome="taken", other_lead_id=1288)     这个域名已经是 1288 的了，没写。
                                                             两行其实可能是同一家公司，等人工看

    outcome 用字符串字面量不用枚举：只有这一个函数产出它、调用方只拿它打日志和分支
    """

    outcome: str
    other_lead_id: int | None


class CardRelation(StrEnum):
    """卡片上一个作者跟这家公司是什么关系，决定他那一行印哪句话。

    跟 triggers/contacts.py 开头「十一种情况」的对应（P3 / P4 按查没查、对没对上拆成三种）：

        OWN_PAGE        P1              这家公司自己的主页发的帖
        OTHER_PAGE      P2              发帖的是别的机构，不是这家公司的，但仍可联络
        MEMBER          P3 / P4 对得上  这家公司的人
        NOT_LOOKED_UP   P3 / P4 还没查  职位串里写着这家公司，还没核对（没花钱查：超了上限、名额满了）
        LEFT            P9              他名字下面写着这家公司，但现在已不在这家
        NOT_MEMBER      P5 / P8         发帖的人不是这家公司的，只是在帖子里提到了它，仍可联络
        UNCLEAR         P7              看不出发帖人是不是这家公司的，仍可联络
    """

    OWN_PAGE = "own_page"
    OTHER_PAGE = "other_page"
    MEMBER = "member"
    NOT_LOOKED_UP = "not_looked_up"
    LEFT = "left"
    NOT_MEMBER = "not_member"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class CardPerson:
    """卡片「怎么联系」那一区里的一个作者：他是谁、跟这家什么关系、点哪里找他。

    两个真例子：

        CardPerson(name="sam hadid", relation=CardRelation.MEMBER, position="Founder",
                   current_company=None,
                   profile_url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
                   post_url="https://www.linkedin.com/posts/sam-hadid-a1b2c3d4_in-the-vault-is-on-its-"
                            "way-to-becoming-one-activity-7507890528395587584-pmIJ")
        CardPerson(name="audioXpress", relation=CardRelation.OTHER_PAGE, position=None,
                   current_company=None,
                   profile_url="https://www.linkedin.com/company/audioxpress/",
                   post_url=<那条帖子的链接>)

    position 只有 MEMBER 有（查回来的现职）；current_company 只有 LEFT 有（他现在在哪家）。
    profile_url 个人的是去掉 ?miniProfileUrn= 之后的主页；公司主页的作者链接是它的帖子列表
    （.../company/audioxpress/posts），换成公司页本身，链接里没有 /company/ 的原样
    """

    name: str
    relation: CardRelation
    position: str | None
    current_company: str | None
    profile_url: str
    post_url: str | None


@dataclass(frozen=True)
class CardEmail:
    """卡片「怎么联系」那一区里的一个邮箱，带出处和可信度。

    两个真例子：

        CardEmail(value="contact@hushly.example", source=ContactSource.WEBSITE,
                  source_url="https://home.hushly.example", person_name=None,
                  email_status=None, catch_all=None, found_in_google=None, quality_score=None,
                  other_company=None)
        CardEmail(value="sam@hushly.example", source=ContactSource.LINKEDIN_EMAIL_SEARCH,
                  source_url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
                  person_name="sam hadid", email_status="risky", catch_all=True,
                  found_in_google=True, quality_score=75, other_company=None)

    source_url 是在哪一页找到的（官网那一页、那个人的主页）；person_name 是个人主页来的邮箱是谁的，
    官网来的是 None。

    other_company：个人主页来的邮箱，域名跟这家的官网对不上、却跟他另一份现职那家的官网对得上时，
    是那一家的名字。例：Evan Park 替 Kestrel 查出来的 evan.park@rawcutstudio.example，
    other_company="Rawcut"，卡片上写「这是他在 Rawcut 的邮箱，不是这家的」。其余都是 None
    """

    value: str
    source: ContactSource
    source_url: str | None
    person_name: str | None
    email_status: str | None
    catch_all: bool | None
    found_in_google: bool | None
    quality_score: int | None
    other_company: str | None


@dataclass(frozen=True)
class CardContacts:
    """一张卡片「怎么联系」和「公司概况」两区要的全部东西。triggers/contacts.card_contacts 拼出来。

    一个真例子（Hushly 找过一次联系方式之后）：

        CardContacts(website_url="https://hushly.example",
                     company_page_url="https://www.linkedin.com/company/hushly/",
                     profile=CompanyProfile(website="http://www.hushly.example", employee_count=11,
                                            employee_count_range="2-10", founded_year=2022,
                                            location="United States", industries=("Music",), ...),
                     emails=(CardEmail(value="contact@hushly.example", ...),
                             CardEmail(value="sam@hushly.example", ...)),
                     people=(CardPerson(name="sam hadid", relation=CardRelation.MEMBER, ...),))

    website_url：leads.domain 有值就用它（跳转之后的真域名），没有就用概况里登记的官网地址，都没有是 None。
    company_page_url：这家有 LinkedIn 公司页就有。profile：抓过公司详情才有（C2 永远是 None）。
    一样都没有的那家是 NO_CONTACTS
    """

    website_url: str | None
    company_page_url: str | None
    profile: CompanyProfile | None
    emails: tuple[CardEmail, ...]
    people: tuple[CardPerson, ...]


# 一样联系方式都没有的那家用这个，不用 None：卡片那一层拿到的永远是一个 CardContacts，
# 不用每处都先判空
NO_CONTACTS = CardContacts(
    website_url=None, company_page_url=None, profile=None, emails=(), people=()
)

