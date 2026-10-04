"""同步 roundtrip 的**按 kind 矩阵**（2026-10-04 审查快照的测试防线条目）。

inventory 有几个 kind，就必须有几个 kind 各自的端到端往返 —— 从前 roundtrip 是
按"上次出过事故的 kind"手工补的（卡是 3/3 全灭后才补的），`_write_thread` 整段
替换——唯一破坏性改写目标端历史的路径——零覆盖，覆盖率缺失行就是它全体。

矩阵对每个 kind 钉三件事：
  1. 源侧 export 交出的载荷，对面 import **收得下**（written ≥1，errors 为空）；
  2. 原样再导一遍 ⇒ **不立双份**（各 kind 的幂等语义不同：card/reachout 是 skipped，
     memory/thread 是按 uid / 整段替换的 "updated" —— 统一断言行数不涨 + errors 空）；
  3. thread 额外钉：消息按 user/assistant 顺序回放、他人 thread 回 "foreign"。

全部走端点（export/import），不 import 私有拼装函数；模型用 ScriptedChat 注入，离线。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from tests.conftest import ScriptedChat

KINDS = ("card", "memory", "reachout", "thread")


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> TestClient:
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / f"data-{name}"))
    app = create_app(
        sqlite_path=tmp_path / name,
        model=ScriptedChat([AIMessage(content="她说的话")]),
    )
    return TestClient(app)


def _seed_all_kinds(client: TestClient) -> dict[str, str]:
    """四类各造一条，返回 {kind: ident}（导出侧的 ident 形状各不相同）。"""
    made = client.post(
        "/api/roles", json={"role_id": "rt_card", "role_name": "矩阵卡", "system_prompt": "s"}
    )
    assert made.status_code == 201, made.text
    tid = str(client.post("/api/session", json={"role_id": "rt_card"}).json()["thread_id"])
    chatted = client.post("/api/chat", json={"thread_id": tid, "message": "我说的"})
    assert chatted.status_code == 200, chatted.text
    conn = client.app.state.ctx.conn
    conn.execute(
        "INSERT INTO role_memory_item (role_id, user_id, uid, text) "
        "VALUES ('rt_card', 'local-user', 'rt-uid-1', '矩阵记忆条目')"
    )
    conn.execute(
        "INSERT INTO agent_reachout (user_id, role_id, role_name, text, fired_by, state) "
        "VALUES ('local-user', 'rt_card', '矩阵卡', '她主动说的一句', 'affection', 'unread')"
    )
    conn.commit()
    return {"card": "rt_card", "memory": "rt-uid-1", "reachout": "rt_card|", "thread": tid}


def _export_all(client: TestClient) -> dict[str, list[dict[str, Any]]]:
    """导出全部条目，按 kind 分组（同 kind 可能多条：内置卡等）。"""
    inventory = client.get("/api/sync/inventory").json()["items"]
    idents = [{"kind": str(r["kind"]), "ident": str(r["ident"])} for r in inventory]
    exported = client.post("/api/sync/export", json={"idents": idents}).json()["items"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in exported:
        grouped.setdefault(str(item["kind"]), []).append(item)
    return grouped


def _pick(grouped: dict[str, list[dict[str, Any]]], kind: str, ident: str) -> dict[str, Any]:
    """按 ident 形状挑出**我们播种的那条**（不是内置卡之类的旁人）。"""
    for item in grouped.get(kind, []):
        got = str(item["ident"])
        if got == ident or (ident.endswith("|") and got.startswith(ident)):
            return item
    raise AssertionError(
        f"kind={kind} ident 前缀 {ident!r} 没在导出载荷里找到："
        f"{[str(i['ident']) for i in grouped.get(kind, [])]}"
    )


def _peer_row_count(client: TestClient, kind: str, ident: str) -> int:
    conn = client.app.state.ctx.conn
    if kind == "card":
        return int(conn.execute(
            "SELECT COUNT(*) FROM role_card WHERE role_id = ?", (ident,)
        ).fetchone()[0])
    if kind == "memory":
        return int(conn.execute(
            "SELECT COUNT(*) FROM role_memory_item WHERE uid = ?", (ident,)
        ).fetchone()[0])
    if kind == "reachout":
        return int(conn.execute(
            "SELECT COUNT(*) FROM agent_reachout WHERE role_id = ? AND text = '她主动说的一句'",
            (ident.split("|", 1)[0],),
        ).fetchone()[0])
    return int(conn.execute(
        "SELECT COUNT(*) FROM session_thread WHERE thread_id = ?", (ident,)
    ).fetchone()[0])


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_roundtrips_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    src = _app(tmp_path, monkeypatch, f"src-{kind}-{uuid.uuid4().hex[:6]}.db")
    dst = _app(tmp_path, monkeypatch, f"dst-{kind}-{uuid.uuid4().hex[:6]}.db")
    with src, dst:
        idents = _seed_all_kinds(src)
        item = _pick(_export_all(src), kind, idents[kind])
        payload = [{"kind": kind, "ident": item["ident"], "payload": item["payload"]}]
        first = dst.post("/api/sync/import", json={"items": payload}).json()
        assert first["errors"] == [], f"kind={kind} 导入报错：{first}"
        assert first["written"].get(kind, 0) == 1, f"kind={kind} 没写进对面：{first}"
        second = dst.post("/api/sync/import", json={"items": payload}).json()
        assert second["errors"] == [], f"kind={kind} 第二遍导入报错：{second}"
        assert _peer_row_count(dst, kind, item["ident"]) == 1, (
            f"kind={kind} 原样重导立了双份（幂等破了）：first={first} second={second}"
        )


def test_thread_import_replays_messages_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """thread 的整段替换必须按序回放消息 —— 迁移后历史乱序等于丢历史。"""
    src = _app(tmp_path, monkeypatch, "thr-src.db")
    dst = _app(tmp_path, monkeypatch, "thr-dst.db")
    with src, dst:
        idents = _seed_all_kinds(src)
        item = _pick(_export_all(src), "thread", idents["thread"])
        out = dst.post(
            "/api/sync/import",
            json={"items": [{"kind": "thread", "ident": item["ident"],
                             "payload": item["payload"]}]},
        ).json()
        assert out["errors"] == [] and out["written"].get("thread") == 1, out
        messages = dst.get(f"/api/session/{item['ident']}/messages").json()["messages"]
        roles = [str(m["role"]) for m in messages]
        assert roles == ["user", "assistant"], roles
        texts = [str(m["content"]) for m in messages]
        assert "我说的" in texts[0] and "她说的话" in texts[1], texts


def test_importing_another_users_thread_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """他人名下的 thread：import 必须回 "foreign" 折进 errors，而不是写进来。"""
    src = _app(tmp_path, monkeypatch, "for-src.db")
    dst = _app(tmp_path, monkeypatch, "for-dst.db")
    with src, dst:
        idents = _seed_all_kinds(src)
        conn = dst.app.state.ctx.conn
        conn.execute(
            "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
            "VALUES ('u9', 'local', '别人')"
        )
        item = _pick(_export_all(src), "thread", idents["thread"])
        conn.execute(
            "INSERT INTO session_thread (thread_id, user_id, title, current_role_id) "
            "VALUES (?, 'u9', '对面的', 'general_assistant')",
            (str(item["ident"]),),
        )
        conn.commit()
        out = dst.post(
            "/api/sync/import",
            json={"items": [{"kind": "thread", "ident": item["ident"],
                             "payload": item["payload"]}]},
        ).json()
        assert out["written"].get("thread", 0) == 0, out
        assert any(e.get("error") == "这条身份已经属于别人" for e in out["errors"]), out
