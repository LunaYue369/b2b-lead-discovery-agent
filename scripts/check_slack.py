"""验一遍 Slack 那三样配好没配好。刻意一个 token 都不回显。

    python -m scripts.check_slack           只验身份和连接，不发消息
    python -m scripts.check_slack --post    往频道发一条测试消息（会被人看到）

三样东西：

    SLACK_BOT_TOKEN       xoxb- 开头，装应用之后拿到的。发消息用它
    SLACK_APP_TOKEN       xapp- 开头，App-Level Token，Socket Mode 用它
    SLACK_LEADS_CHANNEL   C 开头的频道 id，不是频道名

为什么不回显 token：Apify 那个 token 在对话记录里出现过一次，后来只能
换掉。这个脚本只打印「前缀对不对、长度合理不合理」和 Slack 那边回的
身份信息，任何时候都可以把它的输出贴给别人看。

为什么验证要包含「真发一条消息」（--post）：能不能发进那个频道，跟
token 有没有效是两件事。bot 没被邀请进频道的话，auth.test 一切正常，
而 chat.postMessage 会回 not_in_channel —— 而这个错要等第一次真跑
早上七点才会出现。
"""

from __future__ import annotations

import os
import sys

from persistence import db

# 频道 id 长这样：C 开头（公开和私有频道都是 C，私有的老频道可能是 G）。
# 频道名（#b2b-leads）不能用来发消息 —— 能用，但改名之后就断了，
# 而 id 永远不变。
CHANNEL_PREFIXES = ("C", "G")


def read_setting(name: str) -> str | None:
    """读一个配置，先看环境变量再看仓库根的 .env。

    输入：配置名
    输出：值，读不到就是 None

    跟 build_llm_client 读 OPENAI_API_KEY 同一条口径：环境变量优先，
    这样上 EC2 之后可以用 systemd 的 Environment= 而不必带 .env。
    """
    return os.environ.get(name) or db._load_env_file().get(name)


def describe(name: str, value: str | None, *, prefix: str) -> bool:
    """打印一个 token 的形状，不打印内容。

    输入：配置名；值；它该有的前缀
    输出：形状对不对

    三种情况：

        没读到        -> "缺" ，返回 False
        前缀不对      -> 说清该是什么开头（xoxb 和 xapp 最容易贴反），返回 False
        看着对        -> 打印前缀和长度，返回 True

    长度也打出来是为了认出「复制的时候少了一截」这种事：Slack 的 token
    很长，从网页上拖选很容易漏尾巴，而那种 token 报的错是 invalid_auth,
    跟「token 写错了」长得一样。
    """
    if not value:
        print(f"  [缺] {name}")
        return False
    if not value.startswith(prefix):
        print(f"  [错] {name} 该是 {prefix} 开头，现在是 {value[:5]}... —— 两个 token 贴反了？")
        return False
    print(f"  [OK] {name}  {prefix}...（{len(value)} 个字符）")
    return True


def main() -> int:
    """验身份、验频道，可选发一条测试消息。

    输入：命令行参数（只有 --post）
    输出：进程退出码，全通就是 0

    四种情况：

    一、三样都齐、bot 也在频道里

        输出   打印 bot 名字、workspace 名字、频道 id，返回 0
               加 --post 的话频道里会多一条 "b2b-outreach 接通了"

    二、少一样

        输出   哪一样缺印出来，返回 1。不继续往下试 ——
               缺 app token 的话 Socket Mode 起不来，而那要到写常驻
               进程才发现

    三、token 无效或者过期

        输出   Slack 回的错码原样印出来（invalid_auth / account_inactive），
               返回 1

    四、bot 没被邀请进频道（--post 才测得出来）

        输出   not_in_channel，并提示去频道里打 /invite，返回 1
               这一条是最容易漏的：token 全对、auth.test 全绿，
               只有发消息那一下会失败
    """
    post = "--post" in sys.argv

    print("── 三样配置的形状 ──────────────────────────────────────")
    bot_token = read_setting("SLACK_BOT_TOKEN")
    app_token = read_setting("SLACK_APP_TOKEN")
    channel = read_setting("SLACK_LEADS_CHANNEL")

    ok = describe("SLACK_BOT_TOKEN", bot_token, prefix="xoxb-")
    ok = describe("SLACK_APP_TOKEN", app_token, prefix="xapp-") and ok

    if not channel:
        print("  [缺] SLACK_LEADS_CHANNEL")
        ok = False
    elif not channel.startswith(CHANNEL_PREFIXES):
        print(f"  [错] SLACK_LEADS_CHANNEL 要填频道 id（C 开头），不是频道名：{channel!r}")
        ok = False
    else:
        print(f"  [OK] SLACK_LEADS_CHANNEL  {channel}")

    if not ok:
        print("\n还没配齐，先补上再跑。")
        return 1

    # 到这里三样形状都对，问 Slack 那边认不认。
    # auth.test 不需要任何 scope，所以它只回答「这个 token 有效吗、是谁」。
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    print("\n── Slack 那边认不认 ───────────────────────────────────")
    client = WebClient(token=bot_token)
    try:
        who = client.auth_test()
    except SlackApiError as err:
        print(f"  [失败] auth.test 回了 {err.response['error']}")
        return 1
    print(f"  [OK] bot 名字 {who['user']}  workspace {who['team']}  bot_id {who['bot_id']}")

    if not post:
        print("\n身份没问题。但「能不能发进那个频道」还没验 —— 加 --post 试一条。")
        print("（bot 没被邀请进频道时，上面全绿，只有发消息那一下会失败）")
        return 0

    print("\n── 往频道发一条 ───────────────────────────────────────")
    try:
        sent = client.chat_postMessage(
            channel=channel,
            text="b2b-outreach 接通了。这条是配置自检，可以删。",
        )
    except SlackApiError as err:
        code = err.response["error"]
        print(f"  [失败] chat.postMessage 回了 {code}")
        if code == "not_in_channel":
            print("  修法：去那个频道里打 /invite @bot名字，把它拉进来")
        elif code == "channel_not_found":
            print("  修法：频道 id 抄错了，或者那是别的 workspace 的频道")
        elif code == "missing_scope":
            print("  修法：应用少了 chat:write，加上之后要重新装一次应用")
        return 1

    print(f"  [OK] 发出去了，ts={sent['ts']}")
    print("\n三样全通。Socket Mode 那一半要等常驻进程写完才验得到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
