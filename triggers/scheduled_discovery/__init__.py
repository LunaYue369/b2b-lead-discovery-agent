"""每天定时跑一次的 discovery：时钟拉起、跑完退出，不挂起、不等人。

    main.py        入口本体：读配置、挑今天跑哪几条 query、调 harvest_once、推卡片
    __main__.py    让 python -m triggers.scheduled_discovery 这条命令能跑

跟 interactive 共用的两段不在这个文件夹里：开 run 到收尾在 triggers/harvest.py，
推卡片在 triggers/digest.py。
"""
