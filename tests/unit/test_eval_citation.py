"""评测环里"数据引用"那一类断言的尺子本身（`scripts/run_eval.py` 的 `_check_assertions`）。

为什么单独给尺子写用例：这一格的通过率是**从 0 开始建立**的，第一版就有两个洞，
而两个洞都不会让任何用例变红 —— 它们只会让数字好看：

1. `_numbers_in` 原先把文本里所有数字都当引用，于是 `2026-03-12` 里的 6 和 12
   能冒充"结石直径 6.0mm"——**只写了复查日期、压根没说直径的回答会判为通过**；
2. 只有"必须出现 6.0"这一半，没有"不许出现 5.0"那一半，于是
   「6.0 还是 5.0 呢？」这种把两份报告一起端出来的回答也算对。

判据就是这两条：把"没说"读成"说了"、把"说错"读成"说对"，都必须当场变红。
"""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "run_eval_u", ROOT / "scripts" / "tools" / "run_eval.py"
)
assert _spec and _spec.loader
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def _check(assertions: list[dict[str, object]], text: str) -> tuple[list[str], tuple[int, int]]:
    return ev._check_assertions(assertions, invoked=["query_health_record"], final_text=text)


def test_a_bare_date_is_not_a_citation() -> None:
    """日期里的数字不算引用 —— 否则"只报了复查日期"会被判成"报了直径"。"""
    assert ev._numbers_in("2026-03-12 那次复查") == []
    assert ev._numbers_in("直径 6.0mm") == [6.0]
    assert ev._numbers_in("2025 年 5 月那次") == [], "年月日式的说法整块剥掉"
    assert ev._numbers_in("2026 年的报告") == [2026], "光一个年份没有日期形状，仍按数字算"

    fails, cit = _check(
        [{"kind": "answer_contains_value", "value": 6.0, "tolerance": 0.05}],
        "看到 2026-03-12 有复查记录，具体数值我这边没显示出来。",
    )
    assert fails, "旧版会在这里判通过：6 来自日期"
    assert cit == (0, 1)


def test_the_absent_half_catches_the_both_numbers_answer() -> None:
    """"不许出现 5.0"是反向那一半：只断言"要有 6.0"，端出两个数的回答也算对。"""
    both = "这次是 6.0mm，去年那次是 5.0mm。"
    fails, cit = _check(
        [
            {"kind": "answer_contains_value", "value": 6.0, "tolerance": 0.05},
            {"kind": "answer_absent_value", "value": 5.0, "tolerance": 0.05},
        ],
        both,
    )
    assert cit == (1, 2), "对的那个算过、串过来的那个算错 —— 分母是断言条数不是用例数"
    assert any("不该引用" in f for f in fails), fails

    ok_fails, ok_cit = _check(
        [
            {"kind": "answer_contains_value", "value": 6.0, "tolerance": 0.05},
            {"kind": "answer_absent_value", "value": 5.0, "tolerance": 0.05},
        ],
        "2026-03-12 复查，结石直径 6.0mm。",
    )
    assert ok_fails == [] and ok_cit == (2, 2)


def test_the_citation_slice_only_counts_value_assertions_and_stays_out_of_the_rest() -> None:
    """引用切片只数 `answer_*_value`：工具与标记类断言不进它的分母。

    这一条是"数据引用正确率"这一格能不能独立解读的全部前提 —— 混进总通过率的话，
    一条"工具调对了、数抄错了"的用例会把两件事压成一个数，看不出该修哪一件。
    """
    fails, cit = _check(
        [
            {"kind": "tool_called", "name": "query_health_record"},
            {"kind": "answer_contains_marker", "marker": "未经人工校验"},
            {"kind": "answer_contains_value", "value": 6.0, "tolerance": 0.05},
        ],
        "6.0mm，未经人工校验。",
    )
    assert cit == (1, 1)
    assert fails == []

    fails2, cit2 = _check([{"kind": "tool_called", "name": "query_health_record"}], "随便说")
    assert cit2 == (0, 0), "不涉及引用的用例必须是 0/0 —— 它不该进分子也不该进分母"
    assert fails2 == []


def test_an_unknown_assertion_kind_is_still_a_failure() -> None:
    """未知断言类型不许静默通过（写错 kind 的代价是"这条永远绿"）。"""
    fails, _ = _check([{"kind": "answer_contains_number", "value": 6.0}], "6.0")
    assert any("未知断言类型" in f for f in fails)


def test_pairing_is_what_catches_the_right_number_wrong_report() -> None:
    """配对断言：数要挨着对的年份。"文本里有 6"看不出它把 6 算给了哪一次检查。"""
    right = "你 2026-03-12 的报告里结石直径 6 mm，2025 年那次是 5 mm。"
    wrong = "你 2025-05-01 的报告里结石直径 6 mm。"
    ok_fails, ok_cit = _check(
        [{"kind": "answer_value_near_marker", "value": 6.0, "marker": "2026", "window": 40}], right
    )
    assert ok_fails == [] and ok_cit == (1, 1), "两个数都报了、但配对正确 —— 这必须算对"
    bad_fails, bad_cit = _check(
        [{"kind": "answer_value_near_marker", "value": 6.0, "marker": "2026", "window": 40}], wrong
    )
    assert bad_cit == (0, 1) and any("没有挨着" in f for f in bad_fails), bad_fails


def test_a_marker_list_accepts_synonyms() -> None:
    """标记给一组同义说法：只认「没有」会把「档案里没存过」判错。"""
    fails, _ = _check(
        [{"kind": "answer_contains_marker", "marker": ["没有", "没存", "未记录"]}],
        "档案里没存过血糖这项。",
    )
    assert fails == []
    fails2, _ = _check(
        [{"kind": "answer_contains_marker", "marker": ["没有", "没存"]}], "血糖偏高一点点。"
    )
    assert any("缺少标记之一" in f for f in fails2)


def test_absent_value_is_sharp_enough_to_cut_a_correct_answer() -> None:
    """这条钉的是**为什么不在参考区间边上用反向断言**。

    直径的参考区间是 `0-5`，于是"6 mm（参考 0-5）"这句完全正确的回答里躺着 5 和 0。
    第一版 cite-002 禁的就是 5.0 —— 三遍全挂，而模型三遍都答对了。断言本身没错，
    错在拿它去禁一个合法表述里必然出现的数。所以这里把这个坑钉成用例：
    它证明 `answer_absent_value` 判得动，也证明用它之前要先确认那个数不出现在区间里。
    """
    correct = "2026-03-12 复查，结石直径 6 mm（参考区间 0-5）。"
    fails, cit = _check([{"kind": "answer_absent_value", "value": 5.0, "tolerance": 0.05}], correct)
    assert cit == (0, 1) and any("不该引用" in f for f in fails), "参考上限把反向断言点着了"
    ok_fails, ok_cit = _check(
        [{"kind": "answer_absent_value", "value": 6.1, "tolerance": 0.02}], correct
    )
    assert ok_cit == (1, 1) and ok_fails == [], "换成记录里真没有的数，同一条回答就过了"
