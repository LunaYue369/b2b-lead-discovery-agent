-- 退避次数落库，以及「哪条 query 命中了哪家公司」的明细账
--
-- 一、leads 加 cooldown_strikes
--
--     cooldown 退避（复查一次分数没起色就把冷却期翻倍，180 天封顶）要求
--     记住「连续几次没起色」。next_cooldown 已经按这个数算了，但库里没有
--     地方存它，每次分析都从 0 起算 —— 退避那一档等于没实现，而认不出来
--     的供应商正是靠它自己沉下去的。
--
--     默认 0 而不是 null：0 的含义是「还没有连续没起色的记录」，对第一次
--     分析和对刚刚提升过的公司都成立，没有第三种情况需要用 null 表达。
--
-- 二、新表 lead_discovery_hits
--
--     query_executions 的六个计数列答不了一个问题：这家公司是不是只有这条
--     query 找得到。两条高度重叠的 query 每天发现同一批公司时，它们各自的
--     novel_lead_count 都很好看，指标说「两条都很棒」，真相是留一条就够。
--     而 novel_lead_count 决定 proven 池的排名，proven 占 60% 的采集预算。
--
--     存明细不存计数列，是因为「独占发现」的算法现在还没定死（按跑算还是
--     按时间窗算、要不要看分数）。有明细，换一种算法是改一句 SQL；只有
--     计数列，换算法要重新跑历史。
--
--     归因落在这张表上，而不是给 signals 加一个 query_id 列：一条帖能被
--     多条 query 搜到，压进一列必然丢；而且同一条帖第二次被别的 query 搜到
--     时不会重新落行（撞 external_id 唯一键跳过），那一列会永远记着第一次
--     的归因。
--
--     append-only：不去重、不更新。这是 query 表现的原始账本，改写它等于
--     改写历史。唯一约束不是拿来去重的，是拿来炸的 —— 同一跑里同一条
--     query 对同一家公司只该有一行（_attribute 按 lead_id 去重之后才生成
--     流水），出现第二行说明归因那一步重复计数了，而那种错落库之后表现为
--     「这条 query 的独占发现莫名其妙地多」，几周后才会被怀疑。

alter table leads
    add column cooldown_strikes int not null default 0
        check (cooldown_strikes >= 0);

create table lead_discovery_hits (
    id       bigserial primary key,
    query_id bigint not null references queries (id),
    lead_id  bigint not null references leads (id) on delete cascade,
    run_id   bigint not null references runs (id) on delete cascade,

    -- 这家公司是不是这一跑头一回进库。重叠命中时每条 query 各记一次
    -- 同样的 true —— 「头一回见到」是公司的属性，跟哪条 query 在问它无关。
    -- 归给「先跑的那条」是错的：那取决于 selector 今天把谁排在前面，
    -- 同样的库状态会因为排序不同给出不同的归因。
    novel boolean not null,

    created_at timestamptz not null default now(),

    -- 列序是按查询方向排的：最常问的是「这条 query 历来命中过哪些公司」，
    -- query_id 打头的索引同时服务它和下面那条唯一性检查。
    unique (query_id, lead_id, run_id)
);

-- 反方向：「这家公司是被哪几条 query 找到的」，算独占发现要用。
create index lead_discovery_hits_lead_idx on lead_discovery_hits (lead_id);
