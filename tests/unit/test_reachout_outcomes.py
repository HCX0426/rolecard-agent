"""`scripts/reachout_outcomes.py` 的口径一致性。

为什么要给一个"打印脚本"写用例（09-26 一晚撞出来三件事）：

1. ④「接了话的那几条由头」和 ①「接话率」说的是同一批行，第一版各算各的 —— ① 报 1 条而
   ④ 数出 9 条。纯 print 的脚本永远不会为这种自相矛盾变红，所以算法抽成 `summarize()`，
   第一条用例钉的就是**两处必须同数**。
2. "看过"原先判的是"有没有 `read_at`"（只有单条点开才写），于是**正在那条会话里跟她说话的人
   被数成"没看"**。判据换成 `state` 之后同一批行从 1/11 翻到 9/11。
3. "有人回"原先判的是"那条线的 `updated_at` 晚于她这句"，可 `updated_at` 连重命名都会推。
   现在真判据读检查点，近似那份只留着做对照 —— 最后一条用例钉的就是**两个数可以不相等**，
   谁把它们当成同一个，就又会回到"一个定义改动让分子翻 9 倍"那种状态。
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

# 线程 id 带身份（B2）：脚本按这份库实际的主人（默认 = local-user）构造主动线程 id。
LANE = f"s_proactive_{mod.OWNER}_active"
# 线程最后活动 05:00：早于它的开口算"近似有人回"，晚于它的不算。
T_TOUCHED = "2026-09-26 05:00:00.000"
# 检查点里读出来的那条线（真判据看的就是这份）
LANE_MSGS: dict[str, list[tuple[str, str]]] = {
    LANE: [
        ("你", "单独点开且有人回"), ("用户", "在的"),
        ("你", "批量刷过且有人回"), ("用户", "对的"),
        ("你", "老数据看不出怎么看的"), ("用户", "嗯"),
        ("你", "看过没接"),
    ],
}


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
    _seed(conn, text="谁都没看过也没人回", state="unread", created_at="2026-09-26 06:00:00")
    _seed(conn, text="批量刷过且有人回", state="read", created_at="2026-09-26 04:20:00",
          seen_at="2026-09-26 04:30:00", fired_by="timer")
    _seed(conn, text="看过没接", state="read", created_at="2026-09-26 06:10:00",
          seen_at="2026-09-26 06:20:00", fired_by="recall")
    # 老数据形状：`state='read'` 而两个时刻列都没有
    _seed(conn, text="老数据看不出怎么看的", state="read", created_at="2026-09-26 04:10:00",
          fired_by="time_pattern")

    s = mod.summarize(conn, LANE_MSGS)
    assert (s["picked"], s["unseen"], s["ignored"], s["dismissed"]) == (3, 1, 1, 0)
    # ④ 的总数必须等于 ① 的接话数
    assert sum(s["picked_sources"].values()) == s["picked"]
    assert s["picked_sources"] == {"open_thread": 1, "timer": 1, "time_pattern": 1}
    # ④′ 交叉表的三个不变量（09-28 拍的口径）：
    #   ① 每格数字加起来 = 由头的总数；
    #   ② 每格的 picked = ④ 的 picked_sources 同源（一处判据，不允许两个数打架）；
    #   ③ 各格的 column 总数打到 ① 的四个总数上（行与列都是同一批行的两种切法）。
    for key, cell in s["cross"].items():
        assert cell["total"] == sum(
            cell[k] for k in ("picked", "ignored", "unseen", "dismissed")
        ), f"由头 {key} 的格子加起来不等于它的总数"
        expected_picked = s["picked_sources"].get(key, 0)
        assert cell["picked"] == expected_picked, f"由头 {key} 的 picked 与④不同源"
    assert {k: v for k, v in s["cross"].items() if k != mod.UNKNOWN_SOURCE} == {
        "open_thread": {"total": 1, "picked": 1, "ignored": 0, "unseen": 0, "dismissed": 0},
        "timer": {"total": 1, "picked": 1, "ignored": 0, "unseen": 0, "dismissed": 0},
        "recall": {"total": 1, "picked": 0, "ignored": 1, "unseen": 0, "dismissed": 0},
        "time_pattern": {"total": 1, "picked": 1, "ignored": 0, "unseen": 0, "dismissed": 0},
    }
    assert s["cross"][mod.UNKNOWN_SOURCE]["unseen"] == 1
    # 没落 `fired_by` 的行单独一档，不摊进任何真实源
    assert s["by_source"][mod.UNKNOWN_SOURCE] == 1
    # "看过"看 `state`；两个时刻列只分**怎么看的**：点开 1 · 批量 2 · 老数据 1
    assert (s["opened"], s["batch_seen"], s["legacy_seen"]) == (1, 2, 1)


def test_batch_seen_counts_as_seen_and_not_as_never_read(conn: Any) -> None:
    """09-26 那组"没看 10/11"的成因：判据写成"有没有 `read_at`"。"""
    _lane(conn)
    _seed(conn, text="单独点开且有人回", state="read", created_at="2026-09-26 04:00:00",
          seen_at="2026-09-26 04:30:00")
    _seed(conn, text="状态还是未读", state="unread", created_at="2026-09-26 06:00:00")
    s = mod.summarize(conn, LANE_MSGS)
    assert (s["picked"], s["unseen"], s["opened"], s["batch_seen"]) == (1, 1, 0, 1)


def test_the_updated_at_proxy_is_not_the_reply(conn: Any) -> None:
    """近似与真判据**可以不相等**：线被推过 ≠ 他回了她那句。

    这里那条线 05:00 动过（所以近似算它"接了"），但检查点里她那句之后没有任何用户消息，
    真判据算"看了没接"。把两个数并排印出来，就是为了这种行不再被一个口径冒充另一个。
    """
    _lane(conn)
    _seed(conn, text="看过没接", state="read", created_at="2026-09-26 04:00:00",
          seen_at="2026-09-26 04:30:00", fired_by="timer")
    s = mod.summarize(conn, {LANE: [("你", "看过没接")]})
    assert (s["picked"], s["ignored"]) == (0, 1)
    assert s["picked_proxy"] == 1, "近似那条仍会把这行算成「接了」—— 所以才要并排印出来"


def test_repeat_distribution_counts_only_scored_rows_and_measures_spread(conn: Any) -> None:
    """⑥ 复读分分布（N4 ①）的读侧：NULL 不算样本，中位数/高分占比/谁在复读自己。

    这一条是"落库值得"的最短断言：分数进了 `agent_reachout.repeat_score`，跨启动还在；
    读不到它，⑥ 就又是只有当次日志、一重启就没的尺子（`reachout_sent` 的老毛病）。
    """
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, repeat_score) VALUES "
        "('a', 'A', 'x1', 0.1), "
        "('a', 'A', 'x2', 0.2), "
        "('b', 'B', 'y1', 0.6), "
        "('b', 'B', 'y2', 0.9), "
        "('c', 'C', 'z1', NULL)"
    )
    conn.commit()
    d = mod.repeat_distribution(conn)
    assert d["n"] == 4, "NULL 那一行被当成了样本"
    assert abs(float(d["median"]) - 0.4) < 1e-9, d["median"]  # 0.1,0.2,0.6,0.9 → (0.2+0.6)/2
    assert int(d["ge_half"]) == 2
    by_role = {str(x["role"]): (int(x["n"]), float(x["max"])) for x in d["by_role"]}
    assert by_role == {"a": (2, 0.2), "b": (2, 0.9)}
    # 最差档排最前：b 的 0.9 是"谁在复读自己"的头号嫌疑人
    assert str(d["by_role"][0]["role"]) == "b"


def test_repeat_distribution_empty_db_reports_no_samples(conn: Any) -> None:
    d = mod.repeat_distribution(conn)
    assert d["n"] == 0 and d["median"] is None and d["ge_half"] is None and d["by_role"] == []


def test_item_five_counts_only_rows_that_know_their_source(conn: Any) -> None:
    """⑤「攒样本进度」只数**带由头**的行，并把观测窗口的两端报出来。

    两个判据都要钉：
    * 老行 `fired_by IS NULL` 不算进样本 —— 那是"不知道"，摊进去就等于替它们猜一个源；
    * 窗口取自**带由头的那几行**的首尾，不是全表首尾 —— 否则上线前那批会把观测时长撑大，
      "每天几条"被系统性低估，回访日期就被推远。
    """
    _lane(conn)
    _seed(conn, text="单独点开且有人回", state="read", created_at="2026-09-20 01:00:00",
          read_at="2026-09-20 02:00:00")                      # 老行：没有由头
    _seed(conn, text="批量刷过且有人回", state="read", created_at="2026-09-26 03:00:00",
          seen_at="2026-09-26 03:30:00", fired_by="recall")   # 窗口左端
    _seed(conn, text="看过没接", state="read", created_at="2026-09-26 09:00:00",
          seen_at="2026-09-26 09:10:00", fired_by="timer")    # 窗口右端
    s = mod.summarize(conn, LANE_MSGS)
    assert s["known_source"] == 2, "NULL 那一行不许算进样本"
    assert s["first_known_at"] == "2026-09-26 03:00:00"
    assert s["last_known_at"] == "2026-09-26 09:00:00"
    assert s["by_source"][mod.UNKNOWN_SOURCE] == 1, "不知道的那一档要单独留着，别摊平"
