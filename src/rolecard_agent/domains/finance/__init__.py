"""finance 领域包：**数据型域**的自描述声明（无 LLM 工具、无查询服务、无种子角色）。

它存在的意义是让「多域」是真实的而不是 health-only：前端 `GenericDomainData` 走通用的
`domain_data` 通路按 `domain = 'finance'` 隔离地增删改查，不依赖任何富模型。从前这个域
在 `domains/registry.py` 里只是一个 `lambda: []` 的空壳工厂 —— 2026-10-04 审查快照的
域机制条目把这类「中心代码必须点名才能存在」的接缝收成 `SPEC` 之后，本目录自己声明
自己：新增同类的域只要补一个目录 + 一份 `SPEC` + 一个 `schema.sql`，registry / main /
roles 一行都不用改。

`schema.sql` 必须存在（`storage/db.schema_files()` 对每个注册域都要求它，缺失即启动
FileNotFoundError）；本域无私有表，文件里只有一段说明。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from rolecard_agent.domains.spec import DomainSpec, DomainToolContext

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

__all__ = ["SPEC"]


def _no_tools(_ctx: DomainToolContext) -> Sequence[BaseTool]:
    """数据型域不给 LLM 工具：数据由操作员在「数据」页直接管理（通用 CRUD）。"""
    return ()


SPEC: DomainSpec = DomainSpec(
    id="finance",
    tool_factory=_no_tools,
)
