"""命令执行审批路由（架构计划 C·§6.2）：待批命令的查看、批准、拒绝。

契约（与工具内部一致）：
  * **批准 = 执行一次**：`approve` 先把记录置 approved，再把执行丢进后台线程池
    （`run_approval_execution`），执行完成后回填结果（status=done）。批准这个 HTTP
    请求自身永远快 —— 命令可能在后台跑几十秒，前端 30s 超时不能等到它。
  * **拒绝 = 终态**：rejected 后同一命令不再自动重提审批（工具向模型转述拒绝原因）。
  * **批准必须持有凭据**：`decide` 要带这条记录下发的一次性 `decide_token`（读侧给的），
    缺 / 错 / 过期 = 403。挡的是"猜自增 id 就批"与浏览器里的跨源盲 POST（读不到响应就拿不到
    令牌），不是"登录"——单机形态本来就不登录。
  * 全部审批动作写 operator 审计（actor=操作员号，与工具内部 actor="agent" 的执行
    审计区分 —— 审批是管理动作，执行是 agent 动作）。
"""

from __future__ import annotations

import contextvars
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core.approvals import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalUnauthorised,
)
from rolecard_agent.core.tools import run as run_tools

router = APIRouter()

# 后台审批执行池是进程级资源（core/tools/run.py）；本路由只用它，不拥有它。关停走
# scripts/run_api.py 的退出路径（与 _CHAT_POOL 同生命周期）。


class DecideBody(BaseModel):
    """对一条待批命令拍板。只允许 pending 记录；批准会在后台立即执行一次。

    `token` 是这条记录下发的一次性能力凭证（读侧 `GET /api/approvals` 带的 `decide_token`）：
    持有它才批得动 —— 挡的是"猜一个自增 id 就批准"和浏览器里跨源的盲 POST。
    """

    decision: Annotated[str, Field(min_length=3, max_length=16)]
    token: Annotated[str | None, Field(default=None, max_length=64)] = None


@router.get("/api/approvals")
def list_approvals(
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ctx: AppContext = Depends(get_context),
) -> object:
    """审批记录列表（默认按 id 倒序）。`status=pending` = 待批队列（侧栏红点计数用）。"""
    rows = ctx.approvals.list_rows(status=status, limit=limit)
    pending = ctx.approvals.list_rows(status="pending", limit=limit)
    return {"items": rows, "pending": len(pending)}


@router.post("/api/approvals/{approval_id}/decide")
def decide_approval(
    approval_id: int,
    body: DecideBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """批准 / 拒绝一条命令审批。approve = 后台执行一次；reject = 终态。"""
    decision = (body.decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(
            status_code=400,
            detail=f"decision 只接受 approve / reject，收到：{body.decision!r}",
        )
    try:
        record = ctx.approvals.decide(approval_id, decision, token=body.token)
    except ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalAlreadyDecided as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ApprovalUnauthorised as exc:
        # 403 而不是 401：这里的缺口不是"没登录"（单机形态本来就不登录），而是"没持有
        # 这条待批下发的凭据"——身份可以匿名，凭据不行。
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    ctx.roles.audit(
        actor=actor.id,
        action="approve_command" if decision == "approve" else "reject_command",
        target=f"approval:{approval_id}",
        detail={"command": record["command"]},
    )
    if decision == "approve":
        # 后台执行：命令会真实落在任务目录里跑。连接是 ThreadLocalConnection，后台线程
        # 自带真实连接；settings 用 ctx.settings（当前生效配置，含运行环境覆盖）。
        #
        # **和 `api/chat.py` 那道缝同一个形状**（`R102-03` 的第二处）：`pool.submit` 也不传播
        # contextvar，不带上上下文，这条执行线程的库代际就永远不变，
        # `_current()` 那句"新请求先回滚上次残留事务"在它身上一次都不会触发。
        run_tools._APPROVAL_EXECUTOR.submit(
            contextvars.copy_context().run,
            run_tools.run_approval_execution,
            approval_id,
            settings=ctx.settings,
            conn=ctx.conn,
        )
    return record


__all__ = ["router"]