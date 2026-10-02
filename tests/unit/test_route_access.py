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
    allowed_hosts,
    classify,
    outbound_target_allowed,
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


# ------------------------------------------------------------------ 凭证分族（c）


def test_operator_tier_requires_an_operator_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """远端"带了某条凭据"不再自动等于操作员 —— 这正是 b/a 之后剩下的那半个洞。

    `AUTH_MODE=on` 下配两条凭据：`alice`（使用者）与 `operator:bob`（操作员）。
    alice 能用自己的界面，但管理端点对她是 403；而且**文案要说清是角色不够**，
    不是"忘了带凭据" —— 她已经带对了凭据，另一种说法会让人对着密码框发愣。
    """
    client = _client(
        monkeypatch, tmp_path, mode="on", creds="alice:pw,operator:bob:pw"
    )
    assert client.get("/api/sessions", auth=("alice", "pw")).status_code == 200
    denied = client.get("/api/settings/runtime", auth=("alice", "pw"))
    assert denied.status_code == 403
    assert "使用者角色" in denied.text
    assert client.get("/api/settings/runtime", auth=("bob", "pw")).status_code == 200


def test_roles_stay_dormant_until_an_operator_credential_is_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """一条 operator 凭据都没配 = 不分族：老配置不能被这次改动锁在自己的设置页外。"""
    client = _client(monkeypatch, tmp_path, mode="on", creds="alice:pw")
    assert client.get("/api/settings/runtime", auth=("alice", "pw")).status_code == 200


def test_loopback_owner_is_not_asked_for_a_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """本机来源不看角色 —— 收紧针对的是"远端里任何带凭证的人"，不是坐在键盘前的主人。

    两档各测一格，因为它们的语义不同：`auto` 下回环**免凭据**（单机形态的全部意义），
    `on` 下凭据仍然要带、但带了使用者凭据也进得来（本机来源这一条不被角色判定推翻）。
    """
    auto = _client(
        monkeypatch, tmp_path, mode="auto", creds="alice:pw,operator:bob:pw", peer="127.0.0.1"
    )
    assert auto.get("/api/settings/runtime").status_code == 200
    on = _client(
        monkeypatch, tmp_path, mode="on", creds="alice:pw,operator:bob:pw", peer="127.0.0.1"
    )
    assert on.get("/api/settings/runtime", auth=("alice", "pw")).status_code == 200
    # 同一个配置从远端来就是另一回事（上一条用例钉的就是这个）
    far = _client(monkeypatch, tmp_path, mode="on", creds="alice:pw,operator:bob:pw")
    assert far.get("/api/settings/runtime", auth=("alice", "pw")).status_code == 403


def test_allowed_truth_table_with_roles() -> None:
    """`allowed()` 的角色维度单独钉：四格都不可省。"""
    operator_put = ("/api/settings/runtime", "PUT")

    def verdict(*, role: str, roles_in_effect: bool, authenticated: bool, ip: str) -> bool:
        return allowed(
            operator_put[0],
            operator_put[1],
            ip=ip,
            authenticated=authenticated,
            enforce=True,
            role=role,
            roles_in_effect=roles_in_effect,
        )

    assert verdict(role="operator", roles_in_effect=True, authenticated=True, ip="203.0.113.9")
    assert not verdict(
        role="user", roles_in_effect=True, authenticated=True, ip="203.0.113.9"
    )
    # 分族未生效：任何已认证身份都算操作员（兼容规则）
    assert verdict(role="user", roles_in_effect=False, authenticated=True, ip="203.0.113.9")
    # 本机来源永远放行，且与"带没带凭据"无关
    assert verdict(role="user", roles_in_effect=True, authenticated=False, ip="127.0.0.1")


# ---------------------------------------------------------------- 出站目标（R102-58）


def test_outbound_target_truth_table() -> None:
    """`outbound_target_allowed` 的真值表：off 档一律放行；开启后按"回环 / 清单 / 档位"分。

    这一格与 `allowed()` 是**同一信任模型的两个方向**：那个判"谁能进来碰我"，这个判
    "我能把数据送到哪去"。写成一张表而不是散在路由里，是因为它要回答的正是审计问过的
    那句话 —— user 档凭什么等于任意地址的数据出口。
    """
    # `roles_in_effect=True` = 配置里写了 `operator:` 凭据、分族真的生效（与 `allowed()`
    # 同一条兼容规则：没写 operator 凭据时，任何已认证身份都按操作员看待）。
    far = {
        "ip": "203.0.113.9",
        "authenticated": True,
        "enforce": True,
        "roles_in_effect": True,
    }
    # off 档（单机单人形态）：连匿名都放行，逐字不变
    assert outbound_target_allowed(
        "http://anywhere.example", ip="203.0.113.9", authenticated=False, enforce=False
    )
    # 回环目标：它就是"本机对面那份自己"，任何档都不该拦
    assert outbound_target_allowed("http://127.0.0.1:8123", **far)
    assert outbound_target_allowed("http://localhost:8123", **far)
    assert outbound_target_allowed("http://[::1]:8123", **far)
    # 清单三种写法等价（裸主机 / 带端口 / 整段 URL），且只比主机名不比端口
    for raw in ("cloud.test", "cloud.test:8443", "https://cloud.test"):
        assert outbound_target_allowed("http://cloud.test:9999", allowlist=raw, **far), raw
    # 不在清单里：user 档拒 → operator 档放行 → 本机来源放行（三条各是一次判定）
    assert not outbound_target_allowed("http://cloud.test:8123", **far)
    assert outbound_target_allowed("http://cloud.test:8123", role="operator", **far)
    assert outbound_target_allowed("http://cloud.test:8123", **{**far, "ip": "127.0.0.1"})
    # 分族未生效（没写 `operator:` 凭据）：已认证身份仍算操作员 —— 与 `allowed()` 同一条兼容
    assert outbound_target_allowed(
        "http://cloud.test:8123", ip="203.0.113.9", authenticated=True, enforce=True
    )
    # 匿名 + 非清单 = 拒（宁严：这一档本来就不该由匿名发起）
    assert not outbound_target_allowed(
        "http://cloud.test:8123", ip="203.0.113.9", authenticated=False, enforce=True
    )
    # 解析不出主机的一律拒 —— 地址合法性归 validate_base_url，但这里也不放过空值
    assert not outbound_target_allowed("", **far)
    assert not outbound_target_allowed("http://", **far)
    # 清单解析：空串 / 多余逗号 / 大小写与空格都不该造出幽灵条目
    assert allowed_hosts("") == frozenset()
    assert allowed_hosts(" , ") == frozenset()
    assert allowed_hosts(" Cloud.Test , ") == frozenset({"cloud.test"})


def test_foreign_sync_target_needs_an_operator_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`/api/sync/*` 属使用者档，但"把数据推到哪台机器"要 operator 认过（`R102-58`）。

    `AUTH_MODE=on` + 远端对端：使用者凭据推到公网地址 = 403 且**文案点名了配置项**；
    操作员凭据同一目标放行（走到真发请求那一步）；回环目标对使用者照常放行 ——
    这三格合起来才是"单机形态零感知、多凭据部署有边界"。
    """
    client = _client(
        monkeypatch, tmp_path, mode="on", creds="operator:bob:pw,alice:pw", peer="203.0.113.9"
    )
    foreign = {"base_url": "http://cloud.test:8123", "user": "alice", "secret": "s"}
    denied = client.post("/api/sync/plan", json=foreign, auth=("alice", "pw"))
    assert denied.status_code == 403
    assert "允许清单" in denied.json()["detail"], denied.json()["detail"]

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"items": [], "skipped": []}

    monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp())
    # 操作员凭据：同一目标放行
    assert client.post("/api/sync/plan", json=foreign, auth=("bob", "pw")).status_code == 200
    # 回环目标：使用者凭据也放行（在清单之外，但它是"本机对面那份自己"）
    near = {"base_url": "http://127.0.0.1:1", "user": "alice", "secret": "s"}
    monkeypatch.undo()  # 去掉替身，让它真的去连（连不上 = 已经过了那道闸）
    assert client.post("/api/sync/plan", json=near, auth=("alice", "pw")).status_code == 502


def test_sync_allowlist_lets_a_user_reach_a_named_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """operator 在配置里点过头的那台，使用者凭据就够（这正是允许清单的用法）。"""
    monkeypatch.setenv("SYNC_ALLOWED_HOSTS", "cloud.test")
    client = _client(
        monkeypatch, tmp_path, mode="on", creds="operator:bob:pw,alice:pw", peer="203.0.113.9"
    )

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"items": [], "skipped": []}

    monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp())
    ok = client.post(
        "/api/sync/plan",
        json={"base_url": "http://cloud.test:8123", "user": "alice", "secret": "s"},
        auth=("alice", "pw"),
    )
    assert ok.status_code == 200, ok.text
