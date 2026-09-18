"""角色主动开口（core/reachout.py）的单元测试。

钉住四件事：两级授权（总闸 / 角色开关）、三层抑制（间隔 / 静默时段 / 未读堆积）、
生成必须过 guard（fail-closed：被拦不发）、调度一轮的落库与留痕。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from langchain_core.messages import AIMessage

from rolecard_agent.config import Settings
from rolecard_agent.core import file_watch as fw
from rolecard_agent.core import reachout as svc
from rolecard_agent.core.memory import save_role_memory_text
from rolecard_agent.core.proactive_state import DEFAULT_AFFINITY_THRESHOLD, get_state
from rolecard_agent.core.reachout import ReachoutScheduler
from rolecard_agent.core.workspace import resolve_task_dir
from rolecard_agent.roles.models import RoleCard

_UTC = UTC


def _role(reachout_enabled: bool = True) -> RoleCard:
    return RoleCard(
        role_id="active",
        role_name="主动角色",
        system_prompt="你是通用助手。",
        reachout_enabled=reachout_enabled,
    )


def _settings(**kw: object) -> Settings:
    return Settings(reachout_interval_minutes=kw.pop("reachout_interval_minutes", 60), **kw)


def _now() -> tuple[datetime, datetime]:
    """间隔档用 CURRENT_TIMESTAMP（真实 UTC），now_utc 必须与之对得上；但 now_local 强制
    落到非静默时刻（本地 14:00）—— 否则 23:00–08:00 跑整套 tick/blocked 用例会误命中静默档
    而集体失败。要单独测静默行为的用例自带显式 quiet_local（见 _blocked_by_quiet_hours 等）。
    """
    real_utc = datetime.now(UTC)
    safe_local = datetime.now().astimezone().replace(
        tzinfo=None, hour=14, minute=0, second=0, microsecond=0
    )
    return real_utc, safe_local


def _seed_last(conn, role_id: str, minutes_ago: int) -> None:
    ts = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, state, created_at) "
        "VALUES (?, ?, ?, 'read', ?)",
        (role_id, "x", "旧消息", ts),
    )
    conn.commit()


# ------------------------------------------------------------------ 抑制判定


def test_mark_read_transitions_state(conn) -> None:
    conn.execute("INSERT INTO agent_reachout (role_id, role_name, text) VALUES ('a', 'x', '嗨')")
    conn.commit()
    rid = conn.execute("SELECT id FROM agent_reachout").fetchone()["id"]
    assert svc.mark_read(conn, int(rid)) is True  # unread → read
    assert svc.mark_read(conn, int(rid)) is False  # 已读再标 = 无变化
    rows = conn.execute("SELECT state FROM agent_reachout").fetchall()
    assert rows[0]["state"] == "read"


def test_blocked_why_is_none_when_all_clear(conn) -> None:
    utc, local = _now()
    assert svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local) is None


def test_blocked_by_interval(conn) -> None:
    _seed_last(conn, "active", minutes_ago=10)  # 间隔 60 分钟，10 分钟前刚开口
    utc, local = _now()
    reason = svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local)
    assert reason is not None and "60 分钟" in reason


def test_blocked_by_quiet_hours(conn) -> None:
    utc = datetime(2026, 9, 18, 5, 0, tzinfo=_UTC)
    local = datetime(2026, 9, 18, 0, 30)  # 本地 00:30 = 静默时段
    reason = svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local)
    assert reason is not None and "静默" in reason


def test_blocked_by_unread_backlog(conn) -> None:
    for i in range(svc.MAX_UNREAD_PER_ROLE):
        conn.execute(
            "INSERT INTO agent_reachout (role_id, role_name, text) VALUES ('active', 'x', ?)",
            (f"未读 {i}",),
        )
    conn.commit()
    utc, local = _now()
    # interval=0 先放行间隔层，单独验证未读堆积这一层
    reason = svc.blocked_why(
        _role(),
        _settings(reachout_interval_minutes=0),
        conn,
        now_utc=utc,
        now_local=local,
    )
    assert reason is not None and "未读" in reason


# ------------------------------------------------------------------ 生成（guard 纪律）


class _FakeModel:
    def __init__(self, reply: AIMessage) -> None:
        self._reply = reply
        self.prompt: list | None = None

    def invoke(self, prompt: list, **kwargs: object) -> AIMessage:
        self.prompt = prompt
        return self._reply


def test_generate_returns_text_and_uses_persona_memory(conn) -> None:
    role = _role()
    model = _FakeModel(AIMessage(content="你今天还好吗？"))
    text = svc.generate_reachout_text(role, model, _settings(), conn)
    assert text == "你今天还好吗？"
    joined = "".join(str(m.content) for m in model.prompt or [])
    assert role.system_prompt in joined  # 人设进系统提示词


def test_generate_drops_guarded_output(conn) -> None:
    """guard fail-closed：被拦下的输出**不发**（而不是过滤后发）。"""
    role = _role()
    model = _FakeModel(AIMessage(content="我建议你服用阿莫西林，一次两粒。"))
    assert svc.generate_reachout_text(role, model, _settings(), conn) is None


def test_generate_ignores_empty_reply(conn) -> None:
    model = _FakeModel(AIMessage(content="   "))
    assert svc.generate_reachout_text(_role(), model, _settings(), conn) is None


# ------------------------------------------------------------------ 调度一轮


class _Roles:
    def __init__(self, roles: list[RoleCard]) -> None:
        self._roles = roles

    def list_roles(self) -> list[RoleCard]:
        return self._roles


def _scheduler(conn, roles: list[RoleCard], model) -> ReachoutScheduler:
    return ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles(roles),  # type: ignore[arg-type]
        model_resolver=lambda _name: model,
        conn=conn,
        tracer=_Tracer(),
    )


class _Tracer:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


def test_tick_once_enables_role_and_respects_fields(conn) -> None:
    model = _FakeModel(AIMessage(content="嗨"))
    scheduler = _scheduler(conn, [_role(reachout_enabled=True)], model)
    utc, local = _now()
    made = scheduler.tick_once(now_utc=utc, now_local=local)
    assert made == 1
    rows = conn.execute("SELECT role_id, state FROM agent_reachout").fetchall()
    assert rows[0]["role_id"] == "active"
    assert rows[0]["state"] == "unread"  # 新开口默认未读（红点来源）


def test_tick_once_respects_master_switch(conn) -> None:
    off = _settings(reachout_enabled=False)
    scheduler = ReachoutScheduler(
        settings_provider=lambda: off,
        roles=_Roles([_role()]),  # type: ignore[arg-type]
        model_resolver=lambda _n: _FakeModel(AIMessage(content="不该发")),
        conn=conn,
        tracer=_Tracer(),
    )
    assert scheduler.tick_once() == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_reachout").fetchone()["n"] == 0


def test_tick_once_skips_roles_without_permission(conn) -> None:
    model = _FakeModel(AIMessage(content="嗨"))
    scheduler = _scheduler(conn, [_role(reachout_enabled=False)], model)
    utc, local = _now()
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_reachout").fetchone()["n"] == 0


def test_tick_once_continues_after_role_failure(conn) -> None:
    """一个角色（生成抛错）不阻塞其它角色，且错误留痕。"""
    roles = [_role(), RoleCard(**{**_role().model_dump(), "role_id": "second"})]

    class _Pick:
        def __init__(self) -> None:
            self.n = 0

        def __call__(self, _name: str | None) -> object:
            self.n += 1
            if self.n == 1:
                raise RuntimeError("模型挂了")
            return _FakeModel(AIMessage(content="我还在"))

    pick = _Pick()
    scheduler = ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles(roles),  # type: ignore[arg-type]
        model_resolver=pick,
        conn=conn,
        tracer=_Tracer(),
    )
    utc, local = _now()
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_reachout").fetchone()["n"] == 1


# ------------------------------------------------------------------ 关系驱动触发源（§5.2 四分类）


def _now_local() -> datetime:
    return datetime.now(UTC).astimezone()


def _seed_state(conn, role_id: str, affinity: float = 0.0) -> None:
    conn.execute(
        "INSERT INTO role_proactive_state "
        "(role_id, affinity, last_interaction_utc) VALUES (?, ?, ?) "
        "ON CONFLICT(role_id) DO UPDATE SET affinity = excluded.affinity",
        (role_id, affinity, datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def test_trigger_affection_fires_at_threshold(conn) -> None:
    _seed_state(conn, "active", DEFAULT_AFFINITY_THRESHOLD)
    state = get_state(conn, "active")
    utc, _ = _now()
    got = svc.trigger_affection(_role(), state, _settings(), now_utc=utc)
    assert got == "affection"


def test_trigger_affection_silent_when_low(conn) -> None:
    state = get_state(conn, "active")  # affinity 0
    utc, _ = _now()
    got = svc.trigger_affection(_role(), state, _settings(), now_utc=utc)
    assert got is None


def test_trigger_time_pattern_fires_on_modal_hour(conn) -> None:
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(3):
        conn.execute(
            "INSERT INTO agent_reachout (role_id, role_name, text, created_at) "
            "VALUES ('active','x','hi',?)",
            (ts,),
        )
    conn.commit()
    # 历史记录的本地小时 == 当前本地小时 → 众数即当前小时，样本足够。
    got = svc.trigger_time_pattern(_role(), conn, now_local=_now_local())
    assert got == "time_pattern"


def test_trigger_time_pattern_silent_without_history(conn) -> None:
    got = svc.trigger_time_pattern(_role(), conn, now_local=_now_local())
    assert got is None


def test_trigger_recall_fires_when_role_memory_present(conn) -> None:
    save_role_memory_text(conn, "active", "用户上周说想学吉他。")
    got = svc.trigger_recall(_role(), conn, now_local=_now_local())
    assert got == "recall"


def test_trigger_recall_silent_without_memory(conn) -> None:
    got = svc.trigger_recall(_role(), conn, now_local=_now_local())
    assert got is None


def test_trigger_recall_respects_role_toggle(conn) -> None:
    """回忆触发受 per-role 开关闸门：关掉后即使有专属记忆也不触发。"""
    save_role_memory_text(conn, "active", "用户上周说想学吉他。")
    role = RoleCard(**{**_role().model_dump(), "recall_enabled": False})
    assert svc.trigger_recall(role, conn, now_local=_now_local()) is None
    # 开关开着则照常触发（对照）
    assert svc.trigger_recall(_role(), conn, now_local=_now_local()) == "recall"


def test_trigger_time_pattern_respects_role_toggle(conn) -> None:
    """时段规律触发受 per-role 开关闸门：关掉后即使众数命中当前小时也不触发。"""
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(3):
        conn.execute(
            "INSERT INTO agent_reachout (role_id, role_name, text, created_at) "
            "VALUES ('active','x','hi',?)",
            (ts,),
        )
    conn.commit()
    role = RoleCard(**{**_role().model_dump(), "time_pattern_enabled": False})
    assert svc.trigger_time_pattern(role, conn, now_local=_now_local()) is None
    assert svc.trigger_time_pattern(_role(), conn, now_local=_now_local()) == "time_pattern"


def test_tick_once_runs_affection_trigger_and_bumps_affinity(conn) -> None:
    _seed_state(conn, "active", DEFAULT_AFFINITY_THRESHOLD)
    model = _FakeModel(AIMessage(content="嗨，想你啦"))
    scheduler = _scheduler(conn, [_role()], model)
    utc, local = _now()
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    state = get_state(conn, "active")
    assert state.affinity > DEFAULT_AFFINITY_THRESHOLD  # 开口后关系数值 +增量


def test_generate_recall_mode_uses_role_memory(conn) -> None:
    """回忆触发的生成必须读该角色的专属记忆（per-role 隔离），而不是全局记忆。"""
    save_role_memory_text(conn, "active", "专属记忆：他养了只猫。")
    role = _role()
    model = _FakeModel(AIMessage(content="我记得你养了猫。"))
    text = svc.generate_reachout_text(
        role, model, _settings(), conn, role_id="active", mode="recall"
    )
    assert text == "我记得你养了猫。"
    joined = "".join(str(m.content) for m in model.prompt or [])
    assert "他养了只猫" in joined  # 角色专属记忆进提示词


def test_list_reachouts_filters_by_role(conn) -> None:
    """收件箱可按角色过滤（架构计划 §5.3：按角色卡隔离查看历史）。"""
    conn.execute("INSERT INTO agent_reachout (role_id, role_name, text) VALUES ('a','甲','找过你')")
    conn.execute("INSERT INTO agent_reachout (role_id, role_name, text) VALUES ('b','乙','也找过')")
    conn.commit()
    all_rows = svc.list_reachouts(conn)
    assert len(all_rows["items"]) == 2
    only_a = svc.list_reachouts(conn, role_id="a")
    assert len(only_a["items"]) == 1
    assert only_a["items"][0]["role_id"] == "a"
    # 未读数也按角色收敛
    assert only_a["unread"] == 1
    assert all_rows["unread"] == 2


# ------------------------------------------------------------------ 文件事件触发（架构计划 C·§5.2）


def _fw_settings(task_dir: Path, **kw: object) -> Settings:
    return Settings(
        file_watch_enabled=kw.pop("file_watch_enabled", True),
        workspace_dir=str(task_dir),
        reachout_interval_minutes=kw.pop("reachout_interval_minutes", 60),
        **kw,
    )


def _file_scheduler(conn, roles: list[RoleCard], model, task_dir: Path, **skw: object):
    settings = _fw_settings(task_dir, **skw)
    tracer = _Tracer()
    scheduler = ReachoutScheduler(
        settings_provider=lambda: settings,
        roles=_Roles(roles),  # type: ignore[arg-type]
        model_resolver=lambda _name: model,
        conn=conn,
        tracer=tracer,
    )
    return scheduler, settings, tracer


def _prime_baseline(conn, settings: Settings, task_dir: Path, when: datetime) -> None:
    """在调度器之外先建好基线（免得 tick 的 timer 基线触发先开口搅局）。"""
    fw.clear_state(conn)
    assert fw.check_changes(conn, resolve_task_dir(settings, conn), now_utc=when) is None


def test_blocked_why_interval_exempt_for_file_event_only(conn) -> None:
    _seed_last(conn, "active", minutes_ago=10)  # 间隔 60 分钟内
    utc, local = _now()
    # 常规：被间隔挡住
    assert svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local) is not None
    # file_event：豁免间隔档（素材门控）
    assert (
        svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local, file_event=True)
        is None
    )


def test_blocked_why_file_event_still_respects_quiet_hours(conn) -> None:
    utc = datetime(2026, 9, 18, 5, 0, tzinfo=_UTC)
    local = datetime(2026, 9, 18, 0, 30)  # 本地 00:30 = 静默时段
    reason = svc.blocked_why(
        _role(), _settings(), conn, now_utc=utc, now_local=local, file_event=True
    )
    assert reason is not None and "静默" in reason  # 用户级护栏不豁免


def test_generate_file_event_mode_injects_change_list(conn) -> None:
    role = _role()
    model = _FakeModel(AIMessage(content="看到你把报告放进目录啦？"))
    text = svc.generate_reachout_text(
        role,
        model,
        _settings(),
        conn,
        role_id="active",
        mode="file_event",
        file_list="- 新增：报告.md",
    )
    assert text == "看到你把报告放进目录啦？"
    joined = "".join(str(m.content) for m in model.prompt or [])
    assert "报告.md" in joined  # 素材清单进提示词
    assert "不得编造文件内容" in joined  # 口吻指令带防编造纪律


def test_format_change_list_caps_and_marks_truncated() -> None:
    events = [fw.FileEvent(op="add", path=f"f{i}.txt") for i in range(12)]
    text = svc._format_change_list(events, truncated=True)
    assert "f0.txt" in text and "f9.txt" in text
    assert "f10.txt" not in text  # 第 11 条起只进总数
    assert "等共 12 项" in text and "部分快照" in text
    assert svc._format_change_list(None) == ""


def test_tick_file_event_bypasses_interval_and_records_trigger(conn, tmp_path: Path) -> None:
    task_dir = tmp_path / "wd"
    task_dir.mkdir()
    model = _FakeModel(AIMessage(content="目录里有新文件？"))
    scheduler, settings, tracer = _file_scheduler(conn, [_role()], model, task_dir)
    utc, local = _now()
    _prime_baseline(conn, settings, task_dir, utc)
    (task_dir / "季度报告.md").write_text("x", encoding="utf-8")
    _seed_last(conn, "active", minutes_ago=10)  # 常规会被间隔挡住
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    joined = "".join(str(m.content) for m in model.prompt or [])
    assert "季度报告.md" in joined
    sent = [e for e in tracer.events if getattr(e, "event", "") == "reachout_sent"]
    assert sent and sent[-1].detail["trigger"] == "file_event"
    # 消费后基线推进：无新变化则下一轮静默（间隔此刻为 10 分钟前刚开口 → 被挡）
    assert fw.pending_count(conn) == 0


def test_tick_file_event_pending_survives_quiet_hours(conn, tmp_path: Path) -> None:
    task_dir = tmp_path / "wd"
    task_dir.mkdir()
    model = _FakeModel(AIMessage(content="不该在静默时段发"))
    scheduler, settings, tracer = _file_scheduler(conn, [_role()], model, task_dir)
    utc, local = _now()
    _prime_baseline(conn, settings, task_dir, utc)
    (task_dir / "a.txt").write_text("x", encoding="utf-8")
    quiet_local = datetime(2026, 9, 18, 0, 30)  # 本地 00:30
    made = scheduler.tick_once(now_utc=utc, now_local=quiet_local)
    assert made == 0
    assert fw.pending_count(conn) == 1  # 事件挂起，基线未推进
    assert not [e for e in tracer.events if getattr(e, "event", "") == "file_watch_advance"]


def test_tick_file_event_respects_role_toggle(conn, tmp_path: Path) -> None:
    task_dir = tmp_path / "wd"
    task_dir.mkdir()
    model = _FakeModel(AIMessage(content="不该发"))
    role = RoleCard(**{**_role().model_dump(), "file_watch_enabled": False})
    scheduler, settings, _tracer = _file_scheduler(conn, [role], model, task_dir)
    utc, local = _now()
    _prime_baseline(conn, settings, task_dir, utc)
    (task_dir / "a.txt").write_text("x", encoding="utf-8")
    _seed_last(conn, "active", minutes_ago=10)  # 角色闸门关 → 无豁免 → 被间隔挡
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 0
    assert fw.pending_count(conn) == 1  # 全局事件不丢，等着被消费或过期
    # 对照：开关开着的同一事件即触发
    on = _file_scheduler(conn, [_role()], _FakeModel(AIMessage(content="发")), task_dir)
    assert on[0].tick_once(now_utc=utc, now_local=local) == 1


def test_tick_file_event_shared_by_multiple_roles(conn, tmp_path: Path) -> None:
    task_dir = tmp_path / "wd"
    task_dir.mkdir()
    roles = [_role(), RoleCard(**{**_role().model_dump(), "role_id": "second"})]
    model = _FakeModel(AIMessage(content="目录有动静"))
    scheduler, settings, _tracer = _file_scheduler(conn, roles, model, task_dir)
    utc, local = _now()
    _prime_baseline(conn, settings, task_dir, utc)
    (task_dir / "b.txt").write_text("x", encoding="utf-8")
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 2  # 同一事件两个角色各说一句
    rows = conn.execute("SELECT role_id FROM agent_reachout ORDER BY id").fetchall()
    assert [r["role_id"] for r in rows] == ["active", "second"]
    assert fw.pending_count(conn) == 0  # 本轮消费 → 基线推进


def test_tick_file_watch_globally_off_is_inert(conn, tmp_path: Path) -> None:
    task_dir = tmp_path / "wd"
    task_dir.mkdir()
    model = _FakeModel(AIMessage(content="嗨"))
    scheduler, settings, tracer = _file_scheduler(
        conn, [_role()], model, task_dir, file_watch_enabled=False
    )
    utc, local = _now()
    (task_dir / "a.txt").write_text("x", encoding="utf-8")
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1  # timer 基线行为照旧
    assert fw.load_state(conn) is None  # 完全不扫描、不留状态
    assert not [e for e in tracer.events if getattr(e, "event", "") == "file_watch_advance"]
