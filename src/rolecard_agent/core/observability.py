"""Observability facade with switchable backends.

local     : structured JSON logs -> stdout / file. Zero deps, fully offline. DEFAULT.
langsmith : enabled only when LANGSMITH_API_KEY is present.
langfuse  : optional self-hosted backend for public deployment that must not ship data out.

Contract: emit only {thread_id, user_id, role_id, node, tool, latency_ms, tokens, error}.
Raw document text and index values are emitted ONLY when OBS_EMIT_RAW_TEXT is true
(default false = redacted).

The facade is deliberately thin. v1 ships the `local` backend (JSON lines, zero deps, works
fully offline); the cloud backends are added later behind the same `Tracer` protocol, so no
caller has to change. Redaction happens inside the tracer rather than at the call sites -
if it were the caller's job, one forgotten call would leak a medical record.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from rolecard_agent.config import Settings

# Anything a caller might pass under these keys is dropped unless raw emission is enabled.
_SENSITIVE_KEYS = frozenset(
    {"input", "output", "text", "content", "answer", "raw_text", "index_value", "messages"}
)


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One structured log line. Unknown/extra context goes in `detail`."""

    event: str
    thread_id: str | None = None
    user_id: str | None = None
    role_id: str | None = None
    node: str | None = None
    tool: str | None = None
    latency_ms: float | None = None
    tokens: int | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class Tracer(Protocol):
    """Single exit point for observability. Implementations must never raise."""

    def emit(self, event: TraceEvent) -> None: ...


def redact(value: Any) -> Any:
    """Replace sensitive payloads with a length/summary marker.

    Keeps shape (so logs remain debuggable: "there was a 412-char answer") while removing
    content (so nothing about a real person ends up in a log line).
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key] = (
                f"<redacted:{len(item)} chars>"
                if key in _SENSITIVE_KEYS and isinstance(item, str)
                else redact(item)
            )
        return out
    if isinstance(value, str):
        return f"<redacted:{len(value)} chars>"
    return value


class LocalTracer:
    """JSON-lines tracer. Default backend: no network, no extra dependencies."""

    def __init__(self, *, emit_raw_text: bool = False, path: Path | None = None) -> None:
        self._emit_raw = emit_raw_text
        self._path = path
        self._stream: Any = None

    def _target(self) -> Any:
        if self._path is None:
            return sys.stderr
        if self._stream is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self._path.open("a", encoding="utf-8")
        return self._stream

    def emit(self, event: TraceEvent) -> None:
        try:
            payload = asdict(event)
            if not self._emit_raw:
                payload["detail"] = redact(payload.get("detail") or {})
            line = json.dumps(payload, ensure_ascii=False, default=str)
            stream = self._target()
            stream.write(line + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 - observability must never break the request
            pass


class NullTracer:
    """Used by tests that assert on behaviour rather than on logs."""

    def emit(self, event: TraceEvent) -> None:  # noqa: ARG002
        return None


def make_tracer(settings: Settings) -> Tracer:
    """Build the tracer named by `settings.obs_backend`.

    Unknown backends fall back to `local` with no error: losing tracing is annoying, failing
    to start because of a typo in an env var would be worse.
    """
    if settings.obs_backend == "local":
        return LocalTracer(emit_raw_text=settings.obs_emit_raw_text, path=settings.obs_log_path)
    return LocalTracer(emit_raw_text=settings.obs_emit_raw_text, path=settings.obs_log_path)


@contextmanager
def timer() -> Iterator[dict[str, float]]:
    """Measure a block:  `with timer() as t: ...  t["ms"]`"""
    started = time.perf_counter()
    box = {"ms": 0.0}
    try:
        yield box
    finally:
        box["ms"] = (time.perf_counter() - started) * 1000.0
