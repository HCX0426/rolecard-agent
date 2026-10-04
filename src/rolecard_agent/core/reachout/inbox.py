"""信箱投递：收件箱读写与「主动会话」的落点（`R102-59` 从 `core/reachout.py` 拆出的四模块之一）。

"她说出口的那句话去了哪里"在本模块答完：落一条 `agent_reachout`（unread）→ 标已读 / 划掉 /
清理，按人（多租户）按角色列收件箱；"能继续谈"的那一侧是每条确定性的主动会话（id 的算法
在 `core/proactive_thread.py` —— 那条约定跨模块共用，不属本功能私有），投递成功与否以
`delivered_at` 记，欠投的按时间窗补投。

抑制判定不在这里（`quiet.py`），触发评估与生成不在这里（`triggers.py`），
编排不在这里（`scheduler.py`）。纯搬层，行为与拆分前逐字一致。
"""

from __future__ import annotations

from typing import Any

from rolecard_agent.core.proactive_thread import (
    PROACTIVE_THREAD_PREFIX,
    proactive_thread_id,
)
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.storage.db import SqlConnection, quote_ident, table_columns

# 「只进了收件箱、没落进会话」的那几条，多久之内还值得补投（R26-40 ②）。过了这个窗口就不管了
# —— 她半小时前说的话现在才冒进会话，读起来像穿越。这个窗同时挡住"这一列上线时那批老行
# （全为 NULL）被当成待办"。
UNDELIVERED_RETRY_MINUTES = 30
# 一次 tick 最多补几条：补投要拿会话写锁，积压太多会把这一 tick 拖住（别的角色还等着开口）。
UNDELIVERED_RETRY_LIMIT = 5


# --------------------------------------------------------------------------- 数据


def list_reachouts(
    conn: SqlConnection,
    limit: int = 100,
    *,
    user_id: str,
    role_id: str | None = None,
    file_watch_pending: int = 0,
) -> dict[str, object]:
    """收件箱：未读 + 最近历史（含未读数，供铃铛红点）。

    `role_id` 给定时只返回该角色主动找过你的历史（架构总览 §5：按角色卡隔离查看）。
    `file_watch_pending` = 当前挂起的目录变更条数（0 = 无事件或功能关闭）。
    """
    # `dismissed` 不进列表（用户已经划掉了），但**行留着** —— 那才是"她冒话而没人接"的证据。
    where = (
        "WHERE state != 'dismissed' AND user_id = ?"
        + (" AND role_id = ?" if role_id else "")
    )
    params = (user_id, role_id, limit) if role_id else (user_id, limit)
    rows = conn.execute(
        f"SELECT id, role_id, role_name, text, state, created_at, read_at, seen_at, dismissed_at "
        f"FROM agent_reachout {where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    # 只有**主动会话真的存在**才给跳转目标：这个功能上线之前落库的老消息没有对应的线程，
    # 给了 id 等于把用户送进一句"加载历史失败"。宁可只给"标记已读"。
    # 线程集合按**这个人**过滤（B2）：线程 id 带身份了，"别人的主动会话"不该成为我的跳转对象。
    opened = {
        str(r["thread_id"])
        for r in conn.execute(
            "SELECT thread_id FROM session_thread WHERE user_id = ? AND thread_id LIKE ?",
            (user_id, f"{PROACTIVE_THREAD_PREFIX}%"),
        )
    }
    if role_id:
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout"
            " WHERE role_id = ? AND user_id = ? AND state = 'unread'",
            (role_id, user_id),
        ).fetchone()
    else:
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout WHERE user_id = ? AND state = 'unread'",
            (user_id,),
        ).fetchone()
    # "某个角色有几条没读"只在这里算一次（审计 §12.11 的"三份各算"那一格）：铃铛、桌宠、
    # 抽屉以前各自 filter 一遍，其中桌宠那份连后端给的 `unread` 都不看。
    # 边界：抽屉里**同一摞**（同角色同一天）的角标仍然在前端数 —— 那是显示分组的一部分，
    # 分组只做在前端（设计稿 §1），后端只负责把 `merge_days` 随列表带回。
    by_role = conn.execute(
        "SELECT role_id, COUNT(*) AS n FROM agent_reachout "
        "WHERE state = 'unread' AND user_id = ? GROUP BY role_id",
        (user_id,),
    ).fetchall()
    return {
        "items": [
            dict(r)
            | {
                # 点进去能翻历史、能回话的那个会话；尚未建立（老消息 / 从没投递成功）→ None。
                "thread_id": _opened_thread(str(r["role_id"]), opened, user_id=user_id)
            }
            for r in rows
        ],
        "unread": int(unread["n"]),
        "unread_by_role": {str(r["role_id"]): int(r["n"]) for r in by_role},
        "file_watch_pending": file_watch_pending,
    }


def _opened_thread(role_id: str, opened: set[str], *, user_id: str) -> str | None:
    tid = proactive_thread_id(role_id, user_id=user_id)
    return tid if tid in opened else None


def mark_read(conn: SqlConnection, reachout_id: int, *, user_id: str) -> bool:
    """把一条置为已读，返回"这条现在处于已读状态"。记录不存在才返回 False。

    **已读再标一次是幂等成功，不是"不存在"** —— 这两件事以前共用一个 False，于是点一条
    历史（已读）消息会弹"主动消息不存在：9"，而那条就在你眼前。收件箱列的是未读+最近历史，
    点已读的那几条是正常路径，不该报错。
    """
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'read', read_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND user_id = ? AND state = 'unread'",
        (reachout_id, user_id),
    )
    if cur.rowcount > 0:
        conn.commit()
        return True
    conn.commit()
    return (
        conn.execute(
            "SELECT 1 FROM agent_reachout WHERE id = ? AND user_id = ?",
            (reachout_id, user_id),
        ).fetchone()
        is not None
    )


def mark_role_read(conn: SqlConnection, role_id: str, *, user_id: str) -> int:
    """把某角色攒下的未读一次标完，返回条数。

    为什么单独要它：主动消息现在落在"该角色的主动会话"里（见 `proactive_thread_id`），
    用户点进会话看 = 已经读过了，此刻应把收件箱那一摞一起清掉；让前端逐条发请求
    既啰嗦又会在中途失败留下半已读状态。
    """
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'read', "
        "seen_at = COALESCE(seen_at, CURRENT_TIMESTAMP) "
        "WHERE role_id = ? AND user_id = ? AND state = 'unread'",
        (role_id, user_id),
    )
    conn.commit()
    return int(cur.rowcount)


def mark_all_read(conn: SqlConnection, *, user_id: str) -> int:
    """把所有未读一次标完，返回条数。

    口径是用户 2026-09-23 拍的：**"我点进对话界面了"就等于都看过了** —— 所以打开任何一个
    角色的主动会话，别的角色攒的未读也一并算读过。代价是"另一个角色找过我"这件事会被
    这一动作抹平（抽屉里那些行还在，只是不再标未读、不再顶红点）。
    与 `mark_role_read` 的分工：那条管"这一摞我看过了"，这条管"我进入阅读状态了"。

    两个都不写 `read_at`（那是"一条条点开过"的证据，批量动作不该留下它，见 `S-2`），
    但**要写 `seen_at`**：不写的话"看过"这件事在库里就没痕迹了，结局度量只能把
    "进过对话界面"的人全数成"没看"—— 09-26 拿这套数做回访时正是这样偏的。
    """
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'read', "
        "seen_at = COALESCE(seen_at, CURRENT_TIMESTAMP) WHERE state = 'unread'"
        " AND user_id = ?",
        (user_id,),
    )
    conn.commit()
    return int(cur.rowcount)


def record_reachout(
    conn: SqlConnection,
    role: RoleCard,
    text: str,
    *,
    user_id: str,
    fired_by: str | None = None,
    repeat_score: float | None = None,
) -> int:
    """落一条主动开口（unread），返回它的 id。role 冗余存角色名：角色被删后收件箱仍可读。

    `fired_by` 是这一条的**由头**（`tick_once` 那条链上命中的第一个源）。不带它 = 这条
    不知道由头（离线单测、以及这一列上线之前的老行都是 NULL）—— 读侧不许把 NULL 当成
    任何一个具体源，理由见 `schema.sql` 那一列的注释。

    `repeat_score` 是这句与最近说过的话的重合分（`core/anti_repeat.repeat_score`，
    N4 ① 开始落库）：NULL = 这条没量过（列上线前 / 调用方没给）。落的是**最后出口那版**
    的分数——重生重写过的取最优，被 DROP 挡下的那句根本没有行可落。
    落完顺手按角色卡的 `reachout_keep` 修剪（0 = 不自动删）：抽屉"只增不减"是用户报的
    第二件事，而这条挂在写入点上就够了 —— 不需要为此再跑一个定时任务。
    """
    cur = conn.execute(
        "INSERT INTO agent_reachout (role_id, user_id, role_name, text, fired_by, repeat_score)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (role.role_id, user_id, role.role_name, text, fired_by, repeat_score),
    )
    conn.commit()
    prune_inbox(
        conn, role.role_id, int(getattr(role, "reachout_keep", 0) or 0), user_id=user_id
    )
    # 返回 id：调用方要把"投进会话了没有"写回同一行（`mark_delivered`）。
    return int(cur.lastrowid or 0)


def restore_row(conn: SqlConnection, *, user_id: str, payload: dict[str, Any]) -> str:
    """同步下行导入：投递记录的**只追加**落库，列集现算。返回 created / skipped。

    这就是 `core/sync` 从前直写 `agent_reachout` 的那段（`_write_reachout`）搬回主人家：
    写入口归 owner，同步链只拿"插了 / 已存在"这个结果。语义逐条对着原件搬：

      * 同 (身份, 时刻, 文本) 已存在 → `skipped`（只追加，永不覆盖 —— 身份含文本，
        改一个字就是另一条）；
      * `state` 缺省 `unread`、`created_at` 空串落 NULL —— 与搬迁前逐字一致。

    **列集现算（`storage.db.table_columns`）而不是抄一张清单**：抄清单就是第二份事实面，
    表加了列而清单没跟上，这条写路径会静默少那一列（2026-10-04 快照的 sync 写入口条目）。
    取列规则与 `memory.restore_row` 同一句：只写"我们有值的列"（`value_map` 的语义列 +
    payload 带来的键），其余列（`id` 自增、各 `*_at` 的表默认）整列不出现，schema 说了算。

    **不 commit**：整份替换的清空与导入共一个事务，收口在调用方
    （`core/sync_service.run_import`；单独跑 `apply_import` 时由它默认的 commit 收口）。
    """
    exists = conn.execute(
        "SELECT 1 FROM agent_reachout WHERE user_id = ? AND role_id = ? AND text = ?"
        " AND created_at = ? LIMIT 1",
        (
            user_id,
            str(payload.get("role_id") or ""),
            str(payload.get("text") or ""),
            str(payload.get("created_at") or ""),
        ),
    ).fetchone()
    if exists is not None:
        return "skipped"
    value_map: dict[str, Any] = {
        "user_id": user_id,
        "role_id": str(payload.get("role_id") or ""),
        "role_name": str(payload.get("role_name") or ""),
        "text": str(payload.get("text") or ""),
        "fired_by": payload.get("fired_by"),
        "state": str(payload.get("state") or "unread"),
        "created_at": str(payload.get("created_at") or "") or None,
    }
    cols = [
        col
        for col in table_columns(conn, "agent_reachout")
        if col in value_map or col in payload
    ]
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO agent_reachout ({', '.join(quote_ident(c) for c in cols)})"
        f" VALUES ({placeholders})",
        tuple(value_map.get(col, payload.get(col)) for col in cols),
    )
    return "created"


def mark_delivered(conn: SqlConnection, reachout_id: int) -> None:
    """记下"这一句真的落进她的主动会话了"（`R26-40` ②）。

    只在 `deliver` 真返回了线程 id 时调 —— 把"收件箱有"与"会话里有"这两件事分开记，
    是这一列存在的全部意义。
    """
    conn.execute(
        "UPDATE agent_reachout SET delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
        (reachout_id,),
    )
    conn.commit()


def undelivered_reachouts(
    conn: SqlConnection, *, user_id: str, within_minutes: int, limit: int
) -> list[dict[str, Any]]:
    """**只进了收件箱、还没落进会话**的近期开口（按时间正序），给调度器补投用。

    三条限定各有理由：
      * `delivered_at IS NULL` —— 欠的就是这些；
      * **时间窗**（`within_minutes`）—— 这句早就过去了就不该再补（她三小时前说的话现在
        才冒进会话，读起来像穿越）；它同时挡住"这一列上线时那批老行（全 NULL）被当成待办"；
      * `state != 'dismissed'` —— 用户亲手划掉的那条不用补，那是"他不想看"。
    """
    rows = conn.execute(
        "SELECT id, role_id, text FROM agent_reachout "
        "WHERE user_id = ? AND delivered_at IS NULL AND state != 'dismissed' "
        "AND created_at >= datetime('now', ?) "
        "ORDER BY id ASC LIMIT ?",
        (user_id, f"-{int(within_minutes)} minutes", int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def prune_inbox(conn: SqlConnection, role_id: str, keep: int, *, user_id: str) -> int:
    """该角色的收件箱只留最近 `keep` 条，返回删掉的条数。`keep <= 0` = 什么都不做。

    **删的是投递记录，不是她说出口的那句话**：那句话在主动会话的 checkpoint 里，留着它
    她才记得自己主动找过你（§"主动开口落进会话"那条设计）。清掉记录只是清掉红点与列表。
    """
    if keep <= 0:
        return 0
    before = int(
        str(conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout "
            "WHERE role_id = ? AND user_id = ? AND state != 'dismissed'",
            (role_id, user_id),
        ).fetchone()["n"])
    )
    conn.execute(
        "UPDATE agent_reachout SET state = 'dismissed', dismissed_at = CURRENT_TIMESTAMP "
        "WHERE role_id = ? AND user_id = ? AND state != 'dismissed' AND id NOT IN ("
        "  SELECT id FROM agent_reachout WHERE role_id = ? AND user_id = ?"
        "  ORDER BY id DESC LIMIT ?)",
        (role_id, user_id, role_id, user_id, keep),
    )
    conn.commit()
    return max(0, before - keep)


def delete_reachout(conn: SqlConnection, reachout_id: int, *, user_id: str) -> bool:
    """把抽屉里的某一行划掉（**不碰会话里的那条消息**）。False = 没有这条。

    软删而不是 DELETE（09-26 轮 R26-14 / S-2）：从前这一行是**真删掉且不进 audit_log**，
    于是"她主动说了话而用户把它划了"这一种结局在库里查无痕迹 —— 三种口径里最重要的
    "看了不接 / 不想接"永久算不出来。留着行只多两个时刻列。
    """
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'dismissed', dismissed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND user_id = ? AND state != 'dismissed'",
        (reachout_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def clear_inbox(conn: SqlConnection, role_id: str, *, user_id: str) -> int:
    """清空该角色的主动消息记录（同样不碰会话）。返回删掉的条数。"""
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'dismissed', dismissed_at = CURRENT_TIMESTAMP "
        "WHERE role_id = ? AND user_id = ? AND state != 'dismissed'",
        (role_id, user_id),
    )
    conn.commit()
    return cur.rowcount


def clear_all_inboxes(conn: SqlConnection, *, user_id: str) -> int:
    """清空所有角色的主动消息记录（不给 role_id 时的那条路）。"""
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'dismissed', dismissed_at = CURRENT_TIMESTAMP "
        "WHERE state != 'dismissed' AND user_id = ?",
        (user_id,),
    )
    conn.commit()
    return cur.rowcount