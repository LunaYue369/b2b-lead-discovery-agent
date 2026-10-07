"""找联系方式那一步的读写：0011 那四张表，外加往 leads.domain 补官网域名。

    写                                   读
                                         leads_to_contact        这一跑要推卡片、要找联系方式的是哪几家
                                         contact_inputs          这几家的身份和每条证据的作者
    insert_company_profile               latest_profiles         这几家各自最新的一份概况
    insert_person_lookup                 person_lookups_for      这几家各自查过哪些人
    insert_contact                       contacts_for            这几家各自找到的联系方式
    record_spend                         spend_since             从某一刻起调 actor 一共花了多少
    set_domain_if_empty

谁调这些：find_contacts（triggers/contacts.py，还没写）在推卡之前调写的那一半；
拼卡片时调读的那一半；月度上限调 spend_since。

这一层只管读写，不做判断：该不该抓、这个人算不算这家的、花多少钱，全是调用方算好了
传进来。跟 lead_db 一个分工。

四张表只往里加，这里没有一个函数改它们的旧行。唯一的 update 是 set_domain_if_empty
改 leads，而且只在那一格为空的时候写。

提交在调用方那边：这里只写不提交，跟 lead_db 一样。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

import psycopg

from models import (
    AuthorRef,
    CompanyProfile,
    ContactChannel,
    ContactSource,
    DomainWrite,
    FoundContact,
    LeadForContacts,
    PersonLookup,
    SavedContact,
    SavedPersonLookup,
    SpendPurpose,
    normalize_domain,
)


def leads_to_contact(cur: psycopg.Cursor, run_id: int, *, min_score: int | None) -> list[int]:
    """这一跑要推卡片、所以要找联系方式的是哪几家。

    输入：游标；哪一跑，如 45；分数下限，如 60（只找过线的）；None 是不看分数（没过线的也推卡时）
    输出：lead_id 列表，按 id 排序，如 [1285, 1289, 1388]；这一跑一家都没打上分是 []

    跟推卡那一步挑卡片的口径一样（digest.push_interactive / push_digest 经过 lead_db.card_sources）：

        这一跑打了分     leads.last_analyzed_run_id = 这一跑，跟 card_sources 同一个条件
        不在写稿         status <> 'in_draft'：在写稿的那家不推卡，也就不用找
        够分             min_score 不是 None 时 score >= min_score

    口径对不上的后果：找了不推卡的那几家是白花钱；推了卡却没找的那几家，卡片上联系方式是空的
    """
    # 输入   run_id=45, min_score=None
    # 输出   [(1285,), (1289,), (1388,)]
    cur.execute(
        """
        select id
          from leads
         where last_analyzed_run_id = %s
           and status <> 'in_draft'
           and (%s::int is null or score >= %s::int)
         order by id
        """,
        (run_id, min_score, min_score),
    )
    return [row[0] for row in cur.fetchall()]


def contact_inputs(
    cur: psycopg.Cursor, lead_ids: Sequence[int]
) -> dict[int, tuple[LeadForContacts, list[AuthorRef]]]:
    """找联系方式要的原料：这几家公司的身份，和它们每条证据里的作者。

    输入：一批 lead_id，如 [1388, 1289]，允许为空
    输出：lead_id -> (身份, 作者列表)，作者按帖子从新到旧排，如

        {1388: (LeadForContacts(lead_id=1388, display_name="Hushly", linkedin_slug="hushly", ...),
                [AuthorRef(signal_id=2820, role="poster", name="sam hadid", ...)]),
         1289: (LeadForContacts(lead_id=1289, display_name="North Field Technologies", ...),
                [AuthorRef(signal_id=2720, role="poster", name="Marc-Andre Lavoie", ...),
                 AuthorRef(signal_id=2720, role="original_poster", name="SingAlong Group", ...)])}

        传进来的 id 在 leads 里没有的，不在输出里；有这家但一条证据都没有的，作者列表是 []

    转发的帖子一条证据出两个作者（P6）：发帖的人，和原帖作者（signals.reposted_actor_* 那三列）。
    原帖作者库里没有「是个人还是公司」那一列，看链接：有 /company/ 的是公司主页
    """
    if not lead_ids:
        return {}
    ids = sorted(set(lead_ids))

    # 一、身份
    # 输出   [(1388, 'Hushly', 'hushly', 'li_id', '100000002', 'hushly', None), ...]
    cur.execute(
        """
        select id, display_name, normalized_name, identity_kind,
               linkedin_company_id, linkedin_slug, domain
          from leads
         where id = any(%s)
        """,
        (ids,),
    )
    out: dict[int, tuple[LeadForContacts, list[AuthorRef]]] = {
        row[0]: (LeadForContacts(*row), []) for row in cur.fetchall()
    }

    # 二、每条证据的作者，帖子新的在前
    # 输出   [(1388, 2820, 'sam hadid', 'ceo @ Hushly - ...',
    #          'https://www.linkedin.com/in/...?miniProfileUrn=...', 'profile',
    #          'https://www.linkedin.com/posts/...', datetime(2026, 9, 21, ...), None, None, None), ...]
    #        后三列是原帖作者的名字、职位串、链接，不是转发的帖子三列都是 None
    cur.execute(
        """
        select e.lead_id, s.id, s.actor_name, s.actor_headline, s.actor_url, s.actor_type,
               s.url, s.event_at,
               s.reposted_actor_name, s.reposted_actor_headline, s.reposted_actor_url
          from lead_evidence e
          join signals s on s.id = e.signal_id
         where e.lead_id = any(%s)
         order by e.lead_id, s.event_at desc nulls last, s.id desc
        """,
        (ids,),
    )
    for (
        lead_id,
        signal_id,
        name,
        headline,
        url,
        actor_type,
        post_url,
        event_at,
        reposted_name,
        reposted_headline,
        reposted_url,
    ) in cur.fetchall():
        authors = out[lead_id][1]
        authors.append(
            AuthorRef(
                signal_id=signal_id,
                role="poster",
                name=name,
                headline=headline or None,
                url=url,
                is_company_page=actor_type == "company",
                post_url=post_url,
                event_at=event_at,
            )
        )
        # 转发的帖子：原帖作者也算一个作者（P6）
        # 输入   reposted_url='https://www.linkedin.com/company/singalong-group/posts'
        # 输出   多一个 AuthorRef(role='original_poster', is_company_page=True)
        if reposted_url:
            authors.append(
                AuthorRef(
                    signal_id=signal_id,
                    role="original_poster",
                    name=reposted_name or "",
                    headline=reposted_headline or None,
                    url=reposted_url,
                    is_company_page="/company/" in reposted_url,
                    post_url=post_url,
                    event_at=event_at,
                )
            )
    return out


def insert_company_profile(
    cur: psycopg.Cursor,
    lead_id: int,
    profile: CompanyProfile,
    *,
    run_id: int | None,
    fetched_at: datetime,
) -> int:
    """把公司详情 actor 抓回来的一份概况存成一行，返回那一行的 id。

    输入：
        lead_id     这是哪家公司的概况，如 1388
        profile     CompanyProfile(website="http://www.hushly.example", employee_count=11, ...)
        run_id      哪一跑触发的抓取，如 45
        fetched_at  抓回来的时刻，带时区
    输出：company_profiles.id，如 3

    同一家抓两次就是两行，不去重：概况会变（人数会涨），两次抓回来不一样的时候两份都该留着。
    「抓过就别再花钱」是调用方先查 latest_profiles 挡掉的，不靠这里的唯一约束
    """
    cur.execute(
        """
        insert into company_profiles
            (lead_id, run_id, website, employee_count, employee_count_range,
             founded_year, location, industries, phone, raw, fetched_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            lead_id,
            run_id,
            profile.website,
            profile.employee_count,
            profile.employee_count_range,
            profile.founded_year,
            profile.location,
            # 输入   ("Music",)
            # 输出   ["Music"]。psycopg 把 tuple 当成一条记录发出去，写进 text[] 会报
            #        malformed array literal，必须转成 list（0010 那次踩过）
            list(profile.industries),
            profile.phone,
            # 输出   '{"website": "http://www.hushly.example", ...}'，字符串进 jsonb 列，库那边解析
            json.dumps(profile.raw),
            fetched_at,
        ),
    )
    # 输出   (3,)
    return int(cur.fetchone()[0])


def latest_profiles(cur: psycopg.Cursor, lead_ids: Sequence[int]) -> dict[int, CompanyProfile]:
    """这几家公司各自最新的一份概况。

    输入：一批 lead_id，如 [1388, 1341, 1355]，允许为空
    输出：lead_id -> CompanyProfile，只含抓过的那几家，如
          {1388: CompanyProfile(website="http://www.hushly.example", ...)}
          1341 只有名字没抓过、1355 还没轮到，都不在里面

    两个用处：find_contacts 拿它判「这家抓过没有」（抓过就不再花 $0.004）；
    拼卡片时拿它印「公司概况」那一区。

    同一家抓过几次时取 fetched_at 最新的那一行；同一刻两行（不该出现）按 id 大的算
    """
    if not lead_ids:
        return {}

    # distinct on (lead_id) 配 order by lead_id, fetched_at desc：每家只留排在最前的那一行，
    # 也就是最新的那一份
    cur.execute(
        """
        select distinct on (lead_id)
               lead_id, website, employee_count, employee_count_range,
               founded_year, location, industries, phone, raw
          from company_profiles
         where lead_id = any(%s)
         order by lead_id, fetched_at desc, id desc
        """,
        (sorted(set(lead_ids)),),
    )

    # 输入   [(1388, 'http://www.hushly.example', 11, '2-10', 2022, 'United States', ['Music'], None, {...})]
    # 输出   {1388: CompanyProfile(...)}
    #        industries 库里是 text[]、查出来是 list，转 tuple（这个类是 frozen 的）；
    #        库里是 null 时给 ()。raw 是 jsonb，psycopg 已经解析成 dict 交回来
    return {
        lead_id: CompanyProfile(
            website=website,
            employee_count=employee_count,
            employee_count_range=employee_count_range,
            founded_year=founded_year,
            location=location,
            industries=tuple(industries or ()),
            phone=phone,
            raw=raw,
        )
        for (
            lead_id,
            website,
            employee_count,
            employee_count_range,
            founded_year,
            location,
            industries,
            phone,
            raw,
        ) in cur.fetchall()
    }


def insert_person_lookup(
    cur: psycopg.Cursor,
    lead_id: int,
    lookup: PersonLookup,
    *,
    signal_id: int | None,
    run_id: int | None,
    fetched_at: datetime,
) -> tuple[int, bool]:
    """记下花钱查过的一个人，返回那一行的 id 和这次是不是新写的。

    输入：
        lead_id     替哪家公司查的，如 1388
        lookup      PersonLookup(person_linkedin_url="https://www.linkedin.com/in/samhadid/",
                                 matches_lead=True, ...)
        signal_id   因为哪一条帖子才去查他，如 5120；那条帖子找不到了是 None
        run_id      哪一跑，如 45
        fetched_at  查回来的时刻，带时区
    输出：(person_lookups.id, 是不是新写的)，两种：

        (7, True)    第一次查这个人，写进去了
        (7, False)   同一家、同一个人库里已经有了（唯一约束撞上），没写，返回旧那一行的 id

    撞上的时候不覆盖旧行：旧行是上一次花钱买回来的事实，这次的是重复。会撞上说明调用方
    没先查 person_lookups_for 就又花了一次钱（或者是崩溃之后的重跑），调用方拿 False
    打一行日志，不该当成故障
    """
    cur.execute(
        """
        insert into person_lookups
            (lead_id, signal_id, run_id, person_linkedin_url, person_name, headline,
             current_company_name, current_company_linkedin_url, matches_lead, raw, fetched_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (lead_id, person_linkedin_url) do nothing
        returning id
        """,
        (
            lead_id,
            signal_id,
            run_id,
            lookup.person_linkedin_url,
            lookup.person_name,
            lookup.headline,
            lookup.current_company_name,
            lookup.current_company_linkedin_url,
            lookup.matches_lead,
            json.dumps(lookup.raw),
            fetched_at,
        ),
    )

    # 输出   (7,) 写进去了；None 撞上了唯一约束，do nothing 不返回任何行
    row = cur.fetchone()
    if row is not None:
        return int(row[0]), True

    # 撞上了：拿旧那一行的 id 回去，调用方后面写 lead_contacts 要用它
    cur.execute(
        "select id from person_lookups where lead_id = %s and person_linkedin_url = %s",
        (lead_id, lookup.person_linkedin_url),
    )
    return int(cur.fetchone()[0]), False


def person_lookups_for(
    cur: psycopg.Cursor, lead_ids: Sequence[int]
) -> dict[int, list[SavedPersonLookup]]:
    """这几家公司各自查过哪些人。

    输入：一批 lead_id，如 [1388, 1341]，允许为空
    输出：lead_id -> 查过的人，按查的先后排，只含查过人的那几家，如
          {1388: [SavedPersonLookup(id=7, signal_id=5120,
                                    lookup=PersonLookup(person_name="sam hadid", ...), ...)]}

    两个用处：find_contacts 拿它判「这个人查过没有」（查过就不再花 $0.01）、
    数这家已经花钱查了几个人（每家最多 2 个）；拼卡片时拿它印每个作者是不是这家的人
    """
    if not lead_ids:
        return {}

    cur.execute(
        """
        select id, lead_id, signal_id, person_linkedin_url, person_name, headline,
               current_company_name, current_company_linkedin_url, matches_lead, raw, fetched_at
          from person_lookups
         where lead_id = any(%s)
         order by lead_id, fetched_at, id
        """,
        (sorted(set(lead_ids)),),
    )

    out: dict[int, list[SavedPersonLookup]] = {}
    for (
        row_id,
        lead_id,
        signal_id,
        url,
        name,
        headline,
        company_name,
        company_url,
        matches,
        raw,
        fetched_at,
    ) in cur.fetchall():
        # 输入   (7, 1388, 5120, 'https://www.linkedin.com/in/samhadid/', 'sam hadid', ..., True, {...}, ...)
        # 输出   out[1388] 多一个 SavedPersonLookup
        out.setdefault(lead_id, []).append(
            SavedPersonLookup(
                id=row_id,
                signal_id=signal_id,
                lookup=PersonLookup(
                    person_linkedin_url=url,
                    person_name=name,
                    headline=headline,
                    current_company_name=company_name,
                    current_company_linkedin_url=company_url,
                    matches_lead=matches,
                    raw=raw,
                ),
                fetched_at=fetched_at,
            )
        )
    return out


def insert_contact(
    cur: psycopg.Cursor,
    lead_id: int,
    contact: FoundContact,
    *,
    person_lookup_id: int | None,
    run_id: int | None,
    found_at: datetime,
) -> bool:
    """存一个找到的邮箱或电话，返回这次是不是新写的。

    输入：
        lead_id           这是哪家公司的联系方式，如 1388
        contact           FoundContact(channel=EMAIL, value="sam@hushly.example",
                                       source=LINKEDIN_EMAIL_SEARCH, ...)
        person_lookup_id  从哪一次查人来的，如 7；官网找到的传 None
        run_id            哪一跑，如 45
        found_at          找到的时刻，带时区
    输出：True 写进去了；False 同一家、同一个来源、同一个地址库里已经有了，跳过

    同一个地址按小写比（唯一索引里是 lower(value)）：官网上写 Contact@Hushly.example、
    重跑时抓回来还是那个地址，算同一条。地址本身按原样存，卡片上照原样印。

    两条规矩这里不管、库替我们挡着：个人主页来的必须带 person_lookup_id（CHECK 约束），
    地址不能是空串（CHECK 约束）。违反了是调用方写错了，psycopg 当场抛
    """
    cur.execute(
        """
        insert into lead_contacts
            (lead_id, run_id, person_lookup_id, channel, value, source, source_url,
             email_status, catch_all, found_in_google, quality_score, found_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (lead_id, channel, lower(value), source) do nothing
        """,
        (
            lead_id,
            run_id,
            person_lookup_id,
            str(contact.channel),
            contact.value,
            str(contact.source),
            contact.source_url,
            contact.email_status,
            contact.catch_all,
            contact.found_in_google,
            contact.quality_score,
            found_at,
        ),
    )
    # rowcount 是这条 insert 实际写进去几行：撞上唯一索引被跳过就是 0
    return cur.rowcount == 1


def contacts_for(cur: psycopg.Cursor, lead_ids: Sequence[int]) -> dict[int, list[SavedContact]]:
    """这几家公司各自找到的联系方式，最新的在前。

    输入：一批 lead_id，如 [1388, 1341]，允许为空
    输出：lead_id -> 联系方式，按 found_at 从新到旧排，只含找到过的那几家，如
          {1388: [SavedContact(id=13, contact=FoundContact(value="sam@hushly.example", ...), ...),
                  SavedContact(id=12, contact=FoundContact(value="contact@hushly.example", ...), ...)]}

    新旧都留、最新的在前：新找到的跟以前的不一样时两个都印，卡片先印新的。
    同一刻找到的几条按 id 从大到小，也就是后写进去的在前
    """
    if not lead_ids:
        return {}

    cur.execute(
        """
        select id, lead_id, person_lookup_id, channel, value, source, source_url,
               email_status, catch_all, found_in_google, quality_score, found_at
          from lead_contacts
         where lead_id = any(%s)
         order by lead_id, found_at desc, id desc
        """,
        (sorted(set(lead_ids)),),
    )

    out: dict[int, list[SavedContact]] = {}
    for (
        row_id,
        lead_id,
        person_lookup_id,
        channel,
        value,
        source,
        source_url,
        email_status,
        catch_all,
        found_in_google,
        quality_score,
        found_at,
    ) in cur.fetchall():
        # 输入   (12, 1388, None, 'email', 'contact@hushly.example', 'website', 'https://home.hushly.example/',
        #         None, None, None, None, ...)
        # 输出   out[1388] 多一个 SavedContact。channel / source 转回枚举：库里是字符串，
        #        值不认识会在这里当场 ValueError，而不是一路带着一个拼错的来源走到卡片上
        out.setdefault(lead_id, []).append(
            SavedContact(
                id=row_id,
                contact=FoundContact(
                    channel=ContactChannel(channel),
                    value=value,
                    source=ContactSource(source),
                    source_url=source_url,
                    email_status=email_status,
                    catch_all=catch_all,
                    found_in_google=found_in_google,
                    quality_score=quality_score,
                ),
                person_lookup_id=person_lookup_id,
                found_at=found_at,
            )
        )
    return out


def record_spend(
    cur: psycopg.Cursor,
    *,
    actor: str,
    purpose: SpendPurpose,
    item_count: int,
    cost_usd: Decimal,
    run_id: int | None,
    lead_id: int | None,
    called_at: datetime,
) -> None:
    """调了一次付费 actor，记下花了多少钱。

    输入：
        actor       例 'harvestapi/linkedin-company'
        purpose     SpendPurpose.COMPANY_PROFILE
        item_count  这一次计费了几条，如 1
        cost_usd    这一次花了多少，如 Decimal("0.00405")，调用方按价格表算好
        run_id      哪一跑，如 45
        lead_id     为哪家公司花的，如 1388；一次调用查了好几家时传 None
        called_at   调用的时刻，带时区
    输出：无

    cost_usd 只收 Decimal，传 float 当场 TypeError：0.1 + 0.2 用 float 加出来是
    0.30000000000000004，加总之后跟上限比大小会差一点点判错，而那种错看不出来。
    库里那一列是 numeric(10, 5)，Decimal 原样进去不丢精度。

    钱已经花出去了才调这个函数，所以要尽早调：调 actor 成功之后的第一件事就是记账，
    后面解析返回、写概况哪一步炸了，这一笔都已经在事务里了
    """
    if not isinstance(cost_usd, Decimal):
        raise TypeError(f"cost_usd 要传 Decimal，拿到 {type(cost_usd).__name__}：{cost_usd!r}")

    cur.execute(
        """
        insert into actor_spend (run_id, lead_id, actor, purpose, item_count, cost_usd, called_at)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (run_id, lead_id, actor, str(purpose), item_count, cost_usd, called_at),
    )


def spend_since(cur: psycopg.Cursor, *, since: datetime) -> Decimal:
    """从 since 起，调付费 actor 一共花了多少钱。月度上限拿它加上采集那一份算已花。

    输入：账期的起点，带时区，如 2026-09-21T00:00Z
    输出：一个 Decimal，如 Decimal("0.48600")；这个账期一次都没调过是 Decimal("0")

    定时那条路和对话那条路都算（两条路找联系方式的钱都算进同一个月度上限），
    所以这里不按 run_type 分，也不 join runs —— run_id 是空的那几行（不属于哪一跑）照样算
    """
    cur.execute(
        "select coalesce(sum(cost_usd), 0) from actor_spend where called_at >= %s",
        (since,),
    )
    # 输出   (Decimal('0.48600'),)。coalesce 让一行都没有时拿到 0 而不是 None；
    #        psycopg 把 numeric 交回来就是 Decimal，这里再包一次是防一行都没有时拿到 int 0
    return Decimal(cur.fetchone()[0])


def set_domain_if_empty(cur: psycopg.Cursor, lead_id: int, domain: str) -> DomainWrite:
    """把官网域名补进 leads.domain，只在那一格为空、而且没被别家占着的时候写。

    输入：lead_id，如 1388；域名，必须已经归一化过（小写、没协议、没 www、没路径），
          如 'hushly.example'。没归一化当场 ValueError：同一家公司写成 Hushly.example 和
          hushly.example 两种，唯一索引就拦不住了
    输出：DomainWrite，三种：

        一、那一格是空的，也没人占着
            库里   leads 1388 的 domain 写成 'hushly.example'
            输出   DomainWrite(outcome="written", other_lead_id=None)

        二、那一格已经有值（不管跟这次的一不一样）
            库里   不动
            输出   DomainWrite(outcome="kept", other_lead_id=None)
            后果   不覆盖：原来那个值可能是别的途径确认过的，这次抓官网跳转出来的不一定更对

        三、这个域名已经写在另一家公司的那一行上
            库里   不动
            输出   DomainWrite(outcome="taken", other_lead_id=1288)
            后果   两行很可能是同一家公司（比如一行按 LinkedIn id 建的，一行按名字建的），
                   合并要人来判。调用方打一行警告，卡片照出。硬写的话会撞唯一索引抛异常，
                   把这一整步都带崩

    先查后写，不是原子的：两个进程同时给两家写同一个域名，后写那个会撞唯一索引抛异常。
    同一时刻只有一个 discovery 在跑（advisory lock），find_contacts 跟在它后面，碰不到
    """
    if normalize_domain(domain) != domain:
        raise ValueError(
            f"域名要先归一化再传进来：拿到 {domain!r}，归一化之后是 {normalize_domain(domain)!r}"
        )

    # 输出   ('hushly.example',) 已经有值；(None,) 空着。这一行不存在是调用方传错了 id，fetchone 是 None
    cur.execute("select domain from leads where id = %s", (lead_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"leads 里没有 id={lead_id} 这一行")
    if row[0] is not None:
        return DomainWrite(outcome="kept", other_lead_id=None)

    # 输出   (1288,) 被别家占着；None 没人占
    cur.execute("select id from leads where domain = %s and id <> %s", (domain, lead_id))
    taken = cur.fetchone()
    if taken is not None:
        return DomainWrite(outcome="taken", other_lead_id=int(taken[0]))

    # `and domain is null` 再挡一次：上面查完到这里之间万一被别处写了，这句改 0 行而不是覆盖
    cur.execute(
        "update leads set domain = %s, updated_at = now() where id = %s and domain is null",
        (domain, lead_id),
    )
    if cur.rowcount != 1:
        return DomainWrite(outcome="kept", other_lead_id=None)
    return DomainWrite(outcome="written", other_lead_id=None)
