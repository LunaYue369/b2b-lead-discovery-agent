"""在 Slack 里跟 bot 对话的那几张 LangGraph 图。一张图一个子文件夹，共用的放在这一层。

    query/        找线索：用户说想找什么 -> 拟检索式 -> 用户说「跑」-> 采集 -> 找联系方式 -> 推卡
    runner.py     Slack 对话的调度员：并发、跑一轮图、崩溃之后怎么收拾。几张图共用一个实例
    budget.py     月度上限。query 那张图的采集、推卡之前找联系方式（两条路）都过这一道

以后写稿那张图（drafting）也放在这一层，跟 query/ 平级。
每个子文件夹自己一套 graph.py / nodes.py / state.py，外加一个 slack_conversation.py
交给 runner 一个 ConversationGraph（例 query/slack_conversation.query_graph）；
存档器那几张表、Slack 监听进程、数据库连接是共用的。
"""
