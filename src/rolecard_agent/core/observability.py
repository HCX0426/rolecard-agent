"""Observability facade with switchable backends.

local     : structured JSON logs -> stdout / file. Zero deps, fully offline. DEFAULT.
langsmith : enabled only when LANGSMITH_API_KEY is present.
langfuse  : optional self-hosted backend for public deployment that must not ship data out.

Contract: emit only {thread_id, user_id, role_id, node, tool, arg_digest, latency_ms,
tokens, error}. Raw document text and index values are emitted ONLY when
OBS_EMIT_RAW_TEXT is true (default false = redacted).
"""
