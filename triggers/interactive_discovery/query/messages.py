"""找公司那张图发进 thread 的话：方案卡片、采集窗口那一句、走到 END 时的那句。全是纯函数。

跟 delivery/conversation.py 的分工：那边是两张图共用的（读懂一条消息、定下怎么处理、编辑 / 占线那几句），
这里只放 query 这张图自己的。谁在用：

    query/slack_conversation._reply_for   render_interrupt / render_ended，一轮 invoke 跑完拼回复
    query/slack_conversation.query_graph  FINISHED_TEXT，对话已经结束之后再说话回的那一句
    query/nodes.plan_queries              window_text，写进 messages 的那一行里的采集窗口

发回去的每一条都是 chat.postMessage 的参数字典，理由写在 delivery/conversation.py 开头：
    一句话       {"text": "你想找做哪一类设备的公司？"}
    方案卡片     {"attachments": [{"color": PLAN_COLOR, "fallback": ..., "blocks": [...]}]}
"""

from __future__ import annotations

from delivery.cards import OUTER_RULE
from delivery.conversation import text_reply
from models import POSTED_WITHIN_TEXT

FINISHED_TEXT = "这次对话已经结束了。要再找一批，请在频道里重新 @ 我，会开一个新的对话。"
CANCELLED_TEXT = "好，这次不跑了。要重新找，在频道里再 @ 我。"

# 方案卡片左侧竖条的颜色：紫色。另外三种已经被早报用掉了（汇总灰蓝 #4a6785、
# A 档绿 #2eb886、B 档橙 #e8912d，见 delivery/cards.py），方案卡片说的是另一件事
# ——「这是一版等你拍板的方案」，不能跟其中任何一种长得一样。
PLAN_COLOR = "#7b5ea7"
PLAN_MARK = ":memo:"

# Slack 一个 section 的 text 最多 3000 个字符，超了整条消息被拒收（invalid_blocks），
# 用户那边就一张卡片都看不到。留一点余量给小标题。
_SECTION_LIMIT = 2900


def render_interrupt(payload: dict) -> dict:
    """图停在 interrupt 上时，交出来的那个字典写成发回 thread 的一条消息。

    输入：payload，也就是 result["__interrupt__"][0].value。两种：

        ask_clarification 交出来的
            {"kind": "clarify", "question": "你想找做哪一类设备的公司？"}

        await_go 交出来的
            {"kind": "go", "reply_hint": "回一句话",
             "plan": {"version": 1,
                      "queries": [{"text": "Kickstarter AND bone conduction",
                                   "filters": {"authorKeywords": "Founder OR CTO"}}],
                      "assumptions": "我理解的需求：…\\n要搜的词：bone conduction\\n…",
                      "estimated_cost_usd": 0.08,
                      "posted_since": "2026-09-21", "posted_limit": None,
                      "time_said": "最近一周", "time_understood": True},
             "estimated_cost_usd": 0.08}

    输出：
        clarify   {"text": "你想找做哪一类设备的公司？"}   对话里的一句话，不做成卡片
        go        plan_message 拼的方案卡片
    """
    if payload.get("kind") == "clarify":
        return text_reply(payload["question"])
    return plan_message(payload["plan"])


def _usd(amount: float) -> str:
    """钱数写成两位小数。例：0.08 -> "$0.08"，0.1 -> "$0.10"，0.0 -> "$0.00" """
    return f"${amount:.2f}"


def _chunks(text: str, limit: int = _SECTION_LIMIT) -> list[str]:
    """一段文字按行切成几段，每段不超过 limit 个字符。

    输入：例 "我理解的需求：…\\n要搜的词：…\\n丢掉了 12 条：…"，limit=2900
    输出：例 ["我理解的需求：…\\n要搜的词：…"]（不长就是原样一段）

    按行切不按字数硬切：清单一行是一项，切在一行中间，那一项就被拆到两块里。
    一行本身就超过 limit 的（几乎不会有），只好在 limit 处硬切，不然 Slack 拒收整条。
    """
    out: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            out.append(current)
            current = line
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _window_parts(plan: dict) -> tuple[str, str]:
    """方案里的采集窗口拆成两截：要用的窗口，和括号里那句说明。

    输入：plan 里 plan_queries 放的四格（posted_since / posted_limit / time_said / time_understood，
          意思见 time_window.PostWindow）
    输出：(窗口, 说明)，四种：

        他说了、换算得出      ('2026-07-28 之后的帖子', '你说的是「最近两个月」')
                             小时级的是 ('2026-09-28 05:00 UTC 之后的帖子', '你说的是「最近 6 小时」')
        他说了不限时间        ('不限时间', '你说的是「不限时间」')
        他没说                ('最近一个月', 'month，你没说时间，按默认')
        他说了、换算不了      ('最近一个月', 'month，你说的「今年上半年」换算不了，按默认')

    原话原样放在说明里：日期是代码从他的原话换算的，把原话摆在旁边，他一眼就能核对换算对不对。
    默认那一档的原值（month）也留着：它是真传给采集那一步的那个字
    """
    said = plan["time_said"]
    since = plan["posted_since"]
    if since:
        # 输入   '2026-07-28'                输出   '2026-07-28'
        # 输入   '2026-09-28T05:00:00Z'      输出   '2026-09-28 05:00 UTC'
        shown = since if "T" not in since else since[:16].replace("T", " ") + " UTC"
        return f"{shown} 之后的帖子", f"你说的是「{said}」"
    limit = plan["posted_limit"]
    if plan["time_understood"]:
        return POSTED_WITHIN_TEXT[limit], f"你说的是「{said}」"
    why = f"你说的「{said}」换算不了" if said else "你没说时间"
    return POSTED_WITHIN_TEXT[limit], f"{limit}，{why}，按默认"


def window_text(plan: dict) -> str:
    """采集窗口的纯文字版，写进对话记录那一句用。

    输入 -> 输出：他说了「最近两个月」 -> '2026-07-28 之后的帖子（你说的是「最近两个月」）'
    """
    shown, note = _window_parts(plan)
    return f"{shown}（{note}）"


def _window_field(plan: dict) -> str:
    """方案卡最上面那一栏里的采集窗口：窗口加粗，说明跟在括号里。

    输入 -> 输出：他说了「最近两个月」 -> '*2026-07-28 之后的帖子* （你说的是「最近两个月」）'

    收尾的 * 后面那个空格不能省：Slack 只在 * 后面跟着空格或半角标点时才认它是加粗，
    紧跟一个全角括号的话，两个星号会原样印出来（实测过）

    为什么放在最上面、还要加粗：时间理解错了代价大（有一轮「最近两个月」被当成 24 小时，
    一条帖子都没搜到），而它以前是「前提」清单的最后一行，很容易一眼扫过去就回了「跑」
    """
    shown, note = _window_parts(plan)
    return f"*{shown}* （{note}）"


def plan_message(plan: dict) -> dict:
    """把一版方案做成一张卡片。返回 chat.postMessage 的参数。

    输入：plan，例
        {"version": 1,
         "queries": [{"text": "Kickstarter AND bone conduction",
                      "filters": {"authorKeywords": "Founder OR CTO"}}],
         "assumptions": "我理解的需求：找刚做完众筹的骨传导耳机团队\\n要搜的词：bone conduction",
         "estimated_cost_usd": 0.08,
         "posted_since": "2026-09-21", "posted_limit": None,
         "time_said": "最近一周", "time_understood": True}
    输出：
        {"attachments": [{"color": PLAN_COLOR,
                          "fallback": "方案 v1，1 条 query，预计 $0.08",
                          "blocks": [...]}]}

    跟线索卡同一套排法（见 delivery/cards.lead_message 那段说明）：外面两条重线字符
    画卡片的边界，里面用原生 divider 分区，header 放标题，账目用 fields 排两列。
    颜色换成紫色，理由写在 PLAN_COLOR 那里。

        section   重线，卡片上边界
        header    :memo: 方案 v1
        section   两列：要搜几条 / 1 条，预计花费 / $0.08，
                  只要多新的帖子 / *2026-09-21 之后的帖子* （你说的是「最近一周」）
        divider
        section   :mag: 要去搜的：每条检索式一行，反引号包住
        divider
        section+  :clipboard: 这版方案的前提：plan["assumptions"] 原样照抄，太长就按行分几块
        divider
        section   怎么回：跑 / 修改意见 / 算了
        section   重线，卡片下边界

    清单那几行原样照抄 plan["assumptions"]，这里一个字都不改写：那份是代码拼的、
    逐字可信（planner.build_assumptions），在这里改写就等于在渲染层另编了一份。
    检索式用反引号包住：Slack 会把它渲染成等宽字体，AND / OR 和括号看得清，
    用户也能直接复制去 LinkedIn 上试。

    刻意不加「跑」「算了」两个按钮：按钮会绕过 classify_reply，而修改意见又只能打字，
    同一个拍板就有了两种说法，两条路迟早判得不一样。

    一条 query 都没留下时不列「要去搜的」那一区：清单第一行已经会说
    「一条能用的 query 都没拟出来」。
    """
    queries = plan["queries"]
    cost = _usd(plan["estimated_cost_usd"])

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": OUTER_RULE}},
        {"type": "header", "text": {"type": "plain_text", "text": f"{PLAN_MARK} 方案 v{plan['version']}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*要搜几条*"},
                {"type": "mrkdwn", "text": f"{len(queries)} 条"},
                {"type": "mrkdwn", "text": "*预计花费*"},
                {"type": "mrkdwn", "text": cost},
                {"type": "mrkdwn", "text": "*只要多新的帖子*"},
                {"type": "mrkdwn", "text": _window_field(plan)},
            ],
        },
    ]

    if queries:
        listed = "\n".join(f"`{item['text']}`" for item in queries)
        blocks.append({"type": "divider"})
        for i, part in enumerate(_chunks(listed)):
            head = ":mag: *要去搜的*\n" if i == 0 else ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": head + part}})

    blocks.append({"type": "divider"})
    for i, part in enumerate(_chunks(plan["assumptions"])):
        head = ":clipboard: *这版方案的前提*\n" if i == 0 else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": head + part}})

    how_to_reply = ":point_right: 回「跑」开始采集（这一步花钱）；回修改意见我重拟一版；回「算了」结束。"
    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": how_to_reply}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": OUTER_RULE}})

    # fallback 放在 attachment 里面、顶层不给 text，跟线索卡同一个理由：顶层的 text
    # 会显示在色条上方，把通知栏那句在客户端里再印一遍
    return {
        "attachments": [
            {
                "color": PLAN_COLOR,
                "fallback": f"方案 v{plan['version']}，{len(queries)} 条 query，预计 {cost}",
                "blocks": blocks,
            }
        ]
    }


def render_ended(values: dict) -> dict:
    """图走到 END 时，发回 thread 的那一条消息。

    输入：values，这一轮 invoke 跑完之后的整份 state。走到 END 只有两条路：

        classify_reply 判成 cancel   values["decision"] == "cancel"
        deliver 跑完                 values["decision"] == "run"，
                                     values["messages"][-1] ==
                                         "AI: 分析了 12 家，3 家过线，推了 3 张到频道：https://..."
    输出：
        cancel   {"text": CANCELLED_TEXT}
        run      {"text": "分析了 12 家，3 家过线，推了 3 张到频道：https://..."}

    run 那一句直接用 deliver 写进 messages 的那一行，去掉「AI: 」前缀：那一行
    就是说给用户的，再在这里另写一句，两处迟早说得不一样。
    run_harvest 自己也写了一句「采完了……」，但它后面还有 deliver，最后一句轮不到它
    """
    if values.get("decision") == "cancel":
        return text_reply(CANCELLED_TEXT)
    return text_reply(values["messages"][-1].removeprefix("AI: "))
