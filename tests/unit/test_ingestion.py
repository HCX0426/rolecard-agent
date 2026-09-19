"""Unit tests for the ingestion-task ledger and its state machine.  Traceability: US-3.

Why this table exists (技术评审与决策.md §9 B1): a separate `ingestion_task` row,
not a `status` column on `medical_report`. These tests pin the two properties that justify the
separate table: idempotency on file hash, and a forward-only status graph.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from rolecard_agent.core.ingestion import (
    INGESTION_FAILED,
    INGESTION_INDEXED,
    INGESTION_PENDING,
    IngestionBadStatus,
    IngestionService,
)
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def conn() -> sqlite3.Connection:
    # Core + roles + health schema：域报告表上有指向 `ingestion_task` 的外键列，
    # 而外键是**真 enforced** 的（PRAGMA foreign_keys=ON），所以要把被引用的域 schema 一起建出来。
    # Seed the identity rows `ingestion_task`/`medical_report` reference, so the FK holds.
    c = connect(":memory:")
    bootstrap(c, enabled_domains=["health"])
    c.executescript(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "  VALUES ('u1', 't1', 'u1'), ('u2', 't1', 'u2');"
    )
    c.commit()
    return c


@pytest.fixture
def ing(conn: sqlite3.Connection) -> IngestionService:
    return IngestionService(conn)


def test_create_is_idempotent_on_file_hash(ing: IngestionService) -> None:
    t1 = ing.create(user_id="u1", file_hash="abc")
    t2 = ing.create(user_id="u1", file_hash="abc")  # same bytes, re-uploaded
    assert t1 == t2
    row = ing.get(t1)
    assert row["status"] == INGESTION_PENDING
    assert row["attempts"] == 0


def test_same_hash_different_users_are_distinct(ing: IngestionService) -> None:
    t1 = ing.create(user_id="u1", file_hash="abc")
    t2 = ing.create(user_id="u2", file_hash="abc")
    assert t1 != t2


def test_advance_walks_the_pipeline_and_stamps_finished(ing: IngestionService) -> None:
    tid = ing.create(user_id="u1", file_hash="h1")
    ing.advance(tid, "parsed")
    ing.advance(tid, "extracted")
    ing.advance(tid, "indexed")
    row = ing.get(tid)
    assert row["status"] == INGESTION_INDEXED
    assert row["finished_at"] is not None  # terminal state stamped


def test_illegal_transition_is_rejected(ing: IngestionService) -> None:
    tid = ing.create(user_id="u1", file_hash="h1")
    with pytest.raises(IngestionBadStatus):
        ing.advance(tid, "indexed")  # cannot jump pending -> indexed


def test_duplicate_status_is_a_noop_not_an_error(ing: IngestionService) -> None:
    tid = ing.create(user_id="u1", file_hash="h1")
    ing.advance(tid, "pending")  # same state
    assert ing.get(tid)["status"] == INGESTION_PENDING


def test_record_failure_increments_attempts_and_is_terminal(ing: IngestionService) -> None:
    tid = ing.create(user_id="u1", file_hash="h1")
    ing.record_failure(tid, "ocr exploded")
    row = ing.get(tid)
    assert row["status"] == INGESTION_FAILED
    assert row["attempts"] == 1
    assert row["last_error"] == "ocr exploded"
    assert row["finished_at"] is not None
    # a failed task may be restarted explicitly
    ing.advance(tid, "pending")
    assert ing.get(tid)["status"] == INGESTION_PENDING


def test_the_ledger_only_touches_its_own_table(
    ing: IngestionService, conn: sqlite3.Connection
) -> None:
    """内核台账只许碰 `ingestion_task`：与域报告行的关系由**域**那一侧写（P1-1）。

    原来这里是 `test_link_report_sets_the_fk` —— 它测的正是那个不该存在的写：内核一条
    `UPDATE medical_report …`。一致性脚本 `check_core_no_domain_token` 已从源码层面挡住，
    这一条从**行为**上再钉一遍（脚本可以被删，测试跟着它一起失效的概率更低）：跑一遍完整
    生命周期，用 sqlite 的 trace 回调收集真正执行过的 SQL，除本表外一张域表都不许出现。
    """
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        tid = ing.create(user_id="u1", file_hash="h1")
        ing.advance(tid, "parsed")
        ing.record_failure(tid, "ocr exploded")
        ing.relink_source(tid, "uploads/x.pdf")
    finally:
        conn.set_trace_callback(None)

    touched = {
        table
        for stmt in seen
        for table in re.findall(r"\b(?:INTO|UPDATE|FROM)\s+(\w+)", stmt, flags=re.I)
    }
    assert touched <= {"ingestion_task"}, f"台账写了别的表：{sorted(touched - {'ingestion_task'})}"
