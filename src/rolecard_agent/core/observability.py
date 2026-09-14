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
    """Replace sensitive payloads with a length marker, keeping everything else readable.

    Redaction is keyed, not value-based: only values under a known-sensitive key are replaced.
    The first implementation redacted *every* string, which also destroyed the diagnostic
    fields - `requested: "langsmith"` became `<redacted:9 chars>` - and made the logs useless.
    A redactor that removes too much is not safer; it just gets switched off.

    Keeps shape (so "there was a 412-char answer" is still visible) while removing content (so
    nothing about a real person lands in a log line).
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in _SENSITIVE_KEYS and isinstance(item, str):
                out[key] = f"<redacted:{len(item)} chars>"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    # Bare strings outside a sensitive key are diagnostic text. `error` is deliberately in
    # this group: an exception type and message are what make a trace actionable, and tools
    # are contracted not to put raw record text into exceptions.
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


KNOWN_BACKENDS = ("local", "langsmith", "langfuse")
# Only `local` exists today. Stated as data rather than prose so the fallback message and any
# future check can agree on it.
IMPLEMENTED_BACKENDS = ("local",)


def make_tracer(settings: Settings) -> Tracer:
    """Build the tracer for `settings.obs_backend`.

    A request for an unimplemented backend does NOT raise - failing to start because of a
    logging setting would be worse than losing cloud traces - but it is **not silent**
    either: a `tracer_fallback` event is emitted so the substitution is visible.

    Silently downgrading would be the dangerous option: someone who sets
    `OBS_BACKEND=langsmith` plus a key and gets local JSON lines would believe they have cloud
    traces. The previous version of this function had two identical branches behind an `if`,
    which read as though the cloud backends were already wired (技术评审与决策.md §9 A1).
    """
    tracer = LocalTracer(emit_raw_text=settings.obs_emit_raw_text, path=settings.obs_log_path)
    if settings.obs_backend not in IMPLEMENTED_BACKENDS:
        tracer.emit(
            TraceEvent(
                event="tracer_fallback",
                detail={
                    "requested": settings.obs_backend,
                    "using": "local",
                    "implemented": list(IMPLEMENTED_BACKENDS),
                    "planned_in": "v2.4" if settings.obs_backend in KNOWN_BACKENDS else None,
                },
            )
        )
    return tracer


@contextmanager
def timer() -> Iterator[dict[str, float]]:
    """Measure a block:  `with timer() as t: ...  t["ms"]`"""
    started = time.perf_counter()
    box = {"ms": 0.0}
    try:
        yield box
    finally:
        box["ms"] = (time.perf_counter() - started) * 1000.0
