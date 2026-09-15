"""Role card CRUD, built-in protection, and whitelist semantics.  Traceability: US-1, US-2, US-8."""

from __future__ import annotations

import sqlite3

import pytest

from rolecard_agent.roles.models import (
    MAX_EXEMPLARS,
    RoleCard,
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


def test_seeded_builtin_is_marked_and_listed_first(roles: RoleCardService) -> None:
    listed = roles.list_roles()
    # 内置角色顺序 = seed 顺序：通用助手（默认）在前，档案管理员其次。
    assert [r.role_id for r in listed[:2]] == ["general_assistant", "medical_archivist"]
    assert all(r.is_builtin for r in listed[:2])
    assert listed[1].temperature == 0.3


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


def test_allows_tool_semantics(roles: RoleCardService) -> None:
    roles.create(_new("wide", tool_whitelist=None))
    roles.create(_new("none", tool_whitelist=[]))
    roles.create(_new("one", tool_whitelist=["list_roles"]))

    assert roles.get("wide").allows_tool("anything") is True
    assert roles.get("none").allows_tool("list_roles") is False
    assert roles.get("one").allows_tool("list_roles") is True
    assert roles.get("one").allows_tool("other") is False


def test_builtin_role_cannot_be_deleted(roles: RoleCardService) -> None:
    with pytest.raises(BuiltinRoleProtected):
        roles.delete("medical_archivist")
    assert roles.exists("medical_archivist")


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


def test_allows_scope_is_opt_in() -> None:
    """`None` means no retrieval here - the opposite default from allows_tool, because
    reading stored documents widens the blast radius rather than narrowing capabilities."""
    base = RoleCard(
        role_id="reader", role_name="只读", system_prompt="x", knowledge_scopes=["health_reports"]
    )
    assert base.allows_scope("health_reports") is True
    assert base.allows_scope("guidelines") is False

    none_scoped = base.model_copy(update={"knowledge_scopes": None})
    empty_scoped = base.model_copy(update={"knowledge_scopes": []})
    assert none_scoped.allows_scope("health_reports") is False
    assert empty_scoped.allows_scope("health_reports") is False


def test_update_can_replace_exemplars(roles: RoleCardService) -> None:
    roles.create(_new("styled", exemplars=[RoleExemplar(user="旧", assistant="旧答")]))
    roles.update("styled", RoleCardUpdate(exemplars=[RoleExemplar(user="新", assistant="新答")]))
    assert [e.user for e in roles.get("styled").exemplars] == ["新"]
