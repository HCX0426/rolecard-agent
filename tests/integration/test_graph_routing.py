"""Graph routing tests. The LLM is MOCKED with fixed responses; we assert which edge was taken.

These are the M1 acceptance tests. They exercise the three claims the kernel makes:

  1. a tool call loops back through the model and the tool result reaches history;
  2. session state survives a process restart (new connection, same file);
  3. the permission boundary is applied *before* binding, so the model never sees a tool the
     role may not call.

Plus the invariant that the system prompt never enters the checkpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from rolecard_agent.config import Settings
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.graph import build_kernel
from rolecard_agent.core.observability import NullTracer
from rolecard_agent.core.state import new_state
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.models import RoleCardCreate
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import bootstrap, connect
from tests.conftest import ScriptedChat, compare_health_index, list_roles, query_health_record


def _kernel(db_path: Path, replies: list[Any], *, registry: ToolRegistry | None = None) -> Any:
    """Build a fresh kernel over an existing database file.

    Reconnecting on every call is deliberate: it is the only way to prove persistence, since
    an in-memory cache would pass a same-process assertion trivially.
    """
    conn = connect(db_path)
    bootstrap(conn, enabled_domains=("health",))
    roles = RoleCardService(conn)
    roles.seed_builtins()

    reg = registry or ToolRegistry()
    if registry is None:
        reg.register(list_roles)
        reg.register_many([query_health_record, compare_health_index], domain="health")

    model = ScriptedChat(replies)
    graph = build_kernel(
        model=model,
        registry=reg,
        roles=roles,
        tracer=NullTracer(),
        settings=Settings(),
        checkpointer=make_checkpointer(conn),
    )
    return graph, model, roles


def _seed_identity(db_path: Path) -> None:
    conn = connect(db_path)
    bootstrap(conn, enabled_domains=("health",))
    conn.executescript(
        """
        INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');
        INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name)
            VALUES ('u1', 't1', 'demo user');
        INSERT OR IGNORE INTO session_thread (thread_id, user_id, current_role_id)
            VALUES ('thread-1', 'u1', 'medical_archivist');
        """
    )
    conn.commit()
    conn.close()


def test_tool_call_loops_back_and_result_reaches_history(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    _seed_identity(db)
    graph, model, _ = _kernel(
        db,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "query_health_record",
                        "args": {"start_date": "2026-01-01"},
                        "id": "c1",
                    }
                ],
            ),
            AIMessage(content="你的结石直径是 6.0 mm。【未经人工校验】"),
        ],
    )

    result = graph.invoke(
        {
            **new_state(
                thread_id="thread-1",
                user_id="u1",
                current_role_id="medical_archivist",
                enabled_domains=["health"],
            ),
            "messages": [HumanMessage(content="上次的结石直径是多少")],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )

    assert len(model.calls) == 2, "the model must be called again after the tool result"
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert "6.0 mm" in tool_messages[0].content
    assert result["messages"][-1].content.startswith("你的结石直径")


def test_system_prompt_never_enters_the_checkpoint(tmp_path: Path) -> None:
    """D3: only user / assistant / tool messages are persisted."""
    db = tmp_path / "app.db"
    _seed_identity(db)
    graph, _, _ = _kernel(db, [AIMessage(content="好的。")])
    result = graph.invoke(
        {
            **new_state(thread_id="thread-1", user_id="u1", current_role_id="medical_archivist"),
            "messages": [HumanMessage(content="你好")],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )
    assert not [m for m in result["messages"] if isinstance(m, SystemMessage)]

    # And it is still absent when the state is reloaded from the checkpoint.
    reloaded = graph.get_state({"configurable": {"thread_id": "thread-1"}})
    assert not [m for m in reloaded.values["messages"] if isinstance(m, SystemMessage)]


def test_history_survives_a_reconnect(tmp_path: Path) -> None:
    """The M1 acceptance criterion: restart the process, the conversation is still there."""
    db = tmp_path / "app.db"
    _seed_identity(db)
    first_graph, _, _ = _kernel(db, [AIMessage(content="第一次回答")])
    first_graph.invoke(
        {
            **new_state(thread_id="thread-1", user_id="u1", current_role_id="medical_archivist"),
            "messages": [HumanMessage(content="第一句")],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )

    # Simulate a restart: brand-new connection, brand-new graph, same database file.
    second_graph, _, _ = _kernel(db, [AIMessage(content="第二次回答")])
    reloaded = second_graph.get_state({"configurable": {"thread_id": "thread-1"}})
    contents = [m.content for m in reloaded.values["messages"]]
    assert "第一句" in contents
    assert "第一次回答" in contents


def test_whitelist_is_applied_before_binding(tmp_path: Path) -> None:
    """The role may only call what its whitelist allows - the model must not even see the rest."""
    db = tmp_path / "app.db"
    _seed_identity(db)
    conn = connect(db)
    bootstrap(conn, enabled_domains=("health",))
    roles = RoleCardService(conn)
    roles.seed_builtins()
    roles.create(
        RoleCardCreate(
            role_id="narrow",
            role_name="只看对比",
            system_prompt="只能做历年对比。",
            tool_whitelist=["compare_health_index"],
        )
    )

    reg = ToolRegistry()
    reg.register(list_roles)
    reg.register_many([query_health_record, compare_health_index], domain="health")
    model = ScriptedChat([AIMessage(content="好的。")])
    graph = build_kernel(
        model=model,
        registry=reg,
        roles=roles,
        tracer=NullTracer(),
        settings=Settings(),
        checkpointer=make_checkpointer(conn),
    )
    graph.invoke(
        {
            **new_state(
                thread_id="thread-1",
                user_id="u1",
                current_role_id="narrow",
                enabled_domains=["health"],
            ),
            "messages": [HumanMessage(content="对比一下")],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )
    assert model.last_visible_tools == ["compare_health_index"]


def test_disabled_domain_removes_tools_and_offline_call_does_not_raise(tmp_path: Path) -> None:
    """C14: a historical tool_call for a now-disabled plugin must degrade, not explode."""
    db = tmp_path / "app.db"
    _seed_identity(db)
    graph, _, _ = _kernel(
        db,
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "query_health_record", "args": {}, "id": "c9"}],
            ),
            AIMessage(content="该项能力当前不可用。"),
        ],
    )
    result = graph.invoke(
        {
            **new_state(
                thread_id="thread-1",
                user_id="u1",
                current_role_id="medical_archivist",
                enabled_domains=[],
            ),  # plugin switched off
            "messages": [HumanMessage(content="查一下")],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert "未启用" in tool_messages[0].content


def test_guard_blocks_a_diagnosing_reply_inside_the_graph(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    _seed_identity(db)
    graph, _, _ = _kernel(db, [AIMessage(content="你得了胆囊结石，建议服用熊去氧胆酸。")])
    result = graph.invoke(
        {
            **new_state(
                thread_id="thread-1",
                user_id="u1",
                current_role_id="medical_archivist",
                enabled_domains=["health"],
            ),
            "messages": [HumanMessage(content="我这个严重吗")],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )
    final = result["messages"][-1].content
    assert "你得了" not in final
    assert "超出" in final  # the standard refusal text
