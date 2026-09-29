"""命令审批 API 测试（架构计划 C·§6.2）：GET /api/approvals + POST decide。

重点验证安全语义：approve = 后台执行一次并回填；reject = 终态不执行；
非法/二次/不存在都如实回 4xx；**批准还要带这条记录下发的一次性决定令牌**（P0-3 第一步）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.core.approvals import ApprovalService
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


def _insert(client_and_path: tuple, cmd: str, *, cwd: str | None = None) -> dict[str, Any]:
    """经 `ApprovalService` 提交一条待批（绕过工具与 HTTP，只验事实面 + decide 语义）。

    用服务而不是裸 INSERT：决定令牌是**提交时**生成的，裸插会留出一条没有令牌的记录，
    那条在 decide 侧必拒 —— 拿它当正向用例就把测试写成了在测迁移兜底。
    返回整行，`decide_token` 就是决定时要带的那一份。

    cwd=None = 执行落在任务目录根（fixture 已 PUT 的真实目录），后台执行才能成功
    完成 —— 命令必须跑在存在且授权的目录里（安全语义的正向证明）。
    """
    conn = connect(str(client_and_path[1]))
    try:
        return ApprovalService(conn).submit(cmd, cwd=cwd)
    finally:
        conn.close()


_AUTO = object()


def _decide(client: TestClient, approval_id: int, decision: str, *, token: Any = _AUTO):
    """POST decide。默认**先走读侧**取这条记录下发的令牌（和真实界面一样的取法）；
    `token=None` = 干脆不带这个字段，`token="..."` = 带一个指定的（可以是错的）。"""
    body: dict[str, Any] = {"decision": decision}
    if token is _AUTO:
        body["token"] = _token_from_api(client, approval_id)
    elif token is not None:
        body["token"] = token
    return client.post(f"/api/approvals/{approval_id}/decide", json=body)


def _token_from_api(client: TestClient, approval_id: int) -> str | None:
    """读侧（GET /api/approvals）是否把令牌交给页面 —— 决定令牌的唯一来源。"""
    for item in client.get("/api/approvals").json()["items"]:
        if item["id"] == approval_id:
            return item["decide_token"]
    raise AssertionError(f"读侧查不到审批 {approval_id}")


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
    assert _decide(client, rejected["id"], "reject").status_code == 200
    pending = client.get("/api/approvals", params={"status": "pending"})
    body = pending.json()
    assert body["pending"] == 2
    assert {i["status"] for i in body["items"]} == {"pending"}
    assert "echo c" not in [i["command"] for i in body["items"]]


# ---------------------------------------------------------------- decide


def test_decide_reject_is_terminal_no_execution(client: TestClient, db_path: str) -> None:
    row = _insert((client, db_path), "echo reject-me", cwd=None)
    res = _decide(client, row["id"], "reject")
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"
    time.sleep(0.2)  # 给后台池一点时间：若它错跑了，这里会查到 run_command 审计
    rows = _approval_rows(db_path)
    assert rows[0]["status"] == "rejected"


def test_decide_approve_executes_once_and_backfills(client: TestClient, db_path: str) -> None:
    row = _insert((client, db_path), "echo approval-api-ran")
    res = _decide(client, row["id"], "approve")
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
    row = _insert((client, db_path), "echo audit-approval")
    assert _decide(client, row["id"], "approve").status_code == 200
    conn = connect(db_path)
    rows = list(
        conn.execute(
            "SELECT actor, action, detail_json FROM audit_log WHERE target = ?",
            (f"approval:{row['id']}",),
        ).fetchall()
    )
    conn.close()
    assert {r["actor"]: r["action"] for r in rows}["anonymous"] == "approve_command"


def test_decide_invalid_decision_400(client: TestClient, db_path: str) -> None:
    row = _insert((client, db_path), "echo x", cwd=None)
    for bad in ("maybe", "yes", "noo"):
        assert _decide(client, row["id"], bad).status_code == 400


def test_decide_unknown_id_404(client: TestClient) -> None:
    # 不存在的 id 没有读侧可问，所以令牌随手给一个 —— 记录判定排在凭据判定之前。
    assert _decide(client, 99999, "approve", token="irrelevant").status_code == 404


def test_decide_twice_400(client: TestClient, db_path: str) -> None:
    row = _insert((client, db_path), "echo twice", cwd=None)
    assert _decide(client, row["id"], "reject").status_code == 200
    # 第二次连令牌都不用给：状态判定在凭据判定之前（"这条早批过了"比"你没令牌"更有用）。
    assert _decide(client, row["id"], "approve", token=None).status_code == 400


# ------------------------------------------------ 决定令牌（P0-3 第一步：批不动 = 没凭据）


def test_read_side_hands_out_the_decide_token(client: TestClient, db_path: str) -> None:
    """令牌从读侧下发 —— 它是"页面确实看到过这条待批"的凭据，不是配置项。"""
    row = _insert((client, db_path), "echo token-issued")
    issued = _token_from_api(client, row["id"])
    assert issued and issued == row["decide_token"]
    assert _decide(client, row["id"], "approve", token=issued).status_code == 200


def test_decide_without_token_403(client: TestClient, db_path: str) -> None:
    """不带令牌 = 403，且**记录还是 pending**：拒的是这次请求，不是把命令悄悄批/否了。"""
    row = _insert((client, db_path), "echo no-token")
    res = _decide(client, row["id"], "approve", token=None)
    assert res.status_code == 403
    # 断的是"这句话提到了批准凭据"，不是内部字段名 —— 界面会把 detail 原样显示给用户
    # （09-28 轮 A 类：`决定令牌` 是代码里的字段名，用户看不见这个概念）。
    assert "批准凭据" in res.json()["detail"]
    assert _approval_rows(db_path)[0]["status"] == "pending"


def test_decide_with_wrong_token_403(client: TestClient, db_path: str) -> None:
    row = _insert((client, db_path), "echo wrong-token")
    assert _decide(client, row["id"], "approve", token="guessed-000000").status_code == 403
    assert _approval_rows(db_path)[0]["status"] == "pending"


def test_token_cannot_be_reused_after_decide(client: TestClient, db_path: str) -> None:
    """批完令牌即清空：同一份读到的内容批第二次没有凭据可用（也就无可重放的东西）。"""
    row = _insert((client, db_path), "echo one-shot")
    token = _token_from_api(client, row["id"])
    assert _decide(client, row["id"], "approve", token=token).status_code == 200
    assert _token_from_api(client, row["id"]) is None
    # 带着刚用完的令牌再来一次 → 400（已决定），不是"悄悄再执行一遍"。
    assert _decide(client, row["id"], "approve", token=token).status_code == 400
    time.sleep(0.3)
    assert len([r for r in _approval_rows(db_path) if r["command"] == "echo one-shot"]) == 1