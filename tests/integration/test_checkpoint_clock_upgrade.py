"""检查点时钟列的旧形态升级 —— `test_legacy_schema_upgrade.py` 的镜像（A2）。

为什么单立这一份：`checkpoints` 的 DDL **不在** bootstrap 的声明面里 —— 表由
langgraph 的 `saver.setup()` 建（七列，无 `created_at`），我们那一列由
`ensure_checkpoint_clock` 在启动时补（checkpointer.py：「库里的 DDL 是我们自己的事」）。
schema 侧的老形态归 `tests/fixtures/legacy_schemas/` 管；这一侧的老形态 = 「setup
建完、ensure 从没跑过」的现场 —— `created_at` 是后补的列，任何一次补列发版之前开过
的库都是它，而且它不走 `user_version` 代际（补列不在 `_migrate` 的声明面上），补错了
只有本文件替它说话。

断言对偶 schema 那份的三条纪律：老行保全（补列不许凭空造历史时间，老行
`created_at` 必须留 NULL —— NULL 正是修剪那侧 fail-open「没钟不删」的输入）、
幂等（重跑一遍列不重、触发器仍恰好一个、历史不被顺手盖成「现在」）、
终点收敛（老表升完的列集合与生产入口 fresh 走出来的逐列相等）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from rolecard_agent.core.storage.checkpointer import ensure_checkpoint_clock, make_checkpointer
from rolecard_agent.core.storage.migrations import MIGRATION_PLAN
from rolecard_agent.storage.db import bootstrap, connect


def _cols(conn: sqlite3.Connection) -> list[str]:
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(checkpoints)")]


def _triggers(conn: sqlite3.Connection) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='checkpoints'"
            " ORDER BY name"
        )
    ]


def _old_shape(db_path: Path) -> sqlite3.Connection:
    """发版前的老库现场：langgraph 建表、`ensure_checkpoint_clock` 从没跑过。

    建完当场自证这不是新形状 —— 与 legacy 那份的 `missing_before` 同纪律：
    fixture 要是自己已经是新库，后面每一条断言都在空转。
    """
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    SqliteSaver(conn).setup()
    assert "created_at" not in _cols(conn), "fixture 自证失败：setup 建出的表已带 created_at"
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint,"
        " metadata) VALUES ('t-old', '', 'c-old', 't', x'00', x'00')"
    )
    conn.commit()
    return conn


def test_old_clockless_table_gains_the_column_and_keeps_its_rows(tmp_path: Path) -> None:
    """补列上线的正面反证：老表升上来，列在、老行在、时钟留空、触发器恰好一个。"""
    conn = _old_shape(tmp_path / "app.db")
    ensure_checkpoint_clock(conn)
    assert "created_at" in _cols(conn), "老表没被补上 created_at —— 读钟那一侧当场 no such column"
    row = conn.execute(
        "SELECT created_at FROM checkpoints WHERE thread_id = 't-old' AND checkpoint_id = 'c-old'"
    ).fetchone()
    assert row is not None, "补列把老行弄丢了"
    assert row["created_at"] is None, "补列凭空给老行造了历史时间（那是伪造观测数据）"
    assert _triggers(conn) == ["checkpoints_stamp_created_at"], "触发器没建成或建了不止一个"
    conn.close()


def test_ensure_is_idempotent_and_leaves_history_unstamped(tmp_path: Path) -> None:
    """重启重跑（生产每次启动都跑这一句）：列不重、触发器仍一个、历史仍 NULL。"""
    conn = _old_shape(tmp_path / "app.db")
    ensure_checkpoint_clock(conn)
    before_cols = _cols(conn)
    ensure_checkpoint_clock(conn)
    assert _cols(conn) == before_cols, "第二遍改了形状（幂等被破坏）"
    assert _triggers(conn) == ["checkpoints_stamp_created_at"], "第二遍把触发器又建了一份"
    (stamp,) = conn.execute(
        "SELECT created_at FROM checkpoints WHERE thread_id = 't-old' AND checkpoint_id = 'c-old'"
    ).fetchone()
    assert stamp is None, "重跑把历史行盖成了现在 —— 时间线被改写"
    conn.close()


def test_rows_inserted_after_the_upgrade_get_a_clock(tmp_path: Path) -> None:
    """老表上迁移后的 INSERT 必须当场盖钟 —— 触发器是给老库装的新钟，不是摆设。"""
    conn = _old_shape(tmp_path / "app.db")
    ensure_checkpoint_clock(conn)
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint,"
        " metadata) VALUES ('t-new', '', 'c-new', 't', x'00', x'00')"
    )
    conn.commit()
    (stamp,) = conn.execute(
        "SELECT created_at FROM checkpoints WHERE thread_id = 't-new' AND checkpoint_id = 'c-new'"
    ).fetchone()
    assert stamp is not None and stamp.endswith("Z"), f"新行没盖钟：{stamp!r}"
    conn.close()


def test_migrated_old_table_converges_to_the_production_fresh_shape(tmp_path: Path) -> None:
    """终点收敛（实弹演练）：老表升完 == 生产入口 fresh 走出来的形状。

    fresh 一侧走 `bootstrap` + `make_checkpointer`（生产启动的完整链），老表一侧只走
    「setup → 补列」—— 起点不同，终点的列集合与触发器名单必须一样：以后谁给生产链
    加了列或触发器而没管老库，红的是这一条。
    """
    fresh = connect(tmp_path / "fresh.db")
    bootstrap(fresh, enabled_domains=(), plan=MIGRATION_PLAN)
    make_checkpointer(fresh)

    old = _old_shape(tmp_path / "old.db")
    ensure_checkpoint_clock(old)

    problems: list[str] = []
    fresh_cols, old_cols = set(_cols(fresh)), set(_cols(old))
    if fresh_cols != old_cols:
        problems.append(
            f"列集合分叉 fresh-only={sorted(fresh_cols - old_cols)}"
            f" old-only={sorted(old_cols - fresh_cols)}"
        )
    if _triggers(fresh) != _triggers(old):
        problems.append(f"触发器分叉 fresh={_triggers(fresh)} old={_triggers(old)}")
    assert not problems, "老表升级未收敛：" + "；".join(problems)
    fresh.close()
    old.close()
