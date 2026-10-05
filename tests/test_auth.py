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


def test_health_reports_size_limits_the_frontend_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/api/health 把两个大小上限报出去：前端附图预检现读，不再手抄数字。

    这是"上限单一出处"契约的前端那一半 —— config.max_upload_bytes /
    max_image_bytes 改了，health 就跟着变；断言钉住"health 报的 = Settings 出厂值"
    这一对应关系（前端 limits.ts 的回落默认只兜后端不可达，不参与契约）。
    """
    from rolecard_agent.config import Settings

    c = _client(monkeypatch, tmp_path, mode="on")
    body = c.get("/api/health").json()
    assert body["max_upload_bytes"] == int(Settings.model_fields["max_upload_bytes"].default)
    assert body["max_image_bytes"] == int(Settings.model_fields["max_image_bytes"].default)


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


# -- 凭证分族（P0-3 第三步）---------------------------------------------------------


def test_operator_prefix_on_basic_credential_sets_the_role() -> None:
    """`operator:bob:pw` = 用户名 bob、密码 pw、角色 operator。

    前缀是**族标记**，不是用户名的一部分：日志里记的仍然是 `bob`，不然审计里会出现
    一个查无此人的 "operator:bob"。
    """
    from rolecard_agent.api.auth import ROLE_OPERATOR, ROLE_USER, resolve_actor
    from rolecard_agent.config import Settings

    settings = Settings(auth_credentials="alice:pw,operator:bob:pw")
    alice = resolve_actor(authorization=_basic("alice", "pw"), api_key=None, settings=settings)
    bob = resolve_actor(authorization=_basic("bob", "pw"), api_key=None, settings=settings)
    assert (alice.role, bob.role) == (ROLE_USER, ROLE_OPERATOR)
    assert bob.id == "bob"
    # 密码错仍然是匿名（`operator:` 那条目的存在不许让 alice 的比对走形）
    wrong = resolve_actor(authorization=_basic("alice", "nope"), api_key=None, settings=settings)
    assert wrong.is_anonymous


def test_operator_prefix_on_api_key_sets_the_role() -> None:
    from rolecard_agent.api.auth import ROLE_OPERATOR, ROLE_USER, resolve_actor
    from rolecard_agent.config import Settings

    settings = Settings(auth_api_keys="k-user,operator:k-admin")
    as_user = resolve_actor(authorization=None, api_key="k-user", settings=settings)
    as_admin = resolve_actor(authorization=None, api_key="k-admin", settings=settings)
    assert (as_user.role, as_admin.role) == (ROLE_USER, ROLE_OPERATOR)
    # 审计里仍然只留前 4 位（分族不许顺手把 key 记全）
    assert as_admin.id.startswith("key:") and len(as_admin.id) <= len("key:") + 4


def test_roles_are_declared_only_when_an_operator_entry_exists() -> None:
    """没写 operator 条目 = 这套配置不分族（老配置升级后不会被锁在门外）。"""
    from rolecard_agent.api.auth import roles_declared
    from rolecard_agent.config import Settings

    assert roles_declared(Settings(auth_credentials="alice:pw")) is False
    assert roles_declared(Settings(auth_api_keys="k-a,k-b")) is False
    assert roles_declared(Settings(auth_credentials="alice:pw,operator:bob:pw")) is True
    assert roles_declared(Settings(auth_api_keys="operator:k-admin")) is True


def test_non_ascii_credentials_are_a_401_not_a_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """中文账号 / 中文口令要走「拒绝」这条路，而不是把中间件炸掉。

    `hmac.compare_digest` 对含非 ASCII 的 `str` 直接抛 `TypeError`，所以修之前：**只要
    用户敲的口令里有一个中文字**（或操作员配的就是中文凭据），这一条请求就是 HTTP 500。
    第一次撞上是在 M5 的双实例端到端里：切换器弹层填了个中文口令，界面报"连不上"，
    而云端日志里一条 401 都没有，只有一段 traceback。
    比对按 UTF-8 字节做（`auth._eq`），常量时间那个性子一点没少。
    """
    c = _client(monkeypatch, tmp_path, creds="爱莉:口令,demo:s3cret")
    # 配好的中文凭据本身要登得进来 —— 只把 500 修成 401 会把登录一起修坏
    assert c.get("/api/roles", headers={"Authorization": _basic("爱莉", "口令")}).status_code == 200
    assert c.get("/api/roles", headers={"Authorization": _basic("爱莉", "看")}).status_code == 401
    # ASCII 凭据 + 非 ASCII 口令：同样 401（这才是"账号或密码不对"那句话的来源）
    assert c.get("/api/roles", headers={"Authorization": _basic("demo", "错")}).status_code == 401
    # API Key 那条路不测非 ASCII：HTTP 头的值本身只吃 latin1，中文 key 连请求都发不出去
    # （实测 httpx 直接 UnicodeEncodeError）。所以 `_eq` 在那一侧只是同一家族的写法统一。


def test_the_browser_prompt_is_only_offered_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`WWW-Authenticate` 只在"客户端压根没带凭据"时给。

    两条各挡一个方向：
      * 什么都没带 → **必须**带 challenge，否则 `AUTH_MODE=on` 的控制台只剩一个干巴巴的
        401，那个原生弹框本来就是"没有登录页"这套形态的登录页；
      * 已经带了一枚错的 → **不许**再 challenge。Chrome 对 `fetch` 收到的 401+challenge
        会去弹原生框：headless 下那个请求永远不返回（界面上就是"连接中…"卡死），
        有头下是用户刚填过的框上又叠一层浏览器弹框 —— 界面那句"账号或密码不对"被吃掉。
        M5 的双实例端到端第一次跑就撞上的是这一条。
    """
    c = _client(monkeypatch, tmp_path)
    naked = c.get("/api/roles")
    assert naked.status_code == 401
    assert "www-authenticate" in {k.lower() for k in naked.headers}

    wrong = c.get("/api/roles", headers={"Authorization": _basic("demo", "不对")})
    assert wrong.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in wrong.headers}, "已经带凭据还弹框"


def test_client_ip_right_to_left_rejects_injected_loopback() -> None:
    """2026-10-04 审查快照的来源 IP 条目：追加式反代下的伪造链必须作废。

    攻击者带 `X-Forwarded-For: 127.0.0.1` 穿过追加式反代（nginx 默认追加），链变成
    "127.0.0.1, 真实IP" —— 右向左第一个不可信跳是真实 IP，绝不是链首那个回环。
    """
    from rolecard_agent.api.auth import client_ip, parse_trusted_proxies

    trusted = parse_trusted_proxies("10.0.0.0/8")
    headers = {"x-forwarded-for": "127.0.0.1, 203.0.113.9"}
    assert client_ip(headers, peer="10.1.2.3", trusted=trusted) == "203.0.113.9"
    # 注入的回环绝不能作为来源返回（operator 判定看的是它是不是回环）
    assert client_ip(headers, peer="10.1.2.3", trusted=trusted) != "127.0.0.1"


def test_client_ip_multi_hop_clean_chain_resolves_the_client() -> None:
    """合法多级代理：客户端 → 反代A(10.1.2.3，追加) → 反代B(10.2.3.4，直连对端)。

    右向左：10.1.2.3 是可信代理跳过，第一个不可信跳 203.0.113.9 就是真实客户端。
    """
    from rolecard_agent.api.auth import client_ip, parse_trusted_proxies

    trusted = parse_trusted_proxies("10.0.0.0/8")
    headers = {"x-forwarded-for": "203.0.113.9, 10.1.2.3"}
    assert client_ip(headers, peer="10.2.3.4", trusted=trusted) == "203.0.113.9"


def test_client_ip_all_trusted_chain_falls_back_to_peer() -> None:
    """整条链都在可信网段（没有可判定的客户端跳）→ 退回对端，不猜。"""
    from rolecard_agent.api.auth import client_ip, parse_trusted_proxies

    trusted = parse_trusted_proxies("10.0.0.0/8")
    headers = {"x-forwarded-for": "10.1.2.3, 10.9.9.9"}
    assert client_ip(headers, peer="10.2.3.4", trusted=trusted) == "10.2.3.4"


def test_client_ip_single_hop_from_clean_proxy_still_trusted() -> None:
    """清洗型反代（覆盖式写 XFF）转发的本机客户端：单跳回环照旧采信（既有合法路径不回退）。"""
    from rolecard_agent.api.auth import client_ip, parse_trusted_proxies

    trusted = parse_trusted_proxies("10.0.0.0/8")
    assert client_ip({"x-forwarded-for": "127.0.0.1"}, peer="10.1.2.3", trusted=trusted) == (
        "127.0.0.1"
    )
