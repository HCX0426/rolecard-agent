"""DTOs: RoleCard, RoleCardCreate, RoleCardUpdate.

Fields of note:

  is_builtin      built-in roles cannot be deleted (D6)
  model_name      a backend NAME from config.MODEL_BACKENDS, not a raw model id - this is
                  what makes per-role routing work (medical role -> local, chat -> cloud)
  tool_whitelist  None = all tools of enabled plugins, [] = no tools at all
  temperature     a SUGGESTION, not a guarantee. Some reasoning models ignore it entirely,
                  and its valid range differs per provider. Never let downstream logic
                  depend on it being honoured (C15).

tool_whitelist is stored as a JSON array for simplicity in v1. Trade-off to state out
loud in the interview: it costs queryability ("which roles can use tool X" needs a full
scan); production would split it into a role_tool join table.
"""
