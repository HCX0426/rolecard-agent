"""事件簿：把一个角色相关的、带时间的行并成一条**只读**时间轴（设计稿 §6）。

**不新建事件表**。轴上每一个点都从已有的三张表派生：`agent_reachout`（它主动说过什么）、
`role_memory_item`（它哪天记下 / 更正了哪条事实）、`session_thread`（你们什么时候开始聊、
最近一次聊到）。多存一份"事件"就是造出第二份事实 —— 而两处真相永远是先看起来没事、
后对不上账的那类 bug（本轮审计删掉的 backends 投影、`role_memory` blob 都属这一族）。

**这里不写文案**：每条事件带的是 `kind` + `verb`（只有会话锚点有 `start` / `recent`，
其余为 None），"记下：/ 更正：/ 开始聊："这些中文动词归界面。后端替界面造句，
下一步就是想本地化或改措辞时发现得改两处。

两个措辞上的硬约束，都是"别骗人"：

  * 记忆条目的 `created_at` 是**被记下的时间**，不是事情发生的时间，所以动词只能是
    "记下 / 更正"；写成"你搬来杭州"就是把它没证据的事说成了事实。
  * 只有**主动会话真的存在**才给跳转目标（与收件箱同一口径）：给一个不存在的 thread_id
    等于把用户送进一句"加载历史失败"。
"""

from __future__ import annotations

from typing import Any

from rolecard_agent.core.reachout import proactive_thread_id
from rolecard_agent.storage.db import SqlConnection

#: 每个源最多扫这么多行（倒序取新的）。轴是给人翻"最近的事"，不是全量归档；
#: 扫到上限就在响应里如实标 `truncated`，而不是假装这就是全部历史。
_SCAN_CAP = 400

#: 同一时刻的先后只是"稳定"，不是语义：主动开口排最前，会话锚点排最后。
KIND_ORDER: dict[str, int] = {
    "reachout": 0,
    "memory_correct": 1,
    "memory": 2,
    "thread": 3,
}
KINDS: tuple[str, ...] = tuple(KIND_ORDER)


def _cursor_of(event: dict[str, Any]) -> str:
    """一页的最后一件事 → 下一页的起点。三元组而不是 OFFSET：深分页会退化成全扫描。"""
    return f"{event['at']}|{event['kind']}|{event['ref_id']}"


def _key(event: dict[str, Any]) -> tuple[str, int, int]:
    """排序键，配合 `sort(reverse=True)` 得到 `at` 倒序、种类正序、id 倒序。

    `at` 用库里的文本（`YYYY-MM-DD HH:MM:SS` 定宽 ⇒ 字典序即时间序）。种类取**负**是因为
    一次 `reverse=True` 会把每个分量一起翻过去：不取负的话"同一秒"会变成种类倒序。
    """
    return (str(event["at"]), -KIND_ORDER[str(event["kind"])], int(event["ref_id"]))


def _reachouts(conn: SqlConnection, role_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, text, created_at FROM agent_reachout WHERE role_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (role_id, _SCAN_CAP),
    ).fetchall()
    thread = proactive_thread_id(role_id)
    exists = conn.execute(
        "SELECT 1 FROM session_thread WHERE thread_id = ?", (thread,)
    ).fetchone()
    return [
        {
            "kind": "reachout",
            "at": str(r["created_at"]),
            "text": str(r["text"]),
            "verb": None,
            "from_text": None,
            # 一整个角色的主动消息都落在同一条主动会话里，所以链接是会话级的、不是行级的。
            "thread_id": thread if exists else None,
            "ref_id": int(r["id"]),
        }
        for r in rows
    ]


def _memory(conn: SqlConnection, role_id: str) -> list[dict[str, Any]]:
    """记下 = `created_at`；更正 = 那条被取代时的 `invalidated_at`。

    "失效不删"此前只是库里一列，价值只在被回滚和被调试；这一条把它画成用户看得见的东西。
    """
    rows = conn.execute(
        "SELECT id, text, created_at, invalidated_at, superseded_by FROM role_memory_item "
        "WHERE role_id = ? ORDER BY id DESC LIMIT ?",
        (role_id, _SCAN_CAP),
    ).fetchall()
    by_id = {int(str(r["id"])): r for r in rows}
    events: list[dict[str, Any]] = [
        {
            "kind": "memory",
            "at": str(r["created_at"]),
            "text": str(r["text"]),
            "verb": None,
            "from_text": None,
            "thread_id": None,
            "ref_id": int(r["id"]),
        }
        for r in rows
    ]
    for r in rows:
        if not r["invalidated_at"]:
            continue
        old_id = int(str(r["id"]))
        newer = by_id.get(int(str(r["superseded_by"] or 0)))
        events.append(
            {
                "kind": "memory_correct",
                "at": str(r["invalidated_at"]),
                # 被整理合并掉时未必有一条新事实（INVALID 可以只作废不写替代），
                # 那种情况就显示"作废了什么"，而不是编一条"改成了"。
                "text": str(newer["text"]) if newer is not None else "",
                "verb": "correct",
                "from_text": str(r["text"]),
                "thread_id": None,
                "ref_id": old_id,
            }
        )
    return events


def _threads(conn: SqlConnection, role_id: str) -> list[dict[str, Any]]:
    """会话只给锚点，不逐条列消息：按角色扫 checkpoint 既贵，又是把对话全文复制进第二个视图。"""
    rows = conn.execute(
        "SELECT thread_id, title, created_at, updated_at FROM session_thread "
        "WHERE current_role_id = ? ORDER BY updated_at DESC LIMIT ?",
        (role_id, _SCAN_CAP),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        title = str(r["title"] or "新对话")
        created, updated = str(r["created_at"]), str(r["updated_at"])
        out.append(
            {
                "kind": "thread",
                "at": created,
                "text": title,
                "verb": "start",
                "from_text": None,
                "thread_id": str(r["thread_id"]),
                "ref_id": _thread_ref(created),
            }
        )
        # 同一分钟里"开始"和"最近"是同一条事件，别把它显示两遍。
        if updated[:16] != created[:16]:
            out.append(
                {
                    "kind": "thread",
                    "at": updated,
                    "text": title,
                    "verb": "recent",
                    "from_text": None,
                    "thread_id": str(r["thread_id"]),
                    "ref_id": _thread_ref(updated),
                }
            )
    return out


def _thread_ref(at: str) -> int:
    """会话锚点没有自己的自增 id，用它的**时间戳当排序身份**（同一秒内同一条会话只会有一条）。"""
    return int(at[:19].replace("-", "").replace(":", "").replace(" ", "_"), 36)


def build(
    conn: SqlConnection,
    *,
    role_id: str,
    limit: int = 50,
    before: str | None = None,
    kinds: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """这条轴的一页。`before` = 上一页最后一件事的游标；`kinds` 省略 = 全部四类。"""
    wanted = set(kinds or KINDS)
    events: list[dict[str, Any]] = []
    truncated = False
    sources = {
        "reachout": _reachouts,
        "memory": _memory,
        "thread": _threads,
    }
    for kind, fetch in sources.items():
        # `memory_correct` 与 `memory` 同源，一起取回再按 wanted 过滤行。
        if kind not in wanted and not (kind == "memory" and "memory_correct" in wanted):
            continue
        part = fetch(conn, role_id)
        truncated = truncated or len(part) >= _SCAN_CAP
        events.extend(part)
    picked = [e for e in events if e["kind"] in wanted]
    picked.sort(key=_key, reverse=True)
    if before:
        # 严格"排在游标之后"：同一条事件的三元组相等就跳过，比较用同一把 `_key`。
        marker = _split(before)
        if marker is not None:
            picked = [e for e in picked if _key(e) < marker]
    page = picked[: limit + 1]
    has_more = len(page) > limit
    items = page[:limit]
    return {
        "role_id": role_id,
        "items": items,
        "next_cursor": _cursor_of(items[-1]) if has_more and items else None,
        # 只说明"扫到了上限"，不承诺"总共就这些"：轴不数全量，数全量是归档功能的事。
        "truncated": truncated,
    }


def _split(before: str) -> tuple[str, int, int] | None:
    parts = before.split("|")
    if len(parts) != 3:
        return None
    try:
        return (parts[0], -KIND_ORDER[parts[1]], int(parts[2]))
    except (KeyError, ValueError):
        return None  # 坏游标当"没有"：宁可重头给一页，也不要 500


__all__ = ["KINDS", "KIND_ORDER", "build"]
