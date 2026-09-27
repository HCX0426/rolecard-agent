"""上行同步的端点（M7）：清单只给本人、计划不写任何东西、凭据不外露、导入只落本人名下。

四条各挡一个坏法：

  * **清单按身份过滤** —— `inventory` 是对面会来敲的那一发。它要是把整库的可同步条目都列出来，
    M1~M6 那一整套归属就白做了：不用导入，直接读走别人的会话标题与记忆文本。
  * **计划这一步真的不写** —— 界面上"下一步：看差异"之后用户可能直接关掉页面，
    所以这一发必须是纯读（连对面也是 GET）。
  * **凭据不回显、不落审计** —— 它出现在请求体里，任何一处把它写进响应、日志或
    `audit_log` 都是把"刚输的那枚口令"变成一条可被读出来的记录。
  * **导入只认调用者自己** —— 对面推来的条目落进"这台机器上这个身份"名下，撞别人的 uid 就跳过；
    整份替换必须带显式确认键，且只清选中的类。
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


# -- 对面写入（import）---------------------------------------------------------------


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_MODE", "off")
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"))


def test_import_lands_in_callers_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 推来的东西只进 A 名下；B 的清单里不该出现，重复推也不该长出双份。"""
    client = _app(tmp_path, monkeypatch)
    with client:
        conn = client.app.state.ctx.conn
        conn.execute(
            "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
            "VALUES ('u1', 'local', '第二个')"
        )
        conn.commit()
        body = {
            "items": [
                {
                    "kind": "card",
                    "ident": "pushed",
                    "payload": {"role_id": "pushed", "role_name": "推来的卡", "system_prompt": "x"},
                },
                {"kind": "memory", "ident": "uid-1", "payload": {"text": "用户住在上海"}},
                {
                    "kind": "reachout",
                    "ident": "r|t|h",
                    "payload": {
                        "role_id": "pushed",
                        "role_name": "推来的卡",
                        "text": "她主动说的",
                        "created_at": "2026-09-27 01:00:00",
                    },
                },
            ]
        }
        first = client.post("/api/sync/import", json=body).json()
        assert first["written"] == {"card": 1, "memory": 1, "reachout": 1}, first
        again = client.post("/api/sync/import", json=body).json()
        # 幂等：记忆按 uid 命中（updated，不是多一条），主动消息按三元组跳过
        assert again["written"].get("memory", 0) <= 1
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM role_memory_item WHERE uid = 'uid-1'"
        ).fetchone()
        assert int(rows["n"]) == 1, "重复上行长出了双份记忆"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout WHERE text = '她主动说的'"
        ).fetchone()[0] == 1
        # 换一个身份看：完全不存在
        mine = client.get("/api/sync/inventory").json()["items"]
        assert "pushed" in {str(r["ident"]) for r in mine}
        conn.execute("UPDATE role_card SET user_id = 'u1' WHERE role_id = 'pushed'")
        conn.commit()


def test_import_refuses_to_clear_without_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _app(tmp_path, monkeypatch)
    with client:
        denied = client.post("/api/sync/import", json={"items": [], "clear_kinds": ["card"]})
        assert denied.status_code == 400 and "confirm" in denied.json()["detail"]
        conn = client.app.state.ctx.conn
        before = int(conn.execute("SELECT COUNT(*) AS n FROM role_card").fetchone()[0])
        ok = client.post(
            "/api/sync/import",
            json={"items": [], "clear_kinds": ["card"], "confirm_replace": True},
        ).json()
        assert ok["cleared"].get("card", 0) == before
        assert int(conn.execute("SELECT COUNT(*) AS n FROM role_card").fetchone()[0]) == 0
        # 只清选中的类：内置卡被清掉后，出厂播种不会在同一个请求里复活它们
        assert client.get("/api/roles").json() == []


def test_import_never_overwrites_another_users_row_by_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uid 撞上别人名下的一条 ⇒ 什么都不写（"概率极低"不等于"不检查"）。"""
    from rolecard_agent.core import memory as mem

    client = _app(tmp_path, monkeypatch)
    with client:
        conn = client.app.state.ctx.conn
        conn.execute(
            "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
            "VALUES ('u1', 'local', '第二个')"
        )
        mem.add_item(conn, bucket=mem.GLOBAL_BUCKET, text="他的事实", user_id="u1")
        his_uid = str(conn.execute(
            "SELECT uid FROM role_memory_item WHERE user_id = 'u1'"
        ).fetchone()["uid"])
        conn.commit()
        out = client.post(
            "/api/sync/import",
            json={"items": [{"kind": "memory", "ident": his_uid, "payload": {"text": "改他"}}]},
        ).json()
        assert out["skipped"].get("memory") == 1
        assert any("属于别人" in str(e["error"]) for e in out["errors"]), out["errors"]
        assert conn.execute(
            "SELECT text FROM role_memory_item WHERE uid = ?", (his_uid,)
        ).fetchone()[0] == "他的事实"


def test_conflict_resolved_as_both_keeps_both_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「两份都留」= 对面那条一个字不动 + 本机这条换新 uid 多出来。

    实现成"沿用同一个 uid 去 UPDATE"就等于把它偷偷变成了"按本机的来"，
    而这一档存在的理由正是"不判断谁对"。
    """
    from rolecard_agent.core import memory as mem

    client = _app(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kw: Any) -> Any:
        captured["json"] = kw.get("json")
        return type("R", (), {"status_code": 200, "json": staticmethod(
            lambda: {"written": {"memory": 1}, "skipped": {}, "errors": []})})()

    with client:
        conn = client.app.state.ctx.conn
        mem.add_item(
            conn, bucket=mem.GLOBAL_BUCKET, text="用户住在上海，跑步习惯是每周三次。",
            user_id="local-user",
        )
        conn.commit()
        uid = str(conn.execute(
            "SELECT uid FROM role_memory_item WHERE user_id = 'local-user'"
        ).fetchone()["uid"])
        mine = next(
            row for row in client.get("/api/sync/inventory").json()["items"]
            if row["kind"] == "memory"
        )
        # 对面：同一个 uid、内容不一样 ⇒ 冲突（对面那份的文本只以预览的形式出现）
        theirs = [dict(mine, hash="f" * 16, preview="用户住在上海，最近改成了每周五次。")]
        monkeypatch.setattr("httpx.get", lambda url, **kw: type("R", (), {
            "status_code": 200,
            "json": staticmethod(lambda: {"items": theirs, "skipped": []})})())
        monkeypatch.setattr("httpx.post", fake_post)
        out = client.post(
            "/api/sync/apply",
            json={
                "base_url": "http://cloud.test:8123",
                "user": "local-user",
                "secret": SECRET,
                "kinds": ["memory"],
                "mode": "merge",
                "resolutions": {f"memory:{uid}": "both"},
            },
        ).json()
        assert out["sent"] == 1, out
        assert captured["json"]["items"][0]["payload"]["keep_both"] is True
        # 对面真的收下这一发（对面就是这台机器，走的是同一个 import 端点）
        client.post("/api/sync/import", json=captured["json"])
        rows = conn.execute(
            "SELECT uid, text FROM role_memory_item WHERE user_id = 'local-user'"
        ).fetchall()
        assert len(rows) == 2, [dict(r) for r in rows]
        assert len({str(r["uid"]) for r in rows}) == 2, "两份都留却共用了同一个 uid"
        assert uid in {str(r["uid"]) for r in rows}, "对面那条被换掉了，不是'都留'"


def test_apply_sends_only_what_was_chosen_and_audits_no_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发起方：勾了的类比、裁决成 mine 的冲突才推；审计里不许出现对话原文。"""
    client = _app(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"written": {"memory": 1}, "skipped": {}, "errors": []}

    def fake_get(url: str, **kw: Any) -> _Resp:
        return _Resp()

    def fake_post(url: str, **kw: Any) -> _Resp:
        captured["url"] = url
        captured["json"] = kw.get("json")
        captured["auth"] = kw.get("headers", {}).get("Authorization", "")
        return _Resp()

    with client:
        conn = client.app.state.ctx.conn
        from rolecard_agent.core import memory as mem

        mem.add_item(conn, bucket=mem.GLOBAL_BUCKET, text="用户住在上海", user_id="local-user")
        conn.commit()
        mine = client.get("/api/sync/inventory").json()["items"]
        memory = next(row for row in mine if row["kind"] == "memory")
        theirs = [dict(memory, hash="9" * 16, preview="用户住在苏州")]
        monkeypatch.setattr("httpx.get", lambda url, **kw: type("R", (), {
            "status_code": 200, "json": staticmethod(lambda: {"items": theirs, "skipped": []})})())
        monkeypatch.setattr("httpx.post", fake_post)
        out = client.post(
            "/api/sync/apply",
            json={
                "base_url": "http://cloud.test:8123",
                "user": "local-user",
                "secret": SECRET,
                "kinds": ["memory"],
                "mode": "merge",
                "resolutions": {f"memory:{memory['ident']}": "mine"},
            },
        ).json()
    assert out["sent"] == 1, out
    assert captured["url"].endswith("/api/sync/import")
    assert captured["auth"].startswith("Basic ")
    kinds = {row["kind"] for row in captured["json"]["items"]}
    assert kinds == {"memory"}, "没勾的类比也被推过去了"
    audit = client.get("/api/audit?limit=50").text
    assert "用户住在上海" not in audit and SECRET not in audit
