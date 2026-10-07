"""draft_revisions 的一行怎么从 state 拼出来，和「太久没人回」的那几份怎么认。

两个地方写 draft_revisions，共用这里拼行：
    nodes.record_round         用户回了一句话，对每份下的判决一行一份
    triggers/sweep_drafts.py   定时扫：停在看稿 / 看计划那一停太久没人回，还在等他表态的那几份记成 abandoned

abandoned 不改存档：对话照样停在那一停，他哪天回了话还能接着往下走，到时候再记一行他的判决。
跟 lead_feedback 的 ignored 同一个用处：沉默也是一种反馈，不记的话学写稿时只看得到他回过的那些稿
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from persistence.draft_db import DraftRevision
from triggers.interactive_discovery.drafting.planner import WRITTEN

# 停在这两个 interrupt 上才算「在等他说话」；停在第一问（ask_request）时还没有稿
_WAITING_NODES = (("await_write",), ("await_plan",))


def revision_row(
    values: Mapping[str, Any],
    thread_id: str,
    *,
    number: int,
    verdict: str,
    feedback: str,
    reply_index: int,
    user_reply: str,
) -> DraftRevision:
    """账本里第 number 份当前那一版、加上判决，拼成 draft_revisions 的一行。

    输入：values（整份 state，用 lead_id / drafts / plan）、thread_id、这一份的号、判决、意见、
          那句话在 messages 里的下标、那句话原话
    输出：DraftRevision，例 DraftRevision(thread_id="draft:C0000000001:1790573599.103039", lead_id=1526,
          reply_index=6, slot_number=3, draft_version=1, plan_version=1, ..., verdict="rewrite",
          feedback="短一点", user_reply="第 1 份可以了。……")
    计划快照取 plan 里那一份；不在计划里了（已经不要了）取账本里存的最后一版
    """
    entry = values["drafts"][str(number)]
    slot = next((s for s in values["plan"]["slots"] if s["number"] == number), None) or entry["slot"]
    return DraftRevision(
        thread_id=thread_id,
        lead_id=values["lead_id"],
        reply_index=reply_index,
        slot_number=number,
        draft_version=entry["version"],
        plan_version=entry["plan_version"],
        plan_slot=slot,
        form=slot["form"],
        recipient_name=slot["recipient"]["name"] if slot["recipient"] is not None else None,
        address=slot["address"],
        text=entry["text"],
        verdict=verdict,
        feedback=feedback,
        user_reply=user_reply,
    )


def abandoned_rows(
    snapshot: Any, thread_id: str, *, now: dt.datetime, after_days: int
) -> list[DraftRevision]:
    """一条起草对话要记的 abandoned 行：停在看稿或看计划那一停超过 after_days 天，还在等他表态的每份一行。

    输入：snapshot（app.get_state 的结果，用 next / created_at / values）、thread_id、现在、天数
    输出：例（停在看稿那一停 9 天，第 2、3 份还在等）
              [DraftRevision(..., reply_index=12, slot_number=2, verdict="abandoned", user_reply=""),
               第 3 份同样]
          没停在那两停、没到天数、没有在等的稿：[]

    reply_index 填 len(messages)：这时候还没有他的回话，填的是「下一句」的位置。他以后真回了话，
    那句的下标在它后面（看稿那一停恢复时先接一句稿子摘要），不会撞唯一键；
    同一天扫两次是同一个 reply_index，撞唯一键、不重复写
    """
    if snapshot.next not in _WAITING_NODES:
        return []
    stopped_at = dt.datetime.fromisoformat(snapshot.created_at)
    if now - stopped_at < dt.timedelta(days=after_days):
        return []
    values = snapshot.values
    return [
        revision_row(
            values,
            thread_id,
            number=int(key),
            verdict="abandoned",
            feedback="",
            reply_index=len(values["messages"]),
            user_reply="",
        )
        for key in sorted(values["drafts"], key=int)
        if values["drafts"][key]["status"] == WRITTEN
    ]


def sweep(app: Any, thread_ids: Iterable[str], *, now: dt.datetime, after_days: int) -> list[DraftRevision]:
    """把这几条起草对话里该记 abandoned 的行都找出来（不写库，写库是调用方的事）。

    输入：app（编好的起草图，用它读存档）、要看的 thread_id、现在、天数
    输出：所有要写的行，按 thread_id、号排
    """
    rows: list[DraftRevision] = []
    for thread_id in sorted(thread_ids):
        snapshot = app.get_state({"configurable": {"thread_id": thread_id}})
        rows.extend(abandoned_rows(snapshot, thread_id, now=now, after_days=after_days))
    return rows
