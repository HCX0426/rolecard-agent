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

import json
from collections.abc import Iterator
from typing import Any

from langchain_core.messages import AIMessageChunk, ToolMessage

from rolecard_agent.core.graph import MODEL_NODE, TOOLS_NODE
from rolecard_agent.core.guard import check
from rolecard_agent.core.nodes import _text_of
from rolecard_agent.core.observability import TraceEvent, Tracer

# Characters of accumulated text held back from emission. Must be >= the longest guard trigger
# pattern (the widest is ~21 chars: subject + 8 filler + modal verb + 8 filler + action verb).
# 32 keeps clear margin without any perceptible rendering delay.
WINDOW = 32


def sse(event: dict[str, Any]) -> str:
    """Frame one Server-Sent Event. The trailing blank line IS the frame delimiter."""
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


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


def chat_events(
    graph: Any,
    *,
    graph_input: dict[str, Any],
    config: dict[str, Any],
    role_summary: dict[str, str],
    tracer: Tracer | None = None,
) -> Iterator[str]:
    """Run one user turn through the kernel graph and translate it into SSE events.

    A SYNC generator on purpose: the project's checkpointer is the sync `SqliteSaver`, whose
    async counterparts raise NotImplementedError - so `graph.stream` (sync) is the one runner
    that works with it. Starlette drives sync generators from its thread pool, which is fine
    for a single-user demo; going async would mean AsyncSqliteSaver + aiosqlite and a second
    connection story for zero demo benefit.

    Event order for a tool-using turn:
        start -> tool_call -> tool_result -> (token)* -> end

    Event order for a direct answer:
        start -> (token)* -> end

    `message_replace` appears whenever the committed text diverges from the streamed tokens
    (guard rewrite, role-missing fallback, or a model that produced no token events at all).
    """
    yield sse({"type": "start", "role": role_summary})
    guard = StreamingGuard()
    try:
        for mode, payload in graph.stream(
            graph_input, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                chunk, _meta = payload
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
                    committed = messages[-1]
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
    except Exception as exc:  # noqa: BLE001 - the client gets a sentence, the log gets the cause
        if tracer is not None:
            tracer.emit(TraceEvent(event="chat_error", error=type(exc).__name__))
        yield sse({"type": "error", "detail": "模型调用失败，请稍后重试或换一种问法。"})
    yield sse({"type": "end"})
