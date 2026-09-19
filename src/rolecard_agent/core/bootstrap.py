"""装配根（bootstrap）：内核、它的服务、图与调度器的唯一组装点。

为什么存在（架构审计报告 §0 / §7）：这些装配步骤原先写在 `api/main.py::create_app` 里，
于是"装配一个能对话的内核"这件事**只有起 HTTP 服务才能做**。C/S 桌宠壳、评测脚本、冒烟
脚本要的其实是同一套东西（建库 → 播种 → 实例化服务 → 建图 → 可热重建），而它们都不该
import FastAPI。抽出来之后 `api/main.py` 只剩 HTTP 绑定：路由、中间件、鉴权、静态托管、
生命周期。

本模块**不知道任何具体域**：域查询服务与工具注册表由宿主注入（`query_factory` /
`registry_factory`）。这既是分层的硬要求（`check_core_no_domain_token` 会拦下 core 里出现
域专名），也让"哪些域存在"继续只有一个声明处（`domains/registry.py` 的 `DOMAINS`）。

热重建（`Runtime.rebuild`）的并发纪律沿用原实现：**构建在锁外、换装在锁内**。两个并发
重建各自完整构建（后写者胜出，浪费但正确），而三个可变引用 + 模型缓存的换装是单个临界区
—— 杜绝"新图配旧知识库"这种中间态被 SSE 请求看到。可变状态只有 `Runtime` 这一份，
宿主（`api.deps.AppContext`）一律读穿，所以不存在"两处各存一份、其中一处忘了换"的可能。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from rolecard_agent.config import Settings
from rolecard_agent.core import mcp_store, runtime_settings
from rolecard_agent.core.approvals import ApprovalService
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.domain_service import DomainQueryService
from rolecard_agent.core.graph import build_graph_config, build_kernel, build_model
from rolecard_agent.core.identity import DEFAULT_USER_ID, seed_demo_identity
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.memory import load_memory_text
from rolecard_agent.core.model_settings import ModelSettingsService, client_style
from rolecard_agent.core.nodes import ChatLike
from rolecard_agent.core.observability import TraceEvent, Tracer, make_tracer
from rolecard_agent.core.plugins import PluginService, seed_plugin_rows
from rolecard_agent.core.probes import ollama_keep
from rolecard_agent.core.reachout import (
    ReachoutScheduler,
    proactive_thread_id,
    proactive_thread_title,
)
from rolecard_agent.core.services import ServiceEndpointService
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.rag.retriever import KnowledgeBase, make_embedder, make_reranker
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import SqlConnection, ThreadLocalConnection, connect_threadlocal
from rolecard_agent.storage.db import bootstrap as apply_schema


#: 装配过程中已经建好的内核件。它是宿主"域接线工厂"的输入，也是 `Runtime` 上那些
#: 稳定引用的来源。单独一个类型而不是把 `Runtime` 本身传出去，是因为接线只需要这几个
#: 只读引用，不该拿到能换图、能触发重建的可变运行时。
@dataclass(frozen=True, slots=True)
class Assembly:
    conn: ThreadLocalConnection
    roles: RoleCardService
    plugins: PluginService
    ingestion: IngestionService
    #: 域查询服务（v1 单域）：由宿主注入，本模块不认识它是哪个域。
    query: DomainQueryService
    tracer: Tracer


#: 宿主提供的注册表工厂：拿到装配件、**当前有效配置**、当前知识库与"启用域"的实时读取器，
#: 返回工具注册表。各域的 tool factory 怎么接、上传目录从哪来，全留在宿主那一侧。
RegistryFactory = Callable[
    [Assembly, Settings, KnowledgeBase, Callable[[], list[str]]], ToolRegistry
]

EnabledDomains = Callable[[], list[str]]


def candidate_ids(services: ServiceEndpointService, key: str) -> list[str]:
    """某类服务当前启用的端点 id（按「服务」页的序）—— 后端选型的唯一事实面。"""
    return [c.id for c in services.ordered_candidates(key)]


def build_knowledge(eff: Settings, services: ServiceEndpointService) -> KnowledgeBase:
    """按有效配置与服务页端点建知识库（嵌入器/重排器是构造期注入的实例）。"""
    return KnowledgeBase(
        eff.chroma_path,
        make_embedder(
            eff,
            order=candidate_ids(services, "embedding"),
            endpoints=services.endpoint_map("embedding"),
        ),
        make_reranker(
            eff,
            order=candidate_ids(services, "rerank"),
            endpoints=services.endpoint_map("rerank"),
        ),
        eff.rag_min_similarity,
    )


def assemble_registry(
    assembly: Assembly,
    registry_factory: RegistryFactory,
    eff: Settings,
    knowledge: KnowledgeBase,
) -> ToolRegistry:
    """按有效配置拼工具注册表。

    MCP 生效集每次重解析（表行可能在两次重建之间被改动）；有生效 server 才把 "mcp" 加进
    启用域 —— 初始装配与热重建必须同构，否则改一次运行环境会让 MCP 工具已加载却被启用域
    挡掉。启用域本身是**闭包**，所以插件启停即刻生效。
    """
    mcp_eff = mcp_store.effective_servers(assembly.conn, eff.mcp_servers)
    eff_for_tools = eff.model_copy(update={"mcp_servers": mcp_eff})
    plugins = assembly.plugins

    def enabled_domains() -> list[str]:
        return [*plugins.enabled_domains(), *(["mcp"] if mcp_eff else [])]

    return registry_factory(assembly, eff_for_tools, knowledge, enabled_domains)


@dataclass
class Runtime:
    """装配完成的内核运行时。可变量（effective / knowledge / registry / state）只有这一份。"""

    #: 进程构建期的 env 快照（鉴权档位、数据路径等只能重启生效的项从它读）。
    env_settings: Settings
    #: 叠加了模型页配置与「运行环境」覆盖之后的**有效**配置；`rebuild` 时整体换掉。
    effective: Settings
    assembly: Assembly
    model_settings: ModelSettingsService
    services: ServiceEndpointService
    knowledge: KnowledgeBase
    registry: ToolRegistry
    approvals: ApprovalService
    #: 端点/图共享的可变槽位：graph / effective / default_model。
    state: dict[str, Any]
    registry_factory: RegistryFactory
    #: 测试注入的模型实例（None = 按配置构建）。注入了它就不再有"角色级后端"这回事。
    injected_model: ChatLike | None = None
    model_factory: Callable[..., ChatLike] = build_model
    checkpointer: Any = None
    #: 主动开口调度器只在实际跑后台循环时存在（`start_background` 里建）。
    reachout: ReachoutScheduler | None = field(default=None, repr=False)
    rebuild_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    role_models: dict[tuple[str | None, float | None], ChatLike] = field(
        default_factory=dict, repr=False
    )

    # -- 稳定引用的读穿 ------------------------------------------------------

    @property
    def conn(self) -> ThreadLocalConnection:
        return self.assembly.conn

    @property
    def roles(self) -> RoleCardService:
        return self.assembly.roles

    @property
    def plugins(self) -> PluginService:
        return self.assembly.plugins

    @property
    def ingestion(self) -> IngestionService:
        return self.assembly.ingestion

    @property
    def query(self) -> DomainQueryService:
        return self.assembly.query

    @property
    def tracer(self) -> Tracer:
        return self.assembly.tracer

    def candidate_ids(self, key: str) -> list[str]:
        return candidate_ids(self.services, key)

    # -- 模型解析与图 --------------------------------------------------------

    def resolve_role_model(
        self, backend_name: str | None, temperature: float | None = None
    ) -> ChatLike:
        """US-8：角色声明了后端名 → 按名解析；未声明 → 默认模型。

        `temperature` 参与缓存键：同一后端在不同温度下是**不同的模型实例**
        （采样参数只能在构造期设置，见 `core.graph._init_model`）。
        未知后端名（设置页删掉了一个仍被角色引用的后端）→ 降级到默认并留痕，而不是
        让整轮对话 500：权限 fail-closed，可用性 fail-soft。
        """
        if self.injected_model is not None:
            return self.injected_model
        if not backend_name and temperature is None:
            return self.state["default_model"]
        cache_key = (backend_name, temperature)
        cached = self.role_models.get(cache_key)
        if cached is not None:
            return cached
        try:
            built = self.model_factory(self.effective, backend_name, temperature)
        except KeyError:
            self.tracer.emit(
                TraceEvent(event="role_backend_missing", detail={"backend": backend_name})
            )
            return self.state["default_model"]
        self.role_models[cache_key] = built
        return built

    def build_graph(self, model: ChatLike, registry: ToolRegistry, eff: Settings) -> Any:
        """建（编译）一张对话图。`model_resolver` 指向本 Runtime，角色级路由与热重建同源。"""
        return build_kernel(
            model=model,
            registry=registry,
            roles=self.roles,
            tracer=self.tracer,
            settings=eff,
            checkpointer=self.checkpointer,
            plugins=self.plugins,
            model_resolver=self.resolve_role_model,
            # 跨会话记忆的读取器：每次调用实时读库；总开关在 call_model 里按当前有效配置把关。
            memory_provider=lambda: load_memory_text(self.conn),
        )

    # -- 热重建 --------------------------------------------------------------

    def rebuild(self) -> None:
        """按当前设置与服务端点引用重建全部运行时对象：模型、知识库、注册表、图。

        由两条路径触发：模型设置保存（settings 端点）与服务端点变更（services 端点）。
        嵌入器/重排器是 KnowledgeBase 构造时注入的实例，引用变了必须连知识库一起重造；
        工具闭包持有知识库，所以注册表也要跟着重建 —— 顺序即依赖序。
        """
        eff = runtime_settings.apply_overrides(
            self.model_settings.effective_settings(self.env_settings),
            runtime_settings.load_overrides(self.conn),
        )
        # 构建在锁外：两个并发重建各自完整构建，后写者胜出（浪费但正确）。
        self.role_models.clear()
        default_model = self.model_factory(eff, None)
        knowledge_new = build_knowledge(eff, self.services)
        registry_new = assemble_registry(self.assembly, self.registry_factory, eff, knowledge_new)
        graph_new = self.build_graph(default_model, registry_new, eff)
        old_knowledge = self.knowledge
        with self.rebuild_lock:
            # 换装是单个临界区：模型缓存 + 可变引用 + state 槽位一起翻，杜绝"新图配旧
            # 知识库"的中间态被 SSE 请求看到。
            self.role_models.clear()
            self.effective = eff
            self.knowledge = knowledge_new
            self.registry = registry_new
            self.state["effective"] = eff
            self.state["default_model"] = default_model
            self.state["graph"] = graph_new
        # 旧实例换装完成后才关闭（旧嵌入器/重排器持有的 httpx 连接在此释放）。
        if old_knowledge is not None and old_knowledge is not self.knowledge:
            with contextlib.suppress(Exception):
                old_knowledge.close()

    # -- 进程生命周期（宿主在自己的启动/退出路径里调用）------------------------

    def start_background(self) -> None:
        """起主动开口调度；按配置决定要不要把默认本地模型预热进显存。"""
        if self.reachout is None:
            # 调度器需要 `resolve_role_model`（角色可按 model_name 路由），而那是本对象的
            # 方法 —— 所以它在这里建，而不是在装配时塞进构造函数。
            # settings_provider 每次 tick 现取**有效配置**：总闸热切即时生效。
            self.reachout = ReachoutScheduler(
                settings_provider=lambda: self.effective,
                roles=self.roles,
                model_resolver=self.resolve_role_model,
                conn=self.conn,
                tracer=self.tracer,
                deliver=self.deliver_proactive,
            )
        self.reachout.start()
        if self.env_settings.model_pin_on_startup:
            threading.Thread(target=self.pin_default_model, daemon=True).start()

    def pin_default_model(self) -> None:
        """启动即预热默认模型：仅本地 Ollama(native) 有意义，按后端配置的 num_ctx
        以 keep_alive=-1 常驻。best-effort —— Ollama 没起 / 默认是云端 / 未配默认后端 /
        任何异常都静默跳过，绝不阻断启动或抛到请求路径。
        """
        with contextlib.suppress(Exception):
            backend = self.effective.backend(None)  # 未配默认 → KeyError，被 suppress 吞掉
            if client_style(backend.provider) == "native":
                ollama_keep(backend.base_url, backend.model, -1, num_ctx=backend.num_ctx)

    # -- 主动开口的投递（收件箱之外，还得能回话）--------------------------------

    def deliver_proactive(self, role: RoleCard, text: str) -> str | None:
        """把角色主动说的那句落进"该角色的主动会话"，返回线程 id（图还没建 → None）。

        为什么需要这一步：主动消息原先只进 `agent_reachout`，于是"角色找我，我却回不了、
        也点不开历史"（用户 2026-09-19）。落进会话后，回复与历史都直接复用既有对话链路，
        不需要再造一套消息通道；角色下一次生成时也能在自己的历史里看到说过什么。

        写检查点用 `graph.update_state`（与上传说明、图片注入同一路数）而**不跑图**：
        这句是角色"已经出口"的话，不是让它接着想 —— 跑图会变成替用户自言自语。
        """
        thread_id = proactive_thread_id(role.role_id)
        # 会话行按需建（幂等）：用户即便在会话列表里删了它，下一条主动消息会重新建回来。
        self.conn.execute(
            "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch, title)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(thread_id) DO NOTHING",
            (
                thread_id,
                DEFAULT_USER_ID,
                role.role_id,
                self.plugins.tool_epoch(),
                proactive_thread_title(role.role_name),
            ),
        )
        self.conn.commit()
        graph = self.state.get("graph")
        if graph is None:  # 还没有图（纯内核装配 / 装配失败）：收件箱那条照样有效
            return None
        graph.update_state(
            build_graph_config(thread_id, self.effective),
            {"messages": [AIMessage(content=text)]},
        )
        return thread_id

    def shutdown(self) -> None:
        """释放本运行时持有的资源：sqlite 连接、知识库的 httpx 客户端、调度线程。

        对话线程池（`api/chat.py` 的模块级 `_CHAT_POOL`）**不在这里关**：它是进程级对象，
        在一个运行时的收尾里关掉它，会让同进程里后续创建的运行时全部拿不到线程池
        （测试就是这么互相干扰的）—— 它的释放放在真实的进程退出路径
        （`scripts/run_api.py` 里 uvicorn.run 返回之后）。
        """
        if self.reachout is not None:
            self.reachout.stop()
        with contextlib.suppress(Exception):
            self.conn.close()
        with contextlib.suppress(Exception):
            if hasattr(self.knowledge, "close"):
                self.knowledge.close()


def build_runtime(
    *,
    domains: Sequence[str],
    query_factory: Callable[[SqlConnection], DomainQueryService],
    registry_factory: RegistryFactory,
    sqlite_path: Path | None = None,
    env_settings: Settings | None = None,
    model: ChatLike | None = None,
    model_factory: Callable[..., ChatLike] | None = None,
    tracer: Tracer | None = None,
) -> Runtime:
    """装配内核：建库 → 播种 → 服务实例化 → 知识库/注册表/图 → 可热重建的 `Runtime`。

    `domains` / `query_factory` / `registry_factory` 由宿主给出（本模块不认识任何具体域；
    查询服务以**工厂**传入，因为它要拿装配过程中建好的连接）。
    `sqlite_path` 可注入便于测试用临时库，省略时回退 `Settings.sqlite_path`。
    `model` / `tracer` / `model_factory` 同理：测试注入替身即可全离线跑通对话链路
    （本项目铁律：测内核行为，不测 LLM 本身）。`model_factory` 会在**默认模型构建、
    角色级路由解析、热重建**三处被调用，省略时用 `core.graph.build_model`。
    """
    settings = env_settings or Settings.from_env()
    db_path = sqlite_path or settings.sqlite_path
    # `connect_threadlocal` 而不是 `connect`：同一进程里 HTTP 端点与图执行会并发使用这个
    # 对象，而 sqlite3 连接不是线程安全的。对外仍表现为"一条连接"（见 storage/db.py）。
    conn = connect_threadlocal(db_path)
    # 每个 REGISTERED 域的 schema 都建好，表因此永远存在，重新启用插件无需 DDL。
    apply_schema(conn, enabled_domains=domains)
    seed_plugin_rows(conn, domains)
    seed_demo_identity(conn)
    roles = RoleCardService(conn)
    roles.seed_builtins()
    roles.seed_domain_roles()
    plugins = PluginService(conn, known_plugins=domains)
    ingestion = IngestionService(conn)
    model_settings = ModelSettingsService(conn)
    services = ServiceEndpointService(conn)
    # 一次性播种默认服务端点行（幂等）：此后「服务」页是后端的唯一事实面。
    services.seed_once()
    # env 后端播种进模型设置表（幂等，此后操作员在 UI 里改），再归一历史行的 provider。
    model_settings.seed_from_env(settings)
    model_settings.normalize_providers()
    # 轨迹器先于注册表：`search_knowledge` 闭包要持有它，否则 rag_search / rerank_fallback
    # 两个事件永远不会被 emit（审查报告 M3）。
    resolved_tracer = tracer or make_tracer(settings)
    # 有效配置 = 模型页（DB）配置 ⊕ 运行环境覆盖。必须先于知识库与注册表构建：嵌入器、
    # 联网与比对工具的闭包拿的都是**叠加后**的配置，而不是裸 env 快照。
    effective = runtime_settings.apply_overrides(
        model_settings.effective_settings(settings),
        runtime_settings.load_overrides(conn),
    )
    assembly = Assembly(
        conn=conn,
        roles=roles,
        plugins=plugins,
        ingestion=ingestion,
        query=query_factory(conn),
        tracer=resolved_tracer,
    )
    knowledge = build_knowledge(effective, services)
    registry = assemble_registry(assembly, registry_factory, effective, knowledge)
    factory = model_factory or build_model
    default_model = model or factory(effective, None)
    state: dict[str, Any] = {
        "graph": None,  # 下面 build 后回填；对话端点每次请求从这里取当前图
        "effective": effective,
        "default_model": default_model,
    }
    runtime = Runtime(
        env_settings=settings,
        effective=effective,
        assembly=assembly,
        model_settings=model_settings,
        services=services,
        knowledge=knowledge,
        registry=registry,
        approvals=ApprovalService(conn),
        state=state,
        registry_factory=registry_factory,
        injected_model=model,
        model_factory=factory,
        checkpointer=make_checkpointer(conn),
    )
    state["graph"] = runtime.build_graph(default_model, registry, effective)
    return runtime
