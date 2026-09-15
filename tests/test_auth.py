"""v2.4 接入层认证：三档开关 + Basic/API Key 双凭证 + 审计 actor。

四条不变量，每条都对应一个真实的事故场景：

  1. **默认 off** —— 本地演示零配置；也保证既有端点测试不受影响；
  2. **on 档没配凭证 = 拒绝所有人**（fail-closed）—— 配置不全绝不能退化成静默放行；
  3. **/api/health 永远可访问** —— 否则容器 healthcheck 会一直 401 把实例判死；
  4. **审计里的 actor 不含完整凭证** —— API Key 只留前 4 位（够区分是哪个 key，
     不够拿去用）。日志是最容易被扩散出去的地方。

Traceability: 代码审查报告 A2。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str = "on",
    creds: str = "demo:s3cret",
    keys: str = "k-abcdef",
    exempt: str = "/api/health",
) -> TestClient:
    monkeypatch.setenv("AUTH_MODE", mode)
    monkeypatch.setenv("AUTH_CREDENTIALS", creds)
    monkeypatch.setenv("AUTH_API_KEYS", keys)
    monkeypatch.setenv("AUTH_EXEMPT_PATHS", exempt)
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"))


# -- 档位 ---------------------------------------------------------------------------


def test_off_mode_lets_local_demo_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """默认 off：不配凭证也能用 —— 这是"本地打开就能演示"的底线。"""
    c = _client(monkeypatch, tmp_path, mode="off")
    assert c.get("/api/roles").status_code == 200


def test_on_mode_rejects_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = _client(monkeypatch, tmp_path)
    res = c.get("/api/roles")
    assert res.status_code == 401
    # 没有 WWW-Authenticate 浏览器不会弹框，用户只会看到一个干巴巴的 401
    assert "www-authenticate" in res.headers


def test_on_mode_without_any_credential_denies_everyone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fail-closed：开了认证却一个凭证都没配 = 谁都进不来，而不是谁都能进。"""
    c = _client(monkeypatch, tmp_path, creds="", keys="")
    assert c.get("/api/roles").status_code == 401
    assert (
        c.get("/api/roles", headers={"Authorization": _basic("demo", "s3cret")}).status_code == 401
    )


def test_auto_mode_allows_loopback_and_denies_foreign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = _client(monkeypatch, tmp_path, mode="auto")
    # 反代后面必须看 X-Forwarded-For，否则把反代自己的 IP 当客户端
    assert c.get("/api/roles", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 200
    assert c.get("/api/roles", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 401


def test_health_endpoint_is_always_reachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = _client(monkeypatch, tmp_path, mode="on")
    res = c.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


# -- 凭证形态 -----------------------------------------------------------------------


def test_basic_credentials_are_accepted_and_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = _client(monkeypatch, tmp_path)
    ok = c.get("/api/roles", headers={"Authorization": _basic("demo", "s3cret")})
    bad = c.get("/api/roles", headers={"Authorization": _basic("demo", "wrong")})
    assert ok.status_code == 200 and bad.status_code == 401
    assert c.get("/api/roles", headers={"Authorization": "Basic not-base64!!"}).status_code == 401


def test_api_key_is_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    c = _client(monkeypatch, tmp_path)
    assert c.get("/api/roles", headers={"X-API-Key": "k-abcdef"}).status_code == 200
    assert c.get("/api/roles", headers={"X-API-Key": "k-wrong"}).status_code == 401


# -- 审计身份 -----------------------------------------------------------------------


def test_audit_records_basic_username(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """审计的 actor 不再是写死的 "operator"，而是真实调用方。"""
    c = _client(monkeypatch, tmp_path)
    res = c.post("/api/session", json={}, headers={"Authorization": _basic("demo", "s3cret")})
    assert res.status_code == 201
    rows = c.get("/api/audit?limit=20", headers={"Authorization": _basic("demo", "s3cret")}).json()
    create_events = [r for r in rows if r["action"] == "create_session"]
    assert create_events and create_events[0]["actor"] == "demo"


def test_audit_never_stores_the_full_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """日志只留 key 前 4 位：够区分是哪个 key，不够拿去用。"""
    c = _client(monkeypatch, tmp_path)
    assert c.post("/api/session", json={}, headers={"X-API-Key": "k-abcdef"}).status_code == 201
    rows = c.get("/api/audit?limit=20", headers={"X-API-Key": "k-abcdef"}).json()
    create_events = [r for r in rows if r["action"] == "create_session"]
    assert create_events
    actor = create_events[0]["actor"]
    assert actor == "key:k-ab"
    assert "abcdef" not in actor
