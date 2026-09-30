"""上行同步的端点（M7 第一批：角色卡 / 会话 / 记忆 / 主动消息）。

两侧共用这一对路由，因为"对面"其实跑的是同一份代码：

  * `GET  /api/sync/inventory` —— **对面**答的那一份清单：这个身份在这里有哪些条目，
    每条只给身份 + 内容指纹 + 一句预览（**不给载荷**：清单是给比对用的，不是数据出口）。
  * `POST /api/sync/plan` —— **本机**发起：带着对面的地址与凭据来，本机收集自己那份、
    拉对面的清单、比出一个计划（多少独有 / 多少相同 / 哪几条冲突）。这一步**一个字都不写**。
  * `POST /api/sync/apply` —— 人看完计划并裁决过冲突之后，本机把选中的载荷推给
    `POST /api/sync/import`（对面写入）。
  * `POST /api/sync/export` —— **批量载荷出口**（下行那一半的燃料）：对面从清单里挑好的
    idents，这里交出完整载荷。全仓第一扇批量数据出口，三道闩见函数 docstring。
  * `POST /api/sync/pull` —— 下行：把对面那份里本机没有的并回本机；写入走的还是
    `apply_import` 那段代码。没有"整份替换"档（它清的是本机）。
  * `POST /api/sync/reconcile` —— 登录对账：推+拉各一遍，自动策略只走无歧义的那半
    （`core.sync.auto_moves`），歧义的留在返回值里让人去向导里挑。**必须幂等**。

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

from rolecard_agent.api.auth import Actor, basic_header
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core import outbound
from rolecard_agent.core import sync as sync_lib
from rolecard_agent.core.model_settings import validate_base_url
from rolecard_agent.core.thread_locks import thread_write

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

    try:
        base = validate_base_url(target.base_url)
    except Exception as exc:  # noqa: BLE001 - ModelSettingsError 的文案已经能直接给人看
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not base:
        raise HTTPException(status_code=400, detail="要填对面那台程序的地址。")
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
                # 锁在 suppress **外面**（R28-03）：`update_state` 自己失败（比如这条线程
                # 从来没有检查点）照旧容忍，但"别人正持有这一会话的写锁"不能跟着被吞掉 ——
                # 那正是整段替换最不该无互斥插进去的时刻，吞了就是分叉同一个父检查点。
                with thread_write(tid), contextlib.suppress(Exception):
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


def _push(ctx: AppContext, *, base: str, user: str, secret: str, items: list[dict[str, Any]],
          clear_kinds: list[str] | None = None) -> dict[str, Any]:
    """把选好的载荷推给**对面**的 import 端点。凭据只在这次请求里活着。"""
    if not items and not clear_kinds:
        return {"written": {}, "skipped": {}, "errors": []}
    try:
        res = outbound.post(
            f"{base}/api/sync/import",
            json={"items": items, "clear_kinds": clear_kinds or [],
                  "confirm_replace": bool(clear_kinds)},
            headers={"Authorization": basic_header(user, secret)},
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
    out: dict[str, Any] = res.json()
    return out


def _fetch_remote_payloads(
    target: TargetBody, wanted: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """从对面取选中条目的**完整载荷**（下行那一半的燃料）。

    这是对面那台的批量数据出口 —— `inventory` 刻意不给载荷就是为了不开这扇门，
    所以这里带三道闩：只答"调用者自己名下的"（collect 本来就按身份过滤）、
    一次最多 `MAX_EXPORT_ITEMS` 条、每一次都进对面那台的审计（只记结构与条数）。
    """
    if not wanted:
        return []
    try:
        base = validate_base_url(target.base_url)
        res = outbound.post(
            f"{base}/api/sync/export",
            json={"idents": [{"kind": k, "ident": i} for k, i in wanted]},
            headers={"Authorization": basic_header(target.user, target.secret)},
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"从 {base} 取数据时断了：{exc}"
        ) from exc
    if res.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="对面拒了这组凭据（账号或密码不对）。")
    if res.status_code >= 400:
        raise HTTPException(
            status_code=502, detail=f"对面不肯交出数据（HTTP {res.status_code}）。"
        )
    body: dict[str, Any] = res.json()
    rows = body.get("items")
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="对面的回答里没有数据。")
    return rows


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
    base = validate_base_url(body.base_url) or ""
    written: dict[str, Any] = _push(
        ctx, base=base, user=body.user, secret=body.secret, items=items, clear_kinds=clear
    )
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


#: 一次导出最多交出多少条。这扇门对面只该在自己登录后为自己开：
#: 超了就是"这份数据大到不该一口气回家"，大声拒绝比静默截断好。
MAX_EXPORT_ITEMS = 1000


class ExportBody(BaseModel):
    """对面（或本机自己）来取载荷。`idents` 是它从清单里挑好的那几张。"""

    idents: list[dict[str, str]] = Field(default_factory=list)


@router.post("/api/sync/export")
def post_export(
    body: ExportBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, object]:
    """交出选中条目的**完整载荷** —— 下行那一半的燃料。

    这是全仓第一扇批量数据出口（`inventory` 刻意只给指纹不给载荷，就是为了不开这扇门）。
    三道闩：只答**调用者自己名下的**（collect 按身份过滤，别人的 id 在这里就是查无此条）、
    一次最多 `MAX_EXPORT_ITEMS` 条、每一次都进审计（只记结构与条数，**载荷一个字不落**）。
    """
    mine, _skipped = sync_lib.collect(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
    )
    if len(body.idents) > MAX_EXPORT_ITEMS:
        raise HTTPException(
            status_code=409,
            detail=f"一次最多取 {MAX_EXPORT_ITEMS} 条（要了 {len(body.idents)}），分几批来。",
        )
    wanted = {(str(r.get("kind")), str(r.get("ident"))) for r in body.idents}
    by_key = {(item.kind, item.ident): item for item in mine}
    out = [
        {"kind": item.kind, "ident": item.ident, "payload": item.payload}
        for key, item in by_key.items()
        if key in wanted
    ]
    # 审计只记结构：谁、取了几类各几条。载荷里是对话原文与记忆，一个字都不落。
    by_kind: dict[str, int] = {}
    for row in out:
        by_kind[str(row["kind"])] = by_kind.get(str(row["kind"]), 0) + 1
    ctx.roles.audit(
        actor=actor.id,
        action="sync_export",
        target="cloud-export",
        detail={"kinds": by_kind, "asked": len(wanted), "absent": len(wanted) - len(out)},
    )
    return {"items": out, "absent": len(wanted) - len(out)}


class PullBody(TargetBody):
    """下行：把**对面那份**里本机没有的并回来。

    三条与上行不对称的地方，都是方向本身决定的：
    * **没有"整份替换"这一档** —— 上行的 replace 清的是对面（你刚登录的那份），
      下行的 replace 清的是**本机**：她这几年的记忆所在。要清本机，走界面上的删除，
      一步一确认；同步器不背这个锅。
    * **没裁决的冲突默认"不拉"** —— 默认保护接收侧（这里是本机）。
      上行默认按对面的、下行默认留本机的，同一原则方向反着来。
    * 拉回来的东西走的是**同一段写入代码**（`apply_import`）：归属盖本机解析出的章，
      撞别人的 uid 照样什么都不写 —— 下行不因为方向反了就多一条特权。
    """

    kinds: list[str] = Field(default_factory=list)
    mode: str = "merge"
    resolutions: dict[str, str] = Field(default_factory=dict)


@router.post("/api/sync/pull")
def post_pull(
    body: PullBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, object]:
    """把对面那份里本机没有的并回**本机**。写入走的还是 `apply_import` 那段代码。"""
    if body.mode == "replace":
        raise HTTPException(
            status_code=400,
            detail="下行的「整份替换」这一档没有：它清的是本机这份。要清本机走界面上的删除。",
        )
    kinds = [k for k in (body.kinds or list(sync_lib.SYNC_KINDS)) if k in sync_lib.SYNC_KINDS]
    mine, skipped = sync_lib.collect(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
    )
    theirs, _ = _remote_inventory(body)
    result = sync_lib.plan(mine, theirs, skipped=skipped)
    wanted: list[tuple[str, str]] = [
        (str(row.get("kind")), str(row.get("ident")))
        for row in result.only_remote
        if str(row.get("kind")) in kinds
    ]
    both: set[str] = set()
    for conflict in result.conflicts:
        if conflict.kind not in kinds:
            continue
        choice = body.resolutions.get(f"{conflict.kind}:{conflict.ident}")
        if choice == "theirs":
            wanted.append((conflict.kind, conflict.ident))
        elif choice == "both" and conflict.kind == sync_lib.KIND_MEMORY:
            wanted.append((conflict.kind, conflict.ident))
            both.add(conflict.ident)
    rows = _fetch_remote_payloads(body, wanted)
    for row in rows:
        # 「两份都留」必须**带着章**走完最后一程：export 交出来的是对面那份的原样载荷，
        # 不盖 `keep_both` 的话，apply_import 会沿用同一个 uid 去 UPDATE —— 下行的
        # "都留"就悄悄变成了"按对面的来"（正是单测当场抓出来的那一笔）。
        if str(row.get("ident")) in both:
            row["payload"] = {**(row.get("payload") or {}), "keep_both": True}
    applied = sync_lib.apply_import(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
        items=rows,
    )
    ctx.roles.audit(
        actor=actor.id,
        action="sync_pull",
        target=f"{body.base_url} · {body.user}",
        detail={"kinds": kinds, "pulled": len(rows), "written": applied["written"],
                "errors": len(applied["errors"])},
    )
    return {"pulled": len(rows), "local": applied, "conflicts_left": len(result.conflicts)}


class ReconcileBody(TargetBody):
    """登录对账：**一次把两边的方向都走完**，用自动策略，人只在最后看一句读数。

    自动策略只走无歧义的那半（`core.sync.auto_moves`）：本机独有的推上去、对面独有的并回来、
    卡按新者胜；**记忆与会话的冲突原地不动**（"两份都留"不幂等，会每登录一次长出两条），
    留在返回值里让人去向导里挑 —— 冲突本该是可数的少数。
    推完再重新比对一次才拉：推上去的东西下一轮就是"两边相同"，不会自己跟自己打架。
    """

    kinds: list[str] = Field(default_factory=list)


@router.post("/api/sync/reconcile")
def post_reconcile(
    body: ReconcileBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, object]:
    """登录时那一次自动对账。幂等是它的命：跑两遍，第二遍必须什么都没动。"""
    kinds = [k for k in (body.kinds or list(sync_lib.SYNC_KINDS)) if k in sync_lib.SYNC_KINDS]

    def _plan() -> sync_lib.SyncPlan:
        mine, skipped = sync_lib.collect(
            ctx.conn,
            user_id=_identity(ctx),
            graph=ctx.app_state["graph"],
            settings=ctx.app_state["effective"],
        )
        theirs, _ = _remote_inventory(body)
        return sync_lib.plan(mine, theirs, skipped=skipped)

    base = validate_base_url(body.base_url) or ""
    first = _plan()
    push, _pull, _ = sync_lib.auto_moves(first)
    push = [item for item in push if item.kind in kinds]
    remote_out = _push(
        ctx,
        base=base,
        user=body.user,
        secret=body.secret,
        items=[{"kind": i.kind, "ident": i.ident, "payload": i.payload} for i in push],
    )
    second = _plan()
    _push2, pull, human = sync_lib.auto_moves(second)
    pull = [(k, i) for k, i in pull if k in kinds]
    rows = _fetch_remote_payloads(body, pull)
    local_out = sync_lib.apply_import(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
        items=rows,
    )
    ctx.roles.audit(
        actor=actor.id,
        action="sync_reconcile",
        target=f"{base} · {body.user}",
        detail={"pushed": len(push), "pulled": len(rows), "left": len(human),
                "written": {"remote": remote_out.get("written"), "local": local_out["written"]}},
    )
    return {
        "pushed": len(push),
        "pulled": len(rows),
        "written": {"remote": remote_out.get("written"), "local": local_out["written"]},
        "left_for_human": [
            {"kind": c.kind, "ident": c.ident,
             "mine": {"at": c.mine.at, "preview": c.mine.preview},
             "theirs": {"at": c.theirs.get("at", ""), "preview": c.theirs.get("preview", "")}}
            for c in human
        ],
    }
