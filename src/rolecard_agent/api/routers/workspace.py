"""任务目录（工作区）路由：查看 / 设置 / 清除任务目录，以及设置页的目录树浏览。

按职责独立成组（vs 塞进 console.py）：任务目录是"角色碰电脑"的授权边界，是 file1
（架构计划 A）的落点，和审计/知识库等"本地资源管理"不是一类操作。

三条纪律贯穿：
  * 树浏览是**人**在设置页选目录用的（不是模型工具），只读、只列单层、限条目数；
  * 设置任务目录 = operator 动作，必须走审计（actor=操作员，与 fs 工具内部 actor="agent"
    的审计区分）；
  * 保存即生效：fs 工具根是每调用实时解析的（core/workspace.make_dir_resolver），
    所以这里不需要 rebuild_runtime。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context, value_error_to_http
from rolecard_agent.core import workspace

router = APIRouter()


class WorkspaceDirBody(BaseModel):
    """设置任务目录的负载：路径（绝对或相对，相对按工作目录解析）。"""

    path: str = Field(min_length=1, max_length=1024)


def _payload(ctx: AppContext) -> dict[str, object]:
    """任务目录当前视图：生效路径 + 是否 DB 覆盖（未覆盖 = 跟随 env WORKSPACE_DIR）。"""
    return {
        "path": str(workspace.resolve_task_dir(ctx.settings, ctx.conn)),
        "overridden": workspace.load_task_dir(ctx.conn) is not None,
    }


@router.get("/api/workspace/dir")
def get_workspace_dir(ctx: AppContext = Depends(get_context)) -> object:
    """任务目录当前生效值（DB 覆盖 or env）。"""
    return _payload(ctx)


@router.put("/api/workspace/dir")
def put_workspace_dir(
    body: WorkspaceDirBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """设置任务目录（规范化绝对路径落库，保存即对下一轮 fs 调用生效）。"""
    try:
        saved = workspace.save_task_dir(ctx.conn, body.path)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    ctx.audit.log(
        actor=actor.id,
        action="set_task_dir",
        target="workspace",
        detail={"path": saved},
    )
    return _payload(ctx)


@router.delete("/api/workspace/dir")
def delete_workspace_dir(
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """清除 DB 覆盖，回落 env WORKSPACE_DIR。"""
    workspace.clear_task_dir(ctx.conn)
    ctx.audit.log(actor=actor.id, action="clear_task_dir", target="workspace", detail={})
    return _payload(ctx)


@router.get("/api/workspace/tree")
def browse(path: Annotated[str, Query(max_length=1024)] = "") -> object:
    """列目录单层内容（设置页树选择器用）。空 path = 从用户主目录起步。"""
    try:
        return workspace.browse_tree(path)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc


__all__ = ["router"]
