"""schema 代际戳、整表重建的自愈与原子性（`R102-54`/`R102-53`）与 touch_thread 唯一出处
（`R102-62`）的行为钉。

变异对照：
* 摘掉 `_check_schema_generation` 的拒启分支 ⇒ `test_user_version_refuses_newer_db` 红；
* 摘掉计划里 `pre.repair_stranded_rebuilds` 那一步 ⇒ `test_strand_repair_*` 两条红
  （滞留现场重跑仍静默）；
* 把 `touch_thread` 里的 `%f` 改回秒级 ⇒ 毫秒精度那条红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rolecard_agent.core.storage.migrations import MIGRATION_PLAN
from rolecard_agent.storage.db import (
    SCHEMA_VERSION,
    bootstrap,
    connect,
)
from rolecard_agent.storage.threads import thread_id_carriers, touch_thread


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    conn = connect(path)
    bootstrap(conn, enabled_domains=("health",))
    conn.commit()
    conn.close()
    return path


def test_fresh_db_gets_generation_stamp(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    finally:
        conn.close()


def test_user_version_refuses_newer_db(db_path: Path, capsys: object) -> None:
    """装包回滚 = 旧代码读新库：一句 loud 报错，而不是散落启动链的 no such column。"""
    conn = connect(db_path)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 3}")
        conn.commit()
        with pytest.raises(RuntimeError, match="装包回滚"):
            bootstrap(conn, enabled_domains=("health",))
    finally:
        conn.close()


def test_strand_repair_restores_rows_and_reruns_migration(db_path: Path) -> None:
    """`R102-53` 的第三种死法现场：原表不在、暂存表有数据。

    从前重跑 bootstrap 静默全绿（数据永远滞留在没人读的暂存表里）；现在自愈回原表、
    迁移整个重跑 —— 行数保住、新形状到位。
    """
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO role_proactive_state (role_id, affinity) VALUES ('r_probe', 0.5)"
        )
        conn.commit()
        # 手工构造"死在 DROP 之后、RENAME 之前"的现场：
        conn.execute("ALTER TABLE role_proactive_state RENAME TO role_proactive_state__b2")
        conn.commit()
        bootstrap(conn, enabled_domains=("health",), plan=MIGRATION_PLAN)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(role_proactive_state)")}
        assert "user_id" in cols, "自愈后迁移必须整个重跑（新形状到位）"
        rows = conn.execute(
            "SELECT affinity FROM role_proactive_state WHERE role_id = 'r_probe'"
        ).fetchall()
        assert rows and float(rows[0][0]) == 0.5, "滞留暂存表里的数据不许丢"
    finally:
        conn.close()


def test_strand_repair_drops_empty_temp(db_path: Path) -> None:
    """死在 INSERT 之前的老窗口（空暂存表）：自愈 = DROP，重跑即净。"""
    conn = connect(db_path)
    try:
        conn.execute("DROP TABLE role_proactive_state")
        conn.execute(
            "CREATE TABLE role_proactive_state__b2 (user_id TEXT, role_id TEXT)"
        )
        conn.commit()
        bootstrap(conn, enabled_domains=("health",), plan=MIGRATION_PLAN)
        stranded = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'role_proactive_state__b2'"
        ).fetchone()[0]
        assert stranded == 0
        cols = {row[1] for row in conn.execute("PRAGMA table_info(role_proactive_state)")}
        assert "user_id" in cols
    finally:
        conn.close()


def test_touch_thread_writes_millisecond_precision(db_path: Path) -> None:
    """`R102-62`：侧栏同秒去歧建立在毫秒上 —— 精度降回秒级 = 排序静默不稳定。"""
    conn = connect(db_path)
    try:
        conn.executescript(
            """
            INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');
            INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name)
                VALUES ('local-user', 't1', 'owner');
            INSERT INTO session_thread (thread_id, user_id, current_role_id)
                VALUES ('thread-touch', 'local-user', 'general_assistant');
            """
        )
        conn.commit()
        touch_thread(conn, "thread-touch")
        conn.commit()
        stamp = conn.execute(
            "SELECT updated_at FROM session_thread WHERE thread_id = 'thread-touch'"
        ).fetchone()[0]
        assert re.search(r"\d{2}:\d{2}:\d{2}\.\d{3}", str(stamp)), stamp
        # 载体名单是活的：touch 的表自己也在名单里（改名迁移的同一判据）。
        assert "session_thread" not in thread_id_carriers(conn)
    finally:
        conn.close()
