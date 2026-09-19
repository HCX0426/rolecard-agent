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

import contextlib
import os
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from rolecard_agent.api.auth import (
    auth_required,
    client_ip,
    parse_trusted_proxies,
    resolve_actor,
    unauthorized_response,
)
from rolecard_agent.api.deps import (
    AppContext,
)
from rolecard_agent.api.routers import approvals as approvals_router
from rolecard_agent.api.routers import console as console_router
from rolecard_agent.api.routers import domains as domains_router
from rolecard_agent.api.routers import mcp as mcp_router
from rolecard_agent.api.routers import reachouts as reachouts_router
from rolecard_agent.api.routers import records as records_router
from rolecard_agent.api.routers import roles as roles_router
from rolecard_agent.api.routers import services as services_router
from rolecard_agent.api.routers import sessions as sessions_router
from rolecard_agent.api.routers import settings as settings_router
from rolecard_agent.api.routers import workspace as workspace_router
from rolecard_agent.config import Settings
from rolecard_agent.core import mcp_store, runtime_settings
from rolecard_agent.core.approvals import ApprovalService
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.graph import build_kernel, build_model
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.memory import load_memory_text
from rolecard_agent.core.model_settings import ModelSettingsService, client_style
from rolecard_agent.core.nodes import ChatLike
from rolecard_agent.core.observability import TraceEvent, Tracer, make_tracer
from rolecard_agent.core.plugins import PluginService, seed_plugin_rows
from rolecard_agent.core.probes import ollama_keep
from rolecard_agent.core.reachout import ReachoutScheduler
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
from rolecard_agent.storage.db import (
    SqlConnection,
    bootstrap,
    connect_threadlocal,
    set_request_epoch,
)

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


def _seed_demo_identity(conn: SqlConnection) -> None:
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
    # 温度参与缓存键，工厂可能被传第三个参数 —— 签名放宽为可变参数（见 P1-2）。
    model_factory: Callable[..., ChatLike] | None = None,
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
    # 公网暴露护栏（P0-3）：非回环地址绑定 + 无鉴权 = 任何人可改配置 / 自批命令 / 浏览全盘。
    # run_api.py 默认绑 127.0.0.1（本地安全）；Docker 绑 0.0.0.0 时若没开 AUTH_MODE，直接拒绝
    # 启动 —— 把"忘记配鉴权就公网裸奔"变成起不来的硬失败，而不是默默暴露。
    bind_host = os.environ.get("RUN_API_HOST", "127.0.0.1")
    if (
        bind_host not in ("127.0.0.1", "localhost", "::1")
        and not bind_host.startswith("127.")
        and settings.auth_mode == "off"
    ):
        raise RuntimeError(
            f"拒绝启动：绑定地址 {bind_host!r} 非回环，但 AUTH_MODE=off（无鉴权）。"
            "公网部署请设 AUTH_MODE=on，或改绑 127.0.0.1 经反向代理转发。"
        )
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
    roles.seed_domain_roles()
    plugins = PluginService(conn, known_plugins=DOMAINS)
    ingestion = IngestionService(conn)
    health_query = HealthQueryService(conn)
    model_settings = ModelSettingsService(conn)
    # 服务端点引用（OCR / 嵌入 / 重排引用哪些后端）—— 启动时一次性播种默认行（幂等）。
    services = ServiceEndpointService(conn)
    services.seed_once()
    # 启动时把 env 后端播种进设置表（幂等，操作员此后在 UI 里改），再计算有效配置。
    model_settings.seed_from_env(settings)
    # 归一化历史行的 provider（旧种子把 SiliconFlow 记成 "openai" 等风格值）→ 厂商 id。
    model_settings.normalize_providers()
    # 设置页（DB）配置优先于 env：空表 = env 原样；保存过 = DB 覆盖同名后端并接管默认。
    # 运行环境覆盖（「运行环境」页签保存的项，kernel_meta runtime:*）在此一并叠加。
    # 必须先于 KnowledgeBase / registry 构建：embedder、联网与 consensus 工具闭包
    # 都要拿到**叠加覆盖后**的配置，而不是裸 env 快照。
    effective = runtime_settings.apply_overrides(
        model_settings.effective_settings(settings),
        runtime_settings.load_overrides(conn),
    )
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
        settings.rag_min_similarity,
    )

    # 轨迹器必须先于工具注册表构建：`search_knowledge` 闭包要持有它，
    # 否则 rag_search / rerank_fallback 两个事件永远不会被 emit（审查报告 M3）。
    resolved_tracer = tracer or make_tracer(settings)

    # 工具注册表：内核工具 + 各域工具（domains/registry 是唯一的装配点）。
    # MCP 生效集 = env MCP_SERVERS ∪ mcp_server 表（同 id 表覆盖 env、表内禁用行抑制 env 同名）。
    # 只把合并结果喂给"构建工具用的 settings"，env 真值仍留在 effective/app_state 不被污染。
    mcp_eff = mcp_store.effective_servers(conn, effective.mcp_servers)
    effective_for_tools = effective.model_copy(update={"mcp_servers": mcp_eff})
    registry = build_registry(
        roles=roles,
        ingestion=ingestion,
        query=health_query,
        knowledge=knowledge,
        # MCP 域按生效集启用：有生效 server 才把 "mcp" 加进启用域（架构计划 C·§6.1）；
        # 未配置则与普通插件一致，不暴露 mcp 工具。callable 形式保证运行时实时判定。
        enabled_domains=lambda: [
            *plugins.enabled_domains(),
            *(["mcp"] if mcp_eff else []),
        ],
        current_user=lambda: DEFAULT_USER_ID,
        # 域写工具（upload_medical_report）必须知道上传目录：它的 file_path 来自模型，
        # 不受限就等于"任意主机文件读取 + 任意目录写"（审查报告 H1）。
        upload_dir=settings.upload_dir,
        # 联网与工作区工具的后端配置（搜索后端 / TAVILY_API_KEY / WORKSPACE_DIR）。
        # 传**叠加了运行环境覆盖 + MCP 合并**的有效配置（而非裸 env 快照）。
        settings=effective_for_tools,
        tracer=resolved_tracer,
        memory_conn=conn,
        fs_conn=conn,
    )

    checkpointer = make_checkpointer(conn)
    factory = model_factory or build_model
    resolved_model = model or factory(effective, None)
    # 角色级路由的模型缓存：按后端名构建一次（惰性）；设置变更时整体失效重建。
    role_models: dict[tuple[str | None, float | None], ChatLike] = {}
    app_state: dict[str, Any] = {
        "graph": None,  # 下面 build 后回填；对话端点每次请求从这里取当前图
        "effective": effective,
        "default_model": resolved_model,
    }

    # H2：重建互斥锁 —— 构建在锁外、换装在锁内（见 rebuild_runtime docstring）。
    _rebuild_lock = threading.Lock()

    def resolve_role_model(backend_name: str | None, temperature: float | None = None) -> ChatLike:
        """US-8：角色声明了后端名 → 按名解析；未声明 → 默认模型。

        `temperature` 参与缓存键：同一后端在不同温度下是**不同的模型实例**
        （采样参数只能在构造期设置，见 `core.graph._init_model`）。
        未知后端名（设置页删掉了一个仍被角色引用的后端）→ 降级到默认并留痕，而不是
        让整轮对话 500：权限 fail-closed，可用性 fail-soft。
        """
        # 测试直接注入模型实例（ScriptedChat）：温度变体对假模型没有意义，而走下面的
        # 工厂会拿真配置去连真实后端 —— 所以注入了 `model` 时原样返回它。
        if model is not None:
            return model
        # 没声明后端但声明了温度 → 仍要走工厂（默认后端 + 该温度的新实例）：
        # 温度必须生效，不管角色有没有指定后端（P1-2 的第一版修复就漏了这条分支）。
        if not backend_name and temperature is None:
            return app_state["default_model"]
        cache_key = (backend_name, temperature)
        cached = role_models.get(cache_key)
        if cached is not None:
            return cached
        try:
            built = factory(app_state["effective"], backend_name, temperature)
        except KeyError:
            resolved_tracer.emit(
                TraceEvent(event="role_backend_missing", detail={"backend": backend_name})
            )
            return app_state["default_model"]
        role_models[cache_key] = built
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
        # 跨会话记忆的读取器：每次调用实时读库；总开关在 call_model 里按当前有效配置把关。
        memory_provider=lambda: load_memory_text(conn),
    )
    # 热替换 holder：设置页保存属于罕见管理动作，重建整图（compile 毫秒级）比把
    # KernelContext 从 build_kernel 里掏出来改签名更简单直接。对话端点每次请求从这里
    # 取当前图，因此保存后无需重启即可生效。
    app_state["graph"] = graph

    # 角色主动开口的调度器（架构计划 B）：后台 daemon 按固定 tick 检查"有资格主动"的
    # 角色，符合抑制条件就生成并落收件箱。settings_provider 每次 tick 现取 app_state
    # 里的**有效配置**（全局总闸 REACHOUT_ENABLED 热切即时生效）；模型解析复用对话路径
    # 的 resolve_role_model（角色可按 model_name 路由）。lifespan 启停。
    reachout = ReachoutScheduler(
        settings_provider=lambda: app_state["effective"],
        roles=roles,
        model_resolver=resolve_role_model,
        conn=conn,
        tracer=resolved_tracer,
    )

    def _auto_pin_default_model() -> None:
        """启动即预热默认模型：仅本地 Ollama(native) 有意义，按后端配置的 num_ctx
        以 keep_alive=-1 常驻。best-effort 后台线程——Ollama 没起 / 默认是云端 / 未配默认
        后端 / 任何异常都静默跳过，绝不阻断启动或抛到请求路径（用户 2026-09-19）。"""
        with contextlib.suppress(Exception):
            eff = app_state.get("effective")
            if eff is None:
                return
            backend = eff.backend(None)  # 未配默认 → KeyError，被 suppress 吞掉
            if client_style(backend.provider) == "native":
                ollama_keep(backend.base_url, backend.model, -1, num_ctx=backend.num_ctx)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """进程退出时收尾：sqlite 连接 / 知识库 httpx 客户端 / 对话线程池 / 主动开口调度。

        之前这三者**从不释放**：uvicorn 被 Ctrl+C 或容器停止时，各线程创建的 sqlite
        连接、嵌入与重排器的 httpx 连接池都随进程一起消失 —— 在长驻进程里（设置页热
        重建 knowledge 会换掉实例）表现为 fd 与连接泄漏（审查报告 P2：无 lifespan）。

        注意**这里只关"本 app 持有的"资源**：`_CHAT_POOL` 是模块级（进程级）对象，
        在一个 app 的 lifespan 里关掉它会让同进程里后续创建的 app 全部拿不到线程池
        （测试就是这么互相干扰的）—— 它的收尾放在真实的进程退出路径
        （scripts/run_api.py 里 uvicorn.run 返回之后）。
        """
        try:
            reachout.start()  # 后台调度：角色主动开口从这里开始转
            # 启动即预热：后台线程把默认本地模型按配置的 num_ctx 常驻（keep_alive=-1），
            # 免首条消息冷加载、也修复空闲后 Ollama 默认 5min 卸载把 pin 打回 4096。
            # MODEL_PIN_ON_STARTUP=0 时跳过：这一句是真 POST /api/generate，会把大模型钉进
            # 显存 —— 测试套件（23 处 with TestClient）与不跑推理的部署都该关掉它。
            if settings.model_pin_on_startup:
                threading.Thread(target=_auto_pin_default_model, daemon=True).start()
            yield
        finally:
            # 每个 suppress 都独立：某一处收尾失败不能连累其它资源。
            with contextlib.suppress(Exception):
                conn.close()
            for holder in (knowledge, app_state.get("knowledge")):
                with contextlib.suppress(Exception):
                    if holder is not None and hasattr(holder, "close"):
                        holder.close()
            # 主动开口 DAEMON 线程退出（stop 只置位；daemon 线程随进程消亡兜底）。
            reachout.stop()

    app = FastAPI(title="rolecard-agent 管理控制台", version="0.3.0", lifespan=_lifespan)

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
        approvals=ApprovalService(conn),
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
    app.include_router(workspace_router.router)
    app.include_router(reachouts_router.router)
    app.include_router(approvals_router.router)
    app.include_router(mcp_router.router)

    def rebuild_runtime() -> None:
        """按当前设置与服务端点引用重建全部运行时对象：模型、KnowledgeBase、registry、图。

        由两条路径触发：模型设置保存（settings router）与服务端点变更（services router）。
        嵌入器/重排器是 KnowledgeBase 构造时注入的实例，引用变了必须连 KB 一起重造；
        search_knowledge 工具闭包持有 KB，所以 registry 也要跟着重建 —— 顺序即依赖序。

        并发安全（H2）：**构建在锁外、换装在锁内**。两个并发重建各自完整构建（后写者
        胜出，浪费但正确），而 ctx 三引用 + role_models 的换装是单个临界区内的原子序列
        —— 杜绝"新图配旧 KB"的中间态被 SSE 请求看到。
        """
        eff = runtime_settings.apply_overrides(
            model_settings.effective_settings(settings),
            runtime_settings.load_overrides(conn),
        )
        # M1：捕获旧实例，换装后关闭，释放 httpx 连接/文件句柄（旧 embedder/reranker 持有
        # httpx.Client 此前从不关闭，累积 fd/连接泄漏）。
        old_knowledge = ctx.knowledge
        role_models.clear()
        default_model = factory(eff, None)

        # 服务端点（OCR / 嵌入 / 重排）从 DB 读引用行；云端行按被引用后端的配置实例化。
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
        knowledge_new = KnowledgeBase(
            settings.chroma_path, embedder, reranker, settings.rag_min_similarity
        )

        # MCP 生效集重建时重新解析（表行可能在两次重建之间被 API 改动）。
        mcp_eff = mcp_store.effective_servers(conn, eff.mcp_servers)
        eff_for_tools = eff.model_copy(update={"mcp_servers": mcp_eff})
        registry_new = build_registry(
            roles=roles,
            ingestion=ingestion,
            query=health_query,
            knowledge=knowledge_new,
            # 与初始装配点同构：有生效 MCP server 才把 "mcp" 加进启用域（此前 rebuild 漏加，
            # 导致改一次运行环境后 mcp 工具虽已加载却被启用域挡掉）。
            enabled_domains=lambda: [
                *plugins.enabled_domains(),
                *(["mcp"] if mcp_eff else []),
            ],
            current_user=lambda: DEFAULT_USER_ID,
            upload_dir=settings.upload_dir,
            # 传**叠加了运行环境覆盖 + MCP 合并**的有效配置：联网/consensus 工具闭包持有它，
            # 「运行环境」页签保存后经 rebuild 在此热生效（此前漏传 → 工具用 env 裸值）。
            settings=eff_for_tools,
            tracer=resolved_tracer,
            memory_conn=conn,
            fs_conn=conn,
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
            memory_provider=lambda: load_memory_text(conn),
        )
        with _rebuild_lock:
            # 一次性换装：KB / registry 换新实例（工具经 registry 间接引用新 KB），图也换新。
            # ctx.settings 同步换新：OCR / 抽取 / 比对在请求时读 ctx.settings（AppContext
            # 是可变 dataclass）—— 不换的话「运行环境」页签对它们不热生效。
            role_models.clear()
            ctx.settings = eff
            ctx.knowledge = knowledge_new
            ctx.registry = registry_new
            app_state["effective"] = eff
            app_state["default_model"] = default_model
            app_state["graph"] = graph_new
        # M1：旧 KB 换装完成后关闭（旧 embedder/reranker 的 httpx 连接在此释放）。
        if old_knowledge is not None and old_knowledge is not ctx.knowledge:
            with contextlib.suppress(Exception):
                old_knowledge.close()

    # 绑定真实现（ctx 构造时是占位 lambda，避免定义顺序上的循环依赖）。
    ctx.rebuild_runtime = rebuild_runtime

    @app.middleware("http")
    async def _begin_db_request(request: object, call_next: object) -> object:
        """每个请求发一个**库代际**；残留事务的清理在持连接的线程里做（P1-10）。

        早期版本在这里 `conn.rollback_current()` —— 但中间件跑在事件循环线程，而同步
        端点与图执行各在别的线程持连接，那个 rollback 清的是另一条线程的连接，等于没清。
        现在只在这里发号，`ThreadLocalConnection._current()` 发现代际变了才清理。
        """
        set_request_epoch(uuid.uuid4().hex)
        return await call_next(request)  # type: ignore[operator]

    # 认证做成**中间件**而不是路由依赖：控制台页面是 StaticFiles mount 的 ASGI 应用，
    # 不经过路由的依赖系统 —— 只用依赖会出现"API 被保护、页面谁都能开"。
    exempt_paths = [p.strip() for p in (settings.auth_exempt_paths or "").split(",") if p.strip()]
    trusted_proxies = parse_trusted_proxies(settings.auth_trusted_proxies)

    @app.middleware("http")
    async def _authenticate(request: object, call_next: object) -> object:
        req = cast("Request", request)
        actor = resolve_actor(
            authorization=req.headers.get("authorization"),
            api_key=req.headers.get("x-api-key"),
            settings=settings,
        )
        # 来源 IP 只认 **TCP 对端**；X-Forwarded-For 仅在直连方命中 AUTH_TRUSTED_PROXIES
        # 时才采信（见 auth.client_ip：否则 `auto` 档可被一行请求头绕过）。
        peer = req.client.host if req.client else ""
        if actor.is_anonymous and auth_required(
            mode=settings.auth_mode,
            ip=client_ip(req.headers, peer=peer, trusted=trusted_proxies),
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
