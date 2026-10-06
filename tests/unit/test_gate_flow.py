"""门禁的**控制流**：失败即停、并发边界、读数先后。

为什么单给这段写用例（与 `test_gate_readings.py` 同一个理由）：这一段的错法全都**不报错**。
"静态组红了但后面的步骤照跑"是白烧几分钟（CI 上白烧几分钟 ×2 计费的 Windows 分钟）；
"pytest 与静态组其实没并发"是白等 15-30s —— 两种都是"门禁看起来正常、只是慢/只是多"，
而**没有人会为此报 bug**。所以判据钉在这里：

* 失败即停那条缝是**真的发生过**的：加了并发组之后 `break` 只跳出了结算循环，
  `rest` 照样跑（ruff 报个错还要把整套用例烧完），而文档里写着"任何一步失败即停"。

**这里曾经还有一条"pytest 与静态组真的并发"的握手用例 —— 连同它守的那条路一起删了**：
2026-10-07 实测把 pytest 也拉进并发反而更慢（整趟 142s → 254s：pytest 88s → 244s、
vitest 9s → 61s，本机 16GB 且常态 70% 占用，三档 mypy + pytest -n 4 + node 是明显超额认购）。
"能并发"不等于"该并发"—— 要再动那一格，先在满载机器上量一遍，别把这条用例当成回归。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable


def _load_gate():
    path = ROOT / "scripts" / "gate.py"
    spec = importlib.util.spec_from_file_location("gate_flow_under_test", str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub(gate, tmp_path: pathlib.Path) -> None:
    """读数落 tmp、读数键清空（假步骤名不该去撞真键表）。"""
    gate.READINGS = tmp_path / "gate-readings.json"
    gate._READING_PATTERNS = {}  # noqa: SLF001


def test_静态组红了就不再跑后面的步骤(tmp_path, monkeypatch, capsys) -> None:
    """`rest` 里那一步**必须没被启动**：用"它会不会留下一个文件"当判据。

    现场：`break` 落在结算循环里，只跳过了"处理剩下的并发结果"，没挡住后面的串行步骤。
    症状是 ruff 报个错、门禁照样把整套用例烧完（本地 15s~90s，CI 上更贵），
    而输出最后仍报红 —— 所以**没有人会发现多跑的那几步**。
    """
    gate = _load_gate()
    _stub(gate, tmp_path)
    ran = tmp_path / "consistency-ran.txt"
    gate.STEPS = [
        ("ruff", [PY, "-c", "raise SystemExit(1)"], "both"),
        ("mypy", [PY, "-c", "pass"], "both"),
        ("pytest(-x, 无覆盖率)", [PY, "-c", "print('1 passed')"], "fast"),
        ("consistency", [PY, "-c", f"open(r'{ran}', 'w').write('x')"], "both"),
    ]
    monkeypatch.setattr(sys, "argv", ["gate.py", "--fast"])
    rc = gate.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert not ran.exists(), "静态组已经红了，consistency 不该再跑（失败即停那条缝又开了）"
    assert "失败即停" in out, out


def test_读数在读数生产者之后才轮到_consistency(tmp_path, monkeypatch, capsys) -> None:
    """先后不能乱：`consistency` 要读 `backend_tests`，它必须排在 pytest **结算之后**。

    读数是"一步一份、跑完立刻落"，而 `consistency` 里比对 README 的那条尺子读的就是这个
    文件 —— 顺序反了，它比的是上一趟的数（加了用例的那趟会看见"README 与读数不一致"，
    而两边其实都对）。这条用例把顺序钉成事实：pytest 那一步故意写一行 `X passed`，
    断言 consistency 启动时读到的就是它。
    """
    gate = _load_gate()
    _stub(gate, tmp_path)
    seen = tmp_path / "seen.txt"
    readings_file = tmp_path / "gate-readings.json"
    reader = (
        "import json, pathlib\n"
        f"data = json.loads(pathlib.Path(r'{readings_file}').read_text('utf-8'))\n"
        f"pathlib.Path(r'{seen}').write_text(str(data.get('backend_tests')), encoding='utf-8')\n"
    )
    gate._READING_PATTERNS = {"pytest(-x, 无覆盖率)": (r"(\d+) passed", "backend_tests")}  # noqa: SLF001
    gate.STEPS = [
        ("ruff", [PY, "-c", "pass"], "both"),
        ("mypy", [PY, "-c", "pass"], "both"),
        ("pytest(-x, 无覆盖率)", [PY, "-c", "print('42 passed in 1s')"], "fast"),
        ("consistency", [PY, "-c", reader], "both"),
    ]
    monkeypatch.setattr(sys, "argv", ["gate.py", "--fast"])
    rc = gate.main()
    assert rc == 0, capsys.readouterr().out
    assert seen.read_text(encoding="utf-8") == "42", "consistency 读到的必须是这一趟刚落的读数"
