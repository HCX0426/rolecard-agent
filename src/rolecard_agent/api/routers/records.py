"""领域数据路由：手动补录 / 结构化抽取 / 指标修正 / 删除。

从 `main.py` 迁出的第 3 组（C1）。抽取端点带完整的三层校验编排（模型调用在
`domains/health/extract.py`），这里只负责 HTTP 编排、降级与审计。
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import (
    AppContext,
    get_actor,
    get_context,
)
from rolecard_agent.base.observability import scrub_endpoints
from rolecard_agent.core.ingestion import IngestionNotFound
from rolecard_agent.core.upload_service import read_source_text, source_kind
from rolecard_agent.core.uploads import parsed_text_path as _parsed_text_path
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
    """每个指标「最近一次」的数值 —— 给抽取的异常突变检查用（只做提示，不做阻断）。

    `isinstance` 检查在这里不是防御性噪音：入参来自 SQLite 行转出来的 dict，
    "indices 一定是 list[dict]" 是调用方（`list_records`）的实现细节，不是类型系统
    能保证的事。既然要遍历它，就顺手把不可信的形状挡在外面。
    """
    latest: dict[str, tuple[str, float]] = {}
    for report in records:
        day = str(report.get("check_time") or "")
        rows = report.get("indices")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("index_name") or "").strip()
            value = row.get("index_value")
            if not name or value is None:
                continue
            try:
                numeric = float(str(value))
            except (TypeError, ValueError):
                continue
            if name not in latest or day >= latest[name][0]:
                latest[name] = (day, numeric)
    return {name: value for name, (_, value) in latest.items()}


@router.get("/api/records")
def list_records(
    ctx: AppContext = Depends(get_context),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """F2 数据管理视图（分页）：报告 + 完整指标行（归属演示用户）。

    为什么要分页：整表返回会让"补录几十份报告之后打开数据页"变成一次几十 KB 的响应，
    而界面一屏只看得下十几条（审查报告 P2）。切片目前发生在服务层：数据量到千级再改成
    SQL 层 `LIMIT/OFFSET`（那时候 `total` 也应改为 COUNT 查询）。
    """
    rows = ctx.health.list_records(ctx.current_user())
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
    }


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
            user_id=ctx.current_user(),
            report_type=body.report_type,
            check_time=body.check_time,
            institution=body.institution,
            note=body.note,
            indices=[i.model_dump() for i in body.indices],
        )
    except HealthInvalidReport as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.audit.log(
        actor=actor.id,
        action="create_report",
        target=report_id,
        detail={"report_type": body.report_type.strip(), "indices": len(body.indices)},
    )
    # 回整份报告（含生成的 index_id），前端可据此直接刷新列表。
    row = ctx.health.get_record(user_id=ctx.current_user(), report_id=report_id)
    return row if row is not None else {"report_id": report_id}


# 「同一 intake 任务不重复抽取」的单进程互斥（审查报告 P1-5）。
#
# 为什么是内存互斥而不是数据库唯一约束：域报告行上的 intake 外键是**有意的 1:N**
# （"one file can yield several reports"，见 `domains/health/schema.sql` 的注释），
# 加 UNIQUE 会把那条设计意图钉死。而抽取是"读-判断-写"，中间隔着几十秒的
# 模型调用，双击/并发必然双写 —— 用进程内互斥把这个窗口关掉即可。
# 本服务是单进程（`scripts/run_api.py` 不起 workers）；**若将来多 worker 部署，
# 这里要换成数据库级约束，那时也得先决定 1:N 是否还成立**。
_EXTRACT_INFLIGHT: set[str] = set()
_EXTRACT_INFLIGHT_LOCK = threading.Lock()


def _claim_extraction(task_id: str) -> bool:
    """抢占某个 intake 任务的抽取权。False = 已经有人在做。"""
    with _EXTRACT_INFLIGHT_LOCK:
        if task_id in _EXTRACT_INFLIGHT:
            return False
        _EXTRACT_INFLIGHT.add(task_id)
        return True


def _release_extraction(task_id: str) -> None:
    with _EXTRACT_INFLIGHT_LOCK:
        _EXTRACT_INFLIGHT.discard(task_id)


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

    # 并发/双击：第二个请求立刻收到明确答复，而不是陪着跑完几十秒再写第二份报告。
    # （前端 30s 就会 abort，所以这里**不能**阻塞等待第一个跑完。）
    if not _claim_extraction(body.task_id):
        return {
            "skipped": "in_progress",
            "detail": "这份文件正在抽取中，稍后刷新即可看到结果（不会重复写入）。",
        }
    try:
        return _extract_and_store(body=body, ctx=ctx, actor=actor)
    finally:
        _release_extraction(body.task_id)


def _extract_and_store(*, body: ExtractRequest, ctx: AppContext, actor: Actor) -> object:
    """抽取的**实际工作**：读文本 → 三层校验 → 交域写库（含 intake 关联）+ 写审计。

    与路由分开只是为了让上面那层互斥有个干净的 try/finally —— 原实现是一个 120 行的
    路由函数，互斥逻辑塞进去要整段重排缩进（审查报告 P2：路由过大）。
    """
    try:
        task = ctx.ingestion.get(body.task_id)
    except IngestionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # 幂等判据在**域**那一侧（`report_id_for_task`）：外键列住在域的表里，路由按约定既不
    # import 具体域、也不自己写它的表名（架构审计报告 P1-1）。
    already = ctx.health.report_id_for_task(user_id=ctx.current_user(), task_id=body.task_id)
    if already is not None:
        return {"skipped": "already_extracted", "report_id": already}

    source_file = Path(str(task.get("source_file") or ""))
    parsed = _parsed_text_path(source_file)
    text = ""
    if parsed.exists():
        text = parsed.read_text(encoding="utf-8", errors="ignore")
    elif source_file.exists():
        # 兜底：本次改动之前上传的文件没有 .parsed.txt，现场再解析一次。
        # OCR 没配 / 解析失败都算"没有文本"，下面那句 skipped 会如实说出来（read_source_text
        # 把这两种失败吞成 None —— 这条路不需要区分，跳过与 500 的分界在调用方）。
        # L3：OCR 后端选择收拢到 AppContext.ocr_candidates()（原与 sessions.py 重复）。
        backend = ctx.ocr_candidates() if source_kind(source_file) == "ocr" else None
        text = read_source_text(source_file, backend=backend) or ""
    if not text.strip():
        return {"skipped": "no_text", "detail": "没有可抽取的文本（未解析成功或内容为空）"}

    source = source_kind(source_file)
    try:
        outcome = run_extraction(
            text=text,
            settings=ctx.app_state["effective"],  # 设置页改了后端也立刻生效
            source=source,
            known_history=_latest_numeric_history(ctx.health.list_records(ctx.current_user())),
            tracer=ctx.tracer,
        )
    except ExtractConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExtractError as exc:
        # 审计详情里的异常文本会被 `/api/audit` 原样回给前端，而模型调用的异常经常带着
        # 内部 base_url —— 先脱敏再落库（审查报告 A5 / M11）。
        ctx.audit.log(
            actor=actor.id,
            action="extract_report_failed",
            target=body.task_id,
            detail={
                "task_id": body.task_id,
                "error": scrub_endpoints(str(exc))[:300],
            },
        )
        # 客户端同样不该看到内部 base_url：审计分支脱敏了，这里没理由不脱（P2）。
        raise HTTPException(status_code=502, detail=scrub_endpoints(str(exc))[:300]) from exc

    if outcome is None:
        return {"skipped": "no_model", "detail": "没有可用的模型，无法抽取指标"}

    written: list[object] = []
    if outcome.agreed and outcome.check_time:
        try:
            report_id = ctx.health.create_report(
                user_id=ctx.current_user(),
                report_type=outcome.report_type or "未命名报告",
                check_time=outcome.check_time,
                institution=outcome.institution,
                note=f"AI 抽取（{outcome.mode} 校对）· 未经人工校验",
                indices=[to_index_payload(i, source=source) for i in outcome.agreed],
                # intake 关联由域在插入这条报告时一起写（外键列在域的表里）—— 内核不再
                # 事后 UPDATE 域表，也就没有"已插入但尚未关联"的可被看到的中间态（P1-1）。
                ingestion_task_id=body.task_id,
            )
        except HealthInvalidReport as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.audit.log(
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
        row = ctx.health.update_index(
            user_id=ctx.current_user(), index_id=index_id, changes=changes
        )
    except HealthNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HealthDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.audit.log(
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
        ctx.health.delete_index(user_id=ctx.current_user(), index_id=index_id)
    except HealthNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ctx.audit.log(actor=actor.id, action="delete_index", target=index_id)


@router.delete("/api/records/report/{report_id}", status_code=204)
def remove_record_report(
    report_id: str, ctx: AppContext = Depends(get_context), actor: Actor = Depends(get_actor)
) -> None:
    """删除报告，**连它的检索索引一起清**（审查报告 P1-1）。

    顺序是刻意的：**先清向量，再删库行**。
      * 先清向量最坏情况 = 行还在、索引没了：报告仍列在页面上，只是搜不到 ——
        可见、可自愈、不泄漏；
      * 反过来最坏情况 = 行没了、向量还在：用户以为删掉的病历原文仍会被模型检索到
        并引用 —— 这正是要修的 bug。
    手工录入的报告没有 intake（`task_id=None`），跳过清理。清索引失败会直接抛错
    **且不删库行**，让用户重试，而不是留下"以为删了"的状态。
    """
    task_id = ctx.health.report_task_id(user_id=ctx.current_user(), report_id=report_id)
    removed = 0
    if task_id is not None:
        removed = ctx.knowledge.delete_source(ctx.health.knowledge_scope, task_id)
    try:
        ctx.health.delete_report(user_id=ctx.current_user(), report_id=report_id)
    except HealthNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ctx.audit.log(
        actor=actor.id,
        action="delete_report",
        target=report_id,
        detail={"removed_chunks": removed} if removed else None,
    )


__all__ = ["router"]
