"""找公司那张图交给 runner 的那一份：存档钥匙、在哪儿开、第一份 state、怎么拼回复、哪几种崩溃要退回。

并发、开连接、跑一轮图、通用的崩溃收拾都在 triggers/interactive_discovery/runner.py，几张图共用。
这里只放 query 这张图自己知道的事，最后由 query_graph() 打包成一个 ConversationGraph：

    name              "query"
    thread_prefix     "slack"                          存档钥匙 "slack:C0000000001:1790126036.992409"
    starts_in         "channel"                        频道里 @bot 开的对话归它
    build             graph.build_graph                给一个存档器、编好 query 这张图
    first_input       _first_input                     ("帮我找骨传导耳机", None) -> initial_state(那句话)
    before_interrupt  _BEFORE_INTERRUPT                await_go -> plan_queries，
                                                       ask_clarification -> understand_intent
    render            _reply_for                       方案卡片 / 一句提问 / 采完了几家 / 不跑了
                                                       （怎么拼在 messages.py）
    finished_text     messages.FINISHED_TEXT           「这次对话已经结束了。要再找一批，…」
    on_crash          _harvest_crash                   run_harvest 当场崩溃，退回 await_go
    unfinished_retry  _harvest_left_unfinished         run_harvest 崩溃留到了下一句，也退回 await_go

生产怎么用（triggers/slack_buttons.build_app）：
    ConversationRunner([query_graph(), drafting_graph()], root_card=root_card)
测试怎么用（tests/test_conversation.py）：
    同上，另外传 saver_opener=<交出 InMemorySaver 的替身>
"""

from __future__ import annotations

from typing import Any

from triggers.harvest import DiscoveryBusy
from triggers.interactive_discovery.budget import MonthlyCapReached
from triggers.interactive_discovery.query import messages
from triggers.interactive_discovery.query.graph import build_graph
from triggers.interactive_discovery.query.state import initial_state
from triggers.interactive_discovery.runner import ConversationGraph


def _first_input(text: str, lead_id: int | None) -> dict:
    """START 时交给 invoke 的第一份 state。

    输入 -> 输出：("帮我找骨传导耳机", None)
               -> {"messages": ["用户: 帮我找骨传导耳机"], "intent_result": {}, "clarify_rounds": 0, ...}
    lead_id 用不上：找公司的对话在频道里开，thread 开头不是线索卡，runner 传进来的永远是 None。
    每个字段为什么都要给上，见 query/state.initial_state
    """
    return initial_state(text)


# interrupt 节点 -> 它前面那个节点。runner._back_to 退回某个 interrupt 时，
# update_state 的 as_node 填的就是这个前面的节点（理由写在 runner._back_to）。
# 图上加了新的 interrupt 节点，这里要跟着加一行，不然退回那一刻 KeyError
_BEFORE_INTERRUPT = {
    "await_go": "plan_queries",
    "ask_clarification": "understand_intent",
}


def _harvest_crash(snapshot: Any, err: Exception) -> tuple[str, str] | None:
    """一轮 invoke 刚崩溃时：是不是 run_harvest 崩的，是就退回 await_go，给出说明。runner._recover 情况一。

    输入：
        snapshot   崩溃之后 app.get_state(config) 的返回值
        err        抛出来的那个异常
    输出：("await_go", 说明)，或者 None（不是 run_harvest 崩的，交给 runner 通用的两种）

        next=('run_harvest',)  err=DiscoveryBusy(...)
            -> ("await_go", "没跑成（DiscoveryBusy: 已经有一跑 discovery 在跑，这次没开始），一分钱没花。
                            方案还在，下面是同一版，再说一次「跑」就重新采。")
        next=('run_harvest',)  err=MonthlyCapReached(...)
            -> ("await_go", "没跑成：本账期（2026-09-18 起）对话采集和找联系方式已经花了 $12.95，…一分钱没花。
                            方案还在，下面是同一版；回「算了」结束这次对话，…")
        next=('run_harvest',)  err=RuntimeError("Neon 掐了连接")
            -> ("await_go", "没跑成（RuntimeError: Neon 掐了连接），可能已经花了钱，
                            要到 runs 表和 Apify 后台核对。…")
        next=('understand_intent',)
            -> None

    next == ('run_harvest',) 说的是什么：第一版方案一出来，用户就回「跑」。之后要连着跑三个节点，
    每跑完一个落一份存档（step 是这个例子里的编号）：

        step 3   await_go 停着等拍板             next=('await_go',)
                 用户回「跑」
        step 4   await_go 跑完                   next=('classify_reply',)
        step 5   classify_reply 判了 run         next=('run_harvest',)
                 run_harvest 开跑 -> 崩溃。它的写一笔都没落，不会有 step 6

    所以 next == ('run_harvest',) 说的是「最新那份存档是 step 5」：classify_reply
    已经跑完，run_harvest 被排上了。不是 await_go 刚跑完就出错 —— 那样的话最新那份
    是 step 4，next=('classify_reply',)，跟 classify_reply 自己崩溃一样，归 runner 情况三。

    run_harvest 到底跑了多少、花没花钱，光看存档分不出来：崩在它里面哪一行，存档都是
    step 5 加一笔 __error__，state 里 run_id 都是 None。只能看是什么异常：
        MonthlyCapReached  run_harvest 开连接之后第一件事查月度上限，超了就抛，
                           Apify 没调，库里一个字没写，一分钱没花。回话里说清已花多少、
                           什么时候恢复，不说「再说一次跑」：再说一次还是会被拒
        DiscoveryBusy   run_harvest 拿 advisory lock 没拿到，Apify 没调，
                        库里一个字没写，一分钱没花
        别的异常        可能已经调过 Apify，钱花了。库里可能整跑回滚了，也可能 runs
                        那行已经提交了，要到 runs 表和 Apify 后台核对
    退回 await_go 之后，下一句「跑」就重新走 classify_reply -> run_harvest，「算了」就结束。
    """
    if tuple(snapshot.next) != ("run_harvest",):
        return None

    if isinstance(err, MonthlyCapReached):
        # 撞了月度上限：这一句不能说「再说一次跑就重新采」，再说一次还是会被拒。
        # 输出   "没跑成：本账期（2026-09-18 起）对话采集和找联系方式已经花了 $12.95，这次报价
        #         $0.15，加起来会超过上限 $13.00。2026-10-18 开始的下一个账期恢复。一分钱没花。…"
        #        方案照样退回 await_go 留在原地：用户可以回「算了」结束，
        #        或者等下个账期再来说「跑」
        return (
            "await_go",
            f"没跑成：{err}一分钱没花。方案还在，下面是同一版；回「算了」结束这次对话，"
            "要调上限改 config/interactive.yaml 的 budget.monthly_cap_usd。",
        )

    reason = f"{type(err).__name__}: {err}"
    if isinstance(err, DiscoveryBusy):
        spent = "一分钱没花"
    else:
        spent = "可能已经花了钱，要到 runs 表和 Apify 后台核对"
    return (
        "await_go",
        f"没跑成（{reason}），{spent}。方案还在，下面是同一版，再说一次「跑」就重新采。",
    )


def _harvest_left_unfinished(snapshot: Any) -> tuple[str, str] | None:
    """下一句判了 RETRY 时：存档是不是还停在 run_harvest 前面，是就退回 await_go，不许直接重跑。

    输入：snapshot，这一句进来时 app.get_state(config) 的返回值
    输出：
        next=('run_harvest',)   -> ("await_go", "上一次采集没跑完就断了（…），…再说一次「跑」就重新采。")
        next=('deliver',) 等    -> None，runner 照常 invoke(None) 重跑

    正常崩溃时 _harvest_crash 当场就退回了，存档留不到下一句。留到了这里，说明上一轮
    runner._recover 没机会跑（例：用户说「跑」，采到一半进程被重启），或者它退回的时候自己又出了错。
    下一句「算了」要是照常 invoke(None)，run_harvest 会直接重跑、又花一次钱。
    说明里不说「一分钱没花」：被杀的时候可能已经在采了
    """
    if tuple(snapshot.next) != ("run_harvest",):
        return None
    return (
        "await_go",
        "上一次采集没跑完就断了（进程中断，或者上次收拾的时候又出了错），"
        "那一次花没花钱要到 runs 表和 Apify 后台核对。方案还在，下面是同一版，"
        "再说一次「跑」就重新采。",
    )


def _reply_for(result: dict) -> dict:
    """一轮 invoke 正常结束之后，发回 thread 的那条消息。

    输入：result，是app.invoke 的返回值，即为这一轮跑完之后的整份 state，
          停在 interrupt 上时多一个 "__interrupt__" 键：
              停在 await_go   {"messages": [...], "plan": {...}, ...,
                               "__interrupt__": (Interrupt(value={"kind": "go", ...}),)}
              走到 END        {"messages": [..., "AI: 采完了。run 31，..."], "decision": "run", ...}
    输出：chat.postMessage 的参数字典
        停住了   render_interrupt 拼的（一句提问，或者一张方案卡片）
        走完了   render_ended 拼的（采完了几家，或者「好，这次不跑了」）

    __interrupt__ 这个元组同一时刻最多一个元素：图上两个 interrupt 分属两个节点，
    图一次只会停在其中一个上。

    谁在调它：runner._invoke（一轮 invoke 正常返回之后），和 runner._back_to（退回某个
    interrupt、invoke(None) 之后，那时一定是停在那个 interrupt 上）。

    invoke 正常返回只有两种情况：
    一、有 __interrupt__，停在两个 interrupt 之一上
    1.停在 ask_clarification
        有 __interrupt__，value["kind"] == "clarify"
            -> render_interrupt -> text_reply(value["question"])
            -> {"text": "你想找做哪一类设备的公司？"}
    2.停在 await_go
        有 __interrupt__，value["kind"] == "go"
            -> render_interrupt -> plan_message(value["plan"])
            -> {"attachments": [{"color": PLAN_COLOR, ...}]}
    二、没有 __interrupt__，图走到了 END，也有两种
    1.用户说了「算了」：await_go 把这句交给 classify_reply，classify_reply 判 cancel，
      条件边查到 cancel 对应 END，图就结束了（await_go 自己不会直接走到 END）
        没有 __interrupt__，result["decision"] == "cancel"
            -> render_ended -> text_reply(CANCELLED_TEXT)
            -> {"text": "好，这次不跑了。要重新找，在频道里再 @ 我。"}
    2.用户说了「跑」：await_go -> classify_reply 判 run -> run_harvest 采完 -> find_contacts 找联系方式
      -> deliver 推卡 -> END
        没有 __interrupt__，result["decision"] == "run"
            -> render_ended -> messages 最后一行（deliver 写的那一句）去掉「AI: 」
            -> {"text": "分析了 12 家，3 家过线，推了 3 张到频道：https://..."}
    """
    interrupts = result.get("__interrupt__") or ()
    if interrupts:
        return messages.render_interrupt(interrupts[0].value)
    return messages.render_ended(result)


def query_graph() -> ConversationGraph:
    """把上面几样打包成 runner 认的那一份。

    输入：无
    输出：ConversationGraph(name="query", thread_prefix="slack", starts_in="channel", build=build_graph, ...)
    """
    return ConversationGraph(
        name="query",
        thread_prefix="slack",
        starts_in="channel",
        build=build_graph,
        first_input=_first_input,
        before_interrupt=_BEFORE_INTERRUPT,
        render=_reply_for,
        finished_text=messages.FINISHED_TEXT,
        # 一个 thread 一次采集：走到 END 之后再 @ 也不重开，要再找一批去频道里重新 @
        restart=None,
        on_crash=_harvest_crash,
        unfinished_retry=_harvest_left_unfinished,
    )
