"""MCP server 接入的存储与合并（架构计划 C·§6.1 的 operator 路径；本轮仅后端）。

职责边界：
  * 把「运行时可增删改的 MCP server」（`mcp_server` 表）与「随部署烧进去的 env
    `MCP_SERVERS`」合并成加载用的 `McpServerConfig` 列表（同 id 时表覆盖 env，禁用行
    连 env 同名一并抑制）；
  * 校验（仅 http、URL 过 SSRF 公网边界、id 合法）与 headers 的只写不回读掩码。

**不做**的事：实际连接/加载工具在 `core/tools/mcp.py`（`load_mcp_tools` / `_fetch_one`）。
本模块纯数据，离线可测。连接失败隔离、审计包裹都在那一层。
"""

from __future__ import annotations

import json
import re
from typing import Any

from rolecard_agent.config import McpServerConfig
from rolecard_agent.core.tools.web import _host_is_public
from rolecard_agent.storage.db import SqlConnection

# id 会进工具名前缀 `{id}__`，所以禁下划线外的分隔符冲突：只允许字母数字与 -_.，
# 且不得含 `__`（与多 server 前缀分隔符撞车）。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_ID = 64
_MAX_NAME = 80


def validate_server(id: str, url: str) -> None:
    """id / url 合法性。非法 → ValueError（router 转可读 400）。

    仅允许 http(s) 且解析到**公网**地址（复用 web_fetch 的 SSRF 边界）：拒绝回环 /
    私网 / link-local / 保留段，否则"接入一个 MCP server"就变成对宿主内网的探测口。
    """
    if not id or len(id) > _MAX_ID or not _ID_RE.match(id) or "__" in id:
        raise ValueError(
            "id 需以字母或数字开头，仅含字母数字与 . _ -，长度 ≤ 64，且不含连续下划线。"
        )
    if not url or not url.strip():
        raise ValueError("URL 不能为空。")
    if not _host_is_public(url):
        raise ValueError("URL 必须是可解析到公网的 http(s) 端点（拒绝回环 / 私网 / 内网地址）。")


def create(
    conn: SqlConnection,
    *,
    id: str,
    display_name: str,
    url: str,
    headers: dict[str, str] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    validate_server(id, url)
    if get(conn, id) is not None:
        raise ValueError(f"MCP server {id!r} 已存在。")
    conn.execute(
        "INSERT INTO mcp_server (id, display_name, transport, url, headers_json, enabled) "
        "VALUES (?, ?, 'http', ?, ?, ?)",
        (id, display_name[:_MAX_NAME], url.strip(), json.dumps(headers or {}), 1 if enabled else 0),
    )
    conn.commit()
    row = get(conn, id)
    assert row is not None  # 刚插入
    return row


def update(
    conn: SqlConnection,
    id: str,
    *,
    display_name: str | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """局部更新。`headers=None` = 保留原值（只写不回读的 round-trip）；传 dict = 整体替换。"""
    row = get(conn, id)
    if row is None:
        raise KeyError(id)
    new_url = row["url"] if url is None else url.strip()
    if url is not None:
        validate_server(id, new_url)
    new_name = row["display_name"] if display_name is None else display_name[:_MAX_NAME]
    new_headers = row["headers_json"] if headers is None else json.dumps(headers)
    new_enabled = int(bool(row["enabled"])) if enabled is None else int(enabled)
    conn.execute(
        "UPDATE mcp_server SET display_name=?, url=?, headers_json=?, enabled=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (new_name, new_url, new_headers, new_enabled, id),
    )
    conn.commit()
    updated = get(conn, id)
    assert updated is not None
    return updated


def delete(conn: SqlConnection, id: str) -> None:
    if get(conn, id) is None:
        raise KeyError(id)
    conn.execute("DELETE FROM mcp_server WHERE id=?", (id,))
    conn.commit()


def get(conn: SqlConnection, id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM mcp_server WHERE id=?", (id,))
    r = cur.fetchone()
    return dict(r) if r is not None else None


def list_rows(conn: SqlConnection) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM mcp_server ORDER BY created_at, id")
    return [dict(r) for r in cur.fetchall()]


def row_to_cfg(row: dict[str, Any]) -> McpServerConfig:
    """表行 → 加载用的 McpServerConfig（transport 恒 http）。"""
    try:
        headers = json.loads(row.get("headers_json") or "{}")
    except json.JSONDecodeError:
        headers = {}
    return McpServerConfig(
        id=str(row["id"]),
        transport="http",
        url=str(row["url"]),
        headers={str(k): str(v) for k, v in headers.items()},
    )


def effective_servers(
    conn: SqlConnection, env_servers: list[McpServerConfig]
) -> list[McpServerConfig]:
    """生效集 = env ∪ 表 enabled 行；同 id 表覆盖 env，表内禁用行连 env 同名一并抑制。"""
    by_id: dict[str, McpServerConfig] = {c.id: c for c in env_servers}
    for row in list_rows(conn):
        cfg = row_to_cfg(row)
        if int(row.get("enabled", 1)):
            by_id[cfg.id] = cfg
        else:
            by_id.pop(cfg.id, None)
    return list(by_id.values())


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """GET 出参用：只暴露键名，值一律掩码（密钥永不出明文，仿 model_backend.api_key）。"""
    return {str(k): "****" for k in headers}


def to_api(row: dict[str, Any]) -> dict[str, Any]:
    """表行 → API 响应（headers 掩码）。"""
    try:
        headers = json.loads(row.get("headers_json") or "{}")
    except json.JSONDecodeError:
        headers = {}
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "transport": row["transport"],
        "url": row["url"],
        "headers": mask_headers(headers),
        "enabled": bool(row["enabled"]),
    }


__all__ = [
    "create",
    "delete",
    "effective_servers",
    "get",
    "list_rows",
    "mask_headers",
    "row_to_cfg",
    "to_api",
    "update",
    "validate_server",
]
