"""装配根（bootstrap）：内核、它的服务、图与调度器的唯一组装点。

为什么存在（架构审计报告 §0 / §7）：这些装配步骤原先写在 `api/main.py::create_app` 里，
于是"装配一个能对话的内核"这件事**只有起 HTTP 服务才能做**。C/S 桌宠壳、评测脚本、冒烟
脚本要的其实是同一套东西（建库 → 播种 → 实例化服务 → 建图 → 可热重建），而它们都不该
import FastAPI。抽出来之后 `api/main.py` 只剩 HTTP 绑定：路由、中间件、鉴权、静态托管、
生命周期。

本模块**不知道任何具体域**：域名单、各域查询服务、域种子角色与工具注册表都由宿主注入
（`domains` / `query_services_factory` / `domain_seed_roles` / `registry_factory`）。这既是
分层的硬要求（`check_core_no_domain_token` 会拦下 core 里出现域专名），也让"哪些域存在"
继续只有一个声明处（`domains/registry.py` 的目录枚举，2026-10-04 审查快照的域机制条目）。

热重建（`Runtime.rebuild`）的并发纪律沿用原实现：**构建在锁外、换装在锁内**。两个并发
重建各自完整构建（后写者胜出，浪费但正确），而三个可变引用 + 模型缓存的换装是单个临界区
—— 杜绝"新图配旧知识库"这种中间态被 SSE 请求看到。可变状态只有 `Runtime` 这一份，
宿主（`api.deps.AppContext`）一律读穿，所以不存在"两处各存一份、其中一处忘了换"的可能。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rolecard_agent.base.audit import AuditTrail
from rolecard_agent.base.identity import (
    active_user_id,
    ensure_identity_row,
    resolve_instance_identity,
)
from rolecard_agent.base.observability import TraceEvent, Tracer, make_tracer
from rolecard_agent.base.paths import user_data_root
from rolecard_agent.config import Settings
from rolecard_agent.core import mcp_store, runtime_settings
from rolecard_agent.core.approvals import ApprovalService, sweep_interrupted
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.domain_service import DomainQueryService
from rolecard_agent.core.graph import build_kernel, build_model
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.knowledge_sources import KnowledgeSourceStore
from rolecard_agent.core.migrations import MIGRATION_PLAN
from rolecard_agent.core.model_resolver import ModelResolver
from rolecard_agent.core.model_settings import ModelSettingsService, client_style
from rolecard_agent.core.nodes import ChatLike
from rolecard_agent.core.plugins import PluginService, seed_plugin_rows
from rolecard_agent.core.proactive import ProactiveGateway
from rolecard_agent.core.probes import ollama_keep, vision_capability
from rolecard_agent.core.reachout import (
    ReachoutScheduler,
)
from rolecard_agent.core.retention import prune_retention_tables
from rolecard_agent.core.services import ServiceEndpointService
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.rag.retriever import KnowledgeBase, make_embedder, make_reranker
from rolecard_agent.roles.models import RoleCard, RoleCardCreate
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import (
    RETENTION_BACKUP_DIRNAME,
    SqlConnection,
    ThreadLocalConnection,
    connect_threadlocal,
)
from rolecard_agent.storage.db import bootstrap as apply_schema


#: 装配过程中已经建好的内核件。它是宿主"域接线工厂"的输入，也是 `Runtime` 上那些
#: 稳定引用的来源。单独一个类型而不是把 `Runtime` 本身传出去，是因为接线只需要这几个
#: 只读引用，不该拿到能换图、能触发重建的可变运行时。
@dataclass(frozen=True, slots=True)
class Assembly:
    conn: ThreadLocalConnection
    roles: RoleCardService
    #: 审计写入的唯一咽喉（`R102-07`）：端点侧 `ctx.audit.log(...)` 走的就是这一件。
    audit: AuditTrail
    plugins: PluginService
    ingestion: IngestionService
    #: 各域自己的查询服务（域 id → 服务；无查询服务的域不进这张表）。由宿主按域自描述
    #: 声明逐个建（`domains.registry.build_query_services`），本模块不认识其中任何一个
    #: 具体域 —— 从此也不存在"把唯一一个服务喂给唯一一个工具工厂"的喂错域中间态。
    queries: Mapping[str, DomainQueryService]
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


def build_knowledge(
    eff: Settings, services: ServiceEndpointService, conn: SqlConnection
) -> KnowledgeBase:
    """按有效配置与服务页端点建知识库（嵌入器/重排器是构造期注入的实例）。

    `R102-55`：连**来源投影表**一起注入（`knowledge_source`）—— 概览页的来源清单
    从此出它，不再全量倒灌 chroma 元数据。`conn` 是宿主那份（线程安全的
    `ThreadLocalConnection`）；`rag/` 只认 Protocol，不认识 storage。
    """
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
        KnowledgeSourceStore(conn),
    )


def heal_knowledge_sources(
    knowledge: KnowledgeBase, conn: SqlConnection, *, tracer: Tracer | None = None
) -> int:
    """投影表的一次性种子（`R102-55` 根治的迁移半边）：把**存量**分块的来源补进表。

    为什么需要：投影表是新加的读侧事实面，而两个真根上都已经有分块 —— 安装根的
    `elysia_lore`（16 条；走的是旁路导入，**没有 ingestion 台账**）与 dev 根的一批旧上传
    （台账在 09-30 清过，如今 0 行）。不做种子，概览页这些来源会当场消失。
    判据：某作用域"投影里没有名字、chroma 里有分块" ⇒ 扫一次它的元数据回填；
    空集合反过来清掉投影里的残留行（自愈）。种子跑过之后这里只花 list + count + 点查。
    返回本次回填的行数（0 = 无事可做）；真回填过才 emit 迁移事件（`R102-64` 的口径）。
    """
    store = KnowledgeSourceStore(conn)
    seeded = 0
    for scope, count in knowledge.collection_counts():
        if count == 0:
            store.forget_scope(scope)
            continue
        if store.names(scope):
            continue
        for source_key, name in knowledge.source_pairs(scope):
            store.remember(scope, source_key, name)
            seeded += 1
    if seeded and tracer is not None and hasattr(tracer, "emit"):
        tracer.emit(TraceEvent(event="knowledge_sources_seeded", detail={"rows": seeded}))
    return seeded


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
    model_factory: Callable[..., ChatLike] = build_model
    checkpointer: Any = None
    #: 主动开口调度器只在实际跑后台循环时存在（`start_background` 里建）。
    reachout: ReachoutScheduler | None = field(default=None, repr=False)
    rebuild_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: 模型解析那一格（两份缓存 + 代数）搬进 `core/model_resolver.py`（2026-10-04 审查
    #: 快照里"Runtime 单对象多职责"那一格）：本对象只留 `effective_for` /
    #: `resolve_role_model` 两个**转调**方法（对外形状不变，调用方与代数测试零改）。
    #: 装配末尾在 `build_runtime` 里挂上（它要读实例主人那份配置，构造期还没有）。
    models: ModelResolver = field(init=False, repr=False)
    #: 主动开口的投递与会话上下文那一族（`_proactive_rows` / `proactive_recent_*` /
    #: `deliver_proactive` / `chat_memory` 的回声半边）搬进 `core/proactive.py`（同一格
    #: 审查快照的第二刀）：本对象只留**转调**，调度器与端点的调用形状不变。
    proactive: ProactiveGateway = field(init=False, repr=False)

    # -- 稳定引用的读穿 ------------------------------------------------------

    @property
    def identity(self) -> str:
        """**这台实例的主人**（`IDENTITY_USER_ID`，空=本机那份）。

        后台那条链（主动开口的投递、图里的域工具）没有"这次请求"可问，读的就是这一个。
        请求级的解析以它为底（`AppContext.current_user()`），两层的关系写在
        `base/identity.resolve_instance_identity` 的 docstring 里。
        """
        return resolve_instance_identity(self.env_settings)

    @property
    def conn(self) -> ThreadLocalConnection:
        return self.assembly.conn

    @property
    def roles(self) -> RoleCardService:
        return self.assembly.roles

    @property
    def audit(self) -> AuditTrail:
        return self.assembly.audit

    @property
    def plugins(self) -> PluginService:
        return self.assembly.plugins

    @property
    def ingestion(self) -> IngestionService:
        return self.assembly.ingestion

    def query_service(self, domain_id: str) -> DomainQueryService:
        """按域 id 取**该域自己的**查询服务 —— 缺域就 loud，不回落到别的域。

        为什么不留一个"唯一查询服务"的属性：v1 只有一个富域时那样写省事，但第二个域
        一来它就变成"谁碰到谁拿走"的共享槽（拿错域的服务 = 读写落进另一套表）。域名单
        来自宿主注入的映射，本模块仍然不认识任何域名。
        """
        try:
            return self.assembly.queries[domain_id]
        except KeyError:
            raise KeyError(
                f"域 {domain_id!r} 没有查询服务（已装配：{sorted(self.assembly.queries)}）"
            ) from None

    @property
    def tracer(self) -> Tracer:
        return self.assembly.tracer

    def candidate_ids(self, key: str) -> list[str]:
        return candidate_ids(self.services, key)

    # -- 模型解析与图 --------------------------------------------------------

    def effective_for(self, user_id: str | None = None) -> Settings:
        """**这一次模型调用花谁的 key** 的那份有效配置（转调 `ModelResolver`）。

        实现与它买的那两条纪律（按人取凭据 / 缓存代数）都在 `core/model_resolver.py`；
        这里保留方法本身，是因为它是 Runtime 的稳定对外形状（节点、调度器、端点都调它），
        拆职责不该让调用方跟着搬家。
        """
        return self.models.effective_for(user_id)

    def resolve_role_model(
        self,
        backend_name: str | None,
        temperature: float | None = None,
        *,
        user_id: str | None = None,
    ) -> ChatLike:
        """US-8：角色声明了后端名 → 按名解析；未声明 → 默认模型（转调 `ModelResolver`）。

        凭据按本轮主人取、未知后端降级留痕、缓存代数挡旧值 —— 三条都住在
        `core/model_resolver.py::resolve_role_model`，本方法只是门面。
        """
        return self.models.resolve_role_model(backend_name, temperature, user_id=user_id)

    @property
    def role_models(self) -> dict[tuple[str, str | None, float | None], ChatLike]:
        """角色级模型缓存的**只读穿**（转调 `ModelResolver`）。

        为什么留这一格而不是让调用方去摸 `models.role_models`：缓存本体搬了家，但钉它
        那条纪律的代数测试（`R28-05`，`test_an_inflight_build_...`）不该跟着搬 —— 拆职责
        的验收就是"守语义的用例一行不改仍然绿"。写路径只有 `models` 自己与 `rebuild`。
        """
        return self.models.role_models

    def chat_memory(self, role_id: str | None, thread_id: str | None) -> str:
        """这一轮她该看见什么：记忆 + 最近主动说过的原话（转调 `ProactiveGateway`）。

        回声那半边的全部道理（为什么补、什么时候**不**补、本轮主人现取）写在
        `core/proactive.py::chat_memory`；这里是图装配用的挂点，形状不动。
        """
        return self.proactive.chat_memory(role_id, thread_id)

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
            # 「这一轮花谁的 key」的挂点（M2d 尾巴）：节点内部现取，那时本轮主人已绑进
            # 上下文。不接这一根的话，模型凭据与能力位都会按实例主人判 —— 两个身份各配
            # 同名后端时，B 会拿着 A 的快照去跑（`_turn_backend` 也是这么读的）。
            settings_resolver=lambda: self.effective_for(active_user_id(self.identity)),
            # 跨会话记忆的读取器：每次调用实时读库、**按本轮角色取**（该角色专属 → 无则回退
            # 全局），与主动开口同源一个 `memory_for_turn`；再补上她最近主动说过的原话
            # （`chat_memory`，别的那条线程里她得知道自己提醒过什么）。总开关在 call_model
            # 里再把关一次（闭着就不问）。
            memory_provider=self.chat_memory,
            # 视觉能力探测（P1-2）：Ollama `/api/show` 的 capabilities，带 TTL 缓存。
            # 只有"声明不支持 + 探测确认不支持"两条同时成立才会调用前拦（见 nodes 里那段）。
            vision_probe=vision_capability,
        )

    # -- 热重建 --------------------------------------------------------------

    def rebuild(self) -> None:
        """按当前设置与服务端点引用重建全部运行时对象：模型、知识库、注册表、图。

        由两条路径触发：模型设置保存（settings 端点）与服务端点变更（services 端点）。
        嵌入器/重排器是 KnowledgeBase 构造时注入的实例，引用变了必须连知识库一起重造；
        工具闭包持有知识库，所以注册表也要跟着重建 —— 顺序即依赖序。
        """
        # 这一步建的是**实例主人**那份（`self.effective`）：知识库、注册表、工具闭包与
        # 编译期默认模型都由它喂，问的是"这台机器能干什么"。请求级那份凭据快照不在这一步，
        # 它按需在 `effective_for(本轮主人)` 里拼 —— 两条缓存同生共死，所以下面一起清。
        eff = runtime_settings.apply_overrides(
            self.model_settings.effective_settings(self.env_settings, user_id=self.identity),
            runtime_settings.load_overrides(self.conn),
        )
        # 构建在锁外：两个并发重建各自完整构建，后写者胜出（浪费但正确）。
        # 代数**先加再清**（`ModelResolver.invalidate`）：加在清之前，任何一个"清之前就
        # 进去了、清之后才落笔"的在飞写者手上都拿着旧代数，回填会被它自己否掉（`R28-05`）。
        self.models.invalidate()
        default_model = self.model_factory(eff, None)
        knowledge_new = build_knowledge(eff, self.services, self.assembly.conn)
        registry_new = assemble_registry(self.assembly, self.registry_factory, eff, knowledge_new)
        graph_new = self.build_graph(default_model, registry_new, eff)
        old_knowledge = self.knowledge
        with self.rebuild_lock:
            # 换装是单个临界区：模型缓存 + 可变引用 + state 槽位一起翻，杜绝"新图配旧
            # 知识库"的中间态被 SSE 请求看到。
            # 只清不加代数：第一次清到这一刻之间，可能有写者拿着**换装前**的配置回填过。
            self.models.clear_caches()
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
                thread_lines=self.proactive_recent_lines,
                thread_window=self.proactive_recent_window,
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

    # -- 主动开口的投递与会话上下文（转调 `ProactiveGateway`）---------------------
    #
    # 实现与它买的那些道理（checkpoint 是唯一真相 / 不跑图 / 拿不到锁就不投但不算丢 /
    # 本轮主人现取）都住在 `core/proactive.py`。这里留门面，是因为它们是调度器、端点与
    # 探针脚本的稳定调用形状 —— 拆职责不该让调用方跟着搬家。

    def proactive_recent_lines(self, role_id: str, *, limit: int = 6) -> str:
        return self.proactive.recent_lines(role_id, limit=limit)

    def proactive_recent_window(self, role_id: str, *, limit: int = 8) -> str:
        return self.proactive.recent_window(role_id, limit=limit)

    def deliver_proactive(self, role: RoleCard, text: str) -> str | None:
        return self.proactive.deliver(role, text)

    def shutdown(self) -> None:
        """释放本运行时持有的资源：sqlite 连接、知识库的 httpx 客户端、调度线程。

        对话线程池（`api/chat.py` 的模块级 `_CHAT_POOL`）**不在这里关**：它是进程级对象，
        在一个运行时的收尾里关掉它，会让同进程里后续创建的运行时全部拿不到线程池
        （测试就是这么互相干扰的）—— 它的释放放在真实的进程退出路径
        （`scripts/run_api.py` 里 uvicorn.run 返回之后）。
        """
        if self.reachout is not None:
            self.reachout.stop()
        # 关掉之前先把 WAL 落回主库（`R28-16` 的另一半）：自动检查点只在**有写**时触发，
        # 而一台闲置的机器可能连着两天不再写一笔 —— 那近两天的数据就一直只活在 -wal 里，
        # 而 -wal 坏掉等于那两天全没（备份走的是 sqlite `backup()`，它读得到 WAL，所以
        # **备份不受影响**，受影响的是"盘坏 / 文件被删"这一条）。正常退出路径上做一次
        # `TRUNCATE`，界面上就回到"主文件是最新的、WAL 是空的"这个可判断的形状。
        # ⚠️ 但**别把这件事只指望在这里**（`R28-48`）：这台机器的发版形态没有一条退出路径跑得
        # 到这一行 —— 壳自己退出与安装包关旧进程都是 `taskkill /T /F`（硬杀），跑不到 lifespan
        # 的 `finally`。真正天天兑现的那一次在启动：`checkpointer.truncate_wal_at_boot`。
        # 这里留着，管的是能走到这一步的另外两条路（POSIX 的 SIGTERM、开发态 Ctrl+C）。
        with contextlib.suppress(Exception):
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with contextlib.suppress(Exception):
            self.conn.close()
        with contextlib.suppress(Exception):
            if hasattr(self.knowledge, "close"):
                self.knowledge.close()


def build_runtime(
    *,
    domains: Sequence[str],
    query_services_factory: Callable[[SqlConnection], Mapping[str, DomainQueryService]],
    registry_factory: RegistryFactory,
    domain_seed_roles: Sequence[RoleCardCreate],
    sqlite_path: Path | None = None,
    env_settings: Settings | None = None,
    model: ChatLike | None = None,
    model_factory: Callable[..., ChatLike] | None = None,
    tracer: Tracer | None = None,
) -> Runtime:
    """装配内核：建库 → 播种 → 服务实例化 → 知识库/注册表/图 → 可热重建的 `Runtime`。

    `domains` / `query_services_factory` / `registry_factory` / `domain_seed_roles` 由宿主
    给出（本模块不认识任何具体域）：各域的查询服务以**工厂**传入（它要拿装配过程中建好的
    连接），域种子角色是**已经聚合好的一沓卡**（宿主从各域 `SPEC.seed_roles` 聚出来，见
    `domains.registry.domain_seed_roles`）—— 这一项刻意**没有默认值**：给了默认 `()` 就
    等于允许某个宿主"忘了接域角色"而没有任何声音，那是本项目最不接受的静默降级。
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
    # 搬层那一步由装配根交给 storage（`R102-08`）：内核认识自己的上层形状，storage 不必
    # 反过来 import core —— 全仓那条唯一的模块级真环就此断开。
    # `plan` 是业务迁移的**全部**（清重 / 滞留自愈 / 整表重建 / 搬层）：步骤的清单与顺序
    # 住在 `core/migrations.py`，storage 只认这个形状（P1-6 的 split-brain 收口）。
    apply_schema(conn, enabled_domains=domains, plan=MIGRATION_PLAN)
    seed_plugin_rows(conn, domains)
    # 出厂卡也有主人：这台实例的主人（`IDENTITY_USER_ID`，空=本机那份）。§4.1 的"两份完整
    # 数据集"落到角色卡上就是这句 —— 每张卡都有归属，读路径只认 `RoleCards` 那个按人过滤的视图。
    owner = resolve_instance_identity(settings)
    # 外键要有对象可指：演示身份 + **这台实例的主人**各一行（同一枚名字时第二次是空转）。
    # 只种演示身份的那一版，在 `IDENTITY_USER_ID` 真的指向第二个人时会让 `POST /api/session`
    # 当场 IntegrityError —— 见 `base/identity.py` 里那句"为什么实例主人也要走这里"。
    ensure_identity_row(conn)
    ensure_identity_row(conn, owner)
    # 终态兜底的崩溃半边（`R102-47`）：进程在命令执行期间被硬杀（本仓装机路径就是
    # `taskkill /F`）没有异常可接，approved 行会永远挂着 —— 开机一次清扫把它收成
    # done + error，模型读到"执行结果丢失"而不是永久的"正在执行"。
    sweep_interrupted(conn)
    # 三张只增表的 retention（`R102-29`；2026-10-02 拍板：分表定档）。
    # 0 = 该档永不清理（旧行为）。reachout 走 per-role `reachout_keep` 的既有机制。
    # 有清理量才落事件（`R102-64` 的事件流），每次开机刷屏没有信息量。
    pruned = prune_retention_tables(
        conn,
        audit_log_days=settings.audit_log_retention_days,
        audit_log_max_rows=settings.audit_log_max_rows,
        approval_done_days=settings.approval_done_retention_days,
        # 「先备份再删」的那一半（`R102-29` 拍板的第三句，10-03 才补上）：要删的行先落成
        # JSONL 再 DELETE。目录由装配根算 —— storage 不 import core（`R102-08` 的尺子）。
        backup_dir=user_data_root() / RETENTION_BACKUP_DIRNAME,
    )
    if any(pruned.values()):
        import sys as _sys

        print(
            "[schema-migrate] retention 清理："
            + ", ".join(f"{key}={count}" for key, count in pruned.items()),
            file=_sys.stderr,
            flush=True,
        )
    audit = AuditTrail(conn)
    roles = RoleCardService(conn)
    roles.seed_builtins(user_id=owner)
    # 域种子角色是**宿主聚合好的**（各域 SPEC.seed_roles）：内核不认识任何域名，也就
    # 无从自己列出"该播种哪些域角色"。参见 `domains.registry.domain_seed_roles`。
    roles.seed_domain_roles(domain_seed_roles, user_id=owner)
    plugins = PluginService(conn, known_plugins=domains)
    ingestion = IngestionService(conn)
    model_settings = ModelSettingsService(conn)
    services = ServiceEndpointService(conn, owner=owner)
    # 一次性播种默认服务端点行（幂等）：此后「服务」页是后端的唯一事实面。
    services.seed_once()
    # env 后端播种进模型设置表（幂等，此后操作员在 UI 里改），再归一历史行的 provider。
    model_settings.seed_from_env(settings, user_id=owner)
    model_settings.normalize_providers()
    # 轨迹器先于注册表：`search_knowledge` 闭包要持有它，否则 rag_search / rerank_fallback
    # 两个事件永远不会被 emit（审查报告 M3）。
    resolved_tracer = tracer or make_tracer(settings)
    # 有效配置 = 模型页（DB）配置 ⊕ 运行环境覆盖。必须先于知识库与注册表构建：嵌入器、
    # 联网与比对工具的闭包拿的都是**叠加后**的配置，而不是裸 env 快照。
    effective = runtime_settings.apply_overrides(
        model_settings.effective_settings(settings, user_id=owner),
        runtime_settings.load_overrides(conn),
    )
    assembly = Assembly(
        conn=conn,
        roles=roles,
        audit=audit,
        plugins=plugins,
        ingestion=ingestion,
        queries=query_services_factory(conn),
        tracer=resolved_tracer,
    )
    knowledge = build_knowledge(effective, services, conn)
    # 投影表的一次性种子（`R102-55`）：存量分块（旁路导入的 lore、旧上传）回填来源清单。
    # 热重建不再跑：表在库里，换装知识库实例不影响它。
    heal_knowledge_sources(knowledge, conn, tracer=resolved_tracer)
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
        model_factory=factory,
        checkpointer=make_checkpointer(conn),
    )
    # 模型解析那一格（审查快照的"Runtime 拆 ModelResolver"）：它要读**实例主人**那份
    # 当前配置，而配置的家在 Runtime 上，所以挂在对象建好之后 —— `identity` 也走闭包，
    # 实例主人的解析只留一处。
    runtime.models = ModelResolver(
        model_settings=model_settings,
        model_factory=factory,
        env_settings=settings,
        conn=conn,
        state=state,
        tracer=resolved_tracer,
        identity=lambda: runtime.identity,
        injected_model=model,
    )
    # 主动开口那一格（同一审查快照的第二刀）：连接、插件表（tool_epoch）与那份共享槽位
    # 都在装配末尾就位，`identity` 同样走闭包 —— 实例主人的解析只留一处。
    runtime.proactive = ProactiveGateway(
        conn=conn,
        tracer=resolved_tracer,
        plugins=plugins,
        state=state,
        identity=lambda: runtime.identity,
    )
    state["graph"] = runtime.build_graph(default_model, registry, effective)
    return runtime
