"""接入层的共享上下文与依赖 —— 把 `create_app` 大闭包里的东西变成可注入的。

拆 `main.py` 的真正难点不是"把函数搬走"，而是那 24 个端点共享的闭包状态（连接、服务、
图句柄、热重建函数）。把它们收进一个 `AppContext`，由依赖注入传递，路由模块就不再需要
闭包，**每个 router 都能独立读、独立测**。

`AppContext` 里的服务持有的是 `ThreadLocalConnection`（storage/db.py）：对外表现为一条
连接，内部按线程分发 —— 所以跨 router 共享同一个实例是安全的。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rolecard_agent.api.auth import Actor
from rolecard_agent.config import Settings
from rolecard_agent.core.domain_service import DomainQueryService
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.nodes import _text_of
from rolecard_agent.core.observability import Tracer
from rolecard_agent.core.plugins import PluginError, PluginService, UnknownPlugin
from rolecard_agent.core.services import ServiceEndpointService
from rolecard_agent.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from rolecard_agent.rag.ocr import OcrBackend
from rolecard_agent.rag.retriever import KnowledgeBase
from rolecard_agent.roles.service import (
    BuiltinRoleProtected,
    RoleAlreadyExists,
    RoleCardService,
    RoleNotFound,
)
from rolecard_agent.storage.db import ThreadLocalConnection

# v1 演示身份（schema 每张表都有 user_id 列；接真实登录是数据替换，不是改表）。
DEFAULT_TENANT_ID = "local"
DEFAULT_USER_ID = "local-user"
# 默认"无角色"：纯对话，不接工具与检索。
DEFAULT_ROLE_ID = "general_assistant"


def get_thread(conn: ThreadLocalConnection, thread_id: str):
    """按 id 取会话行；不存在 404。多个 router 共用（sessions / chat / upload）。"""
    row = conn.execute(
        "SELECT thread_id, user_id, current_role_id, model_name FROM session_thread "
        "WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{thread_id}")
    return row


def serialize_message(
    message: object, call_args: dict[str, dict[str, Any]] | None = None
) -> dict[str, object]:
    """Checkpoint message -> JSON shape for the frontend history replay.

    `id` 是前端**编辑 / 删除某条消息**时的寻址依据：LangGraph 的 `add_messages`
    按 id 去重与删除（`RemoveMessage(id=...)`），没有 id 就无法精确改动历史中的一条。
    `call_args`（tool_call_id → 入参）由调用方从消息序列里预先配对 —— 工具行回放时
    才能显示"搜了什么"（单条 ToolMessage 自己看不到入参）。
    """
    created_at = (getattr(message, "additional_kwargs", None) or {}).get("created_at")
    row: dict[str, object]
    if isinstance(message, HumanMessage):
        row = {
            "role": "user",
            "content": _text_of(message),
            "id": message.id,
        }
    elif isinstance(message, ToolMessage):
        trow: dict[str, object] = {
            "role": "tool",
            "name": message.name,
            "content": _text_of(message),
            "id": message.id,
        }
        if args := (call_args or {}).get(message.tool_call_id):
            trow["args"] = args  # 历史工具卡显示"搜了什么"（用户 2026-09-17 反馈）
        if created_at:
            trow["ts"] = str(created_at)
        return trow
    elif isinstance(message, AIMessage):
        tools = [tc.get("name") for tc in (message.tool_calls or [])]
        row = {
            "role": "assistant",
            "content": _text_of(message),
            "tools": tools,
            "id": message.id,
        }
        # 思考内容随消息一起回放。为什么不只在前台的 live 气泡里显示：一轮结束后前端会以
        # checkpoint 回放**整体替换**消息区（乐观气泡连同思考一起被销毁），用户就再也看不到
        # 推理过程了。把 reasoning 放进回放数据，思考过程才和回答一样是历史的一部分。
        reasoning = (message.additional_kwargs or {}).get("reasoning_content")
        if reasoning:
            row["reasoning"] = reasoning
    else:
        row = {
            "role": "assistant",
            "content": _text_of(message),
            "id": getattr(message, "id", None),
        }
    if created_at:
        row["ts"] = str(created_at)  # 旧消息没有该字段 → 不显示时间
    return row


def group_turns(messages: Sequence[object]) -> list[list[int]]:
    """把消息序列切成"一轮问答"：`[用户消息, (工具消息…), 助手回答(可无)]`。

    为什么要有这个函数：删除一条消息时，只删用户消息会留下孤立的助手回答，只删助手回答
    会留下没有答案的提问，而**工具消息与发起它的 AI 消息必须同生共死**（切断配对会被
    供应商判为非法序列）。所以"选中一条 = 选中整轮"由后端统一执行，前端只传 id。

    规则：每遇到一条用户消息就开启新一轮；首条不是用户消息时（历史被删过），
    它自成一轮，保证删除不会漏掉孤儿消息。
    """
    turns: list[list[int]] = []
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage) or not turns:
            turns.append([index])
        else:
            turns[-1].append(index)
    return turns


def expand_to_turns(messages: Sequence[object], ids: Sequence[str]) -> list[str]:
    """把"用户选中的若干 id"扩展为**整轮的 id 集合**（含配对的助手回答与工具消息）。"""
    wanted = set(ids)
    turns = group_turns(messages)
    out: list[str] = []
    for turn in turns:
        indices = {getattr(messages[i], "id", None) for i in turn}
        if indices & wanted:
            out.extend(str(i) for i in indices if i is not None)
    # 保持原有顺序，便于按序删除
    order = {getattr(m, "id", None): n for n, m in enumerate(messages)}
    return sorted(set(out), key=lambda i: order.get(i, 0))


def parsed_text_path(target: Path) -> Path:
    """解析文本的落点：`<上传文件>.parsed.txt`（与上传文件同目录，随 uploads/ 一起被 gitignore）。

    为什么落盘：结构化抽取需要原文，而图片的解析要走 OCR 子进程（很贵）。上传时顺手存一份，
    抽取就不必再跑一次 OCR。
    """
    return target.with_name(target.name + ".parsed.txt")


@dataclass(slots=True)
class AppContext:
    """一次应用启动的全部运行时依赖。挂在 `app.state.ctx` 上，由 `get_context` 注入。"""

    settings: Settings
    conn: ThreadLocalConnection
    roles: RoleCardService
    plugins: PluginService
    ingestion: IngestionService
    # M9：只依赖域查询抽象，不持有 health 具体类（api 层不直接 import 具体域）。
    health: DomainQueryService
    model_settings: ModelSettingsService
    services: ServiceEndpointService
    knowledge: KnowledgeBase
    registry: ToolRegistry
    tracer: Tracer

    # 图句柄：设置页保存后整体热重建，所以是可变容器而不是直接持有 graph 对象。
    app_state: dict[str, Any] = field(default_factory=dict)
    # 运行时热重建入口（模型设置或服务策略保存时调用）—— 见 main.create_app 的实现。
    rebuild_runtime: Callable[[], None] = field(default=lambda: None)

    def ocr_candidates(self) -> OcrBackend | None:
        """按「服务」页的 OCR 端点序选一个可用后端（L3：两处重复调用收拢到此）。

        records（兜底现场解析）与 sessions（上传解析）此前各写一遍同样的
        `select_ocr_backend(settings, order=…, endpoints=…)` —— 抽成方法后调用方只剩
        一行，选择策略改动只碰这里。惰性 import 避免 api → rag 的模块级耦合。
        """
        from rolecard_agent.rag.ocr import select_ocr_backend

        return select_ocr_backend(
            self.settings,
            order=[c.id for c in self.services.ordered_candidates("ocr")],
            endpoints=self.services.endpoint_map("ocr"),
        )


def get_context(request: Request) -> AppContext:
    """取应用上下文。端点通过 `Depends(get_context)` 拿到全部服务，不需闭包。"""
    # `app.state` 上的属性在类型系统里是 Any（Starlette 的动态属性），这里显式收敛成
    # AppContext —— 比留一个"看起来在防 Any 其实没生效"的 ignore 更诚实。
    return cast("AppContext", request.app.state.ctx)


def get_actor(request: Request) -> Actor:
    """取认证中间件解析好的身份 —— 端点用它把真实 actor 写进审计日志。"""
    from rolecard_agent.api.auth import Actor as _Actor

    return getattr(request.state, "actor", _Actor())


# ---------------------------------------------------------------- 领域错误 → HTTP


def role_error_to_http(exc: Exception) -> HTTPException:
    """Map a domain error to the right HTTP status. Never leaks a stack trace."""
    if isinstance(exc, RoleNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (RoleAlreadyExists, BuiltinRoleProtected)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def plugin_error_to_http(exc: PluginError) -> HTTPException:
    if isinstance(exc, UnknownPlugin):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))
