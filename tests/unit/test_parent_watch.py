"""父进程看门狗（core/watch/parent_watch.py）。

这条逻辑的全部风险都在**误杀**：健康的服务器被判成"父没了"就地自尽，用户看到的是
"服务随机消失"。所以测试的重心不是"能触发"，而是"什么情况下绝不触发"。

其中一条用真子进程跑完整闭环（父活着装上 → 父退出 → 看门狗开火），因为"能不能看见
别的进程"这件事只有操作系统能回答。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from rolecard_agent.core.watch import parent_watch as watch


def _reaped_pid() -> int:
    """一个确定已经死掉并被回收的 PID。"""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return int(child.pid)


def test_process_alive_answers_for_real_on_this_machine() -> None:
    assert watch.process_alive(os.getpid()) is True  # 自己当然活着
    assert watch.process_alive(_reaped_pid()) is False  # 没了就是没了
    assert watch.process_alive(0) is True and watch.process_alive(-1) is True  # 无对象=不判


def test_no_watchdog_without_a_parent_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """没注入 ROLECARD_PARENT_PID = 人手工起的服务器，谁也不许杀它。"""
    monkeypatch.delenv("ROLECARD_PARENT_PID", raising=False)
    assert watch.start(on_exit=lambda: pytest.fail("不该退出")) is None
    monkeypatch.setenv("ROLECARD_PARENT_PID", "not-a-number")
    assert watch.start(on_exit=lambda: pytest.fail("不该退出")) is None


def test_startup_refuses_to_watch_a_pid_it_cannot_see() -> None:
    """启动时就看不到父进程 → 不装（宁缺毋滥，绝不留一个会误判的定时器）。"""
    fired = threading.Event()

    def _on_exit() -> None:
        # 只置事件不抛异常：真实 on_exit 是 `os._exit`（永不返回、也从不 raise），
        # 而在看门狗线程里抛 SystemExit 会被 pytest 包成
        # PytestUnhandledThreadExceptionWarning —— 自己造的假异常不该占用警告摘要。
        fired.set()

    assert watch.start(parent_pid=_reaped_pid(), on_exit=_on_exit) is None
    assert not fired.wait(0.3)


def test_watchdog_fires_when_the_parent_really_goes_away() -> None:
    """真进程闭环：父活着时装上，父退出后看门狗必须开火。"""
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.2)"])
    fired = threading.Event()

    def _on_exit() -> None:
        # 同上一支的理由：置事件就是"开火了"的完整证据，抛 SystemExit 只会给
        # 警告摘要添一条自己造的噪音（父消失后 loop() 走到 on_exit 返回，线程自然退）。
        fired.set()

    thread = watch.start(
        parent_pid=int(parent.pid), on_exit=_on_exit, interval=0.1, is_alive=watch.process_alive
    )
    assert thread is not None  # 此刻父进程确实看得见
    parent.wait()
    assert fired.wait(5), "父进程已退出，看门狗却没有开火"


def test_watchdog_stays_quiet_while_the_parent_lives() -> None:
    """父还在就绝不能动手 —— 这条是上一号的反面，也是误杀事故的本体。"""
    fired = threading.Event()
    thread = watch.start(
        parent_pid=os.getpid(),
        on_exit=lambda: fired.set(),  # type: ignore[arg-type]
        interval=0.05,
    )
    assert thread is not None
    time.sleep(0.4)
    assert not fired.is_set()


# --------------------------- POSIX 那半边（两台机器都要跑得到）


def test_posix_alive_branches_are_exercised_on_any_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_posix_process_alive` 的三格用 os.kill 替身打 —— **平台无关**。

    发布闸升全量后首次在 Linux runner 上量出它 64.71%（本机 Windows 永远跑不到
    这条分支，"两台机器各查各的那半"的分工把这一格漏给了没人查的空档 —— 现在
    替身让两半都在每台机器上被查）。三格的语义各有归属：
      * ProcessLookupError → False（真没了）；
      * PermissionError → True（存在但不是我的 —— 宁可少一道保险，不误杀）；
      * 正常返回 → True。
    """

    def raise_no_process(pid: int, sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(watch.os, "kill", raise_no_process)
    assert watch._posix_process_alive(12345) is False

    def raise_perm(pid: int, sig: int) -> None:
        raise PermissionError()

    monkeypatch.setattr(watch.os, "kill", raise_perm)
    assert watch._posix_process_alive(12345) is True

    monkeypatch.setattr(watch.os, "kill", lambda pid, sig: None)
    assert watch._posix_process_alive(12345) is True


def test_process_alive_pid_guards_are_platform_free() -> None:
    """pid<=0 一律"活着"（无可监视对象）——这条在两个平台上都必须成立。"""
    assert watch.process_alive(0) is True
    assert watch.process_alive(-3) is True
