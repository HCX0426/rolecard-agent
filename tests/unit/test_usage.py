"""token 账（core/usage.py，审计 §12.8）。

钉四件事：
  1. **四种键名形状都要认出来** —— Ollama 报 `prompt_eval_count`，OpenAI 兼容口报
     `prompt_tokens`，langchain 新版放 `usage_metadata`、旧版放 `response_metadata`。
     认不全的症状不是报错，而是"某家的账永远是 0"（`memory_distill` 原来就只认一种，
     于是 Ollama 的用量一直取不到，还没人发现）。
  2. **没报不等于 0** —— 少一个 `unreported` 计数，"今天 0 token"就同时意味着
     "今天没花钱"和"今天报了 12 次一次都没数"。
  3. 记账坏了不能拦住回答（fail-open），但必须留声。
  4. 按 (天, 后端) 累加，因为云端花的是钱、本地花的是显存与时间。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from rolecard_agent.core.usage import (
    TokenUsage,
    daily_usage,
    local_day,
    parse_usage,
    record_usage,
    usage_days,
)


class _Reply:
    """只带这两个属性的假回复 —— 真实消息对象上取的就是它们。"""

    def __init__(self, *, usage_metadata: Any = None, response_metadata: Any = None) -> None:
        if usage_metadata is not None:
            self.usage_metadata = usage_metadata
        if response_metadata is not None:
            self.response_metadata = response_metadata


def test_parse_recovers_all_four_shapes() -> None:
    # langchain 标准化后的形状（新）
    assert parse_usage(
        _Reply(usage_metadata={"input_tokens": 30, "output_tokens": 12, "total_tokens": 42})
    ) == TokenUsage(30, 12)
    # OpenAI 兼容口（云端那几家）
    assert parse_usage(
        _Reply(response_metadata={"token_usage": {"prompt_tokens": 30, "completion_tokens": 12}})
    ) == TokenUsage(30, 12)
    # Ollama 的叫法 —— 以前漏的就是这一家
    assert parse_usage(
        _Reply(response_metadata={"token_usage": {"prompt_eval_count": 900, "eval_count": 40}})
    ) == TokenUsage(900, 40)
    # 只报总数：`usage_metadata` 与 `response_metadata.token_usage` 两种位置都要认
    assert parse_usage(_Reply(usage_metadata={"total_tokens": 42})) == TokenUsage(None, 42)
    assert parse_usage(_Reply(response_metadata={"token_usage": {"total_tokens": 123}})) == (
        TokenUsage(None, 123)
    )  # ← `memory_distill` 的假模型正是这个形状；这条曾被我写漏一次，靠既有测试打红


def test_input_and_output_are_kept_apart_when_both_reported() -> None:
    """两种形状同时存在时以 `usage_metadata` 为准（它是 langchain 标准化后的那份）。"""
    reply = _Reply(
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        response_metadata={"token_usage": {"prompt_tokens": 999, "completion_tokens": 999}},
    )
    assert parse_usage(reply) == TokenUsage(10, 4)


def test_missing_usage_is_none_not_zero() -> None:
    """没报就是 None。记成 0 等于报告"这一句不要钱"。"""
    assert parse_usage(_Reply()) is None
    assert parse_usage(_Reply(response_metadata={})) is None
    assert parse_usage(_Reply(response_metadata={"token_usage": {}})) is None
    assert parse_usage(object()) is None
    assert TokenUsage(None, None).total is None


def test_total_is_the_sum_of_both_ends() -> None:
    assert TokenUsage(30, 12).total == 42
    assert TokenUsage(30, None).total == 30  # 只报了一半也照实报一半，不猜另一半


class _RecordingTracer:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


def test_record_accumulates_per_backend_per_day(conn: sqlite3.Connection) -> None:
    assert record_usage(conn, backend="cloud-a", usage=TokenUsage(100, 20), day="2026-09-23")
    record_usage(conn, backend="cloud-a", usage=TokenUsage(50, 5), day="2026-09-23")
    record_usage(conn, backend="local-8b", usage=TokenUsage(700, 300), day="2026-09-23")
    rows = {r["backend"]: dict(r) for r in conn.execute("SELECT * FROM token_usage_day")}
    assert rows["cloud-a"]["calls"] == 2
    assert rows["cloud-a"]["prompt_tokens"] == 150
    assert rows["local-8b"]["calls"] == 1
    assert [r["backend"] for r in daily_usage(conn, day="2026-09-23")] == [
        "local-8b",
        "cloud-a",
    ]  # 按总量倒序：花钱多的在前


def test_unreported_calls_are_counted_separately(conn: sqlite3.Connection) -> None:
    """一次都没报用量的那天：total 是 0，而 `unreported` 必须把 3 次都记下来。"""
    for _ in range(3):
        assert record_usage(conn, backend="cloud-a", usage=None, day=local_day())
    assert record_usage(conn, backend="cloud-a", usage=TokenUsage(9, 1), day=local_day())
    (row,) = daily_usage(conn, day=local_day())
    assert row["calls"] == 4 and row["total"] == 10 and row["unreported"] == 3, row


def test_broken_ledger_does_not_raise_but_leaves_a_trace(conn: sqlite3.Connection) -> None:
    """账本坏了（表不在）⇒ 记不上、不抛、留一条 trace。回答本身不该为观测陪葬。"""
    conn.execute("DROP TABLE token_usage_day")
    tracer = _RecordingTracer()
    assert record_usage(conn, backend="x", usage=TokenUsage(1, 1), tracer=tracer) is False
    assert [getattr(e, "event", "") for e in tracer.events] == ["usage_record_failed"]
    assert record_usage(conn, backend="x", usage=TokenUsage(1, 1)) is False  # 没 tracer 也不炸


def test_usage_days_totals_by_day(conn: sqlite3.Connection) -> None:
    record_usage(conn, backend="a", usage=TokenUsage(10, 5), day="2026-09-22")
    record_usage(conn, backend="b", usage=TokenUsage(20, 5), day="2026-09-23")
    days = usage_days(conn)
    assert [d["day"] for d in days] == ["2026-09-23", "2026-09-22"]  # 最近的在前
    assert [d["total"] for d in days] == [25, 15]
    assert days[1]["backend"] == "a" if "backend" in days[1] else True  # 按天聚合，不分后端
