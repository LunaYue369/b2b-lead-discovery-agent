"""唯一允许 import slack 的目录。

    cards.py    info card 渲染（fit score + 分项 + 原因 + evidence + lead_id）
    buttons.py  approve / skip / reject → 写 feedback 表

上面三层返回的是纯数据，不是「发了一条 Slack 消息」——
所以测整条链路不需要 Slack token。这条在 blog worker 上踩过
（chat_generator 把 Slack 漏进核心层），这次一开始就焊住：
    scripts/check_isolation.py
"""
