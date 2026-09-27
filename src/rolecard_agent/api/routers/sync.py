"""上行同步的端点（M7 第一批：角色卡 / 会话 / 记忆 / 主动消息）。

两侧共用这一对路由，因为"对面"其实跑的是同一份代码：

  * `GET  /api/sync/inventory` —— **对面**答的那一份清单：这个身份在这里有哪些条目，
    每条只给身份 + 内容指纹 + 一句预览（**不给载荷**：清单是给比对用的，不是数据出口）。
  * `POST /api/sync/plan` —— **本机**发起：带着对面的地址与凭据来，本机收集自己那份、
    拉对面的清单、比出一个计划（多少独有 / 多少相同 / 哪几条冲突）。这一步**一个字都不写**。
  * `POST /api/sync/apply` —— 人看完计划并裁决过冲突之后，本机把选中的载荷推给
    `POST /api/sync/import`（对面写入）。

## 三条安全口径

1. **凭据只在请求里过一手**：它存在浏览器那一侧的 `dataSource`，本机后端不留副本；
   不进审计、不进日志、不进异常文本（与 api_key 同一条纪律）。审计只记结构：几类、几条、
   什么模式。
2. **写侧永远以"这次请求解析出来的人"为归属**：对面那台收到 import 时，写进去的行盖的是
   **它自己认出的那个身份**的章，不是载荷里带来的 `user_id`。所以 B 的凭据推不动 A 的数据，
   载荷里塞 `user_id` 也没用。
3. **对面的地址要过校验**（`validate_base_url`：只认 http/https，拒 `file://` 这类）。
   这与探测端点同一条 SSRF 口径 —— 差别只在于这里连凭据一起发出去了，所以那句提示里
   写清了"发给谁由你决定"。
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from typing import Any

import httpx  # 只用它的异常类型；请求一律走 core/outbound
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core import outbound
from rolecard_agent.core import sync as sync_lib
from rolecard_agent.core.model_settings import validate_base_url

router = APIRouter()

#: 一次比对最多看多少条。超了就是"这份数据大到不该走这条路"，大声拒绝比静默截断好。
MAX_ITEMS = 5000


def _identity(ctx: AppContext) -> str:
    return ctx.current_user()


@router.get("/api/sync/inventory")
def get_inventory(ctx: AppContext = Depends(get_context)) -> dict[str, object]:
    """这个身份在本机上有哪些可同步条目（身份 + 指纹 + 预览，不含载荷）。"""
    items, skipped = sync_lib.collect(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
    )
    if len(items) > MAX_ITEMS:
        raise HTTPException(status_code=409, detail=f"条目过多（{len(items)}），这一版不做分批比对")
    return {
        "items": [item.brief() for item in items],
        "skipped": skipped,
        "kinds": list(sync_lib.SYNC_KINDS),
    }


class TargetBody(BaseModel):
    """打到哪台、用哪个身份打。`secret` 只在本次请求内存活，绝不回显。"""

    base_url: str = Field(min_length=4)
    user: str = Field(min_length=1)
    secret: str = Field(default="")


def _remote_inventory(target: TargetBody) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from rolecard_agent.api.auth import basic_header

    try:
        base = validate_base_url(target.base_url)
    except Exception as exc:  # noqa: BLE001 - ModelSettingsError 的文案已经能直接给人看
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not base:
        raise HTTPException(status_code=400, detail="要填对面的服务地址。")
    try:
        res = outbound.get(
            f"{base}/api/sync/inventory",
            headers={"Authorization": basic_header(target.user, target.secret)},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"连不上 {base}（地址不通，或它没开跨域/同步端点）：{exc}",
        ) from exc
    if res.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="对面拒了这组凭据（账号或密码不对）。")
    if res.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"对面回 HTTP {res.status_code}，不像一个 rolecard 服务。",
        )
    try:
        body: dict[str, Any] = res.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="对面的回答不是 JSON。") from exc
    rows = body.get("items")
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="对面没给出可同步的清单。")
    skipped = body.get("skipped")
    return rows, [s for s in skipped if isinstance(s, dict)] if isinstance(skipped, list) else []


@router.post("/api/sync/plan")
def post_plan(
    target: TargetBody, ctx: AppContext = Depends(get_context)
) -> dict[str, object]:
    """比出计划，**不写任何东西**。界面上"下一步：看差异"就是这一发。"""
    mine, skipped = sync_lib.collect(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
    )
    theirs, _their_skipped = _remote_inventory(target)
    result = sync_lib.plan(mine, theirs, skipped=skipped)
    return {
        "counts": result.counts(),
        "by_kind": result.by_kind(),
        "remote_counts": result.remote_counts,
        "skipped": result.skipped,
        "conflicts": [
            {
                "kind": c.kind,
                "ident": c.ident,
                "mine": {"at": c.mine.at, "preview": c.mine.preview, "payload": c.mine.payload},
                "theirs": {"at": c.theirs.get("at", ""), "preview": c.theirs.get("preview", "")},
            }
            for c in result.conflicts
        ],
        # 只给身份，不给载荷：这一屏要列"会过去什么"，而载荷由 apply 那一步现收。
        "only_local": [item.brief() for item in result.only_local],
    }


class ImportBody(BaseModel):
    """对面写入的载荷。`items` 由发起方按用户的选择挑好，这里不再判冲突。

    `clear_kinds` 只服务"整份替换"那一档，且必须同时带 `confirm_replace=true`：
    删的是**这台机器上这个身份**的该类条目，删错了没有回头路，所以宁可让协议
    多一个显式的键，也不要"看起来只是个普通参数"。
    """

    items: list[dict[str, Any]] = Field(default_factory=list)
    clear_kinds: list[str] = Field(default_factory=list)
    confirm_replace: bool = False


def _clear_for_replace(
    conn: Any, *, user_id: str, kinds: list[str], graph: Any, settings: Any
) -> dict[str, int]:
    """整份替换的前半：把这个身份名下的该类条目清掉（**只清选了的类**）。"""
    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    from rolecard_agent.core.graph import build_graph_config

    cleared: dict[str, int] = {}
    if sync_lib.KIND_CARD in kinds:
        cur = conn.execute("DELETE FROM role_card WHERE user_id = ?", (user_id,))
        cleared[sync_lib.KIND_CARD] = max(cur.rowcount, 0)
    if sync_lib.KIND_MEMORY in kinds:
        cur = conn.execute("DELETE FROM role_memory_item WHERE user_id = ?", (user_id,))
        cleared[sync_lib.KIND_MEMORY] = max(cur.rowcount, 0)
    if sync_lib.KIND_REACHOUT in kinds:
        cur = conn.execute("DELETE FROM agent_reachout WHERE user_id = ?", (user_id,))
        cleared[sync_lib.KIND_REACHOUT] = max(cur.rowcount, 0)
    if sync_lib.KIND_THREAD in kinds:
        rows = conn.execute(
            "SELECT thread_id FROM session_thread WHERE user_id = ?", (user_id,)
        ).fetchall()
        for row in rows:
            tid = str(row["thread_id"])
            if graph is not None:
                with contextlib.suppress(Exception):
                    graph.update_state(
                        build_graph_config(tid, settings),
                        {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]},
                    )
        cur = conn.execute("DELETE FROM session_thread WHERE user_id = ?", (user_id,))
        cleared[sync_lib.KIND_THREAD] = max(cur.rowcount, 0)
    conn.commit()
    return cleared


@router.post("/api/sync/import")
def post_import(
    body: ImportBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, object]:
    """对面写入：把推过来的条目落到**这台机器上这个身份**名下。"""
    cleared: dict[str, int] = {}
    if body.clear_kinds:
        if not body.confirm_replace:
            raise HTTPException(
                status_code=400, detail="整份替换要显式确认（confirm_replace）才允许清空。"
            )
        cleared = _clear_for_replace(
            ctx.conn,
            user_id=_identity(ctx),
            kinds=body.clear_kinds,
            graph=ctx.app_state["graph"],
            settings=ctx.app_state["effective"],
        )
    result = sync_lib.apply_import(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
        items=body.items,
    )
    # 审计只记结构：几类各写了多少、清了多少。**绝不记载荷**（那里面是对话原文与记忆）。
    ctx.roles.audit(
        actor=actor.id,
        action="sync_import",
        target="cloud-import",
        detail={
            "written": result["written"],
            "skipped": result["skipped"],
            "cleared": cleared,
            "errors": len(result["errors"]),
        },
    )
    return {**result, "cleared": cleared}


MODES = ("merge", "append", "replace")


class ApplyBody(TargetBody):
    """发起方的"开始上行"。

    * `kinds` = 界面上勾了的同步项；**没勾的类一律不动对面**（用户 09-27：
      「可勾选同步项，不勾选的就用云端」）。
    * `mode`：`merge`（默认，逐条合并）/ `append`（只追加）/ `replace`（整份替换，
      会先清对面这一身份名下被选中的那几类）。
    * `resolutions`：冲突的裁决，键是 `kind:ident`，值 `mine` / `theirs` / `both`。
      **没给裁决的冲突默认按对面的**（`theirs`）—— 上行是"把本机这份推过去"，
      但没被明确挑过的东西不该顺手覆盖别人已经写好的。
    """

    kinds: list[str] = Field(default_factory=list)
    mode: str = "merge"
    resolutions: dict[str, str] = Field(default_factory=dict)


def _select(
    result: sync_lib.SyncPlan,
    mine: list[sync_lib.SyncItem],
    *,
    kinds: list[str],
    mode: str,
    resolutions: dict[str, str],
) -> tuple[list[sync_lib.SyncItem], list[str]]:
    """按勾选与裁决挑出要推的条目；返回 (条目, 要清空哪几类)。"""
    if mode == "replace":
        return [item for item in mine if item.kind in kinds], list(kinds)
    selected = [item for item in result.only_local if item.kind in kinds]
    for conflict in result.conflicts:
        if conflict.kind not in kinds:
            continue
        choice = resolutions.get(f"{conflict.kind}:{conflict.ident}", "theirs")
        if choice == "mine":
            selected.append(conflict.mine)
        elif choice == "both":
            # 「两份都留」只有记忆讲得通：卡与会话的身份就是那个 id，留两份 = 覆盖。
            # 所以这里给载荷盖一个 `keep_both` 的章，对面看到它就换新 uid 插一条，
            # 而不是沿用那个已经撞上别人的 uid（那等于把"都留"实现成"按本机的来"）。
            if conflict.kind != sync_lib.KIND_MEMORY:
                continue
            selected.append(
                replace(
                    conflict.mine,
                    payload={**conflict.mine.payload, "keep_both": True},
                )
            )
    return selected, []


@router.post("/api/sync/apply")
def post_apply(
    body: ApplyBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, object]:
    """本机挑好要推的东西，交给对面的导入端点写。凭据只在本次请求里活着。"""
    from rolecard_agent.api.auth import basic_header

    kinds = [k for k in (body.kinds or list(sync_lib.SYNC_KINDS)) if k in sync_lib.SYNC_KINDS]
    mode = body.mode if body.mode in MODES else "merge"
    mine, skipped = sync_lib.collect(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
    )
    theirs, _ = _remote_inventory(body)
    result = sync_lib.plan(mine, theirs, skipped=skipped)
    selected, clear = _select(result, mine, kinds=kinds, mode=mode, resolutions=body.resolutions)
    items = [
        {"kind": item.kind, "ident": item.ident, "payload": item.payload} for item in selected
    ]
    payload: dict[str, Any] = {
        "items": items,
        "clear_kinds": clear,
        "confirm_replace": mode == "replace",
    }
    base = validate_base_url(body.base_url) or ""
    try:
        res = outbound.post(
            f"{base}/api/sync/import",
            json=payload,
            headers={"Authorization": basic_header(body.user, body.secret)},
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"写到 {base} 时断了（对面可能重启了）：{exc}"
        ) from exc
    if res.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"对面拒绝写入（HTTP {res.status_code}）：{res.text[:180]}",
        )
    written: dict[str, Any] = res.json()
    remote_errors = written.get("errors")
    error_count = len(remote_errors) if isinstance(remote_errors, list) else 0
    # 审计只记**结构**：几类各推了多少、什么模式。对话原文与记忆文本一条都不落。
    ctx.roles.audit(
        actor=actor.id,
        action="sync_apply",
        target=f"{base} · {body.user}",
        detail={
            "mode": mode,
            "kinds": kinds,
            "sent": len(items),
            "written": written.get("written"),
            "errors": error_count,
        },
    )
    return {
        "sent": len(items),
        "mode": mode,
        "kinds": kinds,
        "remote": written,
        "conflicts_left": len(result.conflicts),
    }
