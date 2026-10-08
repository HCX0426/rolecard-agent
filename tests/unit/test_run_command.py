"""run_command（core/tools/run.py，架构计划 C·§6.2）的单元测试。

覆盖面：
  * execute_command：成功 / 失败退出码 / 超时 / 输出截断；
  * 路径边界：cwd 必须落在任务目录内（兼容测试宿主回落）；
  * 任务目录独立 venv 的 PATH 注入（.venv-tasks/<目录名>）；
  * 工具门：总闸关闭 / 审批状态机各分支（pending 等待 / rejected / done 历史回放）；
  * run_approval_execution：批准 → 后台执行一次 → 回填结果 + 审计。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from rolecard_agent.config import Settings
from rolecard_agent.core.common.approvals import ApprovalService
from rolecard_agent.core.tools import run as run_module
from rolecard_agent.core.tools.errors import ToolExecutionError
from rolecard_agent.core.tools.run import (
    OUTPUT_MAX_BYTES,
    RunCommandError,
    _build_env,
    _resolve_within,
    _task_venv_bin,
    _truncate,
    execute_command,
    make_run_tool,
    run_approval_execution,
)

PY = f'"{sys.executable}"'


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    d = tmp_path / "task"
    d.mkdir()
    (d / "hello.txt").write_text("hi", encoding="utf-8")
    return d


@pytest.fixture
def db(conn: Any) -> Any:
    return conn


def _invoke(tool: Any, command: str, cwd: str = "") -> str:
    return str(tool.invoke({"command": command, "cwd": cwd}))


# ---------------------------------------------------------------- execute_command


def test_execute_success(task_dir: Path) -> None:
    res = execute_command(f'{PY} -c "print(\'run-ok\')"', task_dir, timeout_seconds=30)
    assert res.exit_code == 0
    assert "run-ok" in res.output
    assert res.duration_ms >= 0


def test_execute_failure_exit_code(task_dir: Path) -> None:
    res = execute_command(f'{PY} -c "import sys; sys.exit(42)"', task_dir, timeout_seconds=30)
    assert res.exit_code == 42


def test_execute_timeout_returns_readable_result(task_dir: Path) -> None:
    """超时必须真的把进程树杀掉：duration 应接近 timeout，而不是等满整条命令。"""
    res = execute_command(
        f'{PY} -c "import time; time.sleep(30)"', task_dir, timeout_seconds=0.3
    )
    assert res.exit_code is None
    assert "未结束" in res.output
    # 上限要能区分"真杀了树"（这里量级是几百毫秒）与"等满 30s 的老 bug"，但**不能贴着**
    # 几百毫秒卡：整条门禁并发跑 + 覆盖率插桩时，Windows 的进程树拆除实测到过 3212ms。
    # 8s 仍然是 30s 的 1/4，牙齿没丢。
    assert res.duration_ms < 8_000


def test_execute_output_truncated(task_dir: Path) -> None:
    res = execute_command(
        f'{PY} -c "print(\'x\' * 200000)"', task_dir, timeout_seconds=30
    )
    assert res.exit_code == 0
    assert len(res.output) <= OUTPUT_MAX_BYTES + 200  # 截断 + 后缀说明
    assert res.output_bytes >= 200_000


def test_execute_missing_cwd_returns_readable_failure(tmp_path: Path) -> None:
    res = execute_command("echo hi", tmp_path / "nope", timeout_seconds=5)
    assert res.exit_code is None


# ---------------------------------------------------------------- 路径边界


def test_resolve_within_allows_task_dir(task_dir: Path) -> None:
    assert _resolve_within(task_dir, ".") == task_dir
    assert _resolve_within(task_dir, "hello.txt") == task_dir / "hello.txt"


def test_resolve_within_rejects_escape(task_dir: Path) -> None:
    with pytest.raises(RunCommandError):
        _resolve_within(task_dir, "..")
    with pytest.raises(RunCommandError):
        _resolve_within(task_dir, "../../../etc/passwd")


# ---------------------------------------------------------------- 独立 venv PATH 注入


def test_task_venv_bin_injects_scripts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    fake_root = tmp_path / "venvs"
    venv = fake_root / "task"
    (venv / "Scripts").mkdir(parents=True)
    monkeypatch.setattr(run_module, "_TASK_VENV_ROOT", fake_root)
    # Scripts 分支（Windows venv 布局）
    got = _task_venv_bin(task)
    assert got is not None
    assert Path(got) == venv / "Scripts"


def test_build_env_prepends_task_venv(
    monkeypatch: pytest.MonkeyPatch, task_dir: Path, tmp_path: Path
) -> None:
    fake_root = tmp_path / "venvs"
    (fake_root / task_dir.name / "bin").mkdir(parents=True)
    monkeypatch.setattr(run_module, "_TASK_VENV_ROOT", fake_root)
    env = _build_env(task_dir)
    assert str(fake_root / task_dir.name / "bin") in env["PATH"].split(os.pathsep)


def test_no_task_venv_keeps_system_path(task_dir: Path) -> None:
    env = _build_env(task_dir)
    assert "PATH" in env


# ---------------------------------------------------------------- 工具门：总闸与权限


def test_tool_executes_in_auto(task_dir: Path, db: Any) -> None:
    settings = Settings(run_approval="auto", run_tools_enabled=True, workspace_dir=str(task_dir))
    tool = make_run_tool(settings=settings, conn=db, dir_resolver=lambda: task_dir)
    text = _invoke(tool, f'{PY} -c "print(\'auto-ran\')"', cwd="")
    assert "auto-ran" in text


def test_tool_total_switch_off(task_dir: Path, db: Any) -> None:
    """总闸关闭：无论 auto/manual，一律返回可读的关闭说明，绝不执行。"""
    settings = Settings(run_approval="auto", run_tools_enabled=False, workspace_dir=str(task_dir))
    tool = make_run_tool(settings=settings, conn=db, dir_resolver=lambda: task_dir)
    text = _invoke(tool, f'{PY} -c "print(\'nope\')"')
    assert "关闭" in text
    assert "nope" not in text


def test_tool_rejects_escape_cwd(task_dir: Path, db: Any) -> None:
    settings = Settings(run_approval="auto", run_tools_enabled=True, workspace_dir=str(task_dir))
    tool = make_run_tool(settings=settings, conn=db, dir_resolver=lambda: task_dir)
    text = _invoke(tool, "echo hi", cwd="../../../../etc")
    assert "越界" in text


# ---------------------------------------------------------------- 工具门：审批状态机


def _manual_tool(task_dir: Path, db: Any) -> Any:
    settings = Settings(run_approval="manual", run_tools_enabled=True, workspace_dir=str(task_dir))
    return make_run_tool(settings=settings, conn=db, dir_resolver=lambda: task_dir)


def test_manual_submits_then_waits_for_decision(task_dir: Path, db: Any) -> None:
    tool = _manual_tool(task_dir, db)
    text1 = _invoke(tool, f'{PY} -c "print(\'pending\')"')
    assert "已提交审批" in text1
    text2 = _invoke(tool, f'{PY} -c "print(\'pending\')"')
    assert "等待审批" in text2
    approvals = ApprovalService(db)
    assert approvals.status_of(f'{PY} -c "print(\'pending\')"') == "pending"


def test_manual_rejected_is_terminal(task_dir: Path, db: Any) -> None:
    tool = _manual_tool(task_dir, db)
    cmd = f'{PY} -c "print(\'rej\')"'
    text1 = _invoke(tool, cmd)
    assert "已提交审批" in text1
    approvals = ApprovalService(db)
    row = approvals.latest(cmd)
    assert row is not None
    approvals.decide(row["id"], "reject", token=row["decide_token"])
    text2 = _invoke(tool, cmd)
    assert "已被拒绝" in text2
    assert approvals.status_of(cmd) == "rejected"


def test_manual_approved_history_replay(task_dir: Path, db: Any) -> None:
    """批准 → 后台执行完成后，工具同一命令应回放历史结果，绝不重复执行。"""
    tool = _manual_tool(task_dir, db)
    cmd = f'{PY} -c "print(\'history-test\')"'
    _invoke(tool, cmd)  # 提交
    approvals = ApprovalService(db)
    row = approvals.latest(cmd)
    assert row is not None
    approvals.decide(row["id"], "approve", token=row["decide_token"])
    # 后台完成的回填（此处用 finish 模拟后台线程写结果，无真实子进程）
    approvals.finish(row["id"], {"exit_code": 0, "output": "history-ran", "duration_ms": 9})
    text = _invoke(tool, cmd)
    assert "已执行过" in text
    assert "history-ran" in text
    assert "history-test" not in text  # 没有真的再跑一次


def test_manual_approved_in_flight(task_dir: Path, db: Any) -> None:
    """刚批准、后台还没回填时：工具如实说执行中，而不是又提交一次。"""
    tool = _manual_tool(task_dir, db)
    cmd = f'{PY} -c "print(\'flying\')"'
    _invoke(tool, cmd)
    approvals = ApprovalService(db)
    row = approvals.latest(cmd)
    assert row is not None
    approvals.decide(row["id"], "approve", token=row["decide_token"])
    text = _invoke(tool, cmd)
    assert "正在执行" in text


# ---------------------------------------------------------------- run_approval_execution


def test_run_approval_execution_finishes_with_audit(task_dir: Path, db: Any) -> None:
    settings = Settings(run_approval="manual", workspace_dir=str(task_dir))
    approvals = ApprovalService(db)
    cmd = f'{PY} -c "print(\'OUTPUT_BODY_TEXT\')"'
    row = approvals.submit(cmd, cwd=str(task_dir))
    approvals.decide(row["id"], "approve", token=row["decide_token"])
    run_approval_execution(row["id"], settings=settings, conn=db)
    done = approvals.get(row["id"])
    assert done["status"] == "done"
    assert done["result"]["exit_code"] == 0
    assert "OUTPUT_BODY_TEXT" in done["result"]["output"]  # 结果回填了
    # 审计：actor=agent，一次 run_command。结构上 detail 就没有 output 字段 ——
    # 无论命令输出什么内容，都不会被审计带走（只记命令、退出码、耗时、字节数）。
    audit = db.execute(
        "SELECT actor, action, target, detail_json FROM audit_log WHERE actor = 'agent'"
    ).fetchall()
    assert len(audit) == 1
    assert audit[0]["action"] == "run_command"
    assert audit[0]["target"] == str(task_dir)
    parsed: dict[str, Any] = json.loads(audit[0]["detail_json"] or "{}")
    assert set(parsed) == {"command", "exit_code", "output_bytes", "duration_ms"}
    assert "output" not in parsed


def test_run_approval_execution_skips_rejected(task_dir: Path, db: Any) -> None:
    settings = Settings(run_approval="manual", workspace_dir=str(task_dir))
    approvals = ApprovalService(db)
    row = approvals.submit(f'{PY} -c "print(\'never\')"', cwd=str(task_dir))
    approvals.decide(row["id"], "reject", token=row["decide_token"])
    run_approval_execution(row["id"], settings=settings, conn=db)
    assert approvals.get(row["id"])["status"] == "rejected"  # 拒绝是终态：不执行
    audit = db.execute(
        "SELECT COUNT(*) AS c FROM audit_log WHERE action = 'run_command'"
    ).fetchone()
    assert audit["c"] == 0


# ---------------------------------------------------------------- 杂项纯函数


def test_truncate_keeps_suffix_note() -> None:
    text = "x" * 100_000
    cut = _truncate(text, limit=1_000)
    assert len(cut) <= 1_000 + 100
    assert "截断" in cut


def test_tool_execution_error_maps_to_readable() -> None:
    assert issubclass(RunCommandError, ToolExecutionError)


def test_approval_mode_without_service_fails_closed(task_dir: Path) -> None:
    """审批档开着的却没接审批服务 → 拒绝执行（2026-10-04 审查快照的审批条目）。

    从前 `svc is None` 会静默落到底部直接执行 —— 任何忘传 conn/approvals 的新接线
    都等于把审批门整个摘掉还不出声。fail-closed：让装配问题当场现形。

    `dir_resolver` 要显式给：不给就回落到 `settings.workspace_dir`，而默认值是仓库的
    `data/workspace/` —— 那一格是 gitignore 的运行时目录，全新 clone 上根本不存在，
    于是这条用例会先撞"目录不存在"那道闸、报不出它真正要断言的那一句（本仓另六条
    同族用例都传了 `dir_resolver`，只有这条漏了）。
    """
    from rolecard_agent.core.tools.run import make_run_tool

    settings = Settings(run_tools_enabled=True, run_approval="manual")
    tool = make_run_tool(
        settings=settings, conn=None, approvals=None, dir_resolver=lambda: task_dir
    )
    out = str(tool.invoke({"command": "echo should-not-run"}))
    assert "命令审批服务未接" in out, out
