"""三段基数里的三跳。整个设计最关键的一个模块。

    RawSignal ─1:1→ Signal ─1:0..N→ Candidate ─N:1→ CompanyCluster

第一跳（算派生字段）：
    content_hash   正文归一化后的摘要。同一篇正文被不同账号各自发一遍时，
                   作者、URL、external_id 全不同，按公司身份一条都归不到
                   一起，只有正文本身一样。而且要跟跨天已见的比，
                   不然明天同一篇又来一遍。
    matched_terms  命中词在正文里的实际出现。空 = 砍，但砍在 prefilter 里。

第二跳（展开候选）：
    发帖人自己是一个候选；正文里被 @ 链接的每个公司各是一个候选；
    转发的话原帖作者和原帖里被 @ 的公司同样各是一个候选。

    被 @ 的公司只占少数的量，但它是唯一身份精确的通道：LinkedIn 直接给
    公司 id、名字和主页 URL，三个字段完整。而个人作者绝大多数拿不到任何
    公司 id，只能从职位串里猜。

第三跳（按 CompanyRef.identity_key 归并）：
    unique_company_count 在这里算出来，corroboration 白拿。

如果把这三跳压成 1:1（一条结果 = 一个公司 = 发帖人）就会整条丢掉真线索：
加速器从公司主页发帖恭喜被投公司众筹成功 —— 发帖人不是买方，
被提到的公司才是。

这个模块只算不砍。为省一次摘要计算让它提前返回，会让「这条为什么没了」
要翻两个地方查。所有丢弃都在 prefilter。
"""

from __future__ import annotations

import functools
import hashlib
import re
import unicodedata
from collections.abc import Iterable

from models import (
    Candidate,
    CompanyCluster,
    CompanyRef,
    LeadType,
    RawSignal,
    Signal,
    normalize_company_name,
)

# 折叠空白用。把连续的空格、换行、制表符看成同一个东西。
_WHITESPACE = re.compile(r"\s+")

# 零宽字符和方向控制符。它们不改变人读到的内容，但会让摘要不一样。
# 富文本编辑器和复制粘贴都会带进来。
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿]")


def content_hash(text: str) -> str:
    """算正文的内容摘要，用来认出「同一篇正文被不同账号各自发一遍」。

    输入：帖子正文原文
    输出：32 位十六进制摘要字符串

    这个摘要不是拿来做安全用途的，它只回答「这两段正文是不是同一段」，
    所以用哪个算法不重要，够短够快就行。

    归一化的力度是这里唯一需要想清楚的事。两个方向的错代价不对等：

        归一化过度  两篇本来不同的帖子被抹成同一段，摘要相同，其中一条
                    被当成重复丢掉。这是静默丢线索，报表上看不到，
                    只会显得「今天线索少」，查不出原因。
        归一化不足  联播漏抓，corroboration 被同一个事实刷高。
                    这是打分偏差，看得见、能追，而且不丢线索。

    所以标点、话题标签、链接、emoji 一律不动。剥掉它们不会多抓到联播 ——
    联播的机制是整段复制粘贴，正文本来就逐字相同 —— 但会实打实造出撞车：

        "Our campaign is live! https://kickstarter.com/<甲公司>"
        "Our campaign is live! https://kickstarter.com/<乙公司>"

    剥掉链接后这两段变成同一句，两家不同公司的独立线索被并成一条。
    而带众筹链接、带产品话题标签的帖子恰好是最有价值的那一类。

    下面四步归一化因此都很轻，也要认清它们的定位：既然联播是复制粘贴出来的、
    正文通常逐字一致，这四步平时并不承担主要工作，它们是保险 —— 防的是
    同一段话经过不同客户端、编辑器、复制路径发出来的情况。代价只有几微秒，
    而真遇到时只有它们能挡。别因为「看起来没在干活」就删掉。

    已知窄处，2026-09-20 实测出来的，数字记在这里免得下次又从头查一遍：

        一次采集里同一篇文章被 16 个账号各转了一遍。这个摘要折掉了其中 8 条
        （那 8 条逐字相同），另外 8 条没折掉 —— 它们之间唯一的差别是正文里那个
        lnkd.in 短码，erGPj8pd / eEH4XYbm / eNgxw4Nn 各不相同。

        代价是钱：没被折掉的每一条都会各自走完 prefilter 和 analyzer，
        同一篇文章的分析费付了好几遍。

        看起来只要剥掉链接就解决了，但上面那个反例还成立：带众筹链接的帖子正是
        最有价值的那一类，剥了会把两家不同公司的独立线索并成一条。
        真要修的话，能区分的地方在于 lnkd.in 的短码是 LinkedIn 给每一条帖子
        单独生成的（同一个目标链接，换一条帖子就换一个短码），所以它不是内容，
        是一个按帖子随机的标识符 —— 而 kickstarter.com/<项目> 那种是内容。
        只剥 lnkd.in 的短码不会碰到上面那个反例。

        今天不改，因为这会踩上面那条「归一化过度是更糟的错」：两条正文一样、
        只是链接指向不同集数的帖子（"New episode out! <lnkd.in/…>"），剥完就撞了，
        而那是静默丢线索。要改得先有一批真数据量一量这种情况占多少。
    """
    # 第一步：Unicode 规范化。出来的还是同一段文本，但全角半角、
    # 兼容字符（合字、带圈数字之类）被统一成一种写法。
    # 同一段话在不同客户端上发出来，这一层可能不同。
    s = unicodedata.normalize("NFKC", text)

    # 第二步：去掉不可见字符。它们人看不见，但会让两段一样的话摘要不同。
    s = _INVISIBLE.sub("", s)

    # 第三步：折叠空白并去掉首尾。出来的是单行文本，
    # 段落之间的空行数、行尾多余空格都被抹平 —— 复制粘贴最容易变的就是这些。
    s = _WHITESPACE.sub(" ", s).strip()

    # 第四步：小写。大小写不同的两段话几乎不可能是不同的内容，
    # 所以这一步撞车风险极低；它挡的是「照着原帖手打一遍」这类差异。
    s = s.lower()

    # 空正文这里会算出一个固定的摘要，两条空正文因此互撞。
    # 不特殊处理：正文空的信号在 prefilter 里会被命中词那条规则先砍掉，
    # 在这里额外炸一次只会把同一个判断分散到两个地方。
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# 布尔符和括号。只认大写：LinkedIn 自己就规定运算符必须大写，
# 而认小写会把 "research and development" 这种词组切成两半。
# 一次扫描就把 query 切开，并且把切出来的符号本身也留在结果里（捕获组的作用），
# 这样才知道每个词的前面站着谁。
#
# 引号短语排在最前面：它整段可能包含 AND / OR / NOT，先认出来才不会被切坏。
# LinkedIn 里加引号表示"就按这个顺序、这些词"精确匹配。
#
# 三个布尔符都只认大写。认小写的话 research and development 会被切成两半，
# 而 cannot、Notion 这种词里的 not 也会被当成运算符。
_TOKENS = re.compile(r'("[^"]+"|\bAND\b|\bOR\b|\bNOT\b|[()])')


def _query_terms(query_search: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把一句布尔检索式拆成两组词：要在正文里找的，和要排除的。

    输入：query 原文
    输出：(要找的词, 排除的词) 两个元组

    三个实际值：

        'Kickstarter AND (microphone OR "voice recorder")'
            -> ("Kickstarter", "microphone", "voice recorder"), ()

        'looking for a hardware manufacturing partner'
            -> ("looking for a hardware manufacturing partner",), ()

        'audio AND wearable NOT (hiring OR "job opening")'
            -> ("audio", "wearable"), ("hiring", "job opening")

    拆分单位是「两个布尔符之间的一整段」，不是单词。这一条是整个函数的关键：
    真实的 query 里有一整句都不带任何布尔符的（找买方求助语气的那类），
    按单词拆会拆出 for、a 这种虚词，而它们命中每一篇帖子 ——
    命中词那条规则会因此永远为真，等于闸门被拆掉。

    词一律按原样返回，不小写、不去标点，顺序就是它们在 query 里出现的顺序。
    因为它的第二个用途是归因：一条 query 通常挂着好几个 OR 分支，记下命中的是
    哪个分支，才知道该删哪个分支。返回值要能跟 query 原文对得上。

    为什么排除词一定要单独一组：混进第一组的话，一条写着 pre-launch 的帖子会被
    算成「命中了 pre-launch，所以相关」—— 方向正好反了，而且不报错。

    NOT 认两种写法，多写几个 NOT 也可以：

        NOT <一个词>            audio NOT hiring
        NOT (<词> OR <词> ...)   audio NOT (hiring OR "job opening")

    刻意不认的一种是「排除词后面直接跟 OR」（audio NOT hiring OR remote）——
    remote 到底算排除还是算要找的，读的人和写的人多半理解不一样。要那个意思就
    写成带括号的第二种。这一条跟整个模块的脾气一致：宁可炸，也不悄悄算一个
    可能是反的结果。

    NOT 是否真的被 LinkedIn 那边执行，我们没验过（actor 的文档说 searchQueries
    支持 AND / OR / NOT，单条最多 5 个布尔符）。这个函数管的是我们自己这一侧：
    不管对面认不认，排除词都不能算成命中。
    """
    relevant: list[str] = []
    excluded: list[str] = []

    #        depth 是当前在第几层括号里；negated_groups 记着哪几层是 NOT 开的。
    #        pending_not 表示刚读到一个 NOT、它要管的那个词还没出现。
    #        bare_negated 表示上一个词是「NOT 后面那个没加括号的词」——
    #        只有这种情况后面跟 OR 才有歧义。
    depth = 0
    negated_groups: list[int] = []
    pending_not = False
    bare_negated = False

    for piece in _TOKENS.split(query_search):
        chunk = piece.strip()
        if not chunk:
            continue

        if chunk == "NOT":
            if pending_not:
                raise ValueError(f"两个 NOT 连在一起：{query_search!r}")
            pending_not = True
            bare_negated = False
            continue

        if chunk in ("AND", "OR"):
            if pending_not:
                raise ValueError(f"NOT 后面直接跟了 {chunk}：{query_search!r}")
            if chunk == "OR" and bare_negated:
                raise ValueError(
                    f"排除词后面直接跟 OR，这句要排除哪些说不清：{query_search!r}。"
                    f"要排除多个就写成 NOT (甲 OR 乙)"
                )
            bare_negated = False
            continue

        if chunk == "(":
            depth += 1
            if pending_not:
                negated_groups.append(depth)
                pending_not = False
            bare_negated = False
            continue

        if chunk == ")":
            if negated_groups and negated_groups[-1] == depth:
                negated_groups.pop()
            depth -= 1
            if depth < 0:
                raise ValueError(f"括号不配对：{query_search!r}")
            bare_negated = False
            continue

        # 剩下的就是一个词：引号里的整段，或者两个布尔符之间的一整段
        term = chunk[1:-1].strip() if chunk.startswith('"') else chunk
        if not term:
            continue

        # 输出   这个词进哪一组：NOT 直接管着它，或者它待在一个 NOT 开的括号里
        target = excluded if (pending_not or negated_groups) else relevant
        bare_negated = pending_not
        pending_not = False
        if term not in target:
            target.append(term)

    if pending_not:
        raise ValueError(f"NOT 后面没有词：{query_search!r}")
    if depth != 0:
        raise ValueError(f"括号不配对：{query_search!r}")

    if not relevant:
        # 一个要找的词都拆不出来，说明 query 是空的、形状没见过、或者整句都是排除。
        # 这跟「正文里没有命中词」是两件完全不同的事，不能都返回空元组：
        # 前者是采集或配置出了问题，后者是这条信号本来就不相关。
        # 混成一个返回值，一次采集故障会被记成「这批 query 全都没线索」。
        raise ValueError(f"从 query 里拆不出任何要找的词：{query_search!r}")

    return tuple(relevant), tuple(excluded)


# 词组内部的分隔。正文里 "voice recorder" 可能写成换行断开、或者写成
# "voice-recorder"，那是同一个意思，所以词里的空格允许对应正文里的
# 任意空白或连字符。
_TERM_GAP = re.compile(r"[\s\-]+")


@functools.lru_cache(maxsize=512)
def term_pattern(term: str) -> re.Pattern[str]:
    """把一个词编译成用来在正文里找它的正则。

    输入：一个词，可能是单个单词也可能是一整个词组
    输出：编译好的正则对象，大小写不敏感

    公开的（没有下划线前缀）：query_intelligence/knowledge.py 也用它，那边判的是
    「这个品类词在一条 query 的正文里出现了吗」。两处问的是同一个问题，边界规则
    只该有一份实现 —— 两份一旦漂掉，那边挑出来的 query 和这边认的命中词会对不上，
    而两边都不报错。

    加缓存是因为一跑之内只有几条 query、却要对成百上千条正文各匹配一遍。
    同一个词反复编译纯属浪费。lru_cache 的意思是「同样的入参直接返回
    上次算好的结果」，词是字符串、结果只依赖它，所以缓存是安全的。

    两头用的不是常见的 \\b 单词边界，而是显式写出「前后不能紧跟
    ASCII 字母或数字」。原因是 \\b 依赖 \\w，而 Python 的 \\w 把中日韩
    文字也算成单词字符 —— 一旦正文里出现中英混排（audio耳机），
    \\b 就认为那里没有边界，词直接匹配不上。改成只看 ASCII 之后：
        audiobook   不算命中 audio      （避免假命中，这是要边界的原因）
        audio耳机    算命中 audio        （中英混排照样能匹配）
    """
    # words 是这个词按空白或连字符切开的各段，
    # pattern 把它们用「任意空白或连字符」重新连起来。
    words = [re.escape(w) for w in _TERM_GAP.split(term) if w]
    body = r"[\s\-]+".join(words)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def matched_terms(text: str, query_search: str) -> tuple[str, ...]:
    """找出这条 query 里哪些词真的出现在正文里。

    输入：帖子正文，以及把它搜出来的那条 query 原文
    输出：命中的词的元组，按它们在 query 里的出现顺序；一个都没命中就是空元组

    一个命中的例子：

        输入 text         "We are launching on Kickstarter with a new
                           voice-recorder built for meetings."
             query_search 'Kickstarter AND (microphone OR "voice recorder")'
        输出              ("Kickstarter", "voice recorder")

        注意三件事：microphone 没出现所以不在结果里；正文写的是连字符的
        voice-recorder，照样算命中 voice recorder；顺序跟这几个词在 query 里
        出现的先后一致，不是跟它们在正文里出现的先后。

    一个不命中的例子：

        输入 text         "Great launch, congrats to the whole team!"
             query_search 'Kickstarter AND (microphone OR "voice recorder")'
        输出              ()

        LinkedIn 把这条当成语义相关返回了，但正文里一个搜索词都没有。
        空元组是正常结果，不是出错。

    这个函数存在的理由是 LinkedIn 的搜索是语义相关匹配，不是精确短语匹配：
    它会把「意思沾边」的帖子也返回回来，而那些帖子的正文里可能一个搜索词
    都没有。所以命中词得我们自己在正文里对一遍，供应商不给这个信息。

    返回空元组不代表出错，它是一个正常且常见的结果，含义是「这条信号跟
    query 的意图不相关」。真正砍掉它的动作在 prefilter，不在这里 ——
    这个模块只算不砍。

    第二个用途是归因：一条 query 挂着好几个 OR 分支，知道命中的是哪个分支，
    才知道该删哪个分支。所以命中的词按 query 里的原样返回，不做归一化。

    NOT 后面的排除词不参与这件事：它们出现在正文里不是「相关」，是反过来的
    证据。这个函数只按要找的那一组算，排除词由 _query_terms 分出去。

    转发的信号有两段正文（转发者那句话和原帖正文），该拿哪段来对是调用方的
    选择，这个函数只认它收到的那一段。
    """
    # 第一步：从 query 里拆出要找的词。排除词在返回值的第二格，这里用不上 ——
    # 它们出现在正文里不算命中，这正是要把两组分开的原因。
    # 拆出来的是一串词，每个要么是单词要么是不带布尔符的词组。
    # 这一步拆不出词会直接炸，那属于配置或采集出了问题，不是「没命中」。
    terms, _excluded = _query_terms(query_search)

    # 第二步：逐个词在正文里找。
    # 正文不做归一化直接搜，因为词的正则本身已经对空白和连字符宽容了；
    # 在这里再折叠一次空白只是多复制一份长文本。
    return tuple(term for term in terms if term_pattern(term).search(text))


# ─────────────────────────────────────────────────────────── 第一跳


def to_signal(raw: RawSignal) -> Signal:
    """三段基数的第一跳：给一条 RawSignal 补上两个派生字段，变成 Signal。

    输入：adapter 翻译出来的一条 RawSignal
    输出：一个 Signal，内容跟输入完全一样，另外多了 content_hash 和 matched_terms

    一个例子：

        输入   RawSignal(text="Launching on Kickstarter next week.",
                         query_search='Kickstarter AND (audio OR microphone)',
                         reposted_from=None, ...)
        输出   Signal(同样那些字段不变,
                      content_hash="7f3c...",
                      matched_terms=("Kickstarter",))

    这一跳只算不砍。命中词算出来是空元组也照样返回一个 Signal，
    砍掉它是 prefilter 的事。把丢弃提前到这里会让「这条为什么没了」
    要翻两个地方查。

    转发的信号有两段正文，这里的口径是两段一起算，理由见下面第一步。
    """
    # 第一步：拼出「这条信号带的全部正文」，后面两个派生字段都基于它算。
    #        full_text 是转发者那句话和原帖正文接在一起的一整段；
    # 不是转发的话它就等于正文本身。
    #
    # 为什么不只取其中一段，两个方向各有一个会静默出错的情形：
    #
    #   只取转发者那句话   转发时他常常只写一句「Congrats」这类通用短语。
    #                      两个人转发两个完全不同的原帖、却都写了同一句，
    #                      摘要会相同，其中一条被当成重复丢掉。而且真正
    #                      有内容的原帖正文没参与命中词，一条讲众筹音频
    #                      硬件的原帖会因为转发者没提关键词而被判成不相关。
    #   只取原帖正文       转发者自己那句话有可能才是买方自述。两个人转发
    #                      同一个原帖、其中一个说「我们也在找代工厂」，
    #                      摘要却跟另一个相同，这条真线索被丢掉。
    #
    # 两段一起算就没有这两种情形：内容不同的转发一定得到不同的摘要，
    # 原帖内容也参与命中词判断。代价是同一个原帖被多人转发时摘要各不相同，
    # 所以「一份新闻稿被联播」这件事不能靠摘要抓 —— 转发有更准的抓法，
    # reposted_from.external_id 直接就是同一个原帖 id。
    full_text = "\n".join(raw.quotable_texts)

    # 第二步：算内容摘要。它认的是「这两段正文是不是同一段」。
    hashed = content_hash(full_text)

    # 第三步：对命中词。
    # query_search 为空或者形状不认识时这一步会直接炸，不返回空元组 ——
    # 「不知道该找什么词」和「找了但一个都没有」是两件事，混成同一个
    # 返回值的话，一次采集故障会被记成这批 query 全都没线索。
    terms = matched_terms(full_text, raw.query_search)

    # 第四步：算事实摘要。它认的是「这两条信号说的是不是同一件事」，
    # 跟上面那个「是不是同一段正文」不是一个问题，转发时两者会分岔。
    #
    # 判据是转发者自己写的那段还有没有相关性，用的就是规则一那两条通道：
    # 他自己那段里的命中词，和他自己正文里被 @ 的公司。
    #     有    这条信号的事实是他自己那段 —— 他说了自己的事
    #     没有  事实是原帖那段 —— 他只是把别人的话又传了一遍
    #
    # 不拿字数当判据。实测 16 条转发的外层字数是 50 到 2778，中间没有
    # 自然断点，阈值划在哪都是拍的，而且没有标注能验证划得对不对；
    # 长度本身也不等于信息量 —— 「我们也在找代工厂」很短却是线索，
    # 一长串恭喜话很长却是噪音。
    #
    # 已知的失败方向是安全的：转发者确实说了实质内容、但用的词跟这条
    # query 不重合时，会被判成「传话」，于是 corroboration 少算一。
    # 这一维只会偏低不会偏高，而偏高才是要防的（联播刷分）。
    own_relevant = bool(matched_terms(raw.text, raw.query_search)) or bool(
        raw.mentioned_companies
    )
    if raw.reposted_from is not None and not own_relevant:
        # 取原帖那段。这一支顺带对上了另一种情形：原帖本身也被单独抓到时，
        # 它自己算出来的就是同一段正文的摘要，两边天然相等。
        fact = content_hash(raw.reposted_from.text)
    else:
        # 非转发时这一行的结果就等于上面的 hashed（quotable_texts 只有一段）。
        fact = content_hash(raw.text)

    # from_raw 会把 RawSignal 的每个字段逐一复制过来，所以以后给 RawSignal
    # 加字段不用改这里；忘了传新字段会当场炸，而不是留一个 None 到下游。
    return Signal.from_raw(
        raw, content_hash=hashed, matched_terms=terms, fact_hash=fact
    )


# ─────────────────────────────────────────────────────── 个人作者的公司兜底


# 职位串里表示「任职于」的标记。只认这两种：
#   at   英文里最常见的写法
#   @    同样常见
# 逗号形式（"Owner, DLR Sales, Inc."）刻意不认，理由见函数说明。
#
# @ 前面必须是空白或者整串的开头。这一条是必须的：职位串里常有人写邮箱
# 招揽联系（"reach us info@example.io"），而邮箱里的 @ 后面跟的是域名，
# 不加这个限制会把域名抽成公司名。要求前面是空白就能把两种用法分开，
# 因为「任职于」这个意思的 @ 一定跟前面的头衔隔着空格。
_EMPLOYER_MARKER = re.compile(r"(?:\bat\b|(?<!\S)@)", re.IGNORECASE)

# 公司名到这里就结束了。职位串是几段拼起来的，段之间用这些符号分隔，
# 抓到的第一段之后全是别的内容。空格加连字符要连着一起算，
# 否则会把 Coca-Cola 这类名字里的连字符当成分隔符。
_SEGMENT_END = re.compile(r"\s*(?:\||·|•|,|;|\n| - |｜)")

# 段分隔符的严格版，不含逗号和分号。给法律后缀那条路用，理由见
# _name_before_legal_suffix。
_HARD_SEGMENT_END = re.compile(r"\s*(?:\||·|•|\n| - |｜)")

# 不是公司名的占位说法。归一化之后整名相等才算命中，不做子串匹配 ——
# 子串匹配会连带杀掉 Independent Media Group 这类真公司。
_PLACEHOLDER_NAMES = frozenset(
    {
        "stealth",
        "stealth startup",
        "stealth mode",
        "stealth mode startup",
        "self employed",
        "selfemployed",
        "freelance",
        "freelancer",
        "independent",
        "independent consultant",
        "independent contractor",
        "various",
        "various companies",
        "confidential",
        "home",
        "retired",
        "unemployed",
        "myself",
        "my own business",
        "n a",
        "none",
        "tbd",
    }
)

# 抽出来的名字最多允许几个词。超过就认为分隔没切干净，
# 抓到的是一句话而不是一个公司名。
_MAX_NAME_WORDS = 6

# 名字归一化后至少要有这么多字符。一个字符的名字确实存在，但它作为
# 身份键会跟所有同名的东西并到一起，而名字本来就是梯度最低的那一级。
# 这种键宁可不要。
_MIN_NAME_CHARS = 2

# 不能用来开头的小写虚词。这一条抓的是 at 后面跟着一句话而不是公司名
# （"Building Tech at the Intersection of AI, Devices, Design"）。
#
# 刻意区分大小写：公司名里的这些词会大写（The Boring Company），
# 而写句子的时候是小写。靠这个区分会连带拒掉少数把 the 写成小写的
# 真公司名，那是可以接受的损失 —— 丢一条线索，而放过一句话会让
# 一整句散文变成一个公司候选被送进 LLM。
_LOWERCASE_LEADERS = frozenset(
    {"the", "a", "an", "my", "our", "his", "her", "their", "this", "that", "these", "those"}
)

# at 后面跟着这些词的时候，at 根本不是「任职于」，而是一个英文固定短语的
# 一半。抓的是 "Writer and independent scholar at large" 这种写法：
# at large 的意思是「没有固定职位的」，恰恰是在说他没有雇主，而按任职标记
# 那条规则读出来的公司名会是 large。
#
# 跟占位说法那张表分开，因为两者说的不是一件事：占位说法是「公司这一栏
# 填的是没有公司」（Stealth Startup），这里是「这个 at 不是任职标记」。
# 合成一张表的话，将来有人拿占位表去做别的判断就会连带用错。
#
# 归一化之后整名相等才算命中，跟占位表同一条口径：做子串匹配会杀掉
# Large Format Displays 这类真名字。
#
# 这张表长不完也不需要长完：漏一个的后果是多花一次 LLM 的钱、库里多一行
# 垃圾公司，不会丢线索。所以只收「at 加这个词是个固定短语、绝不可能是
# 雇主名」的那几个，凭想象往里加词的代价反而是误杀真公司名。
#
# 为什么不干脆改成「at 后面第一个词是小写就拒」：职位串里写小写域名的
# 人不少（"... at acme-audio.example"），那是真雇主，一刀切会把它们连带杀掉。
_IDIOM_AFTER_MARKER = frozenset({"large", "heart", "scale"})


# 公司的法律后缀。命中它基本就能确定前面那串是公司名。
#
# 这个表跟 models/company.py 里剥后缀用的那个刻意不共用，因为两处的
# 要求相反：剥后缀时激进一点没坏处（把 Lumen Co 剥成 lumen 是对的），
# 而认后缀时激进会造出假阳性 —— 裸的 Co 会命中 Co-Founder，
# 裸的 AS 和 AB 是常见英文词。所以这里只留歧义低的那些。
_LEGAL_SUFFIX_TOKENS = frozenset(
    {
        "inc",
        "inc.",
        "ltd",
        "ltd.",
        "llc",
        "l.l.c.",
        "gmbh",
        "corp",
        "corp.",
        "corporation",
        "pte",
        "pte.",
        "pty",
        "plc",
        "bv",
        "b.v.",
        "nv",
        "n.v.",
        "sdn",
        "bhd",
        "srl",
        "s.r.l.",
        "spa",
        "s.p.a.",
        "oyj",
        "aps",
    }
)


def _name_after_marker(headline: str) -> str | None:
    """走任职标记那条路：取 at 或 @ 后面那一段。

    输入：职位串
    输出：抽出来的那串字，没有标记就是 None。这一步不做任何检查

    只取第一个标记：职位串里出现两次 at 时
    （"Engineer at Intel | Advisor at Foo"），第一个是主职，
    后面的是兼职或过往任职。
    """
    marker = _EMPLOYER_MARKER.search(headline)
    if marker is None:
        return None

    #        tail 是标记之后的全部内容，切到最近的段分隔符为止
    tail = headline[marker.end() :]
    end = _SEGMENT_END.search(tail)
    return (tail[: end.start()] if end else tail).strip()


def _name_before_legal_suffix(headline: str) -> str | None:
    """走法律后缀那条路：取 Inc. GmbH 这类后缀前面那串大写词。

    输入：职位串
    输出：抽出来的那串字（含后缀本身），找不到就是 None。这一步不做任何检查

    这条路专门捡 "Founder/CEO Chatterly Inc." 这种没有任何任职标记、
    但公司名后面挂着法律后缀的写法。

    难点是分不清哪个词是职位、哪个词是公司名，因为两者都是大写开头。
    这里用的判据不是职位词表，是位置：从后缀往前收大写开头的词，
    但不许收到整段的第一个词。因为 LinkedIn 的职位串几乎总是先写角色
    再写公司，第一个词是职位而不是公司名。

        "Founder/CEO Chatterly Inc."     往前收到 Chatterly 就停，
                                       Founder/CEO 是第一个词，不收
        "Co-CEO bei Trelo Group GmbH"  往前收 Group、Trelo，
                                       再往前 bei 是小写，停

    代价是「整段以公司名开头」那种写法会漏掉。那是漏不是错，方向对。
    """
    # 只看第一段。后面几段通常是标签和自我介绍，里面出现的公司名
    # 多半是合作方或前雇主，不是他现在任职的地方。
    #
    # 这里用的分隔符比另一条路少一个逗号，因为逗号对两条路的含义相反：
    # 走 at 那条时它是段的结尾（"at Adams Wealth Management Group,
    # a Veteran Owned Small Business" 要在逗号处截断），
    # 走后缀这条时它在公司名里面（"DLR Sales, Inc."）。
    # 共用一个分隔符表的话，带逗号的法律后缀写法会被切碎，一个都抽不到。
    segment = _HARD_SEGMENT_END.split(headline)[0].strip()
    tokens = segment.split()

    for i, token in enumerate(tokens):
        if token.lower().strip(",") not in _LEGAL_SUFFIX_TOKENS:
            continue

        #        parts 是后缀前面那串连续的大写开头的词，从右往左收。
        # 下界是 1 不是 0，那一个词就是上面说的「第一个词是职位」。
        parts: list[str] = []
        j = i - 1
        while j >= 1 and tokens[j][:1].isupper():
            parts.insert(0, tokens[j])
            j -= 1

        if parts:
            return " ".join([*parts, tokens[i]]).strip(",")

    return None


def company_from_headline(headline: str) -> CompanyRef | None:
    """从个人作者的职位串里兜底解析出他所属的公司。

    输入：作者的职位串，也就是 RawSignal.actor_headline
    输出：一个只有名字的 CompanyRef，解析不出就是 None

    两个例子：

        输入   "Consultant | Low-Power Power Management at PULSAR
                Microelectronics | Core Training, Physics Simulation"
        输出   CompanyRef(name="PULSAR Microelectronics",
                          linkedin_company_id=None, linkedin_url=None, domain=None)

        输入   "Sales + Ops @ Stealth Startup | Business Development"
        输出   None

    这个函数存在的理由：数据源只在作者是公司主页时才给结构化的公司 id，
    而绝大多数帖子是个人发的，那一类拿不到任何 id，职位串是唯一的线索。

    但它同时是整个流水线里唯一会凭文本造数据的一步，所以取舍是一边倒的：
    宁可解析不出，也绝不猜。解析不出的候选会被记成身份不明然后丢掉，
    那只损失一条线索；而猜错一个名字，归并时会按名字把两家不相干的公司
    并成一家，那条错会一直留在库里，并且污染的是别人的证据。

    由此定下三条：

      1. 只认两种明确信号，按优先级排：任职标记（at 和 @）在先，
         公司法律后缀（Inc. GmbH Ltd 之类）在后。逗号形式不认 ——
         "Chief Legal Officer, Corporate Secretary at Effectual" 里逗号
         分隔的是两个头衔不是公司，而这种写法跟真的逗号加公司名
         （"Owner, DLR Sales, Inc."）在文本上无法区分。
         认逗号会稳定地把头衔当成公司名。
      2. 抽出来的东西要过一遍形状检查，占位说法和不像名字的一律拒。
      3. 返回的 CompanyRef 只有名字，另外三个身份字段一律 None。
         它的身份键会落在梯度最低的一级，模型里 is_weak_identity 为真，
         归并命中要打折 —— 这个弱是真实的，不要在这里假装补齐。

    为什么只加法律后缀、不加「职位词加逗号」那类规则：职位词表是个永远
    长不完的表，今天要英文的，明天要德语法语的、要缩写的，而没收录的那些
    会静默失败。法律后缀不一样，它是公司的法律标识，全世界就那么几十个，
    而且这个表本来就有现成的。
    """
    # 第一步：先走任职标记那条路，走不通再退到法律后缀那条。
    #        candidate 是抽出来的那串字，还没有经过任何检查。
    # 顺序不能反：at 和 @ 是明确的「任职于」，比后缀更直接，
    # 而且带后缀的公司名也常常跟在 at 后面，反过来会让后缀那条抢答。
    candidate = _name_after_marker(headline) or _name_before_legal_suffix(headline)
    if candidate is None:
        return None

    # 第二步：形状检查。这一步的每一条都是在拒绝，不是在修补 ——
    # 修补等于猜，而猜错的代价见函数说明。
    #
    # 归一化后的形式同时用于两件事：跟占位词表比对，以及确认它真能
    # 建出身份键。normalize_company_name 会剥掉法律后缀和标点，
    # 所以只剩标点或只剩一个 Inc. 的候选在这里会变成空串。
    normalized = normalize_company_name(candidate)

    if not normalized:
        # 剥完什么都不剩，说明抓到的不是名字
        return None

    if len(normalized) < _MIN_NAME_CHARS:
        # 太短，做身份键会跟所有同名的东西撞在一起
        return None

    first_word = candidate.split()[0] if candidate.split() else ""
    if first_word in _LOWERCASE_LEADERS:
        # 小写虚词开头，说明标记后面接的是一句话不是公司名
        return None

    if normalized in _PLACEHOLDER_NAMES:
        # 这类词看着像公司名，实际是「没有公司」的另一种说法。
        # 放过去的话，所有隐形创业公司会被归并成同一家。
        return None

    if normalized in _IDIOM_AFTER_MARKER:
        # 命中的是 "at large" 这类固定短语的后半截，那个 at 不是任职标记。
        return None

    if len(normalized.split()) > _MAX_NAME_WORDS:
        # 词太多说明段分隔符没起作用，抓到的是一整句话
        return None

    if not any(ch.isalpha() for ch in normalized):
        # 一个字母都没有，纯数字或纯符号，不可能是公司名
        return None

    # 名字用切出来的原样，不用归一化后的那份：归一化是为了比对和建键，
    # 展示和写给 Riley 看的时候要保留原始大小写和写法。
    return CompanyRef(
        name=candidate,
        linkedin_company_id=None,
        linkedin_url=None,
        domain=None,
    )


# ─────────────────────────────────────────────────────────── 第二跳


def _employer_candidate(
    signal: Signal,
    *,
    company: CompanyRef | None,
    headline: str,
    from_company: LeadType,
    from_headline: LeadType,
) -> list[Candidate]:
    """把一个发帖主体转成「他所属公司」这一个候选，带上通道标记。

    输入：这条信号，那个主体的公司（可能是 None）和职位串，
          以及两种来源各自该记成哪个通道
    输出：一个候选的列表，或者空列表（连名字都解析不出来时）

    星号后面的参数只能用名字传。这个函数有两个 LeadType 参数，
    允许按位置传的话，调换它们不会报错，只会让报表上两条通道的产出互换，
    而那种错要等到看统计时才可能被发现。

    拆成独立函数是因为外层发帖人和被转发的原帖作者走的是同一套逻辑，
    只是通道标记不同。写两遍的话，以后改判定规则会漏改一处，
    而漏改的那一边是转发，量小、不容易发现。
    """
    if company is not None:
        # 数据源直接给了结构化的公司身份，这是最可靠的一路
        return [Candidate(company=company, lead_type=from_company, signal=signal)]

    # 拿不到结构化身份，降级去职位串里解析。解析不出就返回空列表 ——
    # 那不是错误，是这个主体的公司确实无法确定，这条候选就不存在。
    from_text = company_from_headline(headline)
    if from_text is None:
        return []

    return [Candidate(company=from_text, lead_type=from_headline, signal=signal)]


def to_candidates(signal: Signal) -> tuple[Candidate, ...]:
    """三段基数的第二跳：把一条信号展开成它指向的所有公司候选。

    输入：一条 Signal
    输出：候选的元组，可能是空的，也可能有好几个

    一个例子。供应商从个人号发帖吹「我们帮 Lumen 做了 5000 只耳机」，
    正文里 @ 了 Lumen：

        输入   Signal(actor_headline="Export Sales at Shenzhen XX Audio",
                      mentioned_companies=(CompanyRef("Lumen", ...),), ...)
        输出   (Candidate(Shenzhen XX Audio, HEADLINE, signal),
                Candidate(Lumen, MENTIONED, signal))

    这两个候选的命运在 prefilter 里正好相反：供应商 headline 那条规则会砍掉
    第一个，第二个是真买方要留。这就是为什么基数必须是一对多 ——
    压成一对一只能二选一，而不管选哪个都是错的。

    四条通道，按固定顺序展开：

        发帖人所属公司      结构化身份优先，没有就去职位串里解析
        正文里被 @ 的公司    每个各产出一个候选
        原帖作者所属公司    这条信号是转发时才有，规则同第一条
        原帖里被 @ 的公司    同第二条

    顺序固定是为了确定性：同一条信号每次展开出的候选顺序都一样，
    报表和回归对照才有立足点。

    两条容易想反的规则：

      1. 不去重。同一家公司可能同时从两条通道出现（公司主页发帖又 @ 了自己），
         这时会产出两个候选。看着重复，但去重是错的：prefilter 是按候选砍的，
         公司主页帖那条规则要砍掉 author 候选、留下 mentioned 候选。
         去重会把两条命运不同的候选压成一条，那条规则就没法只砍一边了。
         真正的合并在第三跳按身份键做，那时候两个候选进同一个簇，
         而簇里数的是独立事实，不会被重复计数。
      2. 身份键建不出来的候选也照样产出。这个模块只算不砍，
         身份不明是一条明确的丢弃理由，由 prefilter 记账后丢弃，
         这样它会出现在漏斗报表里。在这里悄悄跳过的话，
         这批候选去哪了就查不出来了。
    """
    out: list[Candidate] = []

    # 第一条通道：发帖人自己所属的公司。
    # 这一条给出 0 或 1 个候选 —— 个人作者且职位串解析不出来时就是 0 个。
    out += _employer_candidate(
        signal,
        company=signal.actor_company,
        headline=signal.actor_headline,
        from_company=LeadType.AUTHOR,
        from_headline=LeadType.HEADLINE,
    )

    # 第二条通道：正文里被 @ 链接的公司。
    # 这条量少但身份精确，数据源直接给公司 id、名字和主页 URL。
    out += [
        Candidate(company=company, lead_type=LeadType.MENTIONED, signal=signal)
        for company in signal.mentioned_companies
    ]

    # 后两条通道只在这条信号是转发时存在。
    # 转发的外层作者是转发的人，他往往跟这件事无关；有内容的原帖连作者
    # 带被 @ 的公司都在 reposted_from 里，不展开就整条丢掉了。
    if signal.reposted_from is not None:
        original = signal.reposted_from

        out += _employer_candidate(
            signal,
            company=original.actor_company,
            headline=original.actor_headline,
            from_company=LeadType.REPOST_AUTHOR,
            from_headline=LeadType.REPOST_HEADLINE,
        )

        out += [
            Candidate(company=company, lead_type=LeadType.REPOST_MENTIONED, signal=signal)
            for company in original.mentioned_companies
        ]

    return tuple(out)


# ─────────────────────────────────────────────────────────── 第三跳


def to_clusters(candidates: Iterable[Candidate]) -> tuple[CompanyCluster, ...]:
    """三段基数的第三跳：把候选按公司身份归并成一家一个簇。

    输入：候选的可迭代序列，通常是这一跑全部信号展开出来的候选
    输出：公司簇的元组，按每家公司第一次出现的顺序排列

    一个例子。四个候选进来，其中三个指向同一家公司但身份完整度不同：

        输入
            Candidate(CompanyRef("Lumen", "100000001", ".../company/lumen/", None),
                      MENTIONED, 信号甲)
            Candidate(CompanyRef("Lumen", None, None, None), HEADLINE, 信号乙)
            Candidate(CompanyRef("Lumen", "100000001", None, None), MENTIONED, 信号丙)
            Candidate(CompanyRef("NAVA", "97531", None, None), AUTHOR, 信号丁)

        中间：按身份键分成三个簇
            ("li_id", "100000001")  <- 甲，丙跟它键相同所以并进来
            ("name",  "lumen")      <- 乙，只有名字所以停在最低那一档
            ("li_id", "97531")      <- 丁

        输出：乙那个簇被并进第一个，剩两个簇
            CompanyCluster(
                ref=CompanyRef("Lumen", "100000001", ".../company/lumen/", None),
                candidates=[甲的候选, 丙的候选, 乙的候选],
            )
            CompanyCluster(
                ref=CompanyRef("NAVA", "97531", None, None),
                candidates=[丁的候选],
            )

        第一个簇的派生读法：corroboration 是 3（三条信号说的是三件事），
        lead_types 是 {MENTIONED, HEADLINE}，signals 是三行。
        被并进来的候选排在最后，所以顺序是甲丙乙而不是甲乙丙。

    输出的簇数就是 ExecutionCounters.unique_company_count。

    关于簇的 ref 和候选自己的 company，这两个不是一回事，别当成重复存储：

        cluster.ref         归并后的权威身份，是一个 CompanyRef 对象而不是
                            身份键。它是这一跑对「这家公司是谁」的结论。
        candidate.company   这条证据当时看到的那个引用，一个字都不会被改。
                            merge 生成的是新对象、赋给 cluster.ref，
                            而 Candidate 是不可变的，它的 company 指向的
                            还是原来那份。

    所以上面例子里，乙那个候选的 company 到最后仍然是
    CompanyRef("Lumen", None, None, None) —— 没有公司 id、名字可能连写法都
    跟簇的 ref 不同（一边带表情符号一边不带是真会发生的）。
    留着它是因为它是现场证据：哪天发现两家公司被并错了，唯一能告诉你
    「是哪条引用把它们连起来的」就是这个字段。簇的 ref 那时已经是合并后的
    结果，看不出来源。

    这一跳跟前两跳不是连着跑的，中间夹着 prefilter：

        第一跳 to_signal          算摘要和命中词
        prefilter 的 signal 级     整条丢弃（命中词为空、摘要见过、词义撞车等）
        第二跳 to_candidates      展开候选
        prefilter 的 candidate 级  丢单个候选（公司主页帖的作者、供应商 headline、身份不明）
        第三跳 to_clusters        归并成公司
        dedup                    拿归并结果去 leads 表比 cooldown

    顺序不能调，因为簇数要等于「这一跑真的要分析的公司数」，而 dedup 的
    三种去向加起来必须跟它对得上。把归并放在 prefilter 之前的话，被砍掉的
    候选会先进簇、簇数虚高，那个对账就永远差一截。

    正因为 prefilter 已经在前面跑过，这里可以直接取身份键而不做保护：
    身份不明是 prefilter 的一条丢弃理由，走到这一跳还有身份不明的候选，
    说明 prefilter 没做它该做的事。那是 bug，应该当场炸，不该在这里
    默默跳过 —— 跳过的话候选数和簇数会对不上，而且查不出差在哪。

    归并只用一个身份键，所以它保证确定性但不保证完备性：同一家公司的两条
    引用停在梯度的不同档（一条有公司 id、一条只有名字）归并不到一起。
    这是已知的性质不是 bug，补法在 dedup 那一步 —— 查 leads 表时拿所有
    非空字段逐级查一遍，命中任一档就是同一家，然后把 id 补上。
    结果是第一次可能建两行，第二次相遇就并上。
    """
    #        clusters 是「身份键 -> 簇」的映射。用 dict 而不是先排序再分组，
    # 是因为 dict 会保留键第一次插入的顺序，输出的簇顺序因此是稳定的 ——
    # 同样的输入每次跑出同样的顺序，报表和回归对照才有立足点。
    # clusters 从空字典开始，循环结束时装的是「身份键 -> 这家公司的簇」。
    # 用 dict 而不是先排序再分组，是因为 dict 会保留键第一次插入的顺序，
    # 输出的簇顺序因此稳定 —— 同样的输入每次跑出同样的顺序。
    clusters: dict[tuple[str, str], CompanyCluster] = {}

    for candidate in candidates:
        # 身份键的梯度是 公司 id 大于主页 slug 大于域名 大于归一化名，
        # identity_key 返回最高那一档，形如：
        #     Lumen 有公司 id           -> ("li_id", "100000001")
        #     Pinecrest 只有名字        -> ("name", "pinecrest")
        # 注意它只表达最高那一档，对低档一个字都没说 —— 两条引用键相同，
        # 低档字段仍然可能一个有一个没有。
        # 建不出来会抛 IdentityUnresolvable，这里刻意不接，理由见上面。
        key = candidate.company.identity_key

        cluster = clusters.get(key)
        if cluster is None:
            # 这家公司第一次出现。簇的 ref 就用这条候选的引用，
            # candidates 里先放它自己一个。此时簇的形态是：
            #     ref=CompanyRef("Lumen", "100000001", url, None)
            #     candidates=[那一个候选]
            # 派生出来的读法：signals 一行、corroboration 为 1、
            # lead_types 只有这条候选的那一个通道。
            clusters[key] = CompanyCluster(ref=candidate.company, candidates=[candidate])
            continue

        # 走到这里说明这家公司之前出现过。两条引用的身份键相同但低档字段
        # 可能各缺一块，merge 把彼此缺的补齐，例如：
        #     簇里的     CompanyRef("Lumen", "100000001", url, None)
        #     新来的     CompanyRef("Lumen", "100000001", None, "lumen.example")
        #     merge 后   CompanyRef("Lumen", "100000001", url, "lumen.example")
        # 取的是「各字段第一个非空值」，所以名字保持第一次见到的写法。
        # 键不同它会抛 ValueError，那是不变式检查 —— 这里键一定相同，
        # 所以正常不会触发；真触发了说明 identity_key 的实现被改坏了。
        cluster.ref = cluster.ref.merge(candidate.company)

        # 候选一律照收，这里不去任何重。同一条信号经由两条通道指向同一家
        # 公司时会收进两个候选，那两个都是真的：一个说这家公司自己发了帖，
        # 另一个说这个帖子里 @ 了它。要「有哪些证据行」的时候读簇的 signals，
        # 它按行去重；要「有几件独立的事」的时候读 corroboration。
        # 在收集这一步就去重的话，通道信息会被抹掉，而那正是写话术要用的。
        cluster.candidates.append(candidate)

    # 循环结束时 clusters 的形态，按上面 docstring 那个例子写具体：
    #
    #     {
    #       ("li_id", "100000001"): CompanyCluster(
    #             ref=CompanyRef("Lumen", "100000001", ".../company/lumen/", None),
    #             candidates=[甲的候选, 丙的候选]),
    #       ("name",  "lumen"):     CompanyCluster(
    #             ref=CompanyRef("Lumen", None, None, None),
    #             candidates=[乙的候选]),
    #       ("li_id", "97531"):     CompanyCluster(
    #             ref=CompanyRef("NAVA", "97531", None, None),
    #             candidates=[丁的候选]),
    #     }
    #
    # 键是身份键，值里的 ref 是合并后的引用对象，候选各自带着原来的引用。
    # 这时候还有一个已知问题没解决 —— 前两个键指的是同一家公司，只是一条
    # 停在公司 id 那一档、一条停在名字那一档，键不相等所以成了两个簇。
    # 交给下面这一步。它就地改 clusters，所以之后字典会变小。
    _absorb_weak_into_strong(clusters)

    # 最终形态：一家公司一个簇。上面那三条变成两条，("name","lumen") 被删掉、
    # 它的候选搬进了 ("li_id","100000001") 那个簇：
    #
    #     {
    #       ("li_id", "100000001"): CompanyCluster(
    #             ref=CompanyRef("Lumen", "100000001", ".../company/lumen/", None),
    #             candidates=[甲的候选, 丙的候选, 乙的候选]),
    #       ("li_id", "97531"):     CompanyCluster(
    #             ref=CompanyRef("NAVA", "97531", None, None),
    #             candidates=[丁的候选]),
    #     }
    #
    # 返回的是它的值，不带键 —— 身份键从这里往后不再需要，
    # 每个簇的 ref 自己能算出来。簇数就是 unique_company_count，
    # 顺序是各家公司第一次出现的顺序。
    return tuple(clusters.values())


def _absorb_weak_into_strong(clusters: dict[tuple[str, str], CompanyCluster]) -> None:
    """把只有名字的簇并进同名的、身份更硬的簇。就地改，不返回新字典。

    输入：身份键到簇的映射，会被就地修改
    输出：无

    要补的是单键归并的固有缺陷。身份键只取梯度最高的那一档，所以同一家
    公司的两条引用停在不同档时，键永远不相等，第一遍归并碰不到一起：

        从职位串抽出名字      ("name", "pinecrest")
        从被 @ 的实体拿到 id  ("li_id", "100000006")

    这两个是同一家公司，却成了两个簇。放着不管的话，后面查 leads 表时
    它们会被当成两家分别记 cooldown、分别送去分析，花两份钱、库里两行。
    这一步解决不了要靠查库来撞上，因为查的是库、不是本跑的别的簇；
    指望第一个先写库第二个再查到，等于把正确性押在写入顺序上。

    这里不能用 CompanyRef.merge，它要求两边身份键相同，而弱和强按定义
    就不同。也不需要用：弱的那份只有名字，强的那份是它的超集，
    直接把候选搬过去、丢掉弱的引用就行。

    歧义就放弃，这是这一步唯一的判断：一个名字同时对上两个身份更硬的簇时，
    不去猜是哪一个，两边都不动。方向跟整个模块一致 —— 并错一次会把两家
    不相干的公司永久并成一家、污染别人的证据，而不并只是少一次交叉印证。

    覆盖范围要说清楚：这一步只处理「以名字为键的簇」被并进「以别的档位为键
    的簇」，也就是梯度最低那一档跟其他档之间。别的档位两两之间不处理，
    比如以主页 slug 为键的簇和以公司 id 为键的簇，它们也可能是同一家公司，
    这一步碰不到。

    没做是因为那两种键在当前数据源上根本不会出现：LinkedIn 的帖子搜索给
    公司身份时，公司 id 和主页 URL 总是一起给，所以有 URL 就必定有 id，
    键就落在 id 那一档，永远停不到 slug 那一档；域名这个源一个都不给，
    所以域名那一档也不会出现。于是实际只有两档在用，正好就是这一步处理的。

    但「不会出现」靠的是数据源当前的行为，不是代码拦住的：adapter 那边
    被 @ 的公司，id 是可选的，供应商哪天漏一个就会冒出 slug 档。所以下面
    那个循环加了一条断言，真冒出第三档就当场炸 —— 让它默默少并一批的代价，
    是同一家公司两行、两份 cooldown、两次 LLM，而且没人看得见。

    以后接了众筹那类数据源就会出现域名档（那边给公司名和官网，不给 LinkedIn
    的任何 id）。那时候不要把这一步扩成「什么档都靠名字对」—— 名字是最弱的
    依据，扩大它的适用面等于扩大并错的机会。正确的做法是等 enrich 给 LinkedIn
    那边的引用补上域名，两边都有域名之后按域名对，那是个精确得多的依据。
    """
    # by_name 从空字典开始。第一个循环结束时它是
    #     {归一化名 -> [身份更硬的簇的键, ...]}
    # 例如 {"pinecrest": [("li_id", "100000006")], "hp": [("li_id", "200000023")]}。
    # 值用列表而不是单个键，是为了能看出一个名字有没有对上多个簇 ——
    # 那种情况是歧义，下面要放弃。
    by_name: dict[str, list[tuple[str, str]]] = {}
    for key, cluster in clusters.items():
        if key[0] == "name":
            continue  # 以名字为键的簇不进索引，它们是被并的一方

        if key[0] != "li_id":
            # 只有 li_id 和 name 两档在用是数据源的行为、不是代码保证，
            # 冒出第三档这一步就会静默漏并。理由和该怎么改见上面 docstring。
            raise ValueError(
                f"出现了 li_id 和 name 以外的身份档：{key!r}。"
                f"_absorb_weak_into_strong 不处理这一档，跑内归并会静默漏并"
            )

        # 索引用的是簇引用里的公司名，归一化之后才比 —— 因为被并的那一方
        # 的键本身就是归一化名，两边必须用同一套规则算，否则永远对不上
        name = normalize_company_name(cluster.ref.name)
        if name:
            by_name.setdefault(name, []).append(key)

    # absorbed 从空列表开始，第二个循环结束时装的是要删掉的簇的键，
    # 例如 [("name", "pinecrest"), ("name", "hp")]。
    # 不在循环里直接删，因为那样会在遍历中修改字典。
    absorbed: list[tuple[str, str]] = []
    for key, weak in clusters.items():
        if key[0] != "name":
            continue

        # 以名字为键的簇，它的键第二项就是归一化后的名字，直接拿来查索引。
        # matches 的三种形态：
        #     []                       没有身份更硬的同名簇，这个簇保持原样
        #     [一个键]                  唯一命中，往下并
        #     [两个以上的键]            歧义，不猜，两边都不动
        matches = by_name.get(key[1], [])
        if len(matches) != 1:
            continue

        # 把弱簇的候选整批搬进强簇。搬完强簇的形态变化：
        #     搬前  ref=CompanyRef("Pinecrest", "100000006", url, None)
        #           candidates=[那条 mentioned 候选]
        #     搬后  ref 一个字没变（强的那份本来就是弱的那份的超集）
        #           candidates=[那条 mentioned 候选, 那条 headline 候选]
        # 派生读法跟着变：lead_types 多一个通道，signals 多一行，
        # corroboration 从 1 变 2 —— 这正是这一步要回收的东西。
        # 不调 merge：它要求两边身份键相同，而这里按定义就不同；也不需要，
        # 弱的那份只有名字，强的那份已经有了。
        strong = clusters[matches[0]]
        strong.candidates.extend(weak.candidates)
        absorbed.append(key)

    # 删掉已经被并走的簇。删完 clusters 里一家公司只剩一个簇，
    # 调用方拿到的簇数因此才等于真实的公司数。
    for key in absorbed:
        del clusters[key]
