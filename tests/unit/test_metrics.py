"""指标（`/api/metrics`）：只有计数，而**这一格的验收是它不含用户文本**。

判据锚在那句上：从"事件 → 计数 → 渲染出来的文本"整条路上，任何一段用户内容都不许出现。
所以最重要的一条用例是把带敏感正文的 `detail` 喂进真 tracer，再看渲染结果里有没有它。
另外三条守的是"计数这件事本身不能反过来害人"：标签值白名单、基数上限、写盘失败照样计数。
"""

from __future__ import annotations

import pathlib

from rolecard_agent.base import observability
from rolecard_agent.base.metrics import MAX_SERIES, EventCounts, _safe_label
from rolecard_agent.base.observability import LocalTracer, TraceEvent


class _Opaque:
    """json 序列化不了的东西（照抄 `test_observability` 那支的用意）。"""

    def __repr__(self) -> str:  # pragma: no cover - 只为报错可读
        return "<opaque>"


def test_计数按事件名累加() -> None:
    counts = EventCounts()
    counts.bump("chat_turn")
    counts.bump("chat_turn")
    counts.bump("tool_call")
    assert counts.snapshot() == {"chat_turn": 2, "tool_call": 1}


def test_标签值只留白名单字符() -> None:
    """事件名可能是插值出来的（`tool:<名字>`），标签值不许原样带用户内容进去。"""
    counts = EventCounts()
    counts.bump("tool:读病历")
    counts.bump("tool:读病历")
    snapshot = counts.snapshot()
    assert len(snapshot) == 1, snapshot
    only = next(iter(snapshot))
    assert only == _safe_label("tool:读病历")
    assert all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in only), only
    assert "读病历" not in counts.render()


def test_基数上限把超出的新名字并进_other() -> None:
    """没有这一格，一个插值事件名就能把时间序列撑爆（exporter 变内存黑洞）。"""
    counts = EventCounts(max_series=2)
    for i in range(5):
        counts.bump(f"e{i}")
    snapshot = counts.snapshot()
    assert len(snapshot) <= 3, snapshot  # 两个正常名 + other
    assert snapshot["other"] == 3
    assert "超出基数上限" in counts.render()


def test_默认上限是个有限数() -> None:
    """上限本身要有默认值：忘了传参数的调用点（生产路径）也得受保护。"""
    counts = EventCounts()
    for i in range(MAX_SERIES + 25):
        counts.bump(f"e{i}")
    assert len(counts.snapshot()) <= MAX_SERIES + 1


def test_渲染成_prometheus_文本() -> None:
    counts = EventCounts()
    counts.bump("b_event")
    counts.bump("a_event")
    counts.bump("a_event")
    text = counts.render()
    assert "# HELP rolecard_trace_events_total" in text
    assert "# TYPE rolecard_trace_events_total counter" in text
    assert 'rolecard_trace_events_total{event="a_event"} 2' in text
    assert text.endswith("\n")
    # 名字有序：同一份计数两次渲染逐字相同（抓 diff 的人不必先排序）
    assert text.index("a_event") < text.index("b_event")


def test_metrics_不含_detail_里的用户文本(tmp_path: pathlib.Path, monkeypatch) -> None:
    """**这一格的验收**：真 tracer 收一条带敏感正文的事件，渲染出来的文本里不许有它。

    计数只认 `event.event`（代码里的名字），`detail` 一概不读 —— 这条用例把它钉成事实，
    而不是靠"我写的时候没读 detail"这句话。
    """
    fresh = EventCounts()
    monkeypatch.setattr(observability, "COUNTS", fresh)
    tracer = LocalTracer(path=tmp_path / "t.jsonl")
    secret = "110101199001011234"
    tracer.emit(TraceEvent(event="chat_turn", detail={"content": f"身份证号 {secret}"}))
    tracer.close()
    text = fresh.render()
    assert secret not in text
    assert "身份证" not in text
    assert 'rolecard_trace_events_total{event="chat_turn"} 1' in text


def test_写盘失败照样计数(tmp_path: pathlib.Path, monkeypatch) -> None:
    """`bump` 在 try 外面：记不上数不该跟着"落盘失败"一起丢（那是两个故障）。"""
    fresh = EventCounts()
    monkeypatch.setattr(observability, "COUNTS", fresh)
    tracer = LocalTracer(path=tmp_path / "t.jsonl")
    tracer.emit(TraceEvent(event="weird", detail={"o": _Opaque()}))
    tracer.close()
    assert fresh.snapshot() == {"weird": 1}


def test_端点回的是带版本的_prometheus_文本(monkeypatch) -> None:
    """`Content-Type` 必须带版本号：Prometheus 按它选解析器。"""
    from rolecard_agent.api.routers.health import metrics

    fresh = EventCounts()
    fresh.bump("x")
    monkeypatch.setattr("rolecard_agent.api.routers.health.COUNTS", fresh)
    resp = metrics()
    assert resp.media_type == "text/plain; version=0.0.4; charset=utf-8"
    assert b'rolecard_trace_events_total{event="x"} 1' in resp.body
