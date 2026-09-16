"""接入层的共享上下文与依赖 —— 把 `create_app` 大闭包里的东西变成可注入的。

拆 `main.py` 的真正难点不是"把函数搬走"，而是那 24 个端点共享的闭包状态（连接、服务、
图句柄、热重建函数）。把它们收进一个 `AppContext`，由依赖注入传递，路由模块就不再需要
闭包，**每个 router 都能独立读、独立测**。

`AppContext` 里的服务持有的是 `ThreadLocalConnection`（storage/db.py）：对外表现为一条
连接，内部按线程分发 —— 所以跨 router 共享同一个实例是安全的。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rolecard_agent.api.auth import Actor
from rolecard_agent.config import Settings
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.observability import Tracer
from rolecard_agent.core.plugins import PluginError, PluginService, UnknownPlugin
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.domains.health.service import HealthQueryService
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


def serialize_message(message: object) -> dict[str, object]:
    """Checkpoint message -> JSON shape for the frontend history replay."""
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _text_of(message)}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "name": message.name, "content": _text_of(message)}
    if isinstance(message, AIMessage):
        tools = [tc.get("name") for tc in (message.tool_calls or [])]
        return {"role": "assistant", "content": _text_of(message), "tools": tools}
    return {"role": "assistant", "content": _text_of(message)}


def _text_of(message: object) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


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
    health: HealthQueryService
    model_settings: ModelSettingsService
    knowledge: KnowledgeBase
    registry: ToolRegistry
    tracer: Tracer

    # 图句柄：设置页保存后整体热重建，所以是可变容器而不是直接持有 graph 对象。
    app_state: dict[str, Any] = field(default_factory=dict)
    # 热重建入口（设置页保存时调用）—— 见 main.create_app 里的实现。
    rebuild_graph: Callable[[], None] = field(default=lambda: None)


def get_context(request: Request) -> AppContext:
    """取应用上下文。端点通过 `Depends(get_context)` 拿到全部服务，不需闭包。"""
    return request.app.state.ctx  # type: ignore[no-any-return]


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
