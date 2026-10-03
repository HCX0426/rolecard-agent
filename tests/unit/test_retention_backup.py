"""retention 的「先备份再删」终于有了判据（`R102-29` 追补，10-03 批 24）。

批 5 当时把这条记成收口，但拍板原文里那半句"先备份再删"从没落地：`prune_retention_tables`
直接 `DELETE`，被删的行在世上任何地方都不存在 —— 那不叫保留策略，叫永久删除。更要紧的是
整条 retention **一条用例都没有**（`grep -rln retention tests` 空），所以"清理只在量级失控时
才咬人"这句话当时也没人量过。

这里的判据按"删掉的行还能不能拿回来"写，不看行数看 ID 集合：

  1. 该删的每一行都逐列落在 JSONL 里（备份文件必须真能还原，不是"有个文件就行"）；
  2. `backup_dir` 是必填 —— 一旦给了默认值，将来的调用点就能写出"删而不备份"的版本；
  3. `0` = 永不清理，且**不落空文件**（落一个空文件会让"有备份"这个信号失去含义）；
  4. 两条判据同轮命中时，备份里的行 = 两轮删掉的行并集，不重不漏；
  5. `pending` / `approved` 是活队列，retention 永不碰（那是状态机的职责）；
  6. 备份自己不许变成新的只增表，且**分表**轮转（整目录裁会让 `command_approval-*`
     按字典序压住 `audit_log-*`，把唯一能找回审计行的那份删掉）；
  7. 备份落不下去时**不许继续删**（先落盘再删的顺序是判据，不是风格）。
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rolecard_agent.storage.db import (
    RETENTION_BACKUP_KEEP,
    bootstrap,
    connect,
    prune_retention_tables,
)


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "app.db")
    bootstrap(conn, enabled_domains=("health",))
    return conn


def _audit(conn: sqlite3.Connection, *, days_old: int) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log (ts, actor, action, target, detail_json) "
        "VALUES (datetime('now', ?), 'user', 'tool.call', '目标/一', ?)",
        (f"-{days_old} days", '{"note": "中文载荷", "n": 1}'),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _approval(conn: sqlite3.Connection, *, status: str, days_old: int) -> int:
    cur = conn.execute(
        "INSERT INTO command_approval (command, status, updated_at, result_json) "
        "VALUES (?, ?, datetime('now', ?), NULL)",
        (f"echo {status}-{days_old}", status, f"-{days_old} days"),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _snapshot(conn: sqlite3.Connection, table: str) -> dict[int, dict[str, Any]]:
    return {int(r["id"]): dict(r) for r in conn.execute(f"SELECT * FROM {table}")}  # noqa: S608


def _read_backup(backup_dir: Path, table: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for f in sorted(backup_dir.glob(f"{table}-*.jsonl")):
        text = f.read_text(encoding="utf-8")
        lines.extend(json.loads(line) for line in text.splitlines() if line.strip())
    return lines


def _prune(conn: sqlite3.Connection, backup_dir: Path, **over: int) -> dict[str, int]:
    args: dict[str, int] = {
        "audit_log_days": 90,
        "audit_log_max_rows": 0,
        "approval_done_days": 30,
    }
    args.update(over)
    return prune_retention_tables(conn, backup_dir=backup_dir, **args)


def test_doomed_rows_are_dumped_column_by_column_before_they_vanish(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    doomed = [_audit(conn, days_old=d) for d in (100, 120, 400)]
    kept = _audit(conn, days_old=3)
    before = _snapshot(conn, "audit_log")
    backup_dir = tmp_path / "retention-backups"

    pruned = _prune(conn, backup_dir)

    assert pruned["audit_log_by_days"] == len(doomed)
    dumped = {int(r["id"]): r for r in _read_backup(backup_dir, "audit_log")}
    assert sorted(dumped) == sorted(doomed), "备份里的行必须正好等于被删的行（按 ID 对减）"
    for rid in doomed:
        assert dumped[rid] == before[rid], f"第 {rid} 行没被逐列备份，事后拿不回原样"
    assert {int(r[0]) for r in conn.execute("SELECT id FROM audit_log")} == {kept}
    conn.close()


def test_backup_dir_is_a_required_argument(tmp_path: Path) -> None:
    """`backup_dir` 不许有默认值：给了默认值，下一个调用点就能写出"删而不备份"的版本。"""
    params = inspect.signature(prune_retention_tables).parameters
    assert params["backup_dir"].default is inspect.Parameter.empty
    assert tmp_path is not None  # 只为让 tmp_path 在这个纯签名用例里不算未用


def test_zero_means_never_prune_and_leaves_no_file(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    old = _audit(conn, days_old=9999)
    done = _approval(conn, status="done", days_old=9999)
    backup_dir = tmp_path / "retention-backups"

    pruned = _prune(conn, backup_dir, audit_log_days=0, approval_done_days=0)

    assert pruned == {}, f"0 档本该什么都不清：{pruned}"
    assert not backup_dir.exists(), "没删行却落了备份目录 = '有备份'这个信号失去了含义"
    assert [int(r[0]) for r in conn.execute("SELECT id FROM audit_log")] == [old]
    assert [int(r[0]) for r in conn.execute("SELECT id FROM command_approval")] == [done]
    conn.close()


def test_both_audit_arms_in_one_round_dump_the_union_exactly_once(tmp_path: Path) -> None:
    """天数档与行数档同轮命中：备份里 = 两轮删掉的并集，不重不漏（同秒共享一个文件）。"""
    conn = _db(tmp_path)
    by_days = [_audit(conn, days_old=200) for _ in range(2)]
    by_rows = [_audit(conn, days_old=1) for _ in range(5)]
    backup_dir = tmp_path / "retention-backups"

    pruned = _prune(conn, backup_dir, audit_log_max_rows=3)

    assert pruned["audit_log_by_days"] == 2
    assert pruned["audit_log_by_rows"] == 2, f"5 行里只留 3：{pruned}"
    dumped = _read_backup(backup_dir, "audit_log")
    ids = [int(r["id"]) for r in dumped]
    assert len(ids) == len(set(ids)), "同一行被备了两遍：两档判据在重叠时会互相盖"
    assert sorted(ids) == sorted(by_days + by_rows[:2]), f"并集对不上：{ids}"
    assert len(list(backup_dir.glob("audit_log-*.jsonl"))) == 1, "同轮两档该并进同一份文件"
    conn.close()


def test_live_approval_queue_is_never_pruned(tmp_path: Path) -> None:
    """pending / approved 是活队列，多旧都不许被保留策略带走；done / rejected 才清。"""
    conn = _db(tmp_path)
    pending = _approval(conn, status="pending", days_old=999)
    approved = _approval(conn, status="approved", days_old=999)
    done = _approval(conn, status="done", days_old=999)
    rejected = _approval(conn, status="rejected", days_old=999)
    fresh = _approval(conn, status="done", days_old=1)
    backup_dir = tmp_path / "retention-backups"

    pruned = _prune(conn, backup_dir)

    assert pruned["approvals_done"] == 2, f"该清的是两条终态旧行：{pruned}"
    left = {str(r[0]): int(r[1]) for r in conn.execute("SELECT status, id FROM command_approval")}
    assert sorted(left.values()) == sorted([pending, approved, fresh])
    assert "pending" in left and "approved" in left, "活队列被保留策略清了"
    ids = {int(r["id"]) for r in _read_backup(backup_dir, "command_approval")}
    assert ids == {done, rejected}
    conn.close()


def test_rotation_is_per_table_so_approvals_cannot_starve_audit(tmp_path: Path) -> None:
    """分表各留 N 份：整目录按名裁会让 `command_approval-*` 压住 `audit_log-*` 全删光。

    历史件造在 `20260101`–`20260105`（都早于本轮的真实时刻戳），本轮各删一次 ⇒ 每张表 6 份
    候选、各留最新 5 份（最旧那份历史件被裁）。整目录按名裁的话，那 5 份会全是
    `command_approval-*`（`c` 排在 `a` 前面），能找回审计行的文件一份不剩。
    """
    conn = _db(tmp_path)
    backup_dir = tmp_path / "retention-backups"
    backup_dir.mkdir(parents=True)
    for i in range(RETENTION_BACKUP_KEEP, 0, -1):
        stamp = f"2026010{i}-000000"  # i=5..1 → 递减的历史件
        for table in ("audit_log", "command_approval"):
            (backup_dir / f"{table}-{stamp}.jsonl").write_text("", encoding="utf-8")
    doomed_audit = _audit(conn, days_old=500)
    doomed_approval = _approval(conn, status="done", days_old=500)

    _prune(conn, backup_dir)

    for table in ("audit_log", "command_approval"):
        survivors = sorted(f.name for f in backup_dir.glob(f"{table}-*.jsonl"))
        assert len(survivors) == RETENTION_BACKUP_KEEP, f"{table} 留下 {len(survivors)} 份"
        assert f"{table}-20260101-000000.jsonl" not in survivors, (
            f"{table} 留住了最旧那份历史件：{survivors}"
        )
    dumped = _read_backup(backup_dir, "audit_log")
    assert [int(r["id"]) for r in dumped] == [doomed_audit]
    assert {int(r["id"]) for r in _read_backup(backup_dir, "command_approval")} == {
        doomed_approval
    }
    conn.close()


def test_backup_that_cannot_be_written_blocks_the_delete(tmp_path: Path) -> None:
    """先落盘、再删、最后 commit：备份落不下去时行必须**还在库里**。

    这里把 `backup_dir` 指到一个同名普通文件上，`mkdir` 当场抛 —— 等价于磁盘满/权限坏了
    那一类真实故障。如果实现是"先删后备"或"备不上就跳过"，这一步之后行就没了。
    """
    conn = _db(tmp_path)
    doomed = _audit(conn, days_old=500)
    blocker = tmp_path / "retention-backups"
    blocker.write_text("我不是目录", encoding="utf-8")

    with pytest.raises(OSError):
        _prune(conn, blocker)

    assert [int(r[0]) for r in conn.execute("SELECT id FROM audit_log")] == [doomed], (
        "备份没写成，行却被删了 —— 顺序是判据"
    )
    conn.close()
