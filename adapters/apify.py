"""调一次 Apify actor、等它跑完、把数据拿回来。全仓调 Apify 只走这一处。

三个 actor 都用它：

    harvestapi_posts.from_apify           搜帖子（采集），一跑可能几分钟
    harvestapi_company.fetch_companies    公司详情，几秒
    harvestapi_profile.fetch_profiles     个人主页 + 找邮箱，几秒

一次调用发三种 HTTP 请求，问的是三样不同的东西：

    1. POST /acts/{actor}/runs        「开一单」
       立刻返回 run_id 和 dataset_id，这时候一条数据都还没抓
    2. GET  /actor-runs/{run_id}      「那一单做好了吗」
       立刻返回，但答案可能是 RUNNING，所以要反复问
    3. GET  /datasets/{ds_id}/items   「把数据给我」
       前提是第 2 步已经答了 SUCCEEDED
    4. GET  /actor-runs/{run_id}      「这一单最后收了多少钱」
       跟第 2 步是同一个接口。刚答 SUCCEEDED 的那一刻扣费还没记完，要再问到记完为止

Apify 另有一个一步到位的接口（run-sync-get-dataset-items：请求挂着等 actor 跑完、直接回数据），
不用它，是因为它最多只等 300 秒：搜帖子那一跑可能超过，超过了请求断开，而那一单在 Apify
那边照样跑完、照样收钱，我们却拿不到数据。三步走没有这个上限，三个 actor 就都走三步，
只留一条路。代价是找联系方式那两个每次多等一个轮询间隔（几秒）。
"""

from __future__ import annotations

import os
import pathlib
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

API_BASE = "https://api.apify.com/v2"


@dataclass(frozen=True)
class ActorRun:
    """跑完一次 actor 拿回来的东西：数据，和这一次真扣了多少钱。

    一个实际值（查 sam hadid 那一次）：

        ActorRun(items=[{"linkedinUrl": "https://www.linkedin.com/in/sam-hadid-a1b2c3d4", ...}],
                 cost_usd=Decimal("0.01000"),
                 charged_events={"profile": 0, "profile_with_email": 1})

    cost_usd 是 Apify 自己算的（状态回复里的 usageTotalUsd），不是我们拿价格表乘出来的。
    两者会不一样：查 Ravi Shah 那一次没找到邮箱，只按「不带邮箱」收了 $0.004，
    价格表那样算会记成 $0.01。

    状态回复里没有 usageTotalUsd 时是 None（至今没见过，但不能编一个数填进去）。

    charged_events 是每种计费事件各收了几次，例 {"apify-actor-start": 1, "apify-default-dataset-item": 2}，
    对账用：钱对不上的时候看它就知道多收的是哪一样
    """

    items: list[dict]
    cost_usd: Decimal | None
    charged_events: dict[str, int]


def load_token() -> str:
    """读 Apify token。优先环境变量，其次仓库根的 .env。

    输出：token 字符串，读不到就炸

    刻意在调用时才读，不在模块加载时读：module-level 的 os.getenv() 会比
    .env 的加载更早执行，读到的是默认值。
    """
    token = os.environ.get("APIFY_TOKEN")
    if token:
        return token

    env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("APIFY_TOKEN="):
                return line.split("=", 1)[1].strip()

    raise RuntimeError("找不到 APIFY_TOKEN：设环境变量，或写进仓库根的 .env")


def run_actor(
    actor_id: str,
    body: dict,
    *,
    poll_seconds: float = 5.0,
    timeout_seconds: float = 900.0,
    settle_seconds: float = 60.0,
) -> ActorRun:
    """跑一次 actor，等它跑完，返回 dataset 里的全部 item 和这一次真扣的钱。这一步花钱。

    输入：
        actor_id         例 'harvestapi~linkedin-company'，斜杠在 API 路径里要写成波浪号
        body             actor 的 input，例 {"companies": ["https://www.linkedin.com/company/100000002/"]}
        poll_seconds     每隔几秒问一次「做好了吗」
        timeout_seconds  最多等多久，超过就炸
        settle_seconds   跑完之后最多再等多久让扣费记完，超过不炸，印一行提醒、用当时的数
    输出：ActorRun，例
        ActorRun(items=[{"id": "100000002", "name": "Hushly", ...}],
                 cost_usd=Decimal("0.00405"),
                 charged_events={"apify-actor-start": 1, "apify-default-dataset-item": 1})

    失败一律抛，不返回空列表冒充成功：静默返回 0 条，采集那边会把这条 query 记成
    「没线索」，找联系方式那边会把这家记成「查过了、什么都没有」，而实际是没查成。
    """
    token = load_token()

    # token 放请求头，不放 URL 的查询参数。放在 URL 里的话，任何把请求 URL 打出来的日志都会
    # 带上完整的 token：httpx 在 INFO 级别每个请求打一行 "HTTP Request: GET https://...?token=..."，
    # 而常驻的 Slack 进程正好把日志开在 INFO，部署之后这些行会进 journal，谁能看日志谁就拿得到它。
    # 请求头不进那一行日志。Apify 两种写法都认
    with httpx.Client(timeout=60.0, headers={"Authorization": f"Bearer {token}"}) as client:
        # ── 第一步：起 run ────────────────────────────────────────────
        # 输入：actor input，例 {"companies": [...]}
        # 输出：run 元数据，含 id 和 defaultDatasetId
        #
        # 关键点：这个请求一发出去就立刻回来了，采集连一条都还没开始。
        # Apify 收到请求后会立刻创建一条 run 记录、顺便建一个空的 dataset，
        # 把两个 id 还给我们，然后在后台慢慢跑。
        # 所以下面必须有第二步的等待循环 —— 现在直接去取数据，
        # 会拿到一个空列表，而且不报错（那个 dataset 确实存在，只是还是空的）。
        run_response = client.post(f"{API_BASE}/acts/{actor_id}/runs", json=body)
        # raise_for_status 不是「发送请求」，请求在上一行就发完了。
        # 它只做一件事：如果回来的是 4xx / 5xx（401 token 无效、404 actor 不存在、
        # 500 平台故障），就在这里抛异常。
        # 不写这行的话，程序会拿着一个错误回复继续走，然后在下面的 ["data"]
        # 报一个莫名其妙的 KeyError —— 出错的地方和暴露的地方隔了两行，最难查。
        run_response.raise_for_status()

        # 输出   run_id='a1B2c3', dataset_id='d4E5f6'
        run = run_response.json()["data"]
        run_id, dataset_id = run["id"], run["defaultDatasetId"]
        print(f"[apify] {actor_id} run 已启动 run_id={run_id} dataset={dataset_id}")

        # ── 第二步：轮询运行状态，直到跑完 ────────────────────────────
        # 注意这一步取的不是数据，是「状态」。
        #
        # status 依次经过 READY（排队中）-> RUNNING（在跑）-> SUCCEEDED。
        # 这几个词是 Apify 平台定义的 run 生命周期，不是 HarvestAPI 定的，
        # 也不是我们定的。异常结局有 FAILED / ABORTED / TIMED-OUT。
        deadline = time.monotonic() + timeout_seconds
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"run {run_id} 超过 {timeout_seconds} 秒还没结束")

            # 先睡再问：刚 POST 完那一瞬间问一定是 READY，白问一次
            time.sleep(poll_seconds)

            status_response = client.get(f"{API_BASE}/actor-runs/{run_id}")
            # 这个 raise_for_status 检查的是「我这一问有没有问成功」
            # （网络断了、token 失效），跟「采集成没成功」是两码事。
            # token 写错 -> 这里抛 401，我们根本不知道 run 怎么样了。
            # token 没错但 run 跑挂了 -> 这里不抛（HTTP 200，问答是成功的），
            # 失败信息在下面的 status 里。
            status_response.raise_for_status()

            # 输出   'RUNNING'，下一圈再问；'SUCCEEDED'，出循环
            #        run_data 留着：跑完那一次的回复里有这一次真扣了多少钱
            run_data = status_response.json()["data"]
            status = run_data["status"]
            print(f"[apify] status={status}")

            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise RuntimeError(f"run {run_id} 结束状态为 {status}")

        # ── 第三步：把 dataset 里的数据全部拉下来 ─────────────────────
        # 到这里 run 一定是 SUCCEEDED，dataset 里的数据是全的。
        # 输出：一个 list，每项是一条原始 dict，例 [{"id": "100000002", ...}]
        dataset_response = client.get(
            f"{API_BASE}/datasets/{dataset_id}/items",
            params={"clean": "true"},
        )
        dataset_response.raise_for_status()
        items = dataset_response.json()

        # 成功时响应体一定是列表。不是的话（比如 Apify 改了返回格式，给了一个 {"data": ...}），
        # 当场炸：下游会按列表去数「查回来几条」，拿一个 dict 去数会数出键的个数，而且不报错
        if not isinstance(items, list):
            raise RuntimeError(f"{actor_id} 返回的不是 item 列表：{str(items)[:200]}")

        # ── 第四步：等扣费记完 ────────────────────────────────────────
        # 刚答 SUCCEEDED 的那一次回复里，扣费往往还没记全。实测（找联系方式那一跑）：
        #     公司详情   跑完那一刻  usageTotalUsd=0.00005  apify-default-dataset-item 记了 8 次
        #                几分钟后    usageTotalUsd=0.04405  apify-default-dataset-item 记了 11 次（11 条数据）
        #     查人       跑完那一刻  0.06000，几分钟后 0.07000
        # 拿跑完那一刻的数去记 actor_spend，月度上限那边就会少算钱。
        #
        # 状态回复里跟钱有关的是三样，到得有先后。单跑一次公司详情（1 家）每秒问一次的实测：
        #     跑完那一刻   chargedEventCounts dataset-item=0   accounted...=0   usageTotalUsd=0.00005
        #     +6.5 秒      chargedEventCounts dataset-item=1   accounted...=0   usageTotalUsd=0.00005
        #     +7.6 秒      chargedEventCounts dataset-item=1   accounted...=1   usageTotalUsd=0.00405
        # chargedEventCounts 是「记了几次」，accountedChargedEventCounts 是「其中几次已经算进钱里」，
        # usageTotalUsd 跟着后者走。所以两个条件都要满足才算记完：
        #     一、按条收钱的次数（开机费不算）追上数据条数：每条数据都要收一次钱
        #         （帖子按 post、公司按 dataset-item、人按 profile 或 profile_with_email）
        #     二、算进钱里的次数等于记了的次数
        # 只看第一条是不够的：run 48 查两个人时两次都记上了，但钱还只算了一次，记成了 $0.01（实际 $0.02）
        # 输入   1 条数据，charged {'apify-actor-start': 1, 'apify-default-dataset-item': 1}，
        #        accounted {'apify-actor-start': 1, 'apify-default-dataset-item': 0}
        # 输出   条件二不满足，隔几秒再问
        #
        # 等到 settle_seconds 还没记完不炸：数据已经拿到手了，钱只是记账用的，
        # 为了记账把整步判成失败不划算。印一行提醒，用当时那个数
        settle_deadline = time.monotonic() + settle_seconds
        while not _charges_settled(run_data, len(items)):
            if time.monotonic() > settle_deadline:
                print(
                    f"[apify] {actor_id} 等了 {settle_seconds} 秒扣费还没记完"
                    f"（数据 {len(items)} 条，记了 {run_data.get('chargedEventCounts')}，"
                    f"算进钱里的 {run_data.get('accountedChargedEventCounts')}），按当时的数记账，可能偏少"
                )
                break
            time.sleep(poll_seconds)
            status_response = client.get(f"{API_BASE}/actor-runs/{run_id}")
            status_response.raise_for_status()
            run_data = status_response.json()["data"]

    # 这一次真扣的钱
    # 输入   run_data["usageTotalUsd"]=0.020050000000000002（Apify 给的是浮点，带尾巴）
    # 输出   Decimal("0.02005")。先转成字符串再转 Decimal，再取到小数点后五位，
    #        跟 actor_spend.cost_usd 那一列 numeric(10, 5) 同一个精度
    usage = run_data.get("usageTotalUsd")
    cost = None if usage is None else Decimal(str(usage)).quantize(Decimal("0.00001"))

    # 输入   {"profile": 0, "profile_with_email": 1}
    # 输出   同样的 dict；没有这个键是 {}
    events = {name: int(count) for name, count in (run_data.get("chargedEventCounts") or {}).items()}
    print(f"[apify] {actor_id} 扣费 {cost} 美元 {events}")

    return ActorRun(items=items, cost_usd=cost, charged_events=events)


def _charges_settled(run_data: dict, item_count: int) -> bool:
    """这一次状态回复里的钱是不是已经记完了。两个条件见 run_actor 第四步。

    输入：状态回复；数据条数
    输出：True / False，三个例子（查两个人）：
        charged {"profile": 0, "profile_with_email": 1}  accounted 同上             -> False  1 < 2
        charged {"profile": 0, "profile_with_email": 2}  accounted ... email: 1     -> False  钱还没算全
        charged {"profile": 0, "profile_with_email": 2}  accounted 同 charged        -> True
    没有 chargedEventCounts 按 {} 算：数据是 0 条时算记完，否则不算
    """
    charged = run_data.get("chargedEventCounts") or {}
    accounted = run_data.get("accountedChargedEventCounts") or {}
    per_item = sum(int(count) for name, count in charged.items() if name != "apify-actor-start")
    return per_item >= item_count and {k: int(v) for k, v in accounted.items()} == {
        k: int(v) for k, v in charged.items()
    }
