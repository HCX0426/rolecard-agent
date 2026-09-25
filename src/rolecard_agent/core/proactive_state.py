"""关系驱动主动开口的 per-role 状态（架构总览 §5）。

所有状态按 role_id 隔离：关系数值（affinity）、最近交互时间、主动度校准。
不依赖任何域概念；core 层内不出现具体域专名（受一致性脚本约束）。
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rolecard_agent.storage.db import SqlConnection

# 关系数值到"想聊"阈值的默认；达到即性格·关系数值触发。
DEFAULT_AFFINITY_THRESHOLD = 1.0
# 每轮主动开口成功后关系数值的增量（互动积累的成长值，封顶 5.0）。
AFFINITY_GAIN_PER_OPEN = 0.2
AFFINITY_MAX = 5.0
# 自然衰减：每过去一天 affinity 衰减的比例（让久不互动的角色慢慢"冷下来"）。
AFFINITY_DECAY_PER_DAY = 0.05


@dataclass
class ProactiveState:
    role_id: str
    affinity: float = 0.0
    last_interaction_utc: datetime | None = None
    calibration: dict[str, Any] = field(default_factory=dict)
    #: 第五个由头「未收尾话题」的缓存。`open_threads_scan_at is None` = **从没扫过**，
    #: 与"扫了但一条都没有"（元组为空、时刻非空）是两回事 —— 后者在过期之前不该再花调用。
    open_threads: tuple[str, ...] = ()
    open_threads_scan_at: datetime | None = None
    #: 上一次**以回忆为由**主动开口的时刻（R26-23 的冷却锚点）。None = 从没以这一档开过。
    recall_at: datetime | None = None

    def decayed_affinity(self, *, now: datetime) -> float:
        """叠加时间衰减后的关系数值（久不互动则回落）。"""
        if self.last_interaction_utc is None:
            return self.affinity
        days = max(0.0, (now - self.last_interaction_utc).total_seconds() / 86400.0)
        return max(0.0, self.affinity - AFFINITY_DECAY_PER_DAY * days)


def _parse_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _fmt_ts(value: datetime) -> str:
    """落库一律先转 UTC 再剥掉 tzinfo。

    为什么必须在写这一侧统一：读回来的 `_parse_ts` 是无条件按 UTC 解释的，所以调用方
    传进来一个本地时刻（`datetime.now()` 的常见形状）就会让这一列**静默偏移一个时区**
    —— 实测过：`recall_at` 因此跑到 8 小时之后，冷却把回忆档多关了一整天还不报错。
    """
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_state(row: dict[str, Any]) -> ProactiveState:
    calibration: dict[str, Any] = {}
    if row["calibration_json"]:
        with contextlib.suppress(Exception):
            calibration = json.loads(row["calibration_json"])
    # 缓存的话题：认不出的一律当"没扫过"（None / 坏 JSON / 不是列表）—— 这一源宁可漏报，
    # 而"扫过但没结果"由 open_threads_at 那一列单独记着，两者不靠一个空值混在一起。
    topics: tuple[str, ...] = ()
    if row["open_threads"]:
        with contextlib.suppress(Exception):
            loaded = json.loads(row["open_threads"])
            if isinstance(loaded, list):
                topics = tuple(str(t).strip() for t in loaded if str(t).strip())
    return ProactiveState(
        role_id=row["role_id"],
        affinity=float(row["affinity"] or 0.0),
        last_interaction_utc=_parse_ts(
            row["last_interaction_utc"]
        ),
        calibration=calibration,
        open_threads=topics,
        open_threads_scan_at=_parse_ts(row["open_threads_at"]),
        recall_at=_parse_ts(row["recall_at"]),
    )


def get_state(conn: SqlConnection, role_id: str) -> ProactiveState:
    """读某角色的主动状态；无记录 = 全新状态（affinity 0）。"""
    row = conn.execute(
        "SELECT role_id, affinity, last_interaction_utc, calibration_json, "
        "open_threads, open_threads_at, recall_at "
        "FROM role_proactive_state WHERE role_id = ?",
        (role_id,),
    ).fetchone()
    if row is None:
        return ProactiveState(role_id=role_id)
    return _row_to_state(row)


def save_state(conn: SqlConnection, state: ProactiveState) -> None:
    conn.execute(
        "INSERT INTO role_proactive_state "
        "(role_id, affinity, last_interaction_utc, calibration_json, "
        " open_threads, open_threads_at, recall_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(role_id) DO UPDATE SET "
        "affinity = excluded.affinity, last_interaction_utc = excluded.last_interaction_utc, "
        "calibration_json = excluded.calibration_json, "
        "open_threads = excluded.open_threads, open_threads_at = excluded.open_threads_at, "
        "recall_at = excluded.recall_at, "
        "updated_at = CURRENT_TIMESTAMP",
        (
            state.role_id,
            state.affinity,
            _fmt_ts(state.last_interaction_utc) if state.last_interaction_utc else None,
            json.dumps(state.calibration, ensure_ascii=False),
            json.dumps(list(state.open_threads), ensure_ascii=False),
            _fmt_ts(state.open_threads_scan_at) if state.open_threads_scan_at else None,
            _fmt_ts(state.recall_at) if state.recall_at else None,
        ),
    )
    conn.commit()


def save_open_threads(
    conn: SqlConnection, role_id: str, topics: list[str], *, now: datetime
) -> ProactiveState:
    """只写「未收尾话题」那两列（其余状态原样留着）。扫到什么写什么，**空也要写**——
    写了"扫过、没有"才不会下一个 tick 又去问一遍。"""
    state = get_state(conn, role_id)
    state.open_threads = tuple(t for t in topics if t)
    state.open_threads_scan_at = now
    save_state(conn, state)
    return state


def record_recall_open(conn: SqlConnection, role_id: str, *, now: datetime) -> ProactiveState:
    """记一笔"这次开口是以回忆为由"——`trigger_recall` 的冷却锚点（R26-23）。

    只写 `recall_at` 这一列，别的状态原样留着（与 `save_open_threads` 同一个道理：
    一处职责一个写点，别让"记时刻"顺手把 affinity 也算一遍）。
    """
    state = get_state(conn, role_id)
    state.recall_at = now
    save_state(conn, state)
    return state


def record_interaction(conn: SqlConnection, role_id: str, *, now: datetime) -> ProactiveState:
    """主动开口成功后调用：关系数值 +增量、刷新交互时间、写回。返回最新状态。"""
    state = get_state(conn, role_id)
    state.affinity = min(AFFINITY_MAX, state.affinity + AFFINITY_GAIN_PER_OPEN)
    state.last_interaction_utc = now
    save_state(conn, state)
    return state


__all__ = [
    "AFFINITY_GAIN_PER_OPEN",
    "AFFINITY_MAX",
    "DEFAULT_AFFINITY_THRESHOLD",
    "ProactiveState",
    "get_state",
    "record_interaction",
    "record_recall_open",
    "save_open_threads",
    "save_state",
]
