"""Graph routing tests. The LLM is MOCKED with fixed responses; we assert which edge was taken.

Traceability: US-1, US-2, US-3, US-4.


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
from langchain_core.tools import tool

from rolecard_agent.config import Settings
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.graph import build_kernel
from rolecard_agent.core.identity import DEFAULT_USER_ID, active_user_id
from rolecard_agent.core.observability import NullTracer
from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.state import new_state
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.models import RoleCardCreate
from rolecard_agent.roles.service import RoleCards, RoleCardService
from rolecard_agent.storage.db import bootstrap, connect
from tests.conftest import ScriptedChat, compare_health_index, list_roles, query_health_record


def cards(store: RoleCardService) -> RoleCards:
    """测试里"本机主人眼里的那些卡"的简写（M2a 之后每次读写都得说清为谁）。"""
    return store.scoped("u1")

def _kernel(
    db_path: Path,
    replies: list[Any],
    *,
    registry: ToolRegistry | None = None,
    enable_health: bool = True,
) -> Any:
    """Build a fresh kernel over an existing database file.

    Reconnecting on every call is deliberate: it is the only way to prove persistence, since
    an in-memory cache would pass a same-process assertion trivially.

    The plugin switch is wired through a real `PluginService`, because `enabled_domains` is read
    live from the `plugin` table (not from state) - that is the whole point of the tool_epoch /
    C14 design. `enable_health=False` simulates "the operator switched the domain off".
    """
    conn = connect(db_path)
    bootstrap(conn, enabled_domains=("health",))
    roles = RoleCardService(conn)
    roles.seed_builtins(user_id="u1")
    roles.seed_domain_roles(user_id="u1")

    plugins = PluginService(conn, known_plugins=["health"])
    plugins.register("health", display_name="Health")
    plugins.set_enabled("health", enable_health)

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
        plugins=plugins,
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
    """US-2: the whitelist is enforced before binding, so the model never sees a forbidden tool."""
    db = tmp_path / "app.db"
    _seed_identity(db)
    conn = connect(db)
    bootstrap(conn, enabled_domains=("health",))
    roles = RoleCardService(conn)
    roles.seed_builtins(user_id="u1")
    roles.seed_domain_roles(user_id="u1")
    cards(roles).create(
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
    plugins = PluginService(conn, known_plugins=["health"])
    plugins.register("health", display_name="Health")
    plugins.set_enabled("health", True)
    model = ScriptedChat([AIMessage(content="好的。")])
    graph = build_kernel(
        model=model,
        registry=reg,
        roles=roles,
        tracer=NullTracer(),
        settings=Settings(),
        checkpointer=make_checkpointer(conn),
        plugins=plugins,
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
        enable_health=False,  # the operator switched the domain off
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


@tool
def who_is_this_turn_for() -> str:
    """域工具的那个形状：装配期定下的**零参**闭包，运行期才回答"在为谁读"。"""
    return active_user_id(DEFAULT_USER_ID)


def _only(tool_: object) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tool_)  # type: ignore[arg-type]
    return reg


def _ask_who(user_id: str) -> str:  # pragma: no cover - 只是把两轮跑法收一处
    return user_id



def _allow_this_tool(db: Path, owner: str) -> None:
    """造一张只授权那个探针工具的角色卡。

    为什么不借内置角色：内置卡的白名单是产品事实（`roles/seed.py`），拿它测机制就等于
    哪天白名单一改，这条机制用例跟着误红。
    """
    conn = connect(db)
    RoleCardService(conn).scoped(owner).create(
        RoleCardCreate(
            role_id="probe",
            role_name="探针",
            system_prompt="只为测机制存在。",
            tool_whitelist=["who_is_this_turn_for"],
        )
    )
    conn.close()

def test_a_turn_binds_its_threads_owner_for_zero_arg_tool_closures(tmp_path: Path) -> None:
    """§4.1 的 M3 后半：她查的是**这条线程的主人**的档案，不是这台实例的主人的。

    绑之前那一版是"HTTP 层按请求解析、工具层按实例主人读"—— 单机自用两者同值所以看不出来，
    `app_user` 一加第二行就变成"界面是 A 的会话、她报出 B 的数值"。这条用例钉的就是那半步：
    节点在入口把 `state["user_id"]` 绑进上下文（`core/identity.bound_user`），
    而工具的签名一个字不改（工具对模型必须看起来零参数，否则模型能自己填"我是谁"）。
    """
    db = tmp_path / "app.db"
    _seed_identity(db)
    _allow_this_tool(db, "u1")
    graph, _, _ = _kernel(
        db,
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "who_is_this_turn_for", "args": {}, "id": "c1"}],
            ),
            AIMessage(content="查好了"),
        ],
        registry=_only(who_is_this_turn_for),
    )
    result = graph.invoke(
        {
            **new_state(
                thread_id="thread-1", user_id="u1", current_role_id="probe"
            ),
            "messages": [HumanMessage(content="看下我的档案")],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )
    seen = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
    assert seen == ["u1"], "工具读到的主人必须是这条线程的，不是实例的"


def test_a_state_without_an_owner_falls_back_instead_of_crashing(tmp_path: Path) -> None:
    """老线程的状态里可以没有 `user_id`（归属是 09-27 才落到角色卡上的）。

    绑不上就回落到这台实例的主人，**而不是炸在图里** —— 炸的症状是"某条老会话突然发不出
    消息"，那种现象没人会往"历史状态少一个键"上想。
    """
    db = tmp_path / "app.db"
    _seed_identity(db)
    _allow_this_tool(db, DEFAULT_USER_ID)
    graph, _, _ = _kernel(
        db,
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "who_is_this_turn_for", "args": {}, "id": "c1"}],
            ),
            AIMessage(content="查好了"),
        ],
        registry=_only(who_is_this_turn_for),
    )
    result = graph.invoke(
        {
            **new_state(
                thread_id="thread-1", user_id="", current_role_id="probe"
            ),
            "messages": [HumanMessage(content="看下我的档案")],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )
    seen = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
    assert seen == [DEFAULT_USER_ID]
