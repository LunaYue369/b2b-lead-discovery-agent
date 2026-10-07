"""找线索那张图：用户在频道里 @ bot 说想找什么，bot 拟检索式、等用户说「跑」，采集、找联系方式、推卡。

跟 scheduled 的唯一区别在前半段（怎么定 queries）和后半段（interrupt 等用户拍板），
中间那段 discovery/pipeline.py 是同一个函数。

    graph.py               图的形状：九个节点怎么连
    nodes.py               每个节点做什么
    state.py               这张图记着的东西
    intent.py / planner.py / reply.py / prompts.py   三次调模型：读需求、拟检索式、读用户回话
    slack_conversation.py  交给 runner 的那一份（query_graph）：开图、第一份 state、拼回复、
                           run_harvest 崩溃退回
"""
