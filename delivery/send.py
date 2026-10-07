"""把拼好的早报发到 Slack。这一层是唯一真的调 Slack API 的地方。

跟 cards.py 分开：那边只拼字典、不认识网络，所以早报的样子能脱离 token
反复改反复测。这边只管「把拼好的东西送出去、并且说清送成了哪些」，
不做任何关于「该推哪几条」的判断 —— 那个判断在 cards.choose。

也不写库。推成功之后要给 leads 记上 pushed_at 和 pushed_run_id，
但那一步在 persistence 那边、由 trigger 调：这个模块返回「哪几条发成了」，
谁去记账不关它的事。分开的理由跟 record_feedback / advance_to_draft 那次
一样 —— 两件事的失败含义不同，发消息失败可以重试，而记账写进去就不该
回退。

一跑发 1 + N 条消息：先一条汇总，再每条线索一条。顺序是这样定的因为
Slack 里新消息在下面，先发汇总的话读起来就是「总账在上、卡片在下」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from delivery.cards import Digest, lead_message, summary_message

# 撞限流之后等多久再试一次。Slack 会在回复里给 Retry-After，有那个就用
# 它，没有就用这个数。chat.postMessage 的限速大约是每频道每秒一条，
# 所以正常发一批十几条根本撞不到，撞到基本是别的进程也在往同一个频道发。
DEFAULT_RETRY_AFTER = 2.0

# 每条之间歇一下。不是为了绕限流（限流有上面那条重试兜着），是为了让
# 消息在频道里的顺序稳定 —— 连着不间断地发，Slack 偶尔会把两条的时间戳
# 排得极近，客户端里的先后就不一定跟我们发的顺序一样。
BETWEEN_MESSAGES_SECONDS = 1.1


def build_client() -> Any:
    """造一个 Slack 客户端。

    输入：无（token 从环境变量或仓库根的 .env 读）
    输出：一个 slack_sdk.WebClient

    在这里读 token，而不在 cards 或者 trigger 里读：这一层本来就是唯一
    认识 Slack 的地方。返回类型写 Any 是因为上层不该依赖 slack_sdk 的
    类型 —— 测试里传进去的就是个假客户端。

    在函数里 import slack_sdk 也是同一个道理：--plan-only 那条路根本不
    需要发消息，没装 slack-bolt 也该能跑。跟 build_llm_client 同一条口径。

    两种情况：

        环境变量或 .env 里有 SLACK_BOT_TOKEN  -> 一个 WebClient
        两处都没有                            -> 抛 RuntimeError

    读不到就抛，不返回 None：返回 None 会让「没配 token」这件事一路
    走到发消息那一行才炸成 AttributeError，离原因太远。
    """
    import os

    from slack_sdk import WebClient

    from persistence.db import _load_env_file

    token = os.environ.get("SLACK_BOT_TOKEN") or _load_env_file().get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("找不到 SLACK_BOT_TOKEN：设环境变量，或写进仓库根的 .env")
    return WebClient(token=token)


def read_channel() -> str:
    """读要发到哪个频道。

    输入：无
    输出：频道 id，形如 C0000000001

    两种情况：

        配了 SLACK_LEADS_CHANNEL   -> 那个值
        没配                       -> 抛 RuntimeError

    要的是频道 id 不是频道名。名字也能发，但改一次名就断了，而 id 永远
    不变；而且用名字发要额外的 scope 去解析名字，我们只申请了 chat:write。
    """
    import os

    from persistence.db import _load_env_file

    channel = os.environ.get("SLACK_LEADS_CHANNEL") or _load_env_file().get("SLACK_LEADS_CHANNEL")
    if not channel:
        raise RuntimeError("找不到 SLACK_LEADS_CHANNEL：要填频道 id（C 开头），不是频道名")
    return channel


def permalink(client: Any, channel: str, ts: str) -> str | None:
    """拿一条消息的链接，拿不到就返回 None。

    输入：Slack 客户端；消息所在的频道 id，如 'C0000000001'；消息的时间戳，如 '1790126072.000100'
    输出：'https://acme-team.slack.com/archives/C0000000001/p1790126072000100'，拿不到是 None

    chat.getPermalink 不要额外的 scope，现有的 chat:write 和 groups:history 就够。
    拿不到只返回 None 不抛：链接只是让人少点一下，不值得为它让已经发出去的卡片跟着失败。
    只接 Slack 回的错（SlackApiError），别的异常照样抛 —— 那是代码错了，不该被吞掉
    """
    from slack_sdk.errors import SlackApiError

    try:
        return client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except SlackApiError as err:
        print(f"[slack] 拿不到 {channel}/{ts} 的链接：{err.response.get('error', 'unknown')}")
        return None


@dataclass
class PushResult:
    """这一跑的早报发出去之后的账。

    一个实际值（汇总加三条线索，全成）：

        PushResult(summary_ts="1788393340.530749",
                   posted={1281: "1788393342.1", 1287: "1788393343.4",
                           1290: "1788393344.7"},
                   failures={})

    一个部分失败的（第二条撞上了消息过长）：

        PushResult(summary_ts="1788393340.530749",
                   posted={1281: "1788393342.1"},
                   failures={1290: "msg_too_long"})

    posted 里存的是每条卡片的时间戳，因为 chat.update 要拿它才能改那条
    消息 —— 点完按钮把卡片改成「已收到」就靠它。现在没人用，但它是发
    消息那一刻唯一能拿到的东西，不存下来以后就得回头去 Slack 里搜。

    failures 记 Slack 回的错码原文，不记我们翻译过的话：那个码是去查
    文档和对着日志排查的钥匙，翻译一遍就对不上了。

    刻意不做成「成功就返回、失败就抛」：一条卡片发失败不该让别的卡片
    不发，也不该让整跑失败 —— 今天少推一条线索是可接受的，而一条卡片
    发不出去通常是那一条的内容问题（太长、块不合法），不是系统问题。
    """

    summary_ts: str | None = None
    posted: dict[int, str] = field(default_factory=dict)
    failures: dict[int, str] = field(default_factory=dict)

    @property
    def pushed_lead_ids(self) -> list[int]:
        """真发出去了的那几条的 lead_id，按 id 排序。

        输入：无
        输出：lead_id 列表

        trigger 拿它去给 leads 记 pushed_at 和 pushed_run_id。只记发成了
        的：记上没发出去的那几条，ignored 扫描会在七天后把它们扫成
        「她没理」，而她根本没见过。
        """
        return sorted(self.posted)


def _post(client: Any, channel: str, payload: dict) -> str:
    """发一条，撞限流就等一下再试一次。

    输入：Slack 客户端；频道 id；chat.postMessage 的参数
    输出：这条消息的时间戳

    三种情况：

        一次就成            -> 返回 ts
        回 ratelimited      -> 按 Retry-After 等一下重发一次，成了返回 ts
        别的错，或者重试也失败 -> 原样抛出去，由调用方决定这一条算失败

    只重试限流这一种。别的错码重试没有意义：msg_too_long 再发一次还是
    太长，not_in_channel 再发一次还是没被邀请，而每次重试都要等一秒。
    传输层的抖动（连接断了、超时）由 slack_sdk 自己那层管。
    """
    from slack_sdk.errors import SlackApiError

    try:
        return client.chat_postMessage(channel=channel, **payload)["ts"]
    except SlackApiError as err:
        if err.response.get("error") != "ratelimited":
            raise
        # Retry-After 在响应头里，单位是秒。拿不到就用默认值。
        wait = float(err.response.headers.get("Retry-After", DEFAULT_RETRY_AFTER))
        print(f"[slack] 撞限流，等 {wait} 秒再试一次")
        time.sleep(wait)
        return client.chat_postMessage(channel=channel, **payload)["ts"]


def post_digest(
    client: Any,
    channel: str,
    digest: Digest,
    *,
    run_id: int,
    company_count: int,
    weights: dict[str, int],
    origin: str | None,
    failed: bool = False,
    pause: float = BETWEEN_MESSAGES_SECONDS,
) -> PushResult:
    """把一份早报发出去：一条汇总，再每条线索一条。

    输入：Slack 客户端；频道 id；分档截断都算完的 Digest；这一跑的
          run_id；这一跑分析了几家公司；七维的权重（卡片上要印）；
          来自哪条对话的链接（定时那一跑是 None，见 cards.summary_message）；
          这一跑的采集是不是全失败了（汇总那段说明要换一套话，
          理由见 cards.summary_message 的情况五）；
          每条之间歇几秒（测试里传 0）
    输出：一个 PushResult，说清汇总和每条各自发成没发成

    先发汇总拿到 summary_ts，然后按 A 档、B 档、C 档的顺序逐条发，
    每条成功就记进 posted、失败就记进 failures 并接着发下一条。

    汇总先发是因为 Slack 里新消息在下面：先发汇总，读起来就是「总账在上、
    卡片在下」。反过来发的话总账会压在一堆卡片底下。

    五种情况：

    一、全成

        输入   digest 里 A 1 条、B 2 条，company_count=22
               发 4 条消息（1 汇总 + 3 卡片），每条之间歇 1.1 秒
        输出   PushResult(summary_ts="1788...", posted={三个 lead_id: ts},
                          failures={})
        后果   调用方拿 pushed_lead_ids 去给 leads 记 pushed_at 和
               pushed_run_id

    二、某一条卡片发失败（内容太长、块不合法）

        输入   同上，但其中一条的 blocks 被 Slack 拒了
        输出   posted 里少那一条，failures={1290: "msg_too_long"}
        后果   别的卡片照样发出去了。刻意不抛：一条卡片发不出去通常是
               那一条的内容问题，不该让今天别的线索也推不出去。
               失败那条不会被记 pushed_at，所以 ignored 扫描不会在七天后
               把它算成「她没理」—— 她根本没见过

        网络断了、超时也按这一种算：slack_sdk 自己重试一次之后抛的是 TimeoutError、
        URLError 这类，不是 SlackApiError。错码记成异常的类名，如 failures={1290: "TimeoutError"}。
        不接住它的话，异常会冲出这个函数：汇总和前面几张已经发出去了，调用方却拿不到
        PushResult，那几张一行 pushed_at 都记不上 —— interactive 那边的 deliver 节点
        重跑时查不到它们推过，会把整批再发一遍

    三、汇总就发失败（token 失效、bot 没被邀请进频道）

        输入   client 的第一次调用就抛 SlackApiError
        输出   不返回，原样抛出去
        后果   汇总发不出去说明的是「跟 Slack 的连接坏了」而不是「某条
               内容有问题」，那时候接着发卡片只会把同一个错重复十几遍。
               抛出去让 trigger 在日志里留下堆栈

    四、这一跑一条都没过线

        输入   digest.pushed_count == 0
        输出   PushResult(summary_ts="1788...", posted={}, failures={})
        后果   汇总照发（那一条会写明「这一跑没有过线的线索，这是合格
               结果不是故障」），后面没有卡片可发。
               不发汇总的话，「系统挂了」和「今天真没有」在频道里长得
               一模一样

    五、撞上限流

        输入   某一条回 ratelimited
               按 Slack 给的 Retry-After 等一下，重发那一条
        输出   重发成了就跟情况一一样；再失败就按情况二记进 failures

    这个函数不做幂等。同一个 run 的早报发两次，频道里就有两份 ——
    它没有「这一跑发过没有」的记忆。trigger 那边一跑只调一次，
    而重跑同一个 run 本来也需要先把库里那一跑的状态想清楚，
    在这一层加去重会把那个问题藏起来。
    """
    result = PushResult()

    # 汇总那一条不进 try：它失败说明连接坏了，接着发卡片只会把同一个错
    # 重复十几遍。理由见情况三。
    result.summary_ts = _post(
        client,
        channel,
        summary_message(
            digest, run_id=run_id, company_count=company_count, origin=origin, failed=failed
        ),
    )

    from slack_sdk.errors import SlackApiError

    # C 档只在 push_below_line 打开时有东西，排在最后：过线的先看到
    for band, cards in (("A", digest.a), ("B", digest.b), ("C", digest.c)):
        for card in cards:
            if pause:
                time.sleep(pause)
            payload = lead_message(card, band=band, run_id=run_id, weights=weights)
            try:
                result.posted[card.lead_id] = _post(client, channel, payload)
            except SlackApiError as err:
                # 错码原样记下来，不翻译：那个码是查文档和对日志的钥匙。
                code = err.response.get("error", "unknown")
                result.failures[card.lead_id] = code
                print(f"[slack] lead={card.lead_id} {card.display_name} 发失败：{code}")
            except Exception as err:
                # 网络断了、超时：slack_sdk 重试过一次还不行，抛的不是 SlackApiError。
                # 输出   failures={1290: "TimeoutError"}
                #        记下来接着发下一张，理由见情况二的最后一段。这里只包了 _post 那一句，
                #        拼 payload 那一步在 try 外面，代码写错了照样当场炸，不会被当成发失败吞掉
                code = type(err).__name__
                result.failures[card.lead_id] = code
                print(f"[slack] lead={card.lead_id} {card.display_name} 发失败：{code} {err}")

    return result
