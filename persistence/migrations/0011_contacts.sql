-- 出卡之前找联系方式：公司概况、找到的联系方式、付费查过的人、调 actor 花的钱
--
-- 推卡之前多一步 find_contacts，给每张要推的卡片找「怎么联系这家」。它要花两种钱、
-- 产出三样东西，这一份 migration 给这四件事各建一张表：
--
--     company_profiles   公司详情 actor 抓回来的公司概况（官网、人数、成立年份……）
--     person_lookups     花钱查过的帖子作者：他现在在哪家公司、跟这家对不对得上
--     lead_contacts      最后上卡片的联系方式（邮箱、电话），每条带出处和可信度
--     actor_spend        每调一次付费 actor 记一行花了多少钱，月度上限拿它算已花
--
-- 一家公司走完一遍，四张表里的样子（Hushly，lead_id=1388）：
--
--     company_profiles  lead=1388  website='http://www.hushly.example'  11 人（2-10）  2022  United States  Music
--     person_lookups    lead=1388  sam hadid  现职 companyLinkedinUrl '…/company/hushly/'  matches_lead=true
--     lead_contacts     lead=1388  email contact@hushly.example  来源 website  出处 https://home.hushly.example
--     lead_contacts     lead=1388  email sam@hushly.example      来源 linkedin_email_search
--                                  risky / catch-all / 谷歌搜索里出现过 / 质量分 75
--     actor_spend       harvestapi/linkedin-company          1 家  $0.00405（含一次启动费）
--     actor_spend       harvestapi/linkedin-profile-scraper  1 人  $0.01（找到邮箱的价）
--
-- 「这个作者是不是这家公司的人」（P1 到 P9 那套判断）不建表存：它是一条规则，拿 signals
-- 和 person_lookups 当场就能算出来。存下来就有两份真相，规则一改，库里那份就是旧的。
-- 要存的只有花钱才拿得到的事实，也就是 person_lookups 里那个人的现职。
--
-- 四张表都只往里加，不改旧行（代码里没有任何 update 语句写它们）：
--
--     公司概况、查人的结果都是「某一天抓回来是这样」的原始记录。以后重抓一次，
--     多一行，旧行留着，卡片取最新那一行。跟 signals 只存原文、不覆盖是同一条纪律
--
-- 删除跟着 leads 走（on delete cascade）：这些行都是「关于这家公司的」，公司那一行
-- 没了它们就没有意义。run_id 那一列是 set null，理由同 0006 的 pushed_run_id：
-- runs 那一行没了不该把找到的联系方式也删掉。公司是长期资产，跑是账。
--
-- 不改 leads 表。官网跳转之后的域名写进 leads.domain（只在它为空的时候写），
-- 那一列 0001 就有，唯一索引也有。


-- ══════════════════════════════════════════════════════════════════
-- company_profiles —— 公司详情 actor 抓一次，存一行
--
-- 只有 leads.identity_kind='li_id' 的公司（手上有 LinkedIn 公司 id）才会抓。只有名字的
-- 那一档不抓：按名字去搜，搜到同名的另一家公司的风险太大。
--
-- 为什么单开一张表、不往 leads 上加列：
--
--     一、这是 actor 某一天返回的原始数据，会过时（人数会涨、公司会搬家）。
--         放 leads 上每次重抓就要覆盖，旧的就丢了
--     二、leads 已经有三十多列，全是「我们怎么判这家」。概况是「这家自己长什么样」，
--         是另一回事
--
-- 挑出来单独成列的是卡片「公司概况」那一区要印的六样，其余全在 raw 里。
-- 六样都允许为空：actor 对小公司经常只给一部分。电话那一格 actor 的返回里没有这个字段，
-- 现在永远是空的，留着列等哪天有了来源再填。
--
--     website               text     例 'http://www.hushly.example'，LinkedIn 上登记的原样，
--                                     还没跟过跳转（跳转之后的在 lead_contacts 的出处里）
--     employee_count        int      例 11，LinkedIn 上关联了这家的人数
--     employee_count_range  text     例 '2-10'，公司自己填的规模档，跟上面那个数常对不上，
--                                     两个都印
--     founded_year          int      例 2022
--     location              text     例 'Euless, US'，总部那一条拼成一句
--     industries            text[]   例 {Music}
--     phone                 text     例 '+1 617 555 0100'，大多数是空的
--     raw                   jsonb    actor 返回的那一整条，一个字不改
--
-- raw 必须存：以后要加一样卡片上的东西（比如 description），或者发现某一列解析错了，
-- 不用再花钱重抓。
-- ══════════════════════════════════════════════════════════════════
create table company_profiles (
    id bigserial primary key,
    lead_id bigint not null references leads (id) on delete cascade,
    run_id  bigint references runs (id) on delete set null,

    website              text,
    employee_count       int check (employee_count >= 0),
    employee_count_range text,
    founded_year         int check (founded_year between 1800 and 2100),
    location             text,
    industries           text[],
    phone                text,

    raw        jsonb not null,
    fetched_at timestamptz not null
);

-- 卡片要「这家最新的一份概况」，find_contacts 要「这家抓过没有」，两个都按 lead_id 查
create index company_profiles_lead_idx on company_profiles (lead_id, fetched_at desc);


-- ══════════════════════════════════════════════════════════════════
-- person_lookups —— 花钱查过的帖子作者
--
-- 帖子作者的职位串（名字下面那行，signals.actor_headline）里有这家公司的名字时，
-- 花 $0.01 抓他的个人主页并找邮箱。这一行记的是「查回来的事实」：
--
--     current_company_name          text    例 'Hushly'，经历里标着「至今」的那份工作
--     current_company_linkedin_url  text    例 'https://www.linkedin.com/company/hushly/'
--     matches_lead                  bool    现职跟这家对不对得上，对上才把邮箱挂到卡片上
--
-- matches_lead 为什么要落库而不是当场算：它是拿现职跟 leads 比出来的，比的时候
-- leads.linkedin_slug 是什么值，判出来就是什么。那一列以后可能被补上或者改掉，
-- 当时为什么挂了（或者没挂）这个邮箱，要能从库里答出来。
--
-- 一个真例子（sam hadid，Hushly 的创始人）：
--
--     person_linkedin_url   'https://www.linkedin.com/in/samhadid/'
--     person_name           'sam hadid'
--     headline              'ceo @ Hushly - a sanctuary for human sound'   查之前职位串写的
--     current_company_name  'Hushly'
--     current_company_linkedin_url  'https://www.linkedin.com/company/hushly/'
--     matches_lead          true
--
-- 对不上的样子（P9，职位串写着这家、现职已经不在这家）：
--
--     headline              'Founder @ Hushly'
--     current_company_name  'Spotify'
--     matches_lead          false
--     -> 这 $0.01 花了，找到的邮箱只留在 raw 里，不写进 lead_contacts。
--        卡片上写「他名字下面写着这家公司，但现在已不在这家」
--
-- signal_id 记的是「因为哪一条帖子才去查他」，方便对账，不参与判断。
--
-- 同一个人对同一家公司只查一次（唯一约束）：find_contacts 崩溃之后重跑，
-- 查过的人不会再花一次钱。插入用 on conflict do nothing。
-- ══════════════════════════════════════════════════════════════════
create table person_lookups (
    id bigserial primary key,
    lead_id   bigint not null references leads (id) on delete cascade,
    signal_id bigint references signals (id) on delete set null,
    run_id    bigint references runs (id) on delete set null,

    person_linkedin_url text not null,
    person_name         text,
    headline            text,

    current_company_name         text,
    current_company_linkedin_url text,
    matches_lead                 boolean not null,

    raw        jsonb not null,
    fetched_at timestamptz not null,

    unique (lead_id, person_linkedin_url)
);


-- ══════════════════════════════════════════════════════════════════
-- lead_contacts —— 上卡片的联系方式，一条一行
--
-- 两个来源：
--
--     website                  我们自己打开官网首页找到的、域名跟官网一样的邮箱，不花钱。没有可信度，
--                              它就是官网上公开写着的
--     linkedin_email_search    个人主页 actor 顺带找的邮箱，$0.01 一个人。
--                              带四样可信度，卡片上原样标出来：可信度不够的
--                              也展示，但必须配着可信度
--
-- 每一列：
--
--     channel          'email' 或 'phone'
--     value            例 'contact@hushly.example'，原样存；去重按小写比（见唯一索引）
--     source           上面两个来源之一
--     source_url       在哪一页找到的，例 'https://home.hushly.example/'（跳转之后的地址）。
--                      linkedin_email_search 的是那个人的主页链接
--     person_lookup_id 从哪一次查人来的，website 来源的是空。那一次查人被删了，这一行跟着删
--     email_status     例 'valid' / 'risky'，找邮箱那边给的验证结果
--     catch_all        例 true，对方邮箱服务器收所有地址，所以验证不了这个地址是不是真的
--     found_in_google  例 true，这个地址在谷歌搜索结果里公开出现过
--     quality_score    例 75，找邮箱那边给的 0-100 质量分
--
-- 后四列只有 linkedin_email_search 有，website 来源的四列全是空。
--
-- 同一家、同一个来源、同一个地址只存一行（唯一索引，地址按小写比）：
-- find_contacts 崩溃之后重跑，官网会再抓一次（不花钱），抓到的还是那几个地址，
-- on conflict do nothing 就跳过了。同一个地址从两个来源都找到了是两行，
-- 两条出处都值得印。
--
-- 新找到的跟以前的不一样（比如换了一个邮箱）：新旧都留，卡片按 found_at 印最新的在前。
-- ══════════════════════════════════════════════════════════════════
create table lead_contacts (
    id bigserial primary key,
    lead_id bigint not null references leads (id) on delete cascade,
    run_id  bigint references runs (id) on delete set null,
    -- cascade 不是 set null：下面那条 CHECK 要求个人主页来的邮箱必须带着这一格。
    -- set null 的话删一行 person_lookups 会撞那条 CHECK、整句删除失败；
    -- 而且查人那一行没了，这个邮箱是谁的就说不清了，留着也印不到卡片上
    person_lookup_id bigint references person_lookups (id) on delete cascade,

    channel    text not null check (channel in ('email', 'phone')),
    value      text not null check (value <> ''),
    source     text not null check (source in ('website', 'linkedin_email_search')),
    source_url text,

    email_status    text,
    catch_all       boolean,
    found_in_google boolean,
    quality_score   int check (quality_score between 0 and 100),

    found_at timestamptz not null,

    -- 从个人主页找来的必须知道是哪一次查人，否则卡片上印不出「这是谁的邮箱」
    constraint lead_contacts_email_search_has_person
        check (source <> 'linkedin_email_search' or person_lookup_id is not null)
);

create unique index lead_contacts_dedup_idx
    on lead_contacts (lead_id, channel, lower(value), source);


-- ══════════════════════════════════════════════════════════════════
-- actor_spend —— 每调一次付费 actor，记一行花了多少钱
--
-- 月度上限算已花时，采集那一份在 query_executions.billed_item_count 里。找联系方式是另外
-- 两个 actor，钱不经过那张表，不单独记的话上限就漏算了这一块。
--
--     actor        例 'harvestapi/linkedin-company'
--     purpose      'company_profile' 或 'person_email'，一个 actor 以后可能有两种用法
--     item_count   这一次调用计费了几条，例 1（一家公司、一个人）
--     cost_usd     这一次花了多少，例 0.00405
--     lead_id      为哪家公司花的；一次调用查了好几家的话是空
--
-- cost_usd 取 Apify 自己算的这一次扣费（跑完之后状态回复里的 usageTotalUsd），不是我们拿价格表
-- 乘出来的：找邮箱只在找到的时候按高价收，按价格表算会多记。Apify 没回这个数时才按价格表记，
-- 单价在 config/contacts.yaml。
--
-- numeric(10, 5)：单价小到 $0.00005（启动费），用浮点存会有 0.30000000000000004 那种尾巴，
-- 加起来跟上限比大小时差一点点就判错。
--
-- 还剩一个缺口：调完 actor、这一行还没提交就崩溃了，这一行就没了，而 Apify 已经扣了钱。
-- 找联系方式那边调完 actor 马上单独提交这一行，把这个窗口压到最小。所以这里只会比账单少，不会多。
-- ══════════════════════════════════════════════════════════════════
create table actor_spend (
    id bigserial primary key,
    run_id  bigint references runs (id) on delete set null,
    lead_id bigint references leads (id) on delete set null,

    actor      text not null,
    purpose    text not null check (purpose in ('company_profile', 'person_email')),
    item_count int not null check (item_count >= 0),
    cost_usd   numeric(10, 5) not null check (cost_usd >= 0),

    called_at timestamptz not null
);

-- 月度上限按账期起点往后加总
create index actor_spend_called_at_idx on actor_spend (called_at);
