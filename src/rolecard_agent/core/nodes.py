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

from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from rolecard_agent.config import Settings
from rolecard_agent.core.guard import check
from rolecard_agent.core.observability import TraceEvent, Tracer, timer
from rolecard_agent.core.prompts import build_system_prompt
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService, RoleNotFound

TOOL_OFFLINE = "该能力当前未启用，无法调用。"
TOOL_DENIED = "当前角色没有调用该工具的权限。"
TOOL_FAILED = "工具执行失败，请稍后重试或换一种问法。"

# v2.1 RAG 的作用域注入：execute_tools 在调用工具前，把**当前角色已授权的知识作用域**
# 放进这里；search_knowledge 工具在调用瞬间读取。作用域从不出现在模型的参数里 ——
# 模型不能指定检索哪个集合（US-8：角色只声明，内核掌库）。
role_knowledge_scopes_ctx: ContextVar[Sequence[str]] = ContextVar(
    "role_knowledge_scopes", default=()
)


def current_knowledge_scopes() -> Sequence[str]:
    """工具层读取：本轮角色已授权的知识作用域（execute_tools 每轮注入）。"""
    return role_knowledge_scopes_ctx.get()


# Bounded retry, per call, same arguments. Honest about what this can and cannot do: retrying
# an identical call only helps with transient failures (IO, a cold model, a locked file), and
# we cannot reliably tell transient from deterministic without inspecting exception types -
# which would couple this module to every tool's exception hierarchy. So the cap is what bounds
# the waste, and the count is surfaced in state and in the trace rather than hidden.
MAX_TOOL_RETRIES = 2


class ChatLike(Protocol):
    """Minimal model interface the kernel needs.

    Narrow on purpose: tests inject a scripted fake, and a Protocol keeps them from having to
    construct a real chat model (which would drag in a running Ollama instance).
    """

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ChatLike: ...

    def invoke(self, input: Any, **kwargs: Any) -> Any: ...


def _no_domains() -> Sequence[str]:
    """Default provider: nothing enabled.

    Fails closed on purpose. A context built without a provider binds only kernel tools, rather
    than silently binding every registered domain's tools because nobody said otherwise.
    """
    return ()


def _default_epoch() -> int:
    """Default provider: version 1, matching the `tool_epoch` row the schema seeds.

    Used only when no `PluginService` is wired in (tests, or a graph built before plugins
    existed). The real value is always read live via a `PluginService`, never snapshotted.
    """
    return 1


@dataclass(slots=True)
class KernelContext:
    """Everything the nodes need, assembled once at graph build time."""

    model: ChatLike
    registry: ToolRegistry
    roles: RoleCardService
    tracer: Tracer
    settings: Settings

    # A callable, not a list. The enabled plugin set is read fresh on every turn so that
    # "disable a plugin and it takes effect immediately" is literally true. Snapshotting it into
    # session state would make the effective set depend on when the session started - and a stale
    # snapshot that still permits a disabled plugin's tools is a silent permission bug.
    enabled_domains: Callable[[], Sequence[str]] = _no_domains

    # The tool-set version, same shape and for the same reason: read live each turn so a plugin
    # toggle that happened since the session started is detectable (C14). Stored as a callable so
    # the value is never frozen at graph-build time.
    tool_epoch: Callable[[], int] = _default_epoch

    # 角色级模型路由（US-8 后半）：按 role.model_name 解析该角色这轮用的模型。
    # None = 未接线，一律用构建期的 `model`（默认后端）。解析器由宿主提供——缓存、
    # 未知后端降级、重建失效都是宿主（settings/model_factory）的职责，内核只管"问谁要"。
    model_resolver: Callable[[str | None], ChatLike] | None = None


def turn_context(state: dict[str, Any], ctx: KernelContext) -> tuple[list[Any], list[str]]:
    """Resolve this turn's permitted tools and the plugin set they were computed against.

    Returns both, from one read, so the model's visible tool set and the executor's permitted set
    can never disagree. `state["enabled_domains"]` is used ONLY as a historical record written
    back by `call_model` - never as an input.
    """
    domains = list(ctx.enabled_domains())
    role = ctx.roles.get(state.get("current_role_id", ""))
    tools = ctx.registry.select(enabled_domains=domains, role_whitelist=role.tool_whitelist)
    return tools, domains


def tools_for_turn(state: dict[str, Any], ctx: KernelContext) -> list[Any]:
    """The tools this turn may bind. Thin wrapper over `turn_context`."""
    return turn_context(state, ctx)[0]


def _text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks (multimodal / v2 streaming shape)
        return " ".join(
            part if isinstance(part, str) else str(part.get("text", "")) for part in content
        )
    return str(content)


def call_model(
    state: dict[str, Any],
    ctx: KernelContext,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """Assemble the prompt, bind exactly the permitted tools, call the model, gate the answer.

    `config` is the LangGraph `RunnableConfig` (injected automatically when the node is wired
    with the config-aware wrapper in `core/graph.py`). Forwarding it is what makes TWO features
    possible at once, because both ride on callback propagation:

      * SSE token streaming (M4): the streaming handler LangGraph attaches travels inside
        `config`; without forwarding, the model call is invisible to it and no token is ever
        streamed.
      * Cloud tracing (US-5): the same propagation is what puts the model call on a LangSmith
        trace instead of leaving only the node-level events.

    Tests call this directly with two positional args; `config=None` keeps that path unchanged.
    """
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

    tools, domains = turn_context(state, ctx)
    # 模型解析优先级（US-8 + 会话级覆盖）：会话 model_name > 角色 model_name > 默认。
    # 会话覆盖由对话页的模型下拉写入 state（chat 端点每轮实时读库）。
    backend = state.get("model_name") or role.model_name
    base = ctx.model_resolver(backend) if ctx.model_resolver else ctx.model
    bound = base.bind_tools(tools) if tools else base

    # A session that outlived a plugin toggle can carry historical tool_calls for tools that no
    # longer exist. `execute_tools` already degrades those to "offline"; this reports the cause
    # once per change, so a confusing transcript becomes an explainable one.
    recorded_epoch = state.get("tool_epoch", 0)
    current_epoch = ctx.tool_epoch()
    if recorded_epoch != current_epoch:
        ctx.tracer.emit(
            TraceEvent(
                event="tool_epoch_drift",
                thread_id=state.get("thread_id"),
                role_id=role_id,
                detail={"recorded": recorded_epoch, "current": current_epoch},
            )
        )

    # System prompt is built here, never stored: see the module docstring. Order inside is
    # role -> exemplars -> global safety rules, so the rules remain last and authoritative.
    system = build_system_prompt(role.system_prompt, role.exemplars)
    prompt = [SystemMessage(content=system), *state["messages"]]

    with timer() as elapsed:
        invoke_kwargs = {} if config is None else {"config": config}
        response = bound.invoke(prompt, **invoke_kwargs)

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
            detail={"tools_visible": len(tools), "enabled_domains": domains},
        )
    )
    # `enabled_domains` is written back as a RECORD of what was in force this turn, and
    # `tool_epoch` as the version now seen - so the drift event fires once per toggle rather than
    # on every turn afterwards.
    return {
        "messages": [response],
        "enabled_domains": domains,
        "tool_epoch": current_epoch,
    }


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

    # v2.1：把当前角色已授权的知识作用域注入工具层（search_knowledge 读取）。
    # 角色缺失/无声明 → 空元组，检索工具自己给出明确拒绝。
    try:
        _role = ctx.roles.get(state.get("current_role_id", ""))
        role_knowledge_scopes_ctx.set(tuple(_role.knowledge_scopes or ()))
    except RoleNotFound:
        role_knowledge_scopes_ctx.set(())

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
    retries = 0
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
        attempts = 0
        last_error: str | None = None
        with timer() as elapsed:
            while True:
                try:
                    outcome = tool.invoke(args)
                    content = outcome if isinstance(outcome, str) else str(outcome)
                    break
                except Exception as exc:  # noqa: BLE001 - model gets a sentence, log gets the cause
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempts >= MAX_TOOL_RETRIES:
                        content = TOOL_FAILED
                        break
                    attempts += 1
                    ctx.tracer.emit(
                        TraceEvent(
                            event="tool_retry",
                            tool=name,
                            thread_id=state.get("thread_id"),
                            error=last_error,
                            detail={"attempt": attempts, "limit": MAX_TOOL_RETRIES},
                        )
                    )
        if last_error is not None:
            ctx.tracer.emit(
                TraceEvent(
                    event="tool_error",
                    tool=name,
                    thread_id=state.get("thread_id"),
                    error=last_error,
                    detail={"attempts": attempts + 1},
                )
            )
        results.append(ToolMessage(content=content, tool_call_id=call_id, name=name))
        retries += attempts
        ctx.tracer.emit(
            TraceEvent(
                event="tool_call",
                tool=name,
                thread_id=state.get("thread_id"),
                latency_ms=elapsed["ms"],
            )
        )

    update: dict[str, Any] = {"messages": results}
    if retries:
        # `retry_count` was a declared-but-never-written field (技术评审与决策.md §9 A4). It is
        # now the per-session tally of tool retries, which is what makes "how flaky is this
        # deployment" answerable from state instead of from log grepping.
        update["retry_count"] = (state.get("retry_count") or 0) + retries
    return update


def route_after_model(state: dict[str, Any]) -> str:
    """Loop back into the tools when the model asked for one; otherwise finish."""
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "end"
