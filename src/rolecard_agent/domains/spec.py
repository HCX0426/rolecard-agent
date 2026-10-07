"""域自描述契约：`DomainSpec` —— 「新增一个域要改几处中心代码」这个问题的答案。

2026-10-04 审查快照的域机制条目给当时的现实记的账是：新增一域要动 **≥6 处**中心代码
（`domains/registry.py` 的 DOMAINS 元组与 factories 字典、`api/main.py` 的 isinstance 接线、
`api/routers/records.py` 的抽取器分派、`roles/seed.py` 的工具名与知识作用域），于是
finance 域至今只能做一个 `lambda: []` 的空壳。本模块把「一个域是什么」收成**一份声明**，
中心代码只认这一份声明、不认域的名字：

  * `domains/registry.py` 按目录枚举各包导出的 `SPEC`（域名单、工具工厂、写工具分流）；
  * `api/main.py` 用 `build_query_services` / `domain_seed_roles` 接线，并把 `router_contrib`
    交回的路由挂上 —— 从此**零处** import 具体域（`api domain seams` 名单为空是目标态）；
  * `core/bootstrap.py` 只收宿主注入的映射与种子角色（分层要求：内核不认识任何域名）。

**金标准**（快照 P1-5 的验收句）：新增一个 demo 域（models/service/tools/schema 四件套 +
一个导出 `SPEC` 的目录）不改任何 registry / main / roles 代码，即可启动、建表、出工具。

本模块只定义形状，不 import 任何具体域 —— 它在 `domains` 包的最底层，被每个域反向依赖。
宿主（api 层）与域之间那两份**请求期**契约也在这里（`RouterHost` / `ActorLike`）：域路由
要的上下文与身份由宿主交进来，域只声明"我用得到哪几件"，反向不 import `rolecard_agent.api`
（api 在 domains 之上，反向 import 就是横向抓取）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fastapi import APIRouter
    from langchain_core.tools import BaseTool

    from rolecard_agent.base.audit import AuditTrail
    from rolecard_agent.base.observability import Tracer
    from rolecard_agent.core.domain_service import DomainQueryService
    from rolecard_agent.core.ingest.ingestion import IngestionService
    from rolecard_agent.rag.ocr import OcrBackend
    from rolecard_agent.rag.retriever import KnowledgeBase
    from rolecard_agent.roles.models import RoleCardCreate
    from rolecard_agent.storage.db import SqlConnection


@dataclass(frozen=True, slots=True)
class DomainToolContext:
    """域工具工厂在**装配期**拿到的全部输入 —— 每个域只拿自己那一件查询服务。

    `query` 按 `spec.id` 从宿主注入的映射里取（见 `core.bootstrap.Assembly.queries`）：
    装配点不再"把唯一一个查询服务喂给唯一一个工具工厂"，也就没有"喂错域"这种中间态。
    没有查询服务的域（纯数据型）这里是 None，工厂自己判。
    """

    ingestion: IngestionService
    query: DomainQueryService | None
    current_user: Callable[[], str]
    upload_dir: Path


#: `(ctx) -> 本域工具`。返回空元组 = 数据型域（无 LLM 工具，数据走通用 domain_data 通路）。
#:
#: 注意这里是**运行期赋值**而不是注解：`from __future__ import annotations` 管不到它，
#: 所以只在 TYPE_CHECKING 里出现的名字必须写成字符串前向引用（否则 import 本模块当场
#: NameError —— 装配点与 init_db 都要先 import 它）。
ToolFactory = Callable[[DomainToolContext], Sequence["BaseTool"]]

#: `(连接) -> 本域查询服务`。None = 本域没有查询服务。
QueryServiceFactory = Callable[["SqlConnection"], "DomainQueryService"]


class RouterHost(Protocol):
    """域路由在**请求期**从宿主拿到的上下文 —— `api.deps.AppContext` 结构性满足本协议。

    为什么是协议而不是 import `AppContext`：api 层在 domains **之上**，域反向 import 宿主
    就是横向抓取（分层契约当场红）。宿主把自己的上下文对象交进来，域只声明"我这个路由
    用到哪几件"——少一件，`api/main.py` 那句 `RouterDeps(...)` 会在 mypy 上直接不成立，
    于是"域要的宿主能力"这件事有了机器兜底，而不是靠读 api 的源码对齐。

    属性一律声明成 `@property`（只读）：宿主那几件本来就都是属性，读-only 的协议成员才
    结构性匹配得上（声明成可写变量，mypy 会报 "expected settable variable"）。
    """

    def current_user(self) -> str:
        """这次请求读写数据所用的身份（模型永远不能指定"我是谁"）。"""
        ...

    @property
    def audit(self) -> AuditTrail:
        """审计写入的唯一咽喉。"""
        ...

    @property
    def ingestion(self) -> IngestionService:
        """摄取台账（读 `source_file` / 幂等判据）。"""
        ...

    @property
    def knowledge(self) -> KnowledgeBase:
        """知识库（删报告时连向量分块一起清）。"""
        ...

    @property
    def tracer(self) -> Tracer:
        """轨迹器（模型调用事件要 emit 得出去）。"""
        ...

    @property
    def app_state(self) -> dict[str, Any]:
        """图 / 有效配置 / 默认模型的槽位（热重建后整体换掉，端点每请求读它）。"""
        ...

    def ocr_candidates(self) -> OcrBackend | None:
        """按「服务」页的 OCR 端点序选一个可用后端（兜底现场解析用）。"""
        ...


class ActorLike(Protocol):
    """宿主认证出来的操作员身份（`api.auth.Actor` 结构性满足：域只用得到 `id`）。"""

    @property
    def id(self) -> str:
        """写进审计的 actor id。"""
        ...


@dataclass(frozen=True, slots=True)
class RouterDeps:
    """宿主交给域路由的三样东西：两个请求期依赖 + 按域取查询服务的映射。

    `query_services` 传**整张映射**而不是替域挑好那一件：挑哪件是域自己的事（域认识自己的
    服务类，宿主不认识域名），域按自己的 id 取、取不到或类型不对就 loud —— 与工具工厂
    `_make_tools` 那道 isinstance 检查同一条纪律。
    """

    #: FastAPI 依赖（签名带 `request` 也无妨，用 `Callable[..., T]` 收）。
    get_context: Callable[..., RouterHost]
    get_actor: Callable[..., ActorLike]
    query_services: Mapping[str, DomainQueryService]


#: 域路由装配：宿主交出 `RouterDeps`，域交回自己的 `APIRouter`（无路由贡献的域给空）。
RouterContrib = Callable[[RouterDeps], Sequence["APIRouter"]]


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """一个域的**全部**自描述内容。装配点只读这份声明，从不点域名。

    字段即快照 P1-5 的那张清单：`id` / `tool_factory` / `query_service_factory` /
    `seed_roles` / `write_tool_names` / `router_contrib`。

    `write_tool_names` 声明的是**不允许执行器重试**的写工具（写台账的工具重试一次就多一条
    记录，审查报告 M10）：分流注册的判据住在域自己身上，装配点只照单执行 —— 谁能安全重试
    是工具的性质，不是宿主的知识。

    `router_contrib` 是域的**富 CRUD 路由**（报告/指标那族）：`api/main` 只负责把宿主的
    请求期依赖交进去、把交回来的路由挂上，路由内部用哪个服务类、抛哪些域异常，全在域自己
    那边 —— 这一格从前是 `api/routers/records.py` 直接 import 具体域（`R102-10` 登记在册
    的那个接缝），收进来之后 api 层对具体域的 import 归零。None = 本域没有专属路由（数据
    走通用 `domain_data` CRUD）。
    """

    #: 域 id：必须与目录名逐字相等（`discover_specs` 机器校验），也是 plugin 表的 plugin_id。
    id: str
    #: 本域工具工厂（数据型域给一个返回空序列的实现即可）。
    tool_factory: ToolFactory
    #: 本域查询服务工厂；None = 本域没有富查询服务（数据走通用 CRUD）。
    query_service_factory: QueryServiceFactory | None = None
    #: 出厂种子角色：随域插件播种（类型是自定义，已存在的行不覆盖）。
    seed_roles: tuple[RoleCardCreate, ...] = ()
    #: 有副作用、不可重试的写工具名（其余工具按只读注册，允许执行器重试）。
    write_tool_names: frozenset[str] = frozenset()
    #: 本域专属路由（宿主注入请求期依赖后挂载）；None = 无。
    router_contrib: RouterContrib | None = None
