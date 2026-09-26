"""`scripts/reachout_outcomes.py` 的口径一致性。

为什么要给一个"打印脚本"写用例（09-26 实测换来的，两件事都是当天撞出来的）：

1. ④「接了话的那几条由头」和 ①「接话率」说的是同一批行，第一版各算各的 —— ① 报 1 条而
   ④ 数出 9 条，因为 ④ 只看了"有没有后续"，没跟着"接了 = 看过 **且** 有人回"走。
   纯 print 的脚本永远不会为这种自相矛盾变红，所以算法抽成 `summarize()`，第一条用例
   钉的就是**两处必须同数**。
2. "看过"原先只认 `read_at`（单条点开），而日常路径是"进对话界面批量刷"（只写 `seen_at`），
   于是**正在那条会话里跟她说话的人被数成"没看"**。第二条用例钉的是这个方向。
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "reachout_outcomes", ROOT / "scripts" / "reachout_outcomes.py"
)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

LANE = "s_proactive_active"
# 线程最后活动 05:00：早于它的开口算"有人回过"，晚于它的不算。
T_TOUCHED = "2026-09-26 05:00:00.000"


def _seed(
    conn: Any,
    *,
    text: str,
    state: str,
    created_at: str,
    read_at: str | None = None,
    seen_at: str | None = None,
    fired_by: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, state, read_at, seen_at,"
        " fired_by, created_at) VALUES ('active', '主动角色', ?, ?, ?, ?, ?, ?)",
        (text, state, read_at, seen_at, fired_by, created_at),
    )
    conn.commit()


def _lane(conn: Any) -> None:
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, updated_at)"
        " VALUES (?, 'u1', 'active', ?)",
        (LANE, T_TOUCHED),
    )
    conn.commit()


def test_the_two_views_of_the_same_rows_agree(conn: Any) -> None:
    _lane(conn)
    _seed(conn, text="单独点开且有人回", state="read", created_at="2026-09-26 04:00:00",
          read_at="2026-09-26 04:05:00", fired_by="open_thread")
    _seed(conn, text="谁都没看过也没人回", state="unread", created_at="2026-09-26 06:00:00",
          fired_by=None)
    _seed(conn, text="批量刷过且有人回", state="read", created_at="2026-09-26 04:20:00",
          seen_at="2026-09-26 04:30:00", fired_by="timer")
    _seed(conn, text="看过没接", state="read", created_at="2026-09-26 06:10:00",
          seen_at="2026-09-26 06:20:00", fired_by="recall")
    # 老数据形状：`state='read'` 但两个时刻列都没有（那两列上线之前批量刷过的行）
    _seed(conn, text="老数据看不出怎么看的", state="read", created_at="2026-09-26 04:10:00",
          fired_by="time_pattern")

    s = mod.summarize(conn)
    assert (s["picked"], s["unseen"], s["ignored"], s["dismissed"]) == (3, 1, 1, 0)
    # 这一条就是原来会红的那处：④ 的总数必须等于 ① 的接话数。
    assert sum(s["picked_sources"].values()) == s["picked"]
    assert s["picked_sources"] == {"open_thread": 1, "timer": 1, "time_pattern": 1}
    # 没落 `fired_by` 的老行单独一档，不摊进任何真实源。
    assert s["by_source"][mod.UNKNOWN_SOURCE] == 1
    # "看过"看的是 `state`，两个时刻列只分**怎么看的**：点开 1 · 批量 2 · 老数据 1。
    assert (s["opened"], s["batch_seen"], s["legacy_seen"]) == (1, 2, 1)


def test_batch_seen_counts_as_seen_and_not_as_never_read(conn: Any) -> None:
    """只有 `seen_at` 的那一条不能算"没看"；而"没看"的判据是 `state='unread'`。

    09-26 那组"没看 10/11"就是这么偏出来的：判据写成"有没有 `read_at`"，于是**正在那条
    会话里跟她说话的人**被数成没看过 —— 可他明明就在屏幕前。
    """
    _lane(conn)
    _seed(conn, text="只被批量刷过", state="read", created_at="2026-09-26 04:00:00",
          seen_at="2026-09-26 04:30:00")
    _seed(conn, text="状态还是未读", state="unread", created_at="2026-09-26 06:00:00")
    s = mod.summarize(conn)
    assert (s["picked"], s["unseen"], s["opened"], s["batch_seen"]) == (1, 1, 0, 1)
