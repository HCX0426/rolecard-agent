"""MCP 接入存储层（core/plugins/mcp_store.py）的单元测试（全离线，无网络）。

钉住四件事：CRUD 与局部更新 round-trip（headers 省略=保留、传{}=清空）、id/URL 校验
（尤其 SSRF：拒回环/私网/link-local）、env ∪ 表 的合并语义（表覆盖、禁用行抑制）、
headers 掩码（密钥永不出明文）。URL 用 IP 字面量避免 DNS 解析依赖，保证离线确定。
"""

from __future__ import annotations

import sqlite3

import pytest

from rolecard_agent.config import McpServerConfig
from rolecard_agent.core.plugins import mcp_store


def test_create_get_list_delete(conn: sqlite3.Connection) -> None:
    row = mcp_store.create(
        conn, id="srv1", display_name="示例", url="http://8.8.8.8/mcp", headers={"k": "v"}
    )
    assert row["id"] == "srv1" and row["transport"] == "http"
    assert mcp_store.get(conn, "srv1") is not None
    assert [r["id"] for r in mcp_store.list_rows(conn)] == ["srv1"]
    mcp_store.delete(conn, "srv1")
    assert mcp_store.get(conn, "srv1") is None
    with pytest.raises(KeyError):
        mcp_store.delete(conn, "srv1")


def test_create_rejects_duplicate_and_bad_id(conn: sqlite3.Connection) -> None:
    mcp_store.create(conn, id="dup", display_name="x", url="http://8.8.8.8/mcp")
    with pytest.raises(ValueError):
        mcp_store.create(conn, id="dup", display_name="y", url="http://8.8.8.8/mcp")
    for bad in ("", "a__b", "__x", "x" * 65, "带中文"):
        with pytest.raises(ValueError):
            mcp_store.create(conn, id=bad, display_name="z", url="http://8.8.8.8/mcp")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # 非 http
        "ftp://8.8.8.8/x",  # 非 http
        "",  # 空
        "http://",  # 无主机
        "not a url",  # 无 scheme
    ],
)
def test_validate_rejects_non_http_or_no_host(url: str) -> None:
    with pytest.raises(ValueError):
        mcp_store.validate_server("ok", url)


def test_validate_allows_private_and_loopback() -> None:
    # MCP URL 由可信 operator 主动填 → 本机/内网/公网均可（公网-only 只留给 web_fetch 的模型 URL）。
    mcp_store.validate_server("a", "http://127.0.0.1:3001/mcp")
    mcp_store.validate_server("b", "http://localhost:3001/mcp")
    mcp_store.validate_server("c", "http://192.168.1.10:8/mcp")
    mcp_store.validate_server("d", "https://8.8.8.8/mcp")


def test_update_roundtrip_headers_and_toggle(conn: sqlite3.Connection) -> None:
    mcp_store.create(
        conn, id="s", display_name="a", url="http://8.8.8.8/mcp", headers={"token": "abc"}
    )
    # 省略 headers = 保留；只改 display_name
    mcp_store.update(conn, "s", display_name="b")
    assert mcp_store.get(conn, "s")["display_name"] == "b"
    assert "abc" in mcp_store.get(conn, "s")["headers_json"]
    # 传 headers = 整体替换
    mcp_store.update(conn, "s", headers={"x": "1"})
    assert mcp_store.get(conn, "s")["headers_json"] == '{"x": "1"}'
    # 传 {} = 清空
    mcp_store.update(conn, "s", headers={})
    assert mcp_store.get(conn, "s")["headers_json"] == "{}"
    # 启停 + 不存在的 id
    assert mcp_store.update(conn, "s", enabled=False)["enabled"] == 0
    with pytest.raises(KeyError):
        mcp_store.update(conn, "ghost", enabled=True)


def test_update_rejects_non_http_url(conn: sqlite3.Connection) -> None:
    mcp_store.create(conn, id="s", display_name="a", url="http://8.8.8.8/mcp")
    with pytest.raises(ValueError):
        mcp_store.update(conn, "s", url="file:///etc/passwd")


def _env(*ids_url: tuple[str, str]) -> list[McpServerConfig]:
    return [McpServerConfig(id=i, transport="http", url=u) for i, u in ids_url]


def test_effective_merges_env_and_table(conn: sqlite3.Connection) -> None:
    # env 有 A(旧) 与 B；表里 A 覆盖（新 url）、C 新增、B 禁用 → 生效 = A新 + C
    mcp_store.create(conn, id="A", display_name="a", url="http://8.8.8.8/A2", enabled=True)
    mcp_store.create(conn, id="C", display_name="c", url="http://8.8.8.8/C", enabled=True)
    mcp_store.create(conn, id="B", display_name="b", url="http://8.8.8.8/B", enabled=False)
    env = _env(("A", "http://8.8.8.8/A1"), ("B", "http://8.8.8.8/B"))
    eff = {c.id: c for c in mcp_store.effective_servers(conn, env)}
    assert set(eff) == {"A", "C"}
    assert eff["A"].url == "http://8.8.8.8/A2"  # 表覆盖 env


def test_to_api_masks_headers_values(conn: sqlite3.Connection) -> None:
    row = mcp_store.create(
        conn, id="m", display_name="m", url="http://8.8.8.8/m", headers={"auth": "s3cr3t"}
    )
    api = mcp_store.to_api(row)
    assert api["headers"] == {"auth": "****"}
    assert "s3cr3t" not in str(api)
