"""SSE 投送层：把内核的轮次事件（`core/turn.py`）帧成 Server-Sent Event，并桥到事件循环。

这里**只有投送**：轮次语义（流式审核、已提交文本对账、思考分流、失败翻译）全部在
`core/turn.py`。拆分的动机见那个模块的开头 —— 桌宠壳（里程碑 D）要消费同一份事件流，
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
import json
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, cast

from rolecard_agent.core.observability import Tracer
from rolecard_agent.core.turn import TurnEvent, run_turn

_CHAT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chat-stream")


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
) -> AsyncIterator[str]:
    """异步投送 `core.turn.run_turn` 的事件流：逐事件从专属线程池取出，块间让出事件循环。

    对外 SSE 协议与直接跑同步版完全一致（start / thinking / token / tool_call / tool_result /
    message_replace / context_trimmed / error / end）。调用方（端点）需用 `async def` +
    `StreamingResponse(async_gen)`。
    """
    loop = asyncio.get_event_loop()
    gen: Iterator[str] = (sse(ev) for ev in run_turn(
        graph,
        graph_input=graph_input,
        config=config,
        role_summary=role_summary,
        tracer=tracer,
    ))
    sentinel = object()
    while True:
        try:
            item = await loop.run_in_executor(_CHAT_POOL, next, gen, sentinel)
        except StopIteration:  # pragma: no cover - next 带 default 不会抛，双保险
            break
        if item is sentinel:
            break
        yield cast("str", item)
