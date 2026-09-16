"""FastAPI 接入层 —— **app 装配层**（C1 拆分后，端点全部在 `api/routers/`）。

本文件只做五件事：
  1. bootstrap + 种子（插件行 / 演示身份 / 内置角色 / env 后端播种）；
  2. 构建内核图（`build_kernel`）与模型热重建入口（`rebuild_graph`）；
  3. 组装 `AppContext`（连接、服务、registry、tracer、图句柄）挂到 `app.state.ctx`，
     各 router 通过 `Depends(get_context)` 注入 —— 不再有共享闭包；
  4. 两个中间件：请求开始清理本线程残留事务；认证（`api/auth.py`，Basic / X-API-Key，
     三档 AUTH_MODE，覆盖含静态资源在内的全部请求）；
  5. 静态托管控制台（`frontend/dist`；未构建时回退提示页）。

端点按职责分包在 `api/routers/`（24 个）：
    roles.py     角色卡 CRUD / 工具目录 / 插件列表与启停
    sessions.py  会话 CRUD / SSE 对话（POST /api/chat）/ 历史回放 / 上传（含解析入索引）
    records.py   手动补录 / 结构化抽取（POST /api/records/extract）/ 指标修正与删除
    console.py   审计查询 / 知识库概览与重建 / 检索延迟指标 / 探活（GET /api/health）
    settings.py  模型后端设置（api_key 只写不回读）与保存后热重建

流式事件框架与增量输出审核在 `api/chat.py`。对话内核与 `call_model` / `bind_tools`
解耦：router 只通过服务层改库，下一轮 `call_model` **实时**读到新的 `enabled_domains`
与角色，因此插件启停 / 切角色 / 换模型无需重启即可生效（07 C14 / US-1 / US-8）。

身份：v1 单用户演示，所有会话归属 `DEFAULT_USER_ID`（schema 的 user_id 列已就位，
接真实登录只是数据替换）。连接：`ThreadLocalConnection` 对外表现为一条连接，内部按
线程分发真实连接（见 storage/db.py）—— 并发安全的连接共享由它负责，本层不感知。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import (
    auth_required,
    client_ip,
    resolve_actor,
    unauthorized_response,
)
from rolecard_agent.api.deps import (
    AppContext,
)
from rolecard_agent.api.routers import console as console_router
from rolecard_agent.api.routers import domains as domains_router
from rolecard_agent.api.routers import records as records_router
from rolecard_agent.api.routers import roles as roles_router
from rolecard_agent.api.routers import services as services_router
from rolecard_agent.api.routers import sessions as sessions_router
from rolecard_agent.api.routers import settings as settings_router
from rolecard_agent.config import Settings
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.graph import build_kernel, build_model
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.nodes import ChatLike
from rolecard_agent.core.observability import TraceEvent, Tracer, make_tracer
from rolecard_agent.core.plugins import PluginService, seed_plugin_rows
from rolecard_agent.core.services import ServiceEndpointService
from rolecard_agent.domains.health.service import (
    HealthQueryService,
)
from rolecard_agent.domains.registry import DOMAINS, build_registry
from rolecard_agent.rag.retriever import (
    KnowledgeBase,
    make_embedder,
    make_reranker,
)
from rolecard_agent.roles.service import (
    RoleCardService,
)
from rolecard_agent.storage.db import bootstrap, connect_threadlocal

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
# Ollama 本地端点不需要凭据；其它 provider（openai 兼容）必须有 key 才能构建客户端。
_KEYLESS_PROVIDER = "ollama"


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
    # 注意是 `connect_threadlocal` 而不是 `connect`：本进程的多线程（FastAPI 同步端点 +
    # 图执行）会并发使用这个对象，而 sqlite3 的连接不是线程安全的。它对外仍表现为"一条
    # 连接"，内部按线程分发（见 storage/db.py 的 ThreadLocalConnection）。
    conn = connect_threadlocal(db_path)
    # 每个 REGISTERED 域的 schema 都建好，这样表永远存在，重新启用插件无需 DDL。
    bootstrap(conn, enabled_domains=DOMAINS)
    seed_plugin_rows(conn, DOMAINS)
    _seed_demo_identity(conn)
    roles = RoleCardService(conn)
    roles.seed_builtins()
    plugins = PluginService(conn, known_plugins=DOMAINS)
    ingestion = IngestionService(conn)
    health_query = HealthQueryService(conn)
    model_settings = ModelSettingsService(conn)
    # 服务端点实例（OCR / 嵌入 / 重排的候选行）—— 启动时一次性播种默认行（幂等）。
    services = ServiceEndpointService(conn)
    services.seed_once(settings)
    knowledge = KnowledgeBase(
        settings.chroma_path,
        make_embedder(
            settings,
            order=[e.id for e in services.ordered_candidates("embedding")],
            endpoints=services.endpoint_map("embedding"),
        ),
        make_reranker(
            settings,
            order=[e.id for e in services.ordered_candidates("rerank")],
            endpoints=services.endpoint_map("rerank"),
        ),
    )

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
    # 归一化历史行的 provider（旧种子把 SiliconFlow 记成 "openai" 等风格值）→ 厂商 id。
    model_settings.normalize_providers()
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

    app = FastAPI(title="rolecard-agent 管理控制台", version="0.3.0")

    # 共享上下文：所有 router 通过 `Depends(get_context)` 取它，不再依赖闭包。
    # rebuild_runtime 先挂占位，定义完成后立刻绑定真实现（见下方）。
    ctx = AppContext(
        settings=settings,
        conn=conn,
        roles=roles,
        plugins=plugins,
        ingestion=ingestion,
        health=health_query,
        model_settings=model_settings,
        services=services,
        knowledge=knowledge,
        registry=registry,
        tracer=resolved_tracer,
        app_state=app_state,
        rebuild_runtime=lambda: None,
    )
    app.state.ctx = ctx
    # C1：端点按职责分包，全部端点已迁出 main.py。本文件只保留 app 装配：
    # 启动 bootstrap/seed、内核图构建、共享上下文、中间件、静态托管。
    app.include_router(roles_router.router)
    app.include_router(sessions_router.router)
    app.include_router(records_router.router)
    app.include_router(console_router.router)
    app.include_router(settings_router.router)
    app.include_router(services_router.router)
    app.include_router(domains_router.router)

    def rebuild_runtime() -> None:
        """按当前设置与服务策略重建全部运行时对象：模型、KnowledgeBase、registry、图。

        由两条路径触发：模型设置保存（settings router）与服务策略保存（services router）。
        嵌入器/重排器是 KnowledgeBase 构造时注入的实例，策略变了必须连 KB 一起重造；
        search_knowledge 工具闭包持有 KB，所以 registry 也要跟着重建 —— 顺序即依赖序。
        """
        eff = model_settings.effective_settings(settings)
        role_models.clear()
        default_model = factory(eff, None)

        # 服务端点（OCR / 嵌入 / 重排）从 DB 读操作员顺序；云端行按行内配置实例化。
        embedder = make_embedder(
            eff,
            order=[e.id for e in services.ordered_candidates("embedding")],
            endpoints=services.endpoint_map("embedding"),
        )
        reranker = make_reranker(
            eff,
            order=[e.id for e in services.ordered_candidates("rerank")],
            endpoints=services.endpoint_map("rerank"),
        )
        knowledge_new = KnowledgeBase(settings.chroma_path, embedder, reranker)

        registry_new = build_registry(
            roles=roles,
            ingestion=ingestion,
            query=health_query,
            knowledge=knowledge_new,
            enabled_domains=plugins.enabled_domains,
            current_user=lambda: DEFAULT_USER_ID,
        )
        graph_new = build_kernel(
            model=default_model,
            registry=registry_new,
            roles=roles,
            tracer=resolved_tracer,
            settings=eff,
            checkpointer=checkpointer,
            plugins=plugins,
            model_resolver=resolve_role_model,
        )
        # 一次性换装：KB / registry 换新实例（工具经 registry 间接引用新 KB），图也换新。
        ctx.knowledge = knowledge_new
        ctx.registry = registry_new
        app_state["effective"] = eff
        app_state["default_model"] = default_model
        app_state["graph"] = graph_new

    # 绑定真实现（ctx 构造时是占位 lambda，避免定义顺序上的循环依赖）。
    ctx.rebuild_runtime = rebuild_runtime

    @app.middleware("http")
    async def _drop_stale_transaction(request: object, call_next: object) -> object:
        """每个请求开始时清掉本线程可能残留的未提交事务。

        线程池的线程会被下一个请求复用；若上一个请求在事务中途异常退出，残留的
        BEGIN/未提交改动会被下一个请求继承（`ThreadLocalConnection` 按线程复用连接）。
        这里 rollback 一次，把"请求边界"和"事务边界"重新对齐。
        """
        conn.rollback_current()
        return await call_next(request)  # type: ignore[operator]

    # 认证做成**中间件**而不是路由依赖：控制台页面是 StaticFiles mount 的 ASGI 应用，
    # 不经过路由的依赖系统 —— 只用依赖会出现"API 被保护、页面谁都能开"。
    exempt_paths = [p.strip() for p in (settings.auth_exempt_paths or "").split(",") if p.strip()]

    @app.middleware("http")
    async def _authenticate(request: object, call_next: object) -> object:
        req = cast("Request", request)
        actor = resolve_actor(
            authorization=req.headers.get("authorization"),
            api_key=req.headers.get("x-api-key"),
            settings=settings,
        )
        if actor.is_anonymous and auth_required(
            mode=settings.auth_mode,
            ip=client_ip(req.headers) or (req.client.host if req.client else ""),
            path=req.url.path,
            exempt=exempt_paths,
        ):
            status, headers, body = unauthorized_response()
            return PlainTextResponse(body, status_code=status, headers=headers)
        req.state.actor = actor
        return await call_next(request)  # type: ignore[operator]

    @app.get("/api/health")
    def health() -> object:
        """探活：容器 healthcheck 与反代探活用，**必须免鉴权**（默认在 `AUTH_EXEMPT_PATHS` 里）。

        只回状态与当前认证档位 —— 便于部署后确认"认证到底开没开"，不含任何凭证信息。
        """
        return {"status": "ok", "version": "0.3.0", "auth_mode": settings.auth_mode}

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
