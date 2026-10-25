"""API 响应模型 —— **自动生成，勿手改**。

生成器：`scripts/tools/gen_response_models.py`（从运行时真实返回反推字段集）。
漂移判据：同一个脚本的 `--check`（CI 每次 push 比对现形状 vs 入库模型）。
手改这份文件 = 下一次生成被覆盖；要改字段集，改端点再重跑生成器。

配套：引用这些模型的端点必须带 `response_model_exclude_unset=True` ——
否则模型会把端点**没给**的缺省键补成 null（实测 pets 的 rows 多出 'listening': None，
响应与端点真返回不再逐字一致）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel


class RoleListItem(BaseModel):
    """自动生成：RoleListItem（Item 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    role_id: str | None = None
    user_id: str | None = None
    role_name: str | None = None
    system_prompt: str | None = None
    temperature: float | None = None
    model_name: Any | None = None
    tool_whitelist: list[str] | None = None
    exemplars: list[Any] | None = None
    knowledge_scopes: list[Any] | None = None
    description: str | None = None
    reachout_enabled: bool | None = None
    recall_enabled: bool | None = None
    time_pattern_enabled: bool | None = None
    affinity_enabled: bool | None = None
    file_watch_enabled: bool | None = None
    greeting_at: Any | None = None
    reachout_keep: int | None = None
    pet_pack: str | None = None
    accent: Any | None = None
    is_builtin: bool | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ServicesViewRoot(BaseModel):
    """自动生成：ServicesViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    services: list[ServicesViewServicesItem] | None = None


class ServicesViewServicesItem(BaseModel):
    """自动生成：ServicesViewServicesItem（servicesItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    key: str | None = None
    title: str | None = None
    hint: str | None = None
    effective: str | None = None
    effective_kind: str | None = None
    degraded_from: Any | None = None
    readonly: bool | None = None
    candidates: list[Any] | None = None


class PetsViewRoot(BaseModel):
    """自动生成：PetsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    packs: list[PetsViewPacksItem] | None = None
    skipped: list[Any] | None = None
    user_dir: str | None = None
    cubism_core: bool | None = None
    cubism_core_path: str | None = None


class PetsViewPacksItem(BaseModel):
    """自动生成：PetsViewPacksItem（packsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    id: str | None = None
    label: str | None = None
    kind: str | None = None
    rows: PetsViewPacksItem_rows | None = None
    motions: PetsViewPacksItem_motions | None = None
    source: str | None = None
    entry: str | None = None
    sheet_url: str | None = None


class PetsViewPacksItem_rows(BaseModel):
    """自动生成：PetsViewPacksItem_rows（packsItem_rows 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    listening: Any | None = None
    thinking: Any | None = None


class PetsViewPacksItem_motions(BaseModel):
    """自动生成：PetsViewPacksItem_motions（packsItem_motions 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    pass


class ToolsCatalogRoot(BaseModel):
    """自动生成：ToolsCatalogRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    kernel: list[ToolsCatalogKernelItem] | None = None
    domains: ToolsCatalogDomains | None = None


class ToolsCatalogKernelItem(BaseModel):
    """自动生成：ToolsCatalogKernelItem（kernelItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    name: str | None = None
    description: str | None = None


class ToolsCatalogDomains(BaseModel):
    """自动生成：ToolsCatalogDomains（domains 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    health: list[ToolsCatalogDomains_healthItem] | None = None


class ToolsCatalogDomains_healthItem(BaseModel):
    """自动生成：ToolsCatalogDomains_healthItem（domains_healthItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")
    name: Any | None = None
    description: Any | None = None


class RoleList(RootModel[list[RoleListItem]]):
    """/api/roles 的顶层形状（数组包一层只为给它一个名字）。"""


class ServicesView(RootModel[ServicesViewRoot]):
    """/api/services 的顶层形状（数组包一层只为给它一个名字）。"""


class PetsView(RootModel[PetsViewRoot]):
    """/api/pets 的顶层形状（数组包一层只为给它一个名字）。"""


class ToolsCatalog(RootModel[ToolsCatalogRoot]):
    """/api/tools/catalog 的顶层形状（数组包一层只为给它一个名字）。"""
