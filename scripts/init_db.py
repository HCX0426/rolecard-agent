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

# v1 ships exactly one plugin. In M2 this list is replaced by the enabled rows of the
# `plugin` table, which is also what `call_model` reads to filter tools.
ENABLED_DOMAINS: tuple[str, ...] = ("health",)


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
    from rolecard_agent.roles.service import RoleCardService
    from rolecard_agent.storage.db import bootstrap, connect

    settings = Settings.from_env()
    conn = connect(settings.sqlite_path)

    applied = bootstrap(conn, enabled_domains=ENABLED_DOMAINS)
    for domain in ENABLED_DOMAINS:
        conn.execute(
            "INSERT INTO plugin (plugin_id, display_name, enabled, sort_order) "
            "VALUES (?, ?, 1, 0) "
            "ON CONFLICT(plugin_id) DO UPDATE SET enabled = 1",
            (domain, domain),
        )
    conn.commit()

    # LangGraph owns the checkpoint tables and creates them in setup(). Doing it here as well
    # means a cloned database is fully self-describing, and removes the "the very first write
    # failed with no such table" class of first-run bug.
    make_checkpointer(conn)

    seeded = RoleCardService(conn).seed_builtins()

    print(f"database   : {settings.sqlite_path}")
    for name in applied:
        print(f"  schema   : {name}")
    print(f"  plugins  : {', '.join(ENABLED_DOMAINS)}")
    print(f"  roles    : {seeded} built-in role(s) seeded")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

