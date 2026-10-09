"""装配与热重建的实现 —— P2-1「Runtime ≤150」那把刀的 **Assembler** 半边（2026-10-09 落地）。

为什么单独一个模块（行内刀法原话：「门面收掉 + Assembler」）：

  * `Runtime` 上从前摆着"怎么把东西拼起来"的三块真逻辑（`build_graph` / `rebuild` /
    知识库与注册表的构造），它们把类撑到 291 行 —— 职责一拆，类只剩字段、稳定转调与
    进程生命周期；
  * **依赖方向**：bootstrap → assembler 单向。`Assembly` / `RegistryFactory` 这两个装配件
    形状因此**住在本模块**（bootstrap 从这里 import 回去用 —— 它们本来就是"装配"的产物，
    家在这里比在 Runtime 文件里更名副其实）；对 `Runtime` 本体只认 **`RuntimeKernel`
    那个形状**（本模块底部 Protocol），不 import 它 —— 反向 import 会造环，而本仓那把
    「包内无循环导入」的尺子连 `TYPE_CHECKING`/延迟 import 压住的环也算（2026-10-09 首版
    就是被它当场抓的，这正是它该抓的）；
  * 注释跟着代码走：每一块的判据与事故编号都从 `bootstrap.py` 逐字搬来 —— 注释说的
    "这里"从此指本文件，留在原处反而指错地址。`probe_startup_timing` 的补丁照旧生效：
    `build_runtime` 以 `assembler.build_knowledge(...)` 的**模块限定**形状调用，打本模块
    属性（定义处）即钩住。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from rolecard_agent.base.audit import AuditTrail
from rolecard_agent.base.observability import Tracer
from rolecard_agent.config import Settings
from rolecard_agent.core import runtime_settings
from rolecard_agent.core.agent.graph import build_kernel
from rolecard_agent.core.agent.nodes import ChatLike
from rolecard_agent.core.domain.domain_service import DomainQueryService
from rolecard_agent.core.ingest.ingestion import IngestionService
from rolecard_agent.core.ingest.knowledge_sources import KnowledgeSourceStore
from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.models.model_resolver import ModelResolver
from rolecard_agent.core.models.services import ServiceEndpointService
from rolecard_agent.core.plugins import PluginService, mcp_store
from rolecard_agent.core.telemetry.probes import vision_capability
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.rag.retriever import KnowledgeBase, make_embedder, make_reranker
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import SqlConnection, ThreadLocalConnection


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


class RuntimeKernel(Protocol):
    """`build_graph` / `rebuild` 需要的 **Runtime 形状**（结构化，不 import 具体类）。

    与 `BackgroundTask` / `ProactiveGatewayLike` 同一族：本模块只认形状、不认类型本体，
    依赖方向因此保持 bootstrap → assembler 单向（具体类在 bootstrap，它 import 本模块）。
    Runtime 的实现天然满足这个形状 —— mypy 在 bootstrap 调用点逐字段核过。
    """

    assembly: Assembly
    env_settings: Settings
    effective: Settings
    model_settings: ModelSettingsService
    services: ServiceEndpointService
    # 不加 `| None`：Runtime 声明它是必有字段，Protocol 上放宽反而让具体类对不上（不变型）。
    knowledge: KnowledgeBase
    registry: ToolRegistry
    state: dict[str, Any]
    # `model_factory` / `registry_factory` 声明为**只读属性**形状：本模块对它们只读
    # （rebuild / build_graph 都只是取用）。写成普通注解成员会要求数据类可赋值，而 mypy
    # 对 @dataclass 的字段一律判 read-only（2026-10-09 用 build/repro_protocol.py 三形状
    # 实测定性：裸默认值 / field(default=...) / 无默认 全都报 settable 不匹配）——
    # property 形状则两边都成立。
    @property
    def model_factory(self) -> Callable[..., ChatLike]: ...
    @property
    def registry_factory(self) -> RegistryFactory: ...
    checkpointer: Any
    rebuild_lock: threading.Lock
    models: ModelResolver

    @property
    def identity(self) -> str: ...

    def effective_for(self, user_id: str | None = None) -> Settings: ...

    def resolve_role_model(
        self,
        backend_name: str | None,
        temperature: float | None = None,
        *,
        user_id: str | None = None,
    ) -> ChatLike: ...

    def chat_memory(
        self, role_id: str | None, thread_id: str | None, user_id: str | None = None
    ) -> str: ...


def build_graph(rt: RuntimeKernel, model: ChatLike, registry: ToolRegistry, eff: Settings) -> Any:
    """建（编译）一张对话图。`model_resolver` 指向本 Runtime，角色级路由与热重建同源。"""
    return build_kernel(
        model=model,
        registry=registry,
        roles=rt.assembly.roles,
        tracer=rt.assembly.tracer,
        settings=eff,
        checkpointer=rt.checkpointer,
        plugins=rt.assembly.plugins,
        model_resolver=rt.resolve_role_model,
        # 「这一轮花谁的 key」的挂点（M2d 尾巴）：owner 由 `_turn_backend` 从 state 现传
        # 进来（显式，不问 ContextVar）。不接这一根的话，模型凭据与能力位都会按实例主人
        # 判 —— 两个身份各配同名后端时，B 会拿着 A 的快照去跑（`_turn_backend` 也是这么读的）。
        settings_resolver=lambda owner: rt.effective_for(owner or rt.identity),
        # 跨会话记忆的读取器：每次调用实时读库、**按本轮角色取**（该角色专属 → 无则回退
        # 全局），与主动开口同源一个 `memory_for_turn`；再补上她最近主动说过的原话
        # （`chat_memory`，别的那条线程里她得知道自己提醒过什么）。总开关在 call_model
        # 里再把关一次（闭着就不问）。
        memory_provider=rt.chat_memory,
        # 视觉能力探测（P1-2）：Ollama `/api/show` 的 capabilities，带 TTL 缓存。
        # 只有"声明不支持 + 探测确认不支持"两条同时成立才会调用前拦（见 nodes 里那段）。
        vision_probe=vision_capability,
    )


def rebuild(rt: RuntimeKernel) -> None:
    """按当前设置与服务端点引用重建全部运行时对象：模型、知识库、注册表、图。

    由两条路径触发：模型设置保存（settings 端点）与服务端点变更（services 端点）。
    嵌入器/重排器是 KnowledgeBase 构造时注入的实例，引用变了必须连知识库一起重造；
    工具闭包持有知识库，所以注册表也要跟着重建 —— 顺序即依赖序。
    """
    # 这一步建的是**实例主人**那份（`rt.effective`）：知识库、注册表、工具闭包与
    # 编译期默认模型都由它喂，问的是"这台机器能干什么"。请求级那份凭据快照不在这一步，
    # 它按需在 `effective_for(本轮主人)` 里拼 —— 两条缓存同生共死，所以下面一起清。
    eff = runtime_settings.apply_overrides(
        rt.model_settings.effective_settings(rt.env_settings, user_id=rt.identity),
        runtime_settings.load_overrides(rt.assembly.conn),
    )
    # 构建在锁外：两个并发重建各自完整构建，后写者胜出（浪费但正确）。
    # 代数**先加再清**（`ModelResolver.invalidate`）：加在清之前，任何一个"清之前就
    # 进去了、清之后才落笔"的在飞写者手上都拿着旧代数，回填会被它自己否掉（`R28-05`）。
    rt.models.invalidate()
    default_model = rt.model_factory(eff, None)
    knowledge_new = build_knowledge(eff, rt.services, rt.assembly.conn)
    registry_new = assemble_registry(rt.assembly, rt.registry_factory, eff, knowledge_new)
    graph_new = build_graph(rt, default_model, registry_new, eff)
    old_knowledge = rt.knowledge
    with rt.rebuild_lock:
        # 换装是单个临界区：模型缓存 + 可变引用 + state 槽位一起翻，杜绝"新图配旧
        # 知识库"的中间态被 SSE 请求看到。
        # 只清不加代数：第一次清到这一刻之间，可能有写者拿着**换装前**的配置回填过。
        rt.models.clear_caches()
        rt.effective = eff
        rt.knowledge = knowledge_new
        rt.registry = registry_new
        rt.state["effective"] = eff
        rt.state["default_model"] = default_model
        rt.state["graph"] = graph_new
    # 旧实例换装完成后才关闭（旧嵌入器/重排器持有的 httpx 连接在此释放）。
    if old_knowledge is not None and old_knowledge is not rt.knowledge:
        with contextlib.suppress(Exception):
            old_knowledge.close()
