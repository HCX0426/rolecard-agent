"""Unit tests for the plugin enable/disable half of the domain registry.  Traceability: US-3.

The point under test is the version stamp: toggling a plugin must bump `tool_epoch` inside the
same transaction, and re-toggling an already-set state must NOT (or every live session would be
needlessly invalidated). See 技术评审与决策.md §9 B2 / C14.
"""

from __future__ import annotations

import sqlite3

import pytest

from rolecard_agent.core.plugins import PluginService, UnknownPlugin
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def conn() -> sqlite3.Connection:
    # Core + roles schema only: seeds the `tool_epoch = 1` row the service reads.
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    return c


@pytest.fixture
def plugins(conn: sqlite3.Connection) -> PluginService:
    return PluginService(conn, known_plugins=["health"])


def test_tool_epoch_starts_at_one(conn: sqlite3.Connection) -> None:
    assert PluginService(conn).tool_epoch() == 1


def test_register_is_idempotent_and_off_by_default(plugins: PluginService) -> None:
    plugins.register("health", display_name="Health")
    plugins.register("health", display_name="Health")  # second call is a no-op
    assert plugins.is_enabled("health") is False
    assert len(plugins.list_plugins()) == 1


def test_set_enabled_bumps_epoch(plugins: PluginService) -> None:
    plugins.register("health", display_name="Health")
    before = plugins.tool_epoch()
    epoch = plugins.set_enabled("health", True)
    assert epoch == before + 1
    assert plugins.tool_epoch() == before + 1
    assert plugins.is_enabled("health") is True


def test_noop_toggle_does_not_bump_epoch(plugins: PluginService) -> None:
    plugins.register("health", display_name="Health")
    plugins.set_enabled("health", True)  # bumps
    e1 = plugins.tool_epoch()
    e2 = plugins.set_enabled("health", True)  # already enabled -> no bump
    assert e2 == e1
    assert plugins.tool_epoch() == e1


def test_disabled_plugin_drops_from_enabled_domains(plugins: PluginService) -> None:
    plugins.register("health", display_name="Health")
    plugins.set_enabled("health", True)
    assert plugins.enabled_domains() == ["health"]
    plugins.set_enabled("health", False)
    assert plugins.enabled_domains() == []


def test_unknown_plugin_is_rejected(plugins: PluginService) -> None:
    with pytest.raises(UnknownPlugin):
        plugins.set_enabled("bogus", True)


def test_toggle_writes_audit(plugins: PluginService) -> None:
    """US-3: every enable/disable toggle is written to the audit_log."""
    plugins.register("health", display_name="Health")
    plugins.set_enabled("health", True, actor="alice")
    rows = [
        (r["action"], r["target"], r["actor"])
        for r in plugins._conn.execute(  # noqa: SLF001 - test inspects the audit table
            "SELECT action, target, actor FROM audit_log"
        ).fetchall()
    ]
    assert ("plugin_enable", "health", "alice") in rows
