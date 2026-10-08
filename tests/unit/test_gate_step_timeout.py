"""`gate.py` 的每步超时、全局墙钟与看门狗（2026-10-09 两趟"什么都没报错的 cancel"换来的）。

形状：一步吊住不动 → runner 在 job 预算上把整条 job 杀掉 → **GitHub 对 cancelled job 不上传
日志**（run 37804463056 / 37815831250 / 37823819338 连续三趟，现场三次都只能从相邻臂倒推）。
所以挂死必须变成**带日志的红** —— 三层各挡一种形状，这一档用例逐层钉：

  * **步级超时**：单步吊住 → 那一步红，别的照常被测；
  * **读流并发**：正常但输出巨大的步不许被读成挂死（管道死锁是反方向的红）；
  * **看门狗**：挂死点在步骤之外（`_git` 那类无界调用）时，步级预算全部界得住却照样救不了
    这趟 —— 只有独立的刀能在主线程配合不了的时候把现场打出来。
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
    而没人读管道时，这一格（15000 行 / 约 3MB，本仓 pytest 一步就是这个量级）在 12s 预算里
    **必然到点** —— 子进程写满管道后阻塞在 write 上。谁把泵线程"简化"回顺序读，这条当场红。
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
    # 界限取**宽**：这一格要的是"到点收摊、别吊着"，不是计时器精度。下界 1.5s（预算 3s，
    # 不许提前掐），上界 45s（本机 3.7s 实测过；慢机器上起 python + 收整棵树多花十几秒是
    # 正常负载，把这种抖动画成红会毒化每一次 push）。
    assert 1.5 < wall < 45.0, f"到点没收掉进程树（墙上 {wall:.1f}s）"
    assert "开工" in out, "挂死前打出的输出要原样留着 —— 这整个机制就是为了留现场"
    assert "没跑完" in out
    capsys.readouterr()  # 吞掉流式打印，别灌进测试输出


def test_the_watchdog_fires_while_the_main_thread_is_stuck(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """看门狗是**独立的刀**：主线程卡住不动，它照样到点点名、打栈、记退码。

    `_WATCHDOG_EXIT` 换成记账替身 —— 默认那把是真 `os._exit`，测试不换掉就会把跑自己的
    pytest 一起带走（这正是它非 `os._exit` 不可的理由：真触发时主线程已经配合不了，
    `SystemExit` 走不到解释器）。这里主线程故意睡 1s 扮演"卡在无界调用上"。
    """
    gate = _load_gate()
    fired: list[int] = []
    monkeypatch.setattr(gate, "_WATCHDOG_EXIT", lambda code: fired.append(code))
    gate._CURRENT[0] = "演示：卡住的这步"
    gate._arm_watchdog(time.perf_counter(), 0.3, [("pytest(-x, 无覆盖率)", 12.3)])
    time.sleep(1.0)
    assert fired == [3], f"到点没退出（或退错码）：{fired}"
    out = capsys.readouterr().out
    assert "被全局墙钟掐停" in out and "演示：卡住的这步" in out, "要点名此刻在跑哪一步"
    assert "卡在这一行" in out and "test_gate_step_timeout" in out, "栈要点名卡住的调用处"
    assert "pytest(-x, 无覆盖率)" in out, "已完成步骤的汇总不能丢 —— 那是唯一的时间线"


def test_the_watchdog_is_not_armed_without_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机默认（不传 `--deadline-minutes`）= 不约束：看门狗一枪不放。"""
    gate = _load_gate()
    fired: list[int] = []
    monkeypatch.setattr(gate, "_WATCHDOG_EXIT", lambda code: fired.append(code))
    gate._arm_watchdog(time.perf_counter(), 0.0, [])  # deadline<=0 → 根本不启动
    time.sleep(0.5)
    assert fired == [], f"没设预算也掐：{fired}"


def test_the_watchdog_is_armed_before_the_first_git_call() -> None:
    """`main()` 里看门狗必须排在**第一个取改动清单的调用之前** —— 结构钉，不是行为测。

    run 37823819338 教的东西：`started` 从前排在改动清单与读数键守卫之后（往下四十行），
    于是 `_changed_paths()` 那批 `_git()` 整个跑在预算外 —— "每步预算都界得住、趟却照样被杀"
    的门缝就是这么留出来的。谁把顺序换回去，这条当场红。
    """
    src = (ROOT / "scripts" / "gate.py").read_text(encoding="utf-8")
    arm = src.index("_arm_watchdog(started, deadline, timings)")
    first_git = src.index("_changed_at_start: list[str] | None = _changed_paths()")
    assert arm < first_git, "看门狗上膛必须排在第一批 git 调用之前"


def test_git_queries_have_a_timeout_now() -> None:
    """`_git()` 不许回到无超时 —— 它是那三趟里唯一一类**步骤之外**的无界 subprocess。

    行为形状（`_run` 层的超时）覆盖不到它：它不在任何一步里，跑在每步之前。
    """
    import inspect

    src = inspect.getsource(_load_gate()._git)
    assert "timeout=60" in src, "_git 又回到无超时了"


def test_the_timeout_table_names_are_the_step_table_prefixes() -> None:
    """`STEP_TIMEOUTS` 的键必须对得上 `STEPS` 的真名（含"快档那步带显示后缀"这一格）。

    查不到不报错 —— 只是那一步的上限**静默退回默认值**，与"这一档本来不设上限"长得一样。
    快档的 pytest 到 `_run` 手里时带着 `｜受影响子集…` 后缀（`_resolve` 加的），所以查表
    先摘后缀；这条把后缀形状一起钉住（用真表里的名字，不假造）。
    """
    gate = _load_gate()
    names = [n for n, _, _ in gate.STEPS]
    for key in gate.STEP_TIMEOUTS:
        assert any(n == key or key in n for n in names), f"超时表里的键对不上任何步骤：{key!r}"
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


def test_remaining_seconds_counts_from_trip_start() -> None:
    """`--only` 只点名静态步时那五并发共用同一份剩余 —— 口径只有这一处。"""
    gate = _load_gate()
    started = time.perf_counter()
    time.sleep(0.2)
    left = gate._remaining_seconds(started, 60.0)
    assert left is not None and 59.0 < left < 60.0
    assert gate._remaining_seconds(started, None) is None
    assert gate._remaining_seconds(started, 0.1) == 0.0  # 过点了就是 0，不许负数


def test_runners_expose_timeout_and_remaining_as_keywords() -> None:
    """替身跟真签名走（本仓 R26-43 那条老纪律）：`--only` 那批用例 stub 的就是这两个函数，
    签名漂了它们会先炸在这里，而不是漂成"CI 上没测到"。
    """
    import inspect

    gate = _load_gate()
    for fn in (gate._run, gate._run_captured):
        params = inspect.signature(fn).parameters
        assert "timeout" in params and "remaining" in params
