"""领域数据路由（通用）：非 health 域的结构化数据增删改查。

health 域有自己 richer 的报告/指标模型（`records.py` + `domains/health/service.py`），走
`/api/records` 那一套，不在此重复。本路由只负责**通用领域数据**——任意一个已注册/启用的域
都可以在「数据」页挂上自己的简单记录（标签 + 数值/文本 + 单位 + 备注），无需再写一套
领域服务。这让「数据」页真正多领域化：页签按 `/api/plugins` 遍历，health 用原视图，
其它域用这里的通用视图。

H4：路由只做"参数映射 + 异常→HTTP 映射"，所有 SQL 在 `core/domain_data.py`。异常语义：
**找不到 = KeyError → 404**，**规则不允许 = ValueError → 400**。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import (
    AppContext,
    get_actor,
    get_context,
    value_error_to_http,
)
from rolecard_agent.core.domain_data import DomainDataService

router = APIRouter()


class DomainRecordCreate(BaseModel):
    """一条通用领域记录（最小可用：标签 + 数值或文本）。"""

    label: str = Field(min_length=1, max_length=100)
    value_text: str | None = None
    value_num: float | None = None
    unit: str | None = None
    note: str | None = None


class DomainRecordPatch(BaseModel):
    """通用记录的修正：每个字段都可单独更新（未给 = 不改）。"""

    label: str | None = None
    value_text: str | None = None
    value_num: float | None = None
    unit: str | None = None
    note: str | None = None


def _domain_service(ctx: AppContext) -> DomainDataService:
    return DomainDataService(ctx.conn)


@router.get("/api/domains/{domain}/records")
def list_domain_records(
    domain: str, ctx: AppContext = Depends(get_context)
) -> list[object]:
    """列出某域的通用记录（归属演示用户）。"""
    try:
        return _domain_service(ctx).list_records(domain, ctx.current_user())  # type: ignore[return-value]
    except ValueError as exc:
        raise value_error_to_http(exc) from exc


@router.post("/api/domains/{domain}/records", status_code=201)
def create_domain_record(
    domain: str,
    body: DomainRecordCreate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """新增一条通用领域记录（兜底录入入口）。"""
    try:
        row = _domain_service(ctx).create_record(
            domain,
            ctx.current_user(),
            label=body.label,
            value_text=body.value_text,
            value_num=body.value_num,
            unit=body.unit,
            note=body.note,
        )
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="create_domain_record",
        target=f"{domain}/{row['id']}",
        detail={"label": body.label.strip()},
    )
    return row


@router.patch("/api/domains/{domain}/records/{record_id}")
def patch_domain_record(
    domain: str,
    record_id: str,
    body: DomainRecordPatch,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """修正一条通用记录（写入审计）。"""
    changes = body.model_dump(exclude_unset=True)
    try:
        row = _domain_service(ctx).patch_record(domain, ctx.current_user(), record_id, changes)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="记录不存在") from None
    ctx.roles.audit(
        actor=actor.id,
        action="update_domain_record",
        target=f"{domain}/{record_id}",
        detail={"fields": sorted(changes)},
    )
    return row


@router.delete("/api/domains/{domain}/records/{record_id}", status_code=204)
def delete_domain_record(
    domain: str,
    record_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    try:
        _domain_service(ctx).delete_record(domain, ctx.current_user(), record_id)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="记录不存在") from None
    ctx.roles.audit(actor=actor.id, action="delete_domain_record", target=f"{domain}/{record_id}")


__all__ = ["router"]
