"""管理面配置路由：角色卡 CRUD / 工具目录 / 插件启停。

从 `main.py` 迁出的第一组（C1）。这些都是**操作员动作**，不是 LLM 工具 —— 与
`switch_role` 的设计一致：让模型移动自己的权限边界等于自我授权。

本文件是**纯搬迁**：路径、状态码、响应体、异常映射都与迁移前一致。任何行为变更
（比如给角色 CRUD 补审计）都不在这里夹带，要改就单独提交 —— 否则出问题分不清是
重构引入的还是特性引入的。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import (
    AppContext,
    get_actor,
    get_context,
    plugin_error_to_http,
    role_error_to_http,
)
from rolecard_agent.core.plugins import PluginError
from rolecard_agent.roles.models import RoleCardCreate, RoleCardUpdate
from rolecard_agent.roles.service import (
    BuiltinRoleProtected,
    RoleAlreadyExists,
    RoleNotFound,
)

router = APIRouter()


class PluginToggle(BaseModel):
    """Plugin enable/disable request body."""

    enabled: bool


@router.get("/api/roles")
def list_roles(ctx: AppContext = Depends(get_context)) -> list[object]:
    """All role cards, built-in first."""
    return [r.model_dump(mode="json") for r in ctx.roles.list_roles()]


@router.post("/api/roles", status_code=201)
def create_role(
    data: RoleCardCreate, ctx: AppContext = Depends(get_context)
) -> object:
    try:
        created = ctx.roles.create(data)
    except RoleAlreadyExists as exc:
        raise role_error_to_http(exc) from exc
    return created.model_dump(mode="json")


@router.patch("/api/roles/{role_id}")
def update_role(
    role_id: str, data: RoleCardUpdate, ctx: AppContext = Depends(get_context)
) -> object:
    try:
        updated = ctx.roles.update(role_id, data)
    except RoleNotFound as exc:
        raise role_error_to_http(exc) from exc
    return updated.model_dump(mode="json")


@router.delete("/api/roles/{role_id}", status_code=204)
def delete_role(
    role_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    try:
        ctx.roles.delete(role_id)
    except (RoleNotFound, BuiltinRoleProtected) as exc:
        raise role_error_to_http(exc) from exc


@router.get("/api/tools/catalog")
def tools_catalog(ctx: AppContext = Depends(get_context)) -> object:
    """按领域分组的工具目录 —— 角色表单的白名单选择器与插件详情共用。

    工具名对模型有意义，对人是一串"方法名"；每个工具带 docstring 首行作为一句话说明，
    白名单才看得懂。内核工具（domain=None）单独成组。
    """
    kernel: list[dict[str, str]] = []
    domains: dict[str, list[dict[str, str]]] = {}
    for spec in ctx.registry.specs():
        entry = {
            "name": spec.name,
            "description": (spec.tool.description or "").split("\n")[0].strip(),
        }
        if spec.domain is None:
            kernel.append(entry)
        else:
            domains.setdefault(spec.domain, []).append(entry)
    return {"kernel": kernel, "domains": domains}


@router.get("/api/plugins")
def list_plugins(ctx: AppContext = Depends(get_context)) -> list[object]:
    return ctx.plugins.list_plugins()


@router.post("/api/plugins/{plugin_id}/toggle")
def toggle_plugin(
    plugin_id: str,
    body: PluginToggle,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    try:
        epoch = ctx.plugins.set_enabled(plugin_id, body.enabled, actor=actor.id)
    except PluginError as exc:
        raise plugin_error_to_http(exc) from exc
    return {"plugin_id": plugin_id, "enabled": body.enabled, "tool_epoch": epoch}


__all__ = ["router", "HTTPException"]
