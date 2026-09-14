"""Kernel tools, domain-agnostic: list_domains / list_roles.

DECISION (resolves D2 vs the old placeholder, logged as C5):

    switch_role is NOT bound to the model.

Letting the model change its own permission boundary is self-authorization - a prompt
injection could switch the session into a role that has more tools. Role switching is an
API / UI action that writes session_thread.current_role_id (see core/schema.sql).

Role switching still preserves history: only the role id changes, never the messages.

search_knowledge is NOT here - it belongs to rag/ and is registered in v2.1.
"""
