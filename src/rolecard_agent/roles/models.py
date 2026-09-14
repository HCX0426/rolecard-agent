"""DTOs: RoleCard, RoleCardCreate, RoleCardUpdate.

Fields of note:

  is_builtin      built-in roles cannot be deleted (D6)
  model_name      a backend NAME from config.MODEL_BACKENDS, not a raw model id - this is
                  what makes per-role routing work (medical role -> local, chat -> cloud)
  tool_whitelist  None = all tools of enabled plugins, [] = no tools at all
  temperature     a SUGGESTION, not a guarantee. Some reasoning models ignore it entirely,
                  and its valid range differs per provider. Never let downstream logic
                  depend on it being honoured (C15).

tool_whitelist is stored as a JSON array for simplicity in v1. Trade-off to state out loud
in the interview: it costs queryability ("which roles can use tool X" needs a full scan);
production would split it into a role_tool join table.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

ROLE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class RoleCard(BaseModel):
    """A role as stored. `is_builtin` roles cannot be deleted."""

    role_id: str = Field(pattern=ROLE_ID_PATTERN)
    role_name: str = Field(min_length=1)
    system_prompt: str
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    model_name: str | None = None
    tool_whitelist: list[str] | None = None
    description: str | None = None
    is_builtin: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def allows_tool(self, tool_name: str) -> bool:
        """Whitelist semantics, in one place.

        `None` means "everything the enabled plugins expose". `[]` means "nothing" - the
        distinction matters and is easy to get backwards, so it is expressed once here and
        every caller goes through it.
        """
        if self.tool_whitelist is None:
            return True
        return tool_name in self.tool_whitelist


class RoleCardCreate(BaseModel):
    """Input for creating a role. `is_builtin` is intentionally absent - it is set by
    `roles/seed.py`, never by a caller."""

    role_id: str = Field(pattern=ROLE_ID_PATTERN)
    role_name: str = Field(min_length=1)
    system_prompt: str
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    model_name: str | None = None
    tool_whitelist: list[str] | None = None
    description: str | None = None

    @field_validator("role_id")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        if value != value.lower():
            raise ValueError("role_id must be lowercase ASCII")
        return value


class RoleCardUpdate(BaseModel):
    """Partial update. Unset fields are left untouched - use `model_fields_set` to tell
    "not provided" apart from "explicitly set to null"."""

    role_name: str | None = Field(default=None, min_length=1)
    system_prompt: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    model_name: str | None = None
    tool_whitelist: list[str] | None = None
    description: str | None = None

    def changes(self) -> dict[str, object]:
        """Only the fields the caller actually provided."""
        return {name: getattr(self, name) for name in self.model_fields_set}

    def is_empty(self) -> bool:
        return not self.model_fields_set
