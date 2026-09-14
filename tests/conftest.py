"""Shared fixtures.

Everything here is offline: no Ollama, no network, no model downloads. If a test needs a
model it gets `ScriptedChat`, which replays a fixed list of replies. That constraint is what
lets `pytest` run in CI on any machine.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool, tool

from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import bootstrap, connect

# --------------------------------------------------------------------------- model fake


class ScriptedChat:
    """Replays queued replies and records what it was asked.

    Records the tools visible at each call, which is how the whitelist tests assert that
    filtering happened *before* binding rather than at execution time.
    """

    def __init__(self, replies: Sequence[BaseMessage] | None = None) -> None:
        self.replies: list[BaseMessage] = list(replies or [])
        self.calls: list[dict[str, Any]] = []
        self._bound: list[str] | None = None

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChat:
        self._bound = sorted(getattr(t, "name", str(t)) for t in tools)
        return self

    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:  # noqa: A002
        self.calls.append({"tools": self._bound, "messages": list(input)})
        if not self.replies:
            return AIMessage(content="(script exhausted)")
        return self.replies.pop(0)

    @property
    def last_visible_tools(self) -> list[str] | None:
        return self.calls[-1]["tools"] if self.calls else None


# --------------------------------------------------------------------------- fake tools


@tool
def query_health_record(start_date: str = "", end_date: str = "") -> str:
    """Query stored health record indicators within an optional date range."""
    return "结石直径 6.0 mm（参考范围 0-5）【未经人工校验】"


@tool
def compare_health_index(index_name: str) -> str:
    """Compare one indicator across years to show how it changed."""
    return "结石直径：2025 年 5.0 mm → 2026 年 6.0 mm【未经人工校验】"


@tool
def list_roles() -> str:
    """List the roles available in this deployment."""
    return "medical_archivist"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def conn(tmp_path: Any) -> Iterator[sqlite3.Connection]:
    """A bootstrapped in-temp-dir database, including one tenant, user and thread."""
    connection = connect(tmp_path / "app.db")
    bootstrap(connection, enabled_domains=("health",))
    connection.executescript(
        """
        INSERT INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');
        INSERT INTO app_user (user_id, tenant_id, display_name) VALUES ('u1', 't1', 'demo user');
        INSERT INTO session_thread (thread_id, user_id, current_role_id)
            VALUES ('thread-1', 'u1', 'medical_archivist');
        """
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def roles(conn: sqlite3.Connection) -> RoleCardService:
    service = RoleCardService(conn)
    service.seed_builtins()
    return service


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(list_roles)  # kernel tool: domain=None
    reg.register_many([query_health_record, compare_health_index], domain="health")
    return reg


@pytest.fixture
def all_tools() -> list[BaseTool]:
    return [list_roles, query_health_record, compare_health_index]
