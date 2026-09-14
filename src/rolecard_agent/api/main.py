"""FastAPI 接入层 —— 管理面 + 对话面（v1 M4）。

管理面：角色卡 CRUD 与插件启停。这两件事都是**操作员动作**，不是 LLM 工具
—— 与 `switch_role` / `plugin.set_enabled` 的设计一致（D2 / C5）：让模型移动自己的权限边界
等于自我授权。

对话面：会话创建/切角色 + `POST /api/chat` 的 SSE 流式对话。流式事件框架与增量输出审核
在 `api/chat.py`。本文件与 `call_model` / `bind_tools` 解耦：它只通过 `RoleCardService` /
`PluginService` 改库，下一轮 `call_model` 会**实时**读到新的 `enabled_domains` 与角色，
因此对插件的停用**无需重启进程**即可生效（07 C14 / US-3）。

端点：
    GET  /api/roles            角色卡列表（内置在前）
    POST /api/roles            新建角色卡
    PATCH /api/roles/{id}      局部更新角色卡
    DELETE /api/roles/{id}     删除角色卡（内置角色返回 409）
    GET  /api/plugins          插件列表（含 enabled 标志）
    POST /api/plugins/{id}/toggle  启停插件，返回新 tool_epoch
    POST /api/session          创建会话（默认绑定内置角色）
    GET  /api/session/{tid}    会话信息（含当前角色）
    PATCH /api/session/{tid}   会话切角色（US-1：历史消息不动，下一轮 prompt 换人）
    POST /api/chat             SSE 流式对话（text/event-stream）
    GET  /                     单页管理控制台 + 聊天界面（HTML）

身份说明：v1 是单用户演示，所有会话归属 `DEFAULT_USER_ID`（schema 的 user_id 列已经
就位，接入真实登录只是数据替换，不需要改表）。

连接策略：`connect()` 已设 `check_same_thread=False`，且本服务是单进程演示，所以一个进程持有一
条连接即可；并发写入由 SQLite 的锁兜底（演示负载下足够）。生产应换连接池。
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from rolecard_agent.api.chat import chat_events
from rolecard_agent.config import Settings
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.graph import build_kernel, build_model
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.nodes import ChatLike
from rolecard_agent.core.observability import Tracer, make_tracer
from rolecard_agent.core.plugins import PluginError, PluginService, UnknownPlugin
from rolecard_agent.core.state import new_state
from rolecard_agent.domains.health.service import HealthQueryService
from rolecard_agent.domains.registry import DOMAINS, build_registry
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

# v1 demo identity. The schema already carries user_id on every table; wiring real auth later
# is a data change, not a schema change (and not a v1 goal - there is no login page by design).
DEFAULT_TENANT_ID = "local"
DEFAULT_USER_ID = "local-user"
DEFAULT_ROLE_ID = "medical_archivist"


class PluginToggle(BaseModel):
    """Plugin enable/disable request body."""

    enabled: bool


class SessionCreate(BaseModel):
    """Create-session request. Omitting `role_id` binds the built-in archivist."""

    role_id: str | None = None


class SessionRole(BaseModel):
    """Switch-role request for an existing session (US-1 operator action)."""

    role_id: str


class ChatMessage(BaseModel):
    """One user turn. Length-capped so a pasted novel cannot become a checkpoint bomb."""

    thread_id: str
    message: str = Field(min_length=1, max_length=8000)


def _seed_demo_identity(conn: sqlite3.Connection) -> None:
    """v1 demo runs as one shared identity. INSERT OR IGNORE: re-running bootstrap must not
    resurrect anything, and the FK on session_thread.user_id needs this row to exist."""
    conn.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES (?, ?, '本地用户')",
        (DEFAULT_USER_ID, DEFAULT_TENANT_ID),
    )
    conn.commit()


def _get_thread(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT thread_id, user_id, current_role_id FROM session_thread WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{thread_id}")
    return row


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


def create_app(
    sqlite_path: Path | None = None,
    *,
    model: ChatLike | None = None,
    tracer: Tracer | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `sqlite_path` 可注入，便于测试用临时库。省略时回退到 `Settings.sqlite_path`
    （环境变量 `SQLITE_PATH`，默认 `./data/sqlite/app.db`）。

    `model` / `tracer` 同样可注入：测试传 `ScriptedChat` + `NullTracer` 即可全离线跑通
    对话链路（本项目的测试铁律：测内核行为，不测 LLM 本身）。省略 `model` 时用
    `build_model(settings)` 按配置实例化真实后端（Ollama 或任意 OpenAI 兼容端点），
    构造是惰性的，不会在启动时连网。
    """
    settings = Settings.from_env()
    db_path = sqlite_path or settings.sqlite_path
    conn = connect(db_path)
    # 每个 REGISTERED 域的 schema 都建好，这样表永远存在，重新启用插件无需 DDL。
    bootstrap(conn, enabled_domains=DOMAINS)
    _seed_plugin_rows(conn)
    _seed_demo_identity(conn)
    roles = RoleCardService(conn)
    roles.seed_builtins()
    plugins = PluginService(conn, known_plugins=DOMAINS)
    ingestion = IngestionService(conn)
    health_query = HealthQueryService(conn)

    # 工具注册表：内核工具 + 各域工具（domains/registry 是唯一的装配点）。
    registry = build_registry(
        roles=roles,
        ingestion=ingestion,
        query=health_query,
        enabled_domains=plugins.enabled_domains,  # callable：list_domains 报告实时状态
        current_user=lambda: DEFAULT_USER_ID,
    )

    resolved_tracer = tracer or make_tracer(settings)
    resolved_model = model or build_model(settings)
    graph = build_kernel(
        model=resolved_model,
        registry=registry,
        roles=roles,
        tracer=resolved_tracer,
        settings=settings,
        checkpointer=make_checkpointer(conn),
        plugins=plugins,
    )

    console_html = (
        _CONSOLE_HTML.read_text(encoding="utf-8")
        if _CONSOLE_HTML.exists()
        else "<h1>rolecard-agent</h1>"
    )

    app = FastAPI(title="rolecard-agent 管理控制台", version="0.2.0")

    @app.get("/api/roles")
    def list_roles() -> list[object]:
        """All role cards, built-in first."""
        return [r.model_dump(mode="json") for r in roles.list_roles()]

    @app.post("/api/roles", status_code=201)
    def create_role(data: RoleCardCreate) -> object:
        try:
            created = roles.create(data)
        except RoleAlreadyExists as exc:
            raise _role_error_to_http(exc) from exc
        return created.model_dump(mode="json")

    @app.patch("/api/roles/{role_id}")
    def update_role(role_id: str, data: RoleCardUpdate) -> object:
        try:
            updated = roles.update(role_id, data)
        except RoleNotFound as exc:
            raise _role_error_to_http(exc) from exc
        return updated.model_dump(mode="json")

    @app.delete("/api/roles/{role_id}", status_code=204)
    def delete_role(role_id: str) -> None:
        try:
            roles.delete(role_id)
        except (RoleNotFound, BuiltinRoleProtected) as exc:
            raise _role_error_to_http(exc) from exc

    @app.get("/api/plugins")
    def list_plugins() -> list[object]:
        return plugins.list_plugins()

    @app.post("/api/plugins/{plugin_id}/toggle")
    def toggle_plugin(plugin_id: str, body: PluginToggle) -> object:
        try:
            epoch = plugins.set_enabled(plugin_id, body.enabled, actor="admin")
        except PluginError as exc:
            raise _plugin_error_to_http(exc) from exc
        return {
            "plugin_id": plugin_id,
            "enabled": body.enabled,
            "tool_epoch": epoch,
        }

    @app.post("/api/session", status_code=201)
    def create_session(body: SessionCreate) -> object:
        """Create a session thread bound to a role. The thread row is what makes the
        LangGraph `thread_id` answerable to "who is talking" (core/schema.sql A2)."""
        role_id = body.role_id or DEFAULT_ROLE_ID
        try:
            role = roles.get(role_id)
        except RoleNotFound as exc:
            raise _role_error_to_http(exc) from exc
        thread_id = f"s_{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch) "
            "VALUES (?, ?, ?, ?)",
            (thread_id, DEFAULT_USER_ID, role_id, plugins.tool_epoch()),
        )
        conn.commit()
        roles.audit(
            actor="operator", action="create_session", target=thread_id, detail={"role_id": role_id}
        )
        return {"thread_id": thread_id, "role_id": role_id, "role_name": role.role_name}

    @app.get("/api/session/{thread_id}")
    def get_session(thread_id: str) -> object:
        row = _get_thread(conn, thread_id)
        try:
            role = roles.get(str(row["current_role_id"]))
            role_name: str | None = role.role_name
        except RoleNotFound:
            # 会话指向已被删除的角色：会话本身还在，角色信息降级为空（图侧有同样的兜底）。
            role_name = None
        return {
            "thread_id": row["thread_id"],
            "user_id": row["user_id"],
            "role_id": row["current_role_id"],
            "role_name": role_name,
        }

    @app.patch("/api/session/{thread_id}")
    def switch_session_role(thread_id: str, body: SessionRole) -> object:
        """US-1：切角色不触碰消息历史 —— 只有 `current_role_id` 变化，
        下一轮 system prompt 从新角色现场拼装。"""
        try:
            roles.set_thread_role(thread_id, body.role_id, actor="operator")
        except RoleError as exc:
            raise _role_error_to_http(exc) from exc  # 角色不存在或线程不存在都是 404
        role = roles.get(body.role_id)
        return {"thread_id": thread_id, "role_id": body.role_id, "role_name": role.role_name}

    @app.post("/api/chat")
    def chat(body: ChatMessage) -> StreamingResponse:
        """SSE 流式对话。线程必须已存在（POST /api/session 创建）。

        首轮注入完整初始状态（`new_state`）；续轮只注入新消息 + 实时角色 —— 后者让
        PATCH /api/session 的切角色在下一轮立即生效，而 enabled_domains / tool_epoch 不进
        输入，让 checkpoint 里的旧值保留，`call_model` 的 epoch 漂移检测才能每个变化只报
        一次（C14）。
        """
        thread = _get_thread(conn, body.thread_id)
        role_id = str(thread["current_role_id"])
        user_id = str(thread["user_id"])
        try:
            role = roles.get(role_id)
        except RoleNotFound as exc:
            raise _role_error_to_http(exc) from exc

        graph_config = {"configurable": {"thread_id": body.thread_id}}
        snapshot = graph.get_state(graph_config)
        if snapshot.values:
            graph_input: dict[str, object] = {
                "messages": [HumanMessage(content=body.message)],
                "current_role_id": role_id,
            }
        else:
            graph_input = {
                **new_state(
                    thread_id=body.thread_id,
                    user_id=user_id,
                    current_role_id=role_id,
                    enabled_domains=plugins.enabled_domains(),
                    tool_epoch=plugins.tool_epoch(),
                ),
                "messages": [HumanMessage(content=body.message)],
            }

        return StreamingResponse(
            chat_events(
                graph,
                graph_input=graph_input,
                config=graph_config,
                role_summary={"role_id": role.role_id, "role_name": role.role_name},
                tracer=resolved_tracer,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/console", response_class=HTMLResponse)
    def console() -> str:
        return console_html

    return app
