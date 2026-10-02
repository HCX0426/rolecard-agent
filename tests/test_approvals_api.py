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

def test_each_accepted_approve_submits_exactly_one_execution(
    client: TestClient, db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`api/routers/approvals.py:4` 那句「approve = 后台执行一次」的**另一半**。

    `tests/unit/test_approval_race.py` 钉的是"并发里只有一个赢家"；这条钉的是路由那一侧的
    等式：**提交给后台池的次数 == 被接受的决定次数**。两者合起来才是"那条命令只跑一遍"——
    少了这一条，赢家再多也可能一发 200 提交两次执行。
    """
    from rolecard_agent.core.tools import run as run_tools

    submitted: list[int] = []

    class _SpyFuture:
        """路由在 submit 之后会挂观察回调（`R102-47`）—— 桩只需要长 Future 的那个形状。"""

        def add_done_callback(self, callback: object) -> None:  # noqa: ARG002
            return None

    class _SpyExecutor:
        def submit(self, *args: Any, **kwargs: Any) -> _SpyFuture:
            # 不认参数位次，只认那一枚整数 id —— 因为**第一个实参现在是"带上下文的壳"**
            # （`copy_context().run`，`R102-03` 的第二道缝），把它写进签名就等于把修法写死。
            submitted.append(next(int(a) for a in args if isinstance(a, int)))
            return _SpyFuture()

    monkeypatch.setattr(run_tools, "_APPROVAL_EXECUTOR", _SpyExecutor())

    row = _insert((client, db_path), "echo submit-count")
    token = _token_from_api(client, row["id"])
    assert _decide(client, row["id"], "approve", token=token).status_code == 200
    assert submitted == [row["id"]], f"第一发批准提交了 {submitted}"
    # 第二次带同一枚（刚用完的）令牌：400，且**不许再往池里丢一次**
    assert _decide(client, row["id"], "approve", token=token).status_code == 400
    assert submitted == [row["id"]], f"被拒的那一发也安排了一次执行：{submitted}"
    # 拒绝永远不执行
    other = _insert((client, db_path), "echo reject-no-run")
    assert _decide(client, other["id"], "reject").status_code == 200
    assert submitted == [row["id"]], f"拒绝不该提交执行，但提交了：{submitted}"

def test_the_approval_execution_thread_sees_the_request_epoch(
    client: TestClient, db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台执行那条线程也得看见库代际（`R102-03` 的第二道缝，同 `api/chat.py` 那一处）。

    `pool.submit` 与 `run_in_executor` 一样**不**传播 contextvar —— 不带上下文，
    `_APPROVAL_EXECUTOR` 里那两条线程的代际一辈子不变，`ThreadLocalConnection._current()`
    那句"新请求先回滚上次残留事务"在他们身上从不调用，于是执行完留下的未提交事务
    会把写锁占满到那条线程再次被用到为止。这里不真跑命令，只问工作线程读到的代际。
    """
    import threading
    import time as _t

    from rolecard_agent.core.tools import run as run_tools
    from rolecard_agent.storage.db import _REQUEST_EPOCH

    seen: list[str] = []
    done = threading.Event()

    def _recorder(approval_id: int, **_kw: Any) -> None:  # 顶掉真执行，只量上下文
        seen.append(_REQUEST_EPOCH.get())
        done.set()

    monkeypatch.setattr(run_tools, "run_approval_execution", _recorder)
    row = _insert((client, db_path), "echo epoch")
    assert _decide(client, row["id"], "approve").status_code == 200
    assert done.wait(10), "后台那一发根本没被调用"
    assert seen and seen[0], (
        "后台执行线程读到的库代际是空串 —— 上下文没跟着 submit 送过去，"
        "那条线程的残留事务清理永不触发"
    )
    _t.sleep(0.05)


def test_on_mode_token_reachability_and_operator_decides_by_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`R102-46`：on 档下列表对使用者角色只给 `has_token` 布尔、不再下发令牌本身
    （读得到列表 ≠ 持有批准能力）；operator 拍板凭凭据、不必持令牌。

    变异：把 list_approvals 的 include_token 摘成恒 True ⇒ 前半红；
    把 decide 的 operator 分支摘掉 ⇒ 后半红。
    """
    monkeypatch.setenv("AUTH_MODE", "on")
    # 凭证分族：`operator:` 前缀 = 操作员；无前缀 = 使用者（没有 "user:" 这种前缀）。
    monkeypatch.setenv("AUTH_CREDENTIALS", "operator:bob:pw,alice:pw")
    app = create_app(sqlite_path=tmp_path / "auth.db")
    c = TestClient(app)

    row = _insert((c, tmp_path / "auth.db"), "echo on-mode-token")
    assert row["decide_token"], "提交时生成的令牌必须在库里"

    # 匿名：on 档一律 401。
    assert c.get("/api/approvals").status_code == 401
    # 使用者角色：列表是 user 面（侧栏红点要用），读得到 —— 但令牌不再跟着来：
    # 行里只有 has_token 布尔，"读得到列表 = 持有批准能力"的等式在 R102-46 断掉。
    user = {"Authorization": "Basic YWxpY2U6cHc="}  # alice:pw
    items = c.get("/api/approvals", headers=user).json()["items"]
    target = next(i for i in items if i["id"] == row["id"])
    assert "decide_token" not in target
    assert target["has_token"] is True
    res = c.post(
        f"/api/approvals/{row['id']}/decide",
        json={"decision": "approve", "token": row["decide_token"]},
        headers=user,
    )
    assert res.status_code == 403

    # 操作员：读列表带令牌与 has_token；拍板**凭凭据**、不带令牌 —— 200 且进 approved。
    operator = {"Authorization": "Basic Ym9iOnB3"}  # bob:pw
    items = c.get("/api/approvals", headers=operator).json()["items"]
    target = next(i for i in items if i["id"] == row["id"])
    assert target["decide_token"] == row["decide_token"]
    assert target["has_token"] is True
    res = c.post(
        f"/api/approvals/{row['id']}/decide",
        json={"decision": "approve"},
        headers=operator,
    )
    assert res.status_code == 200
    assert res.json()["status"] == "approved"

def test_off_mode_list_still_carries_token_for_the_local_ui(
    client: TestClient, db_path: str
) -> None:
    """off 档的本机 UI 不许被误伤：读侧照发令牌（护栏 + 确认点击 + 令牌，三样缺一不可）。"""
    row = _insert((client, db_path), "echo off-token")
    items = client.get("/api/approvals").json()["items"]
    target = next(i for i in items if i["id"] == row["id"])
    assert target["decide_token"] == row["decide_token"]
    assert target["has_token"] is True
