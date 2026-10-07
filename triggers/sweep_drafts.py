"""定时扫起草对话：停在看稿或看计划那一停太久没人回，还在等他表态的稿记成 abandoned。

    python -m triggers.sweep_drafts           看一眼要写哪几行，不写
    python -m triggers.sweep_drafts --apply   真写进 draft_revisions

默认只看不写，同 scripts/migrate.py：这个入口写的是生产库。
天数在 config/drafting.yaml 的 sweep.abandon_after_days。只写库，不改存档：对话照样停在那一停，
他哪天回了话还能接着走。每天跑一次就够；同一天跑两次不会重复写（唯一键挡着）。
部署时跟 lead_feedback 的 ignored 扫描一起挂定时任务。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

import yaml

from persistence import db, draft_db
from triggers.interactive_discovery.drafting.graph import build_graph
from triggers.interactive_discovery.drafting.revisions import sweep
from triggers.interactive_discovery.runner import open_saver

# 往上两层到仓库根：triggers/ -> 仓库根
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "drafting.yaml"


def main() -> int:
    """扫一遍，打印要写的行；带 --apply 才写库。返回进程退出码。"""
    apply = "--apply" in sys.argv
    after_days = int(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["sweep"]["abandon_after_days"])
    now = dt.datetime.now(dt.UTC)

    # 所有起草对话的存档钥匙，例 "draft:C0000000001:1790573599.103039"
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("select distinct thread_id from checkpoints where thread_id like 'draft:%'")
        thread_ids = [row[0] for row in cur.fetchall()]

    with open_saver() as saver:
        rows = sweep(build_graph(saver), thread_ids, now=now, after_days=after_days)

    print(f"看了 {len(thread_ids)} 条起草对话，超过 {after_days} 天没回、要记 abandoned 的 {len(rows)} 份：")
    for row in rows:
        print(f"    {row.thread_id} 第 {row.slot_number} 份第 {row.draft_version} 版")
    if not apply:
        print("只看不写。要写加 --apply")
        return 0
    with db.connect() as conn, conn.cursor() as cur:
        written = sum(draft_db.insert_revision(cur, row) for row in rows)
    print(f"写了 {written} 行（其余之前扫过、已经写过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
