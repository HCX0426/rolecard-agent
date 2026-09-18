"""关系驱动主动开口的 per-role 状态（架构计划 §5.2）。

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


def _row_to_state(row: dict[str, Any]) -> ProactiveState:
    calibration: dict[str, Any] = {}
    if row["calibration_json"]:
        with contextlib.suppress(Exception):
            calibration = json.loads(row["calibration_json"])
    return ProactiveState(
        role_id=row["role_id"],
        affinity=float(row["affinity"] or 0.0),
        last_interaction_utc=_parse_ts(
            row["last_interaction_utc"]
        ),
        calibration=calibration,
    )


def get_state(conn: SqlConnection, role_id: str) -> ProactiveState:
    """读某角色的主动状态；无记录 = 全新状态（affinity 0）。"""
    row = conn.execute(
        "SELECT role_id, affinity, last_interaction_utc, calibration_json "
        "FROM role_proactive_state WHERE role_id = ?",
        (role_id,),
    ).fetchone()
    if row is None:
        return ProactiveState(role_id=role_id)
    return _row_to_state(row)


def save_state(conn: SqlConnection, state: ProactiveState) -> None:
    conn.execute(
        "INSERT INTO role_proactive_state "
        "(role_id, affinity, last_interaction_utc, calibration_json, updated_at) "
        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(role_id) DO UPDATE SET "
        "affinity = excluded.affinity, last_interaction_utc = excluded.last_interaction_utc, "
        "calibration_json = excluded.calibration_json, updated_at = CURRENT_TIMESTAMP",
        (
            state.role_id,
            state.affinity,
            state.last_interaction_utc.strftime("%Y-%m-%d %H:%M:%S")
            if state.last_interaction_utc
            else None,
            json.dumps(state.calibration, ensure_ascii=False),
        ),
    )
    conn.commit()


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
    "save_state",
]
