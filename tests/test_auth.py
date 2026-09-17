"""v2.4 接入层认证：三档开关 + Basic/API Key 双凭证 + 审计 actor。

六条不变量，每条都对应一个真实的事故场景：

  1. **默认 off** —— 本地演示零配置；也保证既有端点测试不受影响；
  2. **on 档没配凭证 = 拒绝所有人**（fail-closed）—— 配置不全绝不能退化成静默放行；
  3. **/api/health 永远可访问** —— 否则容器 healthcheck 会一直 401 把实例判死；
  4. **审计里的 actor 不含完整凭证** —— API Key 只留前 4 位（够区分是哪个 key，
     不够拿去用）。日志是最容易被扩散出去的地方；
  5. **`auto` 的"回环"只看 TCP 对端** —— 伪造 `X-Forwarded-For: 127.0.0.1` 不能绕过
     （修复前实测可绕过，历史审查 H2）；
  6. **可信反代仍有合法路径** —— `AUTH_TRUSTED_PROXIES` 命中时才采信 XFF，
     修安全漏洞不能把反代部署一起修坏。

Traceability: 代码审查报告 A2 / H2。
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
    trusted_proxies: str = "",
    peer: tuple[str, int] = ("testclient", 50000),
) -> TestClient:
    monkeypatch.setenv("AUTH_MODE", mode)
    monkeypatch.setenv("AUTH_CREDENTIALS", creds)
    monkeypatch.setenv("AUTH_API_KEYS", keys)
    monkeypatch.setenv("AUTH_EXEMPT_PATHS", exempt)
    monkeypatch.setenv("AUTH_TRUSTED_PROXIES", trusted_proxies)
    # `client=` 模拟 TCP 对端地址：`auto` 档的"回环"判定只看它，不看请求头。
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"), client=peer)


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
    """`auto`：直连对端是回环 → 放行；是外部地址 → 要求凭证。

    判定依据是 **TCP 对端**（`request.client.host`），不是 `X-Forwarded-For`。
    """
    loopback = _client(monkeypatch, tmp_path, mode="auto", peer=("127.0.0.1", 5000))
    assert loopback.get("/api/roles").status_code == 200

    foreign = _client(monkeypatch, tmp_path, mode="auto", peer=("203.0.113.9", 5000))
    assert foreign.get("/api/roles").status_code == 401


def test_forwarded_for_cannot_forge_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**安全回归**：伪造 `X-Forwarded-For: 127.0.0.1` 不能绕过 `auto`。

    这是历史审查项 H2 的安全回归：修复前实测为 200（完整认证绕过）。
    修复后 `auto` 只看 TCP 对端，XFF 被完全忽略（未配置可信代理时）。
    注意：本测试同时断言"未配置可信代理时，连 `req.client.host` 都不该被 XFF 影响"，
    所以这里刻意用外部对端 + 伪造 XFF 的组合。
    """
    c = _client(monkeypatch, tmp_path, mode="auto", peer=("203.0.113.9", 5000))
    for spoof in ("127.0.0.1", "::1", "127.0.0.1, 203.0.113.9", "localhost"):
        res = c.get("/api/roles", headers={"X-Forwarded-For": spoof})
        assert res.status_code == 401, f"伪造 {spoof!r} 竟然通过了认证"


def test_forwarded_for_is_honoured_only_from_a_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """反代场景仍然可用：直连方命中 `AUTH_TRUSTED_PROXIES` 时才采信 XFF。

    这条守的是"修 H2 不能把反代部署一起修坏"：修完之后必须有**一条**合法路径
    让反代把自己的客户端 IP 传进来。
    """
    c = _client(
        monkeypatch,
        tmp_path,
        mode="auto",
        trusted_proxies="10.0.0.0/8",
        peer=("10.1.2.3", 5000),  # 直连方是可信反代
    )
    assert c.get("/api/roles", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 200
    assert c.get("/api/roles", headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 401


def test_trusted_proxy_outside_the_configured_range_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """配了可信网段 ≠ 谁都可信：网段外的直连方带着 XFF 也一样要凭证。"""
    c = _client(
        monkeypatch,
        tmp_path,
        mode="auto",
        trusted_proxies="10.0.0.0/8",
        peer=("198.51.100.7", 5000),
    )
    assert c.get("/api/roles", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401


# -- 来源 IP 解析（纯函数） -----------------------------------------------------------


def test_parse_trusted_proxies_skips_garbage() -> None:
    from rolecard_agent.api.auth import parse_trusted_proxies

    nets = parse_trusted_proxies("10.0.0.0/8, not-a-network, 192.168.1.0/24,")
    assert len(nets) == 2  # 非法条目被跳过，不该让服务起不来


def test_client_ip_defaults_to_the_tcp_peer() -> None:
    from rolecard_agent.api.auth import client_ip

    class Headers(dict):
        pass

    headers = Headers({"x-forwarded-for": "127.0.0.1"})
    # 未配置可信代理 → 忽略 XFF，返回对端地址
    assert client_ip(headers, peer="203.0.113.9") == "203.0.113.9"
    # 无对端信息（理论上的空 client）→ 空串，由 auth_required 按"非回环"处理
    assert client_ip(headers, peer="") == ""


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
