"""管理面配置路由：角色卡 CRUD / 工具目录 / 插件启停。

从 `main.py` 迁出的第一组（C1）。这些都是**操作员动作**，不是 LLM 工具 —— 与
`switch_role` 的设计一致：让模型移动自己的权限边界等于自我授权。

审计（审查报告 M2）：C1 拆分时本文件是"纯搬迁"，于是角色卡 CRUD 成了管理面里唯一
**不写审计**的一组 —— 而 `tool_whitelist` / `knowledge_scopes` / `model_name` 恰恰就是
能力权限、可读文档范围与"数据去哪家模型"的定义，改它们等于改权限。
现在三处都写审计，`detail` 只记**变更了哪些字段**，不记字段内容（角色 prompt 可能很长）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import (
    AppContext,
    get_actor,
    get_context,
    plugin_error_to_http,
    role_error_to_http,
)
from rolecard_agent.core import timeline
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


def _validate_role_model(ctx: AppContext, model_name: str | None) -> None:
    """角色声明的模型后端必须是**这个人真跑得起来**的后端名。

    与 `PATCH /api/session` 的会话级校验同一纪律。角色侧此前不校验，拼错一个名字只能靠
    运行期的"降级 + 留痕"兜 —— 配置错误要当场大声，而不是等用户发现回答质量不对
    （审查报告 L3）。
    按 `current_user()` 而不是实例主人：图取凭据也是按这一轮的主人（`Runtime.effective_for`），
    两处差一个人就会放行一个"存得下、跑不动"的名字。
    """
    if not model_name:
        return
    effective = ctx.model_settings.effective_settings(
        ctx.settings, user_id=ctx.current_user()
    )
    if model_name not in effective.model_backends:
        known = ", ".join(sorted(effective.model_backends))
        raise HTTPException(status_code=400, detail=f"未知后端 {model_name!r}；可用：{known}")


@router.get("/api/roles")
def list_roles(ctx: AppContext = Depends(get_context)) -> list[object]:
    """All role cards, built-in first."""
    return [r.model_dump(mode="json") for r in ctx.role_cards.list_roles()]


@router.post("/api/roles", status_code=201)
def create_role(
    data: RoleCardCreate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    _validate_role_model(ctx, data.model_name)
    try:
        created = ctx.role_cards.create(data)
    except RoleAlreadyExists as exc:
        raise role_error_to_http(exc) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="create_role",
        target=created.role_id,
        detail={
            "role_name": created.role_name,
            # 能力权限与知识范围是权限边界，必须留痕；内容本身可能很长，只记条数。
            "tools": None if created.tool_whitelist is None else len(created.tool_whitelist),
            "scopes": len(created.knowledge_scopes or []),
            "model_name": created.model_name,
        },
    )
    return created.model_dump(mode="json")


@router.patch("/api/roles/{role_id}")
def update_role(
    role_id: str,
    data: RoleCardUpdate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    if "model_name" in data.model_fields_set:
        _validate_role_model(ctx, data.model_name)
    try:
        updated = ctx.role_cards.update(role_id, data)
    except RoleNotFound as exc:
        raise role_error_to_http(exc) from exc
    changed = sorted(data.model_fields_set)
    if changed:  # 空 PATCH 是"什么都没改"，不值得污染审计
        ctx.roles.audit(
            actor=actor.id,
            action="update_role",
            target=role_id,
            detail={"fields": changed},
        )
    return updated.model_dump(mode="json")


@router.delete("/api/roles/{role_id}", status_code=204)
def delete_role(
    role_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    try:
        ctx.role_cards.delete(role_id)
    except (RoleNotFound, BuiltinRoleProtected) as exc:
        raise role_error_to_http(exc) from exc
    ctx.roles.audit(actor=actor.id, action="delete_role", target=role_id)


@router.get("/api/roles/{role_id}/timeline")
def role_timeline(
    role_id: str,
    ctx: AppContext = Depends(get_context),
    limit: int = Query(50, ge=1, le=200),
    before: Annotated[str | None, Query(max_length=120)] = None,
    kinds: Annotated[
        str | None, Query(description="逗号分隔：reachout,memory,memory_correct,thread")
    ] = None,
) -> object:
    """事件簿（设计稿 §6）：把这个角色的主动开口、记下/更正的事实、会话锚点并成一条**只读**轴。

    纯读，所以不写审计 —— "谁翻了时间线"不是运维要关心的事，而这条轴的内容全是用户自己的对话
    与事实，写进审计流水反而是在给它们做第二份留存。
    """
    try:
        ctx.role_cards.get(role_id)
    except RoleNotFound as exc:
        raise role_error_to_http(exc) from exc
    wanted = tuple(k.strip() for k in kinds.split(",") if k.strip()) if kinds else None
    if bad := [k for k in wanted or () if k not in timeline.KINDS]:
        raise HTTPException(
            status_code=400, detail=f"未知的事件种类 {bad}；可用：{' / '.join(timeline.KINDS)}"
        )
    return timeline.build(
        ctx.conn,
        user_id=ctx.current_user(),
        role_id=role_id,
        limit=limit,
        before=before,
        kinds=wanted,
    )


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
def list_plugins(ctx: AppContext = Depends(get_context)) -> list[dict[str, object]]:
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


__all__ = ["router"]
