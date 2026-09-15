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
from typing import Any

from fastapi import HTTPException, Request

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
