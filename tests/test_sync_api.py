"""上行同步的端点（M7 第一批）：清单只给本人、计划不写任何东西、凭据不外露。

三条各挡一个坏法：

  * **清单按身份过滤** —— `inventory` 是对面会来敲的那一发。它要是把整库的可同步条目都列出来，
    M1~M6 那一整套归属就白做了：不用导入，直接读走别人的会话标题与记忆文本。
  * **计划这一步真的不写** —— 界面上"下一步：看差异"之后用户可能直接关掉页面，
    所以这一发必须是纯读（连对面也是 GET）。
  * **凭据不回显、不落审计** —— 它出现在请求体里，任何一处把它写进响应、日志或
    `audit_log` 都是把"刚输的那枚口令"变成一条可被读出来的记录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.core import sync as S

SECRET = "sk-secret-不该出现在任何回答里"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_MODE", "off")
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    with c:
        conn = c.app.state.ctx.conn
        conn.execute(
            "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
            "VALUES ('u1', 'local', '第二个')"
        )
        conn.execute(
            "INSERT INTO role_card (role_id, user_id, role_name, system_prompt) "
            "VALUES ('mine_a', 'local-user', '我的卡', '本机主人的')"
        )
        conn.execute(
            "INSERT INTO role_card (role_id, user_id, role_name, system_prompt) "
            "VALUES ('theirs_b', 'u1', '他的卡', '第二个身份的')"
        )
        conn.execute(
            "INSERT INTO role_memory_item (role_id, user_id, uid, text) "
            "VALUES ('', 'u1', 'uid-of-his', '他不该被列出来')"
        )
        conn.commit()
        yield c


def test_inventory_lists_only_the_callers_own_items(client: TestClient) -> None:
    mine = client.get("/api/sync/inventory").json()["items"]
    idents = {str(row["ident"]) for row in mine}
    assert "mine_a" in idents, sorted(idents)
    assert "theirs_b" not in idents and "uid-of-his" not in idents
    # 清单里没有载荷：只有身份、指纹、时刻与一句预览
    assert all("payload" not in row for row in mine)


def test_plan_is_a_read_and_rejects_a_non_http_target(client: TestClient) -> None:
    bad = client.post(
        "/api/sync/plan",
        json={"base_url": "file:///etc/passwd", "user": "u1", "secret": "x"},
    )
    assert bad.status_code == 400
    # 对面不可达：502 + 一句能照着查的话，不是 500 空壳
    dead = client.post(
        "/api/sync/plan",
        json={"base_url": "http://127.0.0.1:1", "user": "u1", "secret": SECRET},
    )
    assert dead.status_code == 502
    assert "连不上" in dead.json()["detail"], dead.json()["detail"]
    assert SECRET not in dead.text


def test_plan_reports_counts_and_conflicts_without_writing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把对面的清单换成一份"有一条相同、有一条冲突"的假答复，看本机比出什么。"""
    mine = client.get("/api/sync/inventory").json()["items"]
    card = next(row for row in mine if row["ident"] == "mine_a")
    theirs = [
        card,  # 相同
        dict(card, ident="ghost", hash="0" * 16),  # 对面独有
        {"kind": S.KIND_MEMORY, "ident": "uid-x", "hash": "1" * 16, "preview": "他那边的一条记忆"},
    ]

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"items": theirs, "skipped": []}

    def fake_get(url: str, **kwargs: Any) -> _Resp:
        assert url.endswith("/api/sync/inventory"), url
        assert kwargs["headers"]["Authorization"].startswith("Basic ")
        return _Resp()

    monkeypatch.setattr("httpx.get", fake_get)
    body = client.post(
        "/api/sync/plan",
        json={"base_url": "http://cloud.test:8123", "user": "u1", "secret": SECRET},
    ).json()
    assert body["counts"]["same"] >= 1
    assert body["counts"]["only_remote"] >= 1
    assert body["remote_counts"].get(S.KIND_MEMORY) == 1
    # 计划这一步不写：本机没有新增审计，也没有把凭据留在任何可读回来的地方
    assert SECRET not in client.get("/api/sync/inventory").text
    assert SECRET not in client.get("/api/audit?limit=50").text
    conn = client.app.state.ctx.conn
    rows = conn.execute("SELECT detail_json FROM audit_log").fetchall()
    assert all(SECRET not in str(r["detail_json"]) for r in rows)
