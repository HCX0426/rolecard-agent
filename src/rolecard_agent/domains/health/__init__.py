"""health 领域包。

本包的**自描述入口是 `SPEC`**（见 `rolecard_agent.domains.spec`）：域名单、工具工厂、
查询服务工厂、出厂种子角色与写工具名单全部从它出，中心代码（`domains/registry`、
`api/main`、`core/bootstrap`、`roles/seed`）从此不点名 health —— 2026-10-04 审查快照
域机制条目要的「新增一个域零改中心代码」就是这一份声明。

字符串常量（`KNOWLEDGE_SCOPE` 等）住在叶模块 `names.py`，业务模块从那里 import，
本文件只做 re-export —— 见 `names.py` 的模块文档里那条半初始化环的说明。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from rolecard_agent.domains.health.names import (
    DOMAIN_TOOL_NAMES,
    KNOWLEDGE_SCOPE,
    WRITE_TOOL_NAMES,
)
from rolecard_agent.domains.health.seed import DOMAIN_SEED_ROLES
from rolecard_agent.domains.spec import DomainSpec, DomainToolContext, RouterDeps

if TYPE_CHECKING:
    from fastapi import APIRouter
    from langchain_core.tools import BaseTool

    from rolecard_agent.core.domain.domain_service import DomainQueryService
    from rolecard_agent.storage.db import SqlConnection

__all__ = [
    "DOMAIN_TOOL_NAMES",
    "DOMAIN_SEED_ROLES",
    "KNOWLEDGE_SCOPE",
    "SPEC",
    "WRITE_TOOL_NAMES",
]


def _make_tools(ctx: DomainToolContext) -> Sequence[BaseTool]:
    """本域工具工厂。**惰性 import**：包导入只付常量与角色卡的钱，工具链（langchain、
    上传编排）等到装配期才进来 —— `scripts/init_db.py` 这类只要域名单的入口不白付。"""
    from rolecard_agent.domains.health.service import HealthQueryService
    from rolecard_agent.domains.health.tools import make_domain_tools

    # 喂错域的查询服务是**启动期硬失败**，不是运行期的 AttributeError：装配点按
    # `spec.id` 取服务之后，这里是最后一道"这份接线对不对"的检查（原 `api/main.py`
    # 那句 isinstance 接线搬到了域自己身上 —— 域认识自己的服务类，装配点不认识域名）。
    if not isinstance(ctx.query, HealthQueryService):
        raise RuntimeError(
            "health 域的工具工厂只接受 HealthQueryService，"
            f"装配点喂进来的是 {type(ctx.query).__name__}"
        )
    return make_domain_tools(
        ctx.ingestion, ctx.query, current_user=ctx.current_user, upload_dir=ctx.upload_dir
    )


def _make_query_service(conn: SqlConnection) -> DomainQueryService:
    """本域查询服务工厂（连接由装配根在建图之前备好，见 `core.bootstrap`）。"""
    from rolecard_agent.domains.health.service import HealthQueryService

    return HealthQueryService(conn)


def _make_records_router(deps: RouterDeps) -> Sequence[APIRouter]:
    """本域的专属路由（`DomainSpec.router_contrib`）：记录补录 / 抽取 / 修正 / 删除。

    **惰性 import** 与 `_make_tools` 同一条理由：包导入只付常量与角色卡的钱，fastapi 与
    抽取编排等到宿主要挂路由时才进来（`scripts/init_db.py` 这类入口不白付）。
    """
    from rolecard_agent.domains.health.records import build_records_router

    return (build_records_router(deps),)


SPEC: DomainSpec = DomainSpec(
    id="health",
    tool_factory=_make_tools,
    query_service_factory=_make_query_service,
    seed_roles=DOMAIN_SEED_ROLES,
    write_tool_names=WRITE_TOOL_NAMES,
    router_contrib=_make_records_router,
)
