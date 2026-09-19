"""MCP 接入端点测试（api/routers/mcp.py）。

钉住安全面：**仅 http + SSRF 边界**（私网 URL 被拒）、**headers 只写不回读**（掩码、
PATCH 省略=保留）、每次写操作都进审计、启停触发 registry 热重载。**全离线**：patch 掉
真实网络加载（load_mcp_tools / _fetch_one），断言与宿主机是否装 mcp 依赖无关。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(
        "MODEL_BACKENDS",
        '{"local": {"model": "qwen3-vl:8b", "provider": "ollama", "base_url": "http://127.0.0.1:9"}}',
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    # 离线：rebuild 时不去真连外部 MCP server。
    import rolecard_agent.core.tools.mcp as _mcp

    monkeypatch.setattr(_mcp, "load_mcp_tools", lambda servers, conn=None: [])
    return TestClient(create_app(sqlite_path=tmp_path / "mcp.db"))


def _create(client: TestClient, **over: object) -> object:
    body = {"id": "srv1", "display_name": "示例", "url": "http://8.8.8.8/mcp"}
    body.update(over)
    return client.post("/api/mcp/servers", json=body)


def test_create_lists_with_masked_headers(client: TestClient) -> None:
    r = _create(client, headers={"Authorization": "supersecretvalue"})
    assert r.status_code == 201, r.text
    srv = r.json()
    assert srv["transport"] == "http" and srv["id"] == "srv1"
    assert srv["headers"] == {"Authorization": "****"}
    assert "supersecretvalue" not in str(srv)  # 明文密钥绝不出现在响应
    body = client.get("/api/mcp/servers").json()
    assert [s["id"] for s in body["servers"]] == ["srv1"]
    assert body["effective_count"] == 1


def test_create_rejects_non_http_url(client: TestClient) -> None:
    r = _create(client, url="file:///etc/passwd")
    assert r.status_code == 400
    assert "http" in r.json()["detail"]


def test_create_allows_loopback_url(client: TestClient) -> None:
    # operator 主动填的本机 MCP 端点应被接受（公网-only 只留给模型给的 URL）。
    r = _create(client, id="local", url="http://127.0.0.1:3001/mcp")
    assert r.status_code == 201
    assert r.json()["url"] == "http://127.0.0.1:3001/mcp"


def test_create_rejects_duplicate(client: TestClient) -> None:
    assert _create(client).status_code == 201
    assert _create(client, display_name="又一条").status_code == 400


def test_patch_preserves_headers_when_omitted(client: TestClient) -> None:
    assert _create(client, headers={"k": "***"}).status_code == 201
    r = client.patch("/api/mcp/servers/srv1", json={"display_name": "改名", "enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert r.json()["display_name"] == "改名"
    assert r.json()["headers"] == {"k": "****"}  # 省略 headers → 原密钥仍在（掩码回显）
    assert "secret" not in str(r.json())


def test_patch_and_delete_missing_is_404(client: TestClient) -> None:
    assert client.patch("/api/mcp/servers/ghost", json={"enabled": True}).status_code == 404
    assert client.delete("/api/mcp/servers/ghost").status_code == 404


def test_delete_removes(client: TestClient) -> None:
    assert _create(client).status_code == 201
    assert client.delete("/api/mcp/servers/srv1").status_code == 204
    assert client.get("/api/mcp/servers").json()["servers"] == []


def test_writes_are_audited_without_secrets(client: TestClient) -> None:
    assert _create(client, headers={"Authorization": "***"}).status_code == 201
    client.patch("/api/mcp/servers/srv1", json={"enabled": False})
    client.delete("/api/mcp/servers/srv1")
    audit = client.get("/api/audit").json()  # 直接返回行列表
    actions = {row["action"] for row in audit}
    assert {"add_mcp_server", "update_mcp_server", "remove_mcp_server"} <= actions
    assert "sk-9999" not in str(audit)  # 审计也不落密钥


def test_test_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _create(client).status_code == 201

    class _T:
        name = "do_thing"

    async def _ok(cfg):  # noqa: ARG001
        return [_T(), _T()]

    monkeypatch.setattr("rolecard_agent.core.tools.mcp._fetch_one", _ok)
    ok = client.post("/api/mcp/servers/srv1/test").json()
    assert ok["ok"] is True and ok["tool_count"] == 2
    assert ok["tools"] == ["do_thing", "do_thing"]

    async def _boom(cfg):  # noqa: ARG001
        raise ConnectionError("connect refused")

    monkeypatch.setattr("rolecard_agent.core.tools.mcp._fetch_one", _boom)
    bad = client.post("/api/mcp/servers/srv1/test").json()
    assert bad["ok"] is False and "refused" in bad["error"]
    # 测试失败仍是 200（连通性失败本身就是结果，不该冒 500）
    assert client.post("/api/mcp/servers/srv1/test").status_code == 200
    assert client.post("/api/mcp/servers/ghost/test").status_code == 404
