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
4. **目的地址要过出口策略**（`_guard_target` → `access.outbound_target_allowed`，`R102-58`）：
   这几条端点属**使用者档**（它是"搬自己的数据"这个日常动作），于是从前任意公网地址都收 ——
   多凭据部署里，持使用者凭据者可把该身份的记忆与会话全量推到自己控制的服务器。
   `AUTH_MODE` 开启后收窄为"回环目标或 operator 允许清单（`SYNC_ALLOWED_HOSTS`）"；
   off 档（单机单人）逐字不变。判据收在三个出站执行器的入口，只此一处。
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime
from typing import Any

import httpx  # 只用它的异常类型；请求一律走 core/outbound
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from rolecard_agent.api import access
from rolecard_agent.api.auth import ROLE_USER, Actor, basic_header
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core import outbound
from rolecard_agent.core import sync as sync_lib
from rolecard_agent.core.model_settings import validate_base_url
from rolecard_agent.core.paths import user_data_root
from rolecard_agent.core.thread_locks import thread_write
from rolecard_agent.storage.db import RETENTION_BACKUP_DIRNAME
from rolecard_agent.storage.threads import delete_thread_everywhere, delete_threads_for_user

router = APIRouter()

#: 一次比对最多看多少条。超了就是"这份数据大到不该走这条路"，大声拒绝比静默截断好。
MAX_ITEMS = 5000

#: 整份替换的删前备份，每张表各留几份（与 retention 的 `_trim_backups` 同一套哲学，
#: 但落在 `sync/` 子目录 —— 那边只 glob 顶层，互不清对方的账）。
_SYNC_BACKUP_KEEP = 5


def _identity(ctx: AppContext) -> str:
    return ctx.current_user()


def _guard_target(request: Request, ctx: AppContext, base_url: str) -> str:
    """解析并校验目标地址，再过**出站策略**（`R102-58`）。返回归一化后的 base。

    两件事分工写清：地址合不合法归 `validate_base_url`（scheme / 空值），"**谁有资格敲它**"
    归 `access.outbound_target_allowed`。三处出站执行器（`_remote_inventory` /
    `_fetch_remote_payloads` / `_push`）都在入口调它 —— 判据只此一处，而这个文件里所有
    发往对岸的请求都只能从这三个函数走。
    """
    try:
        base = validate_base_url(base_url)
    except Exception as exc:  # noqa: BLE001 - ModelSettingsError 的文案已经能直接给人看
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not base:
        raise HTTPException(status_code=400, detail="要填对面那台程序的地址。")
    actor: Actor | None = getattr(request.state, "actor", None)
    settings = ctx.settings
    allowed = access.outbound_target_allowed(
        base,
        # 来源与分族由认证中间件挂上（`api/main.py`）：XFF 只在那层按可信代理解析。
        ip=str(getattr(request.state, "origin", "") or ""),
        authenticated=actor is not None and not actor.is_anonymous,
        enforce=(settings.auth_mode or "off").strip().lower() != "off",
        role=actor.role if actor is not None else ROLE_USER,
        roles_in_effect=bool(getattr(request.state, "roles_in_effect", False)),
        allowlist=settings.sync_allowed_hosts,
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                f"这一档不许把数据推到 {base} —— 它不在允许清单里。"
                "让操作员在「设置 → 运行环境」把目的主机加进 SYNC_ALLOWED_HOSTS，"
                "或用操作员凭据 / 本机来源发起（见架构总览 §6）。"
            ),
        )
    return base


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


def _remote_inventory(
    target: TargetBody, *, request: Request, ctx: AppContext
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base = _guard_target(request, ctx, target.base_url)
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
    target: TargetBody,
    request: Request,
    ctx: AppContext = Depends(get_context),
) -> dict[str, object]:
    """比出计划，**不写任何东西**。界面上"下一步：看差异"就是这一发。"""
    mine, skipped = sync_lib.collect(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
    )
    theirs, _their_skipped = _remote_inventory(target, request=request, ctx=ctx)
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


def _b64_json(obj: Any) -> Any:
    """JSONL 序列化的兜底：blob 走 base64（可无损还原 msgpack），其余 str 化。"""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"__base64__": base64.b64encode(bytes(obj)).decode("ascii")}
    return str(obj)


def _write_jsonl(path: Any, rows: list[Any]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            record = dict(zip(row.keys(), tuple(row), strict=True))
            fh.write(json.dumps(record, ensure_ascii=False, default=_b64_json))
            fh.write("\n")
    return len(rows)


def _dump_before_clear(conn: Any, *, user_id: str, kinds: list[str]) -> dict[str, int]:
    """整份替换清空**之前**，把将要被删的行先落成 JSONL（2026-10-04 审查快照的数据丢失条目）。

    从前 `_clear_for_replace` 直接 DELETE 并 commit：清空落盘、导入逐条尽力，两段之间没有
    事务边界 —— 导入中断时对面就只剩"清了不导"。备份兜住最坏情况：`checkpoints` 的 blob
    是 msgpack，base64 原样落盘，配上同目录的 `session_thread` 行足以人工重放。顺序沿用
    retention 的纪律：先落盘、再删，中间崩掉的结果是"行还在库里 + 多一个备份文件"。

    落在 `retention-backups/sync/` 子目录：retention 的 `_trim_backups` 只 glob 顶层，
    两边互不清账；本函数按表各留最近 `_SYNC_BACKUP_KEEP` 份。
    """
    backup_dir = user_data_root() / RETENTION_BACKUP_DIRNAME / "sync"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dumped: dict[str, int] = {}
    tables = [
        table
        for table, kind in (
            ("role_card", sync_lib.KIND_CARD),
            ("role_memory_item", sync_lib.KIND_MEMORY),
            ("agent_reachout", sync_lib.KIND_REACHOUT),
            ("session_thread", sync_lib.KIND_THREAD),
        )
        if kind in kinds
    ]
    for table in tables:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE user_id = ?", (user_id,)  # noqa: S608
        ).fetchall()
        dumped[table] = _write_jsonl(backup_dir / f"{table}-{stamp}.jsonl", list(rows))
    if sync_lib.KIND_THREAD in kinds:
        tid_rows = conn.execute(
            "SELECT thread_id FROM session_thread WHERE user_id = ?", (user_id,)
        ).fetchall()
        tids = [str(r["thread_id"]) for r in tid_rows]
        # 会话的正文在 langgraph 的检查点表里（blob），载体行只是元数据 —— 两样都要。
        for table in ("checkpoints", "writes"):
            if not tids:
                dumped[table] = 0
                continue
            placeholders = ",".join("?" for _ in tids)
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE thread_id IN ({placeholders})",  # noqa: S608
                tids,
            ).fetchall()
            dumped[table] = _write_jsonl(backup_dir / f"{table}-{stamp}.jsonl", list(rows))
    by_table: dict[str, list[Any]] = {}
    for f in backup_dir.glob("*.jsonl"):
        by_table.setdefault(f.name.split("-", 1)[0], []).append(f)
    for files in by_table.values():
        for stale in sorted(files, key=lambda p: p.name, reverse=True)[_SYNC_BACKUP_KEEP:]:
            stale.unlink(missing_ok=True)
    return dumped


class ImportBody(BaseModel):
    """对面写入的载荷。`items` 由发起方按用户的选择挑好，这里不再判冲突。

    `clear_kinds` 只服务"整份替换"那一档，且必须同时带 `confirm_replace=true`：
    删的是**这台机器上这个身份**的该类条目，删错了没有回头路，所以宁可让协议
    多一个显式的键，也不要"看起来只是个普通参数"。
    """

    items: list[dict[str, Any]] = Field(default_factory=list)
    clear_kinds: list[str] = Field(default_factory=list)
    confirm_replace: bool = False


def _clear_for_replace(conn: Any, *, user_id: str, kinds: list[str], graph: Any) -> dict[str, int]:
    """整份替换的前半：把这个身份名下的该类条目清掉（**只清选了的类**）。

    会话走 `storage.db.delete_thread_everywhere` 级联**真删**（2026-10-02 拍板）——
    从前这里用 `update_state(REMOVE_ALL)` 留空壳 + 写死删两张表：`command_approval`
    恰好漏掉（孤儿审批挂在已删会话上），空壳检查点还被修剪器**永留**最新一条。
    锁在级联删外面（R28-03）：在飞轮次不该被从脚下抽走检查点。
    """
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
        removed = 0
        for row in rows:
            tid = str(row["thread_id"])
            if graph is not None:
                with thread_write(tid):
                    removed += delete_thread_everywhere(conn, tid)["session_thread"]
        # 收尾那一条只兜住"没有载体行的空壳"，所以两个数相加才是"清了几条会话"——
        # 从前只取收尾那一个数，逐条级联删跑过之后它恒为 0，于是报的是"清了 0 条"。
        removed += delete_threads_for_user(conn, user_id)
        cleared[sync_lib.KIND_THREAD] = removed
    conn.commit()
    return cleared


@router.post("/api/sync/import")
def post_import(
    body: ImportBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, object]:
    """对面写入：把推过来的条目落到**这台机器上这个身份**名下。

    入域校验在**任何清空/写入之前**（`R102-48`）：从前未知 kind 会在 apply_import 的
    排序里炸成 ValueError → 500，而 `_clear_for_replace` 的清空已经落盘 —— 两机版本
    偏差多出一个第五类就是"清了不导"。协议枚举只有 `sync_lib.SYNC_KINDS` 一份。
    """
    unknown_clear = sorted(set(body.clear_kinds) - set(sync_lib.SYNC_KINDS))
    unknown_item_kinds = sorted(
        {str(item.get("kind") or "") for item in body.items} - set(sync_lib.SYNC_KINDS)
    )
    if unknown_clear or unknown_item_kinds:
        raise HTTPException(
            status_code=400,
            detail=(
                "载荷里有协议之外的 kind，什么都没动："
                f"clear_kinds 多出 [{', '.join(unknown_clear) or '无'}]，"
                f"items 多出 [{', '.join(unknown_item_kinds) or '无'}]"
                f"（允许：{', '.join(sync_lib.SYNC_KINDS)}）。"
                "两台机器的版本可能不一致 —— 先把两边都升到同一版再同步。"
            ),
        )
    cleared: dict[str, int] = {}
    backed_up: dict[str, int] = {}
    if body.clear_kinds:
        if not body.confirm_replace:
            raise HTTPException(
                status_code=400, detail="整份替换要显式确认（confirm_replace）才允许清空。"
            )
        if not body.items:
            # 空载荷 + 清空指令 = "清了不导"（2026-10-04 审查快照的数据丢失条目）：
            # 发起方该类为空时选 replace，会把这台机器清成空、却什么都不进来 —— 而且从前
            # 连备份都没有。宁可让用户显式重试，也不接这种一眼就是事故形状的请求。
            raise HTTPException(
                status_code=400,
                detail=(
                    "载荷里一条数据都没有，却要求整份替换清空 —— 这一趟会把本机选中的类"
                    "清成空、什么都不导入（清了不导）。请先确认发起方真的有要推的条目；"
                    "若确实想清空这一类，请在本机逐条删除。"
                ),
            )
        backed_up = _dump_before_clear(ctx.conn, user_id=_identity(ctx), kinds=body.clear_kinds)
        cleared = _clear_for_replace(
            ctx.conn,
            user_id=_identity(ctx),
            kinds=body.clear_kinds,
            graph=ctx.app_state["graph"],
        )
    result = sync_lib.apply_import(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
        items=body.items,
    )
    # 审计只记结构：几类各写了多少、清了多少、删前备份了几行。
    # **绝不记载荷**（那里面是对话原文与记忆）。
    ctx.audit.log(
        actor=actor.id,
        action="sync_import",
        target="cloud-import",
        detail={
            "written": result["written"],
            "skipped": result["skipped"],
            "cleared": cleared,
            "backed_up": backed_up,
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


def _push(
    ctx: AppContext,
    *,
    request: Request,
    base: str,
    user: str,
    secret: str,
    items: list[dict[str, Any]],
    clear_kinds: list[str] | None = None,
) -> dict[str, Any]:
    """把选好的载荷推给**对面**的 import 端点。凭据只在这次请求里活着。"""
    if not items and not clear_kinds:
        return {"written": {}, "skipped": {}, "errors": []}
    if not items and clear_kinds:
        # 发起侧的同一道闸（2026-10-04 审查快照的数据丢失条目）：replace 档 `_select` 会
        # 无条件返回 clear，本机该类为空时就成了"清对面、不推任何东西"。在离用户最近
        # 的这一端拒绝，比让对面的 400 兜底多一句人话。
        raise HTTPException(
            status_code=400,
            detail=(
                "本机选中的条目为空、却带着整份替换的清空指令 —— 这会把对面清成空、"
                "什么都不推（清了不导）。请确认本机这一类真的有数据，或改用逐条删除。"
            ),
        )
    base = _guard_target(request, ctx, base)
    try:
        res = outbound.post(
            f"{base}/api/sync/import",
            json={
                "items": items,
                "clear_kinds": clear_kinds or [],
                "confirm_replace": bool(clear_kinds),
            },
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
    target: TargetBody, wanted: list[tuple[str, str]], *, request: Request, ctx: AppContext
) -> list[dict[str, Any]]:
    """从对面取选中条目的**完整载荷**（下行那一半的燃料）。

    这是对面那台的批量数据出口 —— `inventory` 刻意不给载荷就是为了不开这扇门，
    所以这里带三道闩：只答"调用者自己名下的"（collect 本来就按身份过滤）、
    一次最多 `MAX_EXPORT_ITEMS` 条、每一次都进对面那台的审计（只记结构与条数）。
    """
    if not wanted:
        return []
    base = _guard_target(request, ctx, target.base_url)
    try:
        res = outbound.post(
            f"{base}/api/sync/export",
            json={"idents": [{"kind": k, "ident": i} for k, i in wanted]},
            headers={"Authorization": basic_header(target.user, target.secret)},
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"从 {base} 取数据时断了：{exc}") from exc
    if res.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="对面拒了这组凭据（账号或密码不对）。")
    if res.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"对面不肯交出数据（HTTP {res.status_code}）。")
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
    request: Request,
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
    theirs, _ = _remote_inventory(body, request=request, ctx=ctx)
    result = sync_lib.plan(mine, theirs, skipped=skipped)
    selected, clear = _select(result, mine, kinds=kinds, mode=mode, resolutions=body.resolutions)
    items = [{"kind": item.kind, "ident": item.ident, "payload": item.payload} for item in selected]
    base = _guard_target(request, ctx, body.base_url)
    written: dict[str, Any] = _push(
        ctx,
        request=request,
        base=base,
        user=body.user,
        secret=body.secret,
        items=items,
        clear_kinds=clear,
    )
    remote_errors = written.get("errors")
    error_count = len(remote_errors) if isinstance(remote_errors, list) else 0
    # 审计只记**结构**：几类各推了多少、什么模式。对话原文与记忆文本一条都不落。
    ctx.audit.log(
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
    ctx.audit.log(
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
    request: Request,
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
    theirs, _ = _remote_inventory(body, request=request, ctx=ctx)
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
    rows = _fetch_remote_payloads(body, wanted, request=request, ctx=ctx)
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
    ctx.audit.log(
        actor=actor.id,
        action="sync_pull",
        target=f"{body.base_url} · {body.user}",
        detail={
            "kinds": kinds,
            "pulled": len(rows),
            "written": applied["written"],
            "errors": len(applied["errors"]),
        },
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
    request: Request,
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
        theirs, _ = _remote_inventory(body, request=request, ctx=ctx)
        return sync_lib.plan(mine, theirs, skipped=skipped)

    base = _guard_target(request, ctx, body.base_url)
    first = _plan()
    push, _pull, _ = sync_lib.auto_moves(first)
    push = [item for item in push if item.kind in kinds]
    remote_out = _push(
        ctx,
        request=request,
        base=base,
        user=body.user,
        secret=body.secret,
        items=[{"kind": i.kind, "ident": i.ident, "payload": i.payload} for i in push],
    )
    second = _plan()
    _push2, pull, human = sync_lib.auto_moves(second)
    pull = [(k, i) for k, i in pull if k in kinds]
    rows = _fetch_remote_payloads(body, pull, request=request, ctx=ctx)
    local_out = sync_lib.apply_import(
        ctx.conn,
        user_id=_identity(ctx),
        graph=ctx.app_state["graph"],
        settings=ctx.app_state["effective"],
        items=rows,
    )
    ctx.audit.log(
        actor=actor.id,
        action="sync_reconcile",
        target=f"{base} · {body.user}",
        detail={
            "pushed": len(push),
            "pulled": len(rows),
            "left": len(human),
            "written": {"remote": remote_out.get("written"), "local": local_out["written"]},
        },
    )
    return {
        "pushed": len(push),
        "pulled": len(rows),
        "written": {"remote": remote_out.get("written"), "local": local_out["written"]},
        # 对账是**登录时自动跑**的那一次，没有人盯着向导的两段读数 —— 两边写失败了几条
        # 必须自己站出来（`R102-24`：那一版这一格整个缺席，于是"卡一条都没落"与
        # "今天没有卡要动"在返回值上一字不差）。
        "errors": {
            "remote": remote_out.get("errors") or [],
            "local": local_out["errors"],
        },
        "left_for_human": [
            {
                "kind": c.kind,
                "ident": c.ident,
                "mine": {"at": c.mine.at, "preview": c.mine.preview},
                "theirs": {"at": c.theirs.get("at", ""), "preview": c.theirs.get("preview", "")},
            }
            for c in human
        ],
    }
