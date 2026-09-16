"""控制台只读与管理路由：审计查询 / 知识库概览与重建 / 检索延迟指标。

从 `main.py` 迁出的第 4 组（C1）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context

router = APIRouter()


@router.get("/api/audit")
def list_audit(limit: int = 100, ctx: AppContext = Depends(get_context)) -> list[object]:
    """F3：审计只读端点 —— 让一直在写入的 audit_log 可被运营方查看。"""
    capped = max(1, min(limit, 500))
    rows = ctx.conn.execute(
        "SELECT ts, actor, action, target, detail_json FROM audit_log ORDER BY id DESC LIMIT ?",
        (capped,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/knowledge")
def list_knowledge(ctx: AppContext = Depends(get_context)) -> list[object]:
    """v2.1 知识库概览（设置页知识库管理）：作用域 → 分块数 + 来源 + 嵌入器。"""
    return ctx.knowledge.describe()


@router.delete("/api/knowledge/{scope}")
def reset_knowledge_scope(
    scope: str, ctx: AppContext = Depends(get_context), actor: Actor = Depends(get_actor)
) -> object:
    """清空一个知识作用域（删除其集合）—— 换嵌入后端后维度不兼容时的重建入口。

    破坏性管理动作，必须写审计（含清掉的分块数）。前端需二次确认后再调。
    """
    removed = ctx.knowledge.scope_count(scope)
    ctx.knowledge.reset_scope(scope)
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


__all__ = ["router"]
