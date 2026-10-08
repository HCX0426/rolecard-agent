"""`gate.py --only` 零步时不许报绿（10-02 轮 `R102-38`；同族第三格 10-03 补钉）。

症状是这条用例存在的全部理由：子串匹配不到任何步骤 ⇒ 循环一步不进 ⇒ 末尾只看
`failures` 的那句"✅ 全部通过"照样打出来、退出码 0。补读数的人拿着"绿"去改文档，
而实际上一个数都没重量 —— 与 `R28-64` 那族"少一个键长得像这一档本来不量它"是同一件事，
只是这一次连读数都不写。

第三格（10-03 终局序列当场撞出）：子串**命中**了名字、却不在当前档位会跑的名单里
（`--only "pytest(-x"` 默认 full 档不带 fast 步骤）—— 零步的第二种零法，症状一字不差。
修法让守卫与循环共用同一条谓词，两条零法都回 2 并说清怎么跑起来。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "gate_under_test", str(ROOT / "scripts" / "gate.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _main_with(gate, steps, argv, tmp_path, run=None):
    """换掉步骤表、掐掉真跑命令与读数落盘，只问 `main()` 的返回码。

    `_READING_PATTERNS` 一起换空：假步骤表里没有读数步骤的名字，那道既有守卫会先一步可乐，
    这条用例就想问 `--only` 那一格，别被邻居拦住。
    """
    real = (
        gate.STEPS,
        gate._run,
        gate._write_readings,
        gate.READINGS,
        gate._READING_PATTERNS,
        sys.argv,
    )
    gate.STEPS = steps
    gate._READING_PATTERNS = {}
    gate._run = run or (lambda name, cmd, cwd=None, **_k: (True, 0.01, ""))
    gate._write_readings = lambda *a, **k: None
    gate.READINGS = tmp_path / "readings.json"
    sys.argv = ["gate.py", *argv]
    try:
        return gate.main()
    finally:
        (
            gate.STEPS,
            gate._run,
            gate._write_readings,
            gate.READINGS,
            gate._READING_PATTERNS,
            sys.argv,
        ) = real


def test_a_typo_in_only_is_a_loud_failure_not_green(tmp_path, capsys):
    gate = _load_gate()
    steps = [("一致性", ["x", "y"], "fast")]
    code = _main_with(gate, steps, ["--only", "不存在的那一步xyz"], tmp_path)
    assert code == 2, f"零步命中本该可乐（回 2），实际回了 {code}"
    out = capsys.readouterr().out
    assert "--only" in out and "一致性" in out, "红了要说清红在哪：把现有步骤名甩出来"


def test_a_matching_substring_still_runs_that_step(tmp_path):
    """反向对照：这条不许被"全拦"实现 —— 命中子串时那一步必须真的跑过。"""
    gate = _load_gate()
    ran: list[str] = []

    def spy(name, cmd, cwd=None, timeout=None, remaining=None):
        ran.append(name)
        return True, 0.01, ""

    steps = [("一致性 检查", ["x", "y"], "both"), ("前端 vitest", ["a", "b"], "full")]
    code = _main_with(gate, steps, ["--only", "一致性"], tmp_path, run=spy)
    assert code == 0, f"命中了步骤却可乐：{code}"
    assert ran == ["一致性 检查"], f"该跑的没跑或跑多了：{ran}"


def test_one_static_step_alone_still_runs(tmp_path) -> None:
    """命中静态组里的**一步**时不许零步当绿 —— 2026-10-09 实测撞出来的第四种零法。

    现场：\gate.py --ci --only "mypy(linux"\ 打出"✅ 全部通过"、退出 0、总计 0.0s。并发组的
    启动条件是 \len(head) >= 2\，只命中一步时并发不启动，而那一步又被旧写法
    est = [非静态]\ 从串行名单里摘掉了 —— 分组的账漏了人。上面那道 \--only\ 守卫拦不住
    它：守卫问"这趟会不会跑"（答"会"），这里的账是"谁真的跑了"（一个都没有）。
    """
    gate = _load_gate()
    ran: list[str] = []

    def spy(name, cmd, cwd=None, timeout=None, remaining=None):
        ran.append(name)
        return True, 0.01, ""

    steps = [("mypy", ["x"], "both"), ("ruff", ["y"], "both"), ("依赖方向", ["z"], "both")]
    code = _main_with(gate, steps, ["--ci", "--only", "mypy"], tmp_path, run=spy)
    assert code == 0, f"命中了静态组里的一步却可乐：{code}"
    assert ran == ["mypy"], f"这一步该被真跑（从前零步当绿）：{ran}"


def test_only_hitting_a_step_this_tier_filters_out_is_not_green(tmp_path, capsys):
    """命中 ≠ 会跑：fast 档步骤在默认 full 档被过滤 —— 零步依旧不许当通过（10-03）。

    实测现场：`gate.py --only "pytest(-x"`（fast 档步骤、不加 `--fast`）0.0s 打出
    "✅ 全部通过"、退出 0，一个数都没重量 —— 与"打错名"是同一症状的第二道门。
    """
    gate = _load_gate()
    ran: list[str] = []

    def spy(name, cmd, cwd=None, timeout=None, remaining=None):
        ran.append(name)
        return True, 0.01, ""

    steps = [("一致性", ["x", "y"], "fast")]
    code = _main_with(gate, steps, ["--only", "一致性"], tmp_path, run=spy)
    assert code == 2, f"命中了却一步不跑，本该可乐（回 2），实际回了 {code}"
    out = capsys.readouterr().out
    assert "--fast" in out and "一致性" in out, "红了要说清命中哪一步、以及怎么让它真的跑"
    assert ran == [], "零步不许真跑"


def test_fast_flag_makes_the_fast_step_runnable(tmp_path):
    """反向对照：给出的修法（加 `--fast`）必须真的能把它跑起来 —— 提示不是空话。"""
    gate = _load_gate()
    ran: list[str] = []

    def spy(name, cmd, cwd=None, timeout=None, remaining=None):
        ran.append(name)
        return True, 0.01, ""

    steps = [("一致性", ["x", "y"], "fast")]
    code = _main_with(gate, steps, ["--fast", "--only", "一致性"], tmp_path, run=spy)
    assert code == 0, f"加了 --fast 还可乐：{code}"
    assert ran == ["一致性"], f"该跑的没跑或跑多了：{ran}"


def test_the_real_step_table_has_unique_names():
    """子串选择器靠名字吃饭：步骤撞名时 `--only` 会一次跑两遍、读数互相盖。"""
    gate = _load_gate()
    names = [name for name, _, _ in gate.STEPS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"步骤名重复：{dupes}"
