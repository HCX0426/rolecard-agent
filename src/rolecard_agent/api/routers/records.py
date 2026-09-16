"""领域数据路由：手动补录 / 结构化抽取 / 指标修正 / 删除。

从 `main.py` 迁出的第 3 组（C1）。抽取端点带完整的三层校验编排（模型调用在
`domains/health/extract.py`），这里只负责 HTTP 编排、降级与审计。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import (
    DEFAULT_USER_ID,
    AppContext,
    get_actor,
    get_context,
)
from rolecard_agent.api.deps import (
    parsed_text_path as _parsed_text_path,
)
from rolecard_agent.core.ingestion import IngestionNotFound
from rolecard_agent.domains.health.extract import (
    ExtractConfigError,
    ExtractError,
    run_extraction,
    to_index_payload,
)
from rolecard_agent.domains.health.service import (
    HealthDataError,
    HealthInvalidReport,
    HealthNotFound,
)
from rolecard_agent.rag.ocr import select_ocr_backend
from rolecard_agent.rag.parser import IMAGE_EXTS, OcrUnavailable, ParseError, parse_document

router = APIRouter()


class IndexPatch(BaseModel):
    """Data-management correction for one indicator row. `exclude_unset` semantics:
    a field explicitly set to null means "clear it" (e.g. switching value -> text)."""

    index_value: float | None = None
    value_text: str | None = None
    unit: str | None = None
    ref_range: str | None = None
    is_verified: bool | None = None


class IndexCreate(BaseModel):
    """One indicator row in a manually created report (最小可用：名称 + 数值或文本)。

    其余（单位 / 参考区间 / 是否已人工校验）都可选；未勾选校验的照旧带
    【未经人工校验】标记 —— 手填不等于已核实。
    """

    index_name: str = Field(min_length=1, max_length=100)
    index_value: float | None = None
    value_text: str | None = None
    unit: str | None = None
    ref_range: str | None = None
    is_verified: bool = False


class ReportCreate(BaseModel):
    """手动补录一份报告。**主流程是"上传报告 / 图片让 AI 解析"，本接口是兜底入口**。

    最小可用契约（与 domain service 一致）：report_type + check_time 必填，至少一行指标，
    每行指标需 index_name 且 index_value / value_text 至少有一个。
    """

    report_type: str = Field(min_length=1, max_length=100)
    check_time: str = Field(min_length=1, max_length=32)
    institution: str | None = None
    note: str | None = None
    indices: list[IndexCreate] = Field(default_factory=list)


class ExtractRequest(BaseModel):
    """触发一次结构化抽取（上传成功后由前端自动调用，见 S5b）。"""

    task_id: str = Field(min_length=1, max_length=64)


def _latest_numeric_history(records: list[dict[str, object]]) -> dict[str, float]:
    """每个指标「最近一次」的数值 —— 给抽取的异常突变检查用（只做提示，不做阻断）。"""
    latest: dict[str, tuple[str, float]] = {}
    for report in records:
        day = str(report.get("check_time") or "")
        for row in report.get("indices") or []:  # type: ignore[union-attr]
            name = str((row or {}).get("index_name") or "").strip()
            value = (row or {}).get("index_value")
            if not name or value is None:
                continue
            try:
                numeric = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if name not in latest or day >= latest[name][0]:
                latest[name] = (day, numeric)
    return {name: value for name, (_, value) in latest.items()}


@router.get("/api/records")
def list_records(ctx: AppContext = Depends(get_context)) -> list[object]:
    """F2 数据管理视图：报告 + 完整指标行（归属演示用户）。"""
    return ctx.health.list_records(DEFAULT_USER_ID)


@router.post("/api/records/report", status_code=201)
def create_record_report(
    body: ReportCreate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """手动补录一份报告（最小可用）。**主流程仍是上传报告让 AI 解析**，这里是兜底入口。

    校验交给 domain service（类型/时间必填、每行指标需名称 + 数值或文本）；失败翻译成
    400 而不是 500 —— 这是用户输入错误，不是服务故障。写入审计。
    """
    try:
        report_id = ctx.health.create_report(
            user_id=DEFAULT_USER_ID,
            report_type=body.report_type,
            check_time=body.check_time,
            institution=body.institution,
            note=body.note,
            indices=[i.model_dump() for i in body.indices],
        )
    except HealthInvalidReport as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="create_report",
        target=report_id,
        detail={"report_type": body.report_type.strip(), "indices": len(body.indices)},
    )
    # 回整份报告（含生成的 index_id），前端可据此直接刷新列表。
    row = ctx.health.get_record(user_id=DEFAULT_USER_ID, report_id=report_id)
    return row if row is not None else {"report_id": report_id}


@router.post("/api/records/extract")
def extract_record(
    body: ExtractRequest,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """把已上传的报告抽成**结构化指标**（v2.3）：让 AI 不只能"读"原文，还能"算"数值。

    三层校验（确定性 / 原文锚定 / 第二模型交叉）在 domains/health/extract.py；
    **只有双方一致的项才写库**，其余作为 conflicts 返回，由人确认。
    铁律：一律 `is_verified=0`（没人核实过）；同一 ingestion task 已有报告则不重复写。
    """
    conn = ctx.conn
    try:
        task = ctx.ingestion.get(body.task_id)
    except IngestionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    already = conn.execute(
        "SELECT report_id FROM medical_report WHERE ingestion_task_id = ?",
        (body.task_id,),
    ).fetchone()
    if already is not None:
        return {"skipped": "already_extracted", "report_id": already["report_id"]}

    source_file = Path(str(task.get("source_file") or ""))
    parsed = _parsed_text_path(source_file)
    text = ""
    if parsed.exists():
        text = parsed.read_text(encoding="utf-8", errors="ignore")
    elif source_file.exists():
        # 兜底：本次改动之前上传的文件没有 .parsed.txt，现场再解析一次。
        try:
            is_image = source_file.suffix.lower() in IMAGE_EXTS
            ocr = select_ocr_backend(ctx.settings) if is_image else None
            text = parse_document(source_file, backend=ocr)
        except (ParseError, OcrUnavailable):
            text = ""
    if not text.strip():
        return {"skipped": "no_text", "detail": "没有可抽取的文本（未解析成功或内容为空）"}

    source = "ocr" if source_file.suffix.lower() in IMAGE_EXTS else "parsed"
    try:
        outcome = run_extraction(
            text=text,
            settings=ctx.app_state["effective"],  # 设置页改了后端也立刻生效
            source=source,
            known_history=_latest_numeric_history(ctx.health.list_records(DEFAULT_USER_ID)),
            tracer=ctx.tracer,
        )
    except ExtractConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExtractError as exc:
        ctx.roles.audit(
            actor=actor.id,
            action="extract_report_failed",
            target=body.task_id,
            detail={"task_id": body.task_id, "error": str(exc)[:300]},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if outcome is None:
        return {"skipped": "no_model", "detail": "没有可用的模型后端，无法抽取指标"}

    written: list[object] = []
    if outcome.agreed and outcome.check_time:
        try:
            report_id = ctx.health.create_report(
                user_id=DEFAULT_USER_ID,
                report_type=outcome.report_type or "未命名报告",
                check_time=outcome.check_time,
                institution=outcome.institution,
                note=f"AI 抽取（{outcome.mode} 校对）· 未经人工校验",
                indices=[to_index_payload(i, source=source) for i in outcome.agreed],
            )
        except HealthInvalidReport as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.ingestion.link_report(body.task_id, report_id)
        ctx.roles.audit(
            actor=actor.id,
            action="extract_report",
            target=report_id,
            detail={
                "task_id": body.task_id,
                "mode": outcome.mode,
                "written": len(outcome.agreed),
            },
        )
        written = [
            {
                "index_name": i.index_name.strip(),
                "index_value": i.index_value,
                "value_text": i.value_text,
                "unit": i.unit,
            }
            for i in outcome.agreed
        ]

    return {
        "mode": outcome.mode,
        "report_type": outcome.report_type,
        "check_time": outcome.check_time,
        "institution": outcome.institution,
        "written": written,
        "conflicts": [
            {
                "index_name": c.index_name,
                "reason": c.reason,
                "primary": (c.primary.model_dump() if c.primary else None),
                "verify": (c.verify.model_dump() if c.verify else None),
            }
            for c in outcome.conflicts
        ],
        "notes": list(outcome.notes),
    }


@router.patch("/api/records/index/{index_id}")
def patch_record_index(
    index_id: str,
    body: IndexPatch,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """F2：修正误录的指标值。变更写审计（US-3 的数据侧延伸）。"""
    changes = body.model_dump(exclude_unset=True)
    try:
        row = ctx.health.update_index(user_id=DEFAULT_USER_ID, index_id=index_id, changes=changes)
    except HealthNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HealthDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_index",
        target=index_id,
        detail={"fields": sorted(changes)},
    )
    return row


@router.delete("/api/records/index/{index_id}", status_code=204)
def remove_record_index(
    index_id: str, ctx: AppContext = Depends(get_context), actor: Actor = Depends(get_actor)
) -> None:
    try:
        ctx.health.delete_index(user_id=DEFAULT_USER_ID, index_id=index_id)
    except HealthNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ctx.roles.audit(actor=actor.id, action="delete_index", target=index_id)


@router.delete("/api/records/report/{report_id}", status_code=204)
def remove_record_report(
    report_id: str, ctx: AppContext = Depends(get_context), actor: Actor = Depends(get_actor)
) -> None:
    try:
        ctx.health.delete_report(user_id=DEFAULT_USER_ID, report_id=report_id)
    except HealthNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ctx.roles.audit(actor=actor.id, action="delete_report", target=report_id)


__all__ = ["router"]
