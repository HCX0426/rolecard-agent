"""进程内共享线程池的**统一构造与上下文传播**原语（2026-10-24 冗余清理的 1.4）。

背景：全仓有 8 处 `ThreadPoolExecutor`，其中两处（`api/chat.py` 的 SSE 流、
`core/agent/nodes.py` 的工具执行）各自手写过一遍"提交前 `copy_context()`"——因为
**contextvar 不跨线程传播**（3.13 实测：`pool.submit` 与 `loop.run_in_executor` 的池线程
只读到默认值）。这不是风格问题，是两起真实事故的共同根因：

  * `nodes.py` 那次：`search_knowledge` 读不到 `role_knowledge_scopes_ctx`，检索工具
    对**已授权**的角色回答"未授权任何知识作用域" —— 权限静默失效；
  * `chat.py` 那次（`R102-03`）：`ThreadLocalConnection._current()` 判"库代际变没变"
    读的正是 contextvar，不带过去，请求级残留事务清理**一次都没触发过**。

所以把"怎么提交"收成一个函数：跨这条边界的调用方不需要知道 contextvar 的存在。
池本身各处照旧自建（worker 数、前缀、关闭时机都是各站点的本地决定）：
进程级的（chat-stream / tool-exec / memory-distill / upload-process / approve-run）
在解释器退出时由 `concurrent.futures` 的 atexit join 在飞任务；随宿主生命周期的
（reachout 的生成池）由 `Runtime.shutdown` 的 `BackgroundTask.stop` 关。

**刻意不收编的两处临时池**：`consensus`（每次比对 fan-out N 个后端）与 `mcp._run_async`
（事件循环在场时的逃生门）——它们是 `with ThreadPoolExecutor(...)` 的**调用级**对象，
活不过一次调用，与这里的进程级池不同族；前者另有"为什么绕开 ModelResolver"的裁决
（见 `core/models/consensus.py::build_consensus_tool` 的 docstring）。
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


def make_pool(name: str, max_workers: int) -> ThreadPoolExecutor:
    """建一条命名池。池**惰性起线程**：从不 submit 就零线程，恒建无害。

    为什么收进来而不是各处裸写 `ThreadPoolExecutor(...)`：让"全仓有哪几条长命池、
    前缀叫什么"有一处可数的清单（模块 docstring 那张账），并且新池默认就长在这个
    形状上 —— 关闭时机仍是各站点的本地决定，这里不接管生命周期。
    """
    return ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)


def submit_with_context[T](
    pool: ThreadPoolExecutor, fn: Callable[..., T], /, *args: Any, **kwargs: Any
) -> Future[T]:
    """把调用提交进池，并**拷贝当前 contextvars** 一起带过去。

    为什么必须拷（两起事故见模块 docstring）：`pool.submit` 不传播 contextvar，池线程
    读到的是默认值。**每次提交各拷一份**，不复用上一份 —— 上一轮被取消后那个在飞调用
    仍在线程里跑完（`core/common/thread_locks.py` 的说明），共用一枚 Context 会撞上
    "同一上下文不可重入"。
    """
    copied = contextvars.copy_context()
    return pool.submit(copied.run, fn, *args, **kwargs)


__all__ = ["make_pool", "submit_with_context"]
