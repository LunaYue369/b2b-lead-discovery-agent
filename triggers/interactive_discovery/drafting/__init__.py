"""写稿那张图：在线索卡的 thread 里，用户说要联系这家，bot 问写什么、定计划、起草、按意见改，直到定稿。

    state.py               这张图记着的东西
    nodes.py               每个节点做什么
    graph.py               节点怎么连
    planner.py             plan_drafts 那次调模型，和「每份写给谁、写哪一种、发到哪、用了哪些默认」、
                           号怎么发、账本怎么跟着计划变的纯函数
    reply.py               classify_plan_reply 那次调模型：用户对着计划回的话是要改、同意、还是不写了
    writer.py              write_draft 写一份稿：调模型 + 代码检查
    feedback.py            classify_feedback 那次调模型，和判决怎么落账本、下一步往哪走的纯函数
    prompts.py             喂给模型的话
    messages.py            发进 thread 的话（纯函数）
    slack_conversation.py  交给 runner 的那一份（drafting_graph）

对话从两个地方开，最后都在那张卡片的 thread 里：点「要联系」按钮（bot 先问写什么），
或者在卡片的 thread 里 @bot（那句话就是要写什么，不再问）。

整张图（讲解和一个从头到尾的大例子见 langgraph学习/drafting/replan_rewrite_1a.txt）：

    START -> retrieve_knowledge -+- request 为空（按钮开的）-> ask_request（停 1）-+-> plan_drafts
                                 +- request 有值（@bot 开的）----------------------+
          plan_drafts（读用户说的 + 补空着的：写哪一种、卖点、开场；号是身份证，改计划时沿用）
          -> await_plan（停 2，计划给用户看）-> classify_plan_reply
                 要改 -> plan_drafts / 同意 -> write_draft / 不写了 -> END
          -> write_draft（账本里 planned / rewrite 的每份各写一次，代码检查不过关重写）
          -> await_write（停 3，稿子发进 thread）-> classify_feedback（一句话拆到每一份）
          -> record_round（判决写进 draft_revisions，唯一写库的节点）
                 有一份要改计划或要加一份 -> plan_drafts / 只改正文 -> write_draft
                 还有没表态的 -> await_write / 都定稿了或不写了 -> END
          对话走到 END 之后，在卡片 thread 里再 @bot，在同一份存档上从头再跑一遍（新一轮从 1 号发起）

一次可以要好几份稿（每轮最多 4 份，数活着的）。写给谁（1 号这家公司 / 2 号起卡片上的人）只有用户能挑，
没挑就写给 1 号；写哪一种（邮件 / LinkedIn 私信 / LinkedIn 加好友附言 / 文章）用户没说由 AI 定；
卖点和开场引哪条证据一直由 AI 定。认不出、对不上的不追问，按固定办法先定下来，在计划里写一句。

稿子只写不发：用户复制粘贴自己发，这张图里没有一个节点往外发消息（发进 thread 的那几句是 runner 发的）。

跟 query 那张图共用的：存档器那几张表（两张图的存档靠 thread_id 分开）、Slack 监听进程、
runner（interactive_discovery/runner.py）、数据库连接。interrupt 为什么写在节点第一行、
恢复时从哪一行重跑，讲解在 langgraph学习/nodes/nodes_1a.txt 第二节，这里不重复。
"""
