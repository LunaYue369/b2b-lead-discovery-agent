-- 起草图：用户看完稿子，对每一份下的判决，连同被判的那一版正文
--
-- 一行 =「用户说某句话的时候，对第 N 份第 v 版下了什么判决」。只追加，不改旧行：
-- 同一份同一版可以先判 approved、后来又被重新打开判 rewrite，两行都留着，
-- 「她先说可以、后来又想加东西」本身就是以后学写稿风格的原料。
--
-- 用来：
--     一、取终稿：每份取 reply_index 最大的那一行，verdict 是 approved 的就是终稿
--     二、以后学写稿风格（初稿 -> 她怎么说 -> 改成什么 -> 定稿）
--
-- 改稿意见不进 lead_feedback（0001 里写过）：那张表是给线索打分、给 query 归因用的，
-- 混进来会污染 approval yield。
--
-- 谁写：起草图的 record_round 节点，只有它写。它不花钱、不停：classify_feedback 的判决先进存档，
-- record_round 照存档里的判决抄进来，库里和存档里的必然一致。崩了重跑靠唯一键挡重复。
-- 计划阶段改来改去不记（那时还没有稿子，计划的每一版在 checkpoint 里）。
--
-- 例（编的，Acme Audio 一轮跑完的前两句判决）：
--     reply_index  slot  version  plan_version  verdict   feedback
--     7            1     1        1             approved
--     7            3     1        2             replan    改成给 Dana 发 LinkedIn 私信
--     11           3     2        3             approved
--
-- 删除跟着 leads 走（on delete cascade）：稿子都是写给这家公司的。

create table draft_revisions (
    id             bigserial primary key,
    -- 哪条起草对话，例 'draft:C0000000001:1759200000.123456'（checkpoint 的 thread_id）
    thread_id      text        not null,
    lead_id        bigint      not null references leads (id) on delete cascade,
    -- 用户哪一句话下的判决：那句在 state.messages 里的下标。messages 只往后接，
    -- 同一个 thread 里重开几轮下标也不会重复
    reply_index    int         not null,
    -- 第几份（身份证号，一轮里永不复用）
    slot_number    int         not null,
    -- 这一份的第几版正文
    draft_version  int         not null check (draft_version >= 1),
    -- 这版正文照的是计划第几版
    plan_version   int         not null check (plan_version >= 1),
    -- 那一版计划的快照（state.plan.slots 里那一份整个）
    plan_slot      jsonb       not null,
    -- 从快照里抄出来方便直接查：email / linkedin_message / linkedin_note / article
    form           text        not null,
    -- 写给谁；写给公司本身是空
    recipient_name text,
    -- 邮件发到哪；不是邮件是空
    address        text,
    -- 被判的那一版正文（邮件带 Subject 行）
    text           text        not null,
    verdict        text        not null
        check (verdict in ('approved', 'rewrite', 'replan', 'dropped', 'cancelled')),
    -- 给这一份的意见；approved / dropped / cancelled 是空串
    feedback       text        not null default '',
    -- 用户那一整句原话
    user_reply     text        not null,
    created_at     timestamptz not null default now(),
    unique (thread_id, reply_index, slot_number)
);

create index draft_revisions_lead_idx on draft_revisions (lead_id);
