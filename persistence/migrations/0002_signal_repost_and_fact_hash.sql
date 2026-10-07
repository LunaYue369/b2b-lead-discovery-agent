-- 转发那一半落库，以及 fact_key 改成纯摘要
--
-- 一、原帖那一半以前完全没存。signals 里没有任何 reposted_from 的列，
--     所以 repost_author / repost_headline / repost_mentioned 三条通道的候选，
--     它们的证据原文在库里查不到。渲染卡片时回读 signals.text 拿到的是
--     转发者自己写的那段，而真正有内容的那段在原帖里。
--     实测 191 条里 16 条是转发，产出 14 个候选（221 里的 6%）。
--
--     不给原帖单独开一行，是因为那样必须给它编一个 query_search 和
--     matched_terms —— 那条 query 返回的是转发，不是原帖。往持久层里
--     写假事实，而 memory processor 正是拿 query_search 做归因的。
--
-- 二、fact_key 改名 fact_hash，值从 "repost:原帖id" / "content:摘要"
--     变成一个纯摘要。原因是旧写法有个洞：同一个原帖被转发、而原帖本身
--     也被单独抓到时，两条记的键不同（'repost:A' vs 'content:hash(A)'），
--     corroboration 会把一件事数成两件。改成一律按摘要之后两边天然相等。
--     哪一段的摘要由 normalize 按「转发者自己那段还有没有相关性」决定。
--     改名是因为它不再是带类别标签的键，留着旧名字会让代码和库对不上。

alter table signals rename column fact_key to fact_hash;
alter index signals_fact_key_idx rename to signals_fact_hash_idx;

-- 五列都可空：非转发的信号这五列全是 NULL，而不是空字符串。
-- 空字符串会让「这条不是转发」和「这条是转发但原帖正文为空」分不开。
alter table signals
    add column reposted_external_id   text,
    add column reposted_text          text,
    add column reposted_actor_name    text,
    add column reposted_actor_headline text,
    add column reposted_actor_url     text;

-- 要么五列全空，要么至少 id 和正文有值。半填的行意味着 adapter 漏翻译了
-- 一个字段，而那种行在几周后表现为「这条线索的证据原文是空的」。
alter table signals
    add constraint signals_repost_all_or_nothing check (
        (reposted_external_id is null and reposted_text is null)
        or (reposted_external_id is not null and reposted_text is not null)
    );
