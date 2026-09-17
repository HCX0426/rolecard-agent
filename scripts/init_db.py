"""Bootstrap SQLite: apply schema in the fixed order (see storage/db.py), then seed the
built-in roles from roles/seed.py.

Built-in roles live in CODE, not in a SQL fixture: `medical_archivist` is undeletable
(role_card.is_builtin = 1) and its tool whitelist is a security boundary, so it must not be
ordinary editable data.

Run:  python scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_importable() -> None:
    """Make `rolecard_agent` importable when run straight from a clone.

    The README promises `python scripts/init_db.py` works without an editable install, so the
    import has to be arranged here rather than assumed.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main() -> int:
    _ensure_importable()

    from rolecard_agent.config import Settings
    from rolecard_agent.core.checkpointer import make_checkpointer
    from rolecard_agent.core.plugins import seed_plugin_rows
    from rolecard_agent.domains.registry import DOMAINS
    from rolecard_agent.roles.service import RoleCardService
    from rolecard_agent.storage.db import bootstrap, connect

    settings = Settings.from_env()
    conn = connect(settings.sqlite_path)

    # Schemas are applied for every REGISTERED domain, not only the enabled ones: the table
    # should exist regardless, so that toggling a plugin never requires DDL.
    applied = bootstrap(conn, enabled_domains=DOMAINS)

    # ENABLED state lives in the database, not in this script. A fresh database starts with
    # every registered domain on; from then on the table is the source of truth and re-running
    # this script must not silently re-enable something an operator switched off.
    # （与 create_app 共用同一个函数 —— 此前是两处镜像实现，属于会漂移的重复。）
    seed_plugin_rows(conn, DOMAINS)

    # LangGraph owns the checkpoint tables and creates them in setup(). Doing it here as well
    # means a cloned database is fully self-describing, and removes the "the very first write
    # failed with no such table" class of first-run bug.
    make_checkpointer(conn)

    seeded = RoleCardService(conn).seed_builtins()
    RoleCardService(conn).seed_domain_roles()

    enabled = [
        row[0]
        for row in conn.execute("SELECT plugin_id FROM plugin WHERE enabled = 1 ORDER BY plugin_id")
    ]

    print(f"database   : {settings.sqlite_path}")
    for name in applied:
        print(f"  schema   : {name}")
    print(f"  registered: {', '.join(DOMAINS) or '(none)'}")
    print(f"  enabled  : {', '.join(enabled) or '(none)'}")
    print(f"  roles    : {seeded} built-in role(s) seeded")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
