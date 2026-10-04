"""错误响应的唯一形状：`{"detail": ...}` 的 JSON —— 中间件那半也不例外。

为什么单独立（2026-10-04 错误收口那条）：错误响应从前**裂成两半**——路由走 JSON
detail，而认证 / 限流 / 操作员分级 / 来源护栏四个中间件返回 `PlainTextResponse` 裸文本。
前端 `request()` 只解析 JSON detail（读不到就退回 `"403 Forbidden"` 这种状态行），
于是**精心写的中文文案在中间件那半到不了用户**，`Retry-After` 也从不被读取。

四个中间件的拒绝各钉一条，再钉注册表的两条族 —— 判据是 content-type、detail 文案、
外加那两个头（401 的 `WWW-Authenticate`、429 的 `Retry-After`），缺一样都算回流。
**只断言 status 的用例很多**（它们现在照样绿），但没人守"正文是什么形状" —— 这条守。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app


def _json_detail(res: object) -> str:
    """每个拒绝都必须是前端认识的那个形状：JSON + 字符串 detail。返回 detail 文本。"""
    headers = res.headers  # type: ignore[attr-defined]
    assert headers["content-type"].startswith("application/json"), (
        f"错误正文不是 JSON（content-type={headers['content-type']}）—— "
        "前端只解析 json detail，裸文本会被退回状态行"
    )
    data = res.json()  # type: ignore[attr-defined]
    assert isinstance(data.get("detail"), str), f"detail 不是字符串：{data!r}"
    return str(data["detail"])


def test_naked_request_gets_a_json_401_with_challenge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没带凭据 → 401 也是 JSON，`WWW-Authenticate` 一字不丢（浏览器弹框靠它）。"""
    monkeypatch.setenv("AUTH_MODE", "on")
    monkeypatch.setenv("AUTH_CREDENTIALS", "alice:pw")
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"), client=("203.0.113.9", 50000))
    res = c.get("/api/sessions")
    assert res.status_code == 401
    assert _json_detail(res) == "Unauthorized"  # 原文照出（auth.unauthorized_response 那句）
    assert "www-authenticate" in {k.lower() for k in res.headers}


def test_operator_denial_carries_the_chinese_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """使用者打操作员面 → 403 的**中文理由**到达前端（从前退化成 '403 Forbidden'）。

    文案还必须说清缺的是哪一样：已经认证却被拒 = 角色不够，不是"忘了带凭据"。
    **分族要有一条 `operator:` 凭据才生效**（`auth.roles_declared`）：否则按老配置的
    兼容语义，认证过的人就是一切 —— 那正是这套区分"没有声明就关闭"的设计。
    """
    monkeypatch.setenv("AUTH_MODE", "on")
    monkeypatch.setenv("AUTH_CREDENTIALS", "alice:pw,operator:bob:pw")
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"), client=("203.0.113.9", 50000))
    res = c.get("/api/settings/runtime", auth=("alice", "pw"))
    assert res.status_code == 403
    detail = _json_detail(res)
    assert "使用者角色" in detail, f"中文理由没到前端：{detail!r}"
    assert "Forbidden" in detail  # 原文案以 Forbidden 起头，一个字没改


def test_rate_limited_response_is_json_with_retry_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """超额度 → 429 同样是 JSON，`Retry-After` 头还在（前端从此读得到它）。"""
    monkeypatch.setenv("AUTH_MODE", "on")
    monkeypatch.setenv("AUTH_CREDENTIALS", "alice:pw")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("RATE_LIMIT_PATHS", "/api/chat")
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"), client=("203.0.113.9", 50000))
    body = {"thread_id": "t", "text": "hi"}
    auth = ("alice", "pw")
    c.post("/api/chat", json=body, auth=auth)  # 第一发占额度
    res = c.post("/api/chat", json=body, auth=auth)
    assert res.status_code == 429
    detail = _json_detail(res)
    assert "秒后再试" in detail and "RATE_LIMIT_PER_MINUTE" in detail, detail
    assert int(res.headers["retry-after"]) >= 1


def test_origin_guard_rejection_is_json_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """来源护栏的 403（rebinding 那条防线）也是同一形状 —— 四个中间件一个不落。"""
    monkeypatch.setenv("LOCAL_ORIGIN_ENFORCE", "1")  # conftest 全局关了它，这里单独打开
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    res = c.get("/api/health", headers={"Host": "attacker.example"})
    assert res.status_code == 403
    detail = _json_detail(res)
    assert "不是回环地址" in detail, detail


def test_registered_family_maps_to_its_status_with_json_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """注册表接管的族：异常自己走到 handler，路由不再 catch，形状与码照旧。

    404（角色不存在）与 409（同名冲突）各探一条 —— 它们从前分别散在
    `role_error_to_http` 与六处手写 catch 里，现在是 `api/errors._FAMILIES` 一份声明。
    """
    monkeypatch.delenv("AUTH_MODE", raising=False)
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"))

    res = c.post("/api/session", json={"role_id": "ghost_role"})
    assert res.status_code == 404, res.text
    assert "role not found" in _json_detail(res)

    role = {"role_id": "dup", "role_name": "重名", "system_prompt": "x"}
    assert c.post("/api/roles", json=role).status_code == 201
    again = c.post("/api/roles", json=role)
    assert again.status_code == 409, again.text
    assert _json_detail(again)  # 有一句可读理由（不许是空的 409 壳）
