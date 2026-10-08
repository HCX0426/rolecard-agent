"""角色主动开口（架构计划 B）—— 角色在不由用户发消息的时刻主动来找用户。

## 两级授权（AND，缺一不可）

  * 全局总闸 `REACHOUT_ENABLED`：运行时热切（调度每 tick 读当前值，关闭下一轮即停）；
  * 角色卡 `reachout_enabled`：谁真有资格主动（默认 False = 出厂静默）。

## 抑制层（决定"值不值得/能不能开口"）

  1. 间隔：同一角色两次**冒话** ≥ `REACHOUT_INTERVAL_MINUTES` **再乘退避与抖动** —— 锚点是她
     最后一次说话（主动开口与在会话里回答都算，09-26 修的），退避看"她开口之后对方没回几句"
     （`_unreplied_streak`：只比时刻，不被"点进对话界面就算都看过"那条已读口径抹掉），并按
     (角色, 上次说话时刻) 派生 ±12% 抖动（否则"每天同一时刻"会精确成立）；
  2. 静默时段：本地时间 23:00–08:00 不主动（与间隔的 UTC 分开，注释点明口径）；
  3. 堆积上限：同一角色未读 ≤ `MAX_UNREAD_PER_ROLE`，满了不再开（防轰炸）。

  三道的判据由 `quiet_gate` **一处**算出，并一次带出全部读数：原因、下一次的时刻、退避与未读的计数。
  分成两处迟早对不上，而界面那句话的全部意义是让人信。原因有两个出口 ——
  `quiet_status()` 随收件箱那份负载给界面（`S-8`），调度器只在**原因变化的那一跳**发一条
  `reachout_quiet` / `reachout_ready` 进 tracer（每 tick 一条会把轨迹刷满，
  重复的同一句不带新信息）。

## 生成（一次单轮模型调用，所有安全纪律照旧）

  主动内容 = 角色人设 + 用户长期记忆 → 单轮生成，**输出必须过 guard**（fail-closed：
  被拦下就不发，而不是过滤后发）→ 落 `agent_reachout`（unread）**并且**落进该角色的
  "主动会话"（`proactive_thread_id`，由宿主注入的 `deliver` 写 checkpoint）。
  生成时不拖对话历史（单轮、独立），但**发出后它就是一条真消息**：用户能从收件箱点进
  会话直接回话，角色下次也记得自己主动说过什么（2026-09-19 用户报"主动找我我却回不了"）。
  两处的分工是刻意的：收件箱负责"攒着 + 红点 + 页面关着也能收"，会话负责"能继续谈"。

## 运行形态

  后台 daemon 线程按固定 tick 轮询（`ReachoutScheduler`），由**宿主**在 `api/main.py` 里
  建好并 `register_background("reachout", …)`，内核只统一启停与保证退出顺序（先 stop 再
  关连接）；单个角色的生成失败只记 tracer、不重试、不阻塞下一轮。

## 模块划分（`R102-59`：1571 行拆四模块，纯搬层）

  实现分住在四个子模块，本包 `__init__` 只做 re-export（公共 API 与拆分前逐字一致）：

  * `triggers.py` —— 触发评估与开口生成（任务指令拼装、四类触发源、去重复测）；
  * `quiet.py` —— 静默策略（`quiet_gate` / `quiet_status` 与度量助手）；
  * `inbox.py` —— 信箱投递（收件箱读写、欠投补投查询）；
  * `scheduler.py` —— 调度（`ReachoutScheduler` 与每 tick 流水线）。

  **另有两件"看起来属于本包、其实是被别人共用"的约定住在 core**（2026-10-04 审查快照
  "core 装了产品功能"那一刀的逆向解法 —— 内核与接入层都要用它们，留在功能包里就得让
  core 反向 import 一个功能）：`core/proactive/proactive_thread.py`（主动会话的 id/标题/建行）与
  `core/sessions/thread_transcript.py`（把 `(说话人, 原文)` 切成 prompt 素材的纯函数）。本包仍从
  这两处 re-export 那些名字，**旧命名空间逐字可用**（测试与脚本按 `reachout.xxx` 调用）。
"""

from __future__ import annotations

from rolecard_agent.core.proactive.proactive_state import save_open_threads
from rolecard_agent.core.proactive.proactive_thread import (
    PROACTIVE_THREAD_PREFIX,
    ensure_proactive_thread,
    proactive_thread_id,
    proactive_thread_title,
)
from rolecard_agent.core.sessions.thread_transcript import (
    CHAT_ECHO_LIMIT,
    RECENT_THREAD_LIMIT,
    RECENT_WINDOW_LIMIT,
    format_recent_window,
    format_thread_lines,
    format_unreplied_lines,
    unanswered_lines,
    unreplied_lines,
)
from rolecard_agent.features.reachout.inbox import (
    UNDELIVERED_RETRY_LIMIT,
    UNDELIVERED_RETRY_MINUTES,
    clear_all_inboxes,
    clear_inbox,
    delete_reachout,
    list_reachouts,
    mark_all_read,
    mark_delivered,
    mark_read,
    mark_role_read,
    prune_inbox,
    record_reachout,
    undelivered_reachouts,
)
from rolecard_agent.features.reachout.quiet import (
    BACKOFF_GROWTH,
    JITTER_FRACTION,
    MAX_UNREAD_PER_ROLE,
    QUIET_HOURS_END,
    QUIET_HOURS_START,
    Gate,
    _quiet_minutes,
    quiet_gate,
    quiet_status,
)
from rolecard_agent.features.reachout.scheduler import TICK_SECONDS, ReachoutScheduler
from rolecard_agent.features.reachout.triggers import (
    DROP_SCORE,
    OPEN_THREADS_REFRESH_MINUTES,
    RECALL_COOLDOWN_HOURS,
    RECENT_CONTEXT_LIMIT,
    REGEN_SCORE,
    ReachoutDraft,
    _format_change_list,
    _task_text,
    generate_reachout_text,
    open_threads_stale,
    recent_own_texts,
    recent_reachout_lines,
    trigger_affection,
    trigger_recall,
    trigger_time_pattern,
)

__all__ = [
    # 调度
    "TICK_SECONDS",
    "ReachoutScheduler",
    # 静默策略
    "BACKOFF_GROWTH",
    "JITTER_FRACTION",
    "MAX_UNREAD_PER_ROLE",
    "QUIET_HOURS_END",
    "QUIET_HOURS_START",
    "Gate",
    "quiet_gate",
    "quiet_status",
    # 触发评估与开口生成
    "CHAT_ECHO_LIMIT",
    "OPEN_THREADS_REFRESH_MINUTES",
    "RECALL_COOLDOWN_HOURS",
    "RECENT_CONTEXT_LIMIT",
    "RECENT_THREAD_LIMIT",
    "RECENT_WINDOW_LIMIT",
    "ReachoutDraft",
    "format_recent_window",
    "format_thread_lines",
    "format_unreplied_lines",
    "generate_reachout_text",
    "open_threads_stale",
    "recent_own_texts",
    "recent_reachout_lines",
    "trigger_affection",
    "trigger_recall",
    "trigger_time_pattern",
    "unanswered_lines",
    "unreplied_lines",
    # 信箱投递
    "PROACTIVE_THREAD_PREFIX",
    "UNDELIVERED_RETRY_LIMIT",
    "UNDELIVERED_RETRY_MINUTES",
    "clear_all_inboxes",
    "clear_inbox",
    "delete_reachout",
    "ensure_proactive_thread",
    "list_reachouts",
    "mark_all_read",
    "mark_delivered",
    "mark_read",
    "mark_role_read",
    "proactive_thread_id",
    "proactive_thread_title",
    "prune_inbox",
    "record_reachout",
    "undelivered_reachouts",
    # 旧命名空间兼容：搬层前它们经 `core/reachout` 模块可达（测试与脚本在用），按"公共 API
    # 不变"保留 —— 前两个是 anti_repeat 的闸门定标、`save_open_threads` 来自 proactive_state、
    # 三个下划线名是测试直接调用的助手。
    "REGEN_SCORE",
    "DROP_SCORE",
    "save_open_threads",
    "_format_change_list",
    "_quiet_minutes",
    "_task_text",
]