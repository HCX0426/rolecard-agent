"""FastAPI app - v1 milestone M4, not deferred to v2 (C20).

Endpoints:
    POST /api/chat            SSE-streamed conversation, keyed by thread_id
    GET  /api/roles           role card list
    POST /api/roles           create / update role
    POST /api/session/role    switch role for a thread (operator action, NOT an LLM tool)
    GET  /api/plugins         plugin list with enabled flag
    POST /api/plugins/toggle  enable / disable a domain plugin
    POST /api/upload          file intake entry point (stub in v1, real in v2.2)

Serves a single-page chat UI as static files; that page is what the 60s demo video
records.
"""
