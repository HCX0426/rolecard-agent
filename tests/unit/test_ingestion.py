"""Unit tests for the ingestion-task ledger and its state machine.  Traceability: US-3.

Why this table exists (技术评审与决策.md §9 B1): a separate `ingestion_task` row,
not a `status` column on `medical_report`. These tests pin the two properties that justify the
separate table: idempotency on file hash, and a forward-only status graph.
"""

from __future__ import annotations

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
    # Core + roles + health schema: `medical_report` (FK target for link_report) needs health.
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


def test_link_report_sets_the_fk(ing: IngestionService, conn: sqlite3.Connection) -> None:
    tid = ing.create(user_id="u1", file_hash="h1")
    conn.execute(
        "INSERT INTO medical_report (report_id, user_id, report_type, check_time) "
        "VALUES (?, ?, 'lab', '2026-01-01')",
        ("r1", "u1"),
    )
    conn.commit()
    ing.link_report(tid, "r1")
    row = conn.execute(
        "SELECT ingestion_task_id FROM medical_report WHERE report_id = 'r1'"
    ).fetchone()
    assert row["ingestion_task_id"] == tid
