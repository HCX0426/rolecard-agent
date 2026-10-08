"""`gate.py` 的每步超时与全局墙钟（2026-10-09 两趟"什么都没报错的 cancel"换来的）。

形状：一步吊住不动 → runner 在 job 预算上把整条 job 杀掉 → **GitHub 对 cancelled job 不上传
日志**（run 37804463056 与 37815831250 连续两趟，现场两次都拿不回来，只能从相邻臂倒推）。
所以挂死必须变成**这一步的红**（带 ⏱、带截止此刻的输出、整趟照常打汇总、日志照常上传），
而不是整条 job 陪葬。两格各测一个方向 —— 只测"挂死会红"是不够的，那会把**正常但很吵**的
步骤也读成挂死（见下）。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_gate():
    spec = importlib.util.spec_from_file_location("gate_to", str(ROOT / "scripts" / "gate.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_chatty_but_healthy_step_is_not_read_as_hung() -> None:
    """输出远超 OS 管道缓冲（~4–64KB）的正常一步，不许被读成挂死。

    这是"读流必须与等待**并发**"那句注释的证据。反证实测过：主线程 `proc.wait(timeout)`
    而不并发读时，这一格（15000 行 / 约 3MB，本仓 pytest 一步就是这个量级）在 12s 预算里
    **必然到点** —— 子进程写满管道后阻塞在 write 上，wait 等不到退出。谁把泵线程"简化"回
    顺序读，这条当场红。
    """
    gate = _load_gate()
    chatty = "import time;[print('x' * 200) for _ in range(15000)];time.sleep(0.3)"
    ok, _dt, out = gate._run("吵但不挂", [sys.executable, "-c", chatty], timeout=45.0)
    assert ok is True, "正常但输出巨大的步骤被读成挂死 = 管道死锁，读流没有并发"
    assert out.count("\n") > 14000, "输出被截断：泵线程提前收了"


def test_a_genuinely_hung_step_goes_red_within_its_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """真挂死的一步：几秒内红、挂死前的输出留着、点名那句在 —— 现场不再丢。"""
    gate = _load_gate()
    hang = "import sys,time;print('开工');sys.stdout.flush();time.sleep(600)"
    t0 = time.perf_counter()
    ok, _dt, out = gate._run("挂死的那步", [sys.executable, "-c", hang], timeout=3.0)
    wall = time.perf_counter() - t0
    assert ok is False, "挂死的一步必须红"
    assert 2.0 < wall < 25.0, f"到点没收掉进程树（墙上 {wall:.1f}s）"
    assert "开工" in out, "挂死前打出的输出要原样留着 —— 这整个机制就是为了留现场"
    assert "没跑完" in out
    capsys.readouterr()  # 吞掉流式打印，别灌进测试输出


def test_the_timeout_table_names_are_the_step_table_prefixes() -> None:
    """`STEP_TIMEOUTS` 的键必须对得上 `STEPS` 的真名（含"快档那步带显示后缀"这一格）。

    查不到不报错 —— 只是那一步的上限**静默退回默认值**，与"这一档本来不设上限"长得一样。
    快档的 pytest 到 `_run` 手里时带着 `｜受影响子集…` 后缀（`_resolve` 加的），所以查表
    先摘后缀；这条把后缀形状一起钉住（假名拼不出来 —— 用真表里那条的名字）。
    """
    gate = _load_gate()
    names = [n for n, _, _ in gate.STEPS]
    for key in gate.STEP_TIMEOUTS:
        assert any(n == key or key in n for n in names), f"超时表里的键对不上任何步骤：{key!r}"
    # 后缀形状：显示名查表必须落到同一条上限，不许静默掉 DEFAULT。
    pytest_name = next(n for n in names if n.startswith("pytest(-x"))
    display = f"{pytest_name}｜受影响子集：改了 2 个文件"
    assert gate._step_timeout(display) == gate._step_timeout(pytest_name)


def test_budgeted_takes_the_smaller_side_and_never_goes_below_one() -> None:
    """全局剩余与步上限取小；下限 1s —— 剩余再少也要跑出一次带日志的红，不许无声跳过。"""
    gate = _load_gate()
    assert gate._budgeted(900.0, None) == 900.0  # 不约束
    assert gate._budgeted(900.0, 120.0) == 120.0  # 全局更紧
    assert gate._budgeted(900.0, 3000.0) == 900.0  # 步自身更紧
    assert gate._budgeted(900.0, 0.0) == 1.0  # 到点也不无声
    assert gate._budgeted(900.0, -5.0) == 1.0


def test_runners_expose_timeout_as_a_keyword() -> None:
    """替身跟真签名走（本仓 R26-43 那条老纪律）：`--only` 那批用例 stub 的就是这两个函数，
    签名漂了它们会先炸在这里，而不是漂成"CI 上没测到"。
    """
    import inspect

    gate = _load_gate()
    for fn in (gate._run, gate._run_captured):
        assert "timeout" in inspect.signature(fn).parameters
        assert "remaining" in inspect.signature(fn).parameters
