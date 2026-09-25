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
import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from rolecard_agent.config import Settings
from rolecard_agent.core.anti_repeat import clean_repeated_spans
from rolecard_agent.core.guard import check
from rolecard_agent.core.memory import current_role_id_ctx
from rolecard_agent.core.observability import TraceEvent, Tracer, timer
from rolecard_agent.core.prompts import (
    DEPTH_INJECT_FROM_END,
    VOICE_DEPTH_PROMPT,
    build_system_prompt,
)
from rolecard_agent.core.state import now_ts
from rolecard_agent.core.text import text_of
from rolecard_agent.core.thread_locks import stop_requested
from rolecard_agent.core.tools.errors import ToolExecutionError
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService, RoleNotFound

TOOL_OFFLINE = "该能力当前未启用，无法调用。"
TOOL_DENIED = "当前角色没有调用该工具的权限。"
TOOL_FAILED = "工具执行失败，请稍后重试或换一种问法。"

# 工具循环熔断（审查报告 P0-1 的配套）：同一个工具（同参数）连续重试到上限就判定为
# 循环，回一句"请直接回答"让模型停下。实测 8B 模型在「插件停用」用例里会幻觉式地
# 反复调用同一个被拒工具，每次约 12s，一直转到步数上限（300 秒白转，评测 authz-001）。
# 熔断不是修模型，是给**一轮对话**一个可预期的上界。
MAX_REPEATED_TOOL_CALLS = 3
TOOL_LOOP_BREAK = (
    f"检测到循环：同一个工具（同参数）已连续调用 {MAX_REPEATED_TOOL_CALLS} 次。"
    "不要再调用任何工具 —— 直接回答用户，说明当前做不到这件事与原因。"
)

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


class VisionNotSupported(Exception):
    """这一轮要送出去的内容含图片，而当前后端**被确认**看不了图（P1-2 的调用前拦截）。

    抛而不是返回一句假回答：`core/turn.py` 的错误路径会把它翻译成与"供应商 400 后识别出来"
    **同一句**用户提示（`VISION_MISMATCH_DETAIL`）—— 一个条件一个句子，不分两处各写一遍。
    """


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


# 反向图搜（image_search）的图片来源注入：与 role_knowledge_scopes_ctx 同一机制。
# execute_tools 在跑工具前把**本轮最近一张图的 data URL** 放进这里；image_search 在调用
# 瞬间读取，模型无需（也不能）把巨大的 base64 塞进工具参数里。无图 → None，工具自降级。
turn_image_ctx: ContextVar[str | None] = ContextVar("turn_image", default=None)


def current_turn_image() -> str | None:
    """工具层读取：本轮最近一张图片的 data URL（execute_tools 每轮注入；无图为 None）。"""
    return turn_image_ctx.get()


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
        size = len(text_of(messages[i])) + 16  # +16：给角色/结构开销留个粗略余量
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


def _depth_insert_at(history: Sequence[Any], depth: int) -> int:
    """深度注入的落点：倒数第 `depth` 条**之前**，必要时向前退到不会拆散工具消息组的位置。

    为什么落点要挑：`AIMessage(tool_calls=[...])` 与它后面那些 `ToolMessage` 必须严格相邻，
    中间插一条系统消息就等价于亲手造出非法消息序列（供应商 400，且只在"上一轮调过工具"
    这一轮才复现 —— 最难查的那种）。所以只允许落在"上一条不是发起工具调用的 AI 消息、
    下一条不是 ToolMessage"的位置；一路退到 0 都找不到，就退化成贴在开头那条 system 之后
    （位置差一点，但发出去的序列永远合法）。
    """
    index = max(0, min(len(history) - depth, len(history)))
    while index > 0:
        prev = history[index - 1]
        cur = history[index] if index < len(history) else None
        splits_tool_group = bool(getattr(prev, "tool_calls", None)) or isinstance(cur, ToolMessage)
        if not splits_tool_group:
            return index
        index -= 1
    return 0


class ChatLike(Protocol):
    """Minimal model interface the kernel needs.

    Narrow on purpose: tests inject a scripted fake, and a Protocol keeps them from having to
    construct a real chat model (which would drag in a running Ollama instance).

    `stream` (not `invoke`) is what the kernel calls since #18: "停止生成" has to reach inside
    the model call, and a call we do not iterate cannot be interrupted. Fakes yield one chunk
    holding the whole scripted reply - accumulation is `acc + chunk`, so a single chunk is the
    identity case.
    """

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ChatLike: ...

    def invoke(self, input: Any, **kwargs: Any) -> Any: ...

    def stream(self, input: Any, **kwargs: Any) -> Any: ...


def _no_memory(_role_id: str | None = None) -> str:
    """Default memory provider: no memory.

    Fails closed on purpose. A context built without a provider injects no memory, rather
    than silently reading from some ambient store that nobody wired in (tests, or a graph
    built before memory existed).
    """
    return ""


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


def _vision_unknown(_base_url: str | None, _model: str) -> bool | None:
    """Default capability probe: **don't know**.

    三态里的 `None`。默认必须是"不知道"而不是"不能"：没接线就拦，等于让一个探测器的缺失
    变成一次功能缺失（用户会撞到"我的模型明明能看图却被挡"）。
    """
    return None


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
    model_resolver: Callable[..., ChatLike] | None = None

    # 送进模型的历史字符预算（见 trim_history）。<=0 = 不裁剪。
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS

    # 单次工具执行的总时长上限（秒），见 `_invoke_tool`。<=0 = 不设上限。
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS

    # 跨会话记忆的读取器，每次调用实时取（用户可能在设置面板里改 / 清空记忆）。
    # **按角色取**：给了 role_id 就是"该角色专属记忆，没有则回退全局"，与主动开口同源于
    # `core/memory.memory_for_turn`（以前对话侧只取全局，角色专属内容聊天时模型看不到）。
    # 返回要注入 system prompt 的记忆文本；"" = 本轮无记忆。总开关在 call_model 里
    # 用 ctx.settings.memory_enabled 把关（双保险：这里 fail-closed，门再闭一次）。
    memory_provider: Callable[[str | None], str] = _no_memory

    # 视觉能力探测（P1-2）：给 (base_url, model) 返回 True/False/**None**。宿主接线到
    # `core/probes.vision_capability`（Ollama `/api/show` 的 capabilities）；没接线就是
    # "永远不知道" ⇒ 永远不拦。内核不自己发 HTTP：能不能看图是**宿主环境**的事实。
    vision_probe: Callable[[str | None, str], bool | None] = _vision_unknown


def _turn_backend(state: dict[str, Any], role: Any, ctx: KernelContext) -> Any | None:
    """这一轮**实际会用到的后端配置**（会话覆盖 > 角色 > 默认）。解析失败返回 None。

    能力位（工具 / 视觉）都从这里取，避免"这一轮到底用哪个后端"出现两份判定。
    """
    name = state.get("model_name") or getattr(role, "model_name", None)
    try:
        return ctx.settings.backend(name)
    except Exception:  # noqa: BLE001 - 配置异常不该让整轮挂掉，退回默认（调用方按未知处理）
        return None


def _backend_supports_tools(state: dict[str, Any], role: Any, ctx: KernelContext) -> bool:
    """当前这轮实际用的后端是否支持工具调用。后端名未知/解析失败 → 保守返回 True（照常暴露）。"""
    backend = _turn_backend(state, role, ctx)
    return True if backend is None else bool(backend.supports_tools)


def _reject_unsupported_vision(
    state: dict[str, Any], role: Any, ctx: KernelContext, history: Sequence[Any]
) -> None:
    """P1-2 的调用前拦截，口径是**只拦确定的否**（用户 2026-09-20 选定）。

    放行是默认，四个条件任一成立就放行：这一轮没有图片 / 后端声明支持视觉 / 探测给不出答案
    （`None`，含云端行根本探不了）/ 探测说**能**看（探针赢过一次过期的勾选框）。
    只有"声明 false" **且** "Ollama 实测 capabilities 里没有 vision" 两条独立证据同时成立，
    才在调用前拒 —— 误杀一个能看图的模型，比多花一次 400 往返严重得多。

    拦下来抛的是 `VisionNotSupported`，由 `core/turn.model_error_detail` 翻成与 reactive
    路径同一句提示：一个条件一句人话，不分两处各写一遍。
    """
    if not _history_has_image(history):
        return
    backend = _turn_backend(state, role, ctx)
    if backend is None or backend.supports_vision:
        return
    if backend.provider != "ollama":
        return  # 云端模型探不了视觉（要真发一张图，有成本），未知 ⇒ 不拦
    if ctx.vision_probe(backend.base_url, backend.model) is not False:
        return
    ctx.tracer.emit(
        TraceEvent(
            event="vision_blocked_pre_call",
            thread_id=state.get("thread_id"),
            role_id=getattr(role, "role_id", None),
            node="call_model",
            detail={"model": backend.model, "base_url": backend.base_url},
        )
    )
    raise VisionNotSupported(f"{backend.model} 的 capabilities 不含 vision")


def turn_context(state: dict[str, Any], ctx: KernelContext) -> tuple[list[Any], list[str]]:
    """Resolve this turn's permitted tools and the plugin set they were computed against.

    Returns both, from one read, so the model's visible tool set and the executor's permitted set
    can never disagree. `state["enabled_domains"]` is used ONLY as a historical record written
    back by `call_model` - never as an input.
    """
    domains = list(ctx.enabled_domains())
    role = ctx.roles.get(state.get("current_role_id", ""))
    tools = ctx.registry.select(enabled_domains=domains, role_whitelist=role.tool_whitelist)
    # 后端能力位：当前模型不支持工具调用（如某些云端 VLM 一旦附 tools 就返回空）→ 本轮清空工具。
    # 放在这个**唯一入口**：call_model 的 bind 与 execute_tools 的 permitted 同源，模型即便幻觉出
    # tool_calls，执行侧也因不在 permitted 而拒绝（P1-3 纵深防御）。
    if tools and not _backend_supports_tools(state, role, ctx):
        tools = []
    return tools, domains


def _message_has_image(message: Any) -> bool:
    """这条消息是否携带图片：多模态 content 块（type=image_url/image），或
    `_user_message` 打的 `additional_kwargs.has_image` 标记。两条都认，兼容不同供应商形态。"""
    if (getattr(message, "additional_kwargs", None) or {}).get("has_image"):
        return True
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and (
                part.get("type") in ("image_url", "image") or "image_url" in part
            ):
                return True
    return False


def _history_has_image(messages: Sequence[Any]) -> bool:
    """本轮模型**实际能看到**的历史里是否含图片——决定要不要注入图像接地规则。
    用裁剪后的 history 判定，而不是全量 state：图已被裁掉时规则不该白占额度。"""
    return any(_message_has_image(m) for m in messages)


def _latest_image_data_url(messages: Sequence[Any]) -> str | None:
    """从历史里取**最近一条**带图消息的 data URL（反向图搜的输入）。找不到 → None。
    兼容两种形态：`image_url.url` 块，或 additional_kwargs 里直接存的 image 字符串。"""
    for m in reversed(list(messages)):
        content = getattr(m, "content", None)
        if isinstance(content, list):
            for part in reversed(content):
                if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    url = (part.get("image_url") or {}).get("url")
                    if isinstance(url, str) and url:
                        return url
    return None


def _scrub_own_repeats(
    history: list[Any], ctx: KernelContext, thread_id: str | None, role_id: str
) -> list[Any]:
    """把她历史里**重复说过的小句**从"送给模型的那一份拷贝"里去掉，原文一个字不动。

    为什么在这里做（而不是在写库时、也不是在显示时）：她复读的源头就是她自己的历史 ——
    模型看见同一句口癖出现四遍就照着续第五遍（真库实测：一条 88 字的回复与她两小时前的
    主动开口**逐字相同**）。断源头只要改"她看见的那一份"，代价为零；改原文则等于毁掉
    回放与审计的证据，那是另一条线（见 `trim_history` 的既定口径：裁剪只影响送给模型的内容）。

    三条硬约束，都在下面这段里守住：
      * **只碰她说过的纯文本消息** —— 用户的原话不是套话（那是关于他的事实），
        带图的、带工具调用的、工具结果一律不动；
      * **返回新对象**（`model_copy`），传进来的那些消息一个字段都不改 ——
        它们同时是 checkpoint 里那份历史的内存形态，改了就等于把清洗写进了库；
      * 改了什么必须留痕（`history_scrubbed`）：这道工序会让"她看到的"与"她说过的"
        不一致，没有这行 trace，下一次排查复读时那个差异就是隐形的。
    """
    own = [
        i
        for i, message in enumerate(history)
        if isinstance(message, AIMessage)
        and isinstance(message.content, str)
        and not getattr(message, "tool_calls", None)
        and message.content.strip()
    ]
    if len(own) < 2:
        return history
    cleaned = clean_repeated_spans([history[i].content for i in own])
    original = [history[i].content for i in own]
    changed = [
        (i, before, after)
        for i, before, after in zip(own, original, cleaned, strict=True)
        if before != after
    ]
    if not changed:
        return history
    out = list(history)
    for i, _before, after in changed:
        out[i] = out[i].model_copy(update={"content": after})
    ctx.tracer.emit(
        TraceEvent(
            event="history_scrubbed",
            thread_id=thread_id,
            role_id=role_id,
            node="call_model",
            detail={"messages": len(changed), "clauses": sum(len(a) for _, _, a in changed)},
        )
    )
    return out


class TurnStopped(Exception):
    """用户按了「停止生成」，而这一轮的模型调用**还没开始**。

    与"中途收手"分开处理是有必要的：中途停 = 把已经生成的那半截如实提交（她看到的就是
    历史里有的，见 `_collect_model_stream`）；还没开始就停 = 什么都不提交，检查点停在
    按下去之前那一刻 —— 留一条空的 AI 消息在历史里，界面上就是一个没人说过的气泡。
    """


def _plain_message(chunk: AIMessageChunk) -> AIMessage:
    """分块 → 一条普通的 AI 消息（搬字段，不是转类型）。

    `AIMessageChunk` 是 `AIMessage` 的子类，看着能直接用 —— 但它的 `type` 是
    `"AIMessageChunk"`，而检查点序列化与历史回放都按消息类型分流，存进去就是界面上一个
    认不出来的行。langchain-core 1.6 没有 `.message` 那个属性，所以自己搬。
    """
    return AIMessage(
        content=chunk.content,
        additional_kwargs=dict(chunk.additional_kwargs or {}),
        response_metadata=dict(chunk.response_metadata or {}),
        tool_calls=list(chunk.tool_calls or []),
        invalid_tool_calls=list(chunk.invalid_tool_calls or []),
        usage_metadata=chunk.usage_metadata,
        name=chunk.name,
    )


def _collect_model_stream(
    bound: ChatLike,
    prompt: list[Any],
    invoke_kwargs: dict[str, Any],
    *,
    thread_id: str,
) -> tuple[Any, bool]:
    """自己拿住模型的**分块流**：攒回一条完整回复，并在每个块边界问一次"该收手了吗"。

    为什么不再是 `bound.invoke()`（2026-09-25，#18）：挂了 SSE 回调时 langchain 内部走的
    确实也是流式路径，但**那条流不在我们手里** —— 于是"停止生成"只能干等：
    `await run_in_executor(next, gen)` 被取消并不中断线程池里已经在跑的那次 `next()`
    （审计 §12.12② 实测：一句被取消的 800 字生成把下一个请求排在它后面）。
    拿住流之后收手就是 `close()`，而实测**关掉连接真的放得开引擎**：同一台机 qwen3-vl:8b，
    长生成跑到第 3 块关连接，之后 1-token 探针 0.30s / 0.20s / 0.16s（基线 0.12s）——
    Ollama 在客户端断开后不到一秒就停了，云端同理是连接一断就不再计。停这才意味着省。

    返回 `(消息, 是否中途被打断)`。中途停时**丢掉半截的 tool_calls**：用户按停止的意思是
    "别说了"，不是"用没生成完的参数去执行工具"；丢掉之后图的路由自然走到结束。
    """
    if thread_id and stop_requested(thread_id):
        raise TurnStopped
    acc: Any = None
    stopped = False
    stream = bound.stream(prompt, **invoke_kwargs)
    try:
        for chunk in stream:
            if thread_id and stop_requested(thread_id):
                stopped = True
                break
            acc = chunk if acc is None else acc + chunk
    finally:
        # 显式关：让底层 HTTP 流立刻断掉，而不是等 GC。这一步才是"省下来"的那一下。
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    if acc is None:
        # 一个块都没来就被停（或模型回了个空流）：这一轮没有内容可提交。
        if not stopped:
            return AIMessage(content=""), False
        raise TurnStopped
    message = _plain_message(acc) if isinstance(acc, AIMessageChunk) else acc
    if stopped and getattr(message, "tool_calls", None):
        kept = {
            k: v
            for k, v in (getattr(message, "additional_kwargs", None) or {}).items()
            if k != "tool_calls"
        }
        message = AIMessage(content=message.content, additional_kwargs=kept)
    return message, stopped


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
    # 注：后端 supports_tools=false 时 turn_context 已把 tools 清空（bind 与执行侧同源）。
    backend = state.get("model_name") or role.model_name
    # 角色卡的 temperature 在构造期生效（见 core/graph._init_model）：
    # 解析器按 (后端, 温度) 缓存模型实例 —— 模型是跨线程共享的，事后改字段会串到别的对话。
    base = (
        ctx.model_resolver(backend, temperature=role.temperature)
        if ctx.model_resolver
        else ctx.model
    )
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
    # role -> memory -> agent plan -> exemplars -> global safety rules, so the rules remain
    # last and authoritative. Memory only enters when the master switch is on (MEMORY_ENABLED);
    # the provider itself is fail-closed (returns "" by default). Agent mode is a per-session
    # state the chat endpoint resolves live from session_thread (NULL = global default).
    # 记忆**按当前角色取**（该角色专属 → 无则回退全局），与主动开口共用一条规则。
    memory_text = ctx.memory_provider(role_id) if ctx.settings.memory_enabled else ""
    # 历史按字符预算裁剪（H3）。裁剪只影响"送给模型的内容"，checkpoint 里的完整历史不动 ——
    # 界面回放、审计、下次裁剪都仍然看得到全量对话。先裁剪，再据此判断本轮模型能否看到图片。
    history, dropped = trim_history(state["messages"], ctx.max_context_chars)
    # 套话清洗：裁剪之后、拼 prompt 之前，只改送出去的那份拷贝（红线见 `_scrub_own_repeats`）。
    history = _scrub_own_repeats(history, ctx, state.get("thread_id"), role_id)
    system = build_system_prompt(
        role.system_prompt,
        role.exemplars,
        memory=memory_text,
        agent=state.get("agent_mode") == "agent",
        has_image=_history_has_image(history),
    )
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
    # 深度注入：同一条写法要求，还要再贴着生成点说一遍（依据见 prompts.VOICE_DEPTH_PROMPT）。
    # 放在裁剪之后：插入位置按"实际发出去的那段历史"倒数，否则被裁掉的旧消息会把落点算偏。
    # `+1` 是因为 `_depth_insert_at` 数的是 history 里的位置，而 prompt 第 0 条是内核自己拼的。
    at = _depth_insert_at(history, DEPTH_INJECT_FROM_END) + 1
    prompt.insert(at, SystemMessage(content=VOICE_DEPTH_PROMPT))
    # 调用前的能力检查（P1-2）：要送出去的内容含图片、而这一轮的后端被**两条独立证据**确认
    # 看不了图 —— 就在这里拒，不要拿一次真调用去换一句供应商 400。放在裁剪之后、组装 prompt
    # 之后：判据必须与"实际发出去的内容"一致，否则被裁掉的图片会误触发拦截。
    _reject_unsupported_vision(state, role, ctx, history)

    with timer() as elapsed:
        invoke_kwargs = {} if config is None else {"config": config}
        response, stopped = _collect_model_stream(
            bound, prompt, invoke_kwargs, thread_id=str(state.get("thread_id") or "")
        )
    if stopped:
        # 中途收手：把已经生成的那半截照原样提交（她看到的与历史里的必须是同一份），
        # 并留一条痕 —— 审计 §12.12② 里"补写半句"那条 P3 的前提正是"半句没进历史"，
        # 从这一版起它不成立了：停在哪儿，历史就到哪儿。
        ctx.tracer.emit(
            TraceEvent(
                event="turn_stopped",
                node="call_model",
                thread_id=state.get("thread_id"),
                role_id=role_id,
                detail={"chars": len(text_of(response))},
            )
        )
    # 这里**不记 token 账，也不往 `node_end` 写用量**（审计 §12.8/#8）。原因不是"取不到"，
    # 而是取到的必然错：挂了 SSE 回调时 langchain 走的也是流式路径，而供应商每个分块都回一份
    # "累计到此"的 usage、合并时逐块相加 —— 实测一条"在吗"非流式 26 token、流式合并后 272,607。
    # 真值只有在分块层看得见，所以记账搬到了 `core/turn.py`（事件 `llm_usage`，按 (节点,步)
    # 取最后一次）。一个错的数比没有数有害：它会安静地喂给"今天花了多少"那个问题。

    verdict = check(text_of(response))
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
            # 用量不在这里（见上）：真值由 `core/turn.py` 的 `llm_usage` 事件带。
            # 这里留 null 是**如实**——不是"取不到"，是"这个位置上取到的一定是错的"。
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


def _consecutive_same_calls(messages: list[Any], call: dict[str, Any]) -> int:
    """最近（含本次）连续多少次要求调用「同一个工具 + 同一份参数」。

    只沿**最近的连续工具循环**回溯：AI(带该调用) → Tool(结果) → AI(带该调用) → …。
    遇到不带该调用的 AI 消息、或用户消息就停 —— 那之后属于新的用户意图，不该算循环。
    """
    key = (call.get("name", ""), json.dumps(call.get("args") or {}, sort_keys=True, default=str))
    count = 1
    for message in reversed(messages[:-1]):
        if isinstance(message, ToolMessage):
            continue  # 工具结果行：循环的"间隔"，跳过继续往回数
        tool_calls = getattr(message, "tool_calls", None) or []
        keys = {
            (c.get("name", ""), json.dumps(c.get("args") or {}, sort_keys=True, default=str))
            for c in tool_calls
        }
        if key in keys:
            count += 1
            continue
        break  # 出现了别的意图（用户消息 / 不带该调用的 AI 轮）→ 停止回溯
    return count


def execute_tools(state: dict[str, Any], ctx: KernelContext) -> dict[str, Any]:
    """Run the tool calls the model asked for, one ToolMessage per call.

    Three ways a call can fail without raising, in this order of likelihood:
      * the tool is not in the registry at all  -> offline (a plugin was disabled: C14)
      * the tool exists but the role lost access -> denied (defence in depth, D1)
      * the tool raised                          -> failed, with the stack sent to the tracer
        only

    还有一种情况排在**最前面**：模型陷入循环、反复要求同一个工具 —— 见 `MAX_REPEATED_TOOL_CALLS`。
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
    # 关系驱动主动开口（架构总览 §5）：当前对话角色注入工具层，memory_save 据此把事实
    # 同时写入该角色专属记忆（与全局 memory:facts 隔离）。无角色 = 空串，只写全局。
    # _invoke_tool 用 copy_context() 提交，所以 worker 线程能看到这里写入的值。
    current_role_id_ctx.set(state.get("current_role_id", "") or "")
    # 反向图搜的图片来源：本轮最近一张图（无图 → None，image_search 自降级）。
    turn_image_ctx.set(_latest_image_data_url(messages))

    # Three sets, and the distinction between them is the point:
    #   known     - the tool exists in the registry at all
    #   stage_one - its owning plugin is currently enabled
    #   permitted - stage_one AND allowed by the role's whitelist
    # "offline" and "denied" are different answers to the user, so they are computed
    # separately rather than collapsed into one "not allowed".
    #
    # 两个集合都从**同一次** `turn_context` 取（P1-9）：以前 stage_one 读
    # `state["enabled_domains"]`（本轮 bind 时写下的**录制值**），而 permitted 走实时 callable ——
    # 于是"插件刚刚被关掉"这种情况会被答成 denied（"这个角色没这个权限"），而真实答案是
    # offline（"这个插件已经关了"）。给用户错的那一句，比不给解释更糟。
    tools, domains = turn_context(state, ctx)
    known = set(ctx.registry.names())
    stage_one = {
        t.name for t in ctx.registry.select(enabled_domains=domains, role_whitelist=None)
    }
    permitted = {t.name for t in tools}

    results: list[ToolMessage] = []
    retries = 0
    for call in calls:
        name = call.get("name", "")
        call_id = call.get("id", "")
        args = call.get("args") or {}

        # 循环熔断放在**所有分支之前**：离线 / 被拒 / 正常工具都可能被模型反复调用，
        # 而循环与否与工具是否可用无关。触发时不执行工具，回一句"请直接回答"。
        if _consecutive_same_calls(messages, call) >= MAX_REPEATED_TOOL_CALLS:
            results.append(
                ToolMessage(
                    content=TOOL_LOOP_BREAK,
                    tool_call_id=call_id,
                    name=name,
                    additional_kwargs={"created_at": now_ts()},
                )
            )
            ctx.tracer.emit(
                TraceEvent(
                    event="tool_loop_break",
                    tool=name,
                    thread_id=state.get("thread_id"),
                    detail={"threshold": MAX_REPEATED_TOOL_CALLS},
                )
            )
            continue

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
