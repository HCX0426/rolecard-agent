"""SQLite connection helpers + schema bootstrap.

Bootstrap order is fixed and not optional:
  1. core/schema.sql        tenant / app_user / session_thread / plugin / audit_log
  2. roles/schema.sql       role_card
  3. domains/<x>/schema.sql for each ENABLED plugin, in DOMAINS order

Step 1 must run first: domains reference app_user(user_id).

Every connection MUST enable `PRAGMA foreign_keys = ON` - SQLite ignores foreign keys by
default, which would silently turn the ON DELETE CASCADE in domains/health/schema.sql into
a no-op and leave orphaned index rows behind.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# Domain ids become path segments, so they are validated rather than trusted.
_DOMAIN_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this schema depends on.

    `check_same_thread=False` because FastAPI serves requests from a thread pool; the
    caller is responsible for not sharing one connection across concurrent writers.
    """
    target = Path(path)
    if target.parent and str(target.parent) not in {"", "."}:
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def core_schema_path() -> Path:
    return PACKAGE_ROOT / "core" / "schema.sql"


def roles_schema_path() -> Path:
    return PACKAGE_ROOT / "roles" / "schema.sql"


def domain_schema_path(domain_id: str) -> Path:
    if not _DOMAIN_ID.match(domain_id):
        raise ValueError(f"invalid domain id: {domain_id!r}")
    return PACKAGE_ROOT / "domains" / domain_id / "schema.sql"


def schema_files(enabled_domains: Iterable[str] = ()) -> list[Path]:
    """Return the schema files to apply, in the fixed order documented above."""
    files = [core_schema_path(), roles_schema_path()]
    files.extend(domain_schema_path(d) for d in enabled_domains)
    return files


def bootstrap(conn: sqlite3.Connection, enabled_domains: Iterable[str] = ()) -> list[str]:
    """Apply every schema file. Idempotent - all DDL uses IF NOT EXISTS.

    Returns the applied file names, which is what tests assert on: a silently skipped
    schema is far worse than a loud failure.
    """
    applied: list[str] = []
    for path in schema_files(enabled_domains):
        if not path.exists():
            raise FileNotFoundError(f"schema file missing: {path}")
        conn.executescript(path.read_text(encoding="utf-8"))
        applied.append(str(path.relative_to(PACKAGE_ROOT)))
    conn.commit()
    return applied
