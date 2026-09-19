"""FastAPI 接入层 —— **HTTP 壳**：把装配好的内核绑成 Web 应用。

装配内核（建库 / 播种 / 服务实例化 / 建图 / 热重建）在 `core/bootstrap.py`；本文件只做
HTTP 这一层该做的五件事：

  1. 绑定地址护栏（非回环 + 无鉴权 = 拒绝启动）；
  2. 给出宿主侧的域接线（哪些域存在、域查询服务与工具注册表怎么拼），然后装配内核；
  3. 把 `AppContext`（内核的读穿视图）挂到 `app.state.ctx`，各 router 通过
     `Depends(get_context)` 注入 —— 不再有共享闭包，也不再另存一份可变引用；
  4. 两个中间件：请求开始发一个库代际；认证（`api/auth.py`，Basic / X-API-Key，三档
     AUTH_MODE，覆盖含静态资源在内的全部请求）；
  5. 静态托管控制台（`frontend/dist`；未构建时回退提示页）+ 进程生命周期（启停后台调度与
     预热、退出时释放）。

端点按职责分包在 `api/routers/`（roles / sessions / records / console / settings /
services / domains / workspace / reachouts / approvals / mcp），流式事件框架与增量输出审核
在 `api/chat.py`。对话内核与 `call_model` / `bind_tools` 解耦：router 只通过服务层改库，
下一轮 `call_model` **实时**读到新的 `enabled_domains` 与角色，因此插件启停 / 切角色 /
换模型无需重启即可生效（07 C14 / US-1 / US-8）。

为什么装配根不在这个文件里（架构审计报告 §0 / §7）：C/S 桌宠壳、评测脚本与冒烟要的是
**同一个内核**，而它们都不该 import FastAPI。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

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
from rolecard_agent.api.deps import AppContext
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
from rolecard_agent.core.bootstrap import Assembly, build_runtime
from rolecard_agent.core.identity import DEFAULT_USER_ID
from rolecard_agent.core.nodes import ChatLike
from rolecard_agent.core.observability import Tracer
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.domains.health.service import HealthQueryService
from rolecard_agent.domains.registry import DOMAINS, build_registry
from rolecard_agent.rag.retriever import KnowledgeBase
from rolecard_agent.storage.db import set_request_epoch

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


def _host_registry_factory(
    assembly: Assembly,
    settings: Settings,
    knowledge: KnowledgeBase,
    enabled_domains: Callable[[], list[str]],
) -> ToolRegistry:
    """宿主侧的域接线：内核装配不认识任何具体域，工具怎么拼在这里给出。

    `upload_dir` 是域**写**工具的路径边界：这些工具的 file_path 来自模型（因而也来自上传
    文档里的提示注入），不设边界就等于"读任意主机文件 + 在任意目录写"（审查报告 H1）。
    `current_user` 每次调用现取，模型永远不能指定"我是谁"。
    """
    query = assembly.query
    if not isinstance(query, HealthQueryService):
        # v1 只接一个域，装配注入的就是它。将来多域时这里要换成"按域取各自的查询服务"，
        # 而不是悄悄把不匹配的服务喂给 health 的工具工厂。
        raise RuntimeError(
            f"域接线只认识 {HealthQueryService.__name__}，注入的是 {type(query).__name__}"
        )
    return build_registry(
        roles=assembly.roles,
        ingestion=assembly.ingestion,
        query=query,
        knowledge=knowledge,
        enabled_domains=enabled_domains,
        current_user=lambda: DEFAULT_USER_ID,
        upload_dir=settings.upload_dir,
        # 联网与工作区工具的后端配置（搜索后端 / TAVILY_API_KEY / WORKSPACE_DIR）：传
        # **叠加了运行环境覆盖 + MCP 合并**的有效配置，工具闭包持有它，保存后经热重建生效。
        settings=settings,
        tracer=assembly.tracer,
        memory_conn=assembly.conn,
        fs_conn=assembly.conn,
    )


def create_app(
    sqlite_path: Path | None = None,
    *,
    model: ChatLike | None = None,
    # 温度参与缓存键，工厂可能被传第三个参数 —— 签名放宽为可变参数（见 P1-2）。
    model_factory: Callable[..., ChatLike] | None = None,
    tracer: Tracer | None = None,
) -> FastAPI:
    """Build the FastAPI app on top of a `core.bootstrap` runtime.

    `sqlite_path` 可注入，便于测试用临时库。省略时回退到 `Settings.sqlite_path`
    （环境变量 `SQLITE_PATH`，默认 `./data/sqlite/app.db`）。

    `model` / `tracer` / `model_factory` 透传给装配根：测试注入 `ScriptedChat` + `NullTracer`
    即可全离线跑通对话链路（本项目铁律：测内核行为，不测 LLM 本身）。省略 `model` 时按配置
    实例化真实后端（Ollama 或任意 OpenAI 兼容端点），构造是惰性的，不会在启动时连网。
    """
    env_settings = Settings.from_env()
    # 公网暴露护栏（P0-3）：非回环地址绑定 + 无鉴权 = 任何人可改配置 / 自批命令 / 浏览全盘。
    # run_api.py 默认绑 127.0.0.1（本地安全）；Docker 绑 0.0.0.0 时若没开 AUTH_MODE，直接拒绝
    # 启动 —— 把"忘记配鉴权就公网裸奔"变成起不来的硬失败，而不是默默暴露。
    bind_host = os.environ.get("RUN_API_HOST", "127.0.0.1")
    if (
        bind_host not in ("127.0.0.1", "localhost", "::1")
        and not bind_host.startswith("127.")
        and env_settings.auth_mode == "off"
    ):
        raise RuntimeError(
            f"拒绝启动：绑定地址 {bind_host!r} 非回环，但 AUTH_MODE=off（无鉴权）。"
            "公网部署请设 AUTH_MODE=on，或改绑 127.0.0.1 经反向代理转发。"
        )

    runtime = build_runtime(
        domains=DOMAINS,
        # 具体域在本文件只出现这一处：查询服务以工厂形式交给装配根（它需要装配过程中建好的
        # 连接）。端点层从头到尾只见到 `DomainQueryService` 抽象。
        query_factory=HealthQueryService,
        registry_factory=_host_registry_factory,
        sqlite_path=sqlite_path,
        env_settings=env_settings,
        model=model,
        model_factory=model_factory,
        tracer=tracer,
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """进程生命周期：起后台调度与（按配置的）模型预热，退出时释放本运行时的资源。

        释放动作本身在装配根（`Runtime.shutdown`）：被热重建换掉的知识库连着 httpx 客户端与
        sqlite 连接，那些是内核持有的对象，HTTP 层不该认识它们的细节。
        """
        try:
            runtime.start_background()
            yield
        finally:
            runtime.shutdown()

    app = FastAPI(
        title="rolecard-agent 管理控制台", version="0.3.0", lifespan=_lifespan
    )
    app.state.ctx = AppContext(runtime=runtime)

    # C1：端点按职责分包，全部端点已迁出本文件。
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
    exempt_paths = [
        p.strip() for p in (env_settings.auth_exempt_paths or "").split(",") if p.strip()
    ]
    trusted_proxies = parse_trusted_proxies(env_settings.auth_trusted_proxies)

    @app.middleware("http")
    async def _authenticate(request: object, call_next: object) -> object:
        req = cast("Request", request)
        actor = resolve_actor(
            authorization=req.headers.get("authorization"),
            api_key=req.headers.get("x-api-key"),
            settings=env_settings,
        )
        # 来源 IP 只认 **TCP 对端**；X-Forwarded-For 仅在直连方命中 AUTH_TRUSTED_PROXIES
        # 时才采信（见 auth.client_ip：否则 `auto` 档可被一行请求头绕过）。
        peer = req.client.host if req.client else ""
        if actor.is_anonymous and auth_required(
            mode=env_settings.auth_mode,
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
        return {"status": "ok", "version": "0.3.0", "auth_mode": env_settings.auth_mode}

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
