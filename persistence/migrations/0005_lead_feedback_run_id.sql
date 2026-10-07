-- lead_feedback 记下「这一下点的是哪一跑推的那张卡」
--
-- 缺这一列会怎样：
--
--     一条 lead 会被反复推。它今天以 67 分推过一次、Riley 没动作（扫成
--     ignored），三周后冷却期过了重新分析、涨到 72 分再推一次、这回她
--     approve 了。两行 feedback 都挂在同一个 lead_id 上，而「她当时看到的
--     是哪一版」只能靠时间戳去猜 —— 猜的方式是拿 created_at 跟 runs 的
--     时间比，那在同一天跑两次（补跑、手动重跑）时就分不开了。
--
-- 为什么归因需要它：approval yield 是「这条 query 找来的线索，Riley 认了
-- 几成」，而哪条 query 找到这家公司记在 lead_discovery_hits (query_id,
-- lead_id, run_id) 上 —— 那张表的键里有 run_id。feedback 不带 run_id 的话，
-- 一条 approve 会算给「历来所有找到过这家公司的 query」，包括三周前那次
-- 推送之后被 Riley 忽略掉的那条。分子分母口径不一致，而那个指标决定每天
-- 60% 的采集预算。
--
-- 为什么不在按钮里塞 query_id（buttons.py 原先的想法，已作废）：
-- 一条 lead 可能被同一跑里的两条 query 同时命中，那正是 corroboration 的
-- 分子。塞一个 query_id 进按钮 payload 等于任选一条，静默错归因。
-- 按钮只带 lead_id 和 run_id，归因去查 lead_discovery_hits ——
-- 那张表就是为这件事建的。
--
-- 允许为空，而且刻意不给默认值：
--     Slack 按钮点回来的  一定带 run_id（卡片是某一跑发出去的）
--     ignored 扫描写的    也带（扫的是某一跑推出去、超时没动作的卡片）
--     以后别的入口        可能真的没有一跑可归（比如 Riley 直接在
--                         interactive 里说「这家不要」，那不是卡片）
-- 填一个假的 run_id 比留空坏得多：留空在归因时会被 join 掉，看得见；
-- 填假的会把一条反馈算给一条根本没找到过这家公司的 query。
--
-- on delete cascade 跟 lead_discovery_hits 那张表一致：runs 那行被删
-- （只发生在整跑回滚，而那时候这些 feedback 行也还不存在）就一起走。

alter table lead_feedback
    add column run_id bigint references runs (id) on delete cascade;

-- 按 run 查「那天推出去的卡收到了哪些反馈」，ignored 扫描和 yield 归因都要走它。
create index lead_feedback_run_idx on lead_feedback (run_id);
