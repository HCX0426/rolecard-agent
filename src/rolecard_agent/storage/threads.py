"""`session_thread` 写链的唯一 repository（2026-10-02 轮 `R102-05` 第二步，`R102-62` 的归宿）。

第一步（批 2）收的是两件公共动作：`touch_thread` 唯一出处 + 按 thread_id 级联删唯一入口。
第二步收的是**剩下的全部写点**：从前 `api/routers/sessions.py` 里 5 处、`core/sync.py` 2 处、
`core/reachout/inbox.py` 1 处、`core/memory_distill.py` 1 处、`roles/service.py` 1 处、
`api/routers/sync.py` 1 处 —— 一层之上一把六个写入者，而这张表的形状（毫秒 `updated_at`、
`title` 的 COALESCE 语义、`distilled_at_seq` 是游标不是计数）每一条都有它的道理。

判据在门禁的 `session_thread write seam`：**除本模块与 `storage/db.py`（形状迁移与身份
重命名那条路）之外，任何文件写这张表即红。** 越层的坏处不是读不出来，是"改一处口径而
另外五处不动"—— `R102-42` 那一族（同一句理由散在七处、注释传到第二处就停）的正面解法。

这里刻意**不 commit 也不 rollback** 的只有 `set_current_role`（调用方要用 rowcount 判
"这条会话不归他"，未命中必须先结束事务再抛，`R102-42`）；其余写完即提交，与各调用点
从前自己的写法逐字等价。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rolecard_agent.storage.db import (
    SqlConnection,
    thread_id_carriers,
)

#: `touch_thread` 的线格式必须是毫秒（`R102-62`）：侧栏按 `updated_at` 排序，两次改动落在
#: 同一秒时秒级精度分不出先后，症状是"刚聊过的那条沉下去了"。精度依据只许写在这里一次。
_TOUCH_SQL = (
    "UPDATE session_thread SET updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
    "WHERE thread_id = ?"
)


def touch_thread(conn: SqlConnection, thread_id: str) -> None:
    """把会话顶到"刚刚"（毫秒精度，唯一出处）。"""
    conn.execute(_TOUCH_SQL, (thread_id,))


def delete_thread_everywhere(conn: SqlConnection, thread_id: str) -> dict[str, int]:
    """按 thread_id 级联删的**唯一入口**（`R102-26`/`48`；2026-10-02 拍板：真删）。

    名单**现数现用**（`thread_id_carriers()`）而不是调用点自列清单 —— 从前的两条删除
    路径（单删会话 / 整份替换）各自写死 `("checkpoints", "writes")` 两张表，
    `command_approval` 恰好都不在名单里，已删会话的待批审批就这么永远挂在队列上。
    走 app 连接而不是 saver 自己的 `delete_thread`，是因为**同一连接才能把删检查点、
    删载体行、删 thread 行包进同一个事务**（saver 是另一条连接，跨不过来 —— 那也是
    "整份替换没法包成一个跨两连接大事务"的根源，`R102-48` 的残留窗口）。调用方负责
    先拿 `thread_write(thread_id)`：在飞轮次不该被从脚下抽走检查点（R28-03 同类事故）。

    返回每张表删掉的行数（审计与测试用）。
    """
    stats: dict[str, int] = {}
    for table in thread_id_carriers(conn):
        cur = conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
        stats[table] = max(cur.rowcount, 0)
    cur = conn.execute("DELETE FROM session_thread WHERE thread_id = ?", (thread_id,))
    stats["session_thread"] = max(cur.rowcount, 0)
    conn.commit()
    return stats


def delete_threads_for_user(conn: SqlConnection, user_id: str) -> int:
    """整份替换那条路径的收尾：删掉这个身份名下**剩下**的会话行。

    逐条级联删（`delete_thread_everywhere`）走的是"有检查点要一起清"的那条路；这里只兜住
    没有载体行的空壳会话。返回真正被这里删掉的行数 —— 调用方要把两个数**相加**才是
    "清了多少条会话"，只取这一个数会报 0（`R102-05` 复核时当场照出的读数错，见
    `api/routers/sync.py::_clear_for_replace`）。
    """
    cur = conn.execute("DELETE FROM session_thread WHERE user_id = ?", (user_id,))
    return max(cur.rowcount, 0)


def create_thread(
    conn: SqlConnection,
    *,
    thread_id: str,
    user_id: str,
    role_id: str,
    tool_epoch: int,
    title: str | None = None,
) -> None:
    """新建一条会话并绑住角色（`tool_epoch` 是"这轮工具集是哪一代"的戳）。"""
    if title is None:
        conn.execute(
            "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch) "
            "VALUES (?, ?, ?, ?)",
            (thread_id, user_id, role_id, tool_epoch),
        )
    else:
        conn.execute(
            "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch, title)"
            " VALUES (?, ?, ?, ?, ?)",
            (thread_id, user_id, role_id, tool_epoch, title),
        )
    conn.commit()


def ensure_thread(
    conn: SqlConnection,
    *,
    thread_id: str,
    user_id: str,
    role_id: str,
    tool_epoch: int,
    title: str,
) -> None:
    """主动开口的固定话题线程：已存在就**一个字都不动**（`ON CONFLICT DO NOTHING`）。

    重开一次主动会话不该把既有那台的标题顶掉，也不该把 `distilled_at_seq` 游标清零。
    """
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch, title)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(thread_id) DO NOTHING",
        (thread_id, user_id, role_id, tool_epoch, title),
    )
    conn.commit()


def set_model(conn: SqlConnection, thread_id: str, model_name: str | None) -> None:
    """会话级模型覆盖（`None` = 清除覆盖，回到"角色 → 默认"那条解析链）。"""
    conn.execute(
        "UPDATE session_thread SET model_name = ? WHERE thread_id = ?",
        (model_name, thread_id),
    )
    touch_thread(conn, thread_id)
    conn.commit()


def set_mode(conn: SqlConnection, thread_id: str, mode: str | None) -> None:
    """会话级「对话/智能体」覆盖。"""
    conn.execute(
        "UPDATE session_thread SET agent_mode = ? WHERE thread_id = ?",
        (mode, thread_id),
    )
    touch_thread(conn, thread_id)
    conn.commit()


def set_title(conn: SqlConnection, thread_id: str, title: str) -> None:
    """显式改标题（与 `seed_title` 的"只在还没标题时兜一个"是两种语义）。"""
    conn.execute(
        "UPDATE session_thread SET title = ? WHERE thread_id = ?",
        (title, thread_id),
    )
    touch_thread(conn, thread_id)
    conn.commit()


def seed_title(conn: SqlConnection, thread_id: str, fallback: str) -> None:
    """第一条消息到达时给个标题：`COALESCE` 让"已经有标题"的会话不被第二次覆盖。"""
    conn.execute(
        "UPDATE session_thread SET title = COALESCE(title, ?) WHERE thread_id = ?",
        (fallback, thread_id),
    )
    touch_thread(conn, thread_id)
    conn.commit()


def set_distilled_seq(conn: SqlConnection, *, thread_id: str, message_count: int) -> None:
    """提取游标：记的是"上次提取时这个消息数"，差值攒够 N 轮才再提一次。"""
    conn.execute(
        "UPDATE session_thread SET distilled_at_seq = ? WHERE thread_id = ?",
        (message_count, thread_id),
    )
    conn.commit()


def set_current_role(conn: SqlConnection, *, thread_id: str, user_id: str, role_id: str) -> int:
    """换角色：只动 `current_role_id`，**不碰消息历史**。

    `WHERE thread_id = ? AND user_id = ?` 是关键 —— 目标卡已经在调用方按归属查过一遍
    （fail-before-write），这条 UPDATE 再把"这条会话是不是他的"交给库判：返回 0 行就是
    不是他的。调用方拿到 0 必须**先 rollback 再抛**（0 行的 UPDATE 也开了写事务，
    `R102-42`）。
    """
    cur = conn.execute(
        "UPDATE session_thread SET current_role_id = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE thread_id = ? AND user_id = ?",
        (role_id, thread_id, user_id),
    )
    return max(cur.rowcount, 0)


#: 对面同步过来的会话载荷里，本家会落的字段（多一个都不写：kind 之外的键是第二真相）。
_IMPORTED_COLUMNS = ("current_role_id", "model_name", "agent_mode", "title")


def insert_imported_thread(
    conn: SqlConnection, *, thread_id: str, user_id: str, payload: Mapping[str, Any]
) -> None:
    """导入一条对面来的会话（新线程）。

    `current_role_id` 缺失时落到 `general_assistant` —— 对面可能有我们没配的卡，那不该
    让这条会话变成无主行。
    """
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, model_name,"
        " agent_mode, title) VALUES (?, ?, ?, ?, ?, ?)",
        (
            thread_id,
            user_id,
            str(payload.get("current_role_id") or "general_assistant"),
            payload.get("model_name"),
            payload.get("agent_mode"),
            payload.get("title"),
        ),
    )
    conn.commit()


def update_imported_thread(
    conn: SqlConnection, *, thread_id: str, user_id: str, payload: Mapping[str, Any]
) -> None:
    """对面同名会话已在本机：只把 `_IMPORTED_COLUMNS` 那几列对上，然后顶到"刚刚"。"""
    conn.execute(
        "UPDATE session_thread SET title = ?, current_role_id = ?"
        " WHERE thread_id = ? AND user_id = ?",
        (
            payload.get("title"),
            str(payload.get("current_role_id") or "general_assistant"),
            thread_id,
            user_id,
        ),
    )
    touch_thread(conn, thread_id)
    conn.commit()
