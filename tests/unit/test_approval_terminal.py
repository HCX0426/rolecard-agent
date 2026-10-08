"""审批执行的终态兜底（`R102-47`）。

`R102-47` 的形状：`run_approval_execution` 跑在**无人监督**的后台线程里，`_audit`/`finish`
写库撞锁（或任何意外）会把异常沉进被弃置的 Future —— 行停在 approved，`run.py` 永远答
"已获批准，正在执行…"，且没有任何 API 能把它推进到 done。修复分三半：

* 异常半边：`run_approval_execution` 的 try/except —— 失败本身落成 done + result.error；
* 崩溃半边：`sweep_interrupted` 开机清扫 —— taskkill /F 没有异常可接，由下一次开机收尾；
* 观察半边：路由侧 Future 的 `add_done_callback`（不碰库，只落 stderr —— 由
  `tests/test_approvals_api.py` 的既有执行用例顺带覆盖它的"不破坏正常路径"）。

变异：摘掉 run_approval_execution 的 except 兜底 ⇒ 前两条红；摘掉 bootstrap 里的
`sweep_interrupted(conn)` 调用 ⇒ 清扫两条红。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rolecard_agent.config import Settings
from rolecard_agent.core.common.approvals import ApprovalService, sweep_interrupted
from rolecard_agent.core.tools import run as run_tools
from rolecard_agent.core.tools.run import RunResult
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    conn = connect(path)
    bootstrap(conn, enabled_domains=("health",))
    conn.commit()
    conn.close()
    return path


def _insert_approved(path: Path, command: str = "echo hi") -> int:
    """直接落一条 approved 行（绕过 submit/decide —— 那两条路径别的用例已经钉过）。"""
    conn = connect(path)
    try:
        svc = ApprovalService(conn)
        row = svc.submit(command, cwd=None, role_id="r1", role_name="医生")
        conn.execute(
            "UPDATE command_approval SET status = 'approved', decide_token = NULL WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
        return int(row["id"])
    finally:
        conn.close()


def _read_row(path: Path, approval_id: int) -> dict[str, object]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT status, result_json FROM command_approval WHERE id = ?",
            (approval_id,),
        ).fetchone()
        assert row is not None
        return {
            "status": row["status"],
            "result": None if not row["result_json"] else json.loads(row["result_json"]),
        }
    finally:
        conn.close()


def test_execute_failure_still_reaches_terminal(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = _insert_approved(db_path)

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(run_tools, "execute_command", boom)
    conn = connect(db_path)
    try:
        run_tools.run_approval_execution(
            approval_id, settings=Settings(), conn=conn
        )
    finally:
        conn.close()
    row = _read_row(db_path, approval_id)
    assert row["status"] == "done"
    result = row["result"]
    assert isinstance(result, dict) and "boom" in str(result.get("error"))


def test_audit_write_failure_still_reaches_terminal(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`R102-47` 的原始现场：finish/_audit 撞上持久写事务（OperationalError）也要达终态。"""
    approval_id = _insert_approved(db_path)

    def locked(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        run_tools,
        "execute_command",
        lambda *a, **k: RunResult(exit_code=0, output="ok", output_bytes=2, duration_ms=1),
    )
    monkeypatch.setattr(run_tools, "_audit", locked)
    conn = connect(db_path)
    try:
        run_tools.run_approval_execution(approval_id, settings=Settings(), conn=conn)
    finally:
        conn.close()
    row = _read_row(db_path, approval_id)
    assert row["status"] == "done"
    result = row["result"]
    assert isinstance(result, dict) and "locked" in str(result.get("error"))


def test_success_path_unaffected(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底不许把正常执行也改写：成功时 result 里没有 error 键。"""
    approval_id = _insert_approved(db_path)
    monkeypatch.setattr(
        run_tools,
        "execute_command",
        lambda *a, **k: RunResult(exit_code=0, output="ok", output_bytes=2, duration_ms=1),
    )
    conn = connect(db_path)
    try:
        run_tools.run_approval_execution(approval_id, settings=Settings(), conn=conn)
    finally:
        conn.close()
    row = _read_row(db_path, approval_id)
    assert row["status"] == "done"
    result = row["result"]
    assert isinstance(result, dict) and "error" not in result


def test_bootstrap_sweep_collects_interrupted(db_path: Path) -> None:
    approval_id = _insert_approved(db_path)
    conn = connect(db_path)
    try:
        # 同库里的其他状态不许被扫：pending 是活队列，rejected/done 是终态。
        svc = ApprovalService(conn)
        pending = svc.submit("echo pending-one", cwd=None)
        swept = sweep_interrupted(conn)
        assert swept >= 1
        row = _read_row(db_path, approval_id)
        assert row["status"] == "done"
        result = row["result"]
        assert isinstance(result, dict) and "丢失" in str(result.get("error"))
        after = svc.latest("echo pending-one")
        assert after is not None and after["id"] == pending["id"]
        assert after["status"] == "pending"
        # 幂等：第二次清扫无事可做。
        assert sweep_interrupted(conn) == 0
    finally:
        conn.close()
