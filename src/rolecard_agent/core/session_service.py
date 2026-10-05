"""会话线程的 service（Router 长成事实 service 那一轮的收口：路由只剩校验 + 异常映射 + 投送）。

三层归属（本仓一贯的那条，`sync_service` 同形）：

  * **语句**：`storage/threads.py` —— 这张表读写 SQL 的唯一 repository，seam 尺子看着；
  * **顺序与事实**：本模块 —— 线程 id 的形状、创建/改字段/删除的编排、`agent_mode` 的
    语义（显式值 vs 回落全局默认）、"角色被删了会话还在"的降级；
  * **HTTP 语义**：路由 —— 404/400/409 的映射、按 actor 写审计、响应序列化。

为什么不是一堆薄转发：今天这些操作大多单步，但**它们是 `message_count` 冗余列的落点** ——
那一列要在一个事务里由四个写入口同维护（创建会话 / 编辑·删除消息 / 主动投递 / 上传后
触达），四个入口的会话写现在都经过这里，届时改一处而不是四处。
另一件今天就归位的重复：两个端点各自手写一份"角色已被删除 → 降级为空"的 try/except ——
那个 bug 正是重复本身造的（patch 端点当年漏了这一格，"只改个标题"也会 500）。

锁的预算也在这一层说清：`WRITE_WAIT`（3 秒）是**用户主动发起的检查点写**等锁的上限，
等不到抛 `ThreadBusy`，由 `api/main.py` 的处理器统一翻 409 —— 六个写检查点的口子共用
那一个出口，而不是各写各的 try。它比 `thread_locks._DEFAULT_WAIT`（150 秒，给整轮对话
排队用的）短得多：这几条路径前端本来就在忙时不给点，真撞上是罕见竞态，挂两分半不如拒。
"""

from __future__ import annotations

import uuid
from typing import Any

from rolecard_agent.config import Settings
from rolecard_agent.core.thread_locks import thread_write
from rolecard_agent.roles.service import RoleCards, RoleNotFound
from rolecard_agent.storage import threads as _threads
from rolecard_agent.storage.db import SqlConnection

#: 会话级对话模式的两个合法档（`Settings.agent_default_mode` 与前端切换钮共用这套词汇）。
MODE_CHOICES = ("chat", "agent")

#: 用户主动发起的**历史写**（改并重答 / 删消息 / 删会话 / 上传时插说明）最多等多久拿
#: 会话写锁。等不到 → `ThreadBusy` → 409（出口在 `api/main.py`，六个写点共用）。
#: 为什么短：前端在忙时不给点这几条，真撞上是罕见竞态，让请求挂两分半比拒掉更糟。
WRITE_WAIT = 3.0


def resolve_agent_mode(raw: object, settings: Settings) -> str:
    """会话级 `agent_mode` 的**有效值**：显式设置('chat'/'agent') 优先生效；
    NULL / 未知值 = 回落全局默认（`settings.agent_default_mode`）。

    语义属于会话行本身（会话详情、局部更新、列表三个端点都读它），所以住 service 而不是
    某一个路由 —— 从前它长在路由里，"列表要不要回落"这种问题得去翻另一个文件。
    """
    if str(raw or "") in MODE_CHOICES:
        return str(raw)
    return settings.agent_default_mode


# ------------------------------------------------------------------ 读


def get_row(conn: SqlConnection, thread_id: str) -> Any | None:
    """会话行（归属与选择那几列）。存不存在、归不归谁由调用方判 —— 那是 HTTP 语义。"""
    return _threads.thread_row(conn, thread_id)


def exists(conn: SqlConnection, thread_id: str) -> bool:
    """这条会话在不在（主动面板那句"有没有历史"用：只问存在，不建行）。"""
    return _threads.thread_exists(conn, thread_id)


def display_row(conn: SqlConnection, thread_id: str) -> Any | None:
    """改完字段之后的回显源：标题 / 模型 / 模式三列（缺行 = None）。"""
    return _threads.thread_display_state(conn, thread_id)


def list_rows(conn: SqlConnection, user_id: str) -> list[Any]:
    """侧栏那份会话清单（按主人过滤、按"刚刚"倒序，含角色名与是否空白旗标）。"""
    return _threads.session_list_rows(conn, user_id)


def role_name_of(roles: RoleCards, role_id: str) -> str | None:
    """会话指向的那个角色叫什么；**角色已被删除 → None**（会话本身照常可用）。

    这一格是两个端点各写一份 try/except 藏出来的 bug：patch 端点当年漏了降级，
    于是"只改个标题"撞上一条指向已删角色的会话就 500（审查报告 M1，已复现）。
    降级逻辑收成一处之后，第三个端点加进来也没法再忘 —— 只吞 `RoleNotFound`：
    库连不上那类错必须照旧抛，静默返回 None 会把 500 藏成一个空字段。
    """
    try:
        return roles.get(role_id).role_name
    except RoleNotFound:
        return None


# ------------------------------------------------------------------ 写


def create(conn: SqlConnection, *, user_id: str, role_id: str, tool_epoch: int) -> str:
    """建一条会话行，返回新 `thread_id`（`s_<12 hex>` 形状由这里定，调用方不再自造）。"""
    thread_id = f"s_{uuid.uuid4().hex[:12]}"
    _threads.create_thread(
        conn, thread_id=thread_id, user_id=user_id, role_id=role_id, tool_epoch=tool_epoch
    )
    return thread_id


def set_model(conn: SqlConnection, thread_id: str, model_name: str | None) -> None:
    """会话级模型覆盖：None = 清除（回落 角色卡 → 全局默认）。"""
    _threads.set_model(conn, thread_id, model_name)


def set_mode(conn: SqlConnection, thread_id: str, mode: str | None) -> None:
    """会话级对话模式：None = 清除（回落全局默认）。合法档见 `MODE_CHOICES`。"""
    _threads.set_mode(conn, thread_id, mode)


def set_title(conn: SqlConnection, thread_id: str, title: str) -> None:
    _threads.set_title(conn, thread_id, title)


def seed_title(conn: SqlConnection, thread_id: str, fallback: str) -> None:
    """首条消息之后兜一次标题（只兜第一次，已有的不覆盖）。"""
    _threads.seed_title(conn, thread_id, fallback)


def touch(conn: SqlConnection, thread_id: str) -> None:
    """把会话顶到"刚刚"（毫秒精度，侧栏排序用）。"""
    _threads.touch_thread(conn, thread_id)


def message_count(conn: SqlConnection, thread_id: str) -> int | None:
    """冗余计数的现值；NULL = 未对账（探针据此决定走便宜路还是真读校准）。"""
    return _threads.message_count(conn, thread_id)


def bump_message_count(conn: SqlConnection, thread_id: str, delta: int) -> None:
    """检查点写入口（chat 轮 / 编辑重生成 / 删除 / 上传说明 / 主动投递）同一步的增量维护。"""
    _threads.bump_message_count(conn, thread_id, delta)


def record_message_count(conn: SqlConnection, thread_id: str, count: int) -> None:
    """全量 /messages 真读之后的对账写回。"""
    _threads.record_message_count(conn, thread_id, count)


def delete_everywhere(conn: SqlConnection, thread_id: str) -> dict[str, int]:
    """删会话：thread 行 + 全部载体表一并清，**持写锁**（在飞轮次不该被抽走检查点）。

    名单现数现用（`storage.db.thread_id_carriers`），从前写死 `("checkpoints","writes")`
    两张表时 `command_approval` 恰好漏掉 —— 已删会话的待批审批永远挂在队列上。
    拿不到锁抛 `ThreadBusy` → 路由/处理器翻 409，绝不"没锁照删"。
    """
    with thread_write(thread_id, timeout=WRITE_WAIT):
        return _threads.delete_thread_everywhere(conn, thread_id)
