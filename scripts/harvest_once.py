"""跑一次真实采集，把原始 dataset 存进 data/（gzip）。这个脚本会花钱。

存原始 JSON 而不是只存翻译后的信号，理由有两个：
    供应商会死。数据落自己手里，以后换供应商时这批数据还能重跑
    它是 prefilter 的回归集。改一次词表就拿它跑一遍，看误砍率有没有变差

用法（在仓库根目录跑，要用 -m，否则 import 不到 adapters）：
    python -m scripts.harvest_once --max-posts 40 "query 一" "query 二"

query 直接写在命令行上，不从配置文件读。因为一次采集用哪几条 query 是
一次性的决定（有些 query 是专门为了招某类噪音、验某条规则），
而 config/queries.yaml 装的是准备上线的种子 query，两者不是一回事。
采回来的数据里每条都回显了把它搜出来的 query，所以数据集自己记着来源。
"""

from __future__ import annotations

import argparse
import pathlib
from datetime import UTC, datetime

from adapters import harvestapi_posts

# 原始数据的落盘目录。跟着 repo 走，不放临时目录。
DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    """解析参数、跑采集、把原始 JSON 存盘并打印一份小结。

    输入：命令行参数
    输出：无返回值，副作用是往 data/ 写一个 JSON 文件
    """
    parser = argparse.ArgumentParser(description="跑一次 HarvestAPI 采集并存下原始数据")
    parser.add_argument("queries", nargs="+", help="要跑的 query，一条一个参数")
    parser.add_argument(
        "--max-posts",
        type=int,
        default=40,
        help="每条 query 的上限，不是总量。总量约等于 query 条数乘这个值",
    )
    parser.add_argument("--posted-limit", default="month", help="时间范围：24h / week / month")
    args = parser.parse_args()

    print(f"[harvest] {len(args.queries)} 条 query，每条最多 {args.max_posts} 条")
    for i, query in enumerate(args.queries, 1):
        print(f"  {i}. {query}")

    # 采集。跑失败会直接抛异常，不会返回空列表冒充成功。
    # signals 是翻译好的信号，billed 是计费口径的 item 数（含评论），
    # raw_items 是供应商原样返回的那份 dataset（存盘用的就是它）。
    signals, billed, raw_items = harvestapi_posts.from_apify(
        args.queries,
        max_posts_per_query=args.max_posts,
        posted_limit=args.posted_limit,
    )

    # 存原始 dataset，一条没删没改。

    #
    # 以前这里是拿 signal.raw 反推 dataset 的，还为此 assert「计费数必须
    # 等于 post 数」—— 因为反推只拿得到 post，开了评论抓取就会漏掉评论
    # 那一半。现在 from_apify 直接把原始 items 还回来，那个将就就不需要了。
    stamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
    out_path = DATA_DIR / f"harvest_{stamp}.json.gz"
    harvestapi_posts.write_dataset(out_path, raw_items)

    print()
    print(f"[harvest] 计费 item {billed} 条，存了 {len(raw_items)} 条原始 item")
    print(f"[harvest] 其中 post {len(signals)} 条（两个数不等说明 dataset 里混着评论）")
    print(f"[harvest] 落盘 {out_path}")


if __name__ == "__main__":
    main()
