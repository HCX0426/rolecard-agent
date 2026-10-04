"""同步那一族的行读写原语（storage 层）—— 这些表的 SQL 只住这里。

为什么单独立一个模块（2026-10-04 依赖收口之后的 service 收口第一步）：整份替换那条链
（备份 → 清空 → 导入）以前把裸 SQL 写在 `api/routers/sync.py` 里。SQL 住在路由层的代价不是
难看，是**非 HTTP 宿主拿不到**：桌宠壳与 `scripts/` 那些取证脚本要复用"清一个身份的某类行"
这件事，只能再抄一遍 SQL，而抄的那一份不会跟着列集一起改（本仓 `R28-14` 那一族的形状）。
归属与 `storage/threads.py` 同一取向：repository 管语句，编排管顺序。

两条纪律：
  * **表名是白名单，不是字符串**：调用方只能取用这里声明的那几张表，`quote_ident` 兜住
    标识符形状。写这张表的人不需要说服读者"这里的插值是安全的"（`# noqa: S608` 那一族）。
  * **本模块不 commit 也不 rollback**：清空与导入必须共一个事务（"清了不导"的原子化），
    所以收口点归调用方 —— 已登记进 `scripts/check_consistency.py` 的 `WRITE_TXN_HELPERS`。
"""

from __future__ import annotations

from typing import Any

from rolecard_agent.storage.db import SqlConnection, quote_ident

#: 允许**按身份整类清空**的表 —— 白名单就是判据本身：不在这里的名字拼不出 DELETE。
#: `session_thread` 故意不在内（它有级联删除的图侧语义，归 `storage/threads.py`）。
ROW_TABLES: frozenset[str] = frozenset({"role_card", "role_memory_item", "agent_reachout"})

#: 允许**按身份整类读出来**的表。比白名单宽一格：删前备份必须带上会话的载体行
#: （检查点表只有 blob 与 thread_id，重放一条会话还靠这行元数据），而那一格不许被删。
#: 读与删是两个判据，所以是两张名单 —— 合成一张迟早会因为"备份少一行"而被迫放宽删侧。
READ_TABLES: frozenset[str] = ROW_TABLES | {"session_thread"}

#: 会话正文真正住在的那两张检查点表（langgraph 的载体，blob 列是 msgpack）。
CHECKPOINT_TABLES: tuple[str, ...] = ("checkpoints", "writes")


def _require(table: str, allowed: frozenset[str]) -> str:
    if table not in allowed:
        raise ValueError(f"未按名单登记的表：{table!r}（可取用：{sorted(allowed)}）")
    return quote_ident(table)


def rows_for_user(conn: SqlConnection, table: str, user_id: str) -> list[Any]:
    """这个身份名下的全部行（逐列原样读出，不做任何投影 —— 删前备份用）。"""
    return list(
        conn.execute(
            f"SELECT * FROM {_require(table, READ_TABLES)} WHERE user_id = ?", (user_id,)
        ).fetchall()
    )


def thread_ids_for_user(conn: SqlConnection, user_id: str) -> list[str]:
    """这个身份名下的会话 id（检查点表按 thread_id 归，不认 user_id，所以要先换一道）。"""
    rows = conn.execute(
        "SELECT thread_id FROM session_thread WHERE user_id = ?", (user_id,)
    ).fetchall()
    return [str(row["thread_id"]) for row in rows]


def checkpoint_rows(conn: SqlConnection, table: str, thread_ids: list[str]) -> list[Any]:
    """某张检查点表里属于这些会话的行。空列表 = 一条都不读（不拼出 `IN ()`）。"""
    if not thread_ids:
        return []
    placeholders = ",".join("?" for _ in thread_ids)
    return list(
        conn.execute(
            f"SELECT * FROM {quote_ident(table)} WHERE thread_id IN ({placeholders})",
            thread_ids,
        ).fetchall()
    )


def delete_rows_for_user(conn: SqlConnection, table: str, user_id: str) -> int:
    """删掉这个身份在某一类表里的全部行，返回删了几行。**不 commit**（与导入共事务）。

    走的是 `ROW_TABLES` 那张**窄**名单：`session_thread` 读得到（备份要它）但删不掉 ——
    级联删必须带上检查点与审批，那条路在 `storage/threads.py`，这里整类删会留下孤儿。
    """
    cur = conn.execute(
        f"DELETE FROM {_require(table, ROW_TABLES)} WHERE user_id = ?", (user_id,)
    )
    return max(cur.rowcount, 0)
