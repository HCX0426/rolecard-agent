"""来源标识护栏（`R102-45`）的行为钉。

`R102-45` 的主弹链（伪造 Host 读全库 → 列表拿 token → 替用户批准 → 命令真执行）已在
复审时用真后端复跑坐实；这里钉的是修复自身的三档应红形状：

* 摘掉 `main.py` 的 `_local_origin_guard` 整段 ⇒ 端到端四条同时红（护栏不在栈上）；
* 摘掉 `origin_guard_violation` 的 Host 分支 ⇒ `off` 档 Host 两条红；
* 摘掉 Origin 分支 ⇒ 盲 POST 与假 null 那组红。

既有全部 API 用例跑在 `conftest` 关掉护栏的会话里（TestClient 的 Host 是 `testserver`），
所以本文件自己把 `LOCAL_ORIGIN_ENFORCE=1` 开回来 —— 顺带钉住"开着时本机请求照常放行"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.auth import origin_guard_violation
from rolecard_agent.api.main import create_app

# -- 判据（函数级） ----------------------------------------------------------------


def test_off_mode_rejects_non_loopback_host() -> None:
    reason = origin_guard_violation(
        host_header="attacker.example", origin=None, sec_fetch_site=None, auth_mode="off"
    )
    assert reason is not None and "127.0.0.1" in reason


def test_off_mode_allows_loopback_hosts_with_any_port() -> None:
    for host in ("127.0.0.1:58431", "localhost", "localhost:8000", "[::1]:8000"):
        assert (
            origin_guard_violation(
                host_header=host, origin=None, sec_fetch_site=None, auth_mode="off"
            )
            is None
        ), host


def test_missing_host_header_is_not_the_rebinding_shape() -> None:
    """HTTP/1.0 的裸客户端不带 Host —— rebinding 浏览器恒带，缺 Host 不该误伤。"""
    assert (
        origin_guard_violation(
            host_header=None, origin=None, sec_fetch_site=None, auth_mode="off"
        )
        is None
    )


def test_on_mode_skips_host_check_public_domain_allowed() -> None:
    """on/auto 档 Host 本来就可能是公网域名 —— Host 校验只属于 off 档的信任模型。"""
    assert (
        origin_guard_violation(
            host_header="api.example.com",
            origin=None,
            sec_fetch_site=None,
            auth_mode="on",
        )
        is None
    )


def test_cross_origin_blind_post_rejected_in_every_mode() -> None:
    """盲 POST 的 Host 是对的（就是 127.0.0.1:8000）—— 挡它的只能是 Origin（全档生效）。"""
    for mode in ("off", "on", "auto"):
        reason = origin_guard_violation(
            host_header="127.0.0.1:8000",
            origin="http://evil.example",
            sec_fetch_site=None,
            auth_mode=mode,
        )
        assert reason is not None and "跨源" in reason, mode


def test_same_origin_and_shell_null_origin_allowed() -> None:
    # 同源：origin 主机名 == Host 主机名（scheme/端口不参与比较 —— 反代终止 TLS 的公网形态）。
    assert (
        origin_guard_violation(
            host_header="127.0.0.1:8000",
            origin="http://127.0.0.1:8000",
            sec_fetch_site=None,
            auth_mode="off",
        )
        is None
    )
    # file:// 壳的两扇窗发合法的 `Origin: null`，放行；
    # 沙箱 iframe 伪造的假 null 恒带 `Sec-Fetch-Site: cross-site`，拒绝。
    assert (
        origin_guard_violation(
            host_header="127.0.0.1:8000", origin="null", sec_fetch_site=None, auth_mode="off"
        )
        is None
    )
    assert (
        origin_guard_violation(
            host_header="127.0.0.1:8000",
            origin="null",
            sec_fetch_site="cross-site",
            auth_mode="off",
        )
        is not None
    )


# -- 端到端（护栏开着、真中间件在栈上） --------------------------------------------


@pytest.fixture
def guarded_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """护栏默认开；conftest 为既有用例关了它，这里单独开回来再建应用。"""
    monkeypatch.setenv("LOCAL_ORIGIN_ENFORCE", "1")
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"), base_url="http://127.0.0.1")


def test_loopback_request_passes_guard(guarded_client: TestClient) -> None:
    """开着护栏，本机客户端（壳/控制台/脚本/探针的形态）一字不变。"""
    assert guarded_client.get("/api/health").status_code == 200


def test_rebinding_host_is_403_everywhere(guarded_client: TestClient) -> None:
    """R102-45 的第①②步（伪造 Host 读端点、读审批列表拿 token）在 403 上断掉。"""
    rebinding = {"Host": "attacker.example"}
    assert guarded_client.get("/api/health", headers=rebinding).status_code == 403
    assert guarded_client.get("/api/approvals", headers=rebinding).status_code == 403


def test_drive_by_post_is_403(guarded_client: TestClient) -> None:
    """SEC-02 的探针原样收编为验收：无预检跨源 POST 403，本机数据不再被盲 POST 动。"""
    res = guarded_client.post(
        "/api/uploads/cleanup",
        headers={"Origin": "http://evil.example", "Content-Type": "text/plain"},
    )
    assert res.status_code == 403


def test_rollback_switch_restores_old_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_ORIGIN_ENFORCE", "0")
    client = TestClient(
        create_app(sqlite_path=tmp_path / "app.db"), base_url="http://127.0.0.1"
    )
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 200
