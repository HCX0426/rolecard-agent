"""管理面配置路由：角色卡 CRUD / 工具目录 / 插件启停。

从 `main.py` 迁出的第一组（C1）。这些都是**操作员动作**，不是 LLM 工具 —— 与
`switch_role` 的设计一致：让模型移动自己的权限边界等于自我授权。

审计（审查报告 M2）：C1 拆分时本文件是"纯搬迁"，于是角色卡 CRUD 成了管理面里唯一
**不写审计**的一组 —— 而 `tool_whitelist` / `knowledge_scopes` / `model_name` 恰恰就是
能力权限、可读文档范围与"数据去哪家模型"的定义，改它们等于改权限。
现在三处都写审计，`detail` 只记**变更了哪些字段**，不记字段内容（角色 prompt 可能很长）。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import (
    AppContext,
    get_actor,
    get_context,
)
from rolecard_agent.contracts.response_models import RoleTimelineView, ToolsCatalog
from rolecard_agent.core.memory import list_items as memory_list_items
from rolecard_agent.core.proactive import timeline
from rolecard_agent.features.card_import import (
    CardImportError,
    build_st_v2_card,
    load_card_bytes,
    parse_sillytavern_card,
)
from rolecard_agent.roles.models import RoleCard, RoleCardCreate, RoleCardUpdate
from rolecard_agent.roles.service import RoleNotFound

router = APIRouter()


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
    effective = ctx.model_settings.effective_settings(ctx.settings, user_id=ctx.current_user())
    if model_name not in effective.model_backends:
        known = ", ".join(sorted(effective.model_backends))
        raise HTTPException(status_code=400, detail=f"未知模型 {model_name!r}；可用：{known}")


@router.get(
    "/api/roles",
)
def list_roles(ctx: AppContext = Depends(get_context)) -> list[RoleCard]:
    """All role cards, built-in first.

    `response_model`（P1，2026-10-25）：本仓 107 个端点此前**零处**声明响应模型 ——
    于是 OpenAPI 里 193 个响应只有 82 个带 $ref，且那 82 个全是 `HTTPValidationError`
    （错误响应，不是成功响应）⇒ 前端没有任何"成功响应"的契约可引用，手写类型无法被
    机器校对。声明它之后前端可引用 `components["schemas"]["RoleCard"]`。

    零风险条件（本端点满足）：返回对象本身就是 `RoleCard`（`list_roles()` 的返回类型），
    字段集与模型**完全同源** —— FastAPI 的响应过滤不会切掉任何字段。
    """
    return ctx.role_cards.list_roles()


@router.post("/api/roles", status_code=201)
def create_role(
    data: RoleCardCreate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    _validate_role_model(ctx, data.model_name)
    created = ctx.role_cards.create(data)
    ctx.audit.log(
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


@router.post("/api/roles/import", status_code=201)
def import_role_card(
    file: UploadFile,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """导入 SillyTavern 角色卡（PNG 内嵌卡或裸 JSON），映射成自家的卡后创建。

    `features/card_import.py` 的 docstring 有完整的字段映射表与对账单口径 —— 路由只做
    三件事：大小闸（与上传同一档）、解析（解不开 400）、落库（重名走 RoleAlreadyExists
    → 409）。世界书/标签这些没有对应物的东西**不静默丢**：report 里计数，界面如实展示。
    """
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的。")
    if len(raw) > ctx.settings.max_upload_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"文件超过上限（{ctx.settings.max_upload_bytes} 字节）。",
        )
    try:
        result = parse_sillytavern_card(load_card_bytes(raw))
    except CardImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    created = ctx.role_cards.create(result.card)
    ctx.audit.log(
        actor=actor.id,
        action="import_role_card",
        target=created.role_id,
        detail={"source_name": result.report.get("source_name"), "spec": result.report.get("spec")},
    )
    return {"role": created.model_dump(mode="json"), "report": result.report}


@router.patch("/api/roles/{role_id}")
def update_role(
    role_id: str,
    data: RoleCardUpdate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    if "model_name" in data.model_fields_set:
        _validate_role_model(ctx, data.model_name)
    updated = ctx.role_cards.update(role_id, data)
    changed = sorted(data.model_fields_set)
    if changed:  # 空 PATCH 是"什么都没改"，不值得污染审计
        ctx.audit.log(
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
    ctx.role_cards.delete(role_id)
    ctx.audit.log(actor=actor.id, action="delete_role", target=role_id)


@router.get(
    "/api/roles/{role_id}/timeline",
    response_model=RoleTimelineView,
    response_model_exclude_unset=True,
)
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
    ctx.role_cards.get(role_id)
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


@router.get(
    "/api/tools/catalog",
    response_model=ToolsCatalog,
    response_model_exclude_unset=True,
)
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


__all__ = ["router"]


@router.get("/api/roles/{role_id}/export")
def export_role_card(
    role_id: str,
    include_memory: Annotated[str | None, Query(max_length=32)] = None,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> Response:
    """导出一张角色卡为 ST V2 JSON（设计稿 §3「隐私出口」的实现，R1031-13）。

    **记忆默认不随卡走**；`?include_memory=pinned_active` 显式勾选后只带
    pinned 且 active 的那几行（失效/被取代/未置顶的一律不出门）——借卡、分享、
    换机三种场景里，"她记得什么"永远是你亲手放行的。
    """
    if include_memory not in (None, "pinned_active"):
        raise HTTPException(
            status_code=400,
            detail="include_memory 只认 pinned_active（记忆出口只有这一档：置顶且未失效）。",
        )
    try:
        card = ctx.role_cards.get(role_id)
    except RoleNotFound as exc:
        raise HTTPException(status_code=404, detail=f"角色不存在：{role_id}") from exc
    memory_items: list[dict[str, Any]] | None = None
    if include_memory == "pinned_active":
        memory_items = [
            {
                "text": item["text"],
                "importance": item["importance"],
                "category": item.get("category"),
            }
            for item in memory_list_items(ctx.conn, user_id=ctx.current_user(), bucket=role_id)
            if item["pinned"]
        ]
    payload = build_st_v2_card(card, memory_items)
    ctx.audit.log(
        actor=actor.id,
        action="export_role_card",
        target=role_id,
        detail={"include_memory": include_memory == "pinned_active"},
    )
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{role_id}.card.v2.json"'},
    )
