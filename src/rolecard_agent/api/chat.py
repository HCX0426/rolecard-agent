"""SSE chat streaming: event framing + the incremental output guard (M4).

Two design decisions, both worth stating out loud:

1. **How does the output guard survive streaming?** `call_model`'s guard checks a COMPLETE
   response. Streaming means tokens exist before that check finishes - but "generated" is not
   the same as "shown". This module streams **only vetted prefixes**: every chunk feeds an
   accumulator that runs `guard.check()` on the accumulated text, and nothing is emitted
   before (a) the check still passes and (b) the chunk is beyond the trailing hold-back window
   (WINDOW >= the longest trigger pattern, so a half-generated pattern is never displayed).
   The moment the accumulated text trips a rule, streaming STOPS and the authoritative
   rewritten text - the same `BLOCKED_RESPONSE` that `call_model` commits into the checkpoint -
   arrives as a `message_replace` event. Residual risk, stated rather than hidden: a partial
   trigger prefix can appear briefly; the replace event corrects it.

2. **Where does the authoritative text come from?** `stream_mode="messages"` yields raw model
   tokens (pre-guard); `stream_mode="updates"` yields what each node COMMITTED (post-guard).
   At the end of every model turn the two are reconciled: equal -> flush the window-held tail
   as final tokens; different (guard rewrite, or the role-missing fallback sentence) ->
   `message_replace`. The client always renders the committed text as truth.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError

from rolecard_agent.core.graph import MODEL_NODE, TOOLS_NODE
from rolecard_agent.core.guard import check
from rolecard_agent.core.nodes import _text_of
from rolecard_agent.core.observability import TraceEvent, Tracer, scrub_endpoints

# Characters of accumulated text held back from emission. Must be >= the longest guard trigger
# pattern (the widest is ~21 chars: subject + 8 filler + modal verb + 8 filler + action verb).
# 32 keeps clear margin without any perceptible rendering delay.
WINDOW = 32


def sse(event: dict[str, Any]) -> str:
    """Frame one Server-Sent Event. The trailing blank line IS the frame delimiter."""
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


# 供应商对"把图片发给不支持视觉的模型"的报错形态各家不一（siliconflow 20041 / OpenAI
# 兼容 / Ollama 文案都不同），但都围绕 "VLM / vision / image / text-only" 这几个词。
# 命中即翻译成可操作提示，而不是笼统的"模型调用失败"——用户据此知道是"含图会话切到了
# 纯文本模型"，该切回视觉模型或新开不含图的会话（用户 2026-09-19 报的 bug）。
_VISION_MISMATCH_SIGNALS = (
    "not a vlm",
    "vision language model",
    "text-only prompt",
    "does not support image",
    "does not support vision",
    "unsupported image",
    "image not supported",
    "input does not contain any image",
)

VISION_MISMATCH_DETAIL = (
    "当前模型不支持图片识别（视觉）。这条对话里含有图片，请切换到支持视觉的模型"
    "（例如本地 qwen3-vl）后再问；或新开一条不含图片的会话来使用当前模型。"
)


def _model_error_detail(exc: Exception) -> str:
    """把模型调用异常映射成给用户的可读提示。纯函数，便于脱机测试。"""
    text = str(exc).lower()
    if any(sig in text for sig in _VISION_MISMATCH_SIGNALS):
        return VISION_MISMATCH_DETAIL
    return "模型调用失败，请稍后重试或换一种问法。"


class StreamingGuard:
    """Incremental fail-closed output review for a single model turn.

    The contract with the client: text arrives only through events this guard allowed, so a
    complete violation pattern never reaches the screen; the authoritative text always arrives
    via the model node's committed update, which the client renders as final truth.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.emitted = 0
        self.blocked = False

    def feed(self, text: str) -> str:
        """Accumulate one chunk; return the safe-to-emit delta ("" when nothing may go out).

        Re-running `check()` on the whole buffer per chunk is correct incremental semantics,
        not just a cost shortcut: a pattern is only detectable once it is COMPLETE in the text,
        and a complete pattern in the buffer is exactly what the full-text check would catch.
        """
        self.buffer += text
        if self.blocked:
            return ""
        verdict = check(self.buffer)
        if not verdict.allowed:
            self.blocked = True
            return ""
        safe = max(0, len(self.buffer) - WINDOW)
        if safe <= self.emitted:
            return ""
        delta = self.buffer[self.emitted : safe]
        self.emitted = safe
        return delta

    def flush_tail(self) -> str:
        """Release what the window held back. Call once the turn's committed text passed the
        full-text guard (which is what makes the WHOLE buffer safe, tail included)."""
        if self.blocked:
            return ""
        delta = self.buffer[self.emitted :]
        self.emitted = len(self.buffer)
        return delta

    def reset(self) -> None:
        """Start the next model turn with a clean accumulator."""
        self.buffer = ""
        self.emitted = 0
        self.blocked = False


def _chat_events_sync(
    graph: Any,
    *,
    graph_input: dict[str, Any],
    config: dict[str, Any],
    role_summary: dict[str, str],
    tracer: Tracer | None = None,
) -> Iterator[str]:
    """Run one user turn through the kernel graph and translate it into SSE events (sync core).

    同步实现的内部核心：项目的检查点是同步 `SqliteSaver`，其 async 对应实现会抛
    NotImplementedError，所以 `graph.stream`（同步）是唯一能用的 runner。见模块级
    `chat_events`：它把这个同步生成器卸到独立的**有界线程池**里逐步取事件，从而不占用
    Starlette 的共享线程池、块间让出事件循环（M6 的并发修复，无需重写检查点为 async）。


Event order for a tool-using turn:
    start -> [thinking]* -> tool_call -> tool_result -> (token)* -> end

Event order for a direct answer:
    start -> [thinking]* -> (token)* -> end

`thinking` events appear only when the model is a reasoning model invoked with reasoning
enabled (see MODEL_THINKING_MODELS in config.py). Thinking text is kept separate from the
answer body - the UI renders it as a collapsible panel, the way AI IDEs do.

`message_replace` appears whenever the committed text diverges from the streamed tokens
(guard rewrite, role-missing fallback, or a model that produced no token events at all).

`context_trimmed` appears at most once per user turn, and only when the history budget forced
older messages out of the prompt (`{dropped, kept}`). It exists so the user is told the truth
about "why doesn't the model remember what I said earlier" instead of having to guess - the
UI surfaces it as a quiet inline notice. Same fact is queryable after a page reload via
`GET /api/session/{thread_id}/context`.
"""
    yield sse({"type": "start", "role": role_summary})
    guard = StreamingGuard()
    # 一次用户轮次里 `call_model` 可能跑多次（工具循环）。裁剪只报**第一次**：那一轮
    # 代表"这一问开始时模型能看到多少历史"，是用户需要知道的那个事实；后面几次的数值
    # 是工具消息把窗口挤得更满的结果，重复上报只会变成噪音。
    trim_reported = False
    think_emitted = False
    try:
        for mode, payload in graph.stream(
            graph_input, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                chunk, _meta = payload
                # 思考模型的推理增量（langchain-ollama：reasoning=True 时思考进
                # additional_kwargs['reasoning_content']）。与正文分流：思考走 thinking
                # 事件、进折叠面板，不混进回答正文。qwen2.5 等非思考模型这里是空 → 零开销。
                think_delta = (getattr(chunk, "additional_kwargs", None) or {}).get(
                    "reasoning_content"
                )
                if isinstance(chunk, AIMessageChunk) and think_delta:
                    think_emitted = True
                    yield sse({"type": "thinking", "text": str(think_delta)})
                if not isinstance(chunk, AIMessageChunk) or not chunk.content:
                    continue
                delta = guard.feed(_text_of(chunk))
                if delta:
                    yield sse({"type": "token", "text": delta})
                continue

            for node, update in (payload or {}).items():
                if node == MODEL_NODE:
                    messages = (update or {}).get("messages") or []
                    if not messages:
                        continue
                    # 历史被上下文预算裁剪过 → 如实告诉客户端（审查报告 H3 的界面部分）。
                    # 事件只承载"发生了什么、多少条"，不含任何对话内容。
                    dropped = (update or {}).get("context_trimmed") or 0
                    if dropped and not trim_reported:
                        trim_reported = True
                        yield sse(
                            {
                                "type": "context_trimmed",
                                "dropped": dropped,
                                "kept": (update or {}).get("context_kept") or 0,
                            }
                        )
                    committed = messages[-1]
                    # 兜底路径：模型没有走增量流（测试脚本 / 非流式后端）时，思考内容会
                    # 完整地落在 committed 消息上 —— 此时一次性发出，并以 think_emitted
                    # 防止与流式增量重复。
                    committed_think = (getattr(committed, "additional_kwargs", None) or {}).get(
                        "reasoning_content"
                    )
                    if committed_think and not think_emitted:
                        think_emitted = True
                        yield sse({"type": "thinking", "text": str(committed_think)})
                    for call in getattr(committed, "tool_calls", None) or []:
                        yield sse(
                            {
                                "type": "tool_call",
                                "name": call.get("name"),
                                "args": call.get("args") or {},
                            }
                        )
                    text = _text_of(committed)
                    if guard.blocked or text != guard.buffer:
                        # Committed text diverged from what was streamed: the client replaces
                        # its in-progress bubble with the authoritative (safe) text.
                        yield sse({"type": "message_replace", "text": text})
                    else:
                        tail = guard.flush_tail()
                        if tail:
                            yield sse({"type": "token", "text": tail})
                    guard.reset()
                elif node == TOOLS_NODE:
                    for message in (update or {}).get("messages") or []:
                        if isinstance(message, ToolMessage):
                            yield sse(
                                {
                                    "type": "tool_result",
                                    "name": message.name,
                                    "content": _text_of(message),
                                }
                            )
    except GraphRecursionError:
        # 工具循环撞上步数上限（core/graph.build_graph_config 设的 recursion_limit）。
        # 这不是"模型调用失败"——模型一直在正常回话，是它陷入了重复调用，所以必须
        # 说清"发生了什么、怎么绕开"，否则用户只会反复重试同一个问法。
        limit = config.get("recursion_limit")
        if tracer is not None:
            tracer.emit(
                TraceEvent(
                    event="chat_error",
                    error=f"GraphRecursionError: 超过步数上限 {limit}",
                    detail={"node": MODEL_NODE, "reason": "recursion_limit"},
                )
            )
        yield sse(
            {
                "type": "error",
                "detail": (
                    f"这一轮的工具调用超过了 {limit} 步上限，已自动停止"
                    "（通常是模型陷入了重复调用）。换个问法，或把任务拆小一点再试。"
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001 - the client gets a sentence, the log gets the cause
        if tracer is not None:
            # 带上消息体（审查报告 E1）：只记异常类型名等于回答不了"这次为什么失败" ——
            # 超时、连接被拒、模型不存在在日志里长得一模一样。异常文本不承载报告原文，
            # 且 URL 会被脱敏，因此脱敏纪律不受影响。
            tracer.emit(
                TraceEvent(
                    event="chat_error",
                    error=f"{type(exc).__name__}: {scrub_endpoints(str(exc))}"[:300],
                    detail={"node": MODEL_NODE},
                )
            )
        yield sse({"type": "error", "detail": _model_error_detail(exc)})
    yield sse({"type": "end"})


# M6：独立的**有界**线程池承载对话流。同步 `graph.stream` 是阻塞调用（最长 model_timeout
# 120s），若直接占 Starlette 共享线程池，并发对话会把池子耗尽、其它请求无线程可用。
# 这里把它隔离到专属小池，并在每次取事件后让出事件循环，既不影响 SSE 协议，又释放了
# 共享池的并发容量。检查点仍是同步 SqliteSaver，无需改写为 async（避免 AsyncSqliteSaver
# + aiosqlite 的第二条连接故事与零收益风险）。
_CHAT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chat-stream")


async def chat_events(
    graph: Any,
    *,
    graph_input: dict[str, Any],
    config: dict[str, Any],
    role_summary: dict[str, str],
    tracer: Tracer | None = None,
) -> AsyncIterator[str]:
    """异步包装 `_chat_events_sync`：逐事件从专属线程池取出，块间让出事件循环。

    对外 SSE 协议与同步版完全一致（start/thinking/token/tool_call/tool_result/
    message_replace/context_trimmed/error/end）。调用方（端点）需用 `async def` +
    `StreamingResponse(async_gen)`。
    """
    loop = asyncio.get_event_loop()
    gen = _chat_events_sync(
        graph, graph_input=graph_input, config=config, role_summary=role_summary, tracer=tracer
    )
    sentinel = object()
    while True:
        try:
            item = await loop.run_in_executor(_CHAT_POOL, next, gen, sentinel)
        except StopIteration:  # pragma: no cover - next 带 default 不会抛，双保险
            break
        if item is sentinel:
            break
        yield cast("str", item)
