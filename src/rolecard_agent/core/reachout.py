"""角色主动开口（架构计划 B）—— 角色在不由用户发消息的时刻主动来找用户。

## 两级授权（AND，缺一不可）

  * 全局总闸 `REACHOUT_ENABLED`：运行时热切（调度每 tick 读当前值，关闭下一轮即停）；
  * 角色卡 `reachout_enabled`：谁真有资格主动（默认 False = 出厂静默）。

## 抑制层（决定"值不值得/能不能开口"）

  1. 间隔：同一角色两次开口 ≥ `REACHOUT_INTERVAL_MINUTES`（间隔记录取 `agent_reachout`
     的 `created_at`，UTC 口径，与 CURRENT_TIMESTAMP 一致）；
  2. 静默时段：本地时间 23:00–08:00 不主动（与间隔的 UTC 分开，注释点明口径）；
  3. 堆积上限：同一角色未读 ≤ `MAX_UNREAD_PER_ROLE`，满了不再开（防轰炸）。

## 生成（一次单轮模型调用，所有安全纪律照旧）

  主动内容 = 角色人设 + 用户长期记忆 → 单轮生成，**输出必须过 guard**（fail-closed：
  被拦下就不发，而不是过滤后发）→ 落 `agent_reachout`（unread）。
  不拖对话历史：主动开口是"另起一句话"，不需要正在进行的会话上下文（v1 口径）——
  如果将来要"基于当前任务开口"，那是 B 的迭代。

## 运行形态

  后台 daemon 线程按固定 tick 轮询（`ReachoutScheduler`），由 api/main.py 的 lifespan
  启停；单个角色的生成失败只记 tracer、不重试、不阻塞下一轮。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from rolecard_agent.config import Settings
from rolecard_agent.core.guard import check
from rolecard_agent.core.memory import (
    MAX_MEMORY_CHARS,
    load_memory_text,
    load_role_memory_text,
)
from rolecard_agent.core.observability import TraceEvent, Tracer
from rolecard_agent.core.proactive_state import (
    DEFAULT_AFFINITY_THRESHOLD,
    ProactiveState,
    get_state,
    record_interaction,
)
from rolecard_agent.core.prompts import build_system_prompt
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import SqlConnection

# 静默时段（本地时间）：23:00–08:00 不主动打扰。
QUIET_HOURS_START = 23
QUIET_HOURS_END = 8
# 同一角色未读堆积上限：满了就不再开（防角色刷屏成轰炸）。
MAX_UNREAD_PER_ROLE = 2
# 后台轮询间隔（秒）：30s 一查足够（真正开口还受间隔/时段抑制）。
TICK_SECONDS = 30

_REACHOUT_TASK = (
    "现在是主动开口的时刻。结合你的角色设定和关于用户的长期记忆，用一两句话主动向用户"
    "问候或说一件此刻值得说的小事：可以是有用的提醒、一句关心，或自然地打招呼。"
    "像真人突然想起跟对方说话那样，自然、简短、口语化；不要长篇，不要说教，不要自我介绍。"
)

# 回忆触发专用的口吻：自然提起一件记得的、之前聊过或答应的事。
_REACHOUT_TASK_RECALL = (
    "现在是主动开口的时刻。结合你的角色设定和关于用户的长期记忆，自然地提起一件"
    "你记得的、之前聊过或答应的事——像突然想起来要跟对方说。简短、口语化；"
    "不要自我介绍、不要说教、不要长篇。"
)


# --------------------------------------------------------------------------- 数据

def list_reachouts(
    conn: SqlConnection, limit: int = 100, *, role_id: str | None = None
) -> dict[str, object]:
    """收件箱：未读 + 最近历史（含未读数，供铃铛红点）。

    `role_id` 给定时只返回该角色主动找过你的历史（架构计划 §5.3：按角色卡隔离查看）。
    """
    where = "WHERE role_id = ?" if role_id else ""
    params = (role_id, limit) if role_id else (limit,)
    rows = conn.execute(
        f"SELECT id, role_id, role_name, text, state, created_at FROM agent_reachout "
        f"{where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    if role_id:
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout WHERE role_id = ? AND state = 'unread'",
            (role_id,),
        ).fetchone()
    else:
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout WHERE state = 'unread'"
        ).fetchone()
    return {
        "items": [dict(r) for r in rows],
        "unread": int(unread["n"]),
    }


def mark_read(conn: SqlConnection, reachout_id: int) -> bool:
    """标记已读；不存在返回 False（路由层转 404）。"""
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'read' WHERE id = ? AND state = 'unread'",
        (reachout_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def record_reachout(conn: SqlConnection, role: RoleCard, text: str) -> None:
    """落一条主动开口（unread）。role 冗余存角色名：角色被删后收件箱仍可读。"""
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text) VALUES (?, ?, ?)",
        (role.role_id, role.role_name, text),
    )
    conn.commit()


# ----------------------------------------------------------- 判定与生成（可测，无线程依赖）

def _last_reachout_utc(conn: SqlConnection, role_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(created_at) AS at FROM agent_reachout WHERE role_id = ?", (role_id,)
    ).fetchone()
    raw = row["at"]
    if not raw:
        return None
    return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _unread_for_role(conn: SqlConnection, role_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_reachout WHERE role_id = ? AND state = 'unread'",
        (role_id,),
    ).fetchone()
    return int(row["n"])


def blocked_why(
    role: RoleCard,
    settings: Settings,
    conn: SqlConnection,
    *,
    now_utc: datetime,
    now_local: datetime,
) -> str | None:
    """决定"这个角色此刻能不能主动开口"。None = 可以；否则返回阻塞原因。

    （全局开关与角色开关由调用方先过滤，这里只负责抑制层判定 —— 两层授权在主流程做。）
    """
    last = _last_reachout_utc(conn, role.role_id)
    if last is not None:
        elapsed = now_utc - last
        if elapsed < timedelta(minutes=settings.reachout_interval_minutes):
            return f"距上次开口不足 {settings.reachout_interval_minutes} 分钟"
    if now_local.hour >= QUIET_HOURS_START or now_local.hour < QUIET_HOURS_END:
        return "处于静默时段（23:00–08:00）"
    if _unread_for_role(conn, role.role_id) >= MAX_UNREAD_PER_ROLE:
        return "未读堆积已达上限"
    return None


def generate_reachout_text(
    role: RoleCard,
    model: Any,
    settings: Settings,
    conn: SqlConnection,
    *,
    role_id: str | None = None,
    mode: str = "general",
) -> str | None:
    """生成一条主动内容：人设 + 记忆 → 单轮 → guard。被拦/失败返回 None（不发）。

    `role_id` 给定时按角色取**专属记忆**（回忆触发 / per-role 隔离）；若该角色无专属记忆，
    回退到用户级全局记忆（用户事实，非角色对话，不造成跨角色串扰）。
    """
    memory = ""
    if role_id is not None and settings.memory_enabled:
        memory = load_role_memory_text(conn, role_id) or load_memory_text(conn)
    elif settings.memory_enabled:
        memory = load_memory_text(conn)
    if len(memory) > MAX_MEMORY_CHARS:
        memory = memory[:MAX_MEMORY_CHARS]
    task = _REACHOUT_TASK_RECALL if mode == "recall" else _REACHOUT_TASK
    system = build_system_prompt(role.system_prompt, role.exemplars, memory=memory, agent=False)
    prompt = [
        SystemMessage(content=system),
        HumanMessage(content=f"{task}\n\n（你的角色是 {role.role_name}）"),
    ]
    reply = model.invoke(prompt)
    text = str(getattr(reply, "content", "") or "").strip()
    if not text:
        return None
    verdict = check(text)
    if not verdict.allowed:
        return None  # guard fail-closed：被拦下就不发
    return text[:2000]


# 触发源（关系驱动，四类共用抑制 / 生成 / 落库流水线）

def trigger_affection(
    role: RoleCard, state: ProactiveState, settings: Settings, *, now_utc: datetime
) -> str | None:
    """性格·关系数值触发：互动积累的成长值（衰减后）到阈值即主动冒泡。"""
    # 阈值比较带极小 epsilon：affinity 恰为阈值、且衰减量仅浮点噪声时仍视为达标。
    if state.decayed_affinity(now=now_utc) >= DEFAULT_AFFINITY_THRESHOLD - 1e-6:
        return "affection"
    return None


def trigger_time_pattern(
    role: RoleCard, conn: SqlConnection, *, now_local: datetime
) -> str | None:
    """时段 / 规律 nudge：若该角色历史上主动开口的本地小时众数 == 当前小时且样本足够，触发。
    该角色关掉时段规律 = 不触发。"""
    if not role.time_pattern_enabled:
        return None
    rows = conn.execute(
        "SELECT created_at FROM agent_reachout WHERE role_id = ?", (role.role_id,)
    ).fetchall()
    if not rows:
        return None
    hours: dict[int, int] = {}
    for r in rows:
        ts = _parse_reachout_ts(r["created_at"])
        if ts is None:
            continue
        hours[ts.astimezone().hour] = hours.get(ts.astimezone().hour, 0) + 1
    if not hours:
        return None
    top_hour, top_n = max(hours.items(), key=lambda kv: kv[1])
    if top_hour == now_local.hour and top_n >= 2:
        return "time_pattern"
    return None


def trigger_recall(role: RoleCard, conn: SqlConnection, *, now_local: datetime) -> str | None:
    """回忆触发：该角色有专属记忆时，自然提起一件记得的事。无记忆 / 该角色关掉回忆 = 不触发。"""
    if not role.recall_enabled:
        return None
    if load_role_memory_text(conn, role.role_id).strip():
        return "recall"
    return None


def _parse_reachout_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


# --------------------------------------------------------------------------- 调度线程

class ReachoutScheduler:
    """后台 daemon：每 tick 检查启用主动的角色，符合条件就生成并落收件箱。

    `settings_provider` 每次 tick 现取（全局总闸热切即时生效）；角色资格实时读库
    （角色卡开关改下一 tick 生效）。`model_resolver` 由宿主提供（角色可按 model_name
    路由模型，同对话路径）。
    """

    def __init__(
        self,
        *,
        settings_provider: Callable[[], Settings],
        roles: RoleCardService,
        model_resolver: Callable[[str | None], Any],
        conn: SqlConnection,
        tracer: Tracer,
    ) -> None:
        self._settings = settings_provider
        self._roles = roles
        self._model = model_resolver
        self._conn = conn
        self._tracer = tracer
        self._stop = threading.Event()

    # -- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        thread = threading.Thread(
            target=self._loop, name="reachout-scheduler", daemon=True
        )
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001 - 调度循环绝不能被一个错误打死
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_tick_error",
                        node="reachout",
                        detail={"fatal": False},
                    )
                )

    # -- 主流程（可注入 now 用于测试） -------------------------------------

    def tick_once(
        self,
        *,
        now_utc: datetime | None = None,
        now_local: datetime | None = None,
    ) -> int:
        """跑一轮检查，返回本轮实际落库的主动消息条数（测试与统计都用它）。"""
        settings = self._settings()
        if not settings.reachout_enabled:
            return 0  # 全局总闸关闭：全部静默
        stamp_utc = now_utc or datetime.now(UTC)
        stamp_local = now_local or datetime.now().astimezone()
        made = 0
        try:
            candidates = [r for r in self._roles.list_roles() if r.reachout_enabled]
        except Exception:  # noqa: BLE001 - 读角色失败不退整个调度
            return 0
        for role in candidates:
            if blocked_why(role, settings, self._conn, now_utc=stamp_utc, now_local=stamp_local):
                continue
            # 关系驱动：按角色状态评估四类触发源，取第一个命中者决定"以什么口吻开口"。
            # "timer" 是基线触发（间隔/时段/未读由 blocked_why 把守），其余为关系驱动增量。
            state = get_state(self._conn, role.role_id)
            fired = (
                trigger_affection(role, state, settings, now_utc=stamp_utc)
                or trigger_time_pattern(role, self._conn, now_local=stamp_local)
                or trigger_recall(role, self._conn, now_local=stamp_local)
                or "timer"
            )
            mode = "recall" if fired == "recall" else "general"
            try:
                model = self._model(role.model_name)
                text = generate_reachout_text(
                    role, model, settings, self._conn, role_id=role.role_id, mode=mode
                )
            except Exception as exc:  # noqa: BLE001 - 生成失败只留痕，不阻塞其它角色
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_failed",
                        node="reachout",
                        role_id=role.role_id,
                        detail={"error": str(exc)},
                    )
                )
                continue
            if text is None:
                continue  # guard 拦下 / 空输出：不发，且不重试
            record_reachout(self._conn, role, text)
            record_interaction(self._conn, role.role_id, now=stamp_utc)
            self._tracer.emit(
                TraceEvent(
                    event="reachout_sent",
                    node="reachout",
                    role_id=role.role_id,
                    detail={"chars": len(text), "trigger": fired},
                )
            )
            made += 1
        return made


__all__ = ["ReachoutScheduler"]