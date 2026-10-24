"""2a 刀的两个新增面的单元测试：`core/common/periodic.PeriodicTask` 骨架与
固定时刻问候档（`trigger_scheduled` + 角色卡 `greeting_at` 字段）。

钉住的行为：
* PeriodicTask —— 异常不杀死循环但必须带异常文本留痕、stop() join、重复 start 是 no-op；
* trigger_scheduled —— 命中分钟才触发、当日只发一次（读台账不建状态表）、没排就不触发；
* greeting_at 的形状校验与归一（空串 = None），以及经 RoleCards 的落库往返。
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.base.observability import TraceEvent, Tracer
from rolecard_agent.core.common.periodic import PeriodicTask
from rolecard_agent.features.reachout.triggers import trigger_scheduled
from rolecard_agent.roles.models import RoleCard, RoleCardCreate, RoleCardUpdate
from rolecard_agent.roles.service import RoleCards

ME = DEFAULT_USER_ID


def _role(greeting_at: str | None) -> RoleCard:
    return RoleCard(
        role_id="scheduled",
        role_name="定时角色",
        system_prompt="你是通用助手。",
        reachout_enabled=True,
        greeting_at=greeting_at,
    )


def _local(hour: int, minute: int) -> datetime:
    """本地时区的某个时刻（today）。触发判据只看 HH:MM 与本地日期。"""
    return datetime.now().astimezone().replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def _seed_scheduled_fire(conn: Any, *, role_id: str, user_id: str, days_ago: int = 0) -> None:
    """在投递台账里种一条 fired_by='scheduled' 的记录（created_at 是 UTC 的 CURRENT 格式）。"""
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO agent_reachout "
        "(role_id, user_id, role_name, text, state, fired_by, created_at) "
        "VALUES (?, ?, 'x', '早', 'read', 'scheduled', ?)",
        (role_id, user_id, ts),
    )
    conn.commit()


# ------------------------------------------------------------------ trigger_scheduled


def test_fires_at_the_scheduled_minute(conn) -> None:
    role = _role("09:00")
    assert trigger_scheduled(role, conn, user_id=ME, now_local=_local(9, 0)) == "scheduled"


def test_silent_outside_the_scheduled_minute(conn) -> None:
    role = _role("09:00")
    assert trigger_scheduled(role, conn, user_id=ME, now_local=_local(9, 1)) is None


def test_silent_when_nothing_scheduled(conn) -> None:
    role = _role(None)
    assert trigger_scheduled(role, conn, user_id=ME, now_local=_local(9, 0)) is None


def test_fires_once_per_day(conn) -> None:
    """台账里今天已有 fired_by='scheduled' 的记录 → 这一拍不再触发（挡 30s 内的第二次命中）。"""
    role = _role("09:00")
    _seed_scheduled_fire(conn, role_id=role.role_id, user_id=ME)
    assert trigger_scheduled(role, conn, user_id=ME, now_local=_local(9, 0)) is None


def test_yesterday_fire_does_not_block_today(conn) -> None:
    role = _role("09:00")
    _seed_scheduled_fire(conn, role_id=role.role_id, user_id=ME, days_ago=1)
    assert trigger_scheduled(role, conn, user_id=ME, now_local=_local(9, 0)) == "scheduled"


def test_fires_only_for_that_owner(conn) -> None:
    """台账按 (role_id, user_id) 记账：别的身份发过不挡这一位（多租户 B2 的口径）。"""
    role = _role("09:00")
    _seed_scheduled_fire(conn, role_id=role.role_id, user_id="someone-else")
    assert trigger_scheduled(role, conn, user_id=ME, now_local=_local(9, 0)) == "scheduled"


# ------------------------------------------------------------------ greeting_at 校验


def test_greeting_at_accepts_hhmm_and_normalizes_empty() -> None:
    assert _role("09:00").greeting_at == "09:00"
    assert _role("23:59").greeting_at == "23:59"
    assert _role("").greeting_at is None  # 空串 = 没排，归一成 None
    assert _role(None).greeting_at is None


@pytest.mark.parametrize("bad", ["9:00", "0900", "24:00", "09:60", "09:00 ", "9"])
def test_greeting_at_rejects_bad_shapes(bad: str) -> None:
    with pytest.raises(ValidationError):
        _role(bad)


# ------------------------------------------------------------------ RoleCards 往返


def test_greeting_at_survives_create_update_clear(conn) -> None:
    cards = RoleCards(conn, user_id=ME)
    cards.create(
        RoleCardCreate(
            role_id="greet_card",
            role_name="带排程的卡",
            system_prompt="你是通用助手。",
            greeting_at="08:30",
        )
    )
    assert cards.get("greet_card").greeting_at == "08:30"
    cards.update("greet_card", RoleCardUpdate(greeting_at="21:05"))
    assert cards.get("greet_card").greeting_at == "21:05"
    # 空串 = 清除（归一成 None）：当日不再触发，且与"从未排过"同一个存储形状。
    cards.update("greet_card", RoleCardUpdate(greeting_at=""))
    assert cards.get("greet_card").greeting_at is None


# ------------------------------------------------------------------ PeriodicTask


class _RecordingTracer(Tracer):
    """把 emit 出来的事件全收进列表（判"错误留痕带异常文本"用）。"""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        self.events.append(event)


class _CountingTask(PeriodicTask):
    """数拍的骨架实现：第 k 拍按预定抛错。"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._guard = threading.Lock()
        self.ticks = 0
        self.fail_at: set[int] = set()

    def tick_once(self) -> None:
        with self._guard:
            self.ticks += 1
            n = self.ticks
        if n in self.fail_at:
            raise RuntimeError(f"boom-{n}")


def test_periodic_task_ticks_until_stopped() -> None:
    tracer = _RecordingTracer()
    task = _CountingTask(
        name="test-tick", tick_interval=0.01, tracer=tracer,
        error_event="test_tick_error", node="test",
    )
    task.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with task._guard:
            if task.ticks >= 3:
                break
        time.sleep(0.01)
    task.stop()
    assert task.ticks >= 3
    assert not tracer.events  # 全程无错 = 无错误事件


def test_periodic_task_survives_tick_errors_and_traces_them() -> None:
    tracer = _RecordingTracer()
    task = _CountingTask(
        name="test-tick", tick_interval=0.01, tracer=tracer,
        error_event="test_tick_error", node="test",
    )
    task.fail_at = {1, 3}
    task.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with task._guard:
            if task.ticks >= 4:
                break
        time.sleep(0.01)
    task.stop()
    assert task.ticks >= 4  # 第 1、3 拍抛错没有打死循环
    errors = [e for e in tracer.events if e.event == "test_tick_error"]
    assert len(errors) == 2
    assert all("RuntimeError" in str(e.detail.get("error")) for e in errors)
    assert all(e.detail.get("fatal") is False for e in errors)


def test_periodic_task_stop_joins_and_double_start_is_noop() -> None:
    task = _CountingTask(
        name="test-tick", tick_interval=30.0, tracer=_RecordingTracer(),
        error_event="test_tick_error", node="test",
    )
    task.start()
    first = task._thread
    assert first is not None and first.is_alive()
    task.start()  # 重复 start = no-op，不产生第二条循环
    assert task._thread is first
    task.stop()
    assert not first.is_alive()  # join 等到了线程真退出
    with task._guard:
        assert task.ticks == 0  # 30s 间隔：没到第一拍就被停了
