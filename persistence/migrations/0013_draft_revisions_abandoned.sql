-- draft_revisions 的判决多一种：abandoned
--
-- 停在看稿或看计划那一停、太久没人回，还在等他表态的稿，由定时扫描（triggers/sweep_drafts.py）
-- 记成 abandoned。跟 lead_feedback 的 ignored 同一个用处：沉默也是反馈，不记的话
-- 学写稿时只看得到他回过的那些稿。
--
-- 只放宽 verdict 的取值，别的不动。abandoned 那几行 user_reply 是空串（他没说话），
-- reply_index 是「下一句」的位置（他以后真回了话，那句的下标在后面，不撞唯一键）。

alter table draft_revisions drop constraint draft_revisions_verdict_check;
alter table draft_revisions add constraint draft_revisions_verdict_check
    check (verdict in ('approved', 'rewrite', 'replan', 'dropped', 'cancelled', 'abandoned'));
