"""真调一次模型，把一句话走到「方案卡片」那一步的全过程打印出来。

    python -m scripts.peek_plan                          用默认那句话
    python -m scripts.peek_plan "帮我找找会议麦克风的初创"   用你自己的话
    python -m scripts.peek_plan "…" --topics microphone   跳过读需求那一步，手填品类词

它花的钱：两次 gpt-4o-mini 调用，加起来约 0.0005 美元。
它不花的钱：一分采集费都没有 —— 这个脚本根本不碰 Apify，也不碰 run_harvest。
它对库做的事：只 select（retrieve_knowledge 那三个查库函数都是只读的）。

为什么要有这个脚本：prompts.py 里那几条经验（平台词最值钱、制造语境的词最坑、
抱怨语气只有买方用）全是我们写上去的，从来没验过模型照着它拟出来是什么样。
测试焊的是形状和纪律，形状对不代表拟出来的词能用。

三段输出，各回答一个问题：

    一、需求读成了什么    understand_intent 那次调用。topics 翻译得对不对、
                          做不到的那几件有没有被诚实地记进 unsupported
    二、拟出来了什么      draft_queries 那次调用的原始输出，一条不筛。
                          这一段要看的是词本身：有没有用平台词、有没有踩制造语境
    三、卡片上会是什么样   screen_queries 筛完、报价、assumptions 拼完之后的样子，
                          跟 Riley 在 Slack 里看到的那一份逐字一样

它跑的是真的那几个函数，不是复刻一遍。哪天节点里的顺序改了，这里跟着改。
"""

from __future__ import annotations

import argparse
import json

from persistence import db, discovery_db, lead_db, query_db
from query_intelligence.knowledge import build_knowledge, load_avoid, pick_relevant
from triggers.harvest import build_llm_client, load_run_config
from triggers.interactive_discovery.query.intent import load_intent_config, read_intent, to_state_dict
from triggers.interactive_discovery.query.planner import (
    build_assumptions,
    draft_queries,
    estimate_cost_usd,
    load_planner_config,
    screen_queries,
)

# 默认那句话。挑它是因为 bone conduction 在库里一条都命中不了（peek_knowledge 验过），
# 所以这一跑同时回答「库里没经验的时候 planner 拿什么拟」。
DEFAULT_ASK = "帮我找找做骨传导耳机的初创公司，最好是刚做完众筹的，别带那种预热帖，只要加州的"


def fetch_knowledge(topics: list[str]) -> dict:
    """真连库，跑一遍 retrieve_knowledge 那个节点里的三次查询。

    输入：这一轮的品类词
    输出：一份 knowledge，形状跟节点里那一份一模一样

    这一段是把节点的函数体抄过来的，不是调节点本身 —— 节点收的是一份 state，
    而这里手上只有 topics。抄过来的代价是哪天节点里的顺序变了这里不会跟着变，
    所以那边改了记得回来看一眼。
    """
    avoid = load_avoid()
    with db.connect() as conn, conn.cursor() as cur:
        relevant = pick_relevant(query_db.all_queries(cur), topics)
        totals = discovery_db.execution_totals(cur)
        hits = lead_db.hits_by_query(cur, [query.query_id for query, _ in relevant])
    return build_knowledge(avoid=avoid, topics=topics, relevant=relevant, totals=totals, hits=hits)


def show_intent(ask: str) -> dict:
    """第一段：把那句话读成一份需求，打印出来。这一步花钱。

    输入：用户那句话
    输出：intent 那七个字段（已经摊成 dict）
    """
    print("=" * 74)
    print("一、understand_intent 把这句话读成了什么")
    print("=" * 74)
    print(f"用户说的：{ask}\n")

    result = read_intent(
        build_llm_client(),
        [f"用户: {ask}"],
        feedback="",
        config=load_intent_config(),
    )
    intent = to_state_dict(result)["intent"]
    for key in ("goal", "topics", "events", "exclusions", "time_said", "max_spend_usd", "unsupported"):
        print(f"    {key:<15} {intent[key]}")
    print(f"\n    它想追问的那句（这一轮没用上）：{result.clarification_question}")
    return intent


def show_draft(intent: dict, knowledge: dict):
    """第二段：真调 planner 拟词，把原始输出一条不筛地打印出来。这一步花钱。

    输入：那份需求；从库里捞出来的经验
    输出：PlanDraft
    """
    print()
    print("=" * 74)
    print("二、planner 拟出来的原始输出（一条都还没筛）")
    print("=" * 74)
    print(
        f"库里捞到的：{len(knowledge['known_queries'])} 条相关 query、"
        f"{knowledge['known_lead_total']} 家已知公司、{len(knowledge['avoid'])} 条要避开的写法\n"
    )

    draft = draft_queries(
        build_llm_client(),
        intent=intent,
        knowledge=knowledge,
        lines=[f"用户: {intent['goal']}"],
        feedback="",
        previous_plan=None,
        config=load_planner_config(),
    )
    for n, item in enumerate(draft.queries, 1):
        print(f"    {n}. {item['text']}")
        if item["filters"]:
            print(f"       旋钮 {item['filters']}")
    return draft


def show_card(intent: dict, draft) -> None:
    """第三段：筛完、报价、拼完清单，打印 Riley 会看到的那一份。不花钱。

    输入：那份需求；planner 的原始输出
    输出：没有返回值，只往终端打
    """
    config = load_planner_config()
    kept, dropped = screen_queries(
        draft.queries, exclusions=intent["exclusions"], max_queries=config.max_queries
    )
    cost = estimate_cost_usd(
        len(kept),
        max_posts_per_query=load_run_config().max_posts_per_query,
        unit_price_usd=config.unit_price_usd,
    )

    print()
    print("=" * 74)
    print("三、卡片上会是什么样")
    print("=" * 74)
    print(f"方案 v1，{len(kept)} 条 query，预计 ${cost}\n")
    for n, item in enumerate(kept, 1):
        print(f"    {n}. {item['text']}")
        if item["filters"]:
            print(f"       旋钮 {item['filters']}")
    print()
    print(build_assumptions(intent, kept=kept, dropped=dropped))
    if dropped:
        print()
        print(f"（被筛掉 {len(dropped)} 条，原因已经在上面那份清单里）")


def main() -> None:
    parser = argparse.ArgumentParser(description="真调一次模型，看一句话走到方案卡片是什么样")
    parser.add_argument("ask", nargs="?", default=DEFAULT_ASK, help="用户那句话")
    parser.add_argument(
        "--topics",
        nargs="*",
        help="跳过读需求那一步，直接手填品类词（省一次调用，也省掉 intent 那一层的干扰）",
    )
    args = parser.parse_args()

    if args.topics:
        intent = {
            "goal": args.ask,
            "topics": list(args.topics),
            "events": [],
            "exclusions": [],
            "time_said": "",
            "max_spend_usd": 0.0,
            "unsupported": [],
        }
        print("（--topics 给了，跳过 understand_intent 那一次调用）")
        print(json.dumps(intent, ensure_ascii=False, indent=2))
    else:
        intent = show_intent(args.ask)

    knowledge = fetch_knowledge(intent["topics"])
    draft = show_draft(intent, knowledge)
    show_card(intent, draft)


if __name__ == "__main__":
    main()
