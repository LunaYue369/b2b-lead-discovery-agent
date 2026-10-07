"""HarvestAPI LinkedIn 公司详情（Apify actor: harvestapi/linkedin-company）。

只做两件事：拿 LinkedIn 公司 id 去查（花钱）、把返回的一条翻译成 CompanyProfile。
不判断这家该不该查、不算钱、不写库，那些在 triggers/contacts.py。

价格（Apify 的 pricingInfos 里 BRONZE 档，也就是我们这一档）：每家 $0.004，
每次 run 另收一次启动费 $0.00005。单价不写在这里，写在配置里，算钱的是调用方。

字段（例子是 Hushly 的原始返回；Hushly、Bonewave、ORBO Audio 三家的原始返回存在
data/probe_contacts_2026-09-27/，测试拿它们当输入）：

    id                  '100000002'，就是我们 leads.linkedin_company_id 那个数
    website             'http://www.hushly.example'，公司自己在 LinkedIn 上登记的，常会跳转
    employeeCount       11，LinkedIn 上把这家填成现职的人数
    employeeCountRange  {"start": 2, "end": 10}，公司自己选的规模档
    foundedOn           {"month": None, "year": 2022, "day": None}
    locations           [{"headquarter": True, "parsed": {"text": "United States", ...}, ...}, ...]
                        Hushly 有两条：总部只写了国家，另一条是 Brooklyn。取 headquarter 那条
    industries          [{"id": "3205", "name": "Music", "title": "Music", ...}]

没有电话：返回里没有任何带 phone 的键，字段名无从知道，所以不猜 —— CompanyProfile.phone
永远给 None，等哪天真见到了再接。
"""

from __future__ import annotations

from decimal import Decimal

# 引模块不引函数：tests/conftest.py 要把 apify.run_actor 换成当场炸的版本，from 引进来的名字
# import 那一刻就绑死了，换不到
from adapters import apify
from models import CompanyProfile

ACTOR_ID = "harvestapi~linkedin-company"


def company_url(linkedin_company_id: str) -> str:
    """拿 LinkedIn 公司 id 拼 actor 认的公司页链接。

    输入：'100000002'
    输出：'https://www.linkedin.com/company/100000002/'

    用数字 id 不用 slug：slug 公司自己能改，id 不会变。actor 认这种写法
    """
    return f"https://www.linkedin.com/company/{linkedin_company_id}/"


def fetch_companies(linkedin_company_ids: list[str]) -> apify.ActorRun:
    """一次 run 查一批公司，返回原始 item 和这一次真扣的钱。这一步花钱：每家 $0.004，外加一次启动费。

    输入：['100000002', '100000003']
    输出：ActorRun(items=[{"id": "100000002", "name": "Hushly", ...}, ...],
                   cost_usd=Decimal("0.00805"),
                   charged_events={"apify-actor-start": 1, "apify-default-dataset-item": 2})
          查 Bonewave 和 ORBO 两家的样子：两家 0.008 加启动费 0.00005

    一批一次 run 而不是一家一次：启动费一次 run 收一次，而且一次 run 要几秒，
    一家一次的话二十家就是二十次等待。

    返回的条数可能比传进去的少（公司页被删了、id 不对），也不保证顺序跟传进去的一样，
    所以调用方要按 item 的 id 对回去，不能按下标
    """
    if not linkedin_company_ids:
        # 一家都不用查：不起 run，一分钱不花
        return apify.ActorRun(items=[], cost_usd=Decimal("0"), charged_events={})

    # 输入   ['100000002', '100000003']
    # 输出   {"companies": ["https://www.linkedin.com/company/100000002/", ".../100000003/"]}
    return apify.run_actor(ACTOR_ID, {"companies": [company_url(i) for i in linkedin_company_ids]})


def item_company_id(item: dict) -> str:
    """一条返回是哪家公司，拿它把返回对回请求。

    输入：{"id": "100000002", "name": "Hushly", ...}
    输出：'100000002'

    用下标不用 get：没有 id 的 item 对不回任何一家公司，当场 KeyError 比悄悄丢掉好
    """
    return str(item["id"])


def to_profile(item: dict) -> CompanyProfile:
    """把一条返回翻译成 CompanyProfile。纯函数，不连网。

    输入（Hushly 的原始返回，只列用到的键）：
        {"id": "100000002", "website": "http://www.hushly.example", "employeeCount": 11,
         "employeeCountRange": {"start": 2, "end": 10},
         "foundedOn": {"month": None, "year": 2022, "day": None},
         "locations": [{"headquarter": True, "parsed": {"text": "United States"}},
                       {"headquarter": False, "parsed": {"text": "Brooklyn, NY, United States"}}],
         "industries": [{"name": "Music", ...}], ...}
    输出：
        CompanyProfile(website="http://www.hushly.example", employee_count=11,
                       employee_count_range="2-10", founded_year=2022,
                       location="United States", industries=("Music",), phone=None,
                       raw=<整条原样>)

    每一格缺了就是 None，不编：小公司经常只填一部分
    """
    # 输入   "http://www.hushly.example"；空串或没有这个键
    # 输出   "http://www.hushly.example"；None。空串当成没有，卡片上不印一个空的官网
    website = item.get("website") or None

    # 输入   11；负数或者不是整数（不该出现，出现了就是供应商那边的数坏了）
    # 输出   11；None。0011 里这一列有 >= 0 的 CHECK，坏数写进去会让整步失败，在这里先滤掉
    employee_count = item.get("employeeCount")
    if not isinstance(employee_count, int) or isinstance(employee_count, bool) or employee_count < 0:
        employee_count = None

    return CompanyProfile(
        website=website,
        employee_count=employee_count,
        employee_count_range=_range_text(item.get("employeeCountRange")),
        founded_year=_founded_year(item.get("foundedOn")),
        location=_headquarter_text(item.get("locations")),
        # 输入   [{"name": "Music", "title": "Music", ...}]
        # 输出   ("Music",)。没有 name 的那一条跳过；一条都没有是 ()
        industries=tuple(i["name"] for i in item.get("industries") or () if i.get("name")),
        # actor 的返回里没有这个字段，见文件开头
        phone=None,
        raw=item,
    )


def _range_text(value: dict | None) -> str | None:
    """规模档拼成一句卡片上印的话。

    三种：
        {"start": 2, "end": 10}      -> "2-10"
        {"start": 10001, "end": None} -> "10001+"   最高那一档没有上界。这种返回还没见过，
                                                    按「只有下界」的意思写
        None 或没有 start            -> None
    """
    if not value or value.get("start") is None:
        return None
    if value.get("end") is None:
        return f"{value['start']}+"
    return f"{value['start']}-{value['end']}"


def _founded_year(value: dict | None) -> int | None:
    """成立年份。

    输入：{"month": None, "year": 2022, "day": None}
    输出：2022；没有 year、或者年份不在 1800 到 2100 之间是 None

    范围跟 0011 里那条 CHECK 是同一个：超出范围的写进去会让整步失败，在这里先滤掉
    """
    year = (value or {}).get("year")
    if isinstance(year, int) and not isinstance(year, bool) and 1800 <= year <= 2100:
        return year
    return None


def _headquarter_text(locations: list[dict] | None) -> str | None:
    """总部在哪，拼成一句。

    输入：[{"headquarter": True, "parsed": {"text": "United States"}},
           {"headquarter": False, "parsed": {"text": "Brooklyn, NY, United States"}}]
    输出："United States"

    三种：
        有标了 headquarter 的           取那一条的 parsed.text
        一条都没标 headquarter          取第一条：公司只填了一个地址时常常不标
        locations 是空的或者没有        None
    """
    if not locations:
        return None
    chosen = next((loc for loc in locations if loc.get("headquarter")), locations[0])
    return ((chosen.get("parsed") or {}).get("text")) or None
