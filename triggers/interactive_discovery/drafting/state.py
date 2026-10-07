"""写稿那张图的 state —— 图跑的时候手上拿着的全部东西。

字段怎么合并（reducer / LastValue）、基本类型和容器各存进哪张表，跟 query 那张图是同一套，
讲解在 query/state.py 和 langgraph学习/state/state_1a.txt，这里每个字段只写值长什么样、谁写谁读。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class DraftingState(TypedDict):
    """一条起草对话跑到中间的时候，这份 state 长什么样。

    下面这一份是 North Field Technologies（lead 1289）那张卡片下面，bot 问完「给这家写什么」、
    用户回了一句之后的样子：

        {
            "messages": [
                "AI: 请问你想给 North Field Technologies 写哪些 outreach 信息？…\\n\\n"
                "名单：\\n1. North Field Technologies 这家公司本身（称呼他们的团队）\\n"
                "2. Zach T.（发帖的人不是这家公司的，…）\\n"
                "3. Marc-Andre Lavoie（Founder & CEO，这家公司的人）\\n…",
                "用户: 给 3 号写封邮件，强调 MOQ 500",
            ],
            "lead_id": 1289,
            "lead": {"display_name": "North Field Technologies",
                     "emails": [{"value": "contact@northfield-inc.example", "source": "website", ...},
                                {"value": "marcandre@northfield-inc.example", ...}],
                     "people": [{"name": "Zach T.", "relation": "not_member", ...},
                                {"name": "Marc-Andre Lavoie", "relation": "member", ...},
                                {"name": "SingAlong Group", "relation": "other_page", ...}]},
            "request": "给 3 号写封邮件，强调 MOQ 500",
            "ai_plan": {"count_user_requested": 0, "count_user_words": "",
                            "drafts": [{"user_words": "给 3 号写封邮件，强调 MOQ 500",
                                        "recipient_user_requested": "3 号", "recipient_ai_understood": 3,
                                        "email_user_requested": "", "email_ai_understood": "",
                                        "form_user_requested": "email",
                                        "emphasis_user_requested": ["MOQ 500"],
                                        "form_ai_planned": "", "selling_points_ai_planned": ["moq"],
                                        "opening_ai_planned": 2720,
                                        "reason_ai_planned": "创始人说要找工厂做 1,000 台，开场引他这条"}],
                            "language_user_requested": "", "tone_user_requested": "", "unsupported": []},
        }

    第一次 invoke 传进去的那份，看对话是怎么开的：

        点「要联系」按钮        initial_state(1289, "")
                                -> {"messages": [], "lead_id": 1289, "lead": {}, "knowledge": {},
                                    "request": "", "round_start": 0, "ai_plan": {}, "plan": {},
                                    "plan_feedback": "", "decision": ""}
        卡片 thread 里 @bot     initial_state(1289, "帮我写封邮件给他们，强调 MOQ 500")
                                -> {"messages": ["用户: 帮我写封邮件给他们，强调 MOQ 500"], "lead_id": 1289,
                                    "lead": {}, "knowledge": {},
                                    "request": "帮我写封邮件给他们，强调 MOQ 500", "round_start": 0,
                                    "ai_plan": {}, "plan": {}, "plan_feedback": "", "decision": ""}

    一张卡片一份：存档钥匙由卡片那条消息算出来（"draft:C0000000001:<卡片的 ts>"），
    同一张卡片下面只有一条起草对话。
    """

    # 这条对话里说过的每一句原话，双方都存，只增不改。前缀「用户:」/「AI:」就是说话人。
    #
    # 值     ["AI: 请问你想给 North Field Technologies 写哪些 outreach 信息？…",
    #         "用户: 给 2 号写封邮件，强调 MOQ 500"]
    # 合并   operator.add：节点返回只装新那几句的列表，库自己接到后面
    # 存     list 是容器，每一版整份一行进 checkpoint_blobs
    messages: Annotated[list[str], operator.add]

    # 给哪一家写。开对话的时候就定了，之后没有节点改它。
    #
    # 值     1289（leads.id，从卡片按钮那一块的 block_id "lead:1289:run:46" 读出来）
    # 合并   LastValue
    # 存     int 是基本类型，留在 checkpoints 表的 jsonb 里
    lead_id: int

    # retrieve_knowledge 从库里读出来的这家公司，整份覆盖。
    #
    # 值     {"display_name": "North Field Technologies",
    #         "emails": [{"value": "contact@northfield-inc.example", "source": "website", "person_name": None,
    #                     "email_status": None, "catch_all": None, "quality_score": None},
    #                    {"value": "marcandre@northfield-inc.example", "source": "linkedin_email_search",
    #                     "person_name": "Marc-Andre Lavoie", "email_status": "risky",
    #                     "catch_all": True, "quality_score": 60}]}
    #        每个邮箱带来源和可信度：第一问要把可信度写给用户看，以后写稿也要知道发给的是谁的邮箱
    #        emails 只放这家自己的邮箱，最新找到的在前；一个都没有是 []（Chatterly Inc.，lead 1281）。
    #        别家的邮箱不放：Kestrel（lead 1518）库里唯一那个 evan.park@rawcutstudio.example
    #        是 Evan Park 在另一家公司 Rawcut 的，写进来就会把稿子发到别家去，所以它的 emails 是 []
    #
    #        "people": 卡片「联系人」那一区的每个人，顺序跟卡片一样，例 lead 1289
    #            [{"name": "Zach T.", "relation": "not_member", "position": None,
    #              "profile_url": "https://www.linkedin.com/in/zachtrent"},
    #             {"name": "Marc-Andre Lavoie", "relation": "member", "position": "Founder & CEO",
    #              "profile_url": "https://www.linkedin.com/in/malavoie"},
    #             {"name": "SingAlong Group", "relation": "other_page", "position": None,
    #              "profile_url": "https://www.linkedin.com/company/singalong-group/"}]
    #        relation 是 CardRelation 的取值。第一问按这个顺序给人编号（1 号是这家公司本身，
    #        人从 2 号起，这家自己的公司主页不单独占号，见 planner.listed_people），
    #        plan_drafts 让模型把用户说的「3 号」「创始人」对到这张名单上的编号
    #        空字典 {} 表示还没读过（第一次 invoke 的初值）
    # 合并   LastValue
    # 存     dict 是容器，整份进 checkpoint_blobs。存 dict 不存 CardContacts 对象，理由同
    #        query/state.py 的 intent_result：存档要在库里躺很久，对象的字段一改旧存档就读不对
    lead: dict[str, Any]

    # retrieve_knowledge 取来的「写什么」要用的东西，整份覆盖。跟 lead 分开放：lead 管发给谁、
    # 发到哪（第一问和 plan_drafts 对编号要用），这一格管稿子里写什么（plan_drafts 挑卖点和开场、
    # 以后 write_draft 写稿用）。
    #
    # 值（编的例子，Acme Audio 这家）：
    #        {"profile": {"website": "https://acme-audio.example", "employee_count_range": "2-10",
    #                     "founded_year": 2023, "location": "Austin, US",
    #                     "industries": ["Consumer Electronics"],
    #                     "tagline": "Clip-on mics for creators",
    #                     "description": "Acme Audio makes wireless clip-on microphones for video creators.",
    #                     "specialities": ["wireless audio"]},
    #         "assessment": {"score": 72,
    #                        "analysis": "Acme Audio just finished a Kickstarter and needs 800 units built.",
    #                        "quotes_mean": "创始人本人说众筹刚结束，在找工厂做 800 台",
    #                        "quotes": ["We just wrapped our Kickstarter"]},
    #         "evidence": [{"signal_id": 2720, "lead_type": "author", "first_person": True,
    #                       "author": "Dana Lee", "role": "Founder @ Acme Audio", "when": "2026-09-20",
    #                       "url": "https://www.linkedin.com/posts/...",
    #                       "text": "We just wrapped our Kickstarter and are looking for a factory ...",
    #                       "reposted_author": None, "reposted_text": None},
    #                      {"signal_id": 2733, "lead_type": "mentioned", "first_person": False,
    #                       "author": "Audio Weekly", ...}],
    #         "facts": {"moq": {"value": 500, "unit": "units", ...}, "services": {...}, "_todo": [...]},
    #         "avoid": [{"key": "third-party-evidence-no-fake-coincidence", "scope": "draft",
    #                    "statement": "证据是从第三方口中得知的……", "source": "models/signal.py ……"},
    #                   另外两条]}
    #
    #        profile      公司详情（company_profiles 最新一份 + raw 里的自我介绍）。没抓过是 {}
    #        assessment   最近一次打分的结论，跟卡片上「为什么推这家」是同一份
    #        evidence     最新的几条证据全文，最新的在前，跟卡片上那几条是同一批。
    #                     first_person=False 的是从第三方口中得知的，开场白不能装作偶然看到本人的帖子
    #        facts        config/facts.json 整份：我们能提供什么，以及稿子里关于我们的数字只能从这里来
    #        avoid        config/drafting_knowledge.yaml：哪类写法别用
    #        空字典 {} 表示还没取过（第一次 invoke 的初值）
    # 合并   LastValue
    # 存     dict 是容器，整份进 checkpoint_blobs。facts 和 avoid 是配置文件，照样存一份：
    #        以后查「这份稿子写的时候模型看到的是哪一版 facts」，翻存档就知道，不怕配置文件后来改过
    knowledge: dict[str, Any]

    # 这次要写什么，用户的原话。两个来处：卡片 thread 里 @bot 的那一句（开对话时就有），
    # 或者点按钮开的对话里、用户对第一问「给这家写什么」回的那句。空串表示还没说。
    #
    # 值     "邮件，发给 Marc-Andre，强调 MOQ 500" / "LinkedIn" / ""
    #        retrieve_knowledge 之后那条条件边看它：空串才去问第一句（graph.route_after_retrieve）
    # 合并   LastValue
    # 存     str 是基本类型，留在 checkpoints 表的 jsonb 里
    #
    # 它跟 messages 里的某一句是同一句话。另存一份只为一件事：route_after_retrieve 要一眼看出
    # 「用户开口时有没有已经说了要写什么」。plan_drafts 读的不是它，是这一轮的 messages ——
    # 以后用户看了计划再改，要写什么会散在好几句里
    request: str

    # 这一轮从 messages 的第几句开始（下标）。每一轮开头由 retrieve_knowledge 写一次。
    #
    # 值     第一轮 0；对话走到 END 之后又 @ 了 bot、在同一份存档上从头再跑（slack_conversation._restart），
    #        这一轮就从新那句开始，例
    #            messages = ["AI: 请问你想给 North Field Technologies 写哪些…",          <- 上一轮
    #                        "用户: 写三份：…再给 Mike 写一份",                       <- 上一轮
    #                        "AI: 好，给 North Field Technologies 写这 3 份：…",         <- 上一轮的计划
    #                        "用户: 写吧",                                           <- 上一轮，到 END
    #                        "用户: 给 3 号写条加好友附言，再写一篇讲小批量的文章"]    <- 这一轮
    #            round_start = 4
    # 合并   LastValue
    # 存     int 是基本类型，留在 checkpoints 表的 jsonb 里
    #
    # 为什么要有它：同一张卡片下面可以起草不止一轮（重开用的是同一份存档），而这份 state 里
    # 只有 messages 是往后接的（operator.add），其余字段每一轮都整份换掉（LastValue：initial_state 把
    # ai_plan / plan / plan_feedback / decision / drafts 这几格清空，
    # retrieve_knowledge 重取 lead / knowledge）。
    # 所以第二轮及以后，别的字段都只剩这一轮的，messages 里却还躺着前几轮的全部原话，
    # 读 messages 的节点要自己只取这一轮那一段。
    #
    # plan_drafts / classify_plan_reply 只读 messages[round_start:]：重开之后前一轮的要求已经写成计划了，
    # 还当成这一轮的，模型会把上一轮的「写三份」再定一遍，把新说的那句挤掉。
    # messages 本身不删，整条对话的记录还在。以后再加读 messages 的节点，也要从 round_start 读起
    round_start: int

    # plan_drafts 那次模型调用给出的，每轮整份覆盖。键和值见 planner.AiPlan / AiPlanDraft：
    # 每份稿分两半，用户说的（*_user_requested / *_ai_understood / user_words）和 AI 定的（*_ai_planned）。
    #
    # 值（编的例子，用户说「给 3 号写封邮件，强调 MOQ 500，再给 2 号写一条加好友附言」）
    #        {"count_user_requested": 0, "count_user_words": "",
    #         "drafts": [{"user_words": "给 3 号写封邮件，强调 MOQ 500", "recipient_user_requested": "3 号",
    #                     "recipient_ai_understood": 3, "email_user_requested": "", "email_ai_understood": "",
    #                     "form_user_requested": "email", "emphasis_user_requested": ["MOQ 500"],
    #                     "form_ai_planned": "", "selling_points_ai_planned": ["moq"],
    #                     "opening_ai_planned": 2720, "reason_ai_planned": "…"},
    #                    {"user_words": "再给 2 号写一条加好友附言", "recipient_user_requested": "2 号",
    #                     "recipient_ai_understood": 2, ..., "form_user_requested": "linkedin_note",
    #                     "selling_points_ai_planned": ["categories"], "opening_ai_planned": 2733, ...}],
    #         "language_user_requested": "", "tone_user_requested": "", "unsupported": []}
    #        空字典 {} 表示还没定过
    # 合并   LastValue，整份覆盖：每次都把这一轮的话重读一遍，读出来的就是到这一句为止的全部要求
    # 存     dict 是容器，整份进 checkpoint_blobs
    #
    # 这一格是模型的原话，留着事后查；「这一轮到底怎么写」在下面的 plan
    ai_plan: dict[str, Any]

    # 这一轮的计划：plan_drafts 拿 ai_plan 加 lead 用代码算出来（planner.finalize_plan），当场存下。
    # 键和值见 planner.Plan / PlanSlot（摊平的，元组都成了 list，见 planner.plan_to_state）。
    #
    # 值（编的例子，接着上面那份 ai_plan，North Field Technologies）
    #        {"slots": [{"number": 1, "user_words": "给 3 号写封邮件，强调 MOQ 500",
    #                    "recipient": {"name": "Marc-Andre Lavoie", "relation": "member", ...},
    #                    "recipient_user_requested": "3 号", "email_user_requested": "", "form": "email",
    #                    "address": "marcandre@northfield-inc.example", "emphasis": ["MOQ 500"],
    #                    "selling_points": ["moq"], "opening": 2720, "reason": "…",
    #                    "ai_decides": [], "notes": []},
    #                   {"number": 2, ..., "form": "linkedin_note", "address": None, ...}],
    #         "language": "英文", "tone": "简短、专业，不用推销腔", "unsupported": [],
    #         "notes": ["语言你没说，按默认用英文", "语气你没说，按默认：简短、专业，不用推销腔"],
    #         "ai_plans_all": False}
    #        空字典 {} 表示还没定过
    # 合并   LastValue，整份覆盖：每定一次计划换一份
    # 存     dict 是容器，整份进 checkpoint_blobs
    #
    # 为什么存、不像 ai_plan 那样每次现算：用户在计划那一停点头的、以后 write_draft 照着写的，
    # 必须是同一份。现算的话，两次之间改过 finalize_plan 的规则或配置（例如默认语气），
    # 写出来的就跟用户看到的对不上，而且查不出来
    plan: dict[str, Any]

    # 用户对着计划回的那句话，原话，不带「用户: 」前缀。await_plan 写，classify_plan_reply 读。
    #
    # 值     "第 2 份写给 3 号" / "写吧" / "算了"。空串表示这一轮还没人对着计划说过话
    # 合并   LastValue
    # 存     str 是基本类型，留在 checkpoints 表的 jsonb 里
    #
    # 它跟 messages 最后一句是同一句话。另存一份，是让「要判的是哪一句」成为一份明写的输入，
    # 不靠「取 messages 最后一句」那条位置约定，理由同 query/state.py 的 plan_feedback
    plan_feedback: str

    # 下一步往哪走，每次重判、整份覆盖。两处写、两个判断函数读：
    #
    # classify_plan_reply（对着计划回的话）写，graph.route_plan_reply 读，取值 reply.DECISIONS：
    #        "revise"  要改：回 plan_drafts 重定一版（例「第 2 份写给 3 号」）
    #        "approve" 同意：去 write_draft
    #        "cancel"  不写了：到 END
    # classify_feedback（看完稿子回的话）写，graph.route_feedback 读，取值 feedback.DECISIONS，
    # 由代码从每份的判决按顺序推（越上游越先，回上游往下走照样经过下游）：
    #        "cancel"  不写了
    #        "replan"  有一份要改计划，或者要加一份 -> plan_drafts
    #        "rewrite" 有一份只改正文 -> write_draft
    #        "wait"    没有要写的，还有没表态的 -> await_write 再问一次
    #        "done"    每份都定稿或不要了 -> END
    #        空串表示还没判过
    # 合并   LastValue
    # 存     str 是基本类型，留在 checkpoints 表的 jsonb 里
    #
    # 占一个字段的理由同 query/state.py 的 decision：条件边的判断函数收到的是 state，不是节点的返回值
    decision: str
    # 每一份稿的账本，键是编号（字符串 "1"、"2"），这一份第一次出现在计划里就建一条，不要了也不删。
    # 讲解和全程的值见 langgraph学习/drafting/replan_rewrite_1a.txt 3.10。
    #
    # 值     {"1": {"status": "approved", "version": 1, "plan_version": 1,
    #               "text": "Subject: ...\n\nHi Dana, ...", "feedback": "", "problems": []},
    #         "3": {"status": "rewrite", "version": 1, "plan_version": 2, "text": "...",
    #               "feedback": "短一点，别提众筹", "problems": []}}
    #        status        planner.STATUSES 六种之一
    #        version       正文第几版，0 = 还没写过
    #        plan_version  当前正文照的是计划第几版，0 = 还没写过
    #        text          当前这一版正文；被判过的旧版在 draft_revisions 表里
    #        feedback      还没执行的正文意见（动计划的话进 plan_feedback，不在这里）
    #        problems      写完代码检查没过、重写也没改好的那几条，发稿时提醒用户；过了是 []
    #        slot          这一份最后一版计划（plan["slots"] 里那一份）。每次定完计划刷新；不要了之后停住，
    #                      要回来时代码照它放回计划，号不变
    #        {} 表示还没定过计划
    # 写     plan_drafts（planner.drafts_after_plan）/ write_draft / classify_feedback
    # 合并   LastValue，整份覆盖：改之前先复制，原地改存档里看不见
    # 存     dict 是容器，整份进 checkpoint_blobs
    drafts: dict[str, Any]
    # 用户看完稿子回的那句原话。await_write 写，classify_feedback 拆，record_round 抄进库。
    #
    # 值     "第 1 份可以了。第 2 份不要了。第 4 份短一点。" / ""（这一轮还没看过稿）
    # 合并   LastValue；存 jsonb
    draft_feedback: str
    # 这一次 write_draft 刚写出来的那几份的号。await_write 只把这几份的正文发出去，
    # 其余的只在总览里提一句。classify_feedback 判完清空（再问一次时不重发正文）。
    #
    # 值     [3, 4, 5] / []
    # 合并   LastValue；存 checkpoint_blobs
    written_now: list[int]
    # 用户刚才那句话对每一份下的判决。classify_feedback 写，record_round 照着写进 draft_revisions。
    # 单独存一份，不让 record_round 从账本反推：账本只有判完的状态，看不出「这一句判了哪几份」。
    #
    # 值     [{"number": 1, "verdict": "approved", "feedback": ""},
    #         {"number": 3, "verdict": "replan", "feedback": "改成给 Dana 发 LinkedIn 私信"}]
    #        verdict 取 approved / rewrite / replan / dropped / cancelled；没判任何一份是 []
    # 合并   LastValue；存 checkpoint_blobs
    verdicts: list[dict[str, Any]]
    # 下一条消息开头要告诉用户的一句。classify_feedback 写（每次判完重写，没话说是 ""），await_write 发。
    #
    # 值     "第 2 份要回来的话，一轮最多 4 份（已定稿的也算），先说不要哪一份" / ""
    # 合并   LastValue；存 jsonb
    notice: str


def initial_state(lead_id: int, request: str) -> dict:
    """开起草对话时交给图的初始 state。其中，request的有无决定了route_after_retrieve。

    输入：
        lead_id   哪一家，例 1289
        request   是runner里通过处理Inbound信息而来的
                  如果用户在线索卡下@bot开启drafting，那么request是户开对话时说的那句（去掉 @ 之后）
                  如果用户是点击按钮开启drafting，那么request为""
    输出：
        (1289, "")
            -> {"messages": [], "lead_id": 1289, "lead": {}, "knowledge": {}, "request": "",
                "round_start": 0, "ai_plan": {}, "plan": {}, "plan_feedback": "", "decision": "",
                "drafts": {}, "draft_feedback": "", "written_now": [], "verdicts": []}
            messages 是空的：按钮开的对话，第一句是 bot 问的，用户还一个字没说
        (1289, "帮我写封邮件给他们，强调 MOQ 500")
            -> {"messages": ["用户: 帮我写封邮件给他们，强调 MOQ 500"], "lead_id": 1289, "lead": {},
                "knowledge": {}, "request": "帮我写封邮件给他们，强调 MOQ 500", "round_start": 0,
                "ai_plan": {}, "plan": {}, "plan_feedback": "", "decision": ""}
            那句话既进 messages（对话记录），也进 request（下一个节点要读的就是它），理由见 request 那个字段

    DraftingState 的每个字段都要给上：LangGraph 只把已经有值的字段交给节点，
    没给的那个在节点里读是 KeyError。tests/test_drafting_graph.py 拿这份字典的键去对字段表。
    round_start 给的 0 只是占位：重开的那一轮，retrieve_knowledge 会按旧存档里已有几句重写它
    """
    return {
        "messages": [f"用户: {request}"] if request else [],
        "lead_id": lead_id,
        "lead": {},
        "knowledge": {},
        "request": request,
        "round_start": 0,
        "ai_plan": {},
        "plan": {},
        "plan_feedback": "",
        "decision": "",
        "drafts": {},
        "draft_feedback": "",
        "written_now": [],
        "verdicts": [],
        "notice": "",
    }
