-- 初始 schema。八张业务表 + schema_migrations。
--
-- 列名和取值一律跟 models/ 里的 dataclass 与枚举对齐，不另起一套口径。
-- 两边漂了的症状是「代码里叫 novel_lead_count、库里叫 new_leads」，
-- 查一个数要先翻译一次，而翻译错了没人发现。
--
-- 所有 check 约束里的取值都能在 models/ 里找到对应的 StrEnum。
-- 加取值的时候两边一起加，别只加一边。

-- ══════════════════════════════════════════════════════════════════
-- runs —— 一次 discovery 执行。一次 run 里跑好几条 query。
--
-- 跟 query_executions 分开是因为 partial 这个状态只在这一层有意义：
-- 五条 query 里两条超时、三条成功，run 是 partial，而每条 execution
-- 各自要么成功要么失败。压成一张表就没地方记这件事了。
-- ══════════════════════════════════════════════════════════════════
create table runs (
    id          bigserial primary key,
    run_type    text        not null check (run_type in ('scheduled', 'interactive')),
    status      text        not null check (status in ('running', 'completed', 'failed', 'partial')),
    run_date    date        not null,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    error       text
);

create index runs_run_date_idx on runs (run_date desc);


-- ══════════════════════════════════════════════════════════════════
-- queries —— 有哪些 query、怎么来的、算哪一档、多久跑一次。
-- ══════════════════════════════════════════════════════════════════
create table queries (
    id        bigserial primary key,
    text      text not null,

    -- 归一化之后唯一。没有这条约束，memory processor 每天会生成一批
    -- 只差大小写和空格的近似重复 query，candidate 池自我污染，
    -- 每条都跑不满最小样本，于是永远评价不了谁好谁坏。
    text_normalized text not null unique,

    source_type text not null check (source_type in ('linkedin_post')),
    source      text not null check (source in ('manual', 'interactive', 'memory_generated')),

    -- 这条 query 是哪条 memory 学出来的，存的是 memories.key（下面有外键）。
    -- source='memory_generated' 时必填，用来回答「这条 memory 到底带来了
    -- 什么」——没有它，memory 的价值只能靠猜。
    --
    -- 取值是描述这条经验的短横线小写串，人和代码都能读，例如：
    --     'crowdfunding-audio-beats-baseline'
    --         带众筹词的 query 在音频品类上合格率高于基线
    --     'supplier-seo-floods-manufacturer-queries'
    --         带 manufacturer 的 query 会大量召回工厂自己的 SEO 文
    --     'stock-queries-need-weekly-cadence'
    --         存量类 query 每天跑没有新增，改成七天一次
    --
    -- 不用自增数字：在 queries 表里扫一眼就该看出这条 query 是哪条经验
    -- 生出来的，而 id 要再查一次表。key 在 memory 被重算时也不变
    -- （变的是 confidence 和 support）。
    generated_by_memory_key text,

    -- pinned 人工钦定不参与筛选；proven 硬指标达标；candidate 试跑期。
    -- 每天额度 pinned 20% / proven 60% / candidate 20%。
    pool text not null check (pool in ('pinned', 'proven', 'candidate')),

    -- 这条 query 隔多少天才值得再跑一次。组队时跳过
    -- last_run_at + cadence_days 还没到的那些。
    --
    -- 存量类填 7：同样那三个人反复出现，天天跑只会天天捞回同一批。
    -- 流量类填 1：内容持续更新（众筹、新品发布），今天不跑就漏了。
    -- 不分开的话，存量 query 的 novel_lead_count 会长期偏低 ——
    -- 不是因为它不好，是因为跑得太勤 —— 然后被 proven 排名不公平地降级。
    cadence_days int not null check (cadence_days >= 1),

    is_active  boolean     not null default true,
    last_run_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    constraint queries_memory_key_required
        check (source <> 'memory_generated' or generated_by_memory_key is not null)
);

create index queries_pickable_idx on queries (pool, is_active, last_run_at);


-- ══════════════════════════════════════════════════════════════════
-- query_executions —— 跑一条 query 的账。列跟 ExecutionCounters 逐一对应。
--
-- 这张表存在的理由是「哪条 query 值得花钱」，所以计数比结果重要。
-- 尤其是 novel_lead_count：没有它，一条只会重复发现别人已发现公司的
-- query，看起来跟真正带来新线索的一样好 —— 而 proven 占 60% 的预算。
-- ══════════════════════════════════════════════════════════════════
create table query_executions (
    id       bigserial primary key,
    run_id   bigint not null references runs (id) on delete cascade,
    query_id bigint not null references queries (id),

    status      text not null check (status in ('running', 'completed', 'failed')),
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    error       text,

    raw_result_count  int not null default 0,  -- 我们能用的 post 有几条

    -- 实际付钱的条数。跟上面那个不相等，因为 Apify 按 dataset 里的 item
    -- 计费，而评论会作为独立 item 混在同一个 dataset 里。
    --
    --     一条 query 要 60 条 post
    --     → dataset 里实际躺着 120 个 item（60 条 post + 60 条评论）
    --     → raw_result_count = 60   我们能用的
    --     → billed_item_count = 120 我们付钱的
    --
    -- 两个都记，是因为要优化的指标是「每块钱几个合格线索」，分母得是
    -- 付出去的钱。只记 60 的话，一条评论特别多的 query 看着跟别的一样便宜。
    billed_item_count int not null default 0,

    signal_dropped_count    int not null default 0,
    candidate_dropped_count int not null default 0,

    -- prefilter 两级各条规则各砍了多少，形如
    --     {"no_relevance_signal": 61, "company_page_author": 28,
    --      "seen_hash_in_run": 2, "hardware_store": 1}
    --
    -- 注意它**只覆盖 prefilter 那一段**，不是整条漏斗的缩水原因。
    -- 一次执行的完整漏斗有六段，每段的账记在不同的列上：
    --
    --     1. 供应商返回          raw_result_count / billed_item_count
    --     2. prefilter signal 级  signal_dropped_count   ← drop_reasons 记原因
    --     3. 展开候选后 candidate 级 candidate_dropped_count ← drop_reasons 记原因
    --     4. 按身份归并成公司     unique_company_count
    --     5. cooldown 分流        researchable / cooldown_skipped / excluded_skipped
    --     6. analyzer 打分过线    qualified_lead_count
    --
    -- 所以 cooldown 跳过的、分数不过线的都不在 drop_reasons 里 ——
    -- 它们不是「被规则砍掉」，是「这次不用花钱看」和「看了不够好」，
    -- 三件事混在一个字段里，报表就没法回答「规则误砍了没有」。
    --
    -- 「各项合计 = signal_dropped_count + candidate_dropped_count」这条
    -- 只有 Python 那层守着（ExecutionCounters.check()），库里没有、也焊不上：
    -- 对 jsonb 求和要子查询，而 Postgres 的 CHECK 不允许子查询。要在库层
    -- 保证得上 trigger 或生成列，现在不值得。所以绕过 finish_execution
    -- 直接写库的路径，这一条拦不住 —— 这里写清楚，免得当成有保证。
    drop_reasons jsonb not null default '{}'::jsonb,

    unique_company_count    int not null default 0,
    researchable_lead_count int not null default 0,
    cooldown_skipped_count  int not null default 0,
    excluded_skipped_count  int not null default 0,
    novel_lead_count        int not null default 0,
    qualified_lead_count    int not null default 0,

    -- ExecutionCounters.check() 在落库前跑同样的自查。这里再焊一层，
    -- 是因为绕过那个对象直接写库的路径迟早会出现。
    constraint query_executions_triage_adds_up check (
        status <> 'completed'
        or researchable_lead_count + cooldown_skipped_count + excluded_skipped_count
           = unique_company_count
    ),
    constraint query_executions_qualified_within_researchable
        check (qualified_lead_count <= researchable_lead_count),
    constraint query_executions_novel_within_unique
        check (novel_lead_count <= unique_company_count)
);

create index query_executions_query_idx on query_executions (query_id, started_at desc);
create index query_executions_run_idx on query_executions (run_id);


-- ══════════════════════════════════════════════════════════════════
-- signals —— post 级证据。一条 raw item 一行。
--
-- 不存供应商返回的原始 JSON：实测一条 5.2 KB，一天 200 条就是
-- 365 MB/年，会吃掉免费层 0.5 GB 的七成。原始 JSON 已经在 git 里
-- （data/harvest_*.json），那份才是回归集。要回溯原文，库里的 url
-- 点开就是原帖，external_id 也能对回 git 那份。
-- ══════════════════════════════════════════════════════════════════
create table signals (
    id     bigserial primary key,
    run_id bigint references runs (id) on delete set null,

    source      text not null,  -- "harvestapi_posts"
    source_type text not null check (source_type in ('linkedin_post')),
    -- 供应商的 id 列。同一条帖被两条 query 搜到时靠它去重。
    external_id text not null unique,
    url         text not null,

    -- 唯一可引用的原文，防幻觉闸门 assert 的对象。
    text text not null,
    -- 正文轻归一化后的摘要。跨天 seen 查的就是这一列。
    content_hash text not null,
    -- 「是不是同一件事」。转发用 ("repost", 原帖id)，其他用 ("content", hash)。
    -- corroboration 按它去重，而不是按 content_hash —— 一份公告被联播
    -- 一轮，正文各不相同但说的是同一件事。
    fact_key text not null,

    query_search  text not null,      -- 归因：哪条 query 搜出来的
    matched_terms text[] not null default '{}',

    actor_name     text not null,
    actor_headline text not null,
    actor_url      text not null,
    actor_type     text not null check (actor_type in ('profile', 'company')),

    -- 这件事什么时候发生（转发时取原帖时刻），recency 用它。
    event_at    timestamptz not null,
    -- 我们什么时候看到的。
    observed_at timestamptz not null,
    created_at  timestamptz not null default now()
);

-- 跨天 seen 查这条索引：
--   select content_hash from signals where created_at > now() - interval '10 days'
-- 窗口定成 10 天不是因为存储（一年 7 万行加索引也就几 MB），是因为超过
-- 十天还一模一样的正文再出现，更可能是新事件而不是同一轮联播的尾巴。
-- 跨过窗口的重复交给 leads 的 cooldown 按公司判 —— 两套记忆各管各的。
create index signals_seen_window_idx on signals (created_at desc, content_hash);
create index signals_fact_key_idx on signals (fact_key);


-- ══════════════════════════════════════════════════════════════════
-- leads —— 公司级。装所有分析过的公司，不是只有合格的。
--
-- 这是整套 schema 里最省钱的一条设计：不合格的公司也必须留记录，
-- 否则明天同一条 query 又搜到它、又送一次 LLM，而按实测分布
-- 80% 到 90% 属于这一类。
-- ══════════════════════════════════════════════════════════════════
create table leads (
    id bigserial primary key,

    -- 身份键，跟 CompanyRef.identity_key 一一对应：
    -- li_id > li_slug > domain > 归一化名，取到哪级算哪级。
    -- 唯一约束加在这一对上，而不是加在下面四列各自身上 ——
    -- 一家公司只有名字的时候，另外三列都是空的。
    identity_kind  text not null check (identity_kind in ('li_id', 'li_slug', 'domain', 'name')),
    identity_value text not null,

    -- 四列都留着，是为了 dedup 能「拿所有非空字段逐级查一遍」：
    -- 两条引用指向同一家公司但停在不同梯度时（一条有 id、一条只有名字），
    -- 单靠 identity_key 归并不到一起。命中任一级即同一家，然后把 id 补上。
    linkedin_company_id text,
    linkedin_slug       text,
    domain              text,
    normalized_name     text not null,
    display_name        text not null,

    status text not null default 'new'
        check (status in ('new', 'pushed', 'in_draft', 'excluded')),

    -- 下次可以再花钱分析的时刻。按结果分级，不是固定 30 天：
    -- excluded 是终态永不复查（cooldown_until 为 null 且 status='excluded'），
    -- 别的按策略算。固定天数会把预算平摊给已知垃圾。
    cooldown_until timestamptz,

    -- 打分。llm_* 是 LLM 填的，det_* 是代码算的，分开存是为了事后能
    -- 分别回答「模型判得准不准」和「确定性维度算得对不对」。
    -- 加权之后是 score，权重放 config 不放这里 —— 调权重不该动 schema。
    score int check (score between 0 and 100),

    -- 品类对不对：麦克风、耳机、带麦可穿戴、会议音响、录音笔。
    -- 品类不对的一律低分，不管公司多大多有钱。
    llm_icp_fit int check (llm_icp_fit between 0 and 10),

    -- 事件有多硬。众筹刚结束 > 在招 NPI 工程师 > 发帖讲愿景。
    -- 量的是「现在正在发生一件需要找代工厂的事」，不是「这家公司好不好」。
    llm_signal_strength int check (llm_signal_strength between 0 and 10),

    -- 说话这个人能不能拍板。创始人和 CTO > 产品经理 > 工程师。
    llm_seniority int check (llm_seniority between 0 and 10),

    -- 反向维度：我们 MOQ 500，所以「刚众筹完只卖 800 台」正好，
    -- 而大厂完美命中所有品类词但永远聊不成，该低分。
    -- 这一维专门防「看着很对但注定谈不下来」那一类。
    llm_moq_fit int check (llm_moq_fit between 0 and 10),

    llm_analysis text,  -- 为什么值得 outreach，人话

    -- 由 Signal.event_at 算，不是 observed_at。转发的信号两者不同，
    -- 而 recency 量的是「这件事什么时候发生」，不是「我们什么时候刷到」。
    det_recency int check (det_recency between 0 and 10),

    -- 够不够得着：有没有 LinkedIn URL、有没有域名。
    -- 现在粒度很粗（有没有链接），enrich 拿到官网和具体联系人之后要细化。
    det_reachability int check (det_reachability between 0 and 10),

    -- 几件互相独立的事指向同一家公司。由 CompanyCluster.corroboration 映射，
    -- 纯计数不用 LLM。两个互不相干的作者各提到同一家公司一次，
    -- 这个确定性任何单条信号自己都给不出来，而且免费。
    det_corroboration int check (det_corroboration between 0 and 10),

    first_seen_run_id bigint references runs (id) on delete set null,
    last_analyzed_at  timestamptz,
    pushed_at         timestamptz,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),

    unique (identity_kind, identity_value),

    -- excluded 是终态，不该再有 cooldown。写进约束是因为
    -- 「终态还带着复查时刻」这种数据只会在几周后表现为莫名其妙的重复分析。
    constraint leads_excluded_has_no_cooldown
        check (status <> 'excluded' or cooldown_until is null)
);

create index leads_cooldown_idx on leads (status, cooldown_until);
create unique index leads_linkedin_company_id_idx
    on leads (linkedin_company_id) where linkedin_company_id is not null;
create unique index leads_linkedin_slug_idx
    on leads (linkedin_slug) where linkedin_slug is not null;
create unique index leads_domain_idx on leads (domain) where domain is not null;
create index leads_normalized_name_idx on leads (normalized_name);


-- ══════════════════════════════════════════════════════════════════
-- lead_evidence —— 哪些信号支持这条 lead。
--
-- 唯一键是 (lead_id, content_hash) 而不是 (lead_id, signal_id)：
-- 同一篇正文被五条 query 各命中一次，signal_id 不同但证据只有一份，
-- 按 signal_id 去重会存五行，corroboration 跟着虚高五倍。
-- ══════════════════════════════════════════════════════════════════
create table lead_evidence (
    id           bigserial primary key,
    lead_id      bigint not null references leads (id) on delete cascade,
    signal_id    bigint not null references signals (id) on delete cascade,
    content_hash text   not null,
    -- 这条证据是从哪个通道来的，取值同 LeadType。
    lead_type text not null check (
        lead_type in ('author', 'headline', 'mentioned',
                      'repost_author', 'repost_headline', 'repost_mentioned')
    ),
    created_at timestamptz not null default now(),

    unique (lead_id, content_hash)
);

create index lead_evidence_signal_idx on lead_evidence (signal_id);


-- ══════════════════════════════════════════════════════════════════
-- lead_feedback —— Riley 对一条 lead 的态度。append-only。
--
-- 刻意允许一条 lead 有多行：Riley approve 之后改稿改到一半反悔，
-- 要能写第二行，而不是把第一行改掉。改掉的话「她当时怎么想的」就没了。
--
-- ignored 是必须有的：大部分卡片是不回应，沉默是弱负信号。
-- 只记 approve 和 reject 会让 human approval yield 的分母是错的，
-- 而那个指标决定每天 60% 的预算怎么分。
--
-- 改稿意见不进这张表，等 draft agent 时另开 draft_revisions ——
-- 混进来会污染 query 的 approval yield。
-- ══════════════════════════════════════════════════════════════════
create table lead_feedback (
    id      bigserial primary key,
    lead_id bigint not null references leads (id) on delete cascade,

    -- approve   要写稿。同时把 leads.status 推进 in_draft，discovery 不再捞它
    -- skip      这次先不联系，但不否定这家公司。以后再出现照样推
    -- reject    不该联系。这是最值钱的一种反馈 —— 它直接告诉我们规则该往哪改
    -- ignored   超时未处理。系统写的不是 Riley 写的，由定时任务把超过 N 天
    --           没动作的卡片扫成这个。沉默是弱负信号，不记的话
    --           approval yield 的分母就是错的
    feedback_type text not null
        check (feedback_type in ('approve', 'skip', 'reject', 'ignored')),

    -- Riley 写的理由，可以为空。
    -- 怎么来的：Slack 卡片上三个按钮，approve 和 skip 点了直接落库不弹窗
    -- （她一天要过很多张，每张都弹窗会让人干脆不点）；reject 弹一个
    -- Slack modal 问一句为什么，可以跳过不填。
    -- 只在 reject 上加这道摩擦，是因为 approve 的理由基本能从 lead 自己
    -- 推出来，而 reject 的理由推不出来 —— 「品类不对」和「这家太大」
    -- 指向完全不同的修法，一个改 query 一个改打分权重。
    -- ignored 这里永远是空的。（落地在 step 6 Slack 按钮那一步）
    note text,

    -- 谁写的。ignored 记 'system'，其余记 Slack user id。
    -- 不做外键，没有 users 表，也不打算为这个建一张。
    author text not null,

    created_at timestamptz not null default now()
);

create index lead_feedback_lead_idx on lead_feedback (lead_id, created_at desc);


-- ══════════════════════════════════════════════════════════════════
-- memories —— memory processor 从历史表现里学出来的经验。
--
-- 一张表带 scope，不是按域拆成三张。判据不是「是不是同一类东西」，
-- 是「会不会进同一个分母」：每条 memory 的 confidence 各按自己的
-- support 算，query 的经验和 lead 的经验不共用任何分母，混在一起
-- 不会互相污染。而拆开意味着同样的 confidence 计算和淘汰规则写三遍 ——
-- 「confidence 不许 LLM 自己写、由 stats 算」这条最怕的就是实现漂掉。
-- （对照：改稿意见必须跟 lead_feedback 分开，因为那两者确实进同一个分母。）
--
-- scope 里先不放 draft：Draft agent 这期不做也不预留接口。
-- 加域的时候改这条 check 就是一条 migration。
-- ══════════════════════════════════════════════════════════════════
create table memories (
    -- id 是数据库自己的主键，没有含义，只给内部外键用。重建库会变。
    id bigserial primary key,

    scope text not null check (scope in ('query', 'lead')),

    -- key 是这条经验的名字，人和代码都读它，重算时不变（变的是
    -- confidence 和 support）。queries.generated_by_memory_key 存的是它，
    -- 不是 id —— 在 queries 表里扫一眼就该看出这条 query 是哪条经验生的。
    -- 取值形如 'crowdfunding-audio-beats-baseline'。
    --
    -- 全局唯一而不是 (scope, key) 唯一：这样 queries 那边才能加真正的外键。
    -- key 本来就是描述性的短语，两个 scope 撞名的情况不该发生 ——
    -- 真撞了说明那两条经验讲的是一回事，该合并不该并存。
    key text not null unique,

    -- 人话，给人看的。不另设一份「给 LLM 看的」：
    -- 读自然语言正是 LLM 最擅长的事，专门给它写一份结构化表述没有收益，
    -- 而两份表述一定会漂 —— 改了人话忘了改机器版，之后查 bug 看的是人话、
    -- 实际跑的是机器版，这种错最难发现。
    -- 真需要机器读的结构化东西（阈值、词表、query 模板）放 support 里，
    -- 那本来就是 jsonb。
    statement text not null,

    -- 由 query_intelligence/stats.py 从 query_executions 聚合出来，
    -- 是我们自己算的，不是 LLM 写的也不是外部来的。
    -- LLM 自己写的置信度是它对自己判断的感觉，不是这条经验在数据上的
    -- 支撑强度，两者没有关系。
    confidence numeric(4, 3) not null check (confidence between 0 and 1),

    -- 算 confidence 用的原始数，形如
    --     {"executions": 42, "qualified_rate": 0.19, "baseline": 0.07}
    --     executions      这条经验背后有多少次执行撑着（样本量）
    --     qualified_rate  这批执行的合格线索率
    --     baseline        同期全部 query 的平均，用来比较
    -- 存原始数而不是只存结论，一是几周后能回答「这个 0.83 当时怎么来的」，
    -- 二是换了算法能拿旧数据重算。
    support jsonb not null,

    -- retired 的不再生成新 query，但保留 —— 淘汰过程本身是数据。
    -- 而且 queries 那边有外键指着，删不掉也不该删。
    status text not null default 'active' check (status in ('active', 'retired')),

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index memories_active_idx on memories (scope, status, confidence desc);


-- queries 建在 memories 前面，所以外键在这里补。
-- 加真外键的好处是拼错的 key 当场炸，而不是留一条指向不存在经验的 query，
-- 那种 query 会一直跑、一直花钱，而「它为什么存在」永远查不出来。
alter table queries
    add constraint queries_generated_by_memory_fk
    foreign key (generated_by_memory_key) references memories (key);
