"""会话列表 / 历史消息 / 删除会话 端点测试（M5 对话页侧栏的后端）。  Traceability: US-9.

三个决定性断言：
  1. 首轮消息自动生成会话标题，且 updated_at 刷新把最近活跃的会话顶到列表最前；
  2. 历史消息从 checkpoint 回放（不是单独的聊天记录表），多轮顺序正确；
  3. 删除会话时 thread 行与 langgraph 的 checkpoints / writes **一起**清掉 —— 留下孤儿
     checkpoint 意味着"删掉的会话还能被 checkpoint 复活"，这是必须堵上的口子。

全部离线：ScriptedChat 注入。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from tests.conftest import ScriptedChat


@pytest.fixture
def model() -> ScriptedChat:
    return ScriptedChat([AIMessage(content=f"reply-{i}") for i in range(10)])


@pytest.fixture
def client(tmp_path: Path, model: ScriptedChat) -> Iterator[TestClient]:
    app = create_app(sqlite_path=tmp_path / "app.db", model=model)
    with TestClient(app) as c:
        yield c


def parse_sse(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def chat(client: TestClient, thread_id: str, message: str) -> list[dict[str, object]]:
    res = client.post("/api/chat", json={"thread_id": thread_id, "message": message})
    assert res.status_code == 200
    return parse_sse(res.text)


def authoritative_text(events: list[dict[str, object]]) -> str:
    replace = [e for e in events if e["type"] == "message_replace"]
    return str(replace[-1]["text"]) if replace else ""


def test_first_message_sets_title_and_bumps_session_to_top(
    client: TestClient,
) -> None:
    """US-9：首轮消息生成标题；活跃会话按毫秒级 updated_at 排到列表最前。"""
    older = client.post("/api/session", json={}).json()
    newer = client.post("/api/session", json={}).json()

    sessions = client.get("/api/sessions").json()
    # 同一秒内创建的两个会话，秒级时间戳会打平——这里只断言集合与标题，
    # 严格顺序断言放在聊天之后（毫秒级 updated_at 保证确定性）。
    assert {s["thread_id"] for s in sessions} == {
        newer["thread_id"],
        older["thread_id"],
    }
    assert all(s["title"] is None for s in sessions)

    chat(client, str(older["thread_id"]), "帮我查一下结石直径")

    sessions = client.get("/api/sessions").json()
    assert sessions[0]["thread_id"] == older["thread_id"]  # 活跃者置顶
    assert sessions[0]["title"] == "帮我查一下结石直径"  # 24 字内全量截取
    assert sessions[0]["role_name"] == "健康档案管理员"


def test_long_message_title_is_truncated(client: TestClient) -> None:
    session = client.post("/api/session", json={}).json()
    chat(client, str(session["thread_id"]), "这是一条很长很长的消息，远远超过二十四个字的截取边界")
    sessions = client.get("/api/sessions").json()
    assert len(str(sessions[0]["title"])) == 24


def test_messages_replay_from_checkpoint_in_order(client: TestClient) -> None:
    """历史消息来自 checkpoint：两轮对话后回放顺序为 user/ai × 2。"""
    session = client.post("/api/session", json={}).json()
    tid = str(session["thread_id"])
    assert client.get(f"/api/session/{tid}/messages").json() == []  # 新会话无历史

    chat(client, tid, "第一问")
    chat(client, tid, "第二问")

    messages = client.get(f"/api/session/{tid}/messages").json()
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "第一问"),
        ("assistant", "reply-0"),
        ("user", "第二问"),
        ("assistant", "reply-1"),
    ]


def test_messages_unknown_thread_404(client: TestClient) -> None:
    assert client.get("/api/session/nope/messages").status_code == 404


def test_delete_session_removes_thread_and_checkpoints(client: TestClient, tmp_path: Path) -> None:
    """US-9：删除会话不留孤儿 —— thread 行与 checkpoint/writes 同时消失。"""
    session = client.post("/api/session", json={}).json()
    tid = str(session["thread_id"])
    chat(client, tid, "留过历史的会话")

    # 删除前确认 checkpoint 里确实有这个线程（前置自检）
    db = sqlite3.connect(tmp_path / "app.db")
    before = db.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (tid,)).fetchone()[
        0
    ]
    db.close()
    assert before > 0

    res = client.delete(f"/api/session/{tid}")
    assert res.status_code == 204

    assert client.get(f"/api/session/{tid}").status_code == 404
    assert client.get(f"/api/session/{tid}/messages").status_code == 404
    assert all(s["thread_id"] != tid for s in client.get("/api/sessions").json())

    db = sqlite3.connect(tmp_path / "app.db")
    left = db.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (tid,)).fetchone()[0]
    left_writes = db.execute("SELECT COUNT(*) FROM writes WHERE thread_id = ?", (tid,)).fetchone()[
        0
    ]
    db.close()
    assert left == 0 and left_writes == 0  # 没有可被"复活"的孤儿 checkpoint


def test_delete_unknown_session_404(client: TestClient) -> None:
    assert client.delete("/api/session/nope").status_code == 404


def test_session_list_survives_role_deletion(client: TestClient) -> None:
    """会话指向的角色被删后，列表仍在且 role_name 降级为 None（不 500）。"""
    client.post(
        "/api/roles",
        json={"role_id": "temp", "role_name": "临时", "system_prompt": "x"},
    )
    session = client.post("/api/session", json={"role_id": "temp"}).json()
    assert client.delete("/api/roles/temp").status_code == 204

    sessions = client.get("/api/sessions").json()
    row = next(s for s in sessions if s["thread_id"] == session["thread_id"])
    assert row["role_id"] == "temp" and row["role_name"] is None


# -- upload（US-7 上传入口的真实落点） --------------------------------------------------


def test_upload_registers_task_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-7 / A1：上传 → 登记 intake 任务；同一文件再传复用同一任务（不产生重复行）。"""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(sqlite_path=tmp_path / "up.db", model=ScriptedChat([]))
    with TestClient(app) as c:
        session = c.post("/api/session", json={}).json()
        tid = str(session["thread_id"])

        first = c.post(
            f"/api/session/{tid}/upload",
            files={"file": ("体检报告.pdf", b"%PDF-1.4 fake scan", "application/pdf")},
        )
        assert first.status_code == 201
        body = first.json()
        assert body["task_id"].startswith("ing_") and body["reused"] is False

        again = c.post(
            f"/api/session/{tid}/upload",
            files={"file": ("体检报告.pdf", b"%PDF-1.4 fake scan", "application/pdf")},
        )
        assert again.json()["reused"] is True
        assert again.json()["task_id"] == body["task_id"]  # 同一任务，不是新行

        # 注入的说明消息进入 checkpoint 历史，模型后续轮次能看到
        messages = c.get(f"/api/session/{tid}/messages").json()
        assert any("上传了报告文件" in str(m["content"]) for m in messages)


def test_upload_rejects_empty_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(sqlite_path=tmp_path / "up2.db", model=ScriptedChat([]))
    with TestClient(app) as c:
        session = c.post("/api/session", json={}).json()
        res = c.post(
            f"/api/session/{session['thread_id']}/upload",
            files={"file": ("a.txt", b"", "text/plain")},
        )
        assert res.status_code == 400


def test_upload_unknown_thread_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(sqlite_path=tmp_path / "up3.db", model=ScriptedChat([]))
    with TestClient(app) as c:
        res = c.post(
            "/api/session/nope/upload",
            files={"file": ("a.txt", b"data", "text/plain")},
        )
        assert res.status_code == 404
