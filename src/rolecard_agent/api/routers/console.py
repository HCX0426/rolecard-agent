"""控制台只读与管理路由：审计查询 / 知识库概览与重建 / 检索延迟指标。

从 `main.py` 迁出的第 4 组（C1）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core.observability import scrub_endpoints
from rolecard_agent.core.uploads import referenced_paths, remove_orphans, scan_orphans

router = APIRouter()

# 审计表只增不减，一次全量倒序取 500 条既慢又没什么用（真正的需求是"往前翻"）。
# 游标分页用自增主键 `id`：它单调、有索引（PRIMARY KEY），比 `ts` 稳（同秒多条会打平）。
# 上限沿用修复前的 500，避免"顺手改了上限"变成一次隐性行为变更。
_AUDIT_PAGE_MAX = 500


@router.get("/api/audit")
def list_audit(
    limit: int = 100,
    before_id: int | None = Query(
        default=None, ge=1, description="只取 id 小于它的记录（翻页游标）"
    ),
    ctx: AppContext = Depends(get_context),
) -> list[dict[str, object]]:
    """F3：审计只读端点 —— 让一直在写入的 audit_log 可被运营方查看。

    `before_id` 是游标：把上一页最后一条的 `id` 传进来即可继续往前翻，避免深分页
    （`OFFSET` 在大表上会退化成全扫描）。
    """
    capped = max(1, min(limit, _AUDIT_PAGE_MAX))
    if before_id is None:
        rows = ctx.conn.execute(
            "SELECT id, ts, actor, action, target, detail_json FROM audit_log "
            "ORDER BY id DESC LIMIT ?",
            (capped,),
        ).fetchall()
    else:
        rows = ctx.conn.execute(
            "SELECT id, ts, actor, action, target, detail_json FROM audit_log "
            "WHERE id < ? ORDER BY id DESC LIMIT ?",
            (before_id, capped),
        ).fetchall()
    return [dict(r) | {"detail_json": scrub_endpoints(r["detail_json"])} for r in rows]


@router.get("/api/knowledge")
def list_knowledge(ctx: AppContext = Depends(get_context)) -> list[dict[str, object]]:
    """v2.1 知识库概览（设置页知识库管理）：作用域 → 分块数 + 来源 + 嵌入器。"""
    return ctx.knowledge.describe()


@router.get("/api/knowledge/scopes")
def list_knowledge_scopes(ctx: AppContext = Depends(get_context)) -> object:
    """角色卡「知识作用域」下拉的可选项来源：取实际已建的知识集合（RAG 真实作用域）。

    返回的是**真实存在的**作用域名（而不是现存角色声明过的并集），让下拉"所见即所得"。
    """
    scopes = [row.get("scope") for row in ctx.knowledge.describe() if row.get("scope")]
    return {"scopes": scopes}


@router.delete("/api/knowledge/{scope}")
def reset_knowledge_scope(
    scope: str, ctx: AppContext = Depends(get_context), actor: Actor = Depends(get_actor)
) -> object:
    """清空一个知识作用域（删除其集合）—— 换嵌入后端后维度不兼容时的重建入口。

    破坏性管理动作，必须写审计（含清掉的分块数）。前端需二次确认后再调。
    """
    removed = ctx.knowledge.scope_count(scope)
    try:
        ctx.knowledge.reset_scope(scope)
    except Exception as exc:  # noqa: BLE001 - 失败要可读，且**不能**写"已清空"的审计
        raise HTTPException(status_code=500, detail=f"重建作用域失败（{exc}）") from exc
    ctx.roles.audit(
        actor=actor.id,
        action="reset_knowledge_scope",
        target=scope,
        detail={"chunks_removed": removed},
    )
    return {"scope": scope, "removed_chunks": removed}


@router.get("/api/rag/metrics")
def rag_metrics(ctx: AppContext = Depends(get_context)) -> object:
    """v2.2 检索延迟细分：P50/P95/P99，按阶段拆（嵌入 / 向量检索 / 重排 / 合计）。

    基于最近 N 次检索的进程内滑动样本。回答"检索慢在哪一段、P95 多少、重排开没开"。
    进程重启样本清零（演示足够；生产应落时序库）。未发生检索时各分位为 null。
    """
    return ctx.knowledge.latency_p95()


# -- 上传目录的孤儿文件（盘点只读 / 回收需确认） ----------------------------------


def _orphan_report(ctx: AppContext) -> object:
    referenced = referenced_paths(ctx.ingestion.all_source_files())
    return scan_orphans(ctx.settings.upload_dir, referenced).as_dict()


@router.get("/api/uploads/orphans")
def list_orphan_uploads(ctx: AppContext = Depends(get_context)) -> object:
    """**只读盘点**：上传目录里没有被任何 ingestion 台账引用的文件。

    刻意与回收分成两个端点：一步到位的 `DELETE /cleanup` 很省事，但"点一下就永久删掉
    一批用户文件"不该是一个没有预览面的操作。前端先拉这个清单、二次确认，再调下面那个。
    """
    return _orphan_report(ctx)


@router.post("/api/uploads/cleanup")
def cleanup_orphan_uploads(
    ctx: AppContext = Depends(get_context), actor: Actor = Depends(get_actor)
) -> object:
    """回收孤儿上传文件（`.parsed.txt` 跟随主文件）。**写审计，含条数与释放字节数。**

    规则与安全边界见 `core/uploads.py` 的模块 docstring：只动上传目录内的普通文件、
    只删无台账引用的文件、删除前重新校验路径归属。
    """
    referenced = referenced_paths(ctx.ingestion.all_source_files())
    report = scan_orphans(ctx.settings.upload_dir, referenced)
    deleted, freed = remove_orphans(ctx.settings.upload_dir, report)
    ctx.roles.audit(
        actor=actor.id,
        action="cleanup_orphan_uploads",
        target=str(ctx.settings.upload_dir),
        detail={"deleted": deleted, "freed_bytes": freed, "scanned": report.scanned},
    )
    return {
        "deleted": deleted,
        "freed_bytes": freed,
        "scanned": report.scanned,
        "referenced": report.referenced,
    }


__all__ = ["router"]
