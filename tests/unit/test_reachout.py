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
from rolecard_agent.core.memory import add_item
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
    # 已读再标 = **幂等成功**（收件箱列的是"未读+最近历史"，点已读条目是正常路径）。
    # 以前这里返回 False，与"记录不存在"混成一谈，用户看到的就是"主动消息不存在"的谎话。
    assert svc.mark_read(conn, int(rid)) is True
    assert svc.mark_read(conn, 999_999) is False  # 真没有这条才是 False
    rows = conn.execute("SELECT state FROM agent_reachout").fetchall()
    assert rows[0]["state"] == "read"


def test_blocked_why_is_none_when_all_clear(conn) -> None:
    utc, local = _now()
    assert svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local) is None


def test_blocked_while_that_conversation_is_talking(conn) -> None:
    """用户正在那条主动会话里回话（她的图在跑）⇒ 这次不插话（审计 #12）。

    判据是"那条会话的写入锁正被持有"，不是"最后一条消息过了多久"：后者在她答完、
    用户还没回的间隙里也是 False，而那同样不该再冒一句。
    """
    from rolecard_agent.core.thread_locks import (
        release_thread,
        try_thread_write,
    )

    utc, local = _now()
    role = _role()
    tid = svc.proactive_thread_id(role.role_id)
    assert try_thread_write(tid, timeout=0.0)
    try:
        reason = svc.blocked_why(role, _settings(), conn, now_utc=utc, now_local=local)
        assert reason and "正在对话" in reason
    finally:
        release_thread(tid)
    assert svc.blocked_why(role, _settings(), conn, now_utc=utc, now_local=local) is None


def test_blocked_by_interval(conn) -> None:
    _seed_last(conn, "active", minutes_ago=10)  # 间隔 60 分钟，10 分钟前刚开口
    utc, local = _now()
    reason = svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local)
    # 数字带 ±12% 抖动，所以钉"报了分钟数 + 没有未读就不提退避"，不钉 60。
    assert reason is not None and "分钟" in reason
    assert "退避" not in reason


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
    draft = svc.generate_reachout_text(role, model, _settings(), conn)
    assert draft.text == "你今天还好吗？" and draft.why == ""
    joined = "".join(str(m.content) for m in model.prompt or [])
    assert role.system_prompt in joined  # 人设进系统提示词


def test_generate_drops_guarded_output(conn) -> None:
    """guard fail-closed：被拦下的输出**不发**（而不是过滤后发），且原因为 `guard`。"""
    role = _role()
    model = _FakeModel(AIMessage(content="我建议你服用阿莫西林，一次两粒。"))
    draft = svc.generate_reachout_text(role, model, _settings(), conn)
    assert draft.text is None and draft.why == "guard"


def test_generate_ignores_empty_reply(conn) -> None:
    """空正文的原因必须是 `empty_output`，不能和"被 guard 拦下"混成同一个 None。

    真机实测过这条通路：qwen3-vl 会把整段 token 预算花在思考上、`done_reason=length`
    而正文为空（同一条 prompt 三次里两次如此）。混进 guard 那一类，就永远没人去修它。
    """
    model = _FakeModel(AIMessage(content="   "))
    draft = svc.generate_reachout_text(_role(), model, _settings(), conn)
    assert draft.text is None and draft.why == "empty_output"


# ----------------------------------------------------- 开口上下文：去重与"别指着空记忆说事"


def _prompt_text(model: _FakeModel) -> str:
    return "".join(str(m.content) for m in model.prompt or [])


def test_reachout_prompt_shows_what_it_already_said(conn) -> None:
    """E1 去重：该角色最近说过的原文要进 prompt，并明令别重复。

    没有这一层时每次开口都是"从零现编"—— 实测同一天连发四条"花海/阳光/亮晶晶"，
    症状不是模型差，是上下文里压根没有"我刚说过这些"。
    """
    role = _role()
    for text in ("今天花海真漂亮", "阳光正好呢"):
        conn.execute(
            "INSERT INTO agent_reachout (role_id, role_name, text, state) VALUES (?, ?, ?, 'read')",
            (role.role_id, role.role_name, text),
        )
    conn.commit()
    model = _FakeModel(AIMessage(content="新的一条"))
    svc.generate_reachout_text(role, model, _settings(), conn, role_id=role.role_id)
    joined = _prompt_text(model)
    assert "今天花海真漂亮" in joined and "阳光正好呢" in joined
    assert "别重复" in joined


def test_recent_context_is_isolated_per_role(conn) -> None:
    """per-role 铁律在这里同样成立：别人的主动历史不能变成"我说过的话"。"""
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text) "
        "VALUES ('other', '别人', '那是别人说的')"
    )
    conn.commit()
    model = _FakeModel(AIMessage(content="嗨"))
    svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active")
    assert "那是别人说的" not in _prompt_text(model)


def test_empty_memory_does_not_claim_long_term_memory(conn) -> None:
    """E3：记忆槽为空时，指令不再写"结合关于用户的长期记忆"（指着空槽说话=假契约）。"""
    model = _FakeModel(AIMessage(content="嗨"))
    svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active")
    assert "长期记忆" not in _prompt_text(model)


def test_memory_present_restores_the_memory_clause(conn) -> None:
    role = _role()
    add_item(conn, bucket=role.role_id, text="用户喜欢猫")
    model = _FakeModel(AIMessage(content="嗨"))
    svc.generate_reachout_text(role, model, _settings(), conn, role_id=role.role_id)
    joined = _prompt_text(model)
    assert "长期记忆" in joined and "用户喜欢猫" in joined


def test_recall_mode_without_memory_refuses_to_fake_the_past(conn) -> None:
    """recall 档在没记忆时必须换成"不假装记得往事"的指令。

    它的字面意思就是"提起一件之前答应过的事"，而素材只有一坨（可能为空的）记忆 ——
    对着空记忆说这句话，是在**要求模型捏造**（审计 §8 那条"人设只解读不捏造"）。
    """
    model = _FakeModel(AIMessage(content="嗨"))
    svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active", mode="recall")
    joined = _prompt_text(model)
    assert "不要假装记得" in joined
    assert "之前聊过或答应的事" not in joined


def test_generate_handles_block_shaped_reply(conn) -> None:
    """P1-8 回归：分块形态的回复不能变成 Python repr 冒给用户。

    多模态/流式模型的 `content` 常是块列表。旧实现 `str(getattr(reply, "content", ""))`
    于是把 `[{'type': 'text', 'text': '…'}]` 这一串送进 guard、再送进用户收件箱 ——
    guard 认不出这是承诺性话术（该拦的拦不住），用户看到的是一坨数据结构。
    """
    model = _FakeModel(AIMessage(content=[{"type": "text", "text": "今天过得怎么样？"}]))
    text = svc.generate_reachout_text(_role(), model, _settings(), conn).text
    assert text == "今天过得怎么样？"
    assert "type" not in (text or "") and "[" not in (text or "")


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


def test_an_empty_generation_leaves_a_trace_saying_why(conn) -> None:
    """空正文不能"静悄悄地少说一句" —— 必须留痕，且原因与 guard 拦下分得开。

    真机实测：qwen3-vl 会把整段 token 预算花在思考上、`done_reason=length` 而正文为空
    （同一条 prompt 三次里两次如此）。改造前 `generate_reachout_text` 对此返回裸 None，
    调度器 `continue`，轨迹里**什么也没有** —— 于是"她最近不找我了"这个症状没有任何地方
    能看出是模型坏了还是护栏在正常工作。
    """
    tracer = _Tracer()
    scheduler = ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles([_role(reachout_enabled=True)]),  # type: ignore[arg-type]
        model_resolver=lambda _n: _FakeModel(AIMessage(content="   ")),
        conn=conn,
        tracer=tracer,
    )
    utc, local = _now()
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 0
    skipped = [e for e in tracer.events if getattr(e, "event", "") == "reachout_skipped"]
    assert len(skipped) == 1
    assert skipped[0].detail["why"] == "empty_output"


# --------------------------------------------------------------- 主动会话（回得来的那条路）


def test_tick_delivers_into_the_proactive_thread(conn) -> None:
    """主动开口除了进收件箱，还要投进"该角色的主动会话"—— 否则用户回不了话。"""
    seen: list[tuple[str, str]] = []

    def _deliver(role: RoleCard, text: str) -> str:
        seen.append((role.role_id, text))
        # 真实投递（bootstrap.deliver_proactive）会顺手建会话行；这里照同一份合同建。
        tid = svc.proactive_thread_id(role.role_id)
        conn.execute(
            "INSERT INTO session_thread (thread_id, user_id, current_role_id, title)"
            " VALUES (?, 'u1', ?, ?)",
            (tid, role.role_id, svc.proactive_thread_title(role.role_name)),
        )
        conn.commit()
        return tid

    scheduler = ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles([_role()]),  # type: ignore[arg-type]
        model_resolver=lambda _n: _FakeModel(AIMessage(content="今天腰还酸吗")),
        conn=conn,
        tracer=_Tracer(),
        deliver=_deliver,
    )
    utc, local = _now()
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    assert seen == [("active", "今天腰还酸吗")]

    items = svc.list_reachouts(conn)["items"]
    assert items[0]["thread_id"] == svc.proactive_thread_id("active")


def test_old_messages_without_a_thread_are_not_links(conn) -> None:
    """跳转目标只在会话**真的存在**时给出。

    这个功能上线之前落库的主动消息没有对应的线程，前端若照着 id 跳，用户看到的是一句
    "加载历史失败" —— 宁可退回到"只能标记已读"。
    """
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text) VALUES ('old', '旧角色', '很久以前')"
    )
    conn.commit()
    assert svc.list_reachouts(conn)["items"][0]["thread_id"] is None


def test_deliver_failure_keeps_the_inbox_message_and_is_traced(conn) -> None:
    """投递炸了不能把消息一起吞掉：用户仍看得见那条，只是暂时点不进会话（要留痕）。"""

    def _boom(_role: RoleCard, _text: str) -> str:
        raise RuntimeError("checkpoint busy")

    tracer = _Tracer()
    scheduler = ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles([_role()]),  # type: ignore[arg-type]
        model_resolver=lambda _n: _FakeModel(AIMessage(content="嗨")),
        conn=conn,
        tracer=tracer,
        deliver=_boom,
    )
    utc, local = _now()
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_reachout").fetchone()["n"] == 1
    events = [getattr(e, "event", "") for e in tracer.events]
    assert "reachout_deliver_failed" in events


def test_without_a_deliverer_the_inbox_still_works(conn) -> None:
    """没接投递接缝（纯内核装配 / 单测）时主动消息照发 —— 两级是可选叠加，不是前置条件。"""
    scheduler = _scheduler(conn, [_role()], _FakeModel(AIMessage(content="嗨")))
    utc, local = _now()
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1


def test_mark_role_read_clears_the_whole_bundle(conn) -> None:
    """跳进主动会话 = 那一摞都算读过：一次标完，别留"半已读"骗红点数字。"""
    for text in ("一", "二"):
        conn.execute(
            "INSERT INTO agent_reachout (role_id, role_name, text) VALUES ('a', 'x', ?)", (text,)
        )
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text) VALUES ('b', 'y', '别人的')"
    )
    conn.commit()

    assert svc.mark_role_read(conn, "a") == 2
    assert svc.list_reachouts(conn)["unread"] == 1  # 别的角色不受影响
    assert svc.mark_role_read(conn, "a") == 0  # 再标一次没有变化


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
    add_item(conn, bucket="active", text="用户上周说想学吉他。")
    got = svc.trigger_recall(_role(), conn, now_local=_now_local())
    assert got == "recall"


def test_trigger_recall_silent_without_memory(conn) -> None:
    got = svc.trigger_recall(_role(), conn, now_local=_now_local())
    assert got is None


def test_trigger_recall_respects_role_toggle(conn) -> None:
    """回忆触发受 per-role 开关闸门：关掉后即使有专属记忆也不触发。"""
    add_item(conn, bucket="active", text="用户上周说想学吉他。")
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
    add_item(conn, bucket="active", text="专属记忆：他养了只猫。")
    role = _role()
    model = _FakeModel(AIMessage(content="我记得你养了猫。"))
    text = svc.generate_reachout_text(
        role, model, _settings(), conn, role_id="active", mode="recall"
    ).text
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
    ).text
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


def test_thread_lines_reach_the_prompt(conn) -> None:
    """用户在桌宠上回的话必须进下一条的上下文 —— 不给她看，她就复读自己那条旧台词。"""
    model = _FakeModel(AIMessage(content="跑完记得拉伸一下。"))
    svc.generate_reachout_text(
        _role(),
        model,
        _settings(),
        conn,
        role_id='active',
        thread_lines=svc.format_thread_lines(
            [('用户', '刚跑完步，坐下歇会儿。'), ('你', '辛苦啦')]
        ),
    )
    joined = _prompt_text(model)
    assert '刚跑完步，坐下歇会儿。' in joined
    assert '还没有接过话' in joined


def test_unanswered_lines_stops_at_her_own_last_utterance() -> None:
    """素材只取"她最后说过话之后"那一截 —— 答过的话不能再当由头（见 test_bootstrap 同名场景）。"""
    assert svc.unanswered_lines([("用户", "想你了")]) == [("用户", "想你了")]
    # 她已经答过 → 空：这次开口不能把同一句再回一遍
    assert svc.unanswered_lines([("用户", "想你了"), ("你", "我也想")]) == []
    # 分界线之后又攒了新话 → 只留新的那些，且保持时间正序
    assert svc.unanswered_lines(
        [("你", "旧台词"), ("用户", "刚跑完步"), ("你", "辛苦啦"), ("用户", "你在哪呢")]
    ) == [("用户", "你在哪呢")]
    # 全是她说的 / 空表 → 空
    assert svc.unanswered_lines([("你", "一句"), ("你", "两句")]) == []
    assert svc.unanswered_lines([]) == []


def test_thread_lines_are_empty_when_she_has_the_last_word() -> None:
    """串起来的那条：她刚答完话的会话，主动开口拿到的上下文必须是空串而不是旧台词。"""
    rows = [("你", "外头降温了，穿上外套。"), ("用户", "想你了"), ("你", "我也想你")]
    assert svc.format_thread_lines(svc.unanswered_lines(rows)) == ""


def test_format_thread_lines_keeps_the_tail_in_order() -> None:
    """只留最后几条、按时间正序；全空白不该产出一段空上下文。"""
    rows = [('你' if i % 2 == 0 else '用户', f'第{i}条') for i in range(10)]
    out = svc.format_thread_lines(rows, limit=3)
    assert '第7条' in out and '第9条' in out
    assert '第6条' not in out
    assert out.index('第7条') < out.index('第9条')
    assert svc.format_thread_lines([('用户', '   ')]) == ''


class _ScriptedModel:
    """按脚本依次吐出回复，并记下每次收到的 prompt（重生那条测试要看第二次的指令）。

    `usages` 给定时第 n 次调用带上第 n 份用量 —— 账要按调用次数列，一次重生就是两份。
    """

    def __init__(self, replies: list[str], usages: list[dict] | None = None) -> None:
        self._replies = list(replies)
        self._usages = list(usages or [])
        self.prompts: list[list] = []

    def invoke(self, prompt: list, **kwargs: object) -> AIMessage:
        self.prompts.append(prompt)
        content = self._replies.pop(0) if self._replies else ""
        usage = self._usages.pop(0) if self._usages else None
        return AIMessage(content=content, usage_metadata=usage)


_PRIOR = (
    "（指尖轻点裙摆，忽然歪头笑出声）哎呀，今天的乐土风铃草开得正盛呢♪ "
    "要是被花粉困扰的话……记得好好看看我的眼睛哦，这样就能在花雨里找到回家的路啦♪"
)
_NEW = "风也有颜色哦，你猜猜——此刻越过乐土的那阵，是不是带着花瓣的粉？♪"


def _seed_prior(conn, role_id: str = "active") -> None:
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, state) "
        "VALUES (?, '主动角色', ?, 'read')",
        (role_id, _PRIOR),
    )
    conn.commit()


def test_reachout_regenerates_once_when_the_draft_echoes_her_own_line(conn) -> None:
    """草稿与她旧话逐字相同 ⇒ 换指令重来一次，发的是第二条。"""
    _seed_prior(conn)
    model = _ScriptedModel([_PRIOR, _NEW])
    draft = svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active")
    assert draft.text == _NEW
    assert len(model.prompts) == 2, "只该重生一次"
    second = "".join(str(m.content) for m in model.prompts[1])
    assert "换个说法" in second and "乐土风铃草" in second, "重生的指令要说清哪句不算数"
    assert draft.score < svc.REGEN_SCORE


def test_reachout_stays_silent_when_both_drafts_echo(conn) -> None:
    """两次都一样 ⇒ **宁可不说**（`why="repeat"`），而不是把复读发进收件箱。"""
    _seed_prior(conn)
    model = _ScriptedModel([_PRIOR, _PRIOR])
    draft = svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active")
    assert draft.text is None and draft.why == "repeat"
    assert draft.score > svc.DROP_SCORE
    assert len(model.prompts) == 2, "重生一次就收工，不许无限重试"
    assert conn.execute("SELECT COUNT(*) c FROM agent_reachout").fetchone()["c"] == 1, "不该落库"


def test_failed_regeneration_keeps_the_first_draft(conn) -> None:
    """重生失败（空正文）时**保留第一条**：第一条只是"像她自己"，不是坏内容。

    因为重生的故障吞掉一条本来能发的话，是我们亏 —— 用户读到的会是"她不找我了"。
    """
    _seed_prior(conn)
    model = _ScriptedModel([_PRIOR, ""])
    draft = svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active")
    assert draft.text == _PRIOR and draft.why == ""
    assert len(model.prompts) == 2


def test_ordinary_draft_costs_exactly_one_call(conn) -> None:
    """普通内容一次调用就够：闸门不能变成"每次都多问一遍"的税。"""
    _seed_prior(conn)
    model = _ScriptedModel([_NEW])
    draft = svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active")
    assert draft.text == _NEW and len(model.prompts) == 1
    assert draft.score < svc.REGEN_SCORE


def test_no_role_id_means_no_scoring_corpus(conn) -> None:
    """没给 role_id（全局口吻）时无从取"她最近说过什么" ⇒ 不判，也不炸。"""
    model = _ScriptedModel([_PRIOR])
    draft = svc.generate_reachout_text(_role(), model, _settings(), conn)
    assert draft.text == _PRIOR and draft.score == 0.0
    assert len(model.prompts) == 1


def test_reachout_costs_are_booked_per_call(conn) -> None:
    """主动开口花的 token 进账，重生一次就是两次调用（审计 §12.8）。

    这条是 §12.1 那道闸门的配套：闸门说"太像就重来一次"，那笔钱必须看得见，
    否则"为了不复读而把每天的双倍调用悄悄加上去"没人会注意到。
    （`_ScriptedModel` 的 `usages` 一份对应一次调用。）
    """
    _seed_prior(conn)
    model = _ScriptedModel(
        [_PRIOR, _NEW],
        [
            {"input_tokens": 800, "output_tokens": 40, "total_tokens": 840},
            {"input_tokens": 820, "output_tokens": 25, "total_tokens": 845},
        ],
    )
    draft = svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active")
    row = conn.execute("SELECT * FROM token_usage_day").fetchone()
    assert row["calls"] == 2 and row["prompt_tokens"] == 1620 and row["completion_tokens"] == 65
    assert draft.tokens == 840 + 845, "草稿要带回这一次开口一共花了多少（审计里那行 tokens）"


def test_reachout_books_a_call_even_when_the_model_reports_nothing(conn) -> None:
    """后端没报用量也要记下"发生过一次调用"（unreported 那一列），否则账上看着是免费。"""
    model = _ScriptedModel([_NEW])
    svc.generate_reachout_text(_role(), model, _settings(), conn, role_id="active")
    row = conn.execute("SELECT calls, unreported FROM token_usage_day").fetchone()
    assert row["calls"] == 1 and row["unreported"] == 1


def test_task_text_names_the_template_explicitly(conn) -> None:
    """实测 8/8 条以（动作）开头、正文一半以「哎呀，今天的」起头、字数挤在窄带 ——
    指令里要点名这件事，而且**按 §8.2 第 2 条改正写**：给"该怎么写"，不是只列"别怎么写"。

    2026-09-23 又改了一次：那条"要写动作就放句中"配上末尾那句"没说的动作和神态用（）包起来"，
    实测把主动会话推成 20 句里 15 句以（开头（同一条卡在普通对话里是 0%）—— 于是
    改成直接禁止旁白，这里钉的就是**新**那四条。
    """
    model = _FakeModel(AIMessage(content='嗨'))
    svc.generate_reachout_text(_role(), model, _settings(), conn, role_id='active')
    joined = _prompt_text(model)
    assert '上一条的开头几个字这次不要用' in joined
    assert '长度由内容决定' in joined
    assert '结尾换一种句式收' in joined
    assert '只写你会说出口的那句话' in joined
    # 旧写法里那两句"教她写（）"的不能悄悄回来。
    assert '动作和神态用（）包起来' not in joined
    assert '一个动作起头' not in joined


def test_quiet_minutes_grows_with_unread_and_jitters_deterministically() -> None:
    """退避按未读翻倍；抖动**按 (角色, 上次开口) 确定**，不是每 tick 重摇的抽签。

    为什么钉"确定性"：调度器每 30 秒问一次"够久了吗"。若阈值每次重算都不同，
    "哪一刻够格"就成了一场抽签 —— 测试钉不住，真机上还会抖出谁也复现不了的时机。
    """
    seed = "active|2026-09-22T00:00:00+00:00"
    plain = svc._quiet_minutes(60, 0, seed)
    assert 60 * 0.88 <= plain <= 60 * 1.12, "抖动只能 ±12%"
    assert svc._quiet_minutes(60, 0, seed) == plain, "同一个种子必须算出同一个数"
    assert svc._quiet_minutes(60, 1, seed) == plain * svc.BACKOFF_GROWTH
    assert svc._quiet_minutes(60, 0, "other|" + seed.split("|", 1)[1]) != plain, "换角色要换节奏"
    assert svc._quiet_minutes(60, 0, seed.replace("00:00", "00:01")) != plain, "换开口时刻也要换"


def test_blocked_by_interval_backs_off_per_unread(conn) -> None:
    """她说了你没回 → 要等的间隔翻倍；这条把"退避真的进了闸门"钉住。

    只改 `state`、不再插新行：新行的 `created_at` 会把"上次开口"挪到现在，那样拦截与退避
    无关，测试就变成钉了个假东西。
    """
    _seed_last(conn, "active", minutes_ago=100)  # 已读，且超过基础 60 分钟
    utc, local = _now()
    assert svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local) is None

    conn.execute("UPDATE agent_reachout SET state = 'unread' WHERE role_id = 'active'")
    conn.commit()
    reason = svc.blocked_why(_role(), _settings(), conn, now_utc=utc, now_local=local)
    assert reason is not None and "退避" in reason, "未读 1 条时 100 分钟还不够（要等 ~120 分钟）"


# --------------------------------------------------------------- 第五个由头：未收尾话题


class _TwoFaceModel:
    """一个模型两用：扫描那一问回 `OPEN …`，生成那一问回一句人话。

    为什么得分脸：扫描与开口都走 `model_resolver` 给的同一个模型。一份答复通吃两种问法，
    测出来的就是"扫描结果污染了正文"或反之，接线的对错根本读不出来。
    """

    def __init__(self, topics: list[str], reply: str = "嗨，在忙什么？") -> None:
        self.topics = topics
        self.reply = reply
        self.scan_calls = 0
        self.gen_calls = 0
        self.last_gen_prompt: list | None = None

    def invoke(self, prompt: object, **kwargs: object) -> AIMessage:
        text = prompt if isinstance(prompt, str) else " ".join(
            str(getattr(m, "content", "")) for m in prompt  # type: ignore[union-attr]
        )
        if "OPEN <" in text:  # 只有扫描那份指令里有这个格式示例
            self.scan_calls += 1
            body = "\n".join(f"OPEN {t}" for t in self.topics) if self.topics else "NONE"
            return AIMessage(content=body)
        self.gen_calls += 1
        self.last_gen_prompt = prompt
        return AIMessage(content=self.reply)


def _thread_scheduler(conn, model: object, lines: str) -> ReachoutScheduler:
    return ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles([_role(reachout_enabled=True)]),  # type: ignore[arg-type]
        model_resolver=lambda _n: model,
        conn=conn,
        tracer=_Tracer(),
        thread_lines=lambda _rid: lines,
    )


def _joined(prompt: list | None) -> str:
    return " ".join(str(getattr(m, "content", "")) for m in prompt or [])


def test_open_threads_replaces_the_generic_timer_and_are_cached(conn) -> None:
    """扫到东西 ⇒ 由头从"timer"升级成"open_thread"，而那几件原样进生成指令。"""
    model = _TwoFaceModel(["下周体检的结果"])
    utc, local = _now()
    scheduler = _thread_scheduler(conn, model, "- 用户：我下周要体检")
    made = scheduler.tick_once(now_utc=utc, now_local=local)
    assert made == 1
    assert model.scan_calls == 1 and model.gen_calls == 1
    assert "下周体检的结果" in _joined(model.last_gen_prompt)
    state = get_state(conn, "active")
    assert "下周体检的结果" not in state.open_threads, "用过就该清掉（见下面那条消费用例）"
    assert state.open_threads_scan_at is not None, "扫过要记时刻，否则下一 tick 又问一遍"


def test_used_topics_are_consumed_so_they_drive_only_one_open(conn) -> None:
    """R26-11 第二条：这批话题被这句话用掉了就要清空。

    缓存寿命 90 分钟 > 开口基线间隔 60 分钟，而原先没有任何清除路径 ⇒ 同一条"下周体检"
    可以在两次开口里各冒一次，用户读到的就是"她为同一件事找了我两次"。清空但**保留时刻**：
    既不再重复冒，也不会下一秒又花一次模型调用。
    """
    from rolecard_agent.core.proactive_state import save_open_threads

    utc, local = _now()
    save_open_threads(conn, "active", ["下周体检的结果"], now=utc - timedelta(minutes=5))
    model = _TwoFaceModel(["这条不该被再扫一遍"], reply="体检怎么样了？")
    scheduler = ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles([_role(reachout_enabled=True)]),  # type: ignore[arg-type]
        model_resolver=lambda _n: model,
        conn=conn,
        tracer=_Tracer(),
        thread_lines=lambda _rid: "- 用户：我下周要体检",
    )
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    assert "下周体检的结果" in _joined(model.last_gen_prompt)
    state = get_state(conn, "active")
    assert state.open_threads == (), state.open_threads
    assert state.open_threads_scan_at is not None
    assert model.scan_calls == 0, "缓存没过期就不该再问模型"


def test_a_failed_scan_does_not_shut_the_source_down_for_90_minutes(conn) -> None:
    """R26-11 第一条：扫描抛一下就等于把这一源关掉一个缓存周期，那是把抖动当成答案。

    这条同时暴露了原先的一处死代码：`find_open_threads` 自己把异常吞成 `[]`，所以调度器
    那个 `try/except` 永远进不去，而"失败"与"没扫到"在库里长得一模一样。现在两者分开
    （`None` vs `[]`），失败**不写扫描时刻** ⇒ 下一个 tick 还会重试；这一轮照常开口
    （只少一个由头），异常也不往上抛给调度线程。
    """
    class _BoomModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, prompt: object, **kwargs: object) -> AIMessage:
            self.calls += 1
            if "OPEN <" in str(prompt):
                raise RuntimeError("connection reset")
            return AIMessage(content="嗨，在忙什么？")

    model = _BoomModel()
    utc, local = _now()
    scheduler = _thread_scheduler(conn, model, "- 用户：我下周要体检")  # type: ignore[arg-type]
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1  # 照常开口
    state = get_state(conn, "active")
    assert state.open_threads_scan_at is None, "失败也写时刻 = 一次抖动关掉这一源 90 分钟"
    assert state.open_threads == ()
    assert model.calls == 2, "一次扫描 + 一次开口正文"

    conn.execute("DELETE FROM agent_reachout")  # 解掉间隔档，模拟"下一个 tick 到了"
    scheduler.tick_once(now_utc=utc, now_local=local)
    assert model.calls >= 4, "下一个 tick 必须重试，而不是把『没扫到』当成结论"


def test_a_fresh_cache_is_used_without_spending_another_call(conn) -> None:
    """这一源的全部代价在那一次模型调用上：缓存没过期就**一个字节都不该问**。"""
    from rolecard_agent.core.proactive_state import save_open_threads

    utc, local = _now()
    save_open_threads(conn, "active", ["猫绝育约上了没"], now=utc - timedelta(minutes=5))
    model = _TwoFaceModel(["这条不该被扫出来"])
    made = _thread_scheduler(conn, model, "- 用户：随便说点什么").tick_once(
        now_utc=utc, now_local=local
    )
    assert made == 1
    assert model.scan_calls == 0, "90 分钟窗口内重复扫描 = 每个 tick 多花一次调用"
    assert "猫绝育约上了没" in _joined(model.last_gen_prompt)
    assert "这条不该被扫出来" not in _joined(model.last_gen_prompt)


def test_an_expired_cache_is_rescanned(conn) -> None:
    from rolecard_agent.core.proactive_state import save_open_threads

    utc, local = _now()
    stale_at = utc - timedelta(minutes=svc.OPEN_THREADS_REFRESH_MINUTES + 1)
    save_open_threads(conn, "active", ["旧话题"], now=stale_at)
    model = _TwoFaceModel(["新扫出来的话题"])
    _thread_scheduler(conn, model, "- 用户：最近事挺多").tick_once(now_utc=utc, now_local=local)
    assert model.scan_calls == 1
    # 扫出来那句被这一轮用掉了 ⇒ 消费掉（见上面那条 R26-11 第二条的用例）。
    # 这里要验的是"过期会重扫"，所以看提示词里有没有，而不是看缓存里还剩什么。
    assert "新扫出来的话题" in _joined(model.last_gen_prompt)
    assert get_state(conn, "active").open_threads == ()


def test_nothing_open_still_speaks_but_says_nothing_about_topics(conn) -> None:
    """扫不出东西是**常态**（设计稿：宁可漏报不要凑数）：照常开口，只是不提任何"没说完的事"，
    并且把"扫过、没有"写进缓存 —— 不然下一个 tick 又去问一遍。"""
    model = _TwoFaceModel([])
    utc, local = _now()
    scheduler = _thread_scheduler(conn, model, "- 用户：今天还行")
    made = scheduler.tick_once(now_utc=utc, now_local=local)
    assert made == 1
    assert model.scan_calls == 1
    joined = _joined(model.last_gen_prompt)
    assert "未收尾" not in joined and "OPEN" not in joined
    state = get_state(conn, "active")
    assert state.open_threads == () and state.open_threads_scan_at is not None


def test_no_thread_lines_means_no_scan_at_all(conn) -> None:
    """宿主没给"你们最近聊过什么"（离线、图还没建）⇒ 不花这一次调用，也不报错。"""
    model = _TwoFaceModel(["不该被扫"])
    utc, local = _now()
    scheduler = ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles([_role(reachout_enabled=True)]),  # type: ignore[arg-type]
        model_resolver=lambda _n: model,
        conn=conn,
        tracer=_Tracer(),
    )
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    assert model.scan_calls == 0


def test_the_scan_reads_the_window_even_when_the_unanswered_tail_is_empty(conn) -> None:
    """第五由头的素材是"最近一窗"，不是"没接住那截"（09-26 轮 R26-03）。

    生产上后者在她每次开口之后必然为空 —— 那不是"这次没话可说"，而是**这一源从没启动过**
    （真库 `open_threads_at` 至今为 NULL）。所以这一条要的是：开口素材为空的同时，扫描照发。
    """
    model = _TwoFaceModel(["下周体检的结果"])
    utc, local = _now()
    scheduler = ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles([_role(reachout_enabled=True)]),  # type: ignore[arg-type]
        model_resolver=lambda _n: model,
        conn=conn,
        tracer=_Tracer(),
        thread_lines=lambda _rid: "",  # 她已经接过话：开口素材为空
        thread_window=lambda _rid: "- 用户：我下周要体检，结果出来跟你说\n- 你：好，我等你说",
    )
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    assert model.scan_calls == 1, "窗口非空却没扫 = 这一源仍然被「没接住」那把锁锁着"
    assert "下周体检的结果" in _joined(model.last_gen_prompt)


def test_affection_toggle_frees_the_rest_of_the_chain(conn) -> None:
    """affinity 触顶之后，只有关掉这一档，排在它后面的由头才轮得到（R26-23）。

    这条同时是"第五由头在生产上可达"的正证：四档全关 ⇒ `fired` 落到链尾的 timer ⇒ 扫描发生。
    """
    from rolecard_agent.core.proactive_state import save_state

    utc, local = _now()
    state = get_state(conn, "active")
    state.affinity = DEFAULT_AFFINITY_THRESHOLD + 4.0
    state.last_interaction_utc = utc
    save_state(conn, state)

    on = _role()
    off = RoleCard(**{**_role().model_dump(), "affinity_enabled": False})
    assert svc.trigger_affection(on, state, _settings(), now_utc=utc) == "affection"
    assert svc.trigger_affection(off, state, _settings(), now_utc=utc) is None

    quiet = RoleCard(
        **{
            **off.model_dump(),
            "recall_enabled": False,
            "time_pattern_enabled": False,
            "file_watch_enabled": False,
        }
    )
    model = _TwoFaceModel(["猫绝育约上了没"])
    scheduler = ReachoutScheduler(
        settings_provider=lambda: _settings(),
        roles=_Roles([quiet]),  # type: ignore[arg-type]
        model_resolver=lambda _n: model,
        conn=conn,
        tracer=_Tracer(),
        thread_lines=lambda _rid: "",
        thread_window=lambda _rid: "- 用户：下个月要给猫做绝育",
    )
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 1
    assert model.scan_calls == 1, "其余几档关掉之后，链尾那一档必须真的轮得到"


def test_recall_cools_down_after_it_actually_spoke(conn) -> None:
    """回忆档一天最多用一次：它的判据（有没有 active 记忆）只会越来越真，不冷却就永久命中。"""
    from rolecard_agent.core.proactive_state import record_recall_open

    add_item(conn, bucket="active", text="用户下周要体检")
    local = _now_local()
    assert svc.trigger_recall(_role(), conn, now_local=local) == "recall"

    record_recall_open(conn, "active", now=local)
    assert svc.trigger_recall(_role(), conn, now_local=local) is None, "刚以回忆开过口，该让位"
    later = local + timedelta(hours=svc.RECALL_COOLDOWN_HOURS + 1)
    assert svc.trigger_recall(_role(), conn, now_local=later) == "recall"


def test_an_unsent_recall_does_not_burn_the_cooldown(conn) -> None:
    """模型没产出正文（`draft.text is None`）不该消耗回忆额度 —— 用户看到的是她没说话，
    而不是"她今天已经回忆过一次了"。冷却的锚点必须只跟着**真的发出去**那一句走。"""
    model = _FakeModel(AIMessage(content=""))
    utc, local = _now()
    add_item(conn, bucket="active", text="用户下周要体检")
    scheduler = _scheduler(conn, [_role()], model)
    assert scheduler.tick_once(now_utc=utc, now_local=local) == 0
    assert get_state(conn, "active").recall_at is None, "没发出去却记了冷却 = 静默关掉这一档一天"


def test_the_open_thread_task_offers_a_way_out() -> None:
    """指令里必须留着"这些都过去了就别硬接"那条退路 —— 硬找话头比不找更假。"""
    text = svc._task_text(
        "open_thread", "", has_memory=True, open_topics=["体检结果", "猫绝育"]
    )
    assert "体检结果" in text and "猫绝育" in text
    assert "都已经过去" in text and "别硬接" in text


def test_open_threads_stale_only_by_age_or_never_scanned() -> None:
    """"从没扫过"与"扫了但没结果"必须分得开：后者在过期之前不该再花一次调用。"""
    from rolecard_agent.core.proactive_state import ProactiveState

    now = datetime.now(_UTC)
    assert svc.open_threads_stale(ProactiveState(role_id="x"), now=now) is True
    scanned_empty = ProactiveState(role_id="x", open_threads_scan_at=now - timedelta(minutes=10))
    assert svc.open_threads_stale(scanned_empty, now=now) is False
    old = ProactiveState(
        role_id="x",
        open_threads_scan_at=now - timedelta(minutes=svc.OPEN_THREADS_REFRESH_MINUTES),
    )
    assert svc.open_threads_stale(old, now=now) is True
