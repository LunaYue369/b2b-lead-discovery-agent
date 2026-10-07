"""HarvestAPI LinkedIn post search（Apify actor: harvestapi/linkedin-post-search）。

这个模块是整个项目里唯一认识 HarvestAPI 字段名的地方。它只做一件事：
把供应商返回的原始 dict 翻译成 RawSignal。不做过滤、不做判断、不算派生字段。

同家的 ...-sales-navigator-lead-search-cookie 永远不要碰：它要 li_at cookie，
违反「LinkedIn 的账号绝不参与任何自动化抓取」那条铁律。识别口诀：
凡是要你连接 LinkedIn 账号或粘贴 cookie 的，一律排除。

字段注意事项：
  1. 唯一 id 用 `id`，不是 `postId`。
  2. 只有post是以company名义发出时， 也就是`author.type == "company"`时，`author.companyId` 才有值，
     且等于author.id。当post是以个人用户发出时， author.id 是个人的领英profifle URN（ACoAA... 开头），
     不是公司 id，
  3. 被提到的公司在 contentAttributes[].company = {id, name, linkedinUrl}，
     三个字段 100% 完整。contentAttributes 里实测只有两种 type（226 条里
     COMPANY_NAME 47 条、PROFILE_MENTION 32 条），没有 hashtag、链接之类的其他标注。
     PROFILE_MENTION 是被 @ 的人，它的 profile 对象是
     {id, firstName, lastName, linkedinUrl, publicIdentifier}，
     没有任何 company 字段 —— 跟个人作者一样，从一个个人 URN 推不出公司，
     所以它填不进 CompanyRef，现在不取。它可能有用的地方是 draft 阶段找联系人
     （那时用 publicIdentifier 这个 slug，比 URN 稳定可读），那一期还没做。
     含 PROFILE_MENTION 的 16 条帖子里 10 条同时有 COMPANY_NAME，
     公司已经从另一条通道拿到了。
  4. 评论会作为独立 item 塞进同一个 dataset（type == "comment"），
     计费对象是全部 item 不是 post 数。估成本时别只算 post。
  5. 导出一律用 JSON。
  6. 转发的帖子在 item["repost"] 里嵌着原帖的完整对象。
     外层的 content 是转发者自己加的那句话，原帖正文在 repost.content ——
     两段是不同的话，别当成一段。外层 repostId 等于 repost.id，取后者就行。
     只读外层 author 会把「转发的人」当成信号主体，见 RepostedFrom 的说明。
  7. 顶层还有几个键现在不取，查过了，记下来免得以后再查一遍：
     article（38%，是帖子附的外链，指向 linkedin pulse，它不是作者公司的官网，
     所以不能拿来填 domain）。socialContent / engagement / reactions / reactionIds /
     commentIds / header / job / poll / document / newsletterUrl。

SourceAdapter 的实现是文件末尾的 HarvestApiPostsAdapter，它只做编排
（一条 query 一个 run、记计费数），翻译仍然在 to_raw_signal。
另有一个 RecordedPostsAdapter 从存下来的 dataset JSON 回放，同一个形状、
不连网不花钱，拿真实数据跑整条链路用它。
"""

from __future__ import annotations

import gzip
import json
import pathlib
from datetime import datetime

# 引模块不引函数：tests/conftest.py 要把 apify.run_actor 换成当场炸的版本，from 引进来的名字
# import 那一刻就绑死了，换不到
from adapters import apify
from models import ActorType, CompanyRef, QuerySpec, RawSignal, RepostedFrom, SourceType

# Apify 上这个 actor 的 id。斜杠在 API 路径里要写成波浪号。
ACTOR_ID = "harvestapi~linkedin-post-search"

SOURCE_NAME = "harvestapi_posts"


# ─────────────────────────────────────────────────────────── 字段翻译


def _parse_posted_at(item: dict) -> datetime:
    """把 postedAt.date 这个 ISO 串解析成 datetime。

    输入：一条 raw item，形如 {"postedAt": {"date": "2026-07-31T11:07:11.782Z", ...}}
    输出：带时区的 datetime
    缺失就直接炸，不给「用当前时间兜底」——那会让 recency 这一维读出满分，
    而这条线索可能是三年前的。
    """
    posted = (item.get("postedAt") or {}).get("date")
    if not posted:
        raise ValueError(f"postedAt.date 缺失，external_id={item.get('id')!r}")
    # Python 3.11 之前 fromisoformat 不认结尾的 Z，统一换成 +00:00 再解析
    return datetime.fromisoformat(posted.replace("Z", "+00:00"))


def _actor_company(author: dict, external_id: object) -> CompanyRef | None:
    """从发帖人对象里取「发帖人所属公司」，只在能结构化拿到时才返回。

    输入：raw item 的 author 对象 + 出错时用来定位的 id
    输出：CompanyRef（company 类型作者）或 None（profile 类型作者）

    profile 作者这里一律返回 None，由 normalize 去解析 actor_headline 兜底。
    profile 的 author.id 是个人 URN 不是公司 id。

    公司主页作者缺 name 时当场炸，不拿空串兜底。
    以前那句 `author.get("name") or ""` 编出来的
    空串会一路流进库里：normalized_name 写成空字符串（空串过得了 not null，
    那个约束在这件事上给的是假安心），于是那一行以后只能靠 id 找回来 ——
    同一家公司再从职位串被解析出来（那条路只有名字）时接不上，库里多一行；
    display_name 同样是空的，Slack 卡片上公司名一片空白。
    症状离原因隔着好几天，所以宁可在这里炸。
    """
    if author.get("type") != "company":
        return None

    # 到这里作者一定是公司主页，author.id 就是 LinkedIn 的公司数字 id。
    # name 用下标式的取法（取不到就抛），跟下面 author["id"] 一个道理：
    # 身份字段缺了要当场知道，不要编一个值让它往下走。
    name = author.get("name")
    if not name:
        raise ValueError(
            f"公司主页作者没有 name：id={author.get('id')!r} "
            f"linkedinUrl={author.get('linkedinUrl')!r} external_id={external_id!r}"
        )

    return CompanyRef(
        name=name,
        linkedin_company_id=str(author["id"]),
        linkedin_url=author.get("linkedinUrl"),
        domain=None,  # post search 拿不到官网，见模块头。domain 留给 enrich 阶段
    )


def _mentioned_companies(item: dict) -> tuple[CompanyRef, ...]:
    """把 contentAttributes 里被 @ 链接的公司抽成 CompanyRef 列表。

    输入：一条 raw item
    输出：CompanyRef 元组，可能为空（实测只有 14% 到 18% 的 post 非空）

    contentAttributes 是 LinkedIn 对正文的富文本标注数组，
    每项形如 {"type": "COMPANY_NAME", "start": 308, "length": 21,
              "company": {"id", "name", "linkedinUrl"}}。
    只取 COMPANY_NAME，跳过 PROFILE_MENTION（被 @ 的人，以后可能捡）。
    """
    out: list[CompanyRef] = []

    for attr in item.get("contentAttributes") or []:
        if attr.get("type") != "COMPANY_NAME":
            continue

        company = attr.get("company") or {}
        if not company.get("name"):
            # 有 COMPANY_NAME 标注但没有公司对象，属于异常数据，跳过不猜
            continue

        out.append(
            CompanyRef(
                name=company["name"],
                linkedin_company_id=str(company["id"]) if company.get("id") else None,
                linkedin_url=company.get("linkedinUrl"),
                domain=None,
            )
        )

    return tuple(out)


def _assert_author_invariant(author: dict, external_id: object) -> None:
    """检查「companyId 有值 当且仅当 作者是公司主页」这条不变式。

    输入：任意一个 author 对象 + 出错时用来定位的 id
    输出：无。不成立就炸

    整个 adapter 对 author.id 的用法都建立在这条不变式上（company 作者时
    它是公司数字 id，profile 作者时它是个人 URN）。哪天供应商改了行为，
    要在这里炸，而不是让个人 URN 悄悄流进 linkedin_company_id 造出假身份键。

    拆成独立函数是因为转发的原帖有它自己的 author，那一份也要查
    （实测原帖 10/10 同样成立）。
    """
    has_company_id = author.get("companyId") is not None
    is_company_actor = author.get("type") == "company"
    if has_company_id != is_company_actor:
        raise ValueError(
            f"作者不变式被打破：companyId={author.get('companyId')!r} "
            f"type={author.get('type')!r} external_id={external_id!r}"
        )


def _reposted_from(item: dict) -> RepostedFrom | None:
    """如果这条是转发，把被转发的原帖翻译成 RepostedFrom。

    输入：一条 raw item
    输出：RepostedFrom，或 None（不是转发，实测 216/226 都是 None）

    item["repost"] 是原帖的完整对象，字段跟外层几乎一样
    （author / content / contentAttributes / postedAt / linkedinUrl 都有），
    少的是 type 和 query —— 因为它不是一条检索结果，是被嵌进来的。
    所以下面复用 _actor_company / _mentioned_companies / _parse_posted_at
    这三个函数，它们只认字段形状不认「这是外层还是内层」。

    外层还有一个 repostId 字段，等于 repost.id，是同一个东西的
    两种写法，只取后者。

    实测没有嵌套转发（转发一条转发），所以不递归。真出现了会在这里
    静默丢掉最内层那条 —— 那时该改的是这个函数，不是在别处兜底。
    """
    repost = item.get("repost")
    if not repost:
        return None

    author = repost.get("author") or {}
    _assert_author_invariant(author, repost.get("id"))

    external_id = repost.get("id")
    if not external_id:
        # 原帖没 id 说明这个对象结构不对，宁可炸也不要造一条身份不明的证据
        raise ValueError(f"repost 没有 id，外层 external_id={item.get('id')!r}")

    return RepostedFrom(
        external_id=str(external_id),
        url=repost.get("linkedinUrl") or "",
        # 原帖正文。注意它跟外层 RawSignal.text 是两段不同的话：
        # 外层是转发者自己加的那句（常常很短甚至只是一句感叹），
        # 这里才是有内容的那段。两段各存一份，不互相覆盖。
        text=repost.get("content") or "",
        actor_name=author.get("name") or "",
        actor_headline=author.get("info") or "",
        actor_url=author.get("linkedinUrl") or "",
        actor_type=ActorType(author.get("type") or "profile"),
        actor_company=_actor_company(author, external_id),
        mentioned_companies=_mentioned_companies(repost),
        # 原帖发布时刻，一定早于外层那个。打分的 recency 走的是这一个，
        # 但调用方不要直接读它，读 RawSignal.event_at ——
        # 「转发该用哪个时刻」这条口径只允许有一份实现。
        observed_at=_parse_posted_at(repost),
    )


def to_raw_signal(item: dict) -> RawSignal:
    """把一条 HarvestAPI 的 raw item 翻译成 RawSignal。这个模块的核心函数。

    输入：供应商返回的原始 dict（一条 post）
    输出：一个 RawSignal，raw 字段里原样保留整个输入
    """
    author = item.get("author") or {}
    _assert_author_invariant(author, item.get("id"))

    # 这条 post 的唯一 id。
    #
    # 用 item["id"] 不用 item["postId"]
    #
    # item["id"] 就是 LinkedIn 自己的 activity id，不是 HarvestAPI 编的号
    #
    # 我们这边叫 external_id 而不叫 post_id，是因为 RawSignal 将来还要装
    # 别的数据源的 id（众筹项目 id、FCC 申请号），但其实就是LinkedIn 的post id。
    #
    # 注意它在一跑之内不是唯一的：同一个帖子被两条不同的 query 搜到，
    # 会作为两条 item 返回、id 相同。
    # 跑内按 external_id 去重是后面 prefilter 的事，这里只负责翻译。
    external_id = item.get("id")
    if not external_id:
        raise ValueError(f"raw item 没有 id：{str(item)[:200]}")

    # query 对象是 actor 回显的检索参数，自带「这条是哪个词搜出来的」归因
    query_search = (item.get("query") or {}).get("search") or ""

    # 下面每个字段都注明「作者是个人」和「作者是公司主页」两种情况下装的是什么。
    # 这两种作者占比 194 : 32（实测 226 条），而且它们的字段含义不是「详细程度不同」，
    # 有几个是含义完全不同 —— 比如 actor_name 一个装人名一个装公司名。
    return RawSignal(
        # 哪个 adapter 翻译出来的。两种作者相同，换供应商时靠它区分来源
        source=SOURCE_NAME,
        source_type=SourceType.LINKEDIN_POST,
        # 两种作者相同：item["id"] 就是 LinkedIn 的 activity id，跟作者是谁无关
        external_id=str(external_id),
        # 帖子本身的永久链接，两种作者相同（注意不是作者主页链接，那个在 actor_url）
        url=item.get("linkedinUrl") or "",
        # 帖子正文。字段两种作者相同，但文本性质差别很大：
        #   个人      多是第一人称，"我们正在找代工厂" 这种买方自述只出现在这一类
        #   公司主页  多是公告或营销稿，里面的「我们」指的是这家公司自己
        #
        # 这条是转发时，这里装的是转发者自己加的那句话，不是原帖正文
        # （原帖正文在 reposted_from.text）。这两段永远各存一份，
        # 没有任何一步会把这里改写成原帖正文：
        #   要判断转发者本人是不是买方，看的就是他自己写的这句 + 他的 headline
        #   要看这件事本身讲了什么，才去读 reposted_from.text
        # 一个字段装两种来源的话，下游拿到它就分不清「谁说的」，
        # 而所有跟发帖人身份有关的规则都要靠这个区分。
        # 两段一起要用的地方（防幻觉闸门取材）读 RawSignal.quotable_texts。
        #
        # 空正文允许存在，会被 prefilter 的命中词规则砍掉
        text=item.get("content") or "",
        # 归因：这条是哪个 query 搜出来的。两种作者相同
        query_search=query_search,
        # 以下四个 actor_ 字段描述的是「发帖的那个主体」，不是「那个主体所属的公司」。
        # 作者是公司主页时这个主体本身就是一家公司，所以两者重合；
        # 作者是个人时这个主体是一个人，公司不在这里。
        #
        #   个人      人名，如 "Neil Shah"
        #   公司主页  公司名，如 "North Field Technologies"，与 actor_company.name 同值
        actor_name=author.get("name") or "",
        # HarvestAPI 把这串放在 author.info：
        #   个人      职位串，如 "Founder & CEO at Lumen | Audio wearables"。
        #             这是 normalize 里 company_from_headline 的唯一原料
        #   公司主页  公司自述的一句 tagline，既没有职位也不一定含公司名，解析不出东西
        actor_headline=author.get("info") or "",
        #   个人      /in/xxx 个人主页
        #   公司主页  /company/xxx 公司主页，与 actor_company.linkedin_url 同值
        actor_url=author.get("linkedinUrl") or "",
        # 上面那个不变式已经保证了它跟 author.companyId 有没有值是一回事
        actor_type=ActorType(author.get("type") or "profile"),
        #   个人      一律 None。author.id 是个人 URN（ACoAA 开头）不是公司 id，
        #             往这里塞会造出一堆假身份键。公司身份留给 normalize 从
        #             actor_headline 解析，解析不出就走 NO_IDENTITY 丢掉
        #   公司主页  必有值，而且 linkedin_company_id 一定填上了
        #             （== author.id == author.companyId，实测 32/32），
        #             这是身份键梯度里最高的一级，跨源归并最可靠
        actor_company=_actor_company(author, item.get("id")),
        # 跟作者类型无关：两种作者都可能带、也都可能空
        # 四种组合都真实存在，这是三段基数必须是 1:0..N、不能压成 1:1 的直接证据：
        #   个人   + 无 mention 
        #   个人   + 有 mention    
        #   公司   + 无 mention    
        #   公司   + 有 mention 
        mentioned_companies=_mentioned_companies(item),
        # 这条是不是转发。跟作者类型无关， 也就是说这条通道给的公司身份
        # 全都是外层拿不到的。理由和为什么不展开成第二条 signal 见 RepostedFrom
        reposted_from=_reposted_from(item),
        # 帖子发布时刻，两种作者相同。缺失直接炸，理由见 _parse_posted_at
        #
        # 这条是转发时，这里装的是转发那一刻，不是原帖发布时刻
        # （原帖时刻在 reposted_from.observed_at）。同样两份都留着，
        # 因为两个时刻各有独立用途，谁也替代不了谁：
        #   这里的时刻   跟采集窗口对账用它（postedLimit 筛的是外层时刻，
        #                改掉这里会让数据里出现比窗口更早的帖子），
        #                跨天认出同一条转发也用它
        #   原帖的时刻   打分的 recency 用它，理由见 RawSignal.event_at
        # 打分层不要自己在这两个之间选，读 event_at，那条口径只有一份实现。
        observed_at=_parse_posted_at(item),
        # 原始 JSON 一个字都不动地留着，供应商会死，数据要落自己手里。
        # 它还让「现在不取某个字段」这个决定可逆：PROFILE_MENTION 之类以后想捡回来，
        # 改 adapter 重跑 from_file 就行，不用重新花钱采集
        raw=item,
    )


def _is_post(item: dict) -> bool:
    """判断一条 item 是 post 还是 comment。

    评论会作为独立 item 混在同一个 dataset 里（type == "comment"）。
    它们参与计费但不是线索来源，翻译阶段直接跳过。
    """
    return item.get("type") != "comment"


# ─────────────────────────────────────────────────────────── 两个入口


def read_dataset(path: str | pathlib.Path) -> list[dict]:
    """把存下来的 dataset 文件读成原始 item 列表，压缩的和没压缩的都认。

    输入：一个 JSON 文件路径，.gz 结尾的按 gzip 解，其余按纯文本
    输出：原始 item 的 list（一条 post 或 comment 一项，不做过滤）

    两种情况：

        输入 'data/harvest_run27_2026-09-02.json.gz'  -> 先解压再解析
        输入 'data/harvest_2026-08-27_010047.json'     -> 直接解析

    两种都认是因为早期那几份没压缩，而它们是现有的回归集。
    按后缀判而不是试探文件头：后缀写错就该当场炸，试探会把
    「我以为存的是压缩的、其实没压」这种事悄悄咽掉。
    """
    file = pathlib.Path(path)
    if file.suffix == ".gz":
        return json.loads(gzip.decompress(file.read_bytes()).decode("utf-8"))
    return json.loads(file.read_text(encoding="utf-8"))


def write_dataset(path: str | pathlib.Path, raw_items: list[dict]) -> None:
    """把原始 item 列表存成 gzip 压缩的 JSON。

    输入：落盘路径（.gz 结尾）；供应商返回的原始 item 列表
    输出：无返回值，副作用是写那个文件

    一个例子：

        输入   path='data/harvest_run27_2026-09-02.json.gz'，一跑两条 query 的 item
               序列化成带缩进的 JSON 文本，再 gzip
        输出   无返回值。压缩后的体积大约是原文的五分之一

    压缩的理由是这些文件要长期留在 git 里：一天一份、一份几百 KB，
    一年就是上百兆，而 git 的历史删不掉 —— 提交进去之后再改成压缩，
    缩不掉已经躺在历史里的那些。

    先写临时文件再改名，是为了别留下半个文件：写到一半崩掉的话，
    那个半截文件会被下次 read_dataset 读到，而 gzip 解压半截数据报的错
    跟「文件本来就坏了」长得一样，查起来会先怀疑到供应商头上。

    ensure_ascii 关掉：正文里有中文和 emoji，转义成十六进制码位之后人就
    读不了了，而这些文件是要拿来逐条看数据质量的。
    """
    file = pathlib.Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(raw_items, ensure_ascii=False, indent=2).encode("utf-8")

    # 临时文件用同目录，不用系统临时目录：跨盘的话 replace 不是原子的。
    tmp = file.with_suffix(file.suffix + ".tmp")
    tmp.write_bytes(gzip.compress(payload, 9))
    tmp.replace(file)


def from_file(path: str | pathlib.Path) -> list[RawSignal]:
    """从已经下载好的 Apify dataset 文件里读信号。零成本，开发主力入口。

    输入：一个 dataset 文件路径（Apify 导出的，顶层是 list；.gz 也认）
    输出：RawSignal 列表（已过滤掉 comment）

    用它开发 normalize 和 prefilter：字段形状跟线上完全一样，
    改一次规则一秒看到结果，不用每次花钱调 API。
    """
    raw_items = read_dataset(path)

    # raw_items 是全部 item（含 comment），posts 是过滤后的
    posts = [item for item in raw_items if _is_post(item)]

    return [to_raw_signal(item) for item in posts]


def build_input(
    queries: list[str],
    *,
    max_posts_per_query: int,
    posted_limit: str = "month",
    posted_since: str | None = None,
    sort_by: str = "relevance",
    scrape_comments: bool = False,
    scrape_reactions: bool = False,
    filters: dict[str, str] | None = None,
) -> dict:
    """拼 actor 的 input 对象。单独拆出来是为了不花钱就能测「参数拼对没有」。

    输入：query 文本列表 + 检索参数
    输出：可以直接 POST 给 Apify 的 dict

    签名里的那个单独的星号：它后面的参数只能用名字传，不能按位置传。
    写成 build_input(qs, max_posts_per_query=20) 可以，build_input(qs, 20) 报错。
    这个函数有四个参数是数字或短字符串，允许按位置传的话，
    调换两个位置不会报错，只会静默跑出不对的结果。

    只放已核实过的字段名。传一个 actor 不认识的键，它可能直接判输入非法而失败。

    filters 是这条 query 自己的采集旋钮（今天只可能有 authorKeywords），
    原样摊进请求体。它跟上面那几个参数分开传，因为地位不同：那几个是
    「这一跑怎么跑」（一跑一套），filters 是「这条 query 是什么」的一部分
    （进了 queries 表的唯一键）。

    键名和值的合法性不在这里查，在 models.validate_filters（进库那一步查）。
    这一层只做翻译，判断口径归我们自己那一侧 —— 否则接第二个供应商时两边规则
    一漂，静默失效。

    时间范围两种给法，只传一种：
        posted_since='2026-07-28'   传 postedLimitDate，不传 postedLimit。用户在对话里说了多久
        posted_since=None           传 postedLimit=posted_limit，那几档之一。定时那一跑，
                                    或者用户没说时间、说了不限时间
    """
    # 输入   posted_since='2026-07-28'          输出   {"postedLimitDate": "2026-07-28"}
    # 输入   posted_since=None, 'month'         输出   {"postedLimit": "month"}
    # postedLimitDate 实测（每次 5 条）：截止 09-21、按相关度排，回来最早一条是 09-21 12:00；
    # 截止放到 07-28，同一个词就能回来 09-07 的；只收回来那几条的钱，被挡掉的不收
    window = {"postedLimitDate": posted_since} if posted_since else {"postedLimit": posted_limit}
    return {
        # 旋钮摊在最前面，所以下面那几个固定参数永远盖得过它。
        # 顺序反过来的话，filters 里一个叫 maxPosts 的键就能把这一跑的上限改掉
        # （实测过：写在最后时 {'maxPosts': 9999} 真的会生效）。
        # validate_filters 的白名单本来就不让这种键进库，但那是另一个模块的事 ——
        # 两道防线互不依赖，这一道的成本只是把这一行写在前面。
        **(filters or {}),
        # 每条 query 就是 LinkedIn 搜索框里那句话，支持 AND / OR / NOT。
        # 单条限 500 字符、最多 5 个布尔符。
        "searchQueries": queries,
        # 注意这是「每条 query 的上限」，不是这一跑的总量。
        # 实际条数约等于 len(queries) 乘以这个数。
        "maxPosts": max_posts_per_query,
        # 时间范围过滤，postedLimit 或 postedLimitDate 二选一，见上面 window 那一句。
        # postedLimit 填 month 就是只要过去一个月发的帖子（实测：08-21 那跑拿回来的
        # 日期范围正好是 07-22 到 08-21）。
        **window,
        # "relevance" 或 "date"。实测 relevance 排序也强烈偏新。
        "sortBy": sort_by,
        # 评论：别人在帖子下写的文字回复，有正文、有作者。
        # 关掉的真实理由是成本，不是「验证过没价值」——我们从没单独评估过评论。
        # 它开着会让计费 item 翻倍（Run A 实测：60 post + 60 comment = 120 个计费单位）。
        # 以后值得单独拿一条 query 做一次对照：评论区里「我们上个月也在找代工厂」
        # 这种话是买方自述，而且评论者本人就是可联系的人。
        "scrapeComments": scrape_comments,
        # 表态：Like / Celebrate / Support 这类，只有「谁 + 哪种表情」，没有一个字正文。
        # 建议一直关着，信号太弱。
        "scrapeReactions": scrape_reactions,
    }


def from_apify(
    queries: list[str],
    *,
    max_posts_per_query: int = 20,
    posted_limit: str = "month",
    posted_since: str | None = None,
    sort_by: str = "relevance",
    poll_seconds: float = 5.0,
    timeout_seconds: float = 900.0,
    filters: dict[str, str] | None = None,
) -> tuple[list[RawSignal], int, list[dict]]:
    """真调 Apify 跑一次采集。这一步花钱。

    输入：query 文本列表 + 检索参数（星号后面的参数只能用名字传，理由见 build_input）
    输出：(RawSignal 列表, 计费 item 总数, 供应商返回的原始 item 列表)

    第二个返回值是计费口径的条数（含 comment），跟列表长度不相等，
    要落进 ExecutionCounters.billed_item_count。

    第三个是原样的 dataset，一条没删没改，给调用方存盘用。它必须从这里
    返回，不能让调用方拿 RawSignal.raw 反推：反推只能拿到 post，而 dataset
    里可能混着 comment（开了评论抓取时评论是独立 item），存下来就缺了一块
    却看不出来 —— 而这份数据的用途恰恰是「以后拿它重跑闸门」，缺一块就不准。

    起 run、轮询、取数据那三次请求在 apify.run_actor 里（三个 actor 共用那一份），
    这里只做这个 actor 自己的事：拼 input、分出帖子和评论、翻译成 RawSignal。
    """
    # 输入   queries=['wearable AND microphone']，max_posts_per_query=40
    # 输出   raw_items=[{"type": "post", "id": "7362...", ...}, {"type": "comment", ...}, ...]
    #        起 run、轮询、取数据那三步在 apify.run_actor 里，三个 actor 共用。
    #        它还带回这一次真扣的钱（.cost_usd），采集这边暂时不用：月度上限按 billed_item_count 算
    raw_items = apify.run_actor(
        ACTOR_ID,
        build_input(
            queries,
            max_posts_per_query=max_posts_per_query,
            posted_limit=posted_limit,
            posted_since=posted_since,
            sort_by=sort_by,
            filters=filters,
        ),
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    ).items

    # raw_items 是全部 item（scrape_comments 开着时会混入 type=="comment"），
    # posts 是过滤后只剩帖子的。两者数量不同，而计费按前者算。
    billed_item_count = len(raw_items)
    posts = [item for item in raw_items if _is_post(item)]
    print(f"[apify] 计费 item {billed_item_count} 条，其中 post {len(posts)} 条")

    return [to_raw_signal(item) for item in posts], billed_item_count, raw_items


class HarvestApiPostsAdapter:
    """SourceAdapter 的 HarvestAPI 实现：一条 query 一个 Apify run。

    输入：构造时给检索参数（每次 fetch 都一样的那几个）
    输出：一个可以交给 pipeline 的采集实现

    这个类自己不翻译字段，翻译在 to_raw_signal。它做的是三件编排的事：
    把 QuerySpec 变成 actor 的输入、一条 query 起一个 run、把计费条数记下来。

    为什么一条 query 一个 run（Alex 定的）：billed_item_count 是
    query_executions 的列，一个 run 传多条 query 的话，Apify 只给一个
    dataset、一个总数，per-query 的花费只能按条数摊 —— 而一条 query 命中
    大量评论、另一条几乎没有的时候，摊出来的数是错的，且看不出来。
    附带的好处是采集失败天然隔离到单条：一条 query 的 run 挂了，别的照跑。
    代价是串行起两次 run、多等一轮轮询，而这是每天一次的定时任务。

    实例是有状态的（billed_items 记着上一次 fetch 的数），所以一个实例
    同一时刻只能跑一条 query。pipeline 就是这么用的：逐条 query 串行，
    fetch 之后紧接着取计费数。

    dump_path 给了的话，每次 fetch 之后把这一跑攒到的原始 dataset 整份
    重写到那个文件（gzip）。存的是闸门之前的原料，理由见 fetch 里那一段。

    一个例子：

        adapter = HarvestApiPostsAdapter(
            dump_path="data/harvest_run27_2026-09-02.json.gz")
        signals = adapter.fetch(spec, max_items=40)   # 起一个 run，等它跑完
        adapter.billed_items()                        # 计费条数，含评论那一半
    """

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        posted_limit: str = "month",
        posted_since: str | None = None,
        sort_by: str = "relevance",
        poll_seconds: float = 5.0,
        timeout_seconds: float = 900.0,
        dump_path: str | pathlib.Path | None = None,
    ) -> None:
        self._posted_limit = posted_limit
        # 给了就按日期截（postedLimitDate），posted_limit 那一档不再传，见 build_input
        self._posted_since = posted_since
        self._sort_by = sort_by
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds

        # 原始 dataset 的落盘路径。None 就不存。
        # _collected 把这一跑各条 query 的原始 item 攒在一起，每次 fetch
        # 之后整份重写一遍 —— 一个实例服务一整跑，所以最后那份就是全的，
        # 而中途崩掉时已经采到的那些也还在文件里（钱已经花了，数据不能丢）。
        self._dump_path = pathlib.Path(dump_path) if dump_path else None
        self._collected: list[dict] = []
        # None 表示还没跑过 fetch。用 None 不用 0：0 是「这次采集一条都没
        # 计费」，是个合法结果，而「还没采过」是调用顺序错了。
        self._billed: int | None = None

    def fetch(self, spec: QuerySpec, *, max_items: int) -> list[RawSignal]:
        """跑这一条 query，返回翻译好的信号。这一步花钱。

        输入：一条 QuerySpec；这条 query 最多要几条 post
        输出：RawSignal 列表

        max_items 是天花板不是地板：供应商只有 12 条就返回 12 条，不凑数。

        source_type 对不上就当场炸。一条众筹或者公司搜索的 query 被交到
        这个 adapter 手上时，它会被原样塞进 LinkedIn 的搜索框 —— 那不会
        报错，只会拿回一批不相干的帖子，而报表上这条 query 看起来跑成功了。

        抓不到就抛（from_apify 自己会抛），不返回空列表冒充成功：静默 0 条
        会让这条 query 的表现指标记成「没线索」，而实际是采集失败了，
        污染的是决定每天 60% 预算的那组数字。

        一个例子：

            输入   spec=QuerySpec(query_id=30,
                                  text='Kickstarter AND (microphone OR audio)',
                                  source_type=LINKEDIN_POST, pool=CANDIDATE,
                                  cadence_days=1)
                   max_items=40
                   起一个 Apify run 只跑这一条 query，轮询到 SUCCEEDED，
                   dataset 里 31 个 item（这次没开评论，所以 31 条全是 post）
            输出   31 个 RawSignal；紧接着 billed_items() 给 31

            要是这条 query 开了评论抓取，dataset 里会是 62 个 item，
            输出仍然是 31 个 RawSignal，而 billed_items() 给 62 ——
            两个数不相等正是它们要分开记的理由。
        """
        if spec.source_type is not SourceType.LINKEDIN_POST:
            raise ValueError(
                f"这个 adapter 只跑 linkedin_post，收到的是 {spec.source_type}"
                f"（query_id={spec.query_id}）"
            )

        # 先把上一条 query 的计费数清掉再采。不清的话这一条采集失败时，
        # billed_items() 会安安静静地把上一条的数还给调用方，然后那笔钱
        # 记在这条 query 的账上 —— 而 billed_item_count 是「每块钱几个
        # 合格线索」的分母，记错了看不出来。清成 None 之后同样的情况会抛。
        self._billed = None

        signals, billed, raw_items = from_apify(
            [spec.text],
            max_posts_per_query=max_items,
            posted_limit=self._posted_limit,
            posted_since=self._posted_since,
            sort_by=self._sort_by,
            poll_seconds=self._poll_seconds,
            timeout_seconds=self._timeout_seconds,
            # 旋钮跟着这条 query 走，不是这个 adapter 实例的属性 ——
            # 一个实例服务一整跑，而一跑里每条 query 的旋钮可以各不相同。
            filters=spec.filters,
        )
        self._billed = billed

        # 原始 dataset 落盘。放在返回之前、放在任何过滤之后面都不行 ——
        # 存的必须是闸门之前的原料：往下走一步就开始砍了，而砍掉的那批
        # 恰恰是以后验「这条规则有没有误砍真买方」要用的样本。
        if self._dump_path is not None:
            self._collected.extend(raw_items)
            write_dataset(self._dump_path, self._collected)
            print(f"[apify] 原始数据已存 {self._dump_path}（累计 {len(self._collected)} 条）")

        return signals

    def billed_items(self) -> int:
        """上一次 fetch 实际计费的 item 数，含评论那一半。

        输入：无
        输出：一个整数

        没跑过 fetch 就问会抛，不返回 0：0 会被原样写进
        query_executions.billed_item_count，然后这条 query 在「每块钱几个
        合格线索」那个指标上显得免费，而那个分母决定预算怎么分。

        三种情况：

            fetch 成功之后问   -> 那一次 run 的计费 item 数，比如 120
            一次都没 fetch 过  -> 抛 RuntimeError
            fetch 抛异常之后问 -> 也抛 RuntimeError，不会给上一条 query 的数

        最后一种是这个字段唯一会骗人的地方，所以 fetch 一开头就把它清掉。
        """
        if self._billed is None:
            raise RuntimeError("还没跑过 fetch，没有计费数可给")
        return self._billed


class RecordedPostsAdapter:
    """从已经存下来的 dataset JSON 回放，不连网不花钱。

    输入：一个 Apify dataset JSON 的路径
    输出：一个跟真 adapter 同形状的采集实现

    用途是拿真实数据跑整条链路：pipeline 加真 store 加真库，唯一假的是
    采集那一步。真 adapter 那条路每跑一次约两毛钱，而这条链路要反复跑。

    按 query 文本回放，不是把整份文件返回：dataset 里每条 item 自带
    query.search 归因（RawSignal.query_search），所以同一份文件能同时
    服务当初那几条 query。data/harvest_2026-08-27 那份里有五条 query，
    其中三条正好是库里那三条种子。

    一个例子：

        adapter = RecordedPostsAdapter('data/harvest_2026-08-27_010047.json')
        adapter.fetch(spec, max_items=40)   # spec.text 是那三条种子之一
        -> 40 个 RawSignal，一分钱没花
        adapter.billed_items()              # 40

    这不是测试桩，是开发主力入口 —— 跟 from_file 同一个定位。
    """

    name = SOURCE_NAME

    def __init__(self, path: str | pathlib.Path) -> None:
        self._signals = from_file(path)
        self._path = str(path)
        # 文件里一共有哪几条 query，出错时要把它们列出来。
        self._available = sorted({signal.query_search for signal in self._signals})
        self._billed: int | None = None

    def fetch(self, spec: QuerySpec, *, max_items: int) -> list[RawSignal]:
        """把这条 query 当初采到的那批信号回放出来。

        输入：一条 QuerySpec；最多回放几条
        输出：RawSignal 列表

        一个例子：

            文件里  五条 query 的 191 条 post，其中
                    'wearable AND (microphone OR audio OR voice)' 有 40 条
            输入    spec.text 是上面那句，max_items=40
            输出    那 40 个 RawSignal；billed_items() 给 40

        文本对不上返回空列表，但会印一行警告并列出文件里有哪几条 query。
        空列表本身是合法结果（真采集也会有「今天什么都没搜到」），所以不炸；
        但在回放这个用途下它几乎总是文本写错了 —— 而整跑会一路正常地跑完、
        产出 0 家公司，报表上跟「今天确实没线索」长得一模一样。
        印出来是让这两种情况在日志里能分开。

        max_items 的截断在这里是「回放前 n 条」，跟真采集的「最多要 n 条」
        不完全等价：真跑时供应商按相关度排序给前 n 条，而文件里的顺序是
        当初那一跑的顺序。同一批数据反复跑时它是确定的，够回归用。

        已知窄处：只按文本配，不看 spec.filters。0009 之后同一句话配不同旋钮
        是两条不同的 query（采回来的也确实是两批不同的帖子），而回放会把同一批
        数据还给它们两个。今天碰不到 —— 库里所有 query 的 filters 都是空的，
        而落盘的 dataset 里也没有记下当初用的旋钮。真要分开，得先让落盘那一步
        把 filters 一起写进文件。在那之前，别拿回放去验跟旋钮有关的行为。
        """
        matched = [signal for signal in self._signals if signal.query_search == spec.text]
        if not matched:
            print(f"[replay] 警告：{self._path} 里没有这条 query 的数据：{spec.text!r}")
            print(f"[replay] 文件里有这几条：{self._available}")
        matched = matched[:max_items]
        self._billed = len(matched)
        return matched

    def billed_items(self) -> int:
        """上一次回放了几条。

        输入：无
        输出：一个整数

        跟真跑的计费口径不同：真跑含评论那一半，这里回放的全是 post。
        它只是让下游的计数列有个非零的形状，不是钱。回放跑出来的
        billed_item_count 不能拿去跟真跑的比。
        """
        if self._billed is None:
            raise RuntimeError("还没跑过 fetch，没有计费数可给")
        return self._billed
