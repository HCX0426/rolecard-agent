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
    GET  /api/sessions         会话列表（对话页侧栏，按更新时间倒序）
    POST /api/session          创建会话（默认绑定内置角色）
    GET  /api/session/{tid}    会话信息（含当前角色）
    PATCH /api/session/{tid}   会话切角色（US-1：历史消息不动，下一轮 prompt 换人）
    DELETE /api/session/{tid}  删除会话（含 checkpoint 清理）
    GET  /api/session/{tid}/messages  历史消息（checkpoint 回放，供续聊）
    POST /api/chat             SSE 流式对话（text/event-stream）
    GET  /api/settings/models  模型后端设置（api_key 只写不回读）
    PUT  /api/settings/models  保存后端集合并热重建（下一轮对话即生效，无需重启）
    GET  /                     控制台（M5 前端构建产物；未构建时回退提示页）

身份说明：v1 是单用户演示，所有会话归属 `DEFAULT_USER_ID`（schema 的 user_id 列已经
就位，接入真实登录只是数据替换，不需要改表）。

连接策略：`connect()` 已设 `check_same_thread=False`，且本服务是单进程演示，所以一个进程持有一
条连接即可；并发写入由 SQLite 的锁兜底（演示负载下足够）。生产应换连接池。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

from rolecard_agent.api.chat import chat_events
from rolecard_agent.config import Settings
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.graph import build_kernel, build_model
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.model_settings import ModelSettingsError, ModelSettingsService
from rolecard_agent.core.nodes import ChatLike, _text_of
from rolecard_agent.core.observability import TraceEvent, Tracer, make_tracer
from rolecard_agent.core.plugins import PluginError, PluginService, UnknownPlugin
from rolecard_agent.core.state import new_state
from rolecard_agent.domains.health.service import (
    HealthDataError,
    HealthNotFound,
    HealthQueryService,
)
from rolecard_agent.domains.registry import DOMAINS, build_registry
from rolecard_agent.rag.retriever import KnowledgeBase, make_embedder
from rolecard_agent.roles.models import RoleCardCreate, RoleCardUpdate
from rolecard_agent.roles.service import (
    BuiltinRoleProtected,
    RoleAlreadyExists,
    RoleCardService,
    RoleError,
    RoleNotFound,
)
from rolecard_agent.storage.db import bootstrap, connect

# M5 前端构建产物的位置：frontend/dist（仓库根下）。可用环境变量 FRONTEND_DIST 覆盖
# （部署布局变化时不必移动文件）。未构建时控制台路由返回回退提示页，后端 API 不受影响。
_DEFAULT_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

_FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>rolecard-agent 管理控制台</title></head>
<body style="font-family:system-ui;padding:40px;line-height:1.8">
<h1>rolecard-agent 管理控制台</h1>
<p>前端尚未构建。请执行：</p>
<pre>cd frontend
npm install
npm run build</pre>
<p>构建后刷新本页即可看到完整控制台（或使用 <code>GET /api/*</code> 直接调用接口）。</p>
</body></html>"""

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


class SessionPatch(BaseModel):
    """Session partial update: switch role and/or rename. At least one field required."""

    role_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=100)


class ChatMessage(BaseModel):
    """One user turn. Length-capped so a pasted novel cannot become a checkpoint bomb."""

    thread_id: str
    message: str = Field(min_length=1, max_length=8000)


class BackendSpec(BaseModel):
    """One model backend row from the settings page.

    `api_key` is write-only: omitted/None = keep the stored key for this name; "" = clear it.
    The GET endpoint never returns keys, so this round-trip rule is what keeps saves from
    silently erasing them.
    """

    name: str = Field(min_length=1, max_length=32)
    provider: str = Field(min_length=1)
    base_url: str | None = None
    model: str = Field(min_length=1)
    api_key: str | None = None


class ModelSettingsBody(BaseModel):
    default: str
    backends: list[BackendSpec]
    fallbacks: list[str] = Field(default_factory=list)


class IndexPatch(BaseModel):
    """Data-management correction for one indicator row. `exclude_unset` semantics:
    a field explicitly set to null means "clear it" (e.g. switching value -> text)."""

    index_value: float | None = None
    value_text: str | None = None
    unit: str | None = None
    ref_range: str | None = None
    is_verified: bool | None = None


def _serialize_message(message: object) -> dict[str, object]:
    """Checkpoint message -> JSON shape for the frontend history replay."""
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _text_of(message)}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "name": message.name, "content": _text_of(message)}
    if isinstance(message, AIMessage):
        tools = [tc.get("name") for tc in (message.tool_calls or [])]
        return {"role": "assistant", "content": _text_of(message), "tools": tools}
    return {"role": "assistant", "content": _text_of(message)}


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
    model_factory: Callable[[Settings, str | None], ChatLike] | None = None,
    tracer: Tracer | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `sqlite_path` 可注入，便于测试用临时库。省略时回退到 `Settings.sqlite_path`
    （环境变量 `SQLITE_PATH`，默认 `./data/sqlite/app.db`）。

    `model` / `tracer` 同样可注入：测试传 `ScriptedChat` + `NullTracer` 即可全离线跑通
    对话链路（本项目的测试铁律：测内核行为，不测 LLM 本身）。省略 `model` 时用
    `model_factory`（默认 `build_model`）按配置实例化真实后端（Ollama 或任意 OpenAI 兼容
    端点），构造是惰性的，不会在启动时连网。

    `model_factory` 会在两处被调用：启动时构建默认模型；**角色级路由**解析 `role_card.model_name`
    （US-8 后半）以及**设置页保存**后的热重建 —— 测试注入一个每次返回新 `ScriptedChat` 的工厂，
    即可在不接触真实后端的情况下验证路由与热切换确实生效。
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
    model_settings = ModelSettingsService(conn)
    knowledge = KnowledgeBase(settings.chroma_path, make_embedder(settings))

    # 工具注册表：内核工具 + 各域工具（domains/registry 是唯一的装配点）。
    registry = build_registry(
        roles=roles,
        ingestion=ingestion,
        query=health_query,
        knowledge=knowledge,
        enabled_domains=plugins.enabled_domains,  # callable：list_domains 报告实时状态
        current_user=lambda: DEFAULT_USER_ID,
    )

    checkpointer = make_checkpointer(conn)
    resolved_tracer = tracer or make_tracer(settings)
    factory = model_factory or build_model
    # 启动时把 env 后端播种进设置表（幂等，操作员此后在 UI 里改），再计算有效配置。
    model_settings.seed_from_env(settings)
    # 设置页（DB）配置优先于 env：空表 = env 原样；保存过 = DB 覆盖同名后端并接管默认。
    effective = model_settings.effective_settings(settings)
    resolved_model = model or factory(effective, None)
    # 角色级路由的模型缓存：按后端名构建一次（惰性）；设置变更时整体失效重建。
    role_models: dict[str, ChatLike] = {}
    app_state: dict[str, Any] = {
        "graph": None,  # 下面 build 后回填；对话端点每次请求从这里取当前图
        "effective": effective,
        "default_model": resolved_model,
    }

    def resolve_role_model(backend_name: str | None) -> ChatLike:
        """US-8：角色声明了后端名 → 按名解析；未声明 → 默认模型。

        未知后端名（设置页删掉了一个仍被角色引用的后端）→ 降级到默认并留痕，而不是
        让整轮对话 500：权限 fail-closed，可用性 fail-soft。
        """
        if not backend_name:
            return app_state["default_model"]
        cached = role_models.get(backend_name)
        if cached is not None:
            return cached
        try:
            built = factory(app_state["effective"], backend_name)
        except KeyError:
            resolved_tracer.emit(
                TraceEvent(event="role_backend_missing", detail={"backend": backend_name})
            )
            return app_state["default_model"]
        role_models[backend_name] = built
        return built

    graph = build_kernel(
        model=resolved_model,
        registry=registry,
        roles=roles,
        tracer=resolved_tracer,
        settings=effective,
        checkpointer=checkpointer,
        plugins=plugins,
        model_resolver=resolve_role_model,
    )
    # 热替换 holder：设置页保存属于罕见管理动作，重建整图（compile 毫秒级）比把
    # KernelContext 从 build_kernel 里掏出来改签名更简单直接。对话端点每次请求从这里
    # 取当前图，因此保存后无需重启即可生效。
    app_state["graph"] = graph

    def rebuild_graph() -> None:
        eff = model_settings.effective_settings(settings)
        role_models.clear()
        default_model = factory(eff, None)
        app_state["effective"] = eff
        app_state["default_model"] = default_model
        app_state["graph"] = build_kernel(
            model=default_model,
            registry=registry,
            roles=roles,
            tracer=resolved_tracer,
            settings=eff,
            checkpointer=checkpointer,
            plugins=plugins,
            model_resolver=resolve_role_model,
        )

    app = FastAPI(title="rolecard-agent 管理控制台", version="0.3.0")

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

    @app.get("/api/tools/catalog")
    def tools_catalog() -> object:
        """按领域分组的工具目录 —— 角色表单的白名单选择器与插件详情共用。

        工具名对模型有意义，对人是一串"方法名"；每个工具带 docstring 首行作为一句话
        说明，白名单才看得懂。内核工具（domain=None）单独成组。
        """
        kernel: list[dict[str, str]] = []
        domains: dict[str, list[dict[str, str]]] = {}
        for spec in registry.specs():
            entry = {
                "name": spec.name,
                "description": (spec.tool.description or "").split("\n")[0].strip(),
            }
            if spec.domain is None:
                kernel.append(entry)
            else:
                domains.setdefault(spec.domain, []).append(entry)
        return {"kernel": kernel, "domains": domains}

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
    def patch_session(thread_id: str, body: SessionPatch) -> object:
        """会话局部更新：切角色（US-1，不触碰历史）和/或重命名标题。"""
        thread = _get_thread(conn, thread_id)
        if not body.role_id and body.title is None:
            raise HTTPException(status_code=400, detail="没有任何要更新的字段。")
        if body.role_id:
            try:
                roles.set_thread_role(thread_id, body.role_id, actor="operator")
            except RoleError as exc:
                raise _role_error_to_http(exc) from exc  # 角色/线程不存在都是 404
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise HTTPException(status_code=400, detail="标题不能为空。")
            conn.execute(
                "UPDATE session_thread SET title = ?, "
                "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE thread_id = ?",
                (title, thread_id),
            )
            conn.commit()

        final_role_id = body.role_id or str(thread["current_role_id"])
        role = roles.get(final_role_id)
        row = conn.execute(
            "SELECT title FROM session_thread WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return {
            "thread_id": thread_id,
            "role_id": final_role_id,
            "role_name": role.role_name,
            "title": row["title"] if row else None,
        }

    @app.post("/api/chat")
    def chat(body: ChatMessage) -> StreamingResponse:
        """SSE 流式对话。线程必须已存在（POST /api/session 创建）。

        首轮注入完整初始状态（`new_state`）；续轮只注入新消息 + 实时角色 —— 后者让
        PATCH /api/session 的切角色在下一轮立即生效，而 enabled_domains / tool_epoch 不进
        输入，让 checkpoint 里的旧值保留，`call_model` 的 epoch 漂移检测才能每个变化只报
        一次（C14）。图从 `app_state` 现取：设置页保存热重建后，下一次对话自动用新图。
        """
        graph = app_state["graph"]
        thread = _get_thread(conn, body.thread_id)
        role_id = str(thread["current_role_id"])
        user_id = str(thread["user_id"])
        try:
            role = roles.get(role_id)
        except RoleNotFound as exc:
            raise _role_error_to_http(exc) from exc

        # 侧栏标题：首轮消息截断生成；updated_at 每轮刷新，会话列表按它倒序。
        # 用毫秒精度（strftime %f）而非 CURRENT_TIMESTAMP（秒级）：同一秒内创建的两个
        # 会话需要靠"谁最近活跃"严格排序，秒级会打平、只能靠随机 thread_id 兜底。
        conn.execute(
            "UPDATE session_thread SET title = COALESCE(title, ?), "
            "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE thread_id = ?",
            (body.message[:24], body.thread_id),
        )
        conn.commit()

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

    @app.get("/api/sessions")
    def list_sessions() -> list[object]:
        """会话列表（对话页侧栏）。v1 单用户演示：只列演示身份名下的会话。"""
        rows = conn.execute(
            "SELECT s.thread_id, s.title, s.current_role_id AS role_id, r.role_name, "
            "s.updated_at FROM session_thread s "
            "LEFT JOIN role_card r ON r.role_id = s.current_role_id "
            "WHERE s.user_id = ? ORDER BY s.updated_at DESC, s.thread_id",
            (DEFAULT_USER_ID,),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/session/{thread_id}/messages")
    def get_session_messages(thread_id: str) -> list[object]:
        """历史消息回放（来源：checkpoint，而非单独的聊天记录表）——
        点击历史会话续聊时，前端用它恢复消息区。"""
        _get_thread(conn, thread_id)
        snapshot = app_state["graph"].get_state({"configurable": {"thread_id": thread_id}})
        return [_serialize_message(m) for m in (snapshot.values or {}).get("messages", [])]

    @app.delete("/api/session/{thread_id}", status_code=204)
    def delete_session(thread_id: str) -> None:
        """删除会话：thread 行 + 该线程的 checkpoint / writes 一并清掉，不留孤儿。"""
        _get_thread(conn, thread_id)
        conn.execute("DELETE FROM session_thread WHERE thread_id = ?", (thread_id,))
        for table in ("checkpoints", "writes"):  # langgraph SqliteSaver 的两张表
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
        conn.commit()

    # 上传大小上限：请求体整个读进内存算哈希，20MB 是演示负载的合理护栏。
    UPLOAD_MAX_BYTES = 20 * 1024 * 1024

    @app.post("/api/session/{thread_id}/upload", status_code=201)
    async def upload_report(thread_id: str, file: UploadFile) -> object:
        """US-7 上传入口的真实落点：存文件 + 登记 intake 任务（幂等键 sha256）。

        解析（OCR / 结构化）仍是 v2.2 的活 —— 这里只做**登记**。登记后向会话注入一条
        说明消息（graph.update_state），让模型在后续对话里知道"有文件已登记但还不能读"，
        而不是假装读过。重复上传同一文件复用同一任务（ingestion_task 幂等键）。
        """
        thread = _get_thread(conn, thread_id)
        user_id = str(thread["user_id"])
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="空文件。")
        if len(data) > UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=400, detail="文件超过 20MB 上限。")

        upload_dir = settings.upload_dir
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file.filename or "report.bin").name  # 去掉任何路径成分
        target = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        target.write_bytes(data)
        file_hash = hashlib.sha256(data).hexdigest()

        before = len(ingestion.list_for_user(user_id))
        task_id = ingestion.create(user_id=user_id, source_file=str(target), file_hash=file_hash)
        reused = len(ingestion.list_for_user(user_id)) == before
        existing = ingestion.get(task_id)

        # v2.1：原生文本文件（.txt/.md）直接解析入检索索引；其余类型等待 v2.2（OCR/解析器）。
        if target.suffix.lower() in {".txt", ".md"}:
            text = target.read_text(encoding="utf-8", errors="ignore")
            chunks = knowledge.index("health_reports", safe_name, text)
            for next_status in ("parsed", "extracted", "indexed"):
                ingestion.advance(task_id, next_status)
            existing = ingestion.get(task_id)
            note = (
                f"[用户上传了文档：{safe_name}（{chunks} 段），已建立检索索引"
                f"（任务 {task_id}，status=indexed）。后续提问可以检索这份文档的内容。]"
            )
        else:
            note = (
                f"[用户上传了报告文件：{safe_name}，已登记 intake 任务 {task_id}"
                f"（status={existing['status']}）。文件解析在 v2.2 接入，当前不能读取其中"
                "内容，不要假装已经读过。]"
            )
        graph_config = {"configurable": {"thread_id": thread_id}}
        app_state["graph"].update_state(graph_config, {"messages": [HumanMessage(content=note)]})
        return {
            "task_id": task_id,
            "reused": reused,
            "file": safe_name,
            "status": existing["status"],
        }

    @app.get("/api/records")
    def list_records() -> list[object]:
        """F2 数据管理视图：报告 + 完整指标行（归属演示用户）。"""
        return health_query.list_records(DEFAULT_USER_ID)

    @app.patch("/api/records/index/{index_id}")
    def patch_record_index(index_id: str, body: IndexPatch) -> object:
        """F2：修正误录的指标值。变更写审计（US-3 的数据侧延伸）。"""
        changes = body.model_dump(exclude_unset=True)
        try:
            row = health_query.update_index(
                user_id=DEFAULT_USER_ID, index_id=index_id, changes=changes
            )
        except HealthNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except HealthDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        roles.audit(
            actor="operator",
            action="update_index",
            target=index_id,
            detail={"fields": sorted(changes)},
        )
        return row

    @app.delete("/api/records/index/{index_id}", status_code=204)
    def remove_record_index(index_id: str) -> None:
        try:
            health_query.delete_index(user_id=DEFAULT_USER_ID, index_id=index_id)
        except HealthNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        roles.audit(actor="operator", action="delete_index", target=index_id)

    @app.delete("/api/records/report/{report_id}", status_code=204)
    def remove_record_report(report_id: str) -> None:
        try:
            health_query.delete_report(user_id=DEFAULT_USER_ID, report_id=report_id)
        except HealthNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        roles.audit(actor="operator", action="delete_report", target=report_id)

    @app.get("/api/audit")
    def list_audit(limit: int = 100) -> list[object]:
        """F3：审计只读端点 —— 让一直在写入的 audit_log 可被运营方查看。"""
        capped = max(1, min(limit, 500))
        rows = conn.execute(
            "SELECT ts, actor, action, target, detail_json FROM audit_log ORDER BY id DESC LIMIT ?",
            (capped,),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/settings/models")
    def get_model_settings() -> object:
        """模型后端设置。api_key 永不回读 —— 只有 has_key 标志。"""
        return {
            "default": model_settings.default_backend(),
            "backends": model_settings.list_backends(),
            "fallbacks": model_settings.list_fallbacks() or [],
        }

    @app.put("/api/settings/models")
    def put_model_settings(body: ModelSettingsBody) -> object:
        """保存后端集合并热重建（下一轮对话即用新后端，无需重启进程）。

        api_key 语义：缺省/None = 保留已存 key；空串 = 清除 —— 否则每次没重输 key 的
        保存都会把 key 抹掉。fallbacks = 失败自动回退链（≤2 级，按序尝试）。"""
        try:
            model_settings.save(
                default=body.default,
                backends=[b.model_dump() for b in body.backends],
                fallbacks=body.fallbacks,
            )
        except ModelSettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rebuild_graph()
        return {
            "default": model_settings.default_backend(),
            "backends": model_settings.list_backends(),
            "fallbacks": model_settings.list_fallbacks() or [],
        }

    dist_dir = Path(os.environ.get("FRONTEND_DIST") or _DEFAULT_DIST)
    if (dist_dir / "index.html").exists():
        # 静态托管必须挂在 API 路由之后注册：FastAPI 按注册顺序匹配，先注册的 /api/* 优先。
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="console")
    else:

        @app.get("/", response_class=HTMLResponse)
        @app.get("/console", response_class=HTMLResponse)
        def console() -> str:
            return _FALLBACK_HTML

    return app
