"""Observability: the tracer must be useful and must never be dangerous.  Traceability: US-5.

Two properties matter and neither was tested before (技术评审与决策.md §9 D1):

  * it never raises - a logging failure must not take down a request;
  * it redacts by default - the whole point of putting redaction *inside* the tracer is that a
    caller cannot forget it.
"""

from __future__ import annotations

import json
from pathlib import Path

from rolecard_agent.base.observability import (
    IMPLEMENTED_BACKENDS,
    LocalTracer,
    NullTracer,
    TraceEvent,
    make_tracer,
    redact,
    timer,
)
from rolecard_agent.config import Settings


def _lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_local_tracer_writes_one_json_line_per_event(tmp_path: Path) -> None:
    """US-5: one JSON line per event, offline-capable local backend."""
    log = tmp_path / "trace.jsonl"
    tracer = LocalTracer(path=log)
    tracer.emit(TraceEvent(event="node_end", node="call_model", latency_ms=12.5))
    tracer.emit(TraceEvent(event="tool_call", tool="list_roles"))
    tracer.close()  # 句柄显式收：留着就是 GC 时一句 ResourceWarning

    records = _lines(log)
    assert [r["event"] for r in records] == ["node_end", "tool_call"]
    assert records[0]["latency_ms"] == 12.5


def test_redaction_is_on_by_default(tmp_path: Path) -> None:
    """A 412-character answer must not appear in the log; its shape may."""
    log = tmp_path / "trace.jsonl"
    tracer = LocalTracer(path=log)
    tracer.emit(
        TraceEvent(event="node_end", detail={"content": "患者" * 6, "tools_visible": 2})
    )
    tracer.close()
    detail = _lines(log)[0]["detail"]
    assert detail["content"] == "<redacted:12 chars>"
    assert detail["tools_visible"] == 2  # non-sensitive values survive


def test_redaction_can_be_disabled_for_fictional_demo_data(tmp_path: Path) -> None:
    log = tmp_path / "trace.jsonl"
    tracer = LocalTracer(emit_raw_text=True, path=log)
    tracer.emit(TraceEvent(event="node_end", detail={"content": "虚构数据"}))
    tracer.close()
    assert _lines(log)[0]["detail"]["content"] == "虚构数据"


def test_redact_walks_nested_structures() -> None:
    out = redact({"outer": {"answer": "secret"}, "keep": 3})
    assert out == {"outer": {"answer": "<redacted:6 chars>"}, "keep": 3}


def test_local_backend_is_silent(tmp_path: Path) -> None:
    log = tmp_path / "trace.jsonl"
    make_tracer(Settings(obs_backend="local", obs_log_path=log)).close()
    assert _lines(log) == []


def test_unimplemented_backend_falls_back_loudly(tmp_path: Path) -> None:
    """Silently downgrading would let someone believe they have cloud traces."""
    log = tmp_path / "trace.jsonl"
    make_tracer(Settings(obs_backend="langsmith", obs_log_path=log)).close()

    records = _lines(log)
    assert len(records) == 1
    assert records[0]["event"] == "tracer_fallback"
    assert records[0]["detail"]["requested"] == "langsmith"
    assert records[0]["detail"]["using"] == "local"
    assert records[0]["detail"]["planned_in"] == "v2.4"


def test_typo_in_backend_name_does_not_raise(tmp_path: Path) -> None:
    tracer = make_tracer(Settings(obs_backend="langsmit", obs_log_path=tmp_path / "t.jsonl"))
    assert isinstance(tracer, LocalTracer)
    tracer.close()


def test_only_local_is_implemented() -> None:
    assert IMPLEMENTED_BACKENDS == ("local",)


def test_emit_never_raises_on_unserializable_detail(tmp_path: Path) -> None:
    """A tracer that can crash a request is worse than no tracer."""

    class Opaque:
        def __repr__(self) -> str:
            raise RuntimeError("no repr for you")

    tracer = LocalTracer(path=tmp_path / "t.jsonl")
    tracer.emit(TraceEvent(event="x", detail={"o": Opaque()}))
    tracer.close()
    # reaching here is the assertion


def test_null_tracer_swallows_everything() -> None:
    assert NullTracer().emit(TraceEvent(event="anything")) is None


def test_timer_reports_elapsed_milliseconds() -> None:
    with timer() as box:
        pass
    assert box["ms"] >= 0.0


# -- 线程安全与日志轮转（代码审查报告（第二轮）M7） --------------------------------


def test_concurrent_emits_produce_only_valid_json_lines(tmp_path: Path) -> None:
    """并发 emit 不能把日志写成互相交错的残行。

    `emit` 会被 FastAPI 线程池、图执行、工具执行并发调用，而"写入整行"不是原子操作。
    修复前无锁：两行日志可以交错成无法解析的残行 —— 恰好在需要排查问题的时候让日志失效。
    """
    import threading

    path = tmp_path / "trace.log"
    tracer = LocalTracer(path=path)
    n_threads, per_thread = 8, 50

    def worker(idx: int) -> None:
        for i in range(per_thread):
            tracer.emit(TraceEvent(event=f"e{idx}-{i}", detail={"payload": "x" * 200}))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    tracer.close()  # 收句柄：留着就是 GC 时一句 ResourceWarning

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * per_thread  # 一行不多、一行不少
    for line in lines:  # 每一行都必须是完整可解析的 JSON
        assert json.loads(line)["event"]


def test_log_file_rotates_instead_of_growing_forever(tmp_path: Path) -> None:
    """日志按大小轮转，保留一个 `.1` 备份 —— 长跑进程里唯一会无限增长的东西。"""
    path = tmp_path / "trace.log"
    tracer = LocalTracer(path=path)
    tracer._MAX_LOG_BYTES = 512  # type: ignore[misc]  # 便于用小数据触发轮转
    for i in range(40):
        tracer.emit(TraceEvent(event=f"e{i}", detail={"pad": "y" * 100}))
    tracer.close()

    assert path.exists()
    assert path.with_name(path.name + ".1").exists()
    assert path.stat().st_size <= 512 * 2  # 轮转之后不会失控


def test_background_emits_to_stderr_do_not_rotate(tmp_path: Path) -> None:
    """没有配置路径（默认写 stderr）时不该尝试轮转 —— 没有文件可轮。"""
    tracer = LocalTracer()
    tracer._MAX_LOG_BYTES = 1  # type: ignore[misc]
    tracer.emit(TraceEvent(event="still-works"))  # 不抛即通过


# -- 端点脱敏（代码审查报告（第二轮）A5 / M11） --------------------------------------


def test_scrub_endpoints_hides_urls_in_audit_text() -> None:
    """`/api/audit` 是前端可见的接口，入库的异常文本不该带着内网地址。"""
    from rolecard_agent.base.observability import scrub_endpoints

    raw = "ExtractError: 模型调用失败：Connection refused to http://localhost:11434/api/chat"
    cleaned = scrub_endpoints(raw)
    assert "http://localhost:11434" not in cleaned
    assert "<endpoint>" in cleaned
    assert "Connection refused" in cleaned  # 诊断信息保留
    assert scrub_endpoints(None) is None  # 非字符串原样返回
