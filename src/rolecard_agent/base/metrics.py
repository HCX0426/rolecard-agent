"""进程级事件计数 + Prometheus 文本渲染（零依赖，手写那十几行）。

2026-10-04 审查快照「无 metrics」那一格的后半（前半是深探那条 `/api/health/deep`）。
为什么**不引** `prometheus_client` / OTel 全家桶：这一格要的只是"把已经在发的事件按名字
数一数"，导出格式本身是十几行字符串拼接；全家桶会带进一个后台线程、一套注册表与一条版本
约束，而本仓的运维面只有一个进程（compose 里应用 + Caddy）。代价**如实记**：只有计数，
没有直方图 —— 耗时分布仍然去读 Tracer 的结构化日志（那是它本来就擅长的事）。

**只数事件名、不碰 `detail`** —— 这不是顺手省下的，是这一格的验收（"metrics 不含用户文本"）：
事件名是代码里的常量或形状固定的插值，而 `detail` 里可能有会话片段、正文、模型名、工具参数。

标签值另外过两道闸：`_safe_label`（白名单字符 + 长度上限）与基数上限（`MAX_SERIES`）。
第二道不是洁癖 —— **一个插值出来的事件名就能把时间序列撑爆**，而插值的来源是运行时数据
（Prometheus 侧的经典事故形状：单个租户把 exporter 打成内存黑洞）。
"""

from __future__ import annotations

import re
import threading

#: 事件名里只允许这些字符进标签值，其余折成 `_`。Prometheus 的标签值放得下任何 UTF-8，
#: 但"能放"不等于"该放"：插值出来的名字可能带着用户内容（`tool:读病历`）。
_SAFE = re.compile(r"[^A-Za-z0-9_]")
_LABEL_MAX = 64
#: 时间序列（不同的事件名）上限：超出的新名字一律并进 `other`，并记下丢了多少次。
MAX_SERIES = 200


def _safe_label(value: object) -> str:
    cleaned = _SAFE.sub("_", str(value))[:_LABEL_MAX]
    return cleaned or "empty"


class EventCounts:
    """按事件名的计数。**进程级一个**（模块底部的 `COUNTS`）—— 指标注册表本来就是进程级的。"""

    def __init__(self, *, max_series: int = MAX_SERIES) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._overflow = 0
        self._max_series = max_series

    def bump(self, event: str) -> None:
        """记一次事件。**绝不抛**：计数是观测面，不能反过来把请求打死。"""
        name = _safe_label(event)
        with self._lock:
            if name not in self._counts and len(self._counts) >= self._max_series:
                self._overflow += 1
                name = "other"
            self._counts[name] = self._counts.get(name, 0) + 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def render(self) -> str:
        """Prometheus 文本格式（`text/plain; version=0.0.4`）。

        只有一条时间序列族：`rolecard_trace_events_total{event="…"}`。名字带 `rolecard_`
        前缀是刻意的 —— exporter 与别的进程共用一个 scrape 配置时，无前缀的通用名会撞。
        """
        with self._lock:
            counts = dict(self._counts)
            overflow = self._overflow
        lines = [
            "# HELP rolecard_trace_events_total 进程内按事件名累计的轨迹事件数",
            "# TYPE rolecard_trace_events_total counter",
        ]
        lines += [
            f'rolecard_trace_events_total{{event="{name}"}} {counts[name]}'
            for name in sorted(counts)
        ]
        if overflow:
            # 注释行（`#` 开头）在文本格式里合法，Prometheus 直接忽略 —— 但人读得到。
            lines.append(
                f"# 另有 {overflow} 次事件因超出基数上限（{self._max_series}）并入 other"
            )
        return "\n".join(lines) + "\n"


#: 进程级注册表：`LocalTracer.emit` 每发一条事件就在这里 +1。
COUNTS = EventCounts()
