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
services / domains / workspace / reachouts / approvals / mcp / local_service），流式事件
框架与增量输出审核在 `api/chat.py`。对话内核与 `call_model` / `bind_tools` 解耦：router
只通过服务层改库，
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from rolecard_agent.api.access import OPERATOR, classify, operator_level_exempts
from rolecard_agent.api.access import allowed as access_allowed
from rolecard_agent.api.auth import (
    ROLE_OPERATOR,
    auth_required,
    client_ip,
    is_loopback,
    origin_guard_violation,
    parse_trusted_proxies,
    resolve_actor,
    roles_declared,
    unauthorized_response,
)
from rolecard_agent.api.body_cap import BodyCapMiddleware
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.api.errors import error_response, register_error_handlers
from rolecard_agent.api.ratelimit import Limiter, bucket_key, is_limited, paths_of
from rolecard_agent.api.routers import approvals as approvals_router
from rolecard_agent.api.routers import console as console_router
from rolecard_agent.api.routers import domains as domains_router
from rolecard_agent.api.routers import health as health_router
from rolecard_agent.api.routers import local_service as local_service_router
from rolecard_agent.api.routers import mcp as mcp_router
from rolecard_agent.api.routers import pets as pets_router
from rolecard_agent.api.routers import reachouts as reachouts_router
from rolecard_agent.api.routers import roles as roles_router
from rolecard_agent.api.routers import services as services_router
from rolecard_agent.api.routers import sessions as sessions_router
from rolecard_agent.api.routers import settings as settings_router
from rolecard_agent.api.routers import shell_release as shell_release_router
from rolecard_agent.api.routers import sync as sync_router
from rolecard_agent.api.routers import workspace as workspace_router
from rolecard_agent.base.identity import active_user_id, resolve_instance_identity
from rolecard_agent.base.observability import Tracer, logline
from rolecard_agent.base.paths import console_dist_dir
from rolecard_agent.config import Settings
from rolecard_agent.core.agent.nodes import ChatLike
from rolecard_agent.core.bootstrap import Assembly, Runtime, build_runtime
from rolecard_agent.core.build_info import read_build_info
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.domains.registry import (
    DOMAINS,
    build_query_services,
    build_registry,
    discover_specs,
    domain_seed_roles,
)
from rolecard_agent.domains.spec import RouterDeps
from rolecard_agent.features.proactive import build_gateway
from rolecard_agent.features.reachout import ReachoutScheduler
from rolecard_agent.rag.retriever import KnowledgeBase
from rolecard_agent.storage.db import set_request_epoch

#: 这个应用对外的**唯一版本号**：`FastAPI(version=…)` 与 `/api/health` 都读它。
#: 从前这里是两份手写的 `"0.3.0"`（09-28 轮 `R28-26`），而 `check_version_parity` 的正则只认
#: `version="x.y.z"` 那一形 —— 健康接口里那份**根本不在对齐检查范围内**：升版本时
#: pyproject 与 FastAPI 跟着走，`/api/health` 继续报旧号，而没有任何东西会红。
#: 合成一个常量之后 parity 比的是 `pyproject` ↔ 这一处，两份手写变成一份。
API_VERSION = "0.3.0"

# M5 前端构建产物的位置解析收在 `base/paths.console_dist_dir()`（09-30）：桌宠形象包
# 也要扫那一份 `dist/pets/`，两处各写一遍路径就会有"界面打得开、素材清单扫不到"的单边红。
# **每次 create_app 现读一次**，不在模块导入时冻成常量 —— `FRONTEND_DIST` 是部署期覆盖，
# 设得比 import 晚也必须生效（`tests/test_api.py` 那条"dist 缺失就回退提示页"就靠这一条；
# 我第一版把它写成了模块级 `_DEFAULT_DIST`，那条用例当场替我红了回来）。

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

    **这一轮到底在为谁读，由图节点在入口绑**（`base/identity.bound_user`，取
    `state["user_id"]`）：这里给的提供者是 `active_user_id(实例主人)` —— 绑过就是
    这条线程的主人，没绑过（后台调度、纯内核装配、老线程状态里缺这一项）才落到
    `IDENTITY_USER_ID` 那份。之所以要在上下文里绕这一道：工具对模型必须看起来零参数，
    把 user_id 做成工具入参等于让模型自己填"我是谁"。
    """
    return build_registry(
        roles=assembly.roles,
        ingestion=assembly.ingestion,
        query_services=assembly.queries,
        knowledge=knowledge,
        enabled_domains=enabled_domains,
        current_user=lambda: active_user_id(resolve_instance_identity(settings)),
        upload_dir=settings.upload_dir,
        # 联网与工作区工具的后端配置（搜索后端 / TAVILY_API_KEY / WORKSPACE_DIR）：传
        # **叠加了运行环境覆盖 + MCP 合并**的有效配置，工具闭包持有它，保存后经热重建生效。
        settings=settings,
        tracer=assembly.tracer,
        memory_conn=assembly.conn,
        fs_conn=assembly.conn,
    )


def _register_background_tasks(runtime: Runtime) -> None:
    """宿主接线：**后台任务由这里建并注册**，装配根只统一启停（快照"后台任务改宿主注册"）。

    主动开口的调度器是一件产品功能：它要的每一件都是 Runtime 对外的形状 —— 有效配置、
    角色服务、按本轮主人取模型、投递与会话上下文读法。从前这些零件在装配根里拼，内核
    因此认识了这个功能；搬到这里之后，内核只剩"注册表 + 退出顺序"两件事。
    """
    runtime.register_background(
        "reachout",
        ReachoutScheduler(
            # 每次 tick 现取**有效配置**：全局总闸热切即时生效。
            settings_provider=lambda: runtime.effective,
            roles=runtime.assembly.roles,
            model_resolver=runtime.resolve_role_model,
            conn=runtime.assembly.conn,
            tracer=runtime.assembly.tracer,
            deliver=runtime.deliver_proactive,
            thread_lines=runtime.proactive_recent_lines,
            thread_window=runtime.proactive_recent_window,
        ),
    )


def create_app(
    sqlite_path: Path | None = None,
    *,
    model: ChatLike | None = None,
    # 温度参与缓存键，工厂可能被传第三个参数 —— 签名放宽为可变参数（见 P1-2）。
    model_factory: Callable[..., ChatLike] | None = None,
    tracer: Tracer | None = None,
    # 限流器的时间源（默认真实墙钟）。**测试注入口，不是配置面**：固定窗口按整分钟切，
    # 端到端用例三连发只要跨过真实墙钟的整分钟边界，"第三发该被拦"就会落进新窗口拿到
    # 新配额（2026-10-09 Windows 臂实测：期待 429 拿到 404 —— 十余趟 CI 的首次边界命中，
    # 赌墙钟的用例迟早赌输）。`Limiter.hit(now=…)` 只够纯函数层，端到端过中间件碰不到，
    # 所以注入口上移到这里。生产路径不传 = 真实墙钟，零行为变化。
    limiter_clock: Callable[[], float] | None = None,
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
        # 域接线三件全部来自**域自己的声明**（`domains/registry.py` 的目录枚举）：查询服务
        # 按域 id 建一张映射、种子角色把各域 SPEC.seed_roles 聚成一沓、工具工厂读各域 SPEC。
        # 本文件因此不再 import 任何具体域类 —— 那句"isinstance 不匹配就拒绝启动"的检查
        # 随之搬进了域自己的工具工厂（喂错域在域那一侧 loud）。
        query_services_factory=build_query_services,
        registry_factory=_host_registry_factory,
        domain_seed_roles=domain_seed_roles(),
        # 主动开口的网关由宿主接进装配根：内核只认 `ProactiveGatewayLike` 那个形状，
        # 实现在 `features/proactive.py`（features→core 单向，内核不认识功能）。
        proactive_factory=build_gateway,
        sqlite_path=sqlite_path,
        env_settings=env_settings,
        model=model,
        model_factory=model_factory,
        tracer=tracer,
    )
    _register_background_tasks(runtime)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """进程生命周期：起后台调度与（按配置的）模型预热，退出时释放本运行时的资源。

        释放动作本身在装配根（`Runtime.shutdown`）：被热重建换掉的知识库连着 httpx 客户端与
        sqlite 连接，那些是内核持有的对象，HTTP 层不该认识它们的细节。

        **后台任务是宿主注册的**（2026-10-04 审查快照"后台任务改宿主注册"那一格）：主动
        开口调度器是一件产品功能，内核不该认识它。装配根只提供统一启停与退出顺序，功能
        接线住在这里 —— 调度器要的每一件都是内核对外形状（有效配置、角色服务、模型解析、
        投递与会话读法），接线动作即"把功能的零件拼回去"。
        """
        try:
            runtime.start_background()
            yield
        finally:
            runtime.shutdown()

    app = FastAPI(title="rolecard-agent 管理控制台", version=API_VERSION, lifespan=_lifespan)
    app.state.ctx = AppContext(runtime=runtime)

    # 错误响应的唯一出口：正文工厂 + 异常族→状态码注册表（HTTPException / ThreadBusy /
    # 角色卡、插件、模型配置、审批、摄取、上传各族）。从前 ThreadBusy 的 409 handler
    # 注册在这儿、而认证/限流/分级/来源护栏四个中间件自己拼裸文本 —— 两半形状不一致，
    # 中文文案到不了前端（它只读 JSON detail）。判据见 `api/errors.py` 的模块文档。
    register_error_handlers(app)

    # C1：端点按职责分包，全部端点已迁出本文件。
    app.include_router(roles_router.router)
    # 诊断端点（深探，随后还有指标）：**不是**免鉴权那条 /api/health —— 它要操作员，
    # 因为它回的是内部状态（库健全性、向量库开不开、配了几条后端）。
    app.include_router(health_router.router)
    app.include_router(sessions_router.router)
    app.include_router(console_router.router)
    app.include_router(settings_router.router)
    app.include_router(services_router.router)
    app.include_router(domains_router.router)
    app.include_router(workspace_router.router)
    app.include_router(reachouts_router.router)
    app.include_router(approvals_router.router)
    app.include_router(mcp_router.router)
    app.include_router(local_service_router.router)
    app.include_router(pets_router.router)
    app.include_router(shell_release_router.router)
    # 上行同步（M7）：对面那台跑的是同一份代码，所以清单端点与计划端点住在同一个 router 里。
    app.include_router(sync_router.router)

    # 域专属路由（2026-10-04 域机制收口，快照 P1-5）：宿主只交**请求期依赖**——上下文、
    # 身份与按域取查询服务的映射，路由本体由各域自己的 `DomainSpec.router_contrib` 交回来。
    # 从前这里是 `api/routers/records.py` 直接 import 具体域（`R102-10` 登记过的接缝）；
    # 搬回域内之后，api 层对具体域的 import 归零 —— `api domain seams` 名单为空是目标态。
    router_deps = RouterDeps(
        get_context=get_context,
        get_actor=get_actor,
        query_services=runtime.assembly.queries,
    )
    for spec in discover_specs():
        if spec.router_contrib is None:
            continue
        for domain_router in spec.router_contrib(router_deps):
            app.include_router(domain_router)

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
    # 豁免名单让到管理面上要**大声说**（warning 落 stderr）：让"匿名可达"扩到
    # operator 级端点无法悄悄发生。报警不拦 —— 分级那层还有 403 兜着。
    for flagged in operator_level_exempts(exempt_paths):
        logline(
            "warning",
            "auth-exempt-operator-path",
            f"AUTH_EXEMPT_PATHS 里的 {flagged} 是操作员级端点 —— 确认这是刻意的让步",
        )
    trusted_proxies = parse_trusted_proxies(env_settings.auth_trusted_proxies)
    # 凭证分族是否生效：配置里出现过 `operator:` 凭据才生效（见 auth.roles_declared）。
    # 随进程构建，与 exempt_paths/trusted_proxies 同类 —— 改了要重启，不在界面可改。
    roles_in_effect = roles_declared(env_settings)
    # 限流（v2.4 公网硬化）：桶长在**应用实例**上，额度随 env 构建（改了要重启）。
    # `RATE_LIMIT_PER_MINUTE=0`（默认）时 `Limiter` 永远放行 —— 本机单人形态逐字不变。
    limiter = Limiter(env_settings.rate_limit_per_minute, clock=limiter_clock)
    limited_paths = paths_of(env_settings.rate_limit_paths)

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
        origin = client_ip(req.headers, peer=peer, trusted=trusted_proxies)
        if actor.is_anonymous and auth_required(
            mode=env_settings.auth_mode,
            ip=origin,
            path=req.url.path,
            exempt=exempt_paths,
            method=req.method,
        ):
            # 客户端已经带了凭据（Basic 或 X-API-Key）就不再 challenge：见
            # `auth.unauthorized_response` 的 `challenge` 一节 —— 那一次弹框会把
            # "账号或密码不对"这句界面提示整个吃掉。
            carried = bool(req.headers.get("authorization") or req.headers.get("x-api-key"))
            status, headers, body = unauthorized_response(challenge=not carried)
            return error_response(status, body, headers)
        # 操作员面（`api/access.py` 那张表里没被降级的一切）：`on`/`auto` 档下必须是
        # **本机来源或操作员凭据**。这一条真正关掉的洞是"`AUTH_EXEMPT_PATHS` 配宽了一点，
        # 于是管理端点变成免凭据可达" —— 认证那一步会因豁免直接放过，分级这里补上。
        # 凭证分族补上另一半：远端"带了某条凭据"不再自动等于操作员，得是 `operator:` 那一族。
        # off 档不启用：它的语义就是"我本机单人用"，且非回环绑定已被启动护栏拒绝。
        if classify(req.url.path, req.method) == OPERATOR and not access_allowed(
            req.url.path,
            req.method,
            ip=origin,
            authenticated=not actor.is_anonymous,
            enforce=env_settings.auth_mode != "off",
            role=actor.role,
            roles_in_effect=roles_in_effect,
        ):
            # 文案要说清缺的是**哪一样**：已经认证却被拒 = 角色不够，不是"忘了带凭据"。
            # 报成"需要凭据"会让人对着已经填对的密码框发愣。
            user_is_authenticated_operator = actor.is_anonymous or actor.role == ROLE_OPERATOR
            reason = (
                "Forbidden: 这一项需要本机来源或操作员凭据"
                if user_is_authenticated_operator
                else "Forbidden: 这一项需要操作员凭据（当前凭据只是使用者角色）"
            )
            return error_response(403, reason)
        # 节流住在这里而不是另起一个中间件：**认证之后才谈"这个人能打多快"** —— 桶的键
        # 就是刚解析出的身份。`RATE_LIMIT_PER_MINUTE=0`（默认）时下面整段是死的。
        # **本机来源不设卡**（与认证那条"本机来源永远放行"同一份信任模型）：桌面壳、
        # 控制台、脚本与探针全从 127.0.0.1 来，主人坐在键盘前不该被自己的机器挡在门外；
        # 公网那档要保护的是**远端那个人**（他的服务器资源与别人 key 的额度）。
        if not is_loopback(origin) and is_limited(req.url.path, req.method, limited_paths):
            key = bucket_key(actor.id, anonymous=actor.is_anonymous, origin=origin)
            ok, retry_after = limiter.hit(key)
            if not ok:
                # 403 与 429 分开：这一条不是"你没权限"，是"你太快了" —— 文案里带上
                # 怎么调（配置项名），免得下一步去翻代码。
                return error_response(
                    429,
                    "Too Many Requests：这一身份对这类端点的请求太密，"
                    f"{retry_after} 秒后再试。"
                    "（上限见配置 RATE_LIMIT_PER_MINUTE，受管路径见 RATE_LIMIT_PATHS）",
                    headers={"Retry-After": str(retry_after)},
                )
        req.state.actor = actor
        # 这次请求的**档位事实**（`R102-58`）：来源 IP 与"凭据分族是否生效"，一起挂给路由层。
        # 为什么必须由中间件给而不是让路由自己算：解析 XFF / 认证档位是这一层的职责，路由
        # 重算一遍就是第二份实现（`X-Forwarded-For` 只在可信代理由才采信这条尤其不能漂）。
        # 出站目标允许清单（`api/access.outbound_target_allowed`）是第一处消费者。
        req.state.origin = origin
        req.state.roles_in_effect = roles_in_effect
        return await call_next(request)  # type: ignore[operator]

    # 来源标识护栏（`R102-45`）：认证只回答"带没带凭据 / 从哪连的"，这一层回答"**来源是谁**"。
    # off 档的信任模型是"TCP 对端是 127.0.0.1 = 本人"，而 rebinding 的浏览器对端同样是
    # 127.0.0.1 —— 实测可读全库数据并替用户批准命令。豁免路径、审批凭据这些下游防线全都
    # 建立在这层之上，它们单独都挡不住 rebinding。注册在 `_authenticate` **之后** = 执行在
    # 它之前（后注册者在外层）。判据见 `auth.origin_guard_violation`；
    # `LOCAL_ORIGIN_ENFORCE=0` 是回滚开关（一键回旧行为）。
    if env_settings.local_origin_enforce:

        @app.middleware("http")
        async def _local_origin_guard(request: object, call_next: object) -> object:
            req = cast("Request", request)
            reason = origin_guard_violation(
                host_header=req.headers.get("host"),
                origin=req.headers.get("origin"),
                sec_fetch_site=req.headers.get("sec-fetch-site"),
                auth_mode=env_settings.auth_mode,
            )
            if reason is not None:
                return error_response(403, reason)
            return await call_next(request)  # type: ignore[operator]

    # 跨域放行（M5）：**默认不装**。装了才允许别的 origin 的浏览器带着凭据打这里，
    # 而"开成 `*`"等于让任意网页在你已登录的浏览器里驱动这个后端 —— 所以这里只收
    # 精确 origin 列表，且空列表时连中间件都不加（行为与今天逐字节相同）。
    # 注册在认证中间件**之后** = 包在它外面：预检 OPTIONS 按规范不带凭据，
    # 让它先被 CORS 答掉，否则 `AUTH_MODE=on` 的云端会把预检 401，症状是
    # "切换器一直报连不上"，而对面日志里一个请求都没收到。
    allowed = [o.strip() for o in (env_settings.api_allow_origins or "").split(",") if o.strip()]
    if allowed:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed,
            allow_credentials=True,
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
            allow_methods=["*"],
        )

    # 请求体上限（`R102-45` 家族的收尾项，10-03 拍板：64 MiB + 413）。注册在最后 =
    # 包在所有中间件外面：超限的请求不该先被认证、被 CORS 预检、被任何一段读 body 的代码
    # 碰上 —— 那正是「一条大 body 就是一次无界的内存承诺」要拦的位置。
    # 判据见 `api/body_cap.py`（两道拦法：声明的长度 + 流进来的计数）。
    app.add_middleware(BodyCapMiddleware, limit=env_settings.max_body_bytes)

    @app.get("/api/health")
    def health() -> object:
        """探活：容器 healthcheck 与反代探活用，**必须免鉴权**（默认在 `AUTH_EXEMPT_PATHS` 里）。

        只回状态、当前认证档位，以及**这份代码是从哪个 commit 打的** —— 便于部署后确认"认证到底
        开没开"与"跑着的是哪一版"，不含任何凭证信息。

        `build` 那一格是 10-01 补的（台账 `R28-56`）：`probe_package_artifact.py` 那三层判据里，
        ① 比前端哈希、② 比刚构建的 exe 字节，一次**纯后端**改动会让 ① 一字不差地绿而 ② 在没有
        `build/sidecar` 时比不了 —— 于是"全绿"证明不了"屏幕上的后端是最新那一笔"。事实烤进产物之后，
        定版只需要问这一个字段。开发态现取 `git rev-parse HEAD` 与工作树脏旗；打不出来说 `unknown`，
        **不猜**（猜出来的相等比红更难查）。
        """
        return {
            "status": "ok",
            "version": API_VERSION,
            "auth_mode": env_settings.auth_mode,
            "build": read_build_info().as_dict(),
            # 上限常量给前端现读（Composer 的附图预检等）—— 前端不再手抄数字，
            # 改这里一处即可（2026-10-04 审查快照「上限常量散布」那条的出口）。
            "max_upload_bytes": env_settings.max_upload_bytes,
            "max_image_bytes": env_settings.max_image_bytes,
        }

    dist_dir = console_dist_dir()
    if (dist_dir / "index.html").exists():
        # 静态托管必须挂在 API 路由之后注册：FastAPI 按注册顺序匹配，先注册的 /api/* 优先。
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="console")
    else:

        @app.get("/", response_class=HTMLResponse)
        @app.get("/console", response_class=HTMLResponse)
        def console() -> str:
            return _FALLBACK_HTML

    return app
