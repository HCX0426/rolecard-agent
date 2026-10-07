"""Bootstrap SQLite: apply schema in the fixed order (see storage/db.py), then seed the
built-in roles from roles/seed.py and each domain's seed roles from its own `DomainSpec`.

Built-in roles live in CODE, not in a SQL fixture: they are undeletable
(role_card.is_builtin = 1) and their tool whitelist is a security boundary, so it must not be
ordinary editable data. 域角色同理是代码出厂（类型是自定义，可改可删），只是清单住在
各自的域包里 —— 见 `domains/registry.domain_seed_roles`。

Run:  python scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from rolecard_agent.base.identity import resolve_instance_identity


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
    from rolecard_agent.core.plugins import seed_plugin_rows
    from rolecard_agent.core.storage.checkpointer import make_checkpointer
    from rolecard_agent.core.storage.migrations import MIGRATION_PLAN
    from rolecard_agent.domains.registry import DOMAINS, domain_seed_roles
    from rolecard_agent.roles.service import RoleCardService
    from rolecard_agent.storage.db import bootstrap, connect

    settings = Settings.from_env()
    conn = connect(settings.sqlite_path)

    # Schemas are applied for every REGISTERED domain, not only the enabled ones: the table
    # should exist regardless, so that toggling a plugin never requires DDL.
    applied = bootstrap(
        conn,
        enabled_domains=DOMAINS,
        # 业务迁移（清重 / 整表重建 / 搬层）的计划从 core 交进来（`R102-08` +
        # 2026-10-04 审查快照 P1-6：storage 不再反向 import，也不再认识业务表名）。
        plan=MIGRATION_PLAN,
    )

    # ENABLED state lives in the database, not in this script. A fresh database starts with
    # every registered domain on; from then on the table is the source of truth and re-running
    # this script must not silently re-enable something an operator switched off.
    # （与 create_app 共用同一个函数 —— 此前是两处镜像实现，属于会漂移的重复。）
    seed_plugin_rows(conn, DOMAINS)

    # LangGraph owns the checkpoint tables and creates them in setup(). Doing it here as well
    # means a cloned database is fully self-describing, and removes the "the very first write
    # failed with no such table" class of first-run bug.
    make_checkpointer(conn)

    # 出厂卡也有主人：这台实例的主人（§4.1 的实例级身份）。空 IDENTITY_USER_ID = 本机那份。
    owner = resolve_instance_identity(settings)
    store = RoleCardService(conn)
    seeded = store.seed_builtins(user_id=owner)
    # 域种子角色由**各域自己的声明**给出（`DomainSpec.seed_roles` 聚合）：本脚本不点名
    # 任何域 —— 新增一个带角色的域，这里一个字都不用改。
    store.seed_domain_roles(domain_seed_roles(), user_id=owner)

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
