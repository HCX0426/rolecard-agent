"""渠道会话绑定（`channel_session` 表）的读写 —— 外部渠道的对端 ↔ 本地会话线程。

为什么单独一个模块：`features/reachout/inbox.py` 同款形状 —— 表在内核 schema
（`core/schema.sql`），SQL 在功能自己手里（features → storage 单向合法），内核不认识
"QQ"这个概念。

**幂等与并发**：`ensure_channel_session` 是"读不到就建"，渠道侧的入站消息由**单 worker
池**串行消费（`qq.py` 的 `MAX_INFLIGHT_TURNS` 那格），同一对端不会并发走到这里 ——
但跨渠道/重启后首条消息仍可能撞行，主键兜底 IntegrityError 后重读一次。
"""

from __future__ import annotations

import sqlite3

from rolecard_agent.core.sessions import session_service
from rolecard_agent.storage.db import SqlConnection


def bound_thread(conn: SqlConnection, *, channel: str, peer: str) -> str | None:
    """这条绑定现在指着的线程 id；没绑过 = None。"""
    row = conn.execute(
        "SELECT thread_id FROM channel_session WHERE channel = ? AND peer = ?",
        (channel, peer),
    ).fetchone()
    return None if row is None else str(row["thread_id"])


def ensure_channel_session(
    conn: SqlConnection,
    *,
    channel: str,
    peer: str,
    user_id: str,
    role_id: str,
    tool_epoch: int,
) -> str:
    """对端 → 线程 id：有绑定直接用（行还在的话），没有就建会话 + 落绑定。

    **同一对端永远同一条线**是这条渠道的语义：群里同一个人今天问的与昨天问的要在
    一份历史里，她才记得住 —— 这与桌宠主动会话（`ensure_proactive_thread`）同一条
    用户拍板的道理。绑定行指向的会话被删过（用户在控制台删了它）→ 重建会话、原地
    改绑 —— 绑定表存的是"这对端该有一条线"，不是"某一行的墓碑"。
    """
    existing = bound_thread(conn, channel=channel, peer=peer)
    if existing is not None:
        if session_service.exists(conn, existing):
            return existing
        # 绑定指向的会话被删过（用户在控制台删了它）：重建会话、**原地改绑**（UPDATE，
        # 不是再 INSERT —— 主键是 (channel, peer)，再插只会撞墙）。
        thread_id = session_service.create(
            conn, user_id=user_id, role_id=role_id, tool_epoch=tool_epoch
        )
        conn.execute(
            "UPDATE channel_session SET thread_id = ?, role_id = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE channel = ? AND peer = ?",
            (thread_id, role_id, channel, peer),
        )
        conn.commit()
        return thread_id

    thread_id = session_service.create(
        conn, user_id=user_id, role_id=role_id, tool_epoch=tool_epoch
    )
    try:
        conn.execute(
            "INSERT INTO channel_session (channel, peer, thread_id, user_id, role_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, peer, thread_id, user_id, role_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # 输给并发建行的那一方：以库里的为准，自己刚建的会话行留成孤儿 ——
        # 无害（没有 checkpoint 引用，retention 与删除路径都能清它）。
        conn.rollback()
        row = bound_thread(conn, channel=channel, peer=peer)
        if row is None:
            raise
        return row
    return thread_id
