-- leads 记下「模型这次是凭哪几句原话打的分」，以及「这次是哪一跑打的」
--
-- 在这之前，一次打分里只有一半进了库：
--
--     进了库    score、llm_ 四维、det_ 三维、llm_analysis（为什么值得联系）
--     没进库    evidence_quotes（模型挑出来的逐字引用）
--               evidence_means（卡片上「证据引用」那一句结论）
--
-- 没进库的那一半恰恰是唯一的事实。分数和 analysis 都是判断，引用是帖子原文的
-- 子串，是判断的依据。只存判断不存依据，事后就答不了「这 80 分是凭哪句话给的」。
-- 帖子全文在 signals.text 里，但模型从里面挑了哪一句，库里没有任何地方记着。
--
-- 它直接挡住了三件事：
--
--     一、卡片只能在跑的那个进程里拼。引用只在内存里，进程一退就没了，
--         所以重发某一跑的卡片、或者 interactive 那张图里单开一个推卡节点，
--         都拿不到原话
--     二、Riley 以后标 A/B/C 阈值，要看模型当时看到了什么才能判它判得对不对
--     三、drafting 要把原话抄进邮件
--
-- 三列：
--
--     llm_evidence_quotes    text[]   例 {"We just wrapped our Kickstarter"}
--     llm_evidence_means     text     例 创始人本人说众筹刚结束，正在找量产
--     last_analyzed_run_id   bigint   例 31
--
-- 前两列放 leads，不放 lead_evidence：
--
--     引用跟 llm_analysis 同生同灭，每次重新分析整组覆盖。而 lead_evidence 是
--     一篇帖子一行、只往上加不覆盖，一次分析的两条引用可能出自两篇帖子，
--     挂在哪一行上都不对。
--
--     覆盖意味着上一次的引用会丢，跟今天 llm_analysis 被覆盖是同一个代价。
--     要留历史的话得另开一张「每次分析一行」的表，现在没有要用历史的地方。
--
-- 第三列回答「某一跑打了分的是哪几家」：
--
--     select id from leads where last_analyzed_run_id = 31
--
--     有了它，推卡只需要一个 run_id，不再需要内存里那份 DiscoveryResult。
--     查出来的正好是这一跑打了分的那批：反复答不好被丢弃的那几家不写分数，
--     这一列也不会被写上。
--
--     它跟 last_analyzed_at 是一对，由同一条 UPDATE 写。不能拿
--     first_seen_run_id 顶：一家公司 8-20 头一回进库、9-10 冷却期过了重新分析，
--     这两个 run 不是同一个。也不能拿 lead_discovery_hits 的 run_id 顶：
--     冷却期里的公司这一跑也会被命中、记一行，但它没有被重新分析，库里是旧分数。
--
--     它会被下一次分析覆盖。所以「重发 run 27 的卡片」拿到的是 run 27 之后
--     没有被重新分析过的那几家。被重新分析过的那几家，最新的判断属于后来那一跑，
--     重发旧的反而不对。
--
-- 三列都允许为空，现有的行全是空：
--
--     从来没分析过的公司（冷却中、终态、刚进库还没轮到）是空的；
--     0010 之前分析过的也是空的。不回填：原话已经不在了，
--     拿什么填都是编的。卡片渲染时这一块直接不出现，别的照旧。
--
-- 引用只卡一件事：有值的话至少一条。
--
--     能写进库的打分一定过了 NO_QUOTE 那道检查（一条有效引用都没给的会被重问，
--     问不好就丢弃，不写库）。所以这里出现空数组，说明那道检查被绕过了。
--     留空（null）跟空数组（{}）是两回事：null 是「没存」，{} 是「存了，
--     模型一条都没给」，后者不该出现。
--
-- on delete set null 而不是 cascade，理由同 0006 的 pushed_run_id：
-- runs 那行没了不该把公司也删掉。公司是长期资产，跑是账。
--
-- 不建索引：leads 按一天两条 query 的速度增长，涨到几千行也还是毫秒级的全表扫。
-- 跟 pushed_run_id 一个待遇。

alter table leads
    add column llm_evidence_quotes  text[],
    add column llm_evidence_means   text,
    add column last_analyzed_run_id bigint references runs (id) on delete set null;

alter table leads
    add constraint leads_evidence_quotes_not_empty
    check (llm_evidence_quotes is null or cardinality(llm_evidence_quotes) >= 1);
