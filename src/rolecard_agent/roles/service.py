"""Role card CRUD + whitelist resolution.

Returns Pydantic models and raises typed errors - callers never see sqlite3.Row, and no SQL
leaks upward into the agent layer.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from rolecard_agent.roles.models import RoleCard, RoleCardCreate, RoleCardUpdate
from rolecard_agent.roles.seed import BUILTIN_ROLES

_COLUMNS = (
    "role_id, role_name, system_prompt, temperature, model_name, "
    "tool_whitelist, description, is_builtin, created_at, updated_at"
)


class RoleError(Exception):
    """Base for role-domain failures. Never carries a stack trace to the caller."""


class RoleNotFound(RoleError):
    pass


class RoleAlreadyExists(RoleError):
    pass


class BuiltinRoleProtected(RoleError):
    """Raised when a caller tries to delete a role whose `is_builtin` flag is set.

    This is a guardrail, not a policy: losing the last role would leave the system with
    nothing to run a conversation as.
    """


def _row_to_model(row: sqlite3.Row) -> RoleCard:
    payload = dict(row)
    raw = payload.get("tool_whitelist")
    payload["tool_whitelist"] = None if raw is None else json.loads(raw)
    payload["is_builtin"] = bool(payload.get("is_builtin"))
    return RoleCard(**payload)


def _dump_whitelist(value: list[str] | None) -> str | None:
    # None and [] mean different things (all vs none), so both round-trip distinctly.
    return None if value is None else json.dumps(value, ensure_ascii=False)


class RoleCardService:
    """CRUD over `role_card`, plus the operator-side role switch.

    Role switching lives here rather than in `core/tools/builtin.py` on purpose: it must NOT
    be reachable by the model, so it is a service call that the API/UI layer invokes - never
    a bound tool (D2 / C5).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- reads ---------------------------------------------------------------

    def list_roles(self) -> list[RoleCard]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM role_card ORDER BY is_builtin DESC, role_id"
        ).fetchall()
        return [_row_to_model(r) for r in rows]

    def get(self, role_id: str) -> RoleCard:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM role_card WHERE role_id = ?", (role_id,)
        ).fetchone()
        if row is None:
            raise RoleNotFound(f"role not found: {role_id}")
        return _row_to_model(row)

    def exists(self, role_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM role_card WHERE role_id = ?", (role_id,)).fetchone()
        return row is not None

    # -- writes --------------------------------------------------------------

    def create(self, data: RoleCardCreate, *, is_builtin: bool = False) -> RoleCard:
        if self.exists(data.role_id):
            raise RoleAlreadyExists(f"role already exists: {data.role_id}")
        self._conn.execute(
            "INSERT INTO role_card "
            "(role_id, role_name, system_prompt, temperature, model_name, "
            " tool_whitelist, description, is_builtin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.role_id,
                data.role_name,
                data.system_prompt,
                data.temperature,
                data.model_name,
                _dump_whitelist(data.tool_whitelist),
                data.description,
                1 if is_builtin else 0,
            ),
        )
        self._conn.commit()
        return self.get(data.role_id)

    def update(self, role_id: str, data: RoleCardUpdate) -> RoleCard:
        changes = data.changes()
        if "tool_whitelist" in changes:
            changes["tool_whitelist"] = _dump_whitelist(data.tool_whitelist)
        if not changes:
            return self.get(role_id)  # nothing to do; still validate existence

        assignments = ", ".join(f"{name} = ?" for name in changes)
        params = [*changes.values(), role_id]
        cur = self._conn.execute(
            f"UPDATE role_card SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE role_id = ?",
            params,
        )
        if cur.rowcount == 0:
            raise RoleNotFound(f"role not found: {role_id}")
        self._conn.commit()
        return self.get(role_id)

    def delete(self, role_id: str) -> None:
        role = self.get(role_id)  # raises RoleNotFound if absent
        if role.is_builtin:
            raise BuiltinRoleProtected(f"built-in role cannot be deleted: {role_id}")
        self._conn.execute("DELETE FROM role_card WHERE role_id = ?", (role_id,))
        self._conn.commit()

    # -- seeding -------------------------------------------------------------

    def seed_builtins(self, roles: Iterable[RoleCardCreate] = BUILTIN_ROLES) -> int:
        """Upsert built-in roles. Idempotent, so `init_db.py` can run on every boot.

        Upsert rather than create: a shipped change to a built-in role's prompt should reach
        existing databases without a migration step.
        """
        count = 0
        for role in roles:
            self._conn.execute(
                "INSERT INTO role_card "
                "(role_id, role_name, system_prompt, temperature, model_name, "
                " tool_whitelist, description, is_builtin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(role_id) DO UPDATE SET "
                "  role_name = excluded.role_name, "
                "  system_prompt = excluded.system_prompt, "
                "  temperature = excluded.temperature, "
                "  model_name = excluded.model_name, "
                "  tool_whitelist = excluded.tool_whitelist, "
                "  description = excluded.description, "
                "  is_builtin = 1, "
                "  updated_at = CURRENT_TIMESTAMP",
                (
                    role.role_id,
                    role.role_name,
                    role.system_prompt,
                    role.temperature,
                    role.model_name,
                    _dump_whitelist(role.tool_whitelist),
                    role.description,
                ),
            )
            count += 1
        self._conn.commit()
        return count

    # -- session-level role switch (operator action, not an LLM tool) --------

    def set_thread_role(self, thread_id: str, role_id: str, *, actor: str = "system") -> None:
        """Point a thread at a different role WITHOUT touching its message history.

        Only `current_role_id` changes. The next turn's system prompt is assembled from the
        new role, while the stored messages remain untouched - that is what "switching a role
        preserves context" actually means in this design.
        """
        self.get(role_id)  # fail before writing if the target role does not exist
        cur = self._conn.execute(
            "UPDATE session_thread SET current_role_id = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE thread_id = ?",
            (role_id, thread_id),
        )
        if cur.rowcount == 0:
            raise RoleNotFound(f"thread not found: {thread_id}")
        self.audit(actor=actor, action="switch_role", target=thread_id, detail={"role_id": role_id})
        self._conn.commit()

    def current_thread_role(self, thread_id: str) -> str:
        row = self._conn.execute(
            "SELECT current_role_id FROM session_thread WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise RoleNotFound(f"thread not found: {thread_id}")
        return str(row["current_role_id"])

    # -- audit ---------------------------------------------------------------

    def audit(
        self,
        *,
        actor: str,
        action: str,
        target: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Append to `audit_log`. Required by US-3 for role switches and plugin toggles."""
        self._conn.execute(
            "INSERT INTO audit_log (actor, action, target, detail_json) VALUES (?, ?, ?, ?)",
            (
                actor,
                action,
                target,
                None if detail is None else json.dumps(detail, ensure_ascii=False),
            ),
        )
        self._conn.commit()
