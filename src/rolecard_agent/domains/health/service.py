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
from datetime import date, timedelta

from rolecard_agent.domains.health import KNOWLEDGE_SCOPE
from rolecard_agent.storage.db import SqlConnection

# A report check date the tool layer accepts: YYYY-MM-DD (or just YYYY-MM). Anything else is
# treated as "no filter" rather than silently matching nothing - a model that passes
# "2026年3月" should still get data, not an empty archive.
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def _valid_date(value: str | None) -> str | None:
    if not value:
        return None
    return value if _DATE_RE.match(value.strip()) else None


def _lower_bound(value: str | None) -> str | None:
    """起点：`YYYY` / `YYYY-MM` 展开成该周期的**第一天**（`YYYY-MM-DD` 原样）。"""
    v = _valid_date(value)
    if v is None:
        return None
    if len(v) == 4:
        return f"{v}-01-01"
    if len(v) == 7:
        return f"{v}-01"
    return v


def _upper_bound(value: str | None) -> str | None:
    """终点：换算成**开区间上界**（下一天的零点），这样"含末尾这一天"不用靠 `<=`。

    为什么必须换算：SQL 里 `date(mr.check_time) <= date('2026-03')` 的 `date('2026-03')`
    求值是 **NULL** → 条件恒为 NULL → **静默返回空**。而 `_DATE_RE` 明确接受 `YYYY-MM`
    （注释写着"模型传『2026年3月』也应该拿到数据"），于是"按月份查"永远查不到东西
    （审查报告 P1-8）。展开成 ISO 字符串后可以直接比较（ISO 日期字典序 = 时间序）。
    """
    v = _valid_date(value)
    if v is None:
        return None
    year = int(v[0:4])
    if len(v) == 4:
        return f"{year + 1:04d}-01-01"
    month = int(v[5:7])
    if len(v) == 7:
        if month == 12:
            return f"{year + 1:04d}-01-01"
        return f"{year:04d}-{month + 1:02d}-01"
    return (date(year, month, int(v[8:10])) + timedelta(days=1)).isoformat()


class HealthDataError(Exception):
    """Base for health-domain failures. Never carries a stack trace to the caller."""


class HealthInvalidReport(HealthDataError):
    """Raised when a report write would produce a row that cannot be queried meaningfully."""


class HealthNotFound(HealthDataError):
    """Raised when the row does not exist OR belongs to another user — one error for both,
    so a wrong id cannot be used to probe other users' data."""


# Fields the data-management UI may edit on an indicator row. Everything else (report_id,
# provenance timestamps) is off-limits by omission rather than by runtime checks.
_EDITABLE_INDEX_FIELDS = frozenset(
    {"index_value", "value_text", "unit", "ref_range", "is_verified", "source", "raw_text"}
)
_SOURCE_VALUES = frozenset({"manual", "parsed", "ocr"})


class HealthQueryService:
    #: 见 `core.domain_service.DomainQueryService.knowledge_scope`。
    knowledge_scope = KNOWLEDGE_SCOPE

    def __init__(self, conn: SqlConnection) -> None:
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
            # 必须回滚：DML 已经开了事务，直接抛出去会留下一个悬挂事务 —— 在按线程分发的
            # 连接模型下（storage/db.py 的 ThreadLocalConnection），它会把**其它线程**的
            # 写操作堵到超时（database is locked）。事务由谁开，就由谁关。
            self._conn.rollback()
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
        # 起止换算成半开区间 [lo, hi)：`2026-03` 这种部分日期也能取到数据（P1-8）。
        lo = _lower_bound(start_date)
        hi = _upper_bound(end_date)

        where = ["mr.user_id = ?"]
        params: list[object] = [user_id]
        if lo:
            where.append("date(mr.check_time) >= ?")
            params.append(lo)
        if hi:
            where.append("date(mr.check_time) < ?")
            params.append(hi)

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

    # -- data management (F2：修正误录 / 删除报告) ----------------------------

    def list_records(self, user_id: str) -> list[dict[str, object]]:
        """Reports with their full indicator rows nested — the data-management view."""
        reports = self._conn.execute(
            "SELECT report_id, report_type, check_time, institution, note "
            "FROM medical_report WHERE user_id = ? ORDER BY check_time",
            (user_id,),
        ).fetchall()
        out: list[dict[str, object]] = []
        for report in reports:
            indices = self._conn.execute(
                "SELECT index_id, index_name, index_value, value_text, unit, ref_range, "
                "is_verified, source, raw_text FROM medical_index WHERE report_id = ? "
                "ORDER BY index_name",
                (report["report_id"],),
            ).fetchall()
            out.append({**dict(report), "indices": [dict(i) for i in indices]})
        return out

    def get_record(self, *, user_id: str, report_id: str) -> dict[str, object] | None:
        """单份报告 + 指标行（shape 同 `list_records`；不存在或非本人返回 None）。"""
        report = self._conn.execute(
            "SELECT report_id, report_type, check_time, institution, note "
            "FROM medical_report WHERE user_id = ? AND report_id = ?",
            (user_id, report_id),
        ).fetchone()
        if report is None:
            return None
        indices = self._conn.execute(
            "SELECT index_id, index_name, index_value, value_text, unit, ref_range, "
            "is_verified, source, raw_text FROM medical_index WHERE report_id = ? "
            "ORDER BY index_name",
            (report_id,),
        ).fetchall()
        return {**dict(report), "indices": [dict(i) for i in indices]}

    def update_index(
        self, *, user_id: str, index_id: str, changes: dict[str, object]
    ) -> dict[str, object]:
        """Apply an operator's corrections to one indicator row, then return the new state.

        `changes` keys must be in _EDITABLE_INDEX_FIELDS; `index_value` explicitly set to None
        is legal (switching to a text value) but the FINAL state must keep value/text from
        both being empty. Ownership is checked in the JOIN — a wrong user gets NotFound, not
        a 403 that confirms the row exists.
        """
        row = self._conn.execute(
            "SELECT mi.index_id, mi.index_name, mi.index_value, mi.value_text, mi.unit, "
            "mi.ref_range, mi.is_verified, mi.source, mi.raw_text "
            "FROM medical_index mi JOIN medical_report mr ON mi.report_id = mr.report_id "
            "WHERE mi.index_id = ? AND mr.user_id = ?",
            (index_id, user_id),
        ).fetchone()
        if row is None:
            raise HealthNotFound(f"indicator not found: {index_id}")

        data = dict(row)
        data.pop("index_id")
        unknown = set(changes) - _EDITABLE_INDEX_FIELDS
        if unknown:
            raise HealthInvalidReport(f"不可编辑的字段：{sorted(unknown)}")
        for key, value in changes.items():
            if key == "index_value" and value is not None:
                try:
                    value = float(value)  # type: ignore[arg-type]
                except (TypeError, ValueError) as exc:
                    raise HealthInvalidReport(f"index_value 必须是数字，得到 {value!r}") from exc
            if key == "is_verified":
                value = 1 if value else 0
            if key == "source" and value not in _SOURCE_VALUES:
                raise HealthInvalidReport(f"source 只能是 {sorted(_SOURCE_VALUES)}")
            data[key] = value
        if data["index_value"] is None and not str(data.get("value_text") or "").strip():
            raise HealthInvalidReport("数值与文本不能同时为空")

        assignments = ", ".join(f"{k} = ?" for k in data)
        self._conn.execute(
            f"UPDATE medical_index SET {assignments} WHERE index_id = ?",
            [*data.values(), index_id],
        )
        self._conn.commit()
        updated = self._conn.execute(
            "SELECT index_id, index_name, index_value, value_text, unit, ref_range, "
            "is_verified, source, raw_text FROM medical_index WHERE index_id = ?",
            (index_id,),
        ).fetchone()
        return dict(updated) if updated else {}

    def delete_index(self, *, user_id: str, index_id: str) -> None:
        """Remove one indicator row. Ownership via subquery; NotFound for wrong user too."""
        cur = self._conn.execute(
            "DELETE FROM medical_index WHERE index_id = ? AND report_id IN "
            "(SELECT report_id FROM medical_report WHERE user_id = ?)",
            (index_id, user_id),
        )
        if cur.rowcount == 0:
            # 同上：未命中也要结束事务，否则悬挂的写事务会堵住别的线程。
            self._conn.rollback()
            raise HealthNotFound(f"indicator not found: {index_id}")
        self._conn.commit()

    def report_task_id(self, *, user_id: str, report_id: str) -> str | None:
        """这份报告由哪个 intake 任务产出（手工录入 = None）。

        删除报告必须连**检索索引**一起清，而上传时用的索引身份就是 `task_id`
        （见 `rag.retriever.index` 的身份/展示名约定）—— 所以删除路径需要先把键取出来。
        行不存在也返回 None（与"存在但没有 intake"不可区分）：唯一的 404 判定点是
        `delete_report`，这里再判一次只会制造两个真相。
        """
        row = self._conn.execute(
            "SELECT ingestion_task_id FROM medical_report WHERE user_id = ? AND report_id = ?",
            (user_id, report_id),
        ).fetchone()
        if row is None or not row["ingestion_task_id"]:
            return None
        return str(row["ingestion_task_id"])

    def delete_report(self, *, user_id: str, report_id: str) -> None:
        """Remove a report; its indicator rows go with it (ON DELETE CASCADE, FK pragma on).

        The ingestion_task ledger is deliberately NOT touched: the ledger records that a
        process happened, the report is the fact it produced.
        """
        cur = self._conn.execute(
            "DELETE FROM medical_report WHERE report_id = ? AND user_id = ?",
            (report_id, user_id),
        )
        if cur.rowcount == 0:
            # 同上：未命中也要结束事务，否则悬挂的写事务会堵住别的线程。
            self._conn.rollback()
            raise HealthNotFound(f"report not found: {report_id}")
        self._conn.commit()
