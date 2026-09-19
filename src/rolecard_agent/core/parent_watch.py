"""父进程看门狗：桌面壳没了，后端也不该继续占着端口与显存。

为什么做在**后端**而不是壳里：壳被任务管理器硬杀 / 崩溃 / 注销时，任何"退出时记得清理"
的逻辑都不会执行（Tauri 版用 Win32 作业对象解决，换壳就得重做一遍）。放在这里，
壳是谁、用什么语言都无所谓 —— 一条保证只写一次。

误杀比不杀更糟：一个健康的服务器被看门狗判成"父没了"就地自尽，用户看到的是"服务随机
消失"。所以启动时先做一次**能力确认**：此刻父进程一定活着，若连它都看不到（权限、
被保护的进程），就干脆不装看门狗，而不是留一个会误判的定时器。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import NoReturn

#: 轮询间隔。2s 足够：父没了以后，端口多占两秒没有任何后果，而更密的轮询要一直花 CPU。
DEFAULT_INTERVAL = 2.0

#: SYNCHRONIZE 是 0x0010_0000，不是 0x0010（那是 VM_READ）。记错这一位的表现非常阴险：
#: OpenProcess 会**成功**，而 WaitForSingleObject 返回 WAIT_FAILED + GetLastError=5
#: (ACCESS_DENIED) —— 于是活着的进程被判成"没了"，看门狗把健康的服务器杀掉。
_SYNCHRONIZE = 0x0010_0000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x0000_1000
_WAIT_OBJECT_0 = 0x0000_0000
_WAIT_FAILED = 0xFFFF_FFFF

# 用 startswith 而不是 `== "win32"`：mypy 会把后者当常量收窄，把另一侧的分支判成
# unreachable（于是这台机器上永远没人检查 POSIX 那条路写得对不对）。
_IS_WINDOWS = sys.platform.startswith("win")

_open_process = _wait = _close_handle = None  # 惰性绑好的 kernel32 入口（见 _win_apis）


def _win_apis() -> tuple:
    """kernel32 的三个入口，带正确的 restype/argtypes（64 位句柄不能按 int 截断）。"""
    global _open_process, _wait, _close_handle
    if _open_process is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = ctypes.c_void_p
        kernel32.OpenProcess.restype = handle
        kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint
        kernel32.WaitForSingleObject.argtypes = [handle, ctypes.c_uint]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [handle]
        _open_process, _wait, _close_handle = (
            kernel32.OpenProcess,
            kernel32.WaitForSingleObject,
            kernel32.CloseHandle,
        )
    return _open_process, _wait, _close_handle


def _win_process_alive(pid: int) -> bool:
    """Windows：开一个**带等待权**的句柄，然后问它"信号了吗"。

    三种答案都要分开处理：`WAIT_TIMEOUT`=活着、`WAIT_OBJECT_0`=已退出、
    `WAIT_FAILED`/句柄打不开=判不准或没权 —— 判不准时**当活着**（宁可少一道保险，
    也不误杀一个正在服务用户的进程）。
    """
    open_process, wait, close_handle = _win_apis()
    handle = open_process(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return False  # 打不开 = 已经没了（装不上的误判风险由启动时的能力确认兜住）
    try:
        result = wait(handle, 0)
        if result == _WAIT_FAILED:
            return True
        return result != _WAIT_OBJECT_0
    finally:
        close_handle(handle)


def _posix_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我的 —— 当作活着，绝不误杀
    return True


def process_alive(pid: int) -> bool:
    """这个进程还活着吗。`pid <= 0` 一律回答"活着"（= 无可监视对象，交给调用方决定不装）。"""
    if pid <= 0:
        return True
    return _win_process_alive(pid) if _IS_WINDOWS else _posix_process_alive(pid)


def _hard_exit() -> NoReturn:
    """立刻退出，不走 atexit：父都没了，优雅收尾要保护的东西（正在送的那条流）已不存在。"""
    os._exit(0)


def _parent_pid_from_env() -> int | None:
    raw = os.environ.get("ROLECARD_PARENT_PID", "").strip()
    return int(raw) if raw.isdigit() else None


def start(
    *,
    parent_pid: int | None = None,
    is_alive: Callable[[int], bool] = process_alive,
    on_exit: Callable[[], NoReturn] = _hard_exit,
    interval: float = DEFAULT_INTERVAL,
) -> threading.Thread | None:
    """装看门狗；返回守护线程。返回 None = **没装**（没给父 PID，或启动时就看不到它）。

    `parent_pid` 省略时读 `ROLECARD_PARENT_PID`（壳 spawn 后端时注入）。
    """
    pid = parent_pid if parent_pid is not None else _parent_pid_from_env()
    if not pid or pid <= 0:
        return None
    if not is_alive(pid):
        print(
            f"[parent-watch] 启动时看不到父进程 {pid}，不装看门狗（宁可少一道保险，不误杀）",
            flush=True,
        )
        return None

    def loop() -> None:
        while is_alive(pid):
            time.sleep(interval)
        print(f"[parent-watch] 父进程 {pid} 已退出，后端随之结束", flush=True)
        on_exit()

    thread = threading.Thread(target=loop, name="parent-watch", daemon=True)
    thread.start()
    return thread
