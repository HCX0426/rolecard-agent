"""域自描述契约：`DomainSpec` —— 「新增一个域要改几处中心代码」这个问题的答案。

2026-10-04 审查快照的域机制条目给当时的现实记的账是：新增一域要动 **≥6 处**中心代码
（`domains/registry.py` 的 DOMAINS 元组与 factories 字典、`api/main.py` 的 isinstance 接线、
`api/routers/records.py` 的抽取器分派、`roles/seed.py` 的工具名与知识作用域），于是
finance 域至今只能做一个 `lambda: []` 的空壳。本模块把「一个域是什么」收成**一份声明**，
中心代码只认这一份声明、不认域的名字：

  * `domains/registry.py` 按目录枚举各包导出的 `SPEC`（域名单、工具工厂、写工具分流）；
  * `api/main.py` 用 `build_query_services` / `domain_seed_roles` 接线，不再 import 具体域；
  * `core/bootstrap.py` 只收宿主注入的映射与种子角色（分层要求：内核不认识任何域名）。

**金标准**（快照 P1-5 的验收句）：新增一个 demo 域（models/service/tools/schema 四件套 +
一个导出 `SPEC` 的目录）不改任何 registry / main / roles 代码，即可启动、建表、出工具。

本模块只定义形状，不 import 任何具体域 —— 它在 `domains` 包的最底层，被每个域反向依赖。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from rolecard_agent.core.domain_service import DomainQueryService
    from rolecard_agent.core.ingestion import IngestionService
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


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """一个域的**全部**自描述内容。装配点只读这份声明，从不点域名。

    字段即快照 P1-5 的那张清单（router_contrib 一族留给域路由那一步）：
    `id` / `tool_factory` / `query_service_factory` / `seed_roles` / `write_tool_names`。

    `write_tool_names` 声明的是**不允许执行器重试**的写工具（写台账的工具重试一次就多一条
    记录，审查报告 M10）：分流注册的判据住在域自己身上，装配点只照单执行 —— 谁能安全重试
    是工具的性质，不是宿主的知识。
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
