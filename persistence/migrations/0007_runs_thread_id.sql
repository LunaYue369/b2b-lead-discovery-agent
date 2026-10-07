-- runs 记下「这一跑是哪次对话找出来的」
--
-- scheduled 那条路留空，interactive 写 thread_id。
--
-- 没有这一列的话，从一条线索反查不到它的来历。leads 上已经有
-- first_seen_run_id 和 pushed_run_id 两个指向 runs 的口子，顺着它们能查到
-- 「哪一跑采到的」，但那一跑本身只有 run_type='interactive' 一个字，
-- 答不了「是谁、在哪条对话里、为了什么让它跑的」。而 interactive 这条路上
-- 那句话恰恰是唯一解释得了「为什么会去搜这个词」的东西 —— 词本身落在
-- queries 表里，为什么搜它只在对话里。
--
-- 值的形状是 'slack:<频道 id>:<thread 时间戳>'，例如
-- 'slack:C0C0139:1788400486.110719'。它是派生出来的，不是另一张表的主键：
-- 这个映射是一条规则不是一个事实，所以不建 interactive_sessions 表 ——
-- 建了就有两处真相，而且每次多一趟 Neon。
--
-- text 不是外键：对面那头是 LangGraph 自己那四张表（checkpoints 等），
-- 它们的主键是 (thread_id, checkpoint_ns, checkpoint_id) 三元组，指不过去。
-- 而且对话是可以删的（delete_thread），账本不能跟着一起没。
--
-- 允许为空，而且不加 CHECK 焊「interactive 必须有值」：0007 之前的
-- interactive 跑（现在还没有）以及将来可能出现的别的入口都会是空的，
-- 留空在查询时 join 得掉、看得见；加约束反而会在补历史数据时挡路。

alter table runs
    add column thread_id text;

-- 从一条对话查它跑过几次。interactive 的量很小，但这个方向的查询是常态
-- （「上次那条对话采到什么了」），而反方向已经有主键了。
create index runs_thread_id_idx on runs (thread_id) where thread_id is not null;
