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

import contextvars
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from rolecard_agent.config import Settings
from rolecard_agent.core.guard import check
from rolecard_agent.core.observability import TraceEvent, Tracer, timer
from rolecard_agent.core.prompts import build_system_prompt
from rolecard_agent.core.state import now_ts
from rolecard_agent.core.tools.errors import ToolExecutionError
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService, RoleNotFound

TOOL_OFFLINE = "该能力当前未启用，无法调用。"
TOOL_DENIED = "当前角色没有调用该工具的权限。"
TOOL_FAILED = "工具执行失败，请稍后重试或换一种问法。"

# 工具执行的**总时长**上限（审查报告 M10）。工具各自的内部超时（OCR 子进程 120s、
# 云端 HTTP 30s、模型 120s）管的是"这一次网络调用"，管不了"这个工具整体跑多久"
# —— 一次坏了的分页循环就能让图执行线程一直被占着。SSE 是同步生成器、跑在 Starlette
# 线程池上，几个挂住的工具就能把整个服务的对话一起拖停。
#
# 实现代价必须说清：超时只放弃等待，**杀不掉已经在跑的线程**。所以：
#   * 执行器有固定的 worker 上限（`_TOOL_MAX_WORKERS`），坑位是有界的；
#   * 只有 `idempotent` 的工具才重试，超时不会在写工具上叠加副作用。
_TOOL_MAX_WORKERS = 16
_tool_executor = ThreadPoolExecutor(max_workers=_TOOL_MAX_WORKERS, thread_name_prefix="tool-exec")

# 默认工具总时长上限（秒）。取 120 与模型超时同量级：单个工具的合理最坏情况（OCR 子进程
# 120s）不会被误杀，而"无限挂起"被挡住。
DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0


class ToolTimeout(Exception):
    """工具在预算内没有返回。转成 `TOOL_FAILED` 给模型，原因进轨迹。"""


def _invoke_tool(tool: Any, args: dict[str, Any], timeout_seconds: float) -> Any:
    """在独立线程里执行工具，并施加总时长上限。

    **必须提交 `copy_context()`**：`search_knowledge` 从 `role_knowledge_scopes_ctx` 读
    当前角色已授权的知识作用域，而 ContextVar 不会自动传播到新线程。不拷贝上下文的话，
    检索工具会看到空作用域、直接回答"当前角色未授权任何知识作用域" —— 一个由并发实现
    引入的、与权限相关的静默故障。测试 `test_search_tool_sees_the_role_scopes` 守这一点。

    `timeout_seconds <= 0` = 不设上限，直接在当前线程调用（调试用，也保留了"没有
    线程池参与"的原始路径）。
    """
    if timeout_seconds <= 0:
        return tool.invoke(args)
    copied = contextvars.copy_context()
    future = _tool_executor.submit(copied.run, tool.invoke, args)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeout as exc:
        future.cancel()
        raise ToolTimeout(f"工具执行超过 {timeout_seconds:g}s 未返回") from exc

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
#
# 两个前提条件（审查报告 M10 修正）：
#   1. **只有 `idempotent` 的工具才重试**（registry 里的显式声明，默认不重试）。对写工具
#      重试不是"多花一次调用"，而是**多一条副作用**；
#   2. 重试同参数，所以真正的收益只在瞬时故障上（见上）。
MAX_TOOL_RETRIES = 2

# 上下文预算（审查报告 H3）。用**字符数**而不是 token：与 roles/models.py 的
# MAX_EXEMPLAR_CHARS 同一取舍 —— 中文场景下字符数是保守代理，且不引入分词器依赖。
DEFAULT_MAX_CONTEXT_CHARS = 24000


def _can_start_history(message: Any) -> bool:
    """这条消息能否作为送给模型的**历史起点**。

    ToolMessage 不行：它必须紧跟在带 `tool_calls` 的 AIMessage 之后，单独出现会被供应商
    判为非法消息序列（400），比"超窗"更难排查。SystemMessage 也不行：内核每次都在最前面
    自己拼一条，历史里再留一条就变成两条系统消息。
    """
    return not isinstance(message, (ToolMessage, SystemMessage))


def trim_history(messages: Sequence[Any], max_chars: int) -> tuple[list[Any], int]:
    """从最近往回保留历史，直到字符预算用完。返回 `(保留的消息, 丢弃的条数)`。

    ## 为什么必须做

    `state["messages"]` 只增不减（`add_messages` 的语义），而 `call_model` 把它**全量**
    拼进 prompt。单条消息有 8000 字上限，**轮次却没有上限** —— 几十轮之后必然超出后端
    上下文窗口，用户拿到的是一句"模型调用失败"，无从判断是历史太长。

    ## 截断点为什么不能随便选

    只按预算切会把历史切在任意位置：一旦起点落在 ToolMessage 上，就等价于"扔掉了发起
    这次调用的 AIMessage、却留下了结果"，消息序列不再合法，多数供应商会直接 400。
    所以按预算定出起点之后要**向前回退**（把更多上下文包含进来）到工具组的头部
    —— 宁可多留几条，也不发出非法序列，而且绝不会丢掉最后一条消息。

    `max_chars <= 0` = 不裁剪（显式关闭，供调试与"我很确定不会超窗"的场景）。
    """
    if max_chars <= 0 or not messages:
        return list(messages), 0
    total = 0
    start = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        size = len(_text_of(messages[i])) + 16  # +16：给角色/结构开销留个粗略余量
        if total + size > max_chars and start < len(messages):
            break
        total += size
        start = i
    # 回退到工具组头部：ToolMessage 必须与发起它的 AIMessage 同进同出。
    while start > 0 and not _can_start_history(messages[start]):
        start -= 1
    if not _can_start_history(messages[start]):
        # 只可能发生在首条本身非法（外部手工构造 / 迁移过来的历史）。这时**向前**推进到
        # 最近的一个合法位置：宁可多丢几条，也不把非法序列发出去。
        # 从 `start` 往后扫而不是从 0 扫 —— 往前找会重新把已丢弃的旧消息拉回来，破坏预算。
        advanced = next(
            (i for i in range(start, len(messages)) if _can_start_history(messages[i])), None
        )
        if advanced is None:
            return list(messages), 0
        start = advanced
    return list(messages[start:]), start


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

    # 送进模型的历史字符预算（见 trim_history）。<=0 = 不裁剪。
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS

    # 单次工具执行的总时长上限（秒），见 `_invoke_tool`。<=0 = 不设上限。
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS


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


def _text_of(message: Any) -> str:
    """把消息压成纯文本。`content` 可能是 str，也可能是分块列表（多模态 / 流式形态）。

    这是全项目**唯一**的实现：`api/deps.py` 曾有一份语义不同的副本（那份用 `"".join`），
    于是同一条消息在"流式输出"与"历史回放"两条路径上会渲染出不同的文本（审查报告 M8）。
    对分块内容用空格连接：可读性优先，且两侧现在用的是同一个函数、不存在偏差。

    入参类型是 `Any` 而不是 `BaseMessage`：调用点既有真实消息对象，也有
    `getattr(msg, "content", msg)` 这类"可能是任意对象"的场景。
    宽容处理异常分块：这个函数跑在 SSE 循环里，一个形状意外的块不该让整轮对话挂掉。
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
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
        reply = AIMessage(content="当前对话绑定的角色已不存在，请重新选择一个角色。")
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
    # 历史按字符预算裁剪（H3）。裁剪只影响"送给模型的内容"，checkpoint 里的完整历史不动 ——
    # 界面回放、审计、下次裁剪都仍然看得到全量对话。
    history, dropped = trim_history(state["messages"], ctx.max_context_chars)
    if dropped:
        ctx.tracer.emit(
            TraceEvent(
                event="context_trimmed",
                thread_id=state.get("thread_id"),
                role_id=role_id,
                detail={
                    "dropped_messages": dropped,
                    "kept_messages": len(history),
                    "budget_chars": ctx.max_context_chars,
                },
            )
        )
    prompt = [SystemMessage(content=system), *history]

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
            detail={
                "tools_visible": len(tools),
                "enabled_domains": domains,
                "history_messages": len(history),
                "history_dropped": dropped,
            },
        )
    )
    # `enabled_domains` is written back as a RECORD of what was in force this turn, and
    # `tool_epoch` as the version now seen - so the drift event fires once per toggle rather than
    # on every turn afterwards. `context_trimmed` / `context_kept` 同理：记录"这一轮模型实际
    # 看到了多少历史"，界面据此如实提示，而不是让用户自己猜"模型怎么忘了前面说的"
    # （审查报告 H3 的界面部分）。
    # created_at 随回复入库：历史回放显示时间（用户 2026-09-17）。
    response.additional_kwargs.setdefault("created_at", now_ts())
    return {
        "messages": [response],
        "enabled_domains": domains,
        "tool_epoch": current_epoch,
        "context_trimmed": dropped,
        "context_kept": len(history),
    }


def execute_tools(state: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    """Run the tool calls the model asked for, one ToolMessage per call.

    Three ways a call can fail without raising, in this order of likelihood:
      * the tool is not in the registry at all  -> offline (a plugin was disabled: C14)
      * the tool exists but the role lost access -> denied (defence in depth, D1)
      * the tool raised                          -> failed, with the stack sent to the tracer
        only
    """
    messages = state.get("messages") or []
    # 空消息态不该 IndexError：这里的目的是"把模型要求的工具跑掉"，没有消息就是没有请求。
    last = messages[-1] if messages else None
    calls = (getattr(last, "tool_calls", None) or []) if last is not None else []

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
            results.append(
                ToolMessage(
                    content=TOOL_OFFLINE,
                    tool_call_id=call_id,
                    name=name,
                    additional_kwargs={"created_at": now_ts()},
                )
            )
            ctx.tracer.emit(
                TraceEvent(event="tool_offline", tool=name, thread_id=state.get("thread_id"))
            )
            continue
        if name not in permitted:
            results.append(
                ToolMessage(
                    content=TOOL_DENIED,
                    tool_call_id=call_id,
                    name=name,
                    additional_kwargs={"created_at": now_ts()},
                )
            )
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
        # 只有显式声明为幂等的工具才允许重试（见 ToolSpec / MAX_TOOL_RETRIES）。
        retryable = ctx.registry.is_idempotent(name)
        attempts = 0
        last_error: str | None = None
        with timer() as elapsed:
            while True:
                try:
                    outcome = _invoke_tool(tool, args, ctx.tool_timeout_seconds)
                    content = outcome if isinstance(outcome, str) else str(outcome)
                    break
                except Exception as exc:  # noqa: BLE001 - model gets a sentence, log gets the cause
                    last_error = f"{type(exc).__name__}: {exc}"
                    # 预料内的工具错误（WebToolError / FsToolError 等）message 本身就是
                    # 设计过的用户文案：透传原因 —— 模型能据此向用户解释（"搜索超时，
                    # 可能需要配 TAVILY_API_KEY"），工具卡也能显示真实原因，而不是让
                    # 用户对着一句万能话猜。未知异常仍回通用句（栈/内部路径不外泄）；
                    # 两种情况 last_error 都进 tracer 日志（带异常类型 + 消息体，E1）。
                    content = (
                        f"工具执行失败：{exc}"
                        if isinstance(exc, ToolExecutionError)
                        else TOOL_FAILED
                    )
                    if not retryable or attempts >= MAX_TOOL_RETRIES:
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
                    # `retryable=False` 时 attempts 恒为 1：这是**有意不重试**，不是漏了重试。
                    detail={"attempts": attempts + 1, "retryable": retryable},
                )
            )
        results.append(
            ToolMessage(
                content=content,
                tool_call_id=call_id,
                name=name,
                additional_kwargs={"created_at": now_ts()},
            )
        )
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
    messages = state.get("messages") or []
    if not messages:
        return "end"
    last = messages[-1]
    return "tools" if getattr(last, "tool_calls", None) else "end"
