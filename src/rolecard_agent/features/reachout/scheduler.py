"""调度：后台 daemon 线程与每 tick 的流水线（`R102-59` 从 `core/reachout.py` 拆出的四模块之一）。

本模块只接线不决策：现取全局总闸 → 补投欠账（inbox）→ 逐角色过静默闸（quiet）→ 评估触发源、
按需补扫未收尾话题（triggers）→ 生成 → 落收件箱并尽力投进主动会话（inbox）→ 文件事件基线
推进。由 api/main.py 的 lifespan 启停。

纯搬层，行为与拆分前逐字一致。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from rolecard_agent.base.identity import resolve_instance_identity
from rolecard_agent.base.observability import TraceEvent, Tracer
from rolecard_agent.config import Settings
from rolecard_agent.core.file_watch import (
    FileEvent,
    advance_baseline,
    check_changes,
)
from rolecard_agent.core.open_threads import find_open_threads
from rolecard_agent.core.proactive_state import (
    get_state,
    record_interaction,
    record_recall_open,
    save_open_threads,
)
from rolecard_agent.core.workspace import resolve_task_dir
from rolecard_agent.features.reachout.inbox import (
    UNDELIVERED_RETRY_LIMIT,
    UNDELIVERED_RETRY_MINUTES,
    mark_delivered,
    record_reachout,
    undelivered_reachouts,
)
from rolecard_agent.features.reachout.quiet import quiet_gate
from rolecard_agent.features.reachout.triggers import (
    _format_change_list,
    generate_reachout_text,
    open_threads_stale,
    trigger_affection,
    trigger_recall,
    trigger_time_pattern,
)
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.roles.service import RoleCardService, RoleNotFound
from rolecard_agent.storage.db import SqlConnection

# 后台轮询间隔（秒）：30s 一查足够（真正开口还受间隔/时段抑制）。
TICK_SECONDS = 30


# --------------------------------------------------------------------------- 调度线程


class ReachoutScheduler:
    """后台 daemon：每 tick 检查启用主动的角色，符合条件就生成并落收件箱。

    `settings_provider` 每次 tick 现取（全局总闸热切即时生效）；角色资格实时读库
    （角色卡开关改下一 tick 生效）。`model_resolver` 由宿主提供（角色可按 model_name
    路由模型，同对话路径）。

    `deliver` 是"把这句话也落进该角色的主动会话"的宿主实现（需要图与检查点，本模块不
    持有）：省略 = 只进收件箱（离线单测与无图环境就走这条）。投递失败**不影响收件箱**
    —— 消息已经在用户能看见的地方了，只是暂时点不进会话，这一点如实进 tracer。
    """

    def __init__(
        self,
        *,
        settings_provider: Callable[[], Settings],
        roles: RoleCardService,
        model_resolver: Callable[[str | None], Any],
        conn: SqlConnection,
        tracer: Tracer,
        deliver: Callable[[RoleCard, str], str | None] | None = None,
        thread_lines: Callable[[str], str] | None = None,
        thread_window: Callable[[str], str] | None = None,
    ) -> None:
        self._settings = settings_provider
        self._roles = roles
        self._model = model_resolver
        self._conn = conn
        self._tracer = tracer
        self._deliver = deliver
        # "你们最近聊过什么"的取法由宿主给（它才知道图与检查点在哪）：None = 不带这段上下文。
        self._thread_lines = thread_lines
        # 第五由头扫描用的是**最近一窗**而不是"没接住那截"（R26-03）。宿主没给独立取法时
        # 退回 `thread_lines`：宁可这一源退化成"照样扫不到东西"，也不要新签名逼所有宿主改。
        self._thread_window = thread_window or thread_lines
        # 每个角色"上一次看到的静默原因"（"" = 可开口）。只是用来判"原因变了没有"，
        # 进程重启就清零 —— 重启后第一次 tick 重新报一句当前状态，那是对的，不是丢消息。
        self._quiet: dict[str, str] = {}
        self._stop = threading.Event()

    # -- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="reachout-scheduler", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self, *, join_timeout: float = 35.0) -> None:
        """停调度：置旗后**等线程真的退出**（2026-10-04 审查快照的停机竞态条目）。

        从前只 `set()` 不 join：一次 tick 内含逐角色的真模型调用（本地 8B 每次 10~120s）
        与 `graph.update_state` 投递，而 `Runtime.shutdown` 在 stop 之后马上就做
        WAL checkpoint 与 conn.close() —— 优雅退出路径（POSIX SIGTERM / 开发态 Ctrl+C）
        上调度线程可能正持另一线程连接写库，撞出 busy 噪音甚至 close 后写库异常。
        join 超时给足一次 tick（TICK_SECONDS=30）+ 余量；超时后仍强收（与旧行为同：
        daemon 线程随进程走），只是至少给了它一次干净收场的机会。

        tick 内的协作式取消配合这里：每次**模型调用前**查旗，正在生成的那个角色这一轮
        主动放弃 —— 等它生成完（最长 120s）再 join 会把退出拖到不可接受。
        """
        self._stop.set()
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001 - 调度循环绝不能被一个错误打死
                # 打死不等于闭嘴：以前这个事件连异常文本都不带（只有 `fatal: False`），
                # 于是"角色找你了你却点不开"这类故障在日志里查不到任何线索。
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_tick_error",
                        node="reachout",
                        detail={
                            "fatal": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )
            if self._stop.is_set():
                break  # stop() 置旗后不再开下一轮 tick：join 才等得到头

    def _stopping(self) -> bool:
        """tick 内部的协作式取消点：True = 该收手了（正在生成的这轮主动放弃）。"""
        return self._stop.is_set()

    # -- 主流程（可注入 now 用于测试） -------------------------------------

    def _retry_undelivered(self, owner: str) -> int:
        """把"只进了收件箱、没落进会话"的那几条补上，返回补上的条数（`R26-40` ②）。

        为什么需要它：`deliver_proactive` 在"那条会话正在对话中"时拿不到写锁就放弃 ——
        刻意不去打断用户那一轮（一轮可能跑几十秒到几分钟，本地模型更久），而收件箱那一行
        **已经先落了**。没有这一步，那句话就永久停在收件箱里：气泡里有它、点进会话却没有。
        补投本身也会撞上"又在忙"，那就留在原地等下一次 tick —— 不需要重试计数，
        `undelivered_reachouts` 的时间窗会替我们收口（过期的那些自己就不再被读到）。
        """
        if self._deliver is None:
            return 0
        try:
            rows = undelivered_reachouts(
                self._conn,
                user_id=owner,
                within_minutes=UNDELIVERED_RETRY_MINUTES,
                limit=UNDELIVERED_RETRY_LIMIT,
            )
        except (sqlite3.Error, OSError):
            return 0
        fixed = 0
        for row in rows:
            try:
                role = self._roles.scoped(owner).get(str(row["role_id"]))
            except RoleNotFound:
                continue  # 卡被删了：这句话留在收件箱就好，没什么可投的
            try:
                thread_id = self._deliver(role, str(row["text"]))
            except Exception as exc:  # noqa: BLE001 - 一个角色投不进去不该拖住别的角色
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_deliver_failed",
                        node="reachout",
                        role_id=str(row["role_id"]),
                        detail={"error": f"{type(exc).__name__}: {exc}", "late": True},
                    )
                )
                continue
            if thread_id is None:
                continue  # 还在忙：下一次 tick 再看
            mark_delivered(self._conn, int(row["id"]))
            fixed += 1
            # 补投成功要留痕：它是"她说过的话晚了多久才落地"的唯一证据，
            # 而"收件箱里有、会话里没有"这种观感只能靠这条读数解释。
            self._tracer.emit(
                TraceEvent(
                    event="reachout_delivered_late",
                    node="reachout",
                    thread_id=thread_id,
                    role_id=str(row["role_id"]),
                    detail={"reachout_id": int(row["id"])},
                )
            )
        return fixed

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
        # 文件事件（架构总览 §5 素材门控）：全局侦测一次，任何合格角色共享同一事件；
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
            # 后台这条链没有"这次请求"可问：它替**这台实例的主人**挑人开口（§4.1 的实例级身份）。
            owner = resolve_instance_identity(settings)
            # 先把上一 tick 欠下的补上（R26-40 ②）：顺序上"她之前说过的那句"该排在"她新要
            # 说的这句"前面，否则补投只会永远让位给新开口。
            self._retry_undelivered(owner)
            candidates = [
                r for r in self._roles.scoped(owner).list_roles() if r.reachout_enabled
            ]
        except (sqlite3.Error, OSError):
            # 读角色时库/盘临时坏了：这一轮不开口，下一轮重试，**不退整个调度**。
            # 刻意不再 `except Exception`：桩少了个方法、参数写错这类接线错曾经被这里
            # 吞成"candidates 为空"，症状只是"她再也不主动说话了"，30 条用例一起哑掉也没人红。
            return 0
        for role in candidates:
            if self._stopping():
                return made  # 停机：没评估完的角色等下一个进程周期
            can_file = file_events is not None and role.file_watch_enabled
            gate = quiet_gate(
                role,
                settings,
                self._conn,
                now_utc=stamp_utc,
                now_local=stamp_local,
                file_event=can_file,
            )
            why = gate.why
            # **静默要有出口**（`S-8`）：这句原因以前被 `if …: continue` 直接丢掉，于是
            # "她最近怎么不找我了"在日志里查不到任何线索。只在**原因变了的那一跳**留痕 ——
            # 每 tick 一条会把轨迹刷满（30s × 角色数），而同一句话重复一百遍不带新信息。
            if self._quiet.get(role.role_id, "") != (gate.why or ""):
                self._quiet[role.role_id] = gate.why or ""
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_quiet" if gate.why else "reachout_ready",
                        node="reachout",
                        role_id=role.role_id,
                        # 徽章那个数一起进事件：界面上写着"连着 1 条没回"而日志里查不到，
                        # 就是 `R26-35` 那一族"屏幕说不一致"。
                        detail={
                            "why": gate.why or "现在随时能开口",
                            "streak": gate.streak,
                            "unread": gate.unread,
                        },
                    )
                )
            if why:
                continue
            # 关系驱动：按角色状态评估触发源，取第一个命中者决定"以什么口吻开口"。
            # file_event 居链首（素材门控：有变化先说变化）；"timer" 是基线触发。
            state = get_state(self._conn, role.role_id, user_id=owner)
            fired = (
                ("file_event" if can_file else None)
                or trigger_affection(role, state, settings, now_utc=stamp_utc)
                or trigger_time_pattern(
                    role, self._conn, user_id=owner, now_local=stamp_local
                )
                or trigger_recall(role, self._conn, user_id=owner, now_local=stamp_local)
                or "timer"
            )
            # 第五个由头「未收尾话题」：只在链子要落到最弱那一档（timer）时才去补一次扫描，
            # 结果按 `OPEN_THREADS_REFRESH_MINUTES` 缓存 —— 一次调用换"她记得你说到一半"，
            # 不能每个 tick 花一遍（本地 8B 一次几十秒，那是直接拖死调度线程的量）。
            # 缓存**先接住**再谈扫描：扫描要花一次模型调用，所以它仍只挂在最弱那一档；
            # 但"读上一轮扫出来的结果"不该跟着一起挂 —— 否则 affection 开口时素材被丢掉。
            open_topics: list[str] = list(state.open_threads)
            if fired == "timer" and self._thread_window is not None:
                # 取"最近一窗"而不是"她还没接住的那一截"：后者在她每次开口之后必然为空，
                # 于是这一源在生产上从没被走到过（09-26 轮 R26-03，实测见
                # scripts/probe_open_threads_reach.py）。
                turns = self._thread_window(role.role_id)
                if turns:
                    if open_threads_stale(state, now=stamp_utc):
                        scanned = find_open_threads(turns, self._model(role.model_name))
                        if scanned is None:
                            # 调用失败：**不写扫描时刻**，下一个 tick 重试。写了就等于一次抖动
                            # 把这一源关掉 `OPEN_THREADS_REFRESH_MINUTES`（90 分 > 60 分的开口
                            # 基线），而下个 tick 根本不会再试（R26-11 第一条）。
                            # `find_open_threads` 不抛只回 None，所以这里没有 try/except ——
                            # 原先那一段包着一个永远进不去的 except 分支（死代码，同族见 R26-13）。
                            self._tracer.emit(
                                TraceEvent(
                                    event="open_threads_failed",
                                    node="reachout",
                                    role_id=role.role_id,
                                    detail={"error": "模型调用未成功，本轮不记扫描时刻"},
                                )
                            )
                        else:
                            state = save_open_threads(
                                self._conn, role.role_id, scanned, user_id=owner, now=stamp_utc
                            )
                    open_topics = list(state.open_threads)
                    if open_topics:
                        fired = "open_thread"
            mode = fired if fired in ("recall", "file_event", "open_thread") else "general"
            if self._stopping():
                return made  # 停机：正在生成的这轮主动放弃（join 才等得到头）
            try:
                model = self._model(role.model_name)
                draft = generate_reachout_text(
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
                    # 读不到就是没有这段上下文（provider 自己吞异常），不该拦住开口。
                    thread_lines=self._thread_lines(role.role_id) if self._thread_lines else "",
                    open_topics=open_topics,
                    tracer=self._tracer,
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
            if draft.text is None:
                # 不发，也不重试 —— 但要留痕：空输出与 guard 拦下是两件不同的事
                # （前者要去修模型配置，后者是护栏在正常工作）。
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_skipped",
                        node="reachout",
                        role_id=role.role_id,
                        detail={"why": draft.why, "trigger": fired, "score": draft.score},
                    )
                )
                continue
            text = draft.text
            reachout_id = record_reachout(
                self._conn, role, text, user_id=owner, fired_by=fired,
                repeat_score=draft.score,
            )
            record_interaction(self._conn, role.role_id, user_id=owner, now=stamp_utc)
            if fired == "recall":
                # 冷却锚点只在**真的发出去了**的时候记：被 guard 拦下、正文为空的那些
                # `reachout_skipped` 不该消耗掉这一档的额度（用户看到的是"她没说话"，
                # 而不是"她说过一次了"）。
                record_recall_open(self._conn, role.role_id, user_id=owner, now=stamp_utc)
            elif fired == "open_thread":
                # 这批话题已经被刚发出去的那句用掉了。不清的话缓存寿命（90 分）比开口
                # 间隔（60 分）长，同一个话题会驱动两次开口（R26-11 第二条）。
                # **时刻保留**：清空 + 留着 scan_at 才是想要的语义 —— 别立刻再花一次调用，
                # 也别让同一个话题再冒一遍。
                save_open_threads(self._conn, role.role_id, [], user_id=owner, now=stamp_utc)
            # 先落收件箱（用户一定能看见），再尽力投进主动会话；投递坏了也不把消息吞掉。
            # 投不进去的那些**不是丢了**：`delivered_at` 仍为空，下一 tick 由
            # `_retry_undelivered` 补上（R26-40 ②）—— 从前那句"调度器下一轮还会再问"是错的，
            # 下一轮是**重新生成一句新话**，上一句就此只在收件箱里。
            thread_id: str | None = None
            if self._deliver is not None:
                try:
                    thread_id = self._deliver(role, text)
                except Exception as exc:  # noqa: BLE001 - 投递失败只留痕，不回滚收件箱
                    self._tracer.emit(
                        TraceEvent(
                            event="reachout_deliver_failed",
                            node="reachout",
                            role_id=role.role_id,
                            detail={"error": f"{type(exc).__name__}: {exc}"},
                        )
                    )
                if thread_id is not None:
                    mark_delivered(self._conn, reachout_id)
            self._tracer.emit(
                TraceEvent(
                    event="reachout_sent",
                    node="reachout",
                    role_id=role.role_id,
                    detail={
                        "chars": len(text),
                        "trigger": fired,
                        "thread_id": thread_id,
                        # 复读分带进审计：闸门会不会误伤只能看分布，而分布要在真机上一天天攒。
                        "score": draft.score,
                        # 一句主动开口花了多少 token（重生过就是两次）。`chars` 是字数，不是钱。
                        "tokens": draft.tokens,
                    },
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