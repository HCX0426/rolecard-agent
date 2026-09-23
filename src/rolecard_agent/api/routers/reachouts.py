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


def _page(ctx: AppContext, *, role_id: str | None = None) -> dict[str, object]:
    """收件箱的响应 = 列表 + 折叠窗口（天）。

    `merge_days` 为什么在这里给而不是让前端去读「运行环境」：那条路由是 **operator 档**
    （`api/access.py`），让使用者面的收件箱去读它等于给自己开一个越权依赖；而折叠窗口
    本来就是"这一摞怎么显示"的一部分，跟着列表一起给最省。分组本身仍然只做在前端
    （后端不算第二份分组逻辑，见 docs/主动消息与记忆设计稿.md §1）。
    """
    return {
        **svc.list_reachouts(ctx.conn, role_id=role_id, file_watch_pending=_pending(ctx)),
        "merge_days": ctx.settings.reachout_merge_days,
    }


@router.get("/api/reachouts")
def get_reachouts(
    ctx: AppContext = Depends(get_context),
    role_id: str | None = Query(default=None, description="只返回该角色主动找过你的历史"),
) -> object:
    """收件箱：最近主动消息（含未读数与折叠窗口）。前端铃铛红点 = unread，可按角色过滤。"""
    return _page(ctx, role_id=role_id)


@router.post("/api/reachouts/{reachout_id}/read")
def mark_read(reachout_id: int, ctx: AppContext = Depends(get_context)) -> object:
    """标记一条主动消息已读（点收件箱条目 / 去对话时调用）。

    已经读过再标一次 = 幂等成功（收件箱列的是"未读 + 最近历史"，点历史条目是正常路径）；
    只有**记录真的不存在**才 404。
    """
    if not svc.mark_read(ctx.conn, reachout_id):
        raise HTTPException(status_code=404, detail=f"主动消息不存在：{reachout_id}")
    return _page(ctx)


@router.post("/api/reachouts/read-by-role")
def mark_role_read(
    ctx: AppContext = Depends(get_context),
    role_id: str = Query(..., max_length=64),
) -> object:
    """把某角色攒下的未读一次标完（返回 `marked` 条数）。

    为什么按角色而不是逐条：点收件箱条目 = 跳进那条主动会话，进去看的是**整段历史**，
    所以那一摞未读同时就都算读过了。逐条发请求会在中途失败留下"半已读"，红点数字还骗人。
    这一条正好也是折叠要的语义：展开后点任意一条，整摞一起变已读。
    """
    marked = svc.mark_role_read(ctx.conn, role_id)
    return {"marked": marked} | _page(ctx)


@router.delete("/api/reachouts/{reachout_id}")
def delete_reachout(reachout_id: int, ctx: AppContext = Depends(get_context)) -> object:
    """删收件箱里的**那一行投递记录**。404 = 没有这条。

    与"标已读"的分工要说清楚：已读只是不再算未读，行还在列表里；删是这行不再出现。
    **她主动说出口的那句话不跟着消失** —— 那句在主动会话的 checkpoint 里，留着它她才记得
    自己找过你（删会话本身才会在界面上抹掉那句话，那是另一条路：DELETE /api/session/…）。
    """
    if not svc.delete_reachout(ctx.conn, reachout_id):
        raise HTTPException(status_code=404, detail=f"主动消息不存在：{reachout_id}")
    return {"deleted": 1} | _page(ctx)


@router.delete("/api/reachouts")
def clear_inbox(
    ctx: AppContext = Depends(get_context),
    role_id: str | None = Query(default=None, max_length=64),
) -> object:
    """清空主动消息记录：给了 `role_id` 就只清那个角色，不给就全清（"整理抽屉"那一下）。

    同样只动投递记录，不动会话里的原话。返回删掉的条数，界面据此说"清掉了 N 条"。
    """
    deleted = svc.clear_inbox(ctx.conn, role_id) if role_id else svc.clear_all_inboxes(ctx.conn)
    return {"deleted": deleted} | _page(ctx, role_id=role_id)


__all__ = ["router"]
