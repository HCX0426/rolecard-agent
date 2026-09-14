"""Plugin enable/disable - the runtime half of the domain registry.

Two different things, kept apart on purpose:

  * **REGISTERED** domains live in `domains/registry.py` - code, changed by a commit.
  * **ENABLED** domains live in the `plugin` table - data, changed by an operator.

This module owns the second one.

Every toggle bumps `kernel_meta.tool_epoch` **inside the same transaction**. That version is
what makes "the tool set changed while this session was alive" detectable after a restart: a
session whose recorded epoch is older than the current one may carry historical tool_calls for
tools that no longer exist. `execute_tools` already degrades those to "this capability is
offline" instead of raising - the epoch is what turns that from mysterious into explainable.

Enabling or disabling is deliberately NOT an LLM-callable tool. Letting the model move its own
permission boundary is self-authorization, the same reasoning as `switch_role` (D2 / C5).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

TOOL_EPOCH_KEY = "tool_epoch"
DEFAULT_TOOL_EPOCH = 1


class PluginError(Exception):
    """Base for plugin-domain failures. Never carries a stack trace to the caller."""


class UnknownPlugin(PluginError):
    """Raised when a caller names a plugin that the code does not register.

    Checked only when `known_plugins` was supplied: the service cannot know the code-side
    registry by itself, and inventing one would re-create the two-lists problem (A3).
    """


class PluginService:
    def __init__(
        self, conn: sqlite3.Connection, *, known_plugins: Sequence[str] | None = None
    ) -> None:
        self._conn = conn
        self._known = tuple(known_plugins) if known_plugins is not None else None

    # -- version ---------------------------------------------------------------

    def tool_epoch(self) -> int:
        """Current tool-set version. Always readable: the schema seeds it at 1."""
        row = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (TOOL_EPOCH_KEY,)
        ).fetchone()
        if row is None:
            return DEFAULT_TOOL_EPOCH
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return DEFAULT_TOOL_EPOCH

    def _bump_tool_epoch(self) -> int:
        """Increment and return the new version. Caller owns the transaction."""
        current = self.tool_epoch() + 1
        self._conn.execute(
            "INSERT INTO kernel_meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "  updated_at = CURRENT_TIMESTAMP",
            (TOOL_EPOCH_KEY, str(current)),
        )
        return current

    # -- reads -----------------------------------------------------------------

    def list_plugins(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT plugin_id, display_name, enabled, config_json, sort_order "
            "FROM plugin ORDER BY sort_order, plugin_id"
        ).fetchall()
        return [
            {
                "plugin_id": row["plugin_id"],
                "display_name": row["display_name"],
                "enabled": bool(row["enabled"]),
                "config": json.loads(row["config_json"]) if row["config_json"] else None,
            }
            for row in rows
        ]

    def is_enabled(self, plugin_id: str) -> bool:
        row = self._conn.execute(
            "SELECT enabled FROM plugin WHERE plugin_id = ?", (plugin_id,)
        ).fetchone()
        return bool(row["enabled"]) if row else False

    def enabled_domains(self) -> list[str]:
        """The domains whose tools may be bound *right now*.

        Read live on every turn rather than snapshotted into session state. A snapshot would
        make "disable a plugin and it takes effect immediately" false - and a stale snapshot
        that still permits a disabled plugin's tools is a silent permission bug.
        """
        rows = self._conn.execute(
            "SELECT plugin_id FROM plugin WHERE enabled = 1 ORDER BY plugin_id"
        ).fetchall()
        return [str(row["plugin_id"]) for row in rows]

    # -- writes ----------------------------------------------------------------

    def register(
        self,
        plugin_id: str,
        *,
        display_name: str | None = None,
        enabled: bool = False,
        sort_order: int = 0,
    ) -> None:
        """Add a plugin row if absent. Does NOT change the state of an existing row.

        Deliberately not an upsert on `enabled`: re-running bootstrap must not switch a plugin
        back on after an operator turned it off.
        """
        self._check_known(plugin_id)
        self._conn.execute(
            "INSERT INTO plugin (plugin_id, display_name, enabled, sort_order) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(plugin_id) DO NOTHING",
            (plugin_id, display_name or plugin_id, 1 if enabled else 0, sort_order),
        )
        self._conn.commit()

    def set_enabled(self, plugin_id: str, enabled: bool, *, actor: str = "operator") -> int:
        """Toggle a plugin. Returns the new `tool_epoch`.

        The state change, the version bump and the audit row are one transaction: a bump without
        the change would make every live session think the tool set moved, and a change without
        the bump would hide it.
        """
        self._check_known(plugin_id)
        row = self._conn.execute(
            "SELECT enabled FROM plugin WHERE plugin_id = ?", (plugin_id,)
        ).fetchone()
        if row is None:
            raise UnknownPlugin(f"plugin not registered: {plugin_id}")

        before = bool(row["enabled"])
        if before == enabled:
            # No state change, so no version bump: re-enabling an enabled plugin must not
            # invalidate every live session's tool set.
            self._audit(actor, "plugin_noop", plugin_id, {"enabled": enabled})
            self._conn.commit()
            return self.tool_epoch()

        self._conn.execute(
            "UPDATE plugin SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE plugin_id = ?",
            (1 if enabled else 0, plugin_id),
        )
        epoch = self._bump_tool_epoch()
        self._audit(
            actor,
            "plugin_enable" if enabled else "plugin_disable",
            plugin_id,
            {"was": before, "now": enabled, "tool_epoch": epoch},
        )
        self._conn.commit()
        return epoch

    # -- internals -------------------------------------------------------------

    def _check_known(self, plugin_id: str) -> None:
        if self._known is not None and plugin_id not in self._known:
            known = ", ".join(self._known) or "none"
            raise UnknownPlugin(f"plugin not registered in code: {plugin_id} (known: {known})")

    def _audit(
        self, actor: str, action: str, target: str, detail: dict[str, object] | None = None
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit_log (actor, action, target, detail_json) VALUES (?, ?, ?, ?)",
            (
                actor,
                action,
                target,
                None if detail is None else json.dumps(detail, ensure_ascii=False),
            ),
        )
