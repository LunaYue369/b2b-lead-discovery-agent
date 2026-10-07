"""trigger 里唯一有判断的那一段：配置读得对不对。

它的主体是编排，而编排的正确性靠真库跑（--replay --no-persist 那条路），
不靠单元测试 —— 那正是 persistence/base.py 里划的那条分工线。
这里只焊住「配置缺键不许退化成默认值」，因为那种错不报错，
症状是「实际采的条数跟配置说的不一样」。
"""

from __future__ import annotations

import pytest

from triggers.harvest import load_run_config
from triggers.scheduled_discovery.main import main


def test_the_real_config_file_loads():
    """仓库里那份 discovery.yaml 要能被读出来，键名对不上当场知道。"""
    config = load_run_config()
    assert config.max_posts_per_query >= 1
    assert config.seen_days >= 1
    assert 0 <= config.review_score <= 100


def test_a_missing_key_blows_up_instead_of_defaulting(tmp_path):
    """键缺了当场 KeyError。

    退化成默认值的后果：明天有人把 max_posts_per_query 从 40 改成 10，
    发现线索变少之后先怀疑的是 query，不会想到配置根本没被读。
    """
    path = tmp_path / "discovery.yaml"
    path.write_text("max_posts_per_query: 40\nseen_days: 10\n", encoding="utf-8")
    with pytest.raises(KeyError):
        load_run_config(path)


def test_replay_without_no_persist_is_refused():
    """回放不带 --no-persist 要被当场拦掉，不许静默落库。

    argparse 的 error() 会退出进程，所以这里等的是 SystemExit 而不是
    某个自定义异常。拦在读配置和连库之前，所以这条测试不碰数据库。

    焊住它的理由：回放的是几周前那批数据，落进 leads 表之后那些公司
    永久算「已经见过」，novel_lead_count 大面积归零 —— 而那一列决定
    60% 的采集预算，且报表上完全看不出来。
    """
    with pytest.raises(SystemExit):
        main(["--replay", "data/harvest_2026-08-27_010047.json"])


def test_replay_with_no_persist_gets_past_the_guard(monkeypatch):
    """两个开关都给的时候，拦截不该挡路。

    只验「过了那道闸」，不真跑整条链：把读配置那一步换成炸的，
    过了闸就会撞上它。用 RuntimeError 这个跟 SystemExit 无关的类型
    来区分「被闸拦住」和「闸放行了」。
    """

    def blow_up():
        raise RuntimeError("过闸了")

    monkeypatch.setattr("triggers.scheduled_discovery.main.load_selection_config", blow_up)
    with pytest.raises(RuntimeError, match="过闸了"):
        main(["--replay", "data/harvest_2026-08-27_010047.json", "--no-persist"])


def _assessment(score, icp_fit):
    """造一条打完分的结果，只有报告要看的那几个字段是有意义的。"""
    from models import Assessment, DeterministicScores, LlmAssessment

    return Assessment(
        llm=LlmAssessment(
            icp_fit=icp_fit,
            signal_strength=8,
            seniority=8,
            moq_fit=8,
            analysis="x",
            evidence_quotes=("y",),
        ),
        det=DeterministicScores(recency=4, reachability=2, corroboration=6),
        score=score,
    )


def test_the_annotation_band_leaves_out_category_mismatches(capsys):
    """边界档不许收品类完全不对的公司。

    那一档是攒给 Riley 标注的样本（阈值要由她标 20-50 条反推），所以里面
    必须都是「品类对、只是时机或者信号强弱还说不清」的那种。品类不对的
    在库里已经被 cooldown 判成终态、永不复查了，报告说的话不能跟它相反。

    输入   两家公司同样是 48 分，一家品类对、一家 icp_fit 是 0
    输出   汇总那一行只数进 1 家边界；清单里品类不对那家不带「边界」标记
    """
    from models import DiscoveryResult, RunStatus
    from triggers.harvest import load_run_config
    from triggers.scheduled_discovery.main import report_result

    result = DiscoveryResult(
        run_id=1,
        status=RunStatus.COMPLETED,
        per_query=(),
        scored={"name:in-range": _assessment(48, 6), "name:off-category": _assessment(48, 0)},
        lead_ids={"name:in-range": 10, "name:off-category": 11},
        discarded={},
        warnings=(),
    )
    report_result(result, run_config=load_run_config(), qualified_score=60)

    printed = capsys.readouterr().out
    assert "边界（40-60）1 家" in printed
    # 两家都在清单里，但只有品类对的那家带标记
    assert "name:off-category" in printed
    marked = [line for line in printed.splitlines() if "边界" in line and "lead=" in line]
    assert len(marked) == 1
    assert "name:in-range" in marked[0]


def test_main_finds_contacts_before_pushing_and_does_not_let_it_block_the_push():
    """main 里：find_contacts_for_run 在 push_digest 前面，而且包在 try 里（出错照样推卡）。

    这段代码在测试里跑不到（要连库、花钱），所以用 ast 看它的形状：
        顺序反了      卡片拼的时候读不到这一次刚找到的联系方式
        没包 try      找联系方式出错那一天，一张卡片都推不出去
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "triggers" / "scheduled_discovery" / "main.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")

    def calls(name):
        return [
            n for n in ast.walk(main)
            if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == name
        ]

    find, push = calls("find_contacts_for_run"), calls("push_digest")
    assert len(find) == 1 and len(push) == 1
    assert find[0].lineno < push[0].lineno

    # 那一句在某个 try 的 body 里，try 有 except
    guarded = [
        t for t in ast.walk(main)
        if isinstance(t, ast.Try) and t.handlers and any(n is find[0] for s in t.body for n in ast.walk(s))
    ]
    assert guarded, "find_contacts_for_run 没有包在 try 里"
