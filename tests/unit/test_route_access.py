"""路由访问分级的清单与执行（P0-3 第三步前半，`api/access.py`）。

为什么值得单独立一个文件钉：这张表是"哪些端点能影响主机"的**唯一答案**，而它的价值
一半在运行时拦截、一半在**没人能悄悄加一条没表态的端点**。所以这里既有枚举全量路由的
清单检查（新端点没进清单 → 落 operator；清单里写了却不匹配任何路由 → 红），也有
中间件真跑一遍的行为用例。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.access import (
    DOC_PATHS,
    OPERATOR,
    PUBLIC,
    PUBLIC_EXACT,
    USER,
    USER_ROUTES,
    allowed,
    classify,
)
from rolecard_agent.api.main import create_app


def _live_routes(app: Any) -> list[tuple[str, str]]:
    """枚举运行时 (method, path)。

    这个版本 FastAPI 把 `include_router` 存成惰性的 `_IncludedRouter`，`app.routes` 里
    根本没有 APIRoute —— 不递归展开就会"看到 19 条"而实际 75 条，清单检查会假绿。
    """
    out: list[tuple[str, str]] = []
    for route in app.routes:
        items = (
            route.effective_candidates()
            if type(route).__name__ == "_IncludedRouter"
            else [route]
        )
        for item in items:
            target = getattr(item, "route", item)
            path = getattr(target, "path", None) or getattr(item, "path", "")
            for method in sorted(getattr(target, "methods", None) or {"GET"}):
                if method not in ("HEAD", "OPTIONS"):
                    out.append((method, path))
    return out


def _app(**kwargs: Any) -> Any:
    return create_app(sqlite_path=Path(tempfile.mkdtemp()) / "access.db", **kwargs)


# ------------------------------------------------------------------ 清单本身


def test_every_live_route_is_classified_and_undeclared_is_operator() -> None:
    routes = _live_routes(_app())
    assert len(routes) > 50, f"路由枚举不对，只看到 {len(routes)} 条（清单检查会假绿）"
    for method, path in routes:
        assert classify(path, method) in (PUBLIC, USER, OPERATOR)
    # 未表态 = 最严那一档，这是整张表的设计前提。
    assert classify("/api/brand-new-admin-button", "POST") is OPERATOR
    assert classify("/api/settings/runtime", "PUT") is OPERATOR


def test_no_manifest_entry_is_dead() -> None:
    """清单里每一条都必须真匹配到某个路由。

    前缀打错字、端点改了名都会让降级**悄悄失效** —— 那时行为是"更严"（掉回 operator），
    测试与界面却还以为它被降级过，于是某天开始 403 而没人知道为什么。宁可现在红。
    """
    routes = _live_routes(_app())
    for prefix, methods in USER_ROUTES:
        hit = any(
            method in methods and (path == prefix or path.startswith(f"{prefix}/"))
            for method, path in routes
        )
        assert hit, f"清单里的 {prefix} 匹配不到任何路由（写错了还是端点改名了？）"
    for prefix in PUBLIC_EXACT:
        assert any(path == prefix for _, path in routes), f"PUBLIC_EXACT 里的 {prefix} 已失效"


def test_boundaries_that_are_easy_to_get_wrong() -> None:
    """几条最容易在重构中被改错的分界，逐条点名。"""
    cases: list[tuple[str, str, str]] = [
        # 对话链路是使用者的日常
        ("POST", "/api/chat", USER),
        ("GET", "/api/sessions", USER),
        ("POST", "/api/roles", USER),
        ("PUT", "/api/settings/memory", USER),
        # 读侧降级、写侧收紧：同一个前缀两档
        ("GET", "/api/settings/models", USER),
        ("PUT", "/api/settings/models", OPERATOR),
        ("GET", "/api/approvals", USER),
        ("POST", "/api/approvals/7/decide", OPERATOR),
        ("GET", "/api/plugins", USER),
        ("POST", "/api/plugins/web_search/toggle", OPERATOR),
        # 管理面与主机影响
        ("GET", "/api/workspace/tree", OPERATOR),
        ("GET", "/api/mcp/servers", OPERATOR),
        ("POST", "/api/local-service/unload", OPERATOR),
        ("GET", "/api/shell-release/download", OPERATOR),
        ("GET", "/api/audit", OPERATOR),
        # 探活与静态、文档
        ("GET", "/api/health", PUBLIC),
        ("GET", "/", PUBLIC),
        ("GET", "/assets/index-abc123.js", PUBLIC),
        ("GET", "/openapi.json", OPERATOR),
    ]
    for method, path, want in cases:
        assert classify(path, method) == want, f"{method} {path} 应该是 {want}"
    assert all(classify(p) is OPERATOR for p in DOC_PATHS), "文档路由不该跟着静态一起豁免"


# ------------------------------------------------------------------ 放行判定


def test_allowed_truth_table() -> None:
    runtime_put = ("/api/settings/runtime", "PUT")

    def ok(ip: str, authed: bool, enforce: bool = True) -> bool:
        return allowed(runtime_put[0], runtime_put[1], ip=ip, authenticated=authed, enforce=enforce)

    # off 档：本机单人使用的语义，不启用分级（非回环绑定已被启动护栏拒绝）
    assert ok("203.0.113.9", authed=False, enforce=False)
    # on/auto 档：操作员面要么本机、要么已认证
    assert ok("127.0.0.1", authed=False)
    assert ok("203.0.113.9", authed=True)
    assert not ok("203.0.113.9", authed=False)
    # 使用者面不受这条收紧（它由认证本身按 AUTH_MODE 管），远端匿名也照样能对话
    assert allowed("/api/chat", "POST", ip="203.0.113.9", authenticated=False, enforce=True)
    # 探活永远可达（容器 healthcheck / 反代不带凭据）
    assert allowed("/api/health", "GET", ip="203.0.113.9", authenticated=False, enforce=True)


def test_xff_cannot_bypass_the_operator_tier() -> None:
    """来源判定只认 TCP 对端（`auth.client_ip` 的老规矩在这里同样成立）。"""
    from rolecard_agent.api.auth import client_ip

    headers = {"x-forwarded-for": "127.0.0.1"}
    origin = client_ip(headers, peer="203.0.113.9", trusted=())
    assert origin == "203.0.113.9"
    assert not allowed("/api/audit", "GET", ip=origin, authenticated=False, enforce=True)


# ------------------------------------------------------------------ 中间件真跑


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str,
    creds: str = "",
    exempt: str = "",
    peer: str = "203.0.113.9",
) -> TestClient:
    """按 env 装一个 app（`create_app` 自己 `Settings.from_env()`），并**指定 TCP 对端**。

    对端可注入是这条用例成立的前提：分级判的是"是不是本机来的"，靠 TestClient 默认的
    "testclient" 那个假对端什么也证明不了。
    """
    monkeypatch.setenv("AUTH_MODE", mode)
    monkeypatch.setenv("AUTH_CREDENTIALS", creds)
    monkeypatch.setenv("AUTH_EXEMPT_PATHS", exempt)
    app = create_app(sqlite_path=tmp_path / "access.db")
    return TestClient(app, client=(peer, 51234))


def test_exempt_path_cannot_make_an_admin_endpoint_credential_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`AUTH_EXEMPT_PATHS` 配宽了也不会把管理端点变成免凭据可达。

    认证那一步因豁免放过 → 分级这层补上 403；同一个配置下使用者端点照常可用，
    证明我们没有顺手把整站拦死。
    """
    client = _client(
        monkeypatch,
        tmp_path,
        mode="on",
        creds="op:pw",
        exempt="/api/settings/runtime,/api/sessions",
    )
    assert client.get("/api/settings/runtime").status_code == 403
    assert client.get("/api/sessions").status_code == 200  # 使用者面不受这条收紧
    # 同一条端点，带上凭据就放行 —— 不是永久锁死
    assert client.get("/api/settings/runtime", auth=("op", "pw")).status_code == 200


def test_loopback_peer_reaches_the_operator_tier_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """本机来源不需要凭据（单机形态的全部意义）。"""
    client = _client(
        monkeypatch,
        tmp_path,
        mode="on",
        creds="op:pw",
        exempt="/api/settings/runtime",
        peer="127.0.0.1",
    )
    assert client.get("/api/settings/runtime").status_code == 200
