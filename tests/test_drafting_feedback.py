"""classify_feedback 那一步的测试：模型拆好的判决怎么落到账本上、拼出 plan_feedback、推出下一步。

不调模型：apply_feedback 是纯函数，喂的是拆好的 FeedbackJudgement；parse_feedback 喂 JSON；
read_feedback 用照剧本回答的假 client。

焊住的几类：

    动计划的话进了账本     「改成私信」被当成正文意见交给写稿的模型，写出一封还是邮件、正文里写着「私信」的信
    去向推错              有一份要改计划却直接去写稿，那一份的计划没人改
    判了没表态的           没点名的那份被当成定稿，再也不会给他看
    已不要的复活成旧号     旧号的计划已经不在这一版里，复活了写不出来
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from triggers.interactive_discovery.drafting.feedback import (
    DECISIONS,
    FeedbackCallFailed,
    FeedbackConfig,
    FeedbackJudgement,
    SlotVerdict,
    apply_feedback,
    build_feedback_messages,
    load_feedback_config,
    parse_feedback,
    read_feedback,
)


def _slot(number: int, form: str = "email", recipient: dict | None = None) -> dict:
    """计划里的一份，只填这里用得到的几格。"""
    return {"number": number, "plan_version": 1, "form": form, "recipient": recipient, "address": None,
            "emphasis": [], "selling_points": ["moq"], "opening": 0, "sources": [number - 1]}  # fmt: skip


def _entry(status: str, version: int = 1, feedback: str = "", number: int = 1) -> dict:
    return {
        "status": status,
        "version": version,
        "plan_version": 1,
        "text": f"正文 v{version}",
        "feedback": feedback,
        "problems": [],
        "slot": _slot(number),
    }


def _plan(drafts: dict) -> dict:
    """账本里没不要的那几份，各一份计划。"""
    return {"slots": [_slot(int(k)) for k in sorted(drafts, key=int) if drafts[k]["status"] != "dropped"]}


def _apply(drafts: dict, judgement: FeedbackJudgement, limit: int = 4):
    return apply_feedback(drafts, _plan(drafts), judgement, limit=limit)


def _judged(*slots: SlotVerdict, add=(), cancel=False, round_plan="", round_content="") -> FeedbackJudgement:
    return FeedbackJudgement(
        cancel=cancel,
        slots=slots,
        add=tuple(add),
        round_plan_change=round_plan,
        round_content_change=round_content,
        reason="测试",
    )


# 第一次看稿时的账本：四份都写好了在等他说（replan_rewrite_1a.txt 第 3 步末尾）
FOUR_WRITTEN = {str(n): _entry("written") for n in range(1, 5)}


def test_the_five_things_in_one_reply():
    """「第 1 份可以了。第 2 份不要了。第 3 份改成给 Dana 发私信。第 4 份短一点，别提众筹。再给 1-2 发一封。」

    动计划的（#3、新加的）拼进 plan_feedback，不进账本；动正文的（#4）进 #4 的 feedback；去向 replan
    """
    judgement = _judged(
        SlotVerdict(1, "approve", "", ""),
        SlotVerdict(2, "drop", "", ""),
        SlotVerdict(3, "change", "改成给 Dana 发 LinkedIn 私信", ""),
        SlotVerdict(4, "change", "", "短一点，别提众筹"),
        add=("再给 1-2 那个邮箱发一封",),
    )

    applied = _apply(FOUR_WRITTEN, judgement)

    assert {k: (e["status"], e["feedback"]) for k, e in applied.drafts.items()} == {
        "1": ("approved", ""),
        "2": ("dropped", ""),
        "3": ("replan", ""),
        "4": ("rewrite", "短一点，别提众筹"),
    }
    assert applied.plan_feedback == "第 3 份：改成给 Dana 发 LinkedIn 私信\n新加一份：再给 1-2 那个邮箱发一封"
    assert applied.verdicts == [
        {"number": 1, "verdict": "approved", "feedback": ""},
        {"number": 2, "verdict": "dropped", "feedback": ""},
        {"number": 3, "verdict": "replan", "feedback": "改成给 Dana 发 LinkedIn 私信"},
        {"number": 4, "verdict": "rewrite", "feedback": "短一点，别提众筹"},
    ]
    assert applied.decision == "replan"
    # 传进来的那份没被改
    assert FOUR_WRITTEN["1"]["status"] == "written"


def test_a_slot_with_both_halves_keeps_the_content_half_for_writing():
    """「第 3 份改成私信，语气随意点」：判 replan，「改成私信」进 plan_feedback，
    「语气随意点」留在账本等写稿时用。
    """
    applied = _apply(FOUR_WRITTEN, _judged(SlotVerdict(3, "change", "改成私信", "语气随意点")))

    assert (applied.drafts["3"]["status"], applied.drafts["3"]["feedback"]) == ("replan", "语气随意点")
    assert applied.plan_feedback == "第 3 份：改成私信"
    assert applied.verdicts == [{"number": 3, "verdict": "replan", "feedback": "改成私信；语气随意点"}]


def test_only_content_changes_go_straight_to_writing():
    """「第 4 份还是太长。其他都行」：没有要改计划的，去向 rewrite；plan_feedback 是空的。"""
    drafts = {
        "1": _entry("approved"),
        "3": _entry("written", 2),
        "4": _entry("written", 2),
        "5": _entry("written"),
    }

    applied = _apply(
        drafts,
        _judged(
            SlotVerdict(3, "approve", "", ""),
            SlotVerdict(4, "change", "", "还是太长，压到三句"),
            SlotVerdict(5, "approve", "", ""),
        ),
    )

    assert applied.decision == "rewrite"
    assert applied.plan_feedback == ""
    assert [v["number"] for v in applied.verdicts] == [3, 4, 5]


def test_unmentioned_drafts_keep_waiting():
    """只说了「第 3 份可以了」：第 4、5 份没表态，还是 written；没有要写的，去向 wait（回去再问一次）。"""
    drafts = {"3": _entry("written"), "4": _entry("written"), "5": _entry("written")}

    applied = _apply(drafts, _judged(SlotVerdict(3, "approve", "", "")))

    assert [applied.drafts[k]["status"] for k in ("3", "4", "5")] == ["approved", "written", "written"]
    assert applied.decision == "wait"


def test_all_approved_is_done():
    """「都可以了」（模型展开成在等的每一份 approve）：每份都定稿或不要了，去向 done。"""
    drafts = {"1": _entry("written", 2), "2": _entry("dropped"), "4": _entry("written", 3)}

    applied = _apply(drafts, _judged(SlotVerdict(1, "approve", "", ""), SlotVerdict(4, "approve", "", "")))

    assert applied.decision == "done"
    assert [v["verdict"] for v in applied.verdicts] == ["approved", "approved"]


def test_an_approved_draft_can_be_reopened():
    """「第 1 份也加一句固件可以定制」：已定稿的改回 rewrite，库里记一行 rewrite。"""
    drafts = {"1": _entry("approved"), "4": _entry("written", 3)}

    applied = _apply(drafts, _judged(SlotVerdict(1, "change", "", "加一句固件可以定制")))

    assert (applied.drafts["1"]["status"], applied.drafts["1"]["feedback"]) == (
        "rewrite",
        "加一句固件可以定制",
    )
    assert applied.drafts["4"]["status"] == "written"
    assert applied.decision == "rewrite"
    assert applied.verdicts == [{"number": 1, "verdict": "rewrite", "feedback": "加一句固件可以定制"}]


def test_approving_an_approved_draft_again_records_nothing():
    """已定稿的又说了一次「可以」：不动，库里不多一行。"""
    applied = _apply({"1": _entry("approved")}, _judged(SlotVerdict(1, "approve", "", "")))

    assert applied.verdicts == []
    assert applied.decision == "done"


def test_a_dropped_draft_comes_back_with_its_number():
    """「第 2 份还是要吧」（两半都空）：原号复活，账本里存的那版计划放回计划，正文 v1 原样再给他看一遍。"""
    drafts = {"1": _entry("approved"), "2": _entry("dropped", number=2) | {"text": "给 Sam 的附言 v1"}}
    plan = {"slots": [_slot(1)]}

    applied = apply_feedback(drafts, plan, _judged(SlotVerdict(2, "change", "", "")), limit=4)

    assert applied.drafts["2"]["status"] == "written"
    assert [s["number"] for s in applied.plan["slots"]] == [1, 2]
    assert applied.plan["slots"][1]["sources"] == []
    assert applied.written_now == [2]
    assert applied.decision == "wait"
    assert applied.verdicts == []
    assert plan["slots"] == [_slot(1)]


@pytest.mark.parametrize(
    ("verdict", "status", "row"),
    [
        (SlotVerdict(2, "approve", "", ""), "approved", ("approved", "")),
        (SlotVerdict(2, "change", "", "短一点"), "rewrite", ("rewrite", "短一点")),
        (SlotVerdict(2, "change", "改成私信", ""), "replan", ("replan", "改成私信")),
    ],
)
def test_a_dropped_draft_coming_back_with_what_he_said(verdict, status, row):
    """要回来时说了什么：就用原来那版 -> 定稿；带正文意见 -> 在原稿上改；带计划改动 -> 回去改计划。"""
    drafts = {"1": _entry("approved"), "2": _entry("dropped", number=2)}

    applied = apply_feedback(drafts, {"slots": [_slot(1)]}, _judged(verdict), limit=4)

    assert applied.drafts["2"]["status"] == status
    assert (applied.verdicts[0]["verdict"], applied.verdicts[0]["feedback"]) == row
    assert [s["number"] for s in applied.plan["slots"]] == [1, 2]


def test_a_draft_dropped_before_it_was_written_comes_back_to_be_written():
    """计划阶段就不要了、一个字没写过的那份要回来：照计划从头写（planned），没有正文，库里不记。"""
    drafts = {"1": _entry("written"), "2": _entry("dropped", version=0, number=2)}

    applied = apply_feedback(
        drafts, {"slots": [_slot(1)]}, _judged(SlotVerdict(2, "change", "", "")), limit=4
    )

    assert applied.drafts["2"]["status"] == "planned"
    assert applied.decision == "rewrite"
    assert applied.verdicts == []


def test_no_room_to_bring_one_back():
    """活着的已经满 4 份：不复活，下一条消息开头告诉他先说不要哪一份。同一句里先不要了一份就有位置。"""
    drafts = {str(n): _entry("written", number=n) for n in (1, 3, 4, 5)} | {"2": _entry("dropped", number=2)}

    full = _apply(drafts, _judged(SlotVerdict(2, "change", "", "")))
    made_room = _apply(drafts, _judged(SlotVerdict(2, "change", "", ""), SlotVerdict(5, "drop", "", "")))

    assert full.drafts["2"]["status"] == "dropped"
    assert full.notice == "第 2 份要回来的话，一轮最多 4 份（已定稿的也算），先说不要哪一份"
    assert made_room.drafts["2"]["status"] == "written"
    assert made_room.notice == ""


def test_round_content_changes_reach_every_draft_not_yet_final():
    """「都随意点」：抄进每份没定稿、没不要的；在等的那几份跟着变 rewrite，库里各记一行。"""
    drafts = {
        "1": _entry("approved"),
        "2": _entry("dropped"),
        "3": _entry("written"),
        "4": _entry("written"),
    }

    applied = _apply(drafts, _judged(SlotVerdict(4, "change", "", "短一点"), round_content="都随意点"))

    assert {k: (e["status"], e["feedback"]) for k, e in applied.drafts.items()} == {
        "1": ("approved", ""),
        "2": ("dropped", ""),
        "3": ("rewrite", "都随意点"),
        "4": ("rewrite", "短一点；都随意点"),
    }
    assert applied.verdicts == [
        {"number": 3, "verdict": "rewrite", "feedback": "都随意点"},
        {"number": 4, "verdict": "rewrite", "feedback": "短一点；都随意点"},
    ]


def test_round_plan_changes_go_to_the_plan():
    """「都改成中文」：管整轮的计划层面的话，进 plan_feedback 一行「整轮：…」，去向 replan。"""
    applied = _apply(FOUR_WRITTEN, _judged(round_plan="都改成中文"))

    assert applied.plan_feedback == "整轮：都改成中文"
    assert applied.decision == "replan"


def test_cancel_records_the_waiting_drafts_and_nothing_else():
    """「算了」：账本不动，还在等的那几份各记一行 cancelled，去向 cancel。"""
    drafts = {"1": _entry("approved"), "3": _entry("written", 2), "4": _entry("written")}

    applied = _apply(drafts, _judged(cancel=True))

    assert applied.decision == "cancel"
    assert applied.drafts == drafts
    assert applied.verdicts == [
        {"number": 3, "verdict": "cancelled", "feedback": ""},
        {"number": 4, "verdict": "cancelled", "feedback": ""},
    ]


def test_parse_refuses_numbers_not_in_the_ledger_and_repeats():
    """判了账本里没有的号、同一份判两次：当场报错。"""
    good = {
        "cancel": False,
        "slots": [],
        "add": [],
        "round_plan_change": "",
        "round_content_change": "",
        "reason": "",
    }

    def item(number, verdict="approve"):
        return {"number": number, "verdict": verdict, "plan_change": "", "content_change": ""}

    with pytest.raises(FeedbackCallFailed, match="账本里只有"):
        parse_feedback(good | {"slots": [item(9)]}, FOUR_WRITTEN)
    with pytest.raises(FeedbackCallFailed, match="同一份判了两次"):
        parse_feedback(good | {"slots": [item(1), item(1, "drop")]}, FOUR_WRITTEN)
    with pytest.raises(FeedbackCallFailed, match="少了字段"):
        parse_feedback({"cancel": False}, FOUR_WRITTEN)
    parsed = parse_feedback(good | {"slots": [item(1)], "add": [" 再加一篇 ", " "]}, FOUR_WRITTEN)
    assert parsed.slots == (SlotVerdict(1, "approve", "", ""),)
    assert parsed.add == ("再加一篇",)


def test_the_messages_list_every_draft_without_its_text():
    """给模型的那份 JSON：每一份的编号、写哪一种、写给谁、到了哪一步，不带正文；waiting 是还在等的号。"""
    plan = {
        "slots": [
            {"number": 1, "form": "email", "recipient": None},
            {"number": 3, "form": "linkedin_message",
             "recipient": {"name": "Dana Lee", "relation": "member", "position": "Founder"}},
        ]
    }  # fmt: skip
    sam = {"name": "Sam Park", "relation": "not_member", "position": None}
    drafts = {
        "1": _entry("approved"),
        "2": _entry("dropped") | {"slot": _slot(2, "linkedin_note", sam)},
        "3": _entry("written", 2),
    }
    lead = {"display_name": "Acme Audio"}

    sent = build_feedback_messages(
        ["用户: 第 3 份可以了"], feedback="第 3 份可以了", drafts=drafts, plan=plan, lead=lead
    )
    payload = json.loads(sent[1]["content"])

    assert payload["drafts"] == [
        {"number": 1, "form": "邮件", "to": "Acme Audio 这家公司本身（称呼他们的团队）",
         "status": "已定稿", "version": 1},
        {"number": 2, "form": "LinkedIn 加好友附言",
         "to": "Sam Park（发帖的人不是这家公司的，只是在帖子里提到了它，仍可联络）",
         "status": "已不要", "version": 1},
        {"number": 3, "form": "LinkedIn 私信", "to": "Dana Lee（Founder，这家公司的人）",
         "status": "写好了在等他说", "version": 2},
    ]  # fmt: skip
    assert payload["waiting"] == [3]
    assert "正文" not in sent[1]["content"]


def test_read_feedback_parses_what_the_model_said():
    """read_feedback 走一遍：照剧本回答的 client，拆出来的就是那份 JSON。"""
    answer = {
        "cancel": False,
        "slots": [{"number": 2, "verdict": "drop", "plan_change": "", "content_change": ""}],
        "add": [],
        "round_plan_change": "",
        "round_content_change": "",
        "reason": "他说第 2 份不要了",
    }
    message = SimpleNamespace(content=json.dumps(answer, ensure_ascii=False), refusal=None)
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=message)])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    config = FeedbackConfig(model="gpt-4.1", max_tokens=800, temperature=0.0, request_timeout_seconds=30.0)

    got = read_feedback(
        client, ["用户: 第 2 份不要了"], feedback="第 2 份不要了", drafts=FOUR_WRITTEN, plan={"slots": []},
        lead={"display_name": "Acme Audio"}, config=config,
    )  # fmt: skip

    assert got.slots == (SlotVerdict(2, "drop", "", ""),)


def test_the_repo_config_loads_and_decisions_are_five():
    """仓库里那份配置读得出来；五个去向就是那五个，顺序是推的顺序。"""
    assert load_feedback_config().model
    assert DECISIONS == ("cancel", "replan", "rewrite", "wait", "done")
