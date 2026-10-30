"""任务目录（工作区）路由：查看 / 设置 / 清除工作区目录，以及目录树浏览。

按职责独立成组（vs 塞进 console.py）：工作区（旧称任务目录）是"角色碰电脑"的授权
边界，是 file1（架构计划 A）的落点，和审计/知识库等"本地资源管理"不是一类操作。

三条纪律贯穿：
  * 树浏览是**人**在角色卡页的工作区卡选目录用的（不是模型工具），只读、只列单层、限条目数；
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
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.api.errors import value_error_to_http
from rolecard_agent.contracts.response_models import WorkspaceDirView, WorkspaceTreeView
from rolecard_agent.core.common import workspace

router = APIRouter()


class WorkspaceDirBody(BaseModel):
    """设置任务目录的负载：路径（绝对或相对，相对按工作目录解析）+ 归属角色。

    `role_id` 空 = 设**全局默认**（所有没单独配的角色都跟着它）；非空 = 只改这一个角色。
    两者是两层：角色的优先（`core/common/workspace.resolve_task_dir` 里排的那一次）。
    """

    path: str = Field(min_length=1, max_length=1024)
    role_id: str = Field(default="", max_length=64)


def _payload(ctx: AppContext, role_id: str = "") -> dict[str, object]:
    """任务目录当前视图：生效路径 + 是否 DB 覆盖（未覆盖 = 跟随 env WORKSPACE_DIR）。

    `overridden` 的口径随作用域变：看的是**这一层**有没有覆盖 —— 全局层看全局那一份，
    角色层看这个角色自己那一份（角色配了就 true，与全局无关）。
    """
    role_dir = workspace.load_role_dir(ctx.conn, role_id) if role_id else None
    return {
        "path": str(workspace.resolve_task_dir(ctx.settings, ctx.conn, role_id)),
        "overridden": (
            role_dir is not None if role_id else workspace.load_task_dir(ctx.conn) is not None
        ),
    }


@router.get(
    "/api/workspace/dir",
    response_model=WorkspaceDirView,
    response_model_exclude_unset=True,
)
def get_workspace_dir(
    ctx: AppContext = Depends(get_context),
    role_id: Annotated[str, Query(max_length=64)] = "",
) -> object:
    """任务目录当前生效值（该角色自己那份 → 全局覆盖 → env）。"""
    return _payload(ctx, role_id)


@router.put("/api/workspace/dir")
def put_workspace_dir(
    body: WorkspaceDirBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """设置任务目录（规范化绝对路径落库，保存即对下一轮 fs 调用生效）。

    带 `role_id` = 只改这个角色的；不带 = 改全局默认。
    """
    try:
        if body.role_id:
            saved = workspace.save_role_dir(ctx.conn, body.role_id, body.path)
        else:
            saved = workspace.save_task_dir(ctx.conn, body.path)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    ctx.audit.log(
        actor=actor.id,
        # 角色级与全局级**分两个动作名**：审计流水要能一眼看出"改的是谁的那份"，
        # 而不是一个动作名配不同的 target 字段（查流水的人得自己记得约定）。
        action="set_role_task_dir" if body.role_id else "set_task_dir",
        target=body.role_id or "workspace",
        detail={"path": saved, "role_id": body.role_id},
    )
    return _payload(ctx, body.role_id)


@router.delete("/api/workspace/dir")
def delete_workspace_dir(
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
    role_id: Annotated[str, Query(max_length=64)] = "",
) -> object:
    """清除覆盖，回落下一层（角色层清掉 → 跟全局；全局层清掉 → 跟 env WORKSPACE_DIR）。"""
    # 两条分支各写各的 action 字面量：审计动作登记表的双向差分靠**字面量**扫源码
    # （`tests/unit/test_audit_trail.py`），用变量拼出来的动作名扫不到，
    # 那样"登记了却没人用"的红就永远不会触发 —— 这条测试也就空转了。
    if role_id:
        workspace.clear_role_dir(ctx.conn, role_id)
        ctx.audit.log(
            actor=actor.id,
            action="clear_role_task_dir",
            target=role_id,
            detail={"role_id": role_id},
        )
    else:
        workspace.clear_task_dir(ctx.conn)
        ctx.audit.log(
            actor=actor.id,
            action="clear_task_dir",
            target="workspace",
            detail={"role_id": role_id},
        )
    return _payload(ctx, role_id)


@router.get(
    "/api/workspace/tree",
    response_model=WorkspaceTreeView,
    response_model_exclude_unset=True,
)
def browse(path: Annotated[str, Query(max_length=1024)] = "") -> object:
    """列目录单层内容（设置页树选择器用）。空 path = 从用户主目录起步。"""
    try:
        return workspace.browse_tree(path)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc


__all__ = ["router"]
