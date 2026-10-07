-- 把转发那条 all-or-nothing 约束收紧到五列
--
-- 0002 里写的那条只管了 reposted_external_id 和 reposted_text 两列：
--
--     check ((external_id is null and text is null)
--            or (external_id is not null and text is not null))
--
-- 而它的注释说的是「要么五列全空，要么至少 id 和正文有值」。前半句库里
-- 其实不成立：id 和正文都为空、但 reposted_actor_name 有值的行能过。
--
-- 现在碰不到这种行，因为 discovery_db._repost_columns 是五列一起给的。
-- 问题正出在这儿 —— 一条只在「调用方碰巧守规矩」时才成立的约束，是假
-- 安心，跟 not null 挡不住空字符串是同一类。以后多一个写 signals 的路径
-- （interactive、补数据脚本、修数据的手工 SQL），它就静默地不管用了。
-- 而半填的行几周后表现为「这条线索的证据原文是空的」，那时已经查不回来。
--
-- 新写法：五列要么全空，要么至少 id 和正文有值（另外三列仍允许单独为空，
-- 因为原帖作者的职位串和主页 URL 供应商确实可能不给）。
-- 区别在于三个作者列不再能脱离 id/正文单独存在。

alter table signals drop constraint signals_repost_all_or_nothing;

alter table signals
    add constraint signals_repost_all_or_nothing check (
        (
            reposted_external_id is null
            and reposted_text is null
            and reposted_actor_name is null
            and reposted_actor_headline is null
            and reposted_actor_url is null
        )
        or (reposted_external_id is not null and reposted_text is not null)
    );
