"""`scripts/gate.py` 那台"读数机"的测试：它安静地少一个键，比它报错更坏。

为什么单给这段写用例（与 `test_bundle_parity.py` 同一个理由）：它防的是 **README 首屏那组数没人量**
（审计 `R28-55` 的第二次收口 `R28-64`）。而它已经以两种互不相同的方式失败过一次 ——
pytest 的 quiet 是累加的，命令行再补一个 `-q` 就是 verbosity −2，那行 `N passed` 连同失败时的
`FAILED tests/...` 一起被吞；vitest 的输出被 pipe 也照样带 ANSI，`Tests  336 passed` 在字节上
读不出 336。两种故障的**症状都是"少一个键"**，而少一个键与"这一档本来不量它"长得一模一样：
门禁全绿、README 一路漂。所以这里钉的是三件事：命令行的形状、剥色之后再匹配、"没跑"与
"跑了没量到"必须分成两种落点。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_gate():
    path = ROOT / "scripts" / "gate.py"
    spec = importlib.util.spec_from_file_location("gate_under_test", str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub(mod, tmp_path: Path):
    """把读数写到 tmp，并掐掉真实 git（读数机不该依赖仓库状态才有测试意义）。"""
    mod.READINGS = tmp_path / "gate-readings.json"
    mod._git = lambda *a: "0123456789abcdef"  # noqa: SLF001


def _read(mod):
    return json.loads(mod.READINGS.read_text(encoding="utf-8"))


def test_pytest_steps_do_not_stack_quiet_flags():
    """`addopts` 已经有一个 `-q`，命令里再补一个就会把汇总行整个吞掉（实测中招过）。"""
    gate = _load_gate()
    addopts = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'addopts = "-q' in addopts, "pyproject 的 addopts 形状变了，这条用例的前提要重看"
    pytest_steps = [cmd for name, cmd, _ in gate.STEPS if name.startswith("pytest")]
    assert pytest_steps, "门禁里找不到 pytest 步骤"
    for cmd in pytest_steps:
        assert "-q" not in cmd, f"{cmd} 又补了一个 -q：与 addopts 叠成 verbosity -2，汇总行会消失"


def test_reading_keys_must_be_real_step_names():
    gate = _load_gate()
    assert set(gate._READING_PATTERNS) <= {name for name, _, _ in gate.STEPS}  # noqa: SLF001


def test_vitest_summary_is_read_even_though_it_is_colored(tmp_path):
    gate = _load_gate()
    _stub(gate, tmp_path)
    # 取自实测的那一行：vitest 被 pipe 也照样上色，数字前后都是转义序列。
    prefix = "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m"
    colored = prefix + "336 passed\x1b[39m\x1b[90m (336)\x1b[39m\n"
    gate._write_readings({"前端 vitest": colored})  # noqa: SLF001
    assert _read(gate)["frontend_tests"] == "336"


def test_pytest_summary_yields_the_passed_count(tmp_path):
    gate = _load_gate()
    _stub(gate, tmp_path)
    out = "1319 passed, 1 skipped, 3 deselected in 134.56s\n"
    gate._write_readings({"pytest(-x, 无覆盖率)": out})  # noqa: SLF001
    data = _read(gate)
    assert data["backend_tests"] == "1319"
    # 口径钉在这里：读的是**通过**数，不是 collect-only 的收集数（两者差的正是那条 skip）。
    assert data["backend_tests_at"]


def test_a_step_that_ran_but_matched_nothing_leaves_a_marker(tmp_path):
    gate = _load_gate()
    _stub(gate, tmp_path)
    gate._write_readings({"consistency": "assertions: 44 passed, 0 failed\n"})  # noqa: SLF001
    gate._write_readings({"consistency": "没有数字的一份输出\n"})  # noqa: SLF001
    data = _read(gate)
    assert "consistency_assertions_unreadable" in data
    assert data["consistency_assertions"] == "44"  # 记号只说"这次没量到"，不抹掉上一次的真读数


def test_a_step_that_never_ran_is_left_alone(tmp_path):
    gate = _load_gate()
    _stub(gate, tmp_path)
    cov = "Required test coverage of 85% reached. Total coverage: 91.89%\n"
    gate._write_readings({"pytest(覆盖率≥85%)": cov})  # noqa: SLF001
    gate._write_readings({"consistency": "assertions: 45 passed, 0 failed\n"})  # noqa: SLF001
    data = _read(gate)
    assert data["coverage_percent"] == "91.89"  # 并发/分档跑：后一趟没跑那一步就不该动它的键
    assert "coverage_percent_unreadable" not in data
    assert data["consistency_assertions"] == "45"
