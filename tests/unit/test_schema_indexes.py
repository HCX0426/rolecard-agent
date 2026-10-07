"""`R28-22` 那三条登记过的缺索引：在不在，以及**老库下次开机拿不拿得到**。

这条只钉"索引存在"，不钉"查询计划会不会选它" —— 表小的时候 SQLite 明明有索引也可能选全表扫，
拿 `EXPLAIN QUERY PLAN` 断言会变成一条随机红的用例。选不选是另一件事，规模上来之后再看。
"""

from __future__ import annotations

from pathlib import Path

from rolecard_agent.core.storage.migrations import MIGRATION_PLAN
from rolecard_agent.domains.registry import DOMAINS
from rolecard_agent.storage.db import bootstrap, connect

#: 台账 `R28-22` 数的那三条（列序按实际查询，不按表名猜）。
REGISTERED = {
    "agent_reachout": "idx_reachout_user_state",
    "model_provider": "idx_model_provider_user",
    "command_approval": "idx_command_approval_thread",
}


def _indexes(conn, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA index_list({table})")}


def test_the_registered_indexes_ship_with_the_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "app.db")
    bootstrap(conn, enabled_domains=DOMAINS, plan=MIGRATION_PLAN)
    for table, index in REGISTERED.items():
        assert index in _indexes(conn, table), f"{table} 上没有 {index}"
    conn.close()


def test_an_old_db_gets_them_on_the_next_boot(tmp_path: Path) -> None:
    """索引是每次启动 `CREATE INDEX IF NOT EXISTS` 跑的 ⇒ 老库不需要迁移就能拿到。

    这里用"建好之后手动 drop 掉再 bootstrap 一次"复现"那份库是在有索引之前建的"这个状态 ——
    与真机上的老库等价（那条 `schema.sql` 里没有这些语句的年代建起来的库）。
    """
    db = tmp_path / "old.db"
    conn = connect(db)
    bootstrap(conn, enabled_domains=DOMAINS, plan=MIGRATION_PLAN)
    for index in REGISTERED.values():
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    conn.commit()
    assert all(
        index not in _indexes(conn, table) for table, index in REGISTERED.items()
    ), "夹具没能把索引去掉"

    bootstrap(conn, enabled_domains=DOMAINS, plan=MIGRATION_PLAN)
    for table, index in REGISTERED.items():
        assert index in _indexes(conn, table), f"老库升上来之后还是没有 {index}"
    conn.close()
