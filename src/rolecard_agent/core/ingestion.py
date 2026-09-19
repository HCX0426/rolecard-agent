"""Ingestion task ledger - the lifecycle of an uploaded document.

This table (`ingestion_task` in core/schema.sql) is the kernel's record of "a file was handed
to us and is being turned into structured reports". It is a KERNEL table on purpose, even
though the work it tracks belongs to a domain plugin: the kernel owns identity, the plugin
switch, and this ledger, so the audit story stays in one place.

Why a separate table and not a `status` column on the domain's report row (技术评审与决策.md §9 B1):

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

from rolecard_agent.storage.db import SqlConnection

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

# 台账行的完整投影列（L4）：get / find_by_hash 共用，避免两份列清单悄悄漂移。
_INGESTION_COLUMNS = (
    "task_id, user_id, source_file, file_hash, status, attempts, "
    "last_error, created_at, updated_at, finished_at"
)

# The pipeline is forward-only; a terminal or failed task may only be restarted explicitly.
# Modelling it as a graph (not a free state machine) is what stops a caller from calling a
# failed task "indexed" without going through the retry.
#
# ⚠️ `extracted` 在实际链路里是**瞬态**：上传端点按 parsed → extracted → indexed 连续推进
# （检索索引在上传时就建好），而结构化抽取是否成功由**域报告行上的 `ingestion_task_id`
# 外键**表达（关系方向：域引用本表，见 core/schema.sql），不再改状态 —— 一个文件可以产出
# 多份报告（1:N），状态表达不了这件事。
# 所以不要写 `WHERE status = 'extracted'` 这类查询：它查不到任何持久化的行。
# 这里保留该状态是给"显式重启 / 分步推进"的调用方用的（见 _INGESTION_TRANSITIONS）。
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
    def __init__(self, conn: SqlConnection) -> None:
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
            f"SELECT {_INGESTION_COLUMNS} FROM ingestion_task WHERE task_id = ?",
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

    def find_by_hash(self, user_id: str, file_hash: str) -> dict[str, object] | None:
        """按幂等键查已有任务（不存在返回 None）。

        为什么单独开一个查询而不是让调用方"先 create 再比对列表长度"（旧写法）：后者
        既做了一次 O(n) 扫描，又在并发下不可靠 —— 另一个请求刚插进来的行会让长度差分
        得出错误的 `reused` 结论。上传端点要靠它决定**要不要落盘**，因此必须是一次
        精确、原子的点查（审查报告 M4）。
        """
        row = self._conn.execute(
            f"SELECT {_INGESTION_COLUMNS} FROM ingestion_task "
            "WHERE user_id = ? AND file_hash = ?",
            (user_id, file_hash),
        ).fetchone()
        return None if row is None else dict(row)

    def all_source_files(self) -> list[str]:
        """全部台账的 `source_file`（跨用户）。

        为什么不按用户过滤：上传目录是**共享的**，一个文件只要被**任何**一条台账引用就
        不该被判为孤儿。按用户过滤会把别人在用的文件算成垃圾（回收动作里那是数据丢失）。
        """
        rows = self._conn.execute(
            "SELECT source_file FROM ingestion_task WHERE source_file IS NOT NULL"
        ).fetchall()
        return [str(r["source_file"]) for r in rows]

    # -- transitions ---------------------------------------------------------

    def advance(self, task_id: str, status: str) -> None:
        """Move to a valid next status. `indexed` / `failed` stamp finished_at.

        `status == current` is allowed and is a no-op write (an idempotent caller may re-assert
        the same state); any other transition must be in _INGESTION_TRANSITIONS.
        """
        if status not in INGESTION_STATUSES:
            raise IngestionBadStatus(f"unknown status: {status!r}")
        current = str(self.get(task_id)["status"])
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

    def relink_source(self, task_id: str, source_file: str) -> None:
        """把台账指到该文件的新位置。

        为什么需要：`file_hash` 幂等意味着"同一份字节只登记一次"，于是**重复上传不会重新
        落盘**（M4 的修复）。但如果那个文件已经被人工删除、或数据卷被重置过，只靠台账那条
        记录就会指向一个不存在的路径 —— 解析直接失败。此时正确的做法是：把字节重新写下来，
        再把台账指过去，而不是让用户看到"重复上传同一个文件却报错"。
        """
        cur = self._conn.execute(
            "UPDATE ingestion_task SET source_file = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE task_id = ?",
            (source_file, task_id),
        )
        if cur.rowcount == 0:
            raise IngestionNotFound(f"ingestion task not found: {task_id}")
        self._conn.commit()
