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
    gate._write_readings({"前端 vitest": colored}, True)  # noqa: SLF001
    assert _read(gate)["frontend_tests"] == "336"


def test_pytest_summary_yields_the_passed_count(tmp_path):
    gate = _load_gate()
    _stub(gate, tmp_path)
    out = "1319 passed, 1 skipped, 3 deselected in 134.56s\n"
    gate._write_readings({"pytest(-x, 无覆盖率)": out}, True)  # noqa: SLF001
    data = _read(gate)
    assert data["backend_tests"] == "1319"
    # 口径钉在这里：读的是**通过**数，不是 collect-only 的收集数（两者差的正是那条 skip）。
    assert data["backend_tests_at"]


def test_a_step_that_ran_but_matched_nothing_leaves_a_marker(tmp_path):
    gate = _load_gate()
    _stub(gate, tmp_path)
    gate._write_readings({"pytest(-x, 无覆盖率)": "1326 passed, 1 skipped in 190s\n"}, True)  # noqa: SLF001
    gate._write_readings({"pytest(-x, 无覆盖率)": "没有数字的一份输出\n"}, True)  # noqa: SLF001
    data = _read(gate)
    assert "backend_tests_unreadable" in data
    assert data["backend_tests"] == "1326"  # 记号只说"这次没量到"，不抹掉上一次的真读数


def test_a_step_that_never_ran_is_left_alone(tmp_path):
    gate = _load_gate()
    _stub(gate, tmp_path)
    cov = "Required test coverage of 85% reached. Total coverage: 91.89%\n"
    gate._write_readings({"pytest(覆盖率≥85%)": cov}, True)  # noqa: SLF001
    gate._write_readings({"前端 vitest": "Tests  336 passed (336)\n"}, True)  # noqa: SLF001
    data = _read(gate)
    assert data["coverage_percent"] == "91.89"  # 并发/分档跑：后一趟没跑那一步就不该动它的键
    assert "coverage_percent_unreadable" not in data
    assert data["frontend_tests"] == "336"


def test_the_assertion_count_is_not_a_reading(tmp_path: Path) -> None:
    """「一致性有几条断言」不许进读数机：那串数里含比对 README 这一条自己，是**自指**。

    README 漂 ⇒ 那条断言红 ⇒ 读到的数少 1 ⇒ 那句变成两处错；把它改对，下一趟又回到原值。
    10-01 实测打过一轮这个转圈（台账 `R28-69`），所以这一格退回门禁输出，谁都不抄。
    """
    gate = _load_gate()
    keys = {key for _, key, *_ in gate._READING_PATTERNS.values()}  # noqa: SLF001
    assert "consistency_assertions" not in keys
    source = (ROOT / "scripts" / "check_consistency.py").read_text(encoding="utf-8")
    start = source.index("def check_readme_headline_numbers")
    body = source[start : start + 3000]
    assert '"consistency_assertions"' not in body, "README 那条比对又去读自指的条数了"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "一致性 4" not in readme and "一致性 1" not in readme, "README 首屏又抄回那串自指的数"

def test_readings_path_moves_off_the_tracked_file_on_a_runner(tmp_path: Path, monkeypatch) -> None:
    """runner 不许回写入库那份读数 —— 覆盖率是**按平台**的数（`R28-74`）。

    本机 win32 量到 91.92%，Linux runner 量到 91.77%，两边都对。入库那份记的是
    "README 抄的是谁量的那一次"，让第二个机器覆盖它，等于两份事实互相把对方判成漂移
    （10-01 那发 CI 红就是这么来的：收尾那步拿 runner 刚写的 91.77 去比 README 的 91.92）。
    """
    gate = _load_gate()
    monkeypatch.delenv("GATE_READINGS_SCRATCH", raising=False)
    assert gate._readings_path() == gate.ROOT / "docs" / "gate-readings.json"  # noqa: SLF001
    monkeypatch.setenv("GATE_READINGS_SCRATCH", "1")
    scratch = gate._readings_path()  # noqa: SLF001
    assert scratch == gate.ROOT / "build" / "gate-readings.json"  # noqa: SLF001
    assert "docs" not in scratch.parts


def test_a_failed_step_produces_no_reading_but_leaves_a_red_mark(tmp_path: Path) -> None:
    """红掉的那一步**不算量到了**：`1 failed, 957 passed` 里那个 957 会被同一个正则读走。

    10-01 实测撞到的：一次 chroma 偶发失败把 `backend_tests` 从 1326 洗成 957，而 README 那一格
    才是对的 —— 于是这条守卫差一点反过来把人对的那一格判成漂移。

    R102-36 半条（10-03 收）：红跑不写值，但要留"这一步红过"的痕 —— 从前静默退场，
    旧读数被钉在原地而没有任何一格说明最近一趟是红的。
    """
    gate = _load_gate()
    _stub(gate, tmp_path)
    gate._write_readings({"pytest(-x, 无覆盖率)": "1326 passed, 1 skipped in 190s\n"}, True)  # noqa: SLF001
    gate._write_readings(  # noqa: SLF001
        {"pytest(-x, 无覆盖率)": "1 failed, 957 passed, 1 skipped in 158.34s\n"}, False
    )
    data = _read(gate)
    assert data["backend_tests"] == "1326", "失败那一步的半截数字不该盖掉上一次的真读数"
    assert "backend_tests_unreadable" not in data, "问题不在输出格式，别打'没量到'的记号"
    assert data["backend_tests_red_at"], "红跑必须留痕：否则旧值被钉住而没人知道（R102-36 半条）"


def test_a_green_write_clears_the_red_mark(tmp_path: Path) -> None:
    """红被绿取代才算翻篇（`R102-36` 半条）：记号只回答"最近一趟红没红"。

    下一次这个键量到新值时必须清掉 `_red_at` —— 否则它会一直喊狼来了，第二次就没人看。
    """
    gate = _load_gate()
    _stub(gate, tmp_path)
    gate._write_readings({"pytest(-x, 无覆盖率)": "1 failed, 957 passed\n"}, False)  # noqa: SLF001
    gate._write_readings(  # noqa: SLF001
        {"pytest(-x, 无覆盖率)": "1330 passed, 1 skipped in 190s\n"}, True
    )
    data = _read(gate)
    assert data["backend_tests"] == "1330"
    assert "backend_tests_red_at" not in data, "绿跑量到新值后红痕必须清掉"


def test_evidence_retry_does_not_poison_the_count(tmp_path: Path) -> None:
    """取证放行的那趟**量不到全量数**：不写这个键，旧值连旧 `_at` 一起留着。

    现场（10-04 实测，输入照抄那次门禁的 stdout）：chroma 偶发命中在册签名 → 首跑带
    `-x` 截断在 `1 failed, 1139 passed`，二跑只跑那一个文件打出 `32 passed`，两行**都
    不是全量**（真值 1524 这趟根本没出现）。读数机从前取匹配的最后一条，把
    `backend_tests` 洗成 32 —— 一致性拿它去比 README 1524，把**对的**那一格判漂。
    读数的诚实性在时间戳上：这趟没量到就不动 `_at`，下一趟干净绿跑刷新；`head` 照常
    更新（这一步确实绿了，只是这个键没数）。
    """
    gate = _load_gate()
    _stub(gate, tmp_path)
    gate._write_readings(  # noqa: SLF001
        {"pytest(-x, 无覆盖率)": "1524 passed, 1 skipped in 60.1s\n"}, True
    )
    before = _read(gate)
    assert before["backend_tests"] == "1524"

    evidence_output = (
        "⚠️  fast 档首跑红，且失败形状命中在册的 chroma 偶发（`R102-41`）。"
        "重跑那 1 个文件一次取证：['tests/unit/test_rag.py']\n"
        "1 failed, 1139 passed, 1 skipped in 58.75s\n"
        "⚠️  FLAKY-RECORDED：同一批文件二跑绿。这一趟按「未知」放行。\n"
        "32 passed in 2.05s\n"
    )
    gate._write_readings({"pytest(-x, 无覆盖率)": evidence_output}, True)  # noqa: SLF001
    after = _read(gate)
    assert after["backend_tests"] == "1524", "取证子集的 32 不许盖掉上一趟的全量读数"
    assert after["backend_tests_at"] == before["backend_tests_at"], "没量到就不许挪时间戳"
    assert after["head"] == "0123456789ab", "head 照常更新：这一步确实绿了"
    assert "backend_tests_unreadable" not in after, (
        "这不是'输出格式读不出'，是'这趟没量到' —— 打 unreadable 记号会让一致性去红，"
        "而取证放行本来就该按未知通过"
    )
