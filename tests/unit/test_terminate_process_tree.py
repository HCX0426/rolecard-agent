"""`terminate_process_tree` 的进程组语义（2026-10-09 一整趟 CI 被它 SIGKILL 换来的）。

这函数有**三个调用点**、干的是"杀进程"这件事，而改之前全仓**一支用例都没有** —— 它的
docstring 明写着契约"POSIX 用 `start_new_session` 建的独立进程组 + killpg"，可那个契约
**在任何调用点都没被执行过**：孩子留在继承来的组里时，`killpg(getpgid(孩子))` 打中的是
调用方自己那一串（bash + `timeout` + gate.py），于是一次"收个后端"变成**把整条 CI 步骤
连坐杀掉**（run 37842997481：README 可跑性起服务后 2 秒整步没，退 137）。Windows 走
taskkill 按树、不碰进程组 ⇒ 本机跑半年照不出来。

判据按臂分两层，缺一条都不算数（本仓"两臂都必须能红"那条）：
  * 跨平台层（本机也跑）：谓词的三种回答 + 支路选择 + 三个调用点都真的独立成组。
    支路那几臂要把执行推进 POSIX 那条路 —— 本机的 `signal.SIGKILL` 不存在（第一版就是
    炸在这个符号缺失上，报出来的错与产品代码无关），所以 `sys/os/signal` 三件得一起给
    假视图；这是**命名空间替身**，量的永远是"选了哪条支路、拿什么参数调用"，不是真信号。
  * POSIX 行为层（Linux 才跑，即 bug 唯一能发作的那个臂）：真起进程真杀，断言做在
    **子进程里** —— 旧形状下这一发会把跑判据的 pytest 自己杀掉，那不是"用例红"而是
    "整个测试会话无声消失"（与 CI 上那次一模一样）。本机 skip 说得明明白白，不假装量过。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from rolecard_agent.core.tools import run as run_mod  # noqa: E402
from rolecard_agent.core.tools.run import (  # noqa: E402
    _child_owns_its_process_group,
    terminate_process_tree,
)

POSIX = os.name != "nt"


class FakeProc:
    """只带 `pid` 与 `kill()` 的最小替身：支路臂量的是**选哪条路**，不是真杀。"""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class FakeOs:
    """`run` 模块命名空间里 `os` 的替身：`killpg` 一律记账，好断言"它到底被没被叫"。"""

    def __init__(self) -> None:
        self.killpg_calls: list[tuple[int, object]] = []

    def killpg(self, pgid: int, sig: object) -> None:
        self.killpg_calls.append((pgid, sig))


class FakeSys:
    """`sys` 的替身：只把 `platform` 说成 linux，好让支路选择在**本机也能量**。

    不动全局 `sys`（那是整个 pytest 进程的地基），只在 `run` 模块的命名空间里换一份假视图。
    """

    platform = "linux"


class FakeSignal:
    """`signal` 的替身：Windows 上真模块没有 `SIGKILL` 这个符号。"""

    SIGKILL = "FAKE-SIGKILL"


def _force_posix_branch(monkeypatch: pytest.MonkeyPatch, owns_group: bool) -> FakeOs:
    """把 `terminate_process_tree` 推进 POSIX 支路（三件一起给，缺一条就炸在符号缺失上）。

    谓词本身也换成常量替身：支路选择与"组怎么判"是两件事，各由各臂量（谓词那三臂在下面）。
    """
    fake_os = FakeOs()
    monkeypatch.setattr(run_mod, "sys", FakeSys())
    monkeypatch.setattr(run_mod, "os", fake_os)
    monkeypatch.setattr(run_mod, "signal", FakeSignal())
    monkeypatch.setattr(run_mod, "_child_owns_its_process_group", lambda _pid: owns_group)
    return fake_os


# ---------------------------------------------------------------- 谓词三臂


def test_没有进程组概念的平台上谓词必须为假(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 上 `os.getpgid` 不存在 ⇒ False，`killpg` 那条路永远走不到。

    这条同时钉住"为什么写 `getattr(os, 'getpgid', None)` 而不是直接 `os.getpgid`"：直接写在
    本机就是 `AttributeError`，而这三个调用点每跑一次门禁都要经过这里。
    """
    monkeypatch.delattr(os, "getpgid", raising=False)
    assert _child_owns_its_process_group(os.getpid()) is False


def test_孩子早退场时谓词按假处理(monkeypatch: pytest.MonkeyPatch) -> None:
    """`getpgid` 抛 OSError（竞态：孩子已经没了）⇒ False，让上层走"只杀 pid"那条。

    从前那一格是 `except ProcessLookupError` 兜在 killpg 外面；抽成谓词之后它必须自己回答，
    不然一个竞态就把"该杀谁"变成"异常冲出调用点"。
    """

    def boom(_pid: int) -> int:
        raise ProcessLookupError("没了")

    monkeypatch.setattr(os, "getpgid", boom, raising=False)
    assert _child_owns_its_process_group(999999) is False


def test_组长时谓词为真(monkeypatch: pytest.MonkeyPatch) -> None:
    """正反两臂都齐（"只测 deny 一半的护栏救不了任何人"）：pgid 等于/不等于 pid 两格。"""
    monkeypatch.setattr(os, "getpgid", lambda _pid: 777, raising=False)
    assert _child_owns_its_process_group(777) is True
    assert _child_owns_its_process_group(778) is False


# ---------------------------------------------------------------- 支路选择两臂


def test_非组长的孩子只杀那一个pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """**连坐的形状就钉在这一格**：非组长时绝不许碰 `killpg`（那打的是调用方所在的整组）。

    旧写法 `killpg(getpgid(pid))` 在这一形状下杀的就是父进程那一串 —— CI 现场里整步没的
    就是这一条。
    """
    fake_os = _force_posix_branch(monkeypatch, owns_group=False)
    proc = FakeProc()
    terminate_process_tree(proc)  # type: ignore[arg-type]
    assert proc.killed is True, "非组长那条支路得真的把这个 pid 杀掉"
    assert fake_os.killpg_calls == [], f"非组长还去 killpg = 把调用方连坐：{fake_os.killpg_calls}"


def test_组长的孩子按整组收掉(monkeypatch: pytest.MonkeyPatch) -> None:
    """另一臂（不许只测"少杀"那半边）：独立成组的孩子要 **killpg 整组**，孙子不留。

    当初要 killpg 的理由就在这 —— 只 `kill()` 外壳会留下活着答了二十分钟的真后端（本仓实测
    过两次）。这条把"修连坐别把按树杀修没了"钉住。
    """
    fake_os = _force_posix_branch(monkeypatch, owns_group=True)
    proc = FakeProc(5150)
    terminate_process_tree(proc)  # type: ignore[arg-type]
    assert fake_os.killpg_calls == [(5150, FakeSignal.SIGKILL)], fake_os.killpg_calls
    assert proc.killed is False, "走整组那条就不该再单独 kill 外壳（两道都发是重复暴力）"


def test_killpg自己失败时退回杀单个pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """组长支路里 `killpg` 抛（权限/竞态）⇒ 兜到 `proc.kill()`，不带着异常冲出调用点。"""

    class Exploding(FakeOs):
        def killpg(self, pgid: int, sig: object) -> None:
            raise PermissionError("假装的 EPERM")

    fake_os = Exploding()
    monkeypatch.setattr(run_mod, "sys", FakeSys())
    monkeypatch.setattr(run_mod, "os", fake_os)
    monkeypatch.setattr(run_mod, "signal", FakeSignal())
    monkeypatch.setattr(run_mod, "_child_owns_its_process_group", lambda _pid: True)
    proc = FakeProc()
    terminate_process_tree(proc)  # type: ignore[arg-type]
    assert proc.killed is True


# ---------------------------------------------------------------- 调用点结构臂


def test_凡喂给按树杀的孩子都独立成组() -> None:
    """三处调用点的 `Popen` 必须都带 `start_new_session` —— 少一处就回到连坐形状。

    结构臂而不是行为臂：Windows 上行为臂跑不到（本机没有进程组），而**这个错误恰恰只在
    Linux 上发作**。只在"看得见的那一侧"设防，等于给看不见的那一侧开了洞。
    """
    expected = {
        "scripts/gate.py": 2,  # 两处步骤跑器：捕获式 + 流式
        "scripts/probe_readme_quickstart.py": 1,
        "src/rolecard_agent/core/tools/run.py": 1,  # execute_command
    }
    for rel, want in expected.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        got = src.count("start_new_session")
        assert got >= want, f"{rel} 里独立成组只有 {got} 处，期望 ≥{want}：那些孩子在等连坐的刀"


# ---------------------------------------------------------------- POSIX 行为臂


@pytest.mark.skipif(not POSIX, reason="进程组语义只在 POSIX 上存在（Windows 走 taskkill 按树）")
def test_同组的孩子被杀后调用方必须活着(tmp_path: Path) -> None:
    """复现 bug 的那一发：孩子**没**独立成组时，杀完调用方还在。

    断言做在**子进程里**：旧形状下这一发会直接把跑判据的 pytest 连坐杀掉 —— 那不是"用例红"，
    那是"整个测试会话无声消失"（与 CI 上那次一模一样）。
    """
    script = tmp_path / "victim.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from rolecard_agent.core.tools.run import terminate_process_tree\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(0.4)\n"
        "terminate_process_tree(kid)\n"
        "kid.wait(timeout=10)\n"
        "print('CALLER-ALIVE', kid.returncode, flush=True)\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert "CALLER-ALIVE" in proc.stdout, (
        f"调用方被连坐了（run 37842997481 的形状）：rc={proc.returncode} "
        f"err={proc.stderr[-200:]}"
    )


@pytest.mark.skipif(not POSIX, reason="同上")
def test_独立成组时孙子也收干净(tmp_path: Path) -> None:
    """`start_new_session` 的孩子被 killpg ⇒ 它派生的**孙子**不能活着（留孤儿=实弹事故，
    而且它会**攥着日志管道的写端不放** —— 那正是三趟旧挂死最像的形状，见账本 ENGI-25）。
    """
    mark = tmp_path / "grandchild.pid"
    inner = (
        "import os, time\n"
        "open(os.environ['MARK'], 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    script = tmp_path / "group.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from rolecard_agent.core.tools.run import terminate_process_tree\n"
        f"kid = subprocess.Popen([sys.executable, '-c', {inner!r}], start_new_session=True)\n"
        "time.sleep(1.2)\n"
        "terminate_process_tree(kid)\n"
        "kid.wait(timeout=10)\n"
        "time.sleep(1.0)\n"
        "print('DONE', flush=True)\n",
        encoding="utf-8",
    )
    env = {**os.environ, "MARK": str(mark)}
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90, env=env,
    )
    assert "DONE" in proc.stdout, proc.stdout[-300:] + proc.stderr[-300:]
    assert mark.exists(), "孙子没起来：这一臂没量到东西（分母为 0 不等于通过）"
    gpid = int(mark.read_text(encoding="utf-8").strip())
    try:
        os.kill(gpid, 0)  # 只是问"还在吗"，不发信号
    except ProcessLookupError:
        return  # 收干净了
    state = "?"
    stat = Path(f"/proc/{gpid}/stat")
    if stat.exists():
        tail = stat.read_text(encoding="utf-8", errors="replace").split(") ", 1)
        state = tail[1].split()[0] if len(tail) > 1 else "?"
    pytest.fail(f"独立成组那臂本该把整组收干净，孙子 {gpid} 还在（state={state}）")


def test_真起一个孩子收掉它() -> None:
    """不带替身的一发（两平台都跑）：支路尽头**真的有刀**。

    替身那几臂量"选了哪条支路"；这一大量"这条码路杀得掉真进程"。只测前者会留下一个形状：
    支路都选对了，但 `taskkill` 的参数拼错 / `killpg` 的签名传反也没人知道。
    """
    sleep_cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    if POSIX:
        proc = subprocess.Popen(sleep_cmd, start_new_session=True)
    else:
        # Windows：`shell=True` 起的是 cmd → 孙子 python 那棵树，正是"只杀外壳会留孤儿"的
        # 形状（本仓实测过两次）。
        proc = subprocess.Popen(
            subprocess.list2cmdline(sleep_cmd), shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    time.sleep(0.6)
    terminate_process_tree(proc)  # type: ignore[arg-type]
    proc.wait(timeout=30)
    assert proc.poll() is not None, "收了刀孩子还站着"
