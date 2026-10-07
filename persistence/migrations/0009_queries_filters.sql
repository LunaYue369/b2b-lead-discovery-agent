-- queries 记下「这条词是带着哪几个采集旋钮跑的」，并且让旋钮参与身份
--
-- 这一列装的是**会改变采回来的是什么**的那些参数，也就是 actor 除了 searchQueries
-- 之外的筛选字段：authorKeywords（按 headline 和 job title 筛发帖人）、
-- authorsCompanies、authorsIndustryId、contentType 这几个。
--
-- 为什么它必须参与唯一键（这是这条 migration 真正在做的事）：
--
--     text 一样、旋钮不同 = 采回来的是两批不同的帖子 = 两条不同的 query。
--     不改唯一键的话，第二条永远进不了库 —— upsert_query 撞上
--     text_normalized 那条唯一约束，静默返回第一条的 id，于是第二条采回来的
--     公司、花掉的钱、命中的记录**全记到第一条头上**。
--     而 query_executions 是评价 query 好坏的唯一依据，proven 靠它排名、
--     proven 占 60% 的每日额度 —— 账记错了，排名跟着错，而报表上完全正常。
--
-- 为什么直接对 jsonb 建唯一索引，不另建一个摘要列：
--
--     jsonb 存进去就是规范化的（键排好序、去重、空白丢掉），所以
--     '{"b":2,"a":1}' 和 '{"a":1,"b":2}' 在库里是同一个值，= 号判它们相等。
--     摘要列要解决的「键顺序不同算不算同一条」这个问题，jsonb 自己已经解决了。
--     多一个列就多一处要保持同步的真相。
--
-- 哪些东西**不**进这一列：
--
--     topics          0008 已经单开一列。它是归因不是参数 —— 不改变采回来的
--                     任何东西，所以同一条 text 不管为哪个题目造的都是同一条 query。
--                     混进来会让这条唯一键变成「一半字段该参与、一半不该」
--     我们这侧的排除词  它们在采集之后才生效，不改变采回来的是什么。放进来的话，
--                     改一次排除名单就分裂出一条采集结果完全相同的新 query，
--                     历史统计跟着断掉，而我们什么都没多采到
--     maxPosts / postedLimit / sortBy
--                     这三个是「这一跑怎么跑」，不是「这条词是什么」。同一条 query
--                     今天采 40 条明天采 20 条仍然是同一条 query，它们属于 run
--
-- 键名不在这里用 CHECK 约束卡：允许的旋钮名写在代码里（upsert_query 那个白名单）。
-- 理由是加一个旋钮不该要一条 migration；而且拼错一个键名（authorKeyword 少个 s）
-- 的症状很静默 —— Apify 收到不认识的键会忽略它，而我们这边它已经算进身份了，
-- 于是凭空多出一条永远采不到东西的 query。那个检查放在代码里才拦得住。
--
-- 只卡「它得是个对象」：数组或者裸标量进来的话，上面那条身份语义整个不成立。
--
-- 现有三行 filters 全是 '{}'，行为一个字不变：唯一键从 (text_normalized) 变成
-- (text_normalized, '{}')，对它们来说跟原来完全等价。

alter table queries
    add column filters jsonb not null default '{}';

alter table queries
    add constraint queries_filters_is_object
    check (jsonb_typeof(filters) = 'object');

-- 先加新的再删旧的：中间那一刻两条都在，任何一个并发写入都不会钻空子建出重复行。
create unique index queries_identity_key on queries (text_normalized, filters);

alter table queries
    drop constraint queries_text_normalized_key;
