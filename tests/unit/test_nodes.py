"""Unit tests for the graph nodes.

Integration tests exercise the happy path through a real compiled graph; these pin the
decisions that are cheap to get wrong and expensive to notice: when to loop back into the
tools, which tools a turn may see, and how a failing tool is retried.

See 技术评审与决策.md §9 D2 - these had no unit coverage before.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from rolecard_agent.config import Settings
from rolecard_agent.core.nodes import (
    MAX_TOOL_RETRIES,
    TOOL_FAILED,
    KernelContext,
    execute_tools,
    route_after_model,
    tools_for_turn,
)
from rolecard_agent.core.observability import NullTracer
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.models import RoleCardCreate
from rolecard_agent.roles.service import RoleCardService

# --------------------------------------------------------------------------- routing


def test_route_ends_when_the_model_answers_directly() -> None:
    state = {"messages": [HumanMessage(content="hi"), AIMessage(content="hello")]}
    assert route_after_model(state) == "end"


def test_route_loops_when_the_model_asks_for_a_tool() -> None:
    state = {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]),
        ]
    }
    assert route_after_model(state) == "tools"


def test_route_ends_on_an_empty_tool_call_list() -> None:
    """Some providers emit `tool_calls: []` instead of omitting it - that is not a call."""
    state = {"messages": [AIMessage(content="answer", tool_calls=[])]}
    assert route_after_model(state) == "end"


# --------------------------------------------------------------------------- tool visibility


@tool
def kernel_tool() -> str:
    """A kernel tool."""
    return "k"


@tool
def domain_tool() -> str:
    """A domain tool."""
    return "d"


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(kernel_tool)
    reg.register(domain_tool, domain="dom")
    return reg


def _ctx(registry: ToolRegistry, roles: RoleCardService, model: Any = None) -> KernelContext:
    return KernelContext(
        model=model,
        registry=registry,
        roles=roles,
        tracer=NullTracer(),
        settings=Settings(),
    )


def test_tools_for_turn_honours_both_stages(registry: ToolRegistry, roles: RoleCardService) -> None:
    roles.create(
        RoleCardCreate(
            role_id="narrow", role_name="窄", system_prompt="x", tool_whitelist=["domain_tool"]
        )
    )
    state = {"current_role_id": "narrow", "enabled_domains": ["dom"]}
    assert [t.name for t in tools_for_turn(state, _ctx(registry, roles))] == ["domain_tool"]


def test_unknown_role_yields_no_tools(registry: ToolRegistry, roles: RoleCardService) -> None:
    """A session pointing at a deleted role must not fall through to 'everything allowed'."""
    state = {"current_role_id": "ghost", "enabled_domains": ["dom"]}
    with pytest.raises(Exception, match="ghost"):
        tools_for_turn(state, _ctx(registry, roles))


# --------------------------------------------------------------------------- tool retry


def _flaky_tool(fail_times: int) -> tuple[Any, list[int]]:
    calls = [0]

    @tool("flaky")
    def flaky() -> str:
        """Fails a fixed number of times, then succeeds."""
        calls[0] += 1
        if calls[0] <= fail_times:
            raise RuntimeError("transient")
        return "ok"

    return flaky, calls


def _state_with_call(name: str, role_id: str = "wide") -> dict[str, Any]:
    return {
        "messages": [AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": "c1"}])],
        "current_role_id": role_id,
        "enabled_domains": [],
        "thread_id": "t1",
    }


@pytest.fixture
def wide_role(roles: RoleCardService) -> str:
    """A role with `tool_whitelist=None`, i.e. every enabled tool is permitted.

    Retry and routing tests must not silently double as permission-filter tests: the built-in
    role has an explicit whitelist, so a tool invented inside a test would be denied before it
    was ever invoked - which is exactly what happened the first time these were written.
    """
    roles.create(
        RoleCardCreate(role_id="wide", role_name="宽", system_prompt="x", tool_whitelist=None)
    )
    return "wide"


def test_transient_failure_is_retried_then_succeeds(roles: RoleCardService, wide_role: str) -> None:
    flaky, calls = _flaky_tool(fail_times=1)
    reg = ToolRegistry()
    reg.register(flaky)
    out = execute_tools(_state_with_call("flaky", wide_role), _ctx(reg, roles))

    assert calls[0] == 2
    assert out["messages"][0].content == "ok"
    assert out["retry_count"] == 1


def test_retry_is_bounded(roles: RoleCardService, wide_role: str) -> None:
    """A permanently broken tool must not be retried forever."""
    flaky, calls = _flaky_tool(fail_times=99)
    reg = ToolRegistry()
    reg.register(flaky)
    out = execute_tools(_state_with_call("flaky", wide_role), _ctx(reg, roles))

    assert calls[0] == MAX_TOOL_RETRIES + 1
    assert out["messages"][0].content == TOOL_FAILED
    assert out["retry_count"] == MAX_TOOL_RETRIES


def test_a_clean_call_reports_no_retries(roles: RoleCardService, wide_role: str) -> None:
    """The field is only written when something actually went wrong."""
    reg = ToolRegistry()
    reg.register(kernel_tool)
    out = execute_tools(_state_with_call("kernel_tool", wide_role), _ctx(reg, roles))
    assert "retry_count" not in out
    assert isinstance(out["messages"][0], ToolMessage)


def test_retry_count_accumulates_across_turns(roles: RoleCardService, wide_role: str) -> None:
    flaky, _ = _flaky_tool(fail_times=99)
    reg = ToolRegistry()
    reg.register(flaky)
    state = {**_state_with_call("flaky", wide_role), "retry_count": 5}
    out = execute_tools(state, _ctx(reg, roles))
    assert out["retry_count"] == 5 + MAX_TOOL_RETRIES
