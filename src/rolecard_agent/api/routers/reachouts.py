"""角色主动开口（收件箱）路由：未读列表 + 标记已读。

架构计划 B：主动消息独立存 `agent_reachout`（不进 checkpoint），web 端轮询这里，
页面关着也能攒下来。主动本身是业务内容（不是 operator 操作），所以**不进 audit_log**；
它在哪进审计由 tracer 的 reachout_sent / reachout_failed 事件承载。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rolecard_agent.api.deps import AppContext, get_context
from rolecard_agent.core import reachout as svc
from rolecard_agent.core.file_watch import pending_count

router = APIRouter()


def _pending(ctx: AppContext) -> int:
    """挂起的目录变更条数（功能关闭 = 不查，恒 0；只读操作，不改基线）。"""
    return pending_count(ctx.conn) if ctx.settings.file_watch_enabled else 0


@router.get("/api/reachouts")
def get_reachouts(
    ctx: AppContext = Depends(get_context),
    role_id: str | None = Query(default=None, description="只返回该角色主动找过你的历史"),
) -> object:
    """收件箱：最近主动消息（含未读数）。前端铃铛红点 = unread。可按角色过滤。"""
    return svc.list_reachouts(ctx.conn, role_id=role_id, file_watch_pending=_pending(ctx))


@router.post("/api/reachouts/{reachout_id}/read")
def mark_read(reachout_id: int, ctx: AppContext = Depends(get_context)) -> object:
    """标记一条主动消息已读（点收件箱条目 / 去对话时调用）。"""
    if not svc.mark_read(ctx.conn, reachout_id):
        raise HTTPException(status_code=404, detail=f"主动消息不存在：{reachout_id}")
    return svc.list_reachouts(ctx.conn, file_watch_pending=_pending(ctx))


@router.post("/api/reachouts/read-by-role")
def mark_role_read(
    ctx: AppContext = Depends(get_context),
    role_id: str = Query(..., max_length=64),
) -> object:
    """把某角色攒下的未读一次标完（返回 `marked` 条数）。

    为什么按角色而不是逐条：点收件箱条目 = 跳进那条主动会话，进去看的是**整段历史**，
    所以那一摞未读同时就都算读过了。逐条发请求会在中途失败留下"半已读"，红点数字还骗人。
    """
    marked = svc.mark_role_read(ctx.conn, role_id)
    return {"marked": marked} | dict(
        svc.list_reachouts(ctx.conn, file_watch_pending=_pending(ctx))
    )


__all__ = ["router"]
