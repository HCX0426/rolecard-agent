"""MCP server 接入路由（架构计划 C·§6.1 的 operator 自助入口；本轮仅后端）。

四态：list / create / update / delete + 单服务器"测试连接"。所有写操作：
  * 经 `mcp_store` 校验（仅 http、URL 过 SSRF 公网边界、id 合法）；
  * 写审计（actor=操作者，detail **不含 headers 值**，只记 url/transport/enabled）；
  * 成功后 `ctx.rebuild_runtime()` 热重载 registry（新增/停用即时生效，无需重启）。

安全红线：这是 operator 动作，绝不注册成 LLM 可调用工具（自我扩权）。headers 里的密钥
只写不回读：GET/写响应一律掩码，PATCH 省略 headers = 保留原值、传 {{}} = 清空。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core import mcp_store

router = APIRouter()


class McpCreateBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=80)
    url: str
    headers: dict[str, str] | None = None
    enabled: bool = True


class McpPatchBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    url: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None


@router.get("/api/mcp/servers")
def list_mcp_servers(ctx: AppContext = Depends(get_context)) -> dict[str, Any]:
    """受管 MCP server 列表（headers 掩码）。env 侧高级配置不进本视图。"""
    rows = mcp_store.list_rows(ctx.conn)
    effective = mcp_store.effective_servers(ctx.conn, ctx.settings.mcp_servers)
    return {
        "servers": [mcp_store.to_api(r) for r in rows],
        "effective_count": len(effective),
    }


@router.post("/api/mcp/servers", status_code=201)
def create_mcp_server(
    body: McpCreateBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    try:
        row = mcp_store.create(
            ctx.conn,
            id=body.id,
            display_name=body.display_name,
            url=body.url,
            headers=body.headers,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="add_mcp_server",
        target=f"mcp:{row['id']}",
        detail={"url": row["url"], "transport": "http", "enabled": bool(row["enabled"])},
    )
    ctx.rebuild_runtime()
    return mcp_store.to_api(row)


@router.patch("/api/mcp/servers/{server_id}")
def patch_mcp_server(
    server_id: str,
    body: McpPatchBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    try:
        row = mcp_store.update(
            ctx.conn,
            server_id,
            display_name=body.display_name,
            url=body.url,
            headers=body.headers,
            enabled=body.enabled,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} 不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_mcp_server",
        target=f"mcp:{server_id}",
        detail={"url": row["url"], "enabled": bool(row["enabled"])},
    )
    ctx.rebuild_runtime()
    return mcp_store.to_api(row)


@router.delete("/api/mcp/servers/{server_id}", status_code=204)
def delete_mcp_server(
    server_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    try:
        mcp_store.delete(ctx.conn, server_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} 不存在。") from None
    ctx.roles.audit(
        actor=actor.id, action="remove_mcp_server", target=f"mcp:{server_id}", detail={}
    )
    ctx.rebuild_runtime()


@router.post("/api/mcp/servers/{server_id}/test")
def test_mcp_server(
    server_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    """真连一次列工具（不落库、不改配置）。失败即结果，返回 200 而非 500。"""
    row = mcp_store.get(ctx.conn, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} 不存在。")
    from rolecard_agent.core.tools.mcp import _fetch_one, _run_async

    cfg = mcp_store.row_to_cfg(row)
    result: dict[str, Any] = {"id": server_id, "ok": False, "tool_count": 0, "tools": []}
    try:
        tools = _run_async(_fetch_one(cfg))
        result["ok"] = True
        result["tool_count"] = len(tools)
        result["tools"] = [t.name for t in tools][:50]
    except Exception as exc:  # noqa: BLE001 - 连通性失败本身就是检测结果
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
    ctx.roles.audit(
        actor=actor.id,
        action="test_mcp_server",
        target=f"mcp:{server_id}",
        detail={"ok": result["ok"], "tool_count": result["tool_count"]},
    )
    return result


__all__ = ["router"]
