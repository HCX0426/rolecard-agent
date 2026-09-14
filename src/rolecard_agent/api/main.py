"""FastAPI 接入层 —— 管理面（v1 的一部分，对应 实施计划.md 的 M4 端点，提前到 M2 收尾落地）。

本文件只承载**管理面**：角色卡 CRUD 与插件启停。这两件事都是**操作员动作**，不是 LLM 工具
—— 与 `switch_role` / `plugin.set_enabled` 的设计一致（D2 / C5）：让模型移动自己的权限边界
等于自我授权。

聊天（SSE）端点是 M4 的剩余部分，不在这里。本文件与 `call_model` / `bind_tools` 解耦：它只
通过 `RoleCardService` / `PluginService` 改库，下一轮 `call_model` 会**实时**读到新的
`enabled_domains` 与角色，因此对插件的停用**无需重启进程**即可生效（`07` C14 / US-3）。

端点：
    GET  /api/roles            角色卡列表（内置在前）
    POST /api/roles            新建角色卡
    PATCH /api/roles/{id}      局部更新角色卡
    DELETE /api/roles/{id}     删除角色卡（内置角色返回 409）
    GET  /api/plugins          插件列表（含 enabled 标志）
    POST /api/plugins/{id}/toggle  启停插件，返回新 tool_epoch
    GET  /                    单页管理控制台（HTML）

连接策略：`connect()` 已设 `check_same_thread=False`，且本服务是单进程演示，所以一个进程持有一
条连接即可；并发写入由 SQLite 的锁兜底（演示负载下足够）。生产应换连接池。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from rolecard_agent.config import Settings
from rolecard_agent.core.plugins import PluginError, PluginService, UnknownPlugin
from rolecard_agent.domains.registry import DOMAINS
from rolecard_agent.roles.models import RoleCardCreate, RoleCardUpdate
from rolecard_agent.roles.service import (
    BuiltinRoleProtected,
    RoleAlreadyExists,
    RoleCardService,
    RoleError,
    RoleNotFound,
)
from rolecard_agent.storage.db import bootstrap, connect

_CONSOLE_HTML = Path(__file__).resolve().parent / "console.html"


class PluginToggle(BaseModel):
    """Plugin enable/disable request body."""

    enabled: bool


def _role_error_to_http(exc: RoleError) -> HTTPException:
    """Map a domain error to the right HTTP status. Never leaks a stack trace."""
    if isinstance(exc, RoleNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (RoleAlreadyExists, BuiltinRoleProtected)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _plugin_error_to_http(exc: PluginError) -> HTTPException:
    if isinstance(exc, UnknownPlugin):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _seed_plugin_rows(conn: sqlite3.Connection) -> None:
    """Mirror `scripts/init_db.py`: every REGISTERED domain gets a plugin row, on by default.

    `ON CONFLICT(plugin_id) DO NOTHING` —— 重跑绝不能把操作员关掉的插件重新打开。表一旦存在就是
    唯一事实来源。
    """
    for domain in DOMAINS:
        conn.execute(
            "INSERT INTO plugin (plugin_id, display_name, enabled, sort_order) "
            "VALUES (?, ?, 1, 0) ON CONFLICT(plugin_id) DO NOTHING",
            (domain, domain),
        )
    conn.commit()


def create_app(sqlite_path: Path | None = None) -> FastAPI:
    """Build the FastAPI app.

    `sqlite_path` 可注入，便于测试用临时库。省略时回退到 `Settings.sqlite_path`
    （环境变量 `SQLITE_PATH`，默认 `./data/sqlite/app.db`）。
    """
    settings = Settings.from_env()
    db_path = sqlite_path or settings.sqlite_path
    conn = connect(db_path)
    # 每个 REGISTERED 域的 schema 都建好，这样表永远存在，重新启用插件无需 DDL。
    bootstrap(conn, enabled_domains=DOMAINS)
    _seed_plugin_rows(conn)
    roles = RoleCardService(conn)
    roles.seed_builtins()
    plugins = PluginService(conn, known_plugins=DOMAINS)

    console_html = (
        _CONSOLE_HTML.read_text(encoding="utf-8") if _CONSOLE_HTML.exists() else "<h1>rolecard-agent</h1>"
    )

    app = FastAPI(title="rolecard-agent 管理控制台", version="0.1.0")

    @app.get("/api/roles")
    def list_roles() -> list[object]:
        """All role cards, built-in first."""
        return [r.model_dump(mode="json") for r in roles.list_roles()]

    @app.post("/api/roles", status_code=201)
    def create_role(data: RoleCardCreate) -> object:
        try:
            created = roles.create(data)
        except RoleAlreadyExists as exc:
            raise _role_error_to_http(exc)
        return created.model_dump(mode="json")

    @app.patch("/api/roles/{role_id}")
    def update_role(role_id: str, data: RoleCardUpdate) -> object:
        try:
            updated = roles.update(role_id, data)
        except RoleNotFound as exc:
            raise _role_error_to_http(exc)
        return updated.model_dump(mode="json")

    @app.delete("/api/roles/{role_id}", status_code=204)
    def delete_role(role_id: str) -> None:
        try:
            roles.delete(role_id)
        except (RoleNotFound, BuiltinRoleProtected) as exc:
            raise _role_error_to_http(exc)

    @app.get("/api/plugins")
    def list_plugins() -> list[object]:
        return plugins.list_plugins()

    @app.post("/api/plugins/{plugin_id}/toggle")
    def toggle_plugin(plugin_id: str, body: PluginToggle) -> object:
        try:
            epoch = plugins.set_enabled(plugin_id, body.enabled, actor="admin")
        except PluginError as exc:
            raise _plugin_error_to_http(exc)
        return {
            "plugin_id": plugin_id,
            "enabled": body.enabled,
            "tool_epoch": epoch,
        }

    @app.get("/", response_class=HTMLResponse)
    @app.get("/console", response_class=HTMLResponse)
    def console() -> str:
        return console_html

    return app
