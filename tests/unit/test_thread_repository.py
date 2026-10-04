"""`session_thread` 写链收进 repository 之后要咬住的东西（`R102-05` 第二步）。

四条判据：

  1. **越层写这张表 = 红**，而且 `pytest` 这一条命令就能否证（门禁的
     `session_thread write seam` 同一条规则，判据不能只住在一个地方）；
  2. **`title` 的两种语义不许被归一**：`set_title` 是"用户显式改名"，`seed_title` 是
     "第一条消息到达时兜一个，已有标题的不覆盖"（`COALESCE`）—— 这是从前散在路由层里的
     两条不同 SQL，搬层时最容易被"顺手合并"的那一对；
  3. **`set_current_role` 改到 0 行时调用方必须能结束事务**（`R102-42` 的那一族：
     0 行的 UPDATE 也开了写锁），而且归属不匹配就是不动别人的会话；
  4. **整份替换报的"清了几条会话"是真的条数**：逐条级联删跑过之后，收尾那句
     `DELETE ... WHERE user_id` 恒为 0 行，从前 `cleared["thread"]` 直接取那一个数，
     于是清了 12 条也报 0 条。
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from rolecard_agent.base.identity import DEFAULT_TENANT_ID, DEFAULT_USER_ID
from rolecard_agent.storage.db import bootstrap, connect
from rolecard_agent.storage.threads import (
    create_thread,
    delete_thread_everywhere,
    delete_threads_for_user,
    seed_title,
    set_current_role,
    set_distilled_seq,
    set_model,
    set_title,
    thread_id_carriers,
    touch_thread,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "rolecard_agent"
SEAM = {"src/rolecard_agent/storage/threads.py", "src/rolecard_agent/storage/db.py"}
MARKERS = (
    "INSERT INTO session_thread",
    "UPDATE session_thread",
    "DELETE FROM session_thread",
)


def _write_sites() -> list[str]:
    """代码里（不含注释/散文）写 `session_thread` 的语句位置。"""
    sites: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(marker in node.value for marker in MARKERS)
            ):
                sites.append(f"{rel}:{node.lineno}")
    return sites


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "app.db")
    bootstrap(c, enabled_domains=("health",))
    c.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    c.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) VALUES (?, ?, ?)",
        (DEFAULT_USER_ID, DEFAULT_TENANT_ID, "本地用户"),
    )
    c.commit()
    try:
        yield c
    finally:
        c.close()


def test_no_layer_above_storage_writes_this_table() -> None:
    outside = [s for s in _write_sites() if s.split(":")[0] not in SEAM]
    assert outside == []
    # 反向一臂：repository 里必须真的躺着这些写点，否则上面那条永远在空转。
    assert len(_write_sites()) >= 5


def test_seed_title_does_not_overwrite_an_existing_one(conn) -> None:
    create_thread(
        conn, thread_id="s_1", user_id=DEFAULT_USER_ID, role_id="girl", tool_epoch=1
    )
    seed_title(conn, "s_1", "第一句的前二十四字")
    assert conn.execute("SELECT title FROM session_thread WHERE thread_id='s_1'").fetchone()[0] == (
        "第一句的前二十四字"
    )
    seed_title(conn, "s_1", "第二句也不该顶掉它")
    assert conn.execute("SELECT title FROM session_thread WHERE thread_id='s_1'").fetchone()[0] == (
        "第一句的前二十四字"
    )
    set_title(conn, "s_1", "用户自己改的名")
    assert conn.execute("SELECT title FROM session_thread WHERE thread_id='s_1'").fetchone()[0] == (
        "用户自己改的名"
    )


def test_millisecond_touch_survives_the_move(conn) -> None:
    """侧栏同秒分先后靠的是 `%f`：改回秒级这一条必须红（变异点）。"""
    create_thread(conn, thread_id="s_2", user_id=DEFAULT_USER_ID, role_id="girl", tool_epoch=1)
    set_model(conn, "s_2", "some-model")
    stamp = str(
        conn.execute(
            "SELECT updated_at FROM session_thread WHERE thread_id='s_2'"
        ).fetchone()[0]
    )
    assert "." in stamp, f"updated_at 丢了毫秒：{stamp!r}"
    touch_thread(conn, "s_2")
    conn.commit()
    again = str(
        conn.execute(
            "SELECT updated_at FROM session_thread WHERE thread_id='s_2'"
        ).fetchone()[0]
    )
    assert again >= stamp and "." in again
    set_distilled_seq(conn, thread_id="s_2", message_count=7)
    assert conn.execute(
        "SELECT distilled_at_seq FROM session_thread WHERE thread_id='s_2'"
    ).fetchone()[0] == 7


def test_foreign_owner_returns_zero_without_hanging_a_write_txn(conn) -> None:
    """0 行的 UPDATE 也开了写事务：repository 交回 rowcount，调用方 rollback 才能放行别人。"""
    create_thread(conn, thread_id="s_3", user_id=DEFAULT_USER_ID, role_id="girl", tool_epoch=1)
    changed = set_current_role(conn, thread_id="s_3", user_id="someone-else", role_id="girl")
    assert changed == 0
    assert conn.in_transaction  # 事务确实开着 —— 这条正是 R102-42 那一族的形状
    conn.rollback()
    assert not conn.in_transaction
    second = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2])
    second.row_factory = sqlite3.Row
    # 归属不匹配就没动过任何一行
    assert second.execute(
        "SELECT current_role_id FROM session_thread WHERE thread_id='s_3'"
    ).fetchone()[0] == "girl"
    second.close()


def test_replace_path_reports_the_true_thread_count(conn) -> None:
    """走 `features/sync_service.py::clear_threads_for_replace` 那条真路径：报的必须是真条数。

    `graph` 给了非 None 就会逐条级联删（那正是生产形状），于是收尾那句
    `DELETE ... WHERE user_id` 恒为 0 行 —— 从前 `cleared["thread"]` 只取那一个数，
    清了 3 条也报 0 条。变异：把相加那句改回只取 rowcount ⇒ 这条红。
    （清空拆成行类（与导入共事务）与 thread 类（本函数）两半，thread 的真删与提交住在
    这一个函数里。2026-10-04 service 收口把它从路由搬进 service —— 从前这条用例得
    `from api.routers.sync import _clear_threads_for_replace`，即测试伸手进路由的私有函数，
    那本身就是"业务逻辑住在 HTTP 层"的症状。）
    """
    from rolecard_agent.features.sync_service import clear_threads_for_replace

    for tid in ("s_x", "s_y", "s_z"):
        create_thread(conn, thread_id=tid, user_id=DEFAULT_USER_ID, role_id="girl", tool_epoch=1)

    cleared = clear_threads_for_replace(conn, user_id=DEFAULT_USER_ID, graph=object())
    assert cleared["thread"] == 3, cleared
    assert conn.execute("SELECT COUNT(*) FROM session_thread").fetchone()[0] == 0


def test_replace_reports_the_threads_it_actually_cleared(conn) -> None:
    for tid in ("s_a", "s_b", "s_c"):
        create_thread(conn, thread_id=tid, user_id=DEFAULT_USER_ID, role_id="girl", tool_epoch=1)
    cleared = 0
    for row in conn.execute(
        "SELECT thread_id FROM session_thread WHERE user_id = ?", (DEFAULT_USER_ID,)
    ).fetchall():
        cleared += delete_thread_everywhere(conn, str(row["thread_id"]))["session_thread"]
    cleared += delete_threads_for_user(conn, DEFAULT_USER_ID)
    assert cleared == 3, "逐条级联删之后收尾那句恒为 0，两个数相加才是真条数"
    assert conn.execute("SELECT COUNT(*) FROM session_thread").fetchone()[0] == 0
    # 级联删用的是权威名单，且那张名单里不含 session_thread 自己（它由本模块单独删）
    assert "session_thread" not in thread_id_carriers(conn)
