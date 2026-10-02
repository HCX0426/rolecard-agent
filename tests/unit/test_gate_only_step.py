"""`gate.py --only` 打错步骤名时不许报绿（10-02 轮 `R102-21`）。

症状是这条用例存在的全部理由：子串匹配不到任何步骤 ⇒ 循环一步不进 ⇒ 末尾只看
`failures` 的那句"✅ 全部通过"照样打出来、退出码 0。补读数的人拿着"绿"去改文档，
而实际上一个数都没重量 —— 与 `R28-64` 那族"少一个键长得像这一档本来不量它"是同一件事，
只是这一次连读数都不写。
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
    gate._run = run or (lambda name, cmd, cwd=None: (True, 0.01, ""))
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

    def spy(name, cmd, cwd=None):
        ran.append(name)
        return True, 0.01, ""

    steps = [("一致性 检查", ["x", "y"], "both"), ("前端 vitest", ["a", "b"], "full")]
    code = _main_with(gate, steps, ["--only", "一致性"], tmp_path, run=spy)
    assert code == 0, f"命中了步骤却可乐：{code}"
    assert ran == ["一致性 检查"], f"该跑的没跑或跑多了：{ran}"


def test_the_real_step_table_has_unique_names():
    """子串选择器靠名字吃饭：步骤撞名时 `--only` 会一次跑两遍、读数互相盖。"""
    gate = _load_gate()
    names = [name for name, _, _ in gate.STEPS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"步骤名重复：{dupes}"
