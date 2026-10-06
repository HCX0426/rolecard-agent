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

import contextlib
import json
import re
import sys
import threading
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


#: 人读日志走 stdout 的级档（其余一律 stderr）。`notice` 这一档是为迁移事件留的
#: （R102-64"迁移事件落 stderr"的旧约定由它承接 —— 拆成两处各自 print(file=sys.stderr)
#: 才是那条约定当初要防的"第二个出口"）。
_LOG_STDOUT_LEVELS = frozenset({"debug", "info"})


def logline(level: str, event: str, text: str) -> None:
    """**人读日志的唯一出口**（2026-10-04 审查快照"三条日志通道并存"那一格收口）。

    从前这个仓库有三条并存的人读日志通道：Tracer 的结构化事件（机器读）、17 处裸 `print`
    （各写各的前缀，stdout/stderr 混着来）、`core/tools/mcp.py` 里一份 stdlib `logging`
    （英文、又是另一套格式）。三条并存的代价不是难看，是**排障时要先猜消息在哪条通道**：
    "Ollama 挂起"那次读日志就得人肉合流。收口之后分工是两句话：

      * **结构化事件归 `Tracer.emit`**（机器读：审计、仪表、按 event 过滤）；
      * **人读的一句话归这里** —— 一行 = 级档 + 事件名 + 内容，`[{level}] [{event}] {text}`。

    级档决定走哪条流：`debug`/`info` → stdout；`notice`/`warning`/`error` **以及任何拼错的
    档** → stderr。拼错不炸是刻意的：这个函数会被包在 `except` 里调（迁移失败、审批线程
    死掉），日志函数自己抛异常是比漏一条日志更坏的故障；拼错的档按字面打出来，第一行就
    能看出档名不对。

    门禁 `log channels unified`（`check_consistency.py`）看着两条纪律：src 里除本文件
    （唯一写手，1 处）与 `storage/db.py`（4 处豁免，理由在那张表里）外不许再有裸 `print`；
    stdlib `logging` 在 src 归零。**`scripts/` 不在此列** —— 那些是一次性取证与启动脚本，
    `print` 就是它们的输出界面。
    """
    stream = sys.stdout if level in _LOG_STDOUT_LEVELS else sys.stderr
    print(f"[{level}] [{event}] {text}", file=stream, flush=True)


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


# 异常文本里的 http(s) 端点会被换成占位符。
# 为什么需要：`/api/audit` 是**前端可见**的只读接口，而入库的异常文本常常带着内部
# base_url（httpx 的连接错误尤其如此）。审计台是给运营看的，不是给"任何能打开控制台的人"
# 看内网拓扑的（审查报告 A5 / M11）。
_ENDPOINT_RE = re.compile(r"https?://[^\s'\"()（）]+")


def scrub_endpoints(text: Any) -> Any:
    """把字符串里的 http(s) 地址替换成 `<endpoint>`；非字符串原样返回。"""
    if not isinstance(text, str):
        return text
    return _ENDPOINT_RE.sub("<endpoint>", text)


class LocalTracer:
    """JSON-lines tracer. Default backend: no network, no extra dependencies.

    线程安全（审查报告 M7）：`emit` 会被 FastAPI 线程池、图执行、工具执行并发调用，
    而"写入整行"不是原子操作 —— 旧实现无锁，两行日志可能交错成无法解析的残行，
    恰恰在需要排查问题的时候让日志失效。懒开文件句柄本身也有竞态（两个线程同时
    判断 `self._stream is None`），所以初始化和写入放在同一把锁里。

    文件按 `_MAX_LOG_BYTES` 轮转（保留一个 `.1` 备份）：日志是长跑进程里唯一会
    无限增长的东西，无上限的日志文件最终会变成运维故障。
    """

    _MAX_LOG_BYTES = 8 * 1024 * 1024

    def __init__(self, *, emit_raw_text: bool = False, path: Path | None = None) -> None:
        self._emit_raw = emit_raw_text
        self._path = path
        self._stream: Any = None
        self._lock = threading.Lock()

    def _target_locked(self) -> Any:
        """取（必要时打开）输出流。**调用方必须已持锁。**"""
        if self._path is None:
            return sys.stderr
        if self._stream is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self._path.open("a", encoding="utf-8")
        return self._stream

    def _rotate_locked(self, stream: Any) -> Any:
        """超限则轮转一次。失败时原样返回当前流（轮转不该让日志断掉）。"""
        if self._path is None:
            return stream
        try:
            if stream.tell() < self._MAX_LOG_BYTES:
                return stream
        except (OSError, ValueError):
            return stream
        try:
            stream.close()
            backup = self._path.with_name(self._path.name + ".1")
            with contextlib.suppress(OSError):
                backup.unlink()
            self._path.rename(backup)
        except OSError:
            pass
        self._stream = self._path.open("a", encoding="utf-8")
        return self._stream

    def close(self) -> None:
        """收掉文件句柄（幂等）。长跑进程由解释器退出时收，但**调用方要能显式收**：

        没有这一格时，测试里每个 `LocalTracer(path=…)` 都会在 GC 那一刻留下一句
        `ResourceWarning: unclosed file` —— 那正是"警告摘要"里最该没有的一族噪声
        （真有句柄没关的地方会被它一起淹掉）。轮转那条路上的旧句柄本来就是显式
        close 的（`_rotate_locked`），所以这只收"当前这一条"。
        """
        with self._lock:
            if self._stream is not None:
                with contextlib.suppress(OSError):
                    self._stream.close()
                self._stream = None

    def emit(self, event: TraceEvent) -> None:
        try:
            payload = asdict(event)
            if not self._emit_raw:
                payload["detail"] = redact(payload.get("detail") or {})
            line = json.dumps(payload, ensure_ascii=False, default=str)
            with self._lock:
                stream = self._rotate_locked(self._target_locked())
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
