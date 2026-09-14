"""Observability: the tracer must be useful and must never be dangerous.

Two properties matter and neither was tested before (技术评审与决策.md §9 D1):

  * it never raises - a logging failure must not take down a request;
  * it redacts by default - the whole point of putting redaction *inside* the tracer is that a
    caller cannot forget it.
"""

from __future__ import annotations

import json
from pathlib import Path

from rolecard_agent.config import Settings
from rolecard_agent.core.observability import (
    IMPLEMENTED_BACKENDS,
    LocalTracer,
    NullTracer,
    TraceEvent,
    make_tracer,
    redact,
    timer,
)


def _lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_local_tracer_writes_one_json_line_per_event(tmp_path: Path) -> None:
    log = tmp_path / "trace.jsonl"
    tracer = LocalTracer(path=log)
    tracer.emit(TraceEvent(event="node_end", node="call_model", latency_ms=12.5))
    tracer.emit(TraceEvent(event="tool_call", tool="list_roles"))

    records = _lines(log)
    assert [r["event"] for r in records] == ["node_end", "tool_call"]
    assert records[0]["latency_ms"] == 12.5


def test_redaction_is_on_by_default(tmp_path: Path) -> None:
    """A 412-character answer must not appear in the log; its shape may."""
    log = tmp_path / "trace.jsonl"
    LocalTracer(path=log).emit(
        TraceEvent(event="node_end", detail={"content": "患者" * 6, "tools_visible": 2})
    )
    detail = _lines(log)[0]["detail"]
    assert detail["content"] == "<redacted:12 chars>"
    assert detail["tools_visible"] == 2  # non-sensitive values survive


def test_redaction_can_be_disabled_for_fictional_demo_data(tmp_path: Path) -> None:
    log = tmp_path / "trace.jsonl"
    LocalTracer(emit_raw_text=True, path=log).emit(
        TraceEvent(event="node_end", detail={"content": "虚构数据"})
    )
    assert _lines(log)[0]["detail"]["content"] == "虚构数据"


def test_redact_walks_nested_structures() -> None:
    out = redact({"outer": {"answer": "secret"}, "keep": 3})
    assert out == {"outer": {"answer": "<redacted:6 chars>"}, "keep": 3}


def test_local_backend_is_silent(tmp_path: Path) -> None:
    log = tmp_path / "trace.jsonl"
    make_tracer(Settings(obs_backend="local", obs_log_path=log))
    assert _lines(log) == []


def test_unimplemented_backend_falls_back_loudly(tmp_path: Path) -> None:
    """Silently downgrading would let someone believe they have cloud traces."""
    log = tmp_path / "trace.jsonl"
    make_tracer(Settings(obs_backend="langsmith", obs_log_path=log))

    records = _lines(log)
    assert len(records) == 1
    assert records[0]["event"] == "tracer_fallback"
    assert records[0]["detail"]["requested"] == "langsmith"
    assert records[0]["detail"]["using"] == "local"
    assert records[0]["detail"]["planned_in"] == "v2.4"


def test_typo_in_backend_name_does_not_raise(tmp_path: Path) -> None:
    tracer = make_tracer(Settings(obs_backend="langsmit", obs_log_path=tmp_path / "t.jsonl"))
    assert isinstance(tracer, LocalTracer)


def test_only_local_is_implemented() -> None:
    assert IMPLEMENTED_BACKENDS == ("local",)


def test_emit_never_raises_on_unserializable_detail(tmp_path: Path) -> None:
    """A tracer that can crash a request is worse than no tracer."""

    class Opaque:
        def __repr__(self) -> str:
            raise RuntimeError("no repr for you")

    LocalTracer(path=tmp_path / "t.jsonl").emit(TraceEvent(event="x", detail={"o": Opaque()}))
    # reaching here is the assertion


def test_null_tracer_swallows_everything() -> None:
    assert NullTracer().emit(TraceEvent(event="anything")) is None


def test_timer_reports_elapsed_milliseconds() -> None:
    with timer() as box:
        pass
    assert box["ms"] >= 0.0
