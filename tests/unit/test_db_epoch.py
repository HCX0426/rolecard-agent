"""ThreadLocalConnection 的「库代际」机制（审查报告 P1-10）。

问题：线程池的线程会被复用，上一个请求若在事务中途异常退出，残留的 BEGIN/未提交改动
会被下一个请求继承并 commit（半写入落地）。早期版本把 `rollback_current()` 放在中间件
里 —— 但中间件跑在事件循环线程，清的是另一条线程的连接，等于没清。

修复：中间件只发**代际号**（`set_request_epoch`），`_current()` 在真正持连接的线程里
发现代际变了（= 新请求第一次用库）才回滚。这里直接测机制本身。
"""

from __future__ import annotations

from pathlib import Path

from rolecard_agent.storage.db import (
    ThreadLocalConnection,
    bootstrap,
    connect,
    set_request_epoch,
)


def _make_conn(tmp_path: Path) -> ThreadLocalConnection:
    path = tmp_path / "app.db"
    raw = connect(path)
    bootstrap(raw, enabled_domains=("health",))
    raw.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo')"
    )
    raw.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES ('u1', 't1', 'demo')"
    )
    raw.commit()
    raw.close()
    return ThreadLocalConnection(path)


def test_residual_transaction_is_discarded_when_the_epoch_changes(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)

    # 请求 1：写一半不提交（模拟"事务中途异常退出"）
    set_request_epoch("req-1")
    conn.execute(
        "INSERT INTO tenant (tenant_id, display_name) VALUES ('t2', 'half-written')"
    )
    assert conn.execute("SELECT COUNT(*) FROM tenant").fetchone()[0] == 2  # 本连接看得见

    # 请求 2（同一个线程、同一个连接）：代际变了 → 残留事务必须被丢弃
    set_request_epoch("req-2")
    count = conn.execute("SELECT COUNT(*) FROM tenant").fetchone()[0]

    assert count == 1, "上一个请求的半写入被下一个请求继承了"


def test_same_epoch_does_not_rollback_mid_request_writes(tmp_path: Path) -> None:
    """同一次请求里的多次用库**不能**互相回滚 —— 否则正常的多步写入全被清掉。"""
    conn = _make_conn(tmp_path)

    set_request_epoch("req-1")
    conn.execute(
        "INSERT INTO tenant (tenant_id, display_name) VALUES ('t2', 'first')"
    )
    conn.execute("SELECT COUNT(*) FROM tenant")  # 同请求内再取一次连接
    count = conn.execute("SELECT COUNT(*) FROM tenant").fetchone()[0]

    assert count == 2, "同请求内的写入被误回滚了"


def test_epoch_change_commits_nothing_hidden(tmp_path: Path) -> None:
    """已提交的数据不因代际切换而丢失（回滚只影响未提交的事务）。"""
    conn = _make_conn(tmp_path)

    set_request_epoch("req-1")
    conn.execute(
        "INSERT INTO tenant (tenant_id, display_name) VALUES ('t2', 'committed')"
    )
    conn.commit()

    set_request_epoch("req-2")
    count = conn.execute("SELECT COUNT(*) FROM tenant").fetchone()[0]

    assert count == 2, "已提交的数据被代际切换弄丢了"


def test_dedupe_ingestion_removes_duplicates_keeps_latest(tmp_path) -> None:
    """老库的重复台账在唯一索引建起前必须清掉（2026-10-04 审查快照的上传幂等条目）。

    保留 updated_at 最新的一条、删掉同 (user_id, file_hash) 的旧影子 —— 被删的都是
    同字节的旧影子，最新那条承载全部语义；无重复时零改动（幂等）。
    """
    import sqlite3

    from rolecard_agent.core.storage.migrations import dedupe_ingestion_tasks

    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE ingestion_task (task_id TEXT PRIMARY KEY, user_id TEXT, "
        "source_file TEXT, file_hash TEXT, status TEXT, updated_at TIMESTAMP)"
    )
    rows = [
        ("t-old", "u1", "a.txt", "hash-x", "indexed", "2026-01-01 00:00:00"),
        ("t-new", "u1", "b.txt", "hash-x", "pending", "2026-06-01 00:00:00"),
        ("t-other", "u1", "c.txt", "hash-y", "pending", "2026-01-01 00:00:00"),
    ]
    conn.executemany(
        "INSERT INTO ingestion_task VALUES (?, ?, ?, ?, ?, ?)", rows
    )
    conn.commit()

    removed = dedupe_ingestion_tasks(conn)
    assert removed == 1
    left = {
        str(r["task_id"])
        for r in conn.execute("SELECT task_id FROM ingestion_task").fetchall()
    }
    assert left == {"t-new", "t-other"}, left
    assert dedupe_ingestion_tasks(conn) == 0, "幂等：第二遍必须零改动"
    conn.close()
