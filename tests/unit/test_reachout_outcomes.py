"""`scripts/reachout_outcomes.py` 的口径一致性。

为什么要给一个"打印脚本"写用例（09-26 实测换来的）：④「接了话的那几条由头」和 ①「接话率」
说的是同一批行，第一版各算各的 —— ① 报 1 条而 ④ 数出 9 条，因为 ④ 只看了"有没有后续"，
没跟着"接了 = 读过 **且** 有人回"这条门槛走。这种自相矛盾在纯 print 的脚本里永远不会红，
所以把算法抽成 `summarize()` 之后，第一条要钉的就是**两处必须同数**。
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


def _seed(conn: Any, *, text: str, state: str, created_at: str, read_at: str | None,
          fired_by: str | None) -> None:
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, state, read_at, fired_by,"
        " created_at) VALUES ('active', '主动角色', ?, ?, ?, ?, ?)",
        (text, state, read_at, fired_by, created_at),
    )
    conn.commit()


def test_the_two_views_of_the_same_rows_agree(conn: Any) -> None:
    # 线程活动时刻 05:00：早于它的开口算"有人回过"，晚于它的不算。
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, updated_at)"
        " VALUES (?, 'u1', 'active', '2026-09-26 05:00:00.000')",
        (LANE,),
    )
    _seed(conn, text="接住的那条", state="read", created_at="2026-09-26 04:00:00",
          read_at="2026-09-26 04:05:00", fired_by="open_thread")
    _seed(conn, text="回了但没读到", state="unread", created_at="2026-09-26 04:30:00",
          read_at=None, fired_by=None)
    _seed(conn, text="读过但没人回", state="read", created_at="2026-09-26 06:00:00",
          read_at="2026-09-26 06:01:00", fired_by="timer")

    s = mod.summarize(conn)
    assert (s["picked"], s["unseen"], s["ignored"]) == (1, 1, 1)
    # 这一条就是原来会红的那处：④ 的总数必须等于 ① 的接话数。
    assert sum(s["picked_sources"].values()) == s["picked"]
    assert s["picked_sources"] == {"open_thread": 1}, "接住的那条归到它自己的由头上"
    # 没落 `fired_by` 的老行单独一档，不摊进任何真实源。
    assert s["by_source"][mod.UNKNOWN_SOURCE] == 1
    assert s["by_source"].get("timer") == 1
