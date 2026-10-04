"""只增表的保留策略（retention）—— **策略**住在 core，机械住在 storage。

为什么搬出 `storage/db.py`（2026-10-04 审查快照 P1-6）：从前 `prune_retention_tables`
把"哪三张表、各留多少天/多少行、终态行才清"这一整套**策略**与"先落盘再 DELETE"那台
机械写在同一处，于是 storage 里又长满了业务表名（`audit_log` / `command_approval`）——
而 `core/ no domain token` 只管 core，那半边一直没有尺子。

现在的分工：

  * storage：连接、`dump_before_delete`（先备份）、`dump_rows_to_jsonl`（逐列落 JSONL）、
    `trim_backups`（分表轮转）—— 全是**不管哪张表**的机械；
  * 本模块：**哪张表按什么条件清**（策略），调用上面那三台机械。

调用点在 `core/bootstrap.py`（拿得到 Settings 的地方）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rolecard_agent.storage.db import (
    SqlConnection,
    dump_before_delete,
    trim_backups,
)


def prune_retention_tables(
    conn: SqlConnection,
    *,
    audit_log_days: int,
    audit_log_max_rows: int,
    approval_done_days: int,
    backup_dir: Path,
) -> dict[str, int]:
    """三张只增表的 retention 清理（`R102-29`；2026-10-02 拍板：分表定档）。

    `audit_log` 留 `audit_log_days` 天、且至多 `audit_log_max_rows` 行（两条判据任一命中
    即清）；`command_approval` 的**终态**行留 `approval_done_days` 天 —— pending/approved
    是活队列，retention 永不碰（卡死的行由 `R102-47` 的兜底与开机清扫负责，那是状态机的
    职责不是保留策略的）；`agent_reachout` 走 per-role `reachout_keep` 既有机制（出厂默认
    200 只对新角色生效）。0 或负数 = 该档永不清理（旧行为）。

    清理量回报调用方（schema-migrate 事件流）。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    pruned: dict[str, int] = {}
    where: str
    params: tuple[object, ...]
    if audit_log_days > 0:
        where, params = "WHERE ts < datetime('now', ?)", (f"-{audit_log_days} days",)
        dump_before_delete(
            conn, table="audit_log", where=where, params=params, backup_dir=backup_dir, stamp=stamp
        )
        cur = conn.execute(f"DELETE FROM audit_log {where}", params)
        pruned["audit_log_by_days"] = max(cur.rowcount, 0)
    if audit_log_max_rows > 0:
        where = "WHERE id NOT IN (SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)"
        params = (audit_log_max_rows,)
        dump_before_delete(
            conn, table="audit_log", where=where, params=params, backup_dir=backup_dir, stamp=stamp
        )
        cur = conn.execute(f"DELETE FROM audit_log {where}", params)
        pruned["audit_log_by_rows"] = max(cur.rowcount, 0)
    if approval_done_days > 0:
        where = "WHERE status IN ('done', 'rejected') AND updated_at < datetime('now', ?)"
        params = (f"-{approval_done_days} days",)
        dump_before_delete(
            conn,
            table="command_approval",
            where=where,
            params=params,
            backup_dir=backup_dir,
            stamp=stamp,
        )
        cur = conn.execute(f"DELETE FROM command_approval {where}", params)
        pruned["approvals_done"] = max(cur.rowcount, 0)
    conn.commit()
    if any(pruned.values()):
        trim_backups(backup_dir)
    return pruned
