r"""Bootstrap SQLite: apply schema in the fixed order (see storage/db.py), then seed the
built-in roles from roles/seed.py.

Built-in roles live in CODE, not in a SQL fixture: they are undeletable
(role_card.is_builtin = 1) and their tool whitelist is a security boundary, so it must not be
ordinary editable data. 域角色不再开机播种（2026-10-31 用户拍板：默认常驻只有通用助手；
开机自动种会让删掉的角色每次重启复活）—— `seed_domain_roles` 保留给显式调用。

Run:  .venv\Scripts\python.exe scripts/init_db.py
      .venv\Scripts\python.exe scripts/init_db.py --seed-domain-roles  # 铺世界时显式种域角色
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# **必须先于一切 rolecard_agent 顶层 import**：_ensure_importable 的职责是"无 editable
# 安装的 clone 上 python scripts/init_db.py 能跑"（本模块 docstring 的承诺）。从前它在
# main() 里才被调 —— 但下面这行顶层 import 在 main() 之前就 ModuleNotFoundError 了，
# 承诺从未兑现（P14 探针首跑实测：`.\.venv\Scripts\python.exe scripts\init_db.py`
# 按文档原样执行当场炸）。
_src = Path(__file__).resolve().parents[1] / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from rolecard_agent.base.identity import resolve_instance_identity  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建库 + 播种出厂角色")
    parser.add_argument(
        "--seed-domain-roles",
        action="store_true",
        help="显式播种域出厂角色卡（默认不种：2026-10-31 教义——开机只留通用助手常驻）。"
        "铺演示/评测世界的脚本要它：persona 三件与 run_eval 的用例点名域角色。",
    )
    args = parser.parse_args(argv)
    from rolecard_agent.config import Settings
    from rolecard_agent.core.plugins import seed_plugin_rows
    from rolecard_agent.core.storage.checkpointer import make_checkpointer
    from rolecard_agent.core.storage.migrations import MIGRATION_PLAN
    from rolecard_agent.domains.registry import DOMAINS
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
    # 域角色**不默认播种**（2026-10-31 用户拍板：默认常驻只有通用助手；开机自动种
    # 会让用户删掉的角色每次重启都复活）。`--seed-domain-roles` 留给显式铺世界的调用方
    # （nightly 的 persona 三件点名 medical_archivist；run_eval 自己会种，不经过这里）。
    domain_seeded = 0
    if args.seed_domain_roles:
        from rolecard_agent.domains.registry import domain_seed_roles

        domain_seeded = store.seed_domain_roles(domain_seed_roles(), user_id=owner)

    enabled = [
        row[0]
        for row in conn.execute("SELECT plugin_id FROM plugin WHERE enabled = 1 ORDER BY plugin_id")
    ]

    print(f"database   : {settings.sqlite_path}")
    for name in applied:
        print(f"  schema   : {name}")
    print(f"  registered: {', '.join(DOMAINS) or '(none)'}")
    print(f"  enabled  : {', '.join(enabled) or '(none)'}")
    print(f"  roles    : {seeded} built-in role(s) seeded, {domain_seeded} domain role(s)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
