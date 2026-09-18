"""角色主动开口（core/reachout.py）的单元测试。

钉住四件事：两级授权（总闸 / 角色开关）、三层抑制（间隔 / 静默时段 / 未读堆积）、
生成必须过 guard（fail-closed：被拦不发）、调度一轮的落库与留痕。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage

from rolecard_agent.config import Settings
from rolecard_agent.core import reachout as svc
from rolecard_agent.core.reachout import ReachoutScheduler
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
    """真实当前时间（UTC + 本地）：间隔记录用 CURRENT_TIMESTAMP（UTC），必须与实例对得上。"""
    return datetime.now(UTC), datetime.now().astimezone().replace(tzinfo=None)


def _seed_last(conn, role_id: str, minutes_ago: int) -> None:
    ts = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
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