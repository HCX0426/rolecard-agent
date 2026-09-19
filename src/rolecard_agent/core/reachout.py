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
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from rolecard_agent.config import Settings
from rolecard_agent.core.file_watch import (
    FileEvent,
    advance_baseline,
    check_changes,
)
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
from rolecard_agent.core.text import text_of
from rolecard_agent.core.workspace import resolve_task_dir
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
# 文件事件素材清单的最大行数（再多只报总数）。
_FILE_EVENT_MAX_LINES = 10

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

# 文件事件触发（架构计划 C·§5.2）的口吻：目录变化是素材，严禁编造未见过的内容。
_REACHOUT_TASK_FILE_EVENT = (
    "现在是主动开口的时刻。你注意到用户的任务目录最近有了变化（清单见下）。以你的"
    "角色口吻自然地就此跟用户说一句——可以是好奇、关心或点评，但**不得编造文件内容**"
    "（你只看到文件名）。简短、口语化；不要自我介绍、不要说教、不要长篇。"
)


def _format_change_list(events: list[FileEvent] | None, *, truncated: bool = False) -> str:
    """变更清单 → prompt 素材行（前 10 条，只给名字不给内容）。"""
    if not events:
        return ""
    verb = {"add": "新增", "mod": "修改", "del": "删除"}
    lines = [f"- {verb.get(e['op'], e['op'])}：{e['path']}" for e in events[:_FILE_EVENT_MAX_LINES]]
    if len(events) > _FILE_EVENT_MAX_LINES:
        lines.append(f"- …等共 {len(events)} 项变化")
    if truncated:
        lines.append("- （目录较大，以上仅为部分快照）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 数据


def list_reachouts(
    conn: SqlConnection,
    limit: int = 100,
    *,
    role_id: str | None = None,
    file_watch_pending: int = 0,
) -> dict[str, object]:
    """收件箱：未读 + 最近历史（含未读数，供铃铛红点）。

    `role_id` 给定时只返回该角色主动找过你的历史（架构计划 §5.3：按角色卡隔离查看）。
    `file_watch_pending` = 当前挂起的目录变更条数（0 = 无事件或功能关闭）。
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
        "file_watch_pending": file_watch_pending,
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
    file_event: bool = False,
) -> str | None:
    """决定"这个角色此刻能不能主动开口"。None = 可以；否则返回阻塞原因。

    （全局开关与角色开关由调用方先过滤，这里只负责抑制层判定 —— 两层授权在主流程做。）
    `file_event=True`（任务目录有变化、该角色可被触发）时**豁免间隔档一次**——素材门控
    语义：变化值得即时播报；静默时段与未读堆积是用户级护栏，不豁免。
    """
    last = _last_reachout_utc(conn, role.role_id)
    if last is not None and not file_event:
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
    file_list: str = "",
) -> str | None:
    """生成一条主动内容：人设 + 记忆 → 单轮 → guard。被拦/失败返回 None（不发）。

    `role_id` 给定时按角色取**专属记忆**（回忆触发 / per-role 隔离）；若该角色无专属记忆，
    回退到用户级全局记忆（用户事实，非角色对话，不造成跨角色串扰）。
    `mode="file_event"` 时 `file_list` 为目录变更素材清单（只含文件名，细节由角色
    自行用 fs 工具查证 —— 素材门控语义，见架构计划 §5.2）。
    """
    memory = ""
    if role_id is not None and settings.memory_enabled:
        memory = load_role_memory_text(conn, role_id) or load_memory_text(conn)
    elif settings.memory_enabled:
        memory = load_memory_text(conn)
    if len(memory) > MAX_MEMORY_CHARS:
        memory = memory[:MAX_MEMORY_CHARS]
    if mode == "recall":
        task = _REACHOUT_TASK_RECALL
    elif mode == "file_event":
        task = f"{_REACHOUT_TASK_FILE_EVENT}\n\n任务目录的变化：\n{file_list}"
    else:
        task = _REACHOUT_TASK
    system = build_system_prompt(role.system_prompt, role.exemplars, memory=memory, agent=False)
    prompt = [
        SystemMessage(content=system),
        HumanMessage(content=f"{task}\n\n（你的角色是 {role.role_name}）"),
    ]
    reply = model.invoke(prompt)
    # 用全项目唯一的取值实现：`str(reply.content)` 在分块形态下会得到 Python repr，
    # 而这份文本既进 guard 又进用户收件箱（架构审计报告 P1-8）。
    text = text_of(reply).strip()
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


def trigger_time_pattern(role: RoleCard, conn: SqlConnection, *, now_local: datetime) -> str | None:
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
        thread = threading.Thread(target=self._loop, name="reachout-scheduler", daemon=True)
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
        # 文件事件（架构计划 C·§5.2 素材门控）：全局侦测一次，任何合格角色共享同一事件；
        # 谁都没开口且未过期 → 事件挂起到下一 tick（基线不推进，变化不会被吞掉）。
        file_events: list[FileEvent] | None = None
        file_truncated = False
        file_expired = False
        if settings.file_watch_enabled:
            try:
                detected = check_changes(
                    self._conn, resolve_task_dir(settings, self._conn), now_utc=stamp_utc
                )
            except OSError:
                detected = None  # 目录暂时不可达：静默，下一轮重试
            if detected is not None:
                file_events, file_truncated, file_expired = detected
        file_event_consumed = False
        try:
            candidates = [r for r in self._roles.list_roles() if r.reachout_enabled]
        except Exception:  # noqa: BLE001 - 读角色失败不退整个调度
            return 0
        for role in candidates:
            can_file = file_events is not None and role.file_watch_enabled
            if blocked_why(
                role,
                settings,
                self._conn,
                now_utc=stamp_utc,
                now_local=stamp_local,
                file_event=can_file,
            ):
                continue
            # 关系驱动：按角色状态评估触发源，取第一个命中者决定"以什么口吻开口"。
            # file_event 居链首（素材门控：有变化先说变化）；"timer" 是基线触发。
            state = get_state(self._conn, role.role_id)
            fired = (
                ("file_event" if can_file else None)
                or trigger_affection(role, state, settings, now_utc=stamp_utc)
                or trigger_time_pattern(role, self._conn, now_local=stamp_local)
                or trigger_recall(role, self._conn, now_local=stamp_local)
                or "timer"
            )
            mode = fired if fired in ("recall", "file_event") else "general"
            try:
                model = self._model(role.model_name)
                text = generate_reachout_text(
                    role,
                    model,
                    settings,
                    self._conn,
                    role_id=role.role_id,
                    mode=mode,
                    file_list=(
                        _format_change_list(file_events, truncated=file_truncated)
                        if can_file
                        else ""
                    ),
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
            file_event_consumed = file_event_consumed or fired == "file_event"
        if file_events is not None and (file_event_consumed or file_expired):
            with suppress(OSError):  # 推进失败：事件仍在，下一 tick 重试
                advance_baseline(
                    self._conn, resolve_task_dir(settings, self._conn), now_utc=stamp_utc
                )
            self._tracer.emit(
                TraceEvent(
                    event="file_watch_advance",
                    node="reachout",
                    detail={"changed": len(file_events), "expired": file_expired},
                )
            )
        return made


__all__ = ["ReachoutScheduler"]
