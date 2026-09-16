"""领域数据路由（通用）：非 health 域的结构化数据增删改查。

health 域有自己 richer 的报告/指标模型（`records.py` + `domains/health/service.py`），走
`/api/records` 那一套，不在此重复。本路由只负责**通用领域数据**——任意一个已注册/启用的域
都可以在「数据」页挂上自己的简单记录（标签 + 数值/文本 + 单位 + 备注），无需再写一套
领域服务。这让「数据」页真正多领域化：页签按 `/api/plugins` 遍历，health 用原视图，
其它域用这里的通用视图。

`health` 也被本路由接受（数据按 domain 列隔离），但前端对 health 仍走 `/api/records`，
故这里实际服务的是 health 之外的域。
"""

from __future__ import annotations

import re
import sqlite3
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import (
    DEFAULT_USER_ID,
    AppContext,
    get_actor,
    get_context,
)

router = APIRouter()

_DOMAIN_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


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


def _check_domain(domain: str) -> str:
    if not _DOMAIN_ID.match(domain):
        raise HTTPException(status_code=400, detail=f"非法域 id：{domain}")
    return domain


def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "domain": row["domain"],
        "label": row["label"],
        "value_text": row["value_text"],
        "value_num": row["value_num"],
        "unit": row["unit"],
        "note": row["note"],
        "created_at": str(row["created_at"]),
    }


@router.get("/api/domains/{domain}/records")
def list_domain_records(
    domain: str, ctx: AppContext = Depends(get_context)
) -> list[object]:
    """列出某域的通用记录（归属演示用户）。"""
    _check_domain(domain)
    rows = ctx.conn.execute(
        "SELECT id, domain, label, value_text, value_num, unit, note, created_at "
        "FROM domain_data WHERE domain = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
        (domain, DEFAULT_USER_ID),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/api/domains/{domain}/records", status_code=201)
def create_domain_record(
    domain: str,
    body: DomainRecordCreate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """新增一条通用领域记录（兜底录入入口）。"""
    _check_domain(domain)
    if body.value_text is None and body.value_num is None:
        raise HTTPException(status_code=400, detail="value_text 与 value_num 至少填一个")
    rid = uuid.uuid4().hex
    ctx.conn.execute(
        "INSERT INTO domain_data (id, domain, user_id, label, value_text, value_num, unit, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid,
            domain,
            DEFAULT_USER_ID,
            body.label.strip(),
            body.value_text,
            body.value_num,
            body.unit,
            body.note,
        ),
    )
    ctx.conn.commit()
    ctx.roles.audit(
        actor=actor.id,
        action="create_domain_record",
        target=f"{domain}/{rid}",
        detail={"label": body.label.strip()},
    )
    return _row_to_dict(
        ctx.conn.execute(
            "SELECT id, domain, label, value_text, value_num, unit, note, created_at "
            "FROM domain_data WHERE id = ?",
            (rid,),
        ).fetchone()
    )


@router.patch("/api/domains/{domain}/records/{record_id}")
def patch_domain_record(
    domain: str,
    record_id: str,
    body: DomainRecordPatch,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """修正一条通用记录（写入审计）。"""
    _check_domain(domain)
    row = ctx.conn.execute(
        "SELECT id FROM domain_data WHERE id = ? AND domain = ? AND user_id = ?",
        (record_id, domain, DEFAULT_USER_ID),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="没有任何要更新的字段")
    sets = ", ".join(f"{k} = ?" for k in changes)
    ctx.conn.execute(
        f"UPDATE domain_data SET {sets} WHERE id = ?",
        (*[changes[k] for k in changes], record_id),
    )
    ctx.conn.commit()
    ctx.roles.audit(
        actor=actor.id,
        action="update_domain_record",
        target=f"{domain}/{record_id}",
        detail={"fields": sorted(changes)},
    )
    return _row_to_dict(
        ctx.conn.execute(
            "SELECT id, domain, label, value_text, value_num, unit, note, created_at "
            "FROM domain_data WHERE id = ?",
            (record_id,),
        ).fetchone()
    )


@router.delete("/api/domains/{domain}/records/{record_id}", status_code=204)
def delete_domain_record(
    domain: str,
    record_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    row = ctx.conn.execute(
        "SELECT id FROM domain_data WHERE id = ? AND domain = ? AND user_id = ?",
        (record_id, domain, DEFAULT_USER_ID),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    ctx.conn.execute("DELETE FROM domain_data WHERE id = ?", (record_id,))
    ctx.conn.commit()
    ctx.roles.audit(actor=actor.id, action="delete_domain_record", target=f"{domain}/{record_id}")


__all__ = ["router"]
