"""Nodes for the kernel graph.

The node set is deliberately small: `call_model` and `execute_tools`, wired in a loop.

Two design choices worth stating out loud, because both are easy to get wrong:

  * **There is no `guard` node.** The output guard runs *inside* `call_model`, immediately
    after the model returns. A separate guard node would be a graph edge, and an edge can be
    routed around - by a later refactor, a subgraph, or an interrupt. Checking in the single
    place that talks to the model means every model response is gated by construction.

  * **There is no `assemble_prompt` node either.** The system prompt must never enter the
    checkpoint (D3), so it is assembled on the fly inside `call_model` from the current role.

Tool errors are converted into `ToolMessage` results rather than exceptions: the model is
allowed to see "that failed", but never a stack trace.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from rolecard_agent.config import Settings
from rolecard_agent.core.guard import check
from rolecard_agent.core.observability import TraceEvent, Tracer, timer
from rolecard_agent.core.prompts import build_system_prompt
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService, RoleNotFound

TOOL_OFFLINE = "该能力当前未启用，无法调用。"
TOOL_DENIED = "当前角色没有调用该工具的权限。"
TOOL_FAILED = "工具执行失败，请稍后重试或换一种问法。"


class ChatLike(Protocol):
    """Minimal model interface the kernel needs.

    Narrow on purpose: tests inject a scripted fake, and a Protocol keeps them from having to
    construct a real chat model (which would drag in a running Ollama instance).
    """

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ChatLike: ...

    def invoke(self, input: Any, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class KernelContext:
    """Everything the nodes need, assembled once at graph build time."""

    model: ChatLike
    registry: ToolRegistry
    roles: RoleCardService
    tracer: Tracer
    settings: Settings


def tools_for_turn(state: dict[str, Any], ctx: KernelContext) -> list[Any]:
    """Resolve the role, then run the two-stage filter.

    Shared by `call_model` and `execute_tools` so the model's visible tool set and the
    executor's permitted set can never disagree - they are computed by the same function.
    """
    role = ctx.roles.get(state.get("current_role_id", ""))
    return ctx.registry.select(
        enabled_domains=state.get("enabled_domains") or [],
        role_whitelist=role.tool_whitelist,
    )


def _text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks (multimodal / v2 streaming shape)
        return " ".join(
            part if isinstance(part, str) else str(part.get("text", "")) for part in content
        )
    return str(content)


def call_model(state: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    """Assemble the prompt, bind exactly the permitted tools, call the model, gate the answer."""
    role_id = state.get("current_role_id", "")
    try:
        role = ctx.roles.get(role_id)
    except RoleNotFound:
        # A session pointing at a deleted role must not crash the graph; degrade to a plain
        # refusal so the user gets a sentence instead of a 500.
        reply = AIMessage(content="当前会话绑定的角色已不存在，请重新选择一个角色。")
        ctx.tracer.emit(
            TraceEvent(event="role_missing", role_id=role_id, thread_id=state.get("thread_id"))
        )
        return {"messages": [reply]}

    tools = tools_for_turn(state, ctx)
    bound = ctx.model.bind_tools(tools) if tools else ctx.model

    # System prompt is built here, never stored: see the module docstring. Order inside is
    # role -> exemplars -> global safety rules, so the rules remain last and authoritative.
    system = build_system_prompt(role.system_prompt, role.exemplars)
    prompt = [SystemMessage(content=system), *state["messages"]]

    with timer() as elapsed:
        response = bound.invoke(prompt)

    verdict = check(_text_of(response))
    if not verdict.allowed:
        # Replacing the message also drops any tool_calls it carried - a blocked answer must
        # not be allowed to keep acting through the tool loop.
        response = AIMessage(content=verdict.rewritten or "")
        ctx.tracer.emit(
            TraceEvent(
                event="guard_block",
                thread_id=state.get("thread_id"),
                role_id=role_id,
                node="call_model",
                detail={"reasons": list(verdict.reasons)},
            )
        )

    ctx.tracer.emit(
        TraceEvent(
            event="node_end",
            thread_id=state.get("thread_id"),
            role_id=role_id,
            node="call_model",
            latency_ms=elapsed["ms"],
            detail={"tools_visible": len(tools)},
        )
    )
    return {"messages": [response]}


def execute_tools(state: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    """Run the tool calls the model asked for, one ToolMessage per call.

    Three ways a call can fail without raising, in this order of likelihood:
      * the tool is not in the registry at all  -> offline (a plugin was disabled: C14)
      * the tool exists but the role lost access -> denied (defence in depth, D1)
      * the tool raised                          -> failed, with the stack sent to the tracer
        only
    """
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []

    # Three sets, and the distinction between them is the point:
    #   known     - the tool exists in the registry at all
    #   stage_one - its owning plugin is currently enabled
    #   permitted - stage_one AND allowed by the role's whitelist
    # "offline" and "denied" are different answers to the user, so they are computed
    # separately rather than collapsed into one "not allowed".
    known = set(ctx.registry.names())
    stage_one = {
        t.name
        for t in ctx.registry.select(
            enabled_domains=state.get("enabled_domains") or [], role_whitelist=None
        )
    }
    permitted = {t.name for t in tools_for_turn(state, ctx)}

    results: list[ToolMessage] = []
    for call in calls:
        name = call.get("name", "")
        call_id = call.get("id", "")
        args = call.get("args") or {}

        if name not in known or name not in stage_one:
            # Either the tool never existed, or its plugin has since been switched off: a
            # resumed session can still carry tool_calls for it (C14).
            results.append(ToolMessage(content=TOOL_OFFLINE, tool_call_id=call_id, name=name))
            ctx.tracer.emit(
                TraceEvent(event="tool_offline", tool=name, thread_id=state.get("thread_id"))
            )
            continue
        if name not in permitted:
            results.append(ToolMessage(content=TOOL_DENIED, tool_call_id=call_id, name=name))
            ctx.tracer.emit(
                TraceEvent(
                    event="tool_denied",
                    tool=name,
                    role_id=state.get("current_role_id"),
                    thread_id=state.get("thread_id"),
                )
            )
            continue

        tool = ctx.registry.get(name)
        with timer() as elapsed:
            try:
                outcome = tool.invoke(args)
                content = outcome if isinstance(outcome, str) else str(outcome)
            except Exception as exc:  # noqa: BLE001 - the model gets a sentence, logs get the cause
                content = TOOL_FAILED
                ctx.tracer.emit(
                    TraceEvent(
                        event="tool_error",
                        tool=name,
                        thread_id=state.get("thread_id"),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        results.append(ToolMessage(content=content, tool_call_id=call_id, name=name))
        ctx.tracer.emit(
            TraceEvent(
                event="tool_call",
                tool=name,
                thread_id=state.get("thread_id"),
                latency_ms=elapsed["ms"],
            )
        )
    return {"messages": results}


def route_after_model(state: dict[str, Any]) -> str:
    """Loop back into the tools when the model asked for one; otherwise finish."""
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "end"
