"""SSE 投送层：把内核的轮次事件（`core/agent/turn.py`）帧成 Server-Sent Event，并桥到事件循环。

这里**只有投送**：轮次语义（流式审核、已提交文本对账、思考分流、失败翻译）全部在
`core/agent/turn.py`。拆分的动机见那个模块的开头 —— 桌宠壳（里程碑 D）要消费同一份事件流，
但不该被迫吃 SSE。

两个只属于这一层的决定：

1. **事件名是线协议**：`frame()` 直接用事件类自带的 `sse_type`，所以浏览器看到的 `type`
   与内核声明的是同一个东西，没有第二份字符串清单可以漂移。前端解析在
   `frontend/src/lib/stream.ts`，两侧的名字一致性由 `tests/unit/test_frontend_contract.py`
   机器校验。
2. **同步图跑在专属有界线程池里**（M6 的并发修复）：`graph.stream` 是阻塞调用（最长
   `model_timeout` 120s），若直接占 Starlette 共享线程池，并发对话会把池子耗尽、其它请求
   无线程可用。这里把它隔离到专属小池，并在每次取事件后让出事件循环 —— 既不改 SSE 协议，
   又释放了共享池的容量。检查点仍是同步 `SqliteSaver`，不必改写为 async（避免
   `AsyncSqliteSaver` + aiosqlite 的第二条连接故事与零收益风险）。池子的释放在真实的进程
   退出路径（`scripts/run_api.py`），不在单个 app 的生命周期里 —— 它是进程级对象。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, cast

from rolecard_agent.base.observability import Tracer
from rolecard_agent.core.agent.turn import Error, TurnEvent, run_turn
from rolecard_agent.core.common.usage import TokenUsage

_CHAT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chat-stream")
#: 与池等量的"占位"信号量：提交前非阻塞 acquire，拿不到 = 8 个 worker 全忙 ——
#: 从前第 9 个并发轮次在队列里**无限等**、SSE 无任何输出也不报错（用户感知"发出去
#: 没反应"）。现在立刻给一句人话的 error 帧（2026-10-04 审查快照的池满盲区条目）。
#: 池子只服务这条链，信号量与 worker 一一对应。
_CHAT_SLOTS = threading.BoundedSemaphore(8)


def frame(event: TurnEvent) -> dict[str, Any]:
    """一个事件对象 → 线上载荷（`type` + 各字段）。"""
    return {"type": event.sse_type, **asdict(event)}


def sse(event: TurnEvent) -> str:
    """帧化一条 Server-Sent Event。结尾的空行**就是**分帧符。"""
    return "data: " + json.dumps(frame(event), ensure_ascii=False) + "\n\n"


async def chat_events(
    graph: Any,
    *,
    graph_input: dict[str, Any],
    config: dict[str, Any],
    role_summary: dict[str, str],
    tracer: Tracer | None = None,
    usage_recorder: Callable[[TokenUsage | None], None] | None = None,
    after_turn: Callable[[int], None] | None = None,
) -> AsyncIterator[str]:
    """异步投送 `core.turn.run_turn` 的事件流：逐事件从专属线程池取出，块间让出事件循环。

    对外 SSE 协议与直接跑同步版完全一致（start / thinking / token / tool_call / tool_result /
    message_replace / context_trimmed / error / end）。调用方（端点）需用 `async def` +
    `StreamingResponse(async_gen)`。

    池满（8 个 worker 全忙）时不再无限排队：立刻 yield 一条 error 帧收场 —— 排队本身
    不报错也不可观测，是用户感知"发出去没反应"的那个盲区。
    """
    if not _CHAT_SLOTS.acquire(blocking=False):
        yield sse(
            Error(
                detail=(
                    "这一路已经有 8 轮对话在跑，新的这一轮进不来了 —— "
                    "等一句话说完再发，或稍后再试。"
                )
            )
        )
        return
    loop = asyncio.get_event_loop()
    gen: Iterator[str] = (sse(ev) for ev in run_turn(
        graph,
        graph_input=graph_input,
        config=config,
        role_summary=role_summary,
        tracer=tracer,
        usage_recorder=usage_recorder,
        after_turn=after_turn,
    ))
    # **把本请求的上下文带进轮次线程池**（`R102-03`）。`loop.run_in_executor` 与
    # `pool.submit` 都**不**传播 contextvar（3.13 实测：裸调时池线程只读到默认值，
    # `asyncio.to_thread` 才会带过去），而 `ThreadLocalConnection._current()` 判
    # "库代际变没变"读的正是 contextvar —— 不带过去，这条线程的代际永远停在它第一次
    # 拿到的那个值，"新请求第一次用库先回滚上次残留事务"这道清理就**一次都没触发过**，
    # 一轮中途断掉留下的未提交事务会一直占着写锁（别的连接等到 busy_timeout 才报错）。
    # 同一个形状在 `core/agent/nodes.py` 的线程池边界上早就做对了（`copied.run(tool.invoke, args)`，
    # 那里的症状是权限静默失效），漏的是这道缝。
    # 每次调用各拷一份上下文：上一轮被取消后那个 `next()` 仍在线程池里跑完（见
    # `core/common/thread_locks.py` 的说明），共用一枚 Context 就会撞上"同一上下文不可重入"。
    ctx = contextvars.copy_context()
    sentinel = object()
    try:
        while True:
            try:
                item = await loop.run_in_executor(
                    _CHAT_POOL, functools.partial(ctx.run, next, gen, sentinel)
                )
            except StopIteration:  # pragma: no cover - next 带 default 不会抛，双保险
                break
            if item is sentinel:
                break
            yield cast("str", item)
    finally:
        # 客户端断线时 async 生成器被 aclose()：GeneratorExit 从 yield 处抛出，
        # 这里是唯一保证 release 的位置（池位漏一个，以后每次第 8 轮就误报忙）。
        _CHAT_SLOTS.release()
