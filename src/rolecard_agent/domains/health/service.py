"""Health record queries + the v1 write path: manual report entry.

Two responsibilities, one class:

  * **Writes (v1)** — `create_report` is how data legally enters `medical_report` /
    `medical_index` in v1: the operator (or the data owner) types values in by hand, so
    `source='manual'`. OCR/parsing (v2.2) will call the same method with
    `source='parsed'|'ocr'` once it exists - one write path, three provenance labels.
  * **Reads (the M3 tools' backing)** — `search_indices` / `list_reports` power
    `query_health_record` / `compare_health_index` / `list_reports`. Every read is scoped to
    one `user_id`: user isolation is enforced HERE, in the WHERE clause, not in the tool
    layer, so a tool bug can never widen what a user sees.

Exact-vs-substring matching lives in `search_indices` (exact first, then substring): the
model often passes "结石" where the archive stores "结石直径", and returning the closest real
rows beats returning nothing - the role can then state what actually exists instead of
hallucinating. Every row keeps its provenance (`is_verified`) so the tool layer can carry
the 【未经人工校验】 marker in the return text.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Sequence

# A report check date the tool layer accepts: YYYY-MM-DD (or just YYYY-MM). Anything else is
# treated as "no filter" rather than silently matching nothing - a model that passes
# "2026年3月" should still get data, not an empty archive.
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def _valid_date(value: str | None) -> str | None:
    if not value:
        return None
    return value if _DATE_RE.match(value.strip()) else None


class HealthDataError(Exception):
    """Base for health-domain failures. Never carries a stack trace to the caller."""


class HealthInvalidReport(HealthDataError):
    """Raised when a report write would produce a row that cannot be queried meaningfully."""


class HealthQueryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- writes ----------------------------------------------------------------

    def create_report(
        self,
        *,
        user_id: str,
        report_type: str,
        check_time: str,
        institution: str | None = None,
        note: str | None = None,
        indices: Sequence[dict[str, object]],
    ) -> str:
        """Insert one report with its indicator rows, atomically.

        Each entry in `indices` needs `index_name` plus at least one of `index_value`
        (numeric) / `value_text` (verbatim non-numeric) - a value with neither cannot be
        answered with, and an unanswerable row is worse than no row. Optional keys: `unit`,
        `ref_range`, `is_verified` (default 0 = AI extracted / not yet human-checked),
        `source` (default 'manual'), `raw_text`.
        """
        if not report_type.strip():
            raise HealthInvalidReport("report_type is required")
        if not check_time.strip():
            raise HealthInvalidReport("check_time is required (e.g. '2026-03-12')")
        if not indices:
            raise HealthInvalidReport("a report without indicator rows cannot be stored")

        prepared: list[tuple[object, ...]] = []
        for item in indices:
            name = str(item.get("index_name") or "").strip()
            value = item.get("index_value")
            text = item.get("value_text")
            if not name:
                raise HealthInvalidReport("every indicator row needs index_name")
            if value is None and (text is None or not str(text).strip()):
                raise HealthInvalidReport(f"indicator {name!r} needs index_value or value_text")
            try:
                numeric = float(value) if value is not None else None  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise HealthInvalidReport(
                    f"indicator {name!r}: index_value must be numeric, got {value!r}"
                ) from exc
            prepared.append(
                (
                    name,
                    numeric,
                    str(text) if text is not None else None,
                    item.get("unit"),
                    item.get("ref_range"),
                    1 if item.get("is_verified") else 0,
                    item.get("source") or "manual",
                    item.get("raw_text"),
                )
            )

        report_id = f"rp_{uuid.uuid4().hex[:12]}"
        index_rows = [(f"mi_{uuid.uuid4().hex[:12]}", report_id, *fields) for fields in prepared]
        try:
            self._conn.execute(
                "INSERT INTO medical_report (report_id, user_id, report_type, check_time, "
                "institution, note) VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, user_id, report_type.strip(), check_time.strip(), institution, note),
            )
            self._conn.executemany(
                "INSERT INTO medical_index (index_id, report_id, index_name, index_value, "
                "value_text, unit, ref_range, is_verified, source, raw_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                index_rows,
            )
        except sqlite3.IntegrityError as exc:
            raise HealthInvalidReport(f"report rejected by database: {exc}") from exc
        self._conn.commit()
        return report_id

    # -- reads -------------------------------------------------------------------

    def index_names(self, user_id: str) -> list[str]:
        """Distinct indicator names this user actually has, sorted - the self-correction
        hint when a query matches nothing."""
        rows = self._conn.execute(
            "SELECT DISTINCT mi.index_name FROM medical_index mi "
            "JOIN medical_report mr ON mi.report_id = mr.report_id "
            "WHERE mr.user_id = ? ORDER BY mi.index_name",
            (user_id,),
        ).fetchall()
        return [str(r["index_name"]) for r in rows]

    def search_indices(
        self,
        user_id: str,
        index_name: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, object]]:
        """Chronological indicator rows for one name (exact match first, then substring).

        Returns plain dicts: check_time, report_type, institution, index_name, index_value,
        value_text, unit, ref_range, is_verified. Empty list = this user has no such row.
        """
        name = index_name.strip()
        if not name:
            return []
        start = _valid_date(start_date)
        end = _valid_date(end_date)

        where = ["mr.user_id = ?"]
        params: list[object] = [user_id]
        if start:
            where.append("date(mr.check_time) >= date(?)")
            params.append(start)
        if end:
            where.append("date(mr.check_time) <= date(?)")
            params.append(end)

        def _run(match_clause: str, match_param: object) -> list[sqlite3.Row]:
            date_filters = (" AND " + " AND ".join(where[1:])) if len(where) > 1 else ""
            sql = (
                "SELECT mr.check_time, mr.report_type, mr.institution, mi.index_name, "
                "mi.index_value, mi.value_text, mi.unit, mi.ref_range, mi.is_verified "
                "FROM medical_index mi JOIN medical_report mr ON mi.report_id = mr.report_id "
                f"WHERE mr.user_id = ? AND mi.index_name {match_clause}"
                f"{date_filters} ORDER BY mr.check_time"
            )
            return self._conn.execute(sql, [user_id, match_param, *params[1:]]).fetchall()

        rows = _run("= ?", name)
        if not rows:
            rows = _run("LIKE '%' || ? || '%'", name)
        return [dict(r) for r in rows]

    def list_reports(self, user_id: str) -> list[dict[str, object]]:
        """All stored reports for one user, newest first, with per-report indicator counts."""
        rows = self._conn.execute(
            "SELECT mr.report_id, mr.report_type, mr.check_time, mr.institution, mr.note, "
            "COUNT(mi.index_id) AS n_indices "
            "FROM medical_report mr LEFT JOIN medical_index mi ON mi.report_id = mr.report_id "
            "WHERE mr.user_id = ? "
            "GROUP BY mr.report_id ORDER BY mr.check_time DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
