"""sync 写入口归 owner 之后的两件事：列集现算 + 搬迁语义不变（2026-10-04 快照的
sync 写入口条目的验收现场）。

快照给这条的验收原文是「给 role_memory_item 加测试列时 sync 路径自动带上」——
第一支用例就是它的现场：**加列不许再改同步的代码**。从前 INSERT 的列集是
`core/sync.py` 里手抄的第二份事实面，表加了列而清单没跟上，这条链静默少列。

第二支钉搬迁的语义不变：`restore_row` 是从 `_write_memory` / `_write_reachout` 逐条
搬回来的（empty→skipped、同 uid→updated、异主→foreign、keep_both 换新 uid），
往返矩阵（`tests/test_sync_roundtrip_matrix.py`）管端到端，这里管**单函数的行为面**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rolecard_agent.base.identity import DEFAULT_USER_ID, ensure_identity_row
from rolecard_agent.core.memory import restore_row as restore_memory
from rolecard_agent.core.reachout.inbox import restore_row as restore_reachout
from rolecard_agent.storage.db import bootstrap, connect

OTHER = "intruder"


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "app.db")
    bootstrap(c, enabled_domains=("health",))
    ensure_identity_row(c, DEFAULT_USER_ID)
    ensure_identity_row(c, OTHER)
    c.commit()
    try:
        yield c
    finally:
        c.close()


def test_a_new_column_flows_through_restore_without_touching_sync(
    conn: sqlite3.Connection,
) -> None:
    """**加列自动带上**：schema 长一列，写路径跟着表走，同步的代码一个字不改。

    两种都要对：
      * wire 带了新键（对端也加了列）→ 列集现算把它落下去；
      * wire 没带 → 这一列**整列不出现**，让表的默认说了算 —— 反方向的坑是"插一个显式
        NULL 把 DEFAULT 抹掉"，那会把"没传"写成"传了空"。
    """
    conn.execute("ALTER TABLE role_memory_item ADD COLUMN memo TEXT")
    conn.execute("ALTER TABLE agent_reachout ADD COLUMN memo TEXT")
    conn.commit()

    outcome = restore_memory(
        conn,
        user_id=DEFAULT_USER_ID,
        uid="m1",
        payload={"text": "用户住在上海", "memo": "新加的列也要跟着走"},
    )
    assert outcome == "created"
    row = conn.execute(
        "SELECT text, memo FROM role_memory_item WHERE uid = ?", ("m1",)
    ).fetchone()
    assert row["text"] == "用户住在上海"
    assert row["memo"] == "新加的列也要跟着走", "wire 带了值，列集现算必须落下去"

    assert restore_memory(
        conn, user_id=DEFAULT_USER_ID, uid="m2", payload={"text": "第二条"}
    ) == "created"
    row2 = conn.execute(
        "SELECT memo FROM role_memory_item WHERE uid = ?", ("m2",)
    ).fetchone()
    assert row2["memo"] is None, "wire 没带 → 整列不出现，表默认说了算（别写显式 NULL）"

    reach = restore_reachout(
        conn,
        user_id=DEFAULT_USER_ID,
        payload={
            "role_id": "girl",
            "role_name": "她",
            "text": "她先开口",
            "created_at": "2026-10-04 12:00:00",
            "memo": "reachout 也一样",
        },
    )
    assert reach == "created"
    got = conn.execute(
        "SELECT memo FROM agent_reachout WHERE text = ?", ("她先开口",)
    ).fetchone()
    assert got["memo"] == "reachout 也一样"
    conn.commit()  # restore 不自收口（登记在册的"写而不收口"），调用方统一 commit


def test_restore_keeps_the_private_writer_semantics(
    conn: sqlite3.Connection,
) -> None:
    """搬迁语义逐条不变：同 uid 更新、异主拒写、keep_both 换新 uid、空文本跳过。"""
    assert (
        restore_memory(conn, user_id=DEFAULT_USER_ID, uid="u1", payload={"text": "老文本"})
        == "created"
    )
    assert (
        restore_memory(conn, user_id=DEFAULT_USER_ID, uid="u1", payload={"text": "新文本"})
        == "updated"
    )
    assert (
        conn.execute("SELECT text FROM role_memory_item WHERE uid = 'u1'").fetchone()["text"]
        == "新文本"
    )

    # 异主的 uid：什么都不写（M2b 的 uid 纪律在跨机器时的唯一守卫）
    conn.execute(
        "INSERT INTO role_memory_item (user_id, role_id, uid, text) "
        "VALUES (?, '', 'u-x', '别人的')",
        (OTHER,),
    )
    conn.commit()
    assert (
        restore_memory(conn, user_id=DEFAULT_USER_ID, uid="u-x", payload={"text": "我的版本"})
        == "foreign"
    )
    assert (
        conn.execute("SELECT text FROM role_memory_item WHERE uid = 'u-x'").fetchone()["text"]
        == "别人的"
    )

    # keep_both：换一枚新 uid 插一条，两条并存
    assert (
        restore_memory(
            conn,
            user_id=DEFAULT_USER_ID,
            uid="u1",
            payload={"text": "新文本", "keep_both": True},
        )
        == "created"
    )
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM role_memory_item WHERE text = '新文本'"
    ).fetchone()
    assert int(rows["c"]) == 2, "两份都留 = 两条并存，不是覆盖"

    # 空文本 = 跳过（对面 payload 的 text 字段空/缺）
    assert (
        restore_memory(conn, user_id=DEFAULT_USER_ID, uid="u-empty", payload={}) == "skipped"
    )
    conn.commit()


def test_reachout_restore_is_append_only(
    conn: sqlite3.Connection,
) -> None:
    """同 (角色, 时刻, 文本) 已存在 → skipped（只追加，永不覆盖 —— 身份含文本）。"""
    payload = {
        "role_id": "girl",
        "role_name": "她",
        "text": "这句说过了",
        "created_at": "2026-10-04 08:00:00",
        "state": "unread",
    }
    assert restore_reachout(conn, user_id=DEFAULT_USER_ID, payload=dict(payload)) == "created"
    assert restore_reachout(conn, user_id=DEFAULT_USER_ID, payload=dict(payload)) == "skipped"
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM agent_reachout WHERE text = ?", ("这句说过了",)
        ).fetchone()["c"]
        == 1
    )
    conn.commit()
