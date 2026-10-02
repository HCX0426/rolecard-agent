"""并发决定同一条待批 —— `R102-01` 的那次"读—判—写"。

契约写在 `api/routers/approvals.py:4`：**approve = 后台执行一次**。而从前 `decide` 是
"SELECT status 判 pending → UPDATE ... WHERE id = ?"，两个读者可以同时读到 pending、
同时持有同一枚令牌（令牌在**决定之后**才清 NULL），于是两发都过校验、都回 200，
而路由按"每个 200 提交一次执行"办事 ⇒ 那条命令真跑两遍（删档、发消息、跑脚本都是两遍）。

这里把那条交错做成**确定性的**：`_check_token` 是写之前的最后一步，用一道 barrier 让
N 个线程全部走到那一步、再同时放行 —— 不靠睡眠时长撞运气。每个线程各开一条自己的连接
（生产形态就是 `ThreadLocalConnection`：一条连接一个线程），谁也不借谁的句柄。

不 spawn 子进程：执行本体在 `test_run_command.py`，这里只数"谁赢"，因为
**赢几个 = 路由提交几次执行 = 那条命令跑几遍**。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from rolecard_agent.core.approvals import (
    ApprovalAlreadyDecided,
    ApprovalService,
)
from rolecard_agent.storage.db import bootstrap, connect

THREADS = 4


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    conn = connect(path)
    bootstrap(conn, enabled_domains=("health",))
    conn.commit()
    conn.close()
    return path


def _submit_one(path: Path) -> dict[str, Any]:
    conn = connect(path)
    try:
        return ApprovalService(conn).submit(
            "python drop_tables.py", cwd="/task", role_id="r1", role_name="医生"
        )
    finally:
        conn.close()


def _decide_gated(path: Path, approval_id: int, decision: str, token: str, threads: int,
                  monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`threads` 个线程同时决定同一条：每个都走到"写完就放行"那道门，然后一起冲出去。"""
    barrier = threading.Barrier(threads)
    original = ApprovalService._check_token

    def gated(approval: int, expected: str | None, given: str | None, age: float) -> None:
        original(approval, expected, given, age)
        barrier.wait(timeout=15)

    monkeypatch.setattr(ApprovalService, "_check_token", staticmethod(gated))

    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        conn = connect(path)
        try:
            ApprovalService(conn).decide(approval_id, decision, token=token)
            tag = "ok"
        except ApprovalAlreadyDecided:
            tag = "lost"
        except BaseException as exc:  # noqa: BLE001 - 别的失败形态必须如实进结果，不许静默
            tag = f"other:{type(exc).__name__}"
        finally:
            conn.close()
        with lock:
            results.append(tag)

    group = [threading.Thread(target=worker) for _ in range(threads)]
    for t in group:
        t.start()
    for t in group:
        t.join(timeout=30)
    return results


def _status(path: Path, approval_id: int) -> tuple[str, Any]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT status, decide_token FROM command_approval WHERE id = ?", (approval_id,)
        ).fetchone()
    finally:
        conn.close()
    return str(row["status"]), row["decide_token"]


def test_four_concurrent_approves_admit_exactly_one(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    path = _db(tmp_path)
    row = _submit_one(path)
    results = _decide_gated(path, int(row["id"]), "approve", str(row["decide_token"]),
                            THREADS, monkeypatch)

    assert not [r for r in results if r.startswith("other:")], f"有线程抛了别的错：{results}"
    assert results.count("ok") == 1, (
        f"同一条待批被 {results.count('ok')} 发并发批准接受 —— 每一发都会让路由"
        f"提交一次真执行（读—判—写没有做成一步）：{results}"
    )
    assert results.count("lost") == THREADS - 1, results
    status, token = _status(path, int(row["id"]))
    assert status == "approved", status
    assert token is None, f"决定之后令牌必须清掉，否则同一枚凭据还能再用：{token}"


def test_approve_and_reject_racing_leave_one_winner(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """批准与拒绝同时到：只能有一个兑现。

    这一对比"两发批准"更疼：她点的是「拒绝」，而另一处（第二个页签、第二次点击）同一秒
    点了「批准」——两个都算数的话，被拒的那条命令照样跑了。
    """
    path = _db(tmp_path)
    row = _submit_one(path)
    token = str(row["decide_token"])
    approval_id = int(row["id"])

    barrier = threading.Barrier(2)
    original = ApprovalService._check_token

    def gated(approval: int, expected: str | None, given: str | None, age: float) -> None:
        original(approval, expected, given, age)
        barrier.wait(timeout=15)

    monkeypatch.setattr(ApprovalService, "_check_token", staticmethod(gated))
    outcomes: list[str] = []

    def worker(decision: str) -> None:
        conn = connect(path)
        try:
            ApprovalService(conn).decide(approval_id, decision, token=token)
            tag = decision
        except ApprovalAlreadyDecided:
            tag = "lost"
        finally:
            conn.close()
        outcomes.append(tag)

    group = [threading.Thread(target=worker, args=(d,)) for d in ("approve", "reject")]
    for t in group:
        t.start()
    for t in group:
        t.join(timeout=30)

    assert outcomes.count("lost") == 1, f"两发都算数了：{outcomes}"
    winner = [o for o in outcomes if o != "lost"]
    assert winner in (["approve"], ["reject"]), outcomes
    expected = "approved" if winner == ["approve"] else "rejected"
    assert _status(path, approval_id)[0] == expected, (winner, _status(path, approval_id))

def test_a_cas_loser_does_not_leave_the_database_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """输在 CAS 上的那一发**不许把写锁留在库里**（修 `R102-01` 时自己长出来的坑）。

    `UPDATE ... WHERE status='pending'` 即使改到 0 行，也已经把那条连接推进了一个写事务
    （SQLite 在写语句前隐式 BEGIN；实测 `in_transaction=True`，而第二条连接连
    `PRAGMA journal_mode` 都会 `database is locked`）。输家走的是抛异常那一路、不会
    `commit()` —— 线程池里的线程不死、那条连接也不死（库代际送不进池线程 = `R102-03`），
    这把 RESERVED 锁就没有来路可解：本轮修完并发批准之后，探针正是这么在第 3 轮卡住的。

    所以这里让赢家与输家**都从 barrier 一起冲出去**（输家必须真的输在 CAS 上，
    而不是在读侧就被挡回来），并且**故意不关**那些连接 —— `close()` 会隐式回滚，
    把这个坑抹平，测出来的是假的。
    """
    path = _db(tmp_path)
    row = _submit_one(path)
    approval_id, token = int(row["id"]), str(row["decide_token"])

    barrier = threading.Barrier(2)
    original = ApprovalService._check_token

    def gated(approval: int, expected: str | None, given: str | None, age: float) -> None:
        original(approval, expected, given, age)
        barrier.wait(timeout=15)

    monkeypatch.setattr(ApprovalService, "_check_token", staticmethod(gated))

    outcomes: list[str] = []
    leaked: list[Any] = []          # 故意留着不关
    lock = threading.Lock()

    def worker(decision: str) -> None:
        conn = connect(path)
        with lock:
            leaked.append(conn)
        try:
            ApprovalService(conn).decide(approval_id, decision, token=token)
            tag = "ok"
        except ApprovalAlreadyDecided:
            tag = "lost"
        except BaseException as exc:  # noqa: BLE001
            tag = f"other:{type(exc).__name__}"
        with lock:
            outcomes.append(tag)

    group = [threading.Thread(target=worker, args=(d,)) for d in ("approve", "reject")]
    for th in group:
        th.start()
    for th in group:
        th.join(timeout=30)

    assert outcomes.count("lost") == 1, f"这一趟没有输在 CAS 上的那一发：{outcomes}"

    # 第三者必须写得动：写锁不该因为"有人来晚了"而被留在库里
    checker = connect(path)
    try:
        checker.execute(
            "UPDATE command_approval SET result_json = ? WHERE id = ?",
            ('{"probe": true}', approval_id),
        )
        checker.commit()
    finally:
        checker.close()
    for conn in leaked:
        conn.close()
