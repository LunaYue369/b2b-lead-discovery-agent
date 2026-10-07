"""第一层 Morganer —— 三个入口，全项目只有这三个地方会「开始干活」。

    scheduled_discovery/     每天定时一次，规则驱动，一次性跑完就结束
    interactive_discovery/   Riley 在 Slack 里聊出来的一次搜索
    nightly_memory.py        每天一次的 memory processor

只属于一条 discovery 的代码进它自己的文件夹，两条都要用的平放在这一层：

    harvest.py         advisory lock、开 run、run_discovery、给 runs 收尾
    digest.py          按 run_id 从库里拼卡片、推进 Slack、记 pushed_at
    slack_buttons.py   唯一收 Slack 入站的常驻进程：两条路推出去的卡片按钮都在这里收，
                       对话消息也在这里收、再交给 interactive_discovery/runner.py

入口只做三件事：建 run 行、把参数凑齐、调下面的层。
业务逻辑不要写在入口里 —— 两个 discovery 入口调的必须是
discovery/pipeline.py 里同一个函数，那才是「Shared Discovery Core」的字面实现。
"""
