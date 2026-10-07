"""打印 interactive discovery 那张图的形状。改完图跑一次，肉眼对一遍。

    python -m scripts.show_graph

输出的是 mermaid 文本，贴进 mermaid.live 就能看到图。实线是固定边，虚线是
条件边，虚线上的字是那条分支的名字。

这里问的是「图长什么样」，跟有没有对话停在 interrupt 上无关。要问「某条对话现在停在哪
一步」是另一件事，用 app.get_state(config).next。

想在终端里直接画成 ASCII 图的话是 app.get_graph().print_ascii()，但那要先
装 grandalf（现在没装）。draw_mermaid_png() 会联网调 mermaid.ink，不用。
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from triggers.interactive_discovery.query.graph import build_graph


def main() -> None:
    # 只是为了编译出图来看形状，用哪个 checkpointer 都不影响输出。
    app = build_graph(InMemorySaver())
    print(app.get_graph().draw_mermaid())


if __name__ == "__main__":
    main()
