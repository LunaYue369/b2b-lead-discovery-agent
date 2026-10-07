"""帖子作者跟这家公司是什么关系：判出是 P1 到 P9 里的哪一种。纯函数，不连库不连网。

    classify_author      不花钱判一次：看主页链接和职位串，判出 P1、P2、P3 / P4、P5、P7、P8
    job_matches_lead     花钱查过之后判一次：他现在的工作是不是在这家（判出 P9）
    pick_current_job     一个人有几份现职时，挑出记进 person_lookups 的那一份
    name_too_common      这家的名字要不要跳过职位串比对（P8）

P6（转发的帖子）不在这里分：转发的人和原帖作者是两个 AuthorRef，各调一次 classify_author。

九种情况各是什么条件、怎么处理、落什么库、为什么、卡片上怎么写、真例子，全写在
triggers/contacts.py 开头「十一种情况」那一段。这里只负责判出是哪一种。

放在 discovery/ 不放 triggers/：这一层是纯函数、不许连库（check_isolation 守着），
同样的输入永远同样的输出，才能拿库里的历史证据反复回归这套规则。
"""

from __future__ import annotations

from models import (
    AuthorKind,
    AuthorRef,
    CurrentJob,
    LeadForContacts,
    linkedin_company_slug,
    normalize_company_name,
)


def name_too_common(normalized_name: str, *, common_words: frozenset[str], max_short_length: int) -> bool:
    """P8（这家的名字只有一个词，而且太短或者是常用英文单词）成不成立。

    输入：规范化之后的公司名，如 'air'；常用词表；多短算太短，如 3
    输出：True 跳过职位串比对，这家的个人作者一律按 P5（职位串里没有这家）处理；False 照常比

    四个例子（max_short_length=3，词表里有 air、fever）：

        'air'            True    在词表里
        'bbc'            True    只有三个字母，太短
        'hushly'         False   不是常用词
        'bvf georgia'    False   两个词，整个词组撞上职位串的机会很小，照常比

    跳过的代价是漏掉这家真正的人：他的职位串里明明写着 Air，我们也不花钱查他。
    卡片上他的主页和帖子照样在，Riley 点开就能判断
    """
    tokens = normalized_name.split()
    if len(tokens) != 1:
        return False
    word = tokens[0]
    return len(word) <= max_short_length or word in common_words


def classify_author(
    lead: LeadForContacts,
    author: AuthorRef,
    *,
    common_words: frozenset[str],
    max_short_length: int,
) -> AuthorKind:
    """不花钱，只看主页链接和职位串，判这个作者落进哪一种情况。

    输入：这家公司，如 LeadForContacts(lead_id=1388, normalized_name='hushly', linkedin_slug='hushly', ...)；
          一条证据里的一个作者，如 AuthorRef(signal_id=2820, name='sam hadid',
          headline='ceo @ Hushly - a sanctuary for human sound', is_company_page=False, ...)；
          P8 用的常用词表和「多短算太短」
    输出：AuthorKind，六种（每种的例子见 triggers/contacts.py 开头「十一种情况」）：

        OWN_PAGE           P1  公司主页，是这家自己
        OTHER_PAGE         P2  公司主页，是别的机构
        NAMED_IN_HEADLINE  P3 / P4  个人，职位串里有这家的名字。只有这一种会花钱去查
        NOT_NAMED          P5  个人，职位串里没有这家的名字
        PARTIAL_NAME       P7  个人，职位串里只对上这家名字的一部分
        NAME_TOO_COMMON    P8  这家的名字太短或是常用词，不比

    P6（转发的帖子）不在这里分：转发的人和原帖作者是两个 AuthorRef，各调一次这个函数。
    P9 要花钱查过才知道，在 job_matches_lead
    """
    # P1 / P2：作者是公司主页。公司主页的 headline 是粉丝数（'2,244 followers'），不看它
    if author.is_company_page:
        return _classify_company_page(lead, author)

    # P8：这家的名字太短或是常用词，职位串里撞上也说明不了什么
    if name_too_common(lead.normalized_name, common_words=common_words, max_short_length=max_short_length):
        return AuthorKind.NAME_TOO_COMMON

    # 职位串跟公司名走同一套规范化：小写、标点变空格（& 和 - 留着）、去掉 inc / llc 这种后缀
    # 输入   'Founder/CEO Chatterly Inc. | Safe-listening headphones | Offset Audioᵀᴹ'
    # 输出   'founder ceo chatterly safe-listening headphones offset audioᵀᴹ'
    #        公司名那边 'Chatterly Inc.' 规范化出来是 'chatterly'，两边口径一样才比得了
    headline = normalize_company_name(author.headline or "")

    # P3 / P4：职位串里有这家的名字，整词比。两边各垫一个空格，'hushly' 不会撞上 'hushlyic'
    # 输入   name='hushly'，headline='ceo hushly - a sanctuary for human sound'
    # 输出   ' hushly ' 在 ' ceo hushly - a sanctuary for human sound ' 里 -> NAMED_IN_HEADLINE
    if f" {lead.normalized_name} " in f" {headline} ":
        return AuthorKind.NAMED_IN_HEADLINE

    # P7：这家的名字有好几个词，只有一部分出现在职位串里
    # 输入   name='bvf georgia'，headline 的词 {'cro', 'bvf', 'helping', 'brands', ...}
    # 输出   'bvf' 在里面、'georgia' 不在 -> PARTIAL_NAME
    #        只算三个字母以上、不在常用词表里的词：'north field technologies' 里的 'new' 太常见，撞上不算数
    name_words = [w for w in lead.normalized_name.split() if len(w) >= 3 and w not in common_words]
    headline_words = set(headline.split())
    if len(lead.normalized_name.split()) > 1 and any(w in headline_words for w in name_words):
        return AuthorKind.PARTIAL_NAME

    # P5：职位串里没有这家的名字
    return AuthorKind.NOT_NAMED


def _classify_company_page(lead: LeadForContacts, author: AuthorRef) -> AuthorKind:
    """作者是公司主页时，分 P1（这家自己的主页）和 P2（别的机构的主页）。

    输入：这家公司；一个公司主页作者，如 AuthorRef(name='Makers&Co',
          url='https://www.linkedin.com/company/makers&co/posts', is_company_page=True, ...)
    输出：AuthorKind.OWN_PAGE（P1）或 AuthorKind.OTHER_PAGE（P2）

    两种比法，按这家公司手上有什么：

        有 linkedin_slug（有 LinkedIn 公司页的那一档都有）
            拿主页链接里 /company/ 后面那一段比
            输入   url='https://www.linkedin.com/company/makers&co/posts'，slug='makers&co'
            输出   OWN_PAGE
        只有名字
            拿主页的名字规范化之后比
            输入   name='Chatterly Inc.' -> 'chatterly'，normalized_name='chatterly'
            输出   OWN_PAGE
    """
    if lead.linkedin_slug:
        # 输入   '.../company/audioxpress/posts'
        # 输出   'audioxpress' != 'earmarkaudio' -> P2。链接里没有 /company/ 是 None，也是 P2
        return (
            AuthorKind.OWN_PAGE
            if linkedin_company_slug(author.url) == lead.linkedin_slug
            else AuthorKind.OTHER_PAGE
        )
    if normalize_company_name(author.name) == lead.normalized_name:
        return AuthorKind.OWN_PAGE
    return AuthorKind.OTHER_PAGE


def job_matches_lead(lead: LeadForContacts, job: CurrentJob) -> bool:
    """P3 / P4 花钱查回来之后：他的这一份现职是不是就在这家。False 就是 P9（现职不在这家）。

    输入：这家公司；那个人的一份现职，如 CurrentJob(company_name='Hushly', company_id='100000002',
          company_slug='hushly', company_linkedin_url='https://www.linkedin.com/company/hushly/')
    输出：True 是这家；False 不是

    从最准的比起，两边都有才比；一旦比了就以它为准，不往下退：

        一、LinkedIn 公司数字 id    两边都有就比 id
            sam hadid 替 Hushly：job.company_id '100000002' == leads.linkedin_company_id '100000002' -> True
        二、公司页 slug            id 至少一边没有、两边都有 slug 就比 slug
        三、公司名                 前两样比不了，比规范化之后的名字（最弱）
            Ravi Shah 替 Chatterly Inc.：job.company_name 'Chatterly Inc.' -> 'chatterly'
            == leads.normalized_name 'chatterly' -> True（他的公司在 LinkedIn 上没有公司页，只能比名字）

    一旦比了就不往下退：id 两边都有但不一样，说明是两家不同的公司，哪怕名字碰巧一样
    （LinkedIn 上同名的公司很多）也算对不上
    """
    if job.company_id and lead.linkedin_company_id:
        return job.company_id == lead.linkedin_company_id
    if job.company_slug and lead.linkedin_slug:
        return job.company_slug.lower() == lead.linkedin_slug
    if job.company_name:
        return normalize_company_name(job.company_name) == lead.normalized_name
    return False


def pick_current_job(lead: LeadForContacts, jobs: tuple[CurrentJob, ...]) -> tuple[CurrentJob | None, bool]:
    """P3 / P4 花钱查回来之后：从他的几份现职里挑出写进 person_lookups 的那一份，连带对不对得上。

    输入：这家公司；他的现职，如 (CurrentJob(company_name="Hushly", company_id="100000002", ...),)
    输出：(那一份现职, 对不对得上)，三种：

        有一份在这家         (那一份, True)     P3 / P4 对上：现职里有 Hushly，就记 Hushly，不管排第几
        有现职但都不在这家   (第一份, False)    P9：职位串写着这家，现职已经不在了
        一份现职都没有       (None, False)      经历里没有标「至今」的工作，也按 P9 处理（邮箱不挂）

    person_lookups 那一行只放得下一份现职（current_company_name / current_company_linkedin_url）。
    记对上的那一份，是因为卡片上要回答的是「他是不是这家的人」；另外几份在那一行的 raw 里
    """
    for job in jobs:
        if job_matches_lead(lead, job):
            return job, True
    return (jobs[0] if jobs else None), False
