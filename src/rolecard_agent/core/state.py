"""Graph state."""
# TODO: TypedDict AgentState:
#         messages         Annotated[list, add_messages]
#         thread_id        str
#         user_id          str        - who is talking; drives memory isolation & authz
#         current_role_id  str        - resolved to prompt + temperature + tool whitelist
#         enabled_domains  list[str]  - snapshot of the plugin switch at this step
#         tool_epoch       int        - version stamp of the enabled tool set
#         retry_count      int
#
# tool_epoch exists because the tool set is filtered dynamically while the graph is
# compiled once. If a plugin is disabled mid-session, historical messages may still
# carry tool_calls for tools that no longer exist; the executor must answer
# "this capability is offline" instead of raising (C14).
#
# NOTE: the system prompt is NOT part of messages and never enters the checkpoint.
# It is reassembled every turn as GLOBAL_SAFETY_PROMPT + role.system_prompt
# (D3).
