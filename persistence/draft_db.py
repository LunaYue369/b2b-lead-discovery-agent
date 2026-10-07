"""draft_revisions 那张表的读写（0012）。只有起草图的 record_round 写它。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg


@dataclass(frozen=True)
class DraftRevision:
    """一行：用户某句话对某一份某一版下的判决。

    verdict 取 approved / rewrite / replan / dropped / cancelled / abandoned（0013 加的：太久没人回）

    例：DraftRevision(thread_id="draft:C0000000001:1759200000.123456", lead_id=1300, reply_index=7,
                      slot_number=3, draft_version=1, plan_version=2, plan_slot={...}, form="email",
                      recipient_name=None, address="sales@acme-audio.example", text="Subject: ...",
                      verdict="replan", feedback="改成给 Dana 发 LinkedIn 私信",
                      user_reply="第 1 份可以了。……")
    """

    thread_id: str
    lead_id: int
    reply_index: int
    slot_number: int
    draft_version: int
    plan_version: int
    plan_slot: dict[str, Any]
    form: str
    recipient_name: str | None
    address: str | None
    text: str
    verdict: str
    feedback: str
    user_reply: str


def insert_revision(cur: psycopg.Cursor, row: DraftRevision) -> bool:
    """写一行，返回这次是不是新写的。同一句话对同一份已经写过（record_round 崩了重跑）就跳过，返回 False。"""
    cur.execute(
        """
        insert into draft_revisions
            (thread_id, lead_id, reply_index, slot_number, draft_version, plan_version, plan_slot,
             form, recipient_name, address, text, verdict, feedback, user_reply)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (thread_id, reply_index, slot_number) do nothing
        """,
        (
            row.thread_id,
            row.lead_id,
            row.reply_index,
            row.slot_number,
            row.draft_version,
            row.plan_version,
            json.dumps(row.plan_slot, ensure_ascii=False),
            row.form,
            row.recipient_name,
            row.address,
            row.text,
            row.verdict,
            row.feedback,
            row.user_reply,
        ),
    )
    return cur.rowcount == 1
