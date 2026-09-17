"""Graph state.

Two invariants the rest of the kernel depends on:

  * The system prompt is NOT part of `messages` and never enters the checkpoint. It is
    reassembled every turn as GLOBAL_SAFETY_PROMPT + role.system_prompt (D3). Storing it
    would leak the previous role's persona into the next one and produce multiple system
    messages per turn.

  * `tool_epoch` exists because the tool set is filtered dynamically while the graph is
    compiled once. If a plugin is disabled mid-session, historical messages may still carry
    tool_calls for tools that no longer exist; the executor must answer "this capability is
    offline" instead of raising (C14).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    """State threaded through the graph.

    `total=False` because a turn may resume from a checkpoint that predates a newly added
    field; nodes must tolerate missing keys rather than assume they were seeded.
    """

    # add_messages appends and de-duplicates by message id. Replacing the list wholesale
    # would silently drop history on every node that returns messages.
    messages: Annotated[list[AnyMessage], add_messages]

    thread_id: str
    user_id: str
    current_role_id: str
    # 会话级模型覆盖（对话页模型下拉）：None = 无覆盖，按 角色.model_name → 默认 解析。
    model_name: str | None

    enabled_domains: list[str]
    tool_epoch: int
    retry_count: int

    # 最近一轮被上下文预算裁掉的历史条数与实际送出的条数（0 = 没裁）。
    # 为什么放进 state 而不是只留在日志里：**用户有权知道模型这次没看到早期对话**。
    # 静默丢弃历史会让"模型怎么忘了我前面说的"变成一个无从解释的现象（审查报告 H3）。
    # 写进 checkpoint 还带来一个副作用：界面重新加载后依然能查到这个事实。
    context_trimmed: int
    context_kept: int


def now_ts() -> str:
    """消息创建时间（本地时间，秒级字符串）。

    随消息存进 `additional_kwargs["created_at"]`（HumanMessage 在会话路由创建、
    AIMessage/ToolMessage 在内核节点创建），历史回放据此显示时间（用户 2026-09-17）。
    存本地时间字符串即可：自用单时区，且 checkpoint 里旧消息天然没有该字段（不显示）。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_state(
    *,
    thread_id: str,
    user_id: str,
    current_role_id: str,
    model_name: str | None = None,
    enabled_domains: list[str] | None = None,
    tool_epoch: int = 1,
) -> dict[str, Any]:
    """Build the initial state for a fresh thread.

    A function rather than a literal at each call site: a missing `tool_epoch` would default
    to 0 and make every resume look like a downgrade.
    """
    return {
        "messages": [],
        "thread_id": thread_id,
        "user_id": user_id,
        "current_role_id": current_role_id,
        "model_name": model_name,
        "enabled_domains": list(enabled_domains or []),
        "tool_epoch": tool_epoch,
        "retry_count": 0,
        "context_trimmed": 0,
        "context_kept": 0,
    }
