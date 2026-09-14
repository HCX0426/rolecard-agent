"""Env-driven configuration.

Same codebase, three deployment topologies - only the config differs:
  A all-local | B app on cloud + model at home | C all-cloud.
Ollama exposes an OpenAI-compatible endpoint (http://localhost:11434/v1), so a "local model"
and a "cloud API" are the SAME provider to this code - only base_url changes.
"""
# TODO: pydantic-settings Settings
#   MODEL_BACKENDS   {"local": {base_url, model}, "cloud": {base_url, model, api_key}}
#                    Referenced BY NAME from config and from role_card.model_name, so adding
#                    a provider is one line - same registry pattern as domains/registry.py.
#   MODEL_FALLBACKS  ordered backend names, MAX 2 (see docs/实施计划.md section 8.5)
#   SQLITE_PATH / CHROMA_PATH / UPLOAD_DIR
#   OBS_BACKEND      "local" (default, works offline) | "langsmith" | "langfuse"
#   OBS_EMIT_RAW_TEXT  bool, default false = redacted. When false the tracer emits only
#                      thread_id / role / node / tool / latency / tokens - never raw
#                      document text or index values.
#   MODEL_DEFAULT    name of the backend to use when the role does not override it
