"""Role card CRUD, built-in protection, and whitelist semantics.  Traceability: US-1, US-2, US-8."""

from __future__ import annotations

import sqlite3

import pytest

from rolecard_agent.roles.models import (
    MAX_EXEMPLARS,
    RoleCardCreate,
    RoleCardUpdate,
    RoleExemplar,
)
from rolecard_agent.roles.service import (
    BuiltinRoleProtected,
    RoleAlreadyExists,
    RoleCardService,
    RoleNotFound,
)


def _new(role_id: str = "custom", **overrides: object) -> RoleCardCreate:
    payload = {
        "role_id": role_id,
        "role_name": "自定义角色",
        "system_prompt": "只做一件事。",
        "temperature": 0.5,
        "tool_whitelist": ["list_roles"],
    }
    payload.update(overrides)
    return RoleCardCreate(**payload)


def test_seeded_roles_are_listed_builtin_first(roles: RoleCardService) -> None:
    listed = roles.list_roles()
    # 通用助手（内置）恒在首位；档案管理员已降级为域种子角色（自定义类型）。
    assert listed[0].role_id == "general_assistant" and listed[0].is_builtin
    assert any(r.role_id == "medical_archivist" and not r.is_builtin for r in listed)
    archivist = roles.get("medical_archivist")
    assert archivist.temperature == 0.3


def test_seeding_is_idempotent(roles: RoleCardService) -> None:
    before = len(roles.list_roles())
    roles.seed_builtins()
    roles.seed_builtins()
    assert len(roles.list_roles()) == before


def test_create_and_get(roles: RoleCardService) -> None:
    created = roles.create(_new("analyst"))
    assert created.role_id == "analyst"
    assert created.is_builtin is False
    assert roles.get("analyst").role_name == "自定义角色"


def test_duplicate_create_is_rejected(roles: RoleCardService) -> None:
    roles.create(_new("analyst"))
    with pytest.raises(RoleAlreadyExists):
        roles.create(_new("analyst"))


def test_missing_role_raises(roles: RoleCardService) -> None:
    with pytest.raises(RoleNotFound):
        roles.get("nope")


def test_update_touches_only_the_provided_fields(roles: RoleCardService) -> None:
    roles.create(_new("analyst", description="原始说明"))
    updated = roles.update("analyst", RoleCardUpdate(temperature=0.9))
    assert updated.temperature == 0.9
    assert updated.description == "原始说明"  # untouched
    assert updated.system_prompt == "只做一件事。"


def test_update_with_nothing_to_do_is_a_noop(roles: RoleCardService) -> None:
    roles.create(_new("analyst"))
    assert roles.update("analyst", RoleCardUpdate()).role_id == "analyst"


def test_update_missing_role_raises(roles: RoleCardService) -> None:
    with pytest.raises(RoleNotFound):
        roles.update("nope", RoleCardUpdate(role_name="x"))


def test_whitelist_none_and_empty_round_trip_distinctly(roles: RoleCardService) -> None:
    """`None` = every enabled tool, `[]` = none. Confusing the two is a silent privilege
    escalation, so both directions are asserted."""
    roles.create(_new("all_tools", tool_whitelist=None))
    roles.create(_new("no_tools", tool_whitelist=[]))
    assert roles.get("all_tools").tool_whitelist is None
    assert roles.get("no_tools").tool_whitelist == []


def test_builtin_role_cannot_be_deleted(roles: RoleCardService) -> None:
    # 内置只剩「通用助手」；域种子角色（档案管理员）类型是自定义，可删除。
    with pytest.raises(BuiltinRoleProtected):
        roles.delete("general_assistant")
    assert roles.exists("general_assistant")
    roles.delete("medical_archivist")
    assert not roles.exists("medical_archivist")
    # 重启（再次播种）：缺失才补插，且不会复活为内置。
    roles.seed_domain_roles()
    assert roles.exists("medical_archivist")
    assert roles.get("medical_archivist").is_builtin is False


def test_custom_role_can_be_deleted(roles: RoleCardService) -> None:
    roles.create(_new("temp"))
    roles.delete("temp")
    assert roles.exists("temp") is False
    with pytest.raises(RoleNotFound):
        roles.delete("temp")


def test_role_id_must_be_lowercase(roles: RoleCardService) -> None:
    with pytest.raises(ValueError):
        _new("BadId")


def test_switch_role_keeps_history_and_audits(
    roles: RoleCardService, conn: sqlite3.Connection
) -> None:
    """Switching changes the pointer only - messages are never touched.  US-1 / US-3.

    The checkpoint table is not even referenced here, which is the point: role switching is a
    one-column update on `session_thread`.
    """
    roles.create(_new("analyst"))
    assert roles.current_thread_role("thread-1") == "medical_archivist"

    roles.set_thread_role("thread-1", "analyst", actor="tester")

    assert roles.current_thread_role("thread-1") == "analyst"
    audit = conn.execute("SELECT actor, action, target FROM audit_log").fetchall()
    assert [tuple(r) for r in audit] == [("tester", "switch_role", "thread-1")]


def test_switch_to_unknown_role_leaves_the_thread_alone(roles: RoleCardService) -> None:
    with pytest.raises(RoleNotFound):
        roles.set_thread_role("thread-1", "nope")
    assert roles.current_thread_role("thread-1") == "medical_archivist"


def test_switch_on_unknown_thread_raises(roles: RoleCardService) -> None:
    with pytest.raises(RoleNotFound):
        roles.set_thread_role("ghost-thread", "medical_archivist")


# --------------------------------------------------------- exemplars & knowledge scopes
#
# Reproducing a role is not just rules. Examples shape behaviour more per token than longer
# instructions, and knowledge scopes are the retrieval authorisation - so both need to survive
# the JSON round trip exactly.


def test_builtin_role_ships_with_examples_and_a_scope(roles: RoleCardService) -> None:
    role = roles.get("medical_archivist")
    assert role.exemplars is not None
    # At least one example must be a refusal: imitation is the strongest signal, so a role
    # that only sees successful lookups learns to answer everything.
    assert any("不能" in item.assistant for item in role.exemplars)
    assert role.knowledge_scopes == ["health_reports"]


def test_exemplars_round_trip(roles: RoleCardService) -> None:
    given = [
        RoleExemplar(user="问一", assistant="答一"),
        RoleExemplar(user="问二", assistant="答二"),
    ]
    roles.create(_new("styled", exemplars=given))
    loaded = roles.get("styled").exemplars
    assert [(e.user, e.assistant) for e in loaded] == [("问一", "答一"), ("问二", "答二")]


def test_exemplars_are_optional_and_stay_none(roles: RoleCardService) -> None:
    roles.create(_new("plain"))
    assert roles.get("plain").exemplars is None


def test_too_many_exemplars_is_rejected() -> None:
    too_many = [RoleExemplar(user=f"q{i}", assistant="a") for i in range(MAX_EXEMPLARS + 1)]
    with pytest.raises(ValueError, match="at most"):
        _new("greedy", exemplars=too_many)


def test_exemplar_char_budget_is_enforced() -> None:
    # Each item stays inside its own per-field limit; it is the TOTAL that must be rejected.
    big = [RoleExemplar(user="问", assistant="答" * 2000) for _ in range(2)]
    with pytest.raises(ValueError, match="budget"):
        _new("wordy", exemplars=big)


def test_exemplar_cannot_be_empty() -> None:
    with pytest.raises(ValueError):
        RoleExemplar(user="", assistant="答")


def test_knowledge_scopes_round_trip(roles: RoleCardService) -> None:
    roles.create(_new("reader", knowledge_scopes=["health_reports", "guidelines"]))
    assert roles.get("reader").knowledge_scopes == ["health_reports", "guidelines"]


def test_bad_scope_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid knowledge scope"):
        _new("badscope", knowledge_scopes=["Health Reports"])


def test_update_can_replace_exemplars(roles: RoleCardService) -> None:
    roles.create(_new("styled", exemplars=[RoleExemplar(user="旧", assistant="旧答")]))
    roles.update("styled", RoleCardUpdate(exemplars=[RoleExemplar(user="新", assistant="新答")]))
    assert [e.user for e in roles.get("styled").exemplars] == ["新"]


def test_deleting_a_role_takes_her_own_state_with_it(
    conn: sqlite3.Connection, roles: RoleCardService
) -> None:
    """R26-08：删角色要连**她自己的**记忆与主动状态一起删，但不动用户的会话。

    从前这里只删 `role_card` 一行，于是同名重建一张卡就把旧记忆原样复活 ——
    用户读到的是"我删掉的角色还记得我从没说过的事"。
    """
    from rolecard_agent.core.memory import add_item
    from rolecard_agent.core.proactive_state import get_state, save_state

    roles.create(_new("ghost"))
    add_item(conn, bucket="ghost", text="用户下周要体检")
    st = get_state(conn, "ghost")
    st.affinity = 3.0
    save_state(conn, st)
    assert get_state(conn, "ghost").affinity == 3.0

    roles.delete("ghost")

    gone = conn.execute(
        "SELECT COUNT(*) c FROM role_card WHERE role_id='ghost'"
    ).fetchone()["c"]
    assert gone == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM role_memory_item WHERE role_id='ghost'"
    ).fetchone()["c"] == 0, "记忆跟着角色走"
    assert conn.execute(
        "SELECT COUNT(*) c FROM role_proactive_state WHERE role_id='ghost'"
    ).fetchone()["c"] == 0, "关系数值跟着角色走"

    # 同名重建：必须是一张干净的卡，不该继承任何旧状态
    roles.create(_new("ghost", system_prompt="全新设定。"))
    fresh = get_state(conn, "ghost")
    assert fresh.affinity == 0.0 and fresh.open_threads == ()
    assert conn.execute(
        "SELECT COUNT(*) c FROM role_memory_item WHERE role_id='ghost'"
    ).fetchone()["c"] == 0


def test_deleting_a_role_keeps_the_users_conversation(
    conn: sqlite3.Connection, roles: RoleCardService
) -> None:
    """删角色销毁的是那个角色，**不是用户聊过的历史**（与"卸载不删数据"同一条理由）。"""
    roles.create(_new("todelete"))
    # 用 conftest 已经种好的 tenant/user（'t1'/'u1'）—— 外键是真开的，自己拼一份
    # 假租户只会先撞 FK。
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title)"
        " VALUES ('s_x', 'u1', 'todelete', '和她要聊的事')"
    )
    conn.commit()

    roles.delete("todelete")

    row = conn.execute(
        "SELECT current_role_id FROM session_thread WHERE thread_id='s_x'"
    ).fetchone()
    assert row is not None, "会话与历史必须原样留着"
    assert row["current_role_id"] == "todelete"
