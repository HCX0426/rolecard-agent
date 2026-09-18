"""DTOs: RoleCard, RoleCardCreate, RoleCardUpdate.

Reproducing a role takes four channels, not one. Two belong to the role card, one to the
kernel, one to the plugin layer:

  人设规则    system_prompt        rules and boundaries
  行为范例    exemplars            how the role answers - the strongest lever per token
  能力权限    tool_whitelist       what it may do
  事实知识    knowledge_scopes     which retrieval scopes it may read (declared, not owned)

Fields of note:

  is_builtin        built-in roles cannot be deleted (D6)
  model_name        a backend NAME from config.MODEL_BACKENDS, not a raw model id - this is
                    what makes per-role routing work (medical role -> local, chat -> cloud)
  tool_whitelist    None = all tools of enabled plugins, [] = no tools at all
  knowledge_scopes  None = no retrieval, [] = no retrieval, [..] = those collections only.
                    A role never owns a vector store; N roles x M stores would duplicate
                    indexes and leave no single source of truth.
  temperature       a SUGGESTION, not a guarantee. Some reasoning models ignore it entirely,
                    and its valid range differs per provider. Never let downstream logic
                    depend on it being honoured (C15).

Both lists are stored as JSON for simplicity in v1. Trade-off to state out loud in the
interview: it costs queryability ("which roles can use tool X" needs a full scan);
production would split them into join tables.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, field_validator

ROLE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
SCOPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

# Exemplar budget. Chars are used rather than tokens because it needs no tokenizer
# dependency, and for Chinese text a character budget is a conservative proxy. The cap is
# not bureaucracy: examples compete with the conversation for context, and past a handful the
# marginal example mostly dilutes the earlier ones.
MAX_EXEMPLARS = 4
MAX_EXEMPLAR_CHARS = 3000


class RoleExemplar(BaseModel):
    """One worked example of how this role answers."""

    user: str = Field(min_length=1, max_length=1000)
    assistant: str = Field(min_length=1, max_length=2000)


def _check_exemplar_budget(value: list[RoleExemplar] | None) -> list[RoleExemplar] | None:
    if value is None:
        return None
    if len(value) > MAX_EXEMPLARS:
        raise ValueError(f"at most {MAX_EXEMPLARS} exemplars, got {len(value)}")
    total = sum(len(item.user) + len(item.assistant) for item in value)
    if total > MAX_EXEMPLAR_CHARS:
        raise ValueError(f"exemplars total {total} chars, budget is {MAX_EXEMPLAR_CHARS}")
    return value


def _check_scopes(value: list[str] | None) -> list[str] | None:
    """Scope names become collection identifiers - validate rather than trust."""
    if value is None:
        return None
    for name in value:
        if not re.match(SCOPE_PATTERN, name):
            raise ValueError(f"invalid knowledge scope name: {name!r}")
    return value


ExemplarList = Annotated[list[RoleExemplar] | None, AfterValidator(_check_exemplar_budget)]
ScopeList = Annotated[list[str] | None, AfterValidator(_check_scopes)]


class RoleCard(BaseModel):
    """A role as stored. `is_builtin` roles cannot be deleted."""

    role_id: str = Field(pattern=ROLE_ID_PATTERN)
    role_name: str = Field(min_length=1)
    system_prompt: str
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    model_name: str | None = None
    tool_whitelist: list[str] | None = None
    exemplars: ExemplarList = None
    knowledge_scopes: ScopeList = None
    description: str | None = None
    # 主动开口（架构计划 B）：角色是否会主动来找用户（还需要全局 REACHOUT_ENABLED 开着）。
    # 默认 False = 出厂即静默 —— 主动打扰是 opt-in，不是默认行为。
    reachout_enabled: bool = False
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

    def allows_scope(self, scope: str) -> bool:
        """Retrieval authorisation. `None` means no retrieval at all - the opposite default
        from `allows_tool`, and deliberately so: reading stored documents is a widen-the-
        blast-radius action, so it is opt-in."""
        if not self.knowledge_scopes:
            return False
        return scope in self.knowledge_scopes


class RoleCardCreate(BaseModel):
    """Input for creating a role. `is_builtin` is intentionally absent - it is set by
    `roles/seed.py`, never by a caller."""

    role_id: str = Field(pattern=ROLE_ID_PATTERN)
    role_name: str = Field(min_length=1)
    system_prompt: str
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    model_name: str | None = None
    tool_whitelist: list[str] | None = None
    exemplars: ExemplarList = None
    knowledge_scopes: ScopeList = None
    description: str | None = None
    reachout_enabled: bool = False

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
    exemplars: ExemplarList = None
    knowledge_scopes: ScopeList = None
    description: str | None = None
    reachout_enabled: bool | None = None

    def changes(self) -> dict[str, object]:
        """Only the fields the caller actually provided."""
        return {name: getattr(self, name) for name in self.model_fields_set}

    def is_empty(self) -> bool:
        return not self.model_fields_set
