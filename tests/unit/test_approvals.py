"""ApprovalService（命令审批状态机，架构计划 C·§6.2）的单元测试。

覆盖审批的事实面：提交的幂等、规范化比较、决定与回填、状态过滤。
执行本体在 test_run_command.py —— 本文件里绝不 spawn 子进程。
"""

from __future__ import annotations

from typing import Any

import pytest

from rolecard_agent.core.common.approvals import (
    DECIDE_TOKEN_TTL_SECONDS,
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalService,
    ApprovalUnauthorised,
    normalise_cmd,
)


@pytest.fixture
def approvals(conn: object) -> ApprovalService:
    return ApprovalService(conn)  # type: ignore[arg-type]


def test_normalise_cmd_collapses_whitespace() -> None:
    assert normalise_cmd("  python   a.py \n b.py ") == "python a.py b.py"
    assert normalise_cmd("\t\n\r") == ""


def test_submit_creates_pending(approvals: ApprovalService) -> None:
    row = approvals.submit("echo hello", cwd="/task", role_id="r1", role_name="医生")
    assert row["status"] == "pending"
    assert row["command"] == "echo hello"
    assert row["cwd"] == "/task"
    assert row["role_id"] == "r1"
    assert row["role_name"] == "医生"


def test_submit_is_idempotent_while_pending(approvals: ApprovalService) -> None:
    """同命令不重复排队：等待中再提交返回同一记录（不刷审批队列）。"""
    first = approvals.submit("echo a", thread_id="t1")
    second = approvals.submit("echo a", thread_id="t2")
    third = approvals.submit("  echo   a ", thread_id="t3")
    assert second["id"] == first["id"]
    assert third["id"] == first["id"]
    assert len(approvals.list_rows(status="pending")) == 1


def test_submit_after_done_returns_history(approvals: ApprovalService) -> None:
    """跑过（done）的命令再提交 = 拿回历史记录，不新排队。"""
    first = approvals.submit("ls -la")
    approvals.decide(first["id"], "approve", token=first["decide_token"])
    approvals.finish(first["id"], {"exit_code": 0, "output": "ok", "duration_ms": 5})
    again = approvals.submit("ls -la")
    assert again["id"] == first["id"]
    assert again["status"] == "done"
    assert again["result"]["exit_code"] == 0


def test_decide_approve_and_reject(approvals: ApprovalService) -> None:
    pending = approvals.submit("rm tmp.txt")
    approved = approvals.decide(pending["id"], "approve", token=pending["decide_token"])
    assert approved["status"] == "approved"
    rejected = approvals.submit("rm other.txt")
    assert (
        approvals.decide(rejected["id"], "reject", token=rejected["decide_token"])["status"]
        == "rejected"
    )


def test_decide_twice_raises(approvals: ApprovalService) -> None:
    row = approvals.submit("echo twice")
    approvals.decide(row["id"], "approve", token=row["decide_token"])
    # 第二次**不带令牌**也报"已决定"而不是"没令牌"：状态判定排在凭据判定之前，
    # 对用户更有用（这条命令早就批过了，跟令牌没关系）。
    with pytest.raises(ApprovalAlreadyDecided):
        approvals.decide(row["id"], "reject")


def test_get_missing_raises(approvals: ApprovalService) -> None:
    with pytest.raises(ApprovalNotFound):
        approvals.get(999_999)


def test_finish_backfills_result(approvals: ApprovalService) -> None:
    row = approvals.submit("python run.py")
    approvals.decide(row["id"], "approve", token=row["decide_token"])
    approvals.finish(row["id"], {"exit_code": 2, "output": "boom", "duration_ms": 10})
    done = approvals.get(row["id"])
    assert done["status"] == "done"
    assert done["result"] == {"exit_code": 2, "output": "boom", "duration_ms": 10}


def test_status_order_newest_first(approvals: ApprovalService) -> None:
    approvals.submit("mkdir -p a/b/c")
    later = approvals.submit("mkdir -p a/b/c")  # 幂等，同一条 —— 换个命令制造第二行
    other = approvals.submit("ls /definitely/not/a/dir")
    assert other["id"] > later["id"]
    rows = approvals.list_rows()
    assert [r["id"] for r in rows] == [other["id"], later["id"]]
    only_pending = approvals.list_rows(status="pending")
    assert [r["id"] for r in only_pending] == [other["id"], later["id"]]


def test_status_of_semantics(approvals: ApprovalService) -> None:
    assert approvals.status_of("never-submitted") is None
    approvals.submit("touch x")
    assert approvals.status_of("touch x") == "pending"
    assert approvals.status_of("  touch   x ") == "pending"  # 规范化后命中


def test_list_rows_empty(approvals: ApprovalService) -> None:
    assert approvals.list_rows() == []
    assert approvals.list_rows(status="pending") == []


# ------------------------------------------------------ 决定令牌（P0-3 第一步：批不动 = 没凭据）


def test_decide_requires_the_token_it_issued(approvals: ApprovalService) -> None:
    """令牌是这条记录自己下发的那一个：缺失 / 空 / 说错，三种都批不动。"""
    row = approvals.submit("echo no-token")
    for bad in (None, "", "not-the-issued-token"):
        with pytest.raises(ApprovalUnauthorised):
            approvals.decide(row["id"], "approve", token=bad)
    assert approvals.get(row["id"])["status"] == "pending"  # 拒的是这次请求，记录还等着批


def test_decide_token_is_one_time(approvals: ApprovalService) -> None:
    """决定之后令牌清空 —— 同一份读到的内容不能批第二次，也没有可重放的东西。"""
    row = approvals.submit("echo once")
    approvals.decide(row["id"], "approve", token=row["decide_token"])
    assert approvals.get(row["id"])["decide_token"] is None
    with pytest.raises(ApprovalAlreadyDecided):
        approvals.decide(row["id"], "reject", token=row["decide_token"])


def test_decide_token_expires(conn: Any, approvals: ApprovalService) -> None:
    """过期就得重新看一遍待批列表：挂着昨天的令牌批今天的命令，不算"刚看到过"。"""
    row = approvals.submit("echo stale")
    conn.execute(
        "UPDATE command_approval SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{DECIDE_TOKEN_TTL_SECONDS // 60 + 1} minutes", row["id"]),
    )
    conn.commit()
    with pytest.raises(ApprovalUnauthorised):
        approvals.decide(row["id"], "approve", token=row["decide_token"])


def test_legacy_pending_row_without_token_must_be_resubmitted(
    conn: Any, approvals: ApprovalService
) -> None:
    """令牌机制之前的 pending 行没有可比对的令牌 —— 宁可拒，也不要"缺令牌就放行"。"""
    approvals.submit("echo legacy")
    conn.execute("UPDATE command_approval SET decide_token = NULL")
    conn.commit()
    pending = approvals.list_rows(status="pending")[0]
    with pytest.raises(ApprovalUnauthorised):
        approvals.decide(pending["id"], "approve", token="whatever")