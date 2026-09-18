"""角色主动开口（收件箱）路由：未读列表 + 标记已读。

架构计划 B：主动消息独立存 `agent_reachout`（不进 checkpoint），web 端轮询这里，
页面关着也能攒下来。主动本身是业务内容（不是 operator 操作），所以**不进 audit_log**；
它在哪进审计由 tracer 的 reachout_sent / reachout_failed 事件承载。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from rolecard_agent.api.deps import AppContext, get_context
from rolecard_agent.core import reachout as svc

router = APIRouter()


@router.get("/api/reachouts")
def get_reachouts(ctx: AppContext = Depends(get_context)) -> object:
    """收件箱：最近主动消息（含未读数）。前端铃铛红点 = unread。"""
    return svc.list_reachouts(ctx.conn)


@router.post("/api/reachouts/{reachout_id}/read")
def mark_read(reachout_id: int, ctx: AppContext = Depends(get_context)) -> object:
    """标记一条主动消息已读（点收件箱条目 / 去对话时调用）。"""
    if not svc.mark_read(ctx.conn, reachout_id):
        raise HTTPException(status_code=404, detail=f"主动消息不存在：{reachout_id}")
    return svc.list_reachouts(ctx.conn)


__all__ = ["router"]