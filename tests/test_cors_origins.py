"""跨域放行（M5 的地基）：默认不装，装了也只认精确 origin，而且预检不能被认证挡。

三条各挡一个坏法：

  * **默认零 CORS** —— 不设 `API_ALLOW_ORIGINS` 时行为与今天逐字节相同。开成 `*` 等于
    让任意网页在用户已登录的浏览器里驱动这个后端，所以这里连"允许某个 origin"的
    响应头都不该出现；
  * **只认列出来的那几个** —— 列了 A 不代表放行 B；
  * **预检必须先被 CORS 答掉** —— 中间件顺序装反的症状非常阴：切换器一直报"连不上"，
    而云端日志里一个请求都没收到（`OPTIONS` 按规范不带凭据，被 `AUTH_MODE=on` 挡成 401
    就等于跨域根本没通）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app

ALLOWED = "http://cloud-ui.example"
OTHER = "http://evil.example"


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origins: str, auth: str):
    monkeypatch.setenv("AUTH_MODE", auth)
    monkeypatch.setenv("AUTH_CREDENTIALS", "u1:pw")
    if origins:
        monkeypatch.setenv("API_ALLOW_ORIGINS", origins)
    else:
        monkeypatch.delenv("API_ALLOW_ORIGINS", raising=False)
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"))


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/api/roles",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


def test_no_cors_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch, "", "off")
    for path, origin in (("/api/roles", OTHER), ("/api/roles", ALLOWED), ("/api/health", OTHER)):
        res = client.request("OPTIONS", path, headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        })
        names = {k.lower() for k in res.headers}
        assert "access-control-allow-origin" not in names, (path, names)


def test_only_the_listed_origin_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch, f"{ALLOWED}, http://also.example", "off")
    ok = _preflight(client, ALLOWED)
    assert ok.headers.get("access-control-allow-origin") == ALLOWED
    assert ok.headers.get("access-control-allow-credentials") == "true"
    denied = _preflight(client, OTHER)
    assert "access-control-allow-origin" not in {k.lower() for k in denied.headers}
    # 真请求（非预检）也要带上那个头，否则浏览器照样拦
    got = client.get("/api/roles", headers={"Origin": ALLOWED})
    assert got.headers.get("access-control-allow-origin") == ALLOWED


def test_preflight_is_answered_before_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AUTH_MODE=on` 时预检不能被 401 —— 它按规范不带凭据。"""
    client = _client(tmp_path, monkeypatch, ALLOWED, "on")
    pre = _preflight(client, ALLOWED)
    assert pre.status_code in (200, 204), pre.status_code
    assert pre.headers.get("access-control-allow-origin") == ALLOWED
    # 而真正的跨域请求仍然要凭据：放行 origin ≠ 放行人格
    anon = client.get("/api/roles", headers={"Origin": ALLOWED})
    assert anon.status_code == 401
