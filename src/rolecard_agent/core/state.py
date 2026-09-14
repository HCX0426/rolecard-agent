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
    }
