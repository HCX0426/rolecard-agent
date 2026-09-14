"""Ingestion task ledger - the lifecycle of an uploaded document.

This table (`ingestion_task` in core/schema.sql) is the kernel's record of "a file was handed
to us and is being turned into structured reports". It is a KERNEL table on purpose, even
though the work it tracks is health-domain: the kernel owns identity, the plugin switch, and
this ledger, so the audit story stays in one place.

Why a separate table and not a `status` column on `medical_report` (技术评审与决策.md §9 B1):

  * **Process vs fact.** An intake can be retried; the resulting report is still one report.
    Putting run state on the report row means either losing attempt history or growing the
    ledger into the report.
  * **No half-finished rows.** An in-flight intake must produce NO report row until it
    succeeds, or an unverified fragment reaches the user through one forgotten WHERE clause.
    In this project that failure mode is not acceptable, so an incomplete intake leaves no
    report behind.
  * **Cardinality.** One file can yield several reports (1:N: a checkup covering several
    departments), which cannot be a column.

This service is the state machine + idempotency key ONLY. It does NOT do OCR or parsing -
those are domain work (v2.2) and call back into `advance` / `record_failure` once they have a
result.
"""

from __future__ import annotations

import sqlite3
import uuid

# The status column is CHECK-constrained to exactly these values. Listing them here keeps the
# valid transitions and the schema in the same place instead of scattering string literals.
INGESTION_PENDING = "pending"
INGESTION_PARSED = "parsed"
INGESTION_EXTRACTED = "extracted"
INGESTION_INDEXED = "indexed"
INGESTION_FAILED = "failed"

INGESTION_STATUSES: tuple[str, ...] = (
    INGESTION_PENDING,
    INGESTION_PARSED,
    INGESTION_EXTRACTED,
    INGESTION_INDEXED,
    INGESTION_FAILED,
)

# The pipeline is forward-only; a terminal or failed task may only be restarted explicitly.
# Modelling it as a graph (not a free state machine) is what stops a caller from calling a
# failed task "indexed" without going through the retry.
_INGESTION_TRANSITIONS: dict[str, tuple[str, ...]] = {
    INGESTION_PENDING: (INGESTION_PARSED, INGESTION_FAILED),
    INGESTION_PARSED: (INGESTION_EXTRACTED, INGESTION_FAILED),
    INGESTION_EXTRACTED: (INGESTION_INDEXED, INGESTION_FAILED),
    INGESTION_INDEXED: (),  # terminal: nothing further
    INGESTION_FAILED: (INGESTION_PENDING,),  # explicit restart only
}


class IngestionError(Exception):
    """Base for ingestion-domain failures. Never carries a stack trace to the caller."""


class IngestionNotFound(IngestionError):
    pass


class IngestionBadStatus(IngestionError):
    """Raised when a transition is not in _INGESTION_TRANSITIONS."""


def _new_task_id() -> str:
    return f"ing_{uuid.uuid4().hex[:12]}"


class IngestionService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- create --------------------------------------------------------------

    def create(
        self,
        *,
        user_id: str,
        file_hash: str,
        source_file: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """Register an intake. Idempotent on (user_id, file_hash): re-uploading the same bytes
        returns the EXISTING task_id instead of a duplicate ledger row.

        `task_id` is accepted only for callers that need a stable id (tests); normally it is
        generated. The UNIQUE constraint is the real guarantee - the collision path is handled
        below, the caller does not pre-check.
        """
        if not file_hash:
            raise IngestionError("file_hash is required for idempotency")
        tid = task_id or _new_task_id()
        try:
            self._conn.execute(
                "INSERT INTO ingestion_task (task_id, user_id, source_file, file_hash, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, user_id, source_file, file_hash, INGESTION_PENDING),
            )
        except sqlite3.IntegrityError:
            existing = self._conn.execute(
                "SELECT task_id FROM ingestion_task WHERE user_id = ? AND file_hash = ?",
                (user_id, file_hash),
            ).fetchone()
            if existing is None:
                raise  # not the collision we expected
            return str(existing["task_id"])
        self._conn.commit()
        return tid

    # -- reads ---------------------------------------------------------------

    def get(self, task_id: str) -> dict[str, object]:
        row = self._conn.execute(
            "SELECT task_id, user_id, source_file, file_hash, status, attempts, "
            "last_error, created_at, updated_at, finished_at "
            "FROM ingestion_task WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise IngestionNotFound(f"ingestion task not found: {task_id}")
        return dict(row)

    def list_for_user(self, user_id: str, *, status: str | None = None) -> list[dict[str, object]]:
        if status is not None:
            rows = self._conn.execute(
                "SELECT task_id, status, attempts, created_at, finished_at "
                "FROM ingestion_task WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
                (user_id, status),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT task_id, status, attempts, created_at, finished_at "
                "FROM ingestion_task WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- transitions ---------------------------------------------------------

    def advance(self, task_id: str, status: str) -> None:
        """Move to a valid next status. `indexed` / `failed` stamp finished_at.

        `status == current` is allowed and is a no-op write (an idempotent caller may re-assert
        the same state); any other transition must be in _INGESTION_TRANSITIONS.
        """
        if status not in INGESTION_STATUSES:
            raise IngestionBadStatus(f"unknown status: {status!r}")
        current = self.get(task_id)["status"]
        if status != current and status not in _INGESTION_TRANSITIONS.get(current, ()):
            allowed = _INGESTION_TRANSITIONS.get(current, ())
            raise IngestionBadStatus(
                f"illegal transition {current!r} -> {status!r}; allowed: {allowed or '(none)'}"
            )
        finished = (
            ", finished_at = CURRENT_TIMESTAMP"
            if status in (INGESTION_INDEXED, INGESTION_FAILED)
            else ""
        )
        self._conn.execute(
            f"UPDATE ingestion_task SET status = ?, updated_at = CURRENT_TIMESTAMP{finished} "
            "WHERE task_id = ?",
            (status, task_id),
        )
        self._conn.commit()

    def record_failure(self, task_id: str, error: str) -> None:
        """Park the task in `failed` and count the attempt. The error text is stored verbatim
        for the operator; it is never shown to the model or the user (D6 / the global safety
        rule about not leaking internals).
        """
        cur = self._conn.execute(
            "UPDATE ingestion_task SET status = ?, attempts = attempts + 1, last_error = ?, "
            "updated_at = CURRENT_TIMESTAMP, finished_at = CURRENT_TIMESTAMP "
            "WHERE task_id = ?",
            (INGESTION_FAILED, error, task_id),
        )
        if cur.rowcount == 0:
            raise IngestionNotFound(f"ingestion task not found: {task_id}")
        self._conn.commit()

    # -- linking -------------------------------------------------------------

    def link_report(self, task_id: str, report_id: str) -> None:
        """Point a successfully parsed report back at the intake that produced it.

        The FK lives in `medical_report.ingestion_task_id`; this just sets it. A report entered
        by hand has no intake, so the caller passes task_id=None and never calls this - the
        column defaults to NULL and the relation stays 1:N as designed.
        """
        cur = self._conn.execute(
            "UPDATE medical_report SET ingestion_task_id = ? WHERE report_id = ?",
            (task_id, report_id),
        )
        if cur.rowcount == 0:
            raise IngestionNotFound(f"report not found: {report_id}")
        self._conn.commit()
