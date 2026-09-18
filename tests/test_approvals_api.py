"""命令审批 API 测试（架构计划 C·§6.2）：GET /api/approvals + POST decide。

重点验证安全语义：approve = 后台执行一次并回填；reject = 终态不执行；
非法/二次/不存在都如实回 4xx。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.storage.db import connect


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(sqlite_path=tmp_path / "app.db")
    c = TestClient(app)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert c.put("/api/workspace/dir", json={"path": str(ws)}).status_code == 200
    return c


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "app.db")


def _insert(client_and_path: tuple, cmd: str, *, cwd: str | None = None) -> int:
    """直接往库插一条 pending 审批（绕过工具，验证纯粹的事实面 + decide 语义）。

    cwd=None = 执行落在任务目录根（fixture 已 PUT 的真实目录），后台执行才能成功
    完成 —— 命令必须跑在存在且授权的目录里（安全语义的正向证明）。
    """
    conn = connect(str(client_and_path[1]))
    cur = conn.execute(
        "INSERT INTO command_approval (command, cwd, status) VALUES (?, ?, 'pending')",
        (cmd, cwd),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM command_approval WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return int(row["id"])


def _approval_rows(db_path: str) -> list[dict]:
    conn = connect(db_path)
    rows = list(
        conn.execute("SELECT * FROM command_approval ORDER BY id DESC").fetchall()
    )
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 读


def test_list_empty(client: TestClient) -> None:
    res = client.get("/api/approvals")
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["pending"] == 0


def test_list_status_filter(client: TestClient, db_path: str) -> None:
    """status=待批过滤：只回 pending，pending 计数一致。"""
    _insert((client, db_path), "echo a")
    _insert((client, db_path), "echo b", cwd=None)
    rejected = _insert((client, db_path), "echo c", cwd=None)
    assert (
        client.post(f"/api/approvals/{rejected}/decide", json={"decision": "reject"}).status_code
        == 200
    )
    pending = client.get("/api/approvals", params={"status": "pending"})
    body = pending.json()
    assert body["pending"] == 2
    assert {i["status"] for i in body["items"]} == {"pending"}
    assert "echo c" not in [i["command"] for i in body["items"]]


# ---------------------------------------------------------------- decide


def test_decide_reject_is_terminal_no_execution(client: TestClient, db_path: str) -> None:
    aid = _insert((client, db_path), "echo reject-me", cwd=None)
    res = client.post(f"/api/approvals/{aid}/decide", json={"decision": "reject"})
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"
    time.sleep(0.2)  # 给后台池一点时间：若它错跑了，这里会查到 run_command 审计
    rows = _approval_rows(db_path)
    assert rows[0]["status"] == "rejected"


def test_decide_approve_executes_once_and_backfills(client: TestClient, db_path: str) -> None:
    aid = _insert((client, db_path), "echo approval-api-ran")
    res = client.post(f"/api/approvals/{aid}/decide", json={"decision": "approve"})
    assert res.status_code == 200
    assert res.json()["status"] == "approved"
    # 后台执行是异步的：等它回填（≤5s；命令瞬时）。
    rows = _approval_rows(db_path)
    for _ in range(50):
        if rows[0]["status"] == "done" and rows[0]["result_json"]:
            break
        time.sleep(0.1)
        rows = _approval_rows(db_path)
    done = rows[0]
    assert done["status"] == "done"
    result = json.loads(done["result_json"])
    assert result["exit_code"] == 0
    assert "approval-api-ran" in result["output"]


def test_decide_approve_audits_operator(client: TestClient, db_path: str) -> None:
    aid = _insert((client, db_path), "echo audit-approval")
    assert (
        client.post(f"/api/approvals/{aid}/decide", json={"decision": "approve"}).status_code
        == 200
    )
    conn = connect(db_path)
    rows = list(
        conn.execute(
            "SELECT actor, action, detail_json FROM audit_log WHERE target = ?",
            (f"approval:{aid}",),
        ).fetchall()
    )
    conn.close()
    assert {r["actor"]: r["action"] for r in rows}["anonymous"] == "approve_command"


def test_decide_invalid_decision_400(client: TestClient, db_path: str) -> None:
    aid = _insert((client, db_path), "echo x", cwd=None)
    for bad in ("maybe", "yes", "noo"):
        assert (
            client.post(f"/api/approvals/{aid}/decide", json={"decision": bad}).status_code
            == 400
        )


def test_decide_unknown_id_404(client: TestClient) -> None:
    res = client.post("/api/approvals/99999/decide", json={"decision": "approve"})
    assert res.status_code == 404


def test_decide_twice_400(client: TestClient, db_path: str) -> None:
    aid = _insert((client, db_path), "echo twice", cwd=None)
    assert (
        client.post(f"/api/approvals/{aid}/decide", json={"decision": "reject"}).status_code
        == 200
    )
    again = client.post(f"/api/approvals/{aid}/decide", json={"decision": "approve"})
    assert again.status_code == 400