"""Role card CRUD, built-in protection, and whitelist semantics."""

from __future__ import annotations

import sqlite3

import pytest

from rolecard_agent.roles.models import RoleCardCreate, RoleCardUpdate
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
    assert listed[0].role_id == "medical_archivist"
    assert listed[0].is_builtin is True
    assert listed[0].temperature == 0.3


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
    """Switching changes the pointer only - messages are never touched.

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
