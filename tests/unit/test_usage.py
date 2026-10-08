"""token 账（core/common/usage.py，审计 §12.8）。

钉五件事：
  1. **四种键名形状都要认出来** —— Ollama 报 `prompt_eval_count`，OpenAI 兼容口报
     `prompt_tokens`，langchain 新版放 `usage_metadata`、旧版放 `response_metadata`。
     认不全的症状不是报错，而是"某家的账永远是 0"（`memory_distill` 原来就只认一种，
     于是 Ollama 的用量一直取不到，还没人发现）。
  2. **没报不等于 0** —— 少一个 `unreported` 计数，"今天 0 token"就同时意味着
     "今天没花钱"和"今天报了 12 次一次都没数"。
  3. 记账坏了不能拦住回答（fail-open），但必须留声。
  4. 按 (天, 后端) 累加，因为云端花的是钱、本地花的是显存与时间。
  5. **走图（流式）那条路记的是真值** —— 分块每块都带累计 usage，取最后一次，
     且工具循环里每次调用各记一笔（文件末尾那三条）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.core.agent.turn import run_turn
from rolecard_agent.core.common.usage import (
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


def test_reasoning_is_split_out_of_completion_but_stays_a_subset() -> None:
    """思考模型：`reasoning` 单独认出来，但**总量仍然是 prompt + completion**。

    实测过的那条"在吗"回 616 个输出 token、其中 590 是 reasoning（审计 §12.8 第二条）。
    这一列存在的唯一理由就是能问出"今天花的钱里有多少在想"，而它不能把合计撑大 ——
    想的那 590 本来就在那 616 里。两家键名各认一次：漏一家就是"这一路永远显示 0 想"。
    """
    lc = parse_usage(
        _Reply(
            usage_metadata={
                "input_tokens": 12,
                "output_tokens": 616,
                "total_tokens": 628,
                "output_token_details": {"reasoning": 590},
            }
        )
    )
    assert lc == TokenUsage(12, 616, 590)
    assert lc.total == 628, "reasoning 不能进 total"
    openai = parse_usage(
        _Reply(
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 616,
                    "completion_tokens_details": {"reasoning_tokens": 590},
                }
            }
        )
    )
    assert openai == TokenUsage(12, 616, 590)
    # 没报 details 的普通模型：这一栏是 None，不是 0（0 = 报过、确实没想；None = 不知道）
    assert parse_usage(
        _Reply(usage_metadata={"input_tokens": 5, "output_tokens": 7})
    ) == TokenUsage(5, 7, None)


def test_streaming_path_carries_reasoning_too() -> None:
    """流式那条路不走 `parse_usage`（分块累计值被逐块相加污染过，见 #8），所以它得单独认。"""
    from rolecard_agent.core.common.usage import usage_from_metadata

    assert usage_from_metadata(
        {"input_tokens": 12, "output_tokens": 616, "output_token_details": {"reasoning": 590}}
    ) == TokenUsage(12, 616, 590)


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


def test_reasoning_accumulates_as_its_own_column(conn: sqlite3.Connection) -> None:
    """日表里「想」单独一列、跟着调用累加，而 `total` 一点没被它撑大。"""
    record_usage(conn, backend="cloud-a", usage=TokenUsage(12, 616, 590), day="2026-09-23")
    record_usage(conn, backend="cloud-a", usage=TokenUsage(8, 84, 60), day="2026-09-23")
    (row,) = daily_usage(conn, day="2026-09-23")
    assert (row["completion"], row["reasoning"], row["total"]) == (700, 650, 720)
    # 没报 details 的那些调用不能把这一栏变成 0 假象之外的东西：累加里它就是 0 贡献。
    record_usage(conn, backend="cloud-a", usage=TokenUsage(1, 1), day="2026-09-23")
    assert daily_usage(conn, day="2026-09-23")[0]["reasoning"] == 650


def test_unreported_calls_are_counted_separately(conn: sqlite3.Connection) -> None:
    """一次都没报用量的那天：total 是 0，而 `unreported` 必须把 3 次都记下来。"""
    for _ in range(3):
        assert record_usage(conn, backend="cloud-a", usage=None, day=local_day())
    assert record_usage(conn, backend="cloud-a", usage=TokenUsage(9, 1), day=local_day())
    (row,) = daily_usage(conn, day=local_day())
    assert row["calls"] == 4 and row["total"] == 10 and row["unreported"] == 3, row


def test_two_identities_keep_separate_ledgers(conn: sqlite3.Connection) -> None:
    """账本按花谁的 key 分格（多租户 B1a）：同一天同一后端，两个身份各一格。

    `user_id=None` 的读法保持"今天总共花了多少"（合并）；按人读时互不串。
    """
    record_usage(conn, backend="cloud-a", usage=TokenUsage(100, 20), day="2026-09-23", user_id="u1")
    record_usage(
        conn, backend="cloud-a", usage=TokenUsage(50, 5), day="2026-09-23", user_id=DEFAULT_USER_ID
    )
    # 同一个 (day, user_id, backend) 照旧累加
    record_usage(conn, backend="cloud-a", usage=TokenUsage(10, 2), day="2026-09-23", user_id="u1")
    u1 = daily_usage(conn, day="2026-09-23", user_id="u1")
    mine = daily_usage(conn, day="2026-09-23", user_id=DEFAULT_USER_ID)
    assert u1[0]["total"] == 132  # (100+20) + (10+2)
    assert mine[0]["total"] == 55  # (50+5)
    # 同一天同一后端若两个身份都花了钱，合并读数会出两行（各一格）—— 这正是"不混账"。
    merged_rows = daily_usage(conn, day="2026-09-23")
    assert sum(r["total"] for r in merged_rows) == 132 + 55
    assert len(merged_rows) == 2, "两个人各一格，而不是并成一格"


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
    record_usage(conn, backend="c", usage=TokenUsage(1, 1), day="2026-09-23")
    days = usage_days(conn)
    assert [d["day"] for d in days] == ["2026-09-23", "2026-09-22"]  # 最近的在前
    # 同一天、不同后端要合成一个数：问"今天花了多少"的人没打算按后端各问一遍。
    assert [d["total"] for d in days] == [27, 15]
    assert days[0]["calls"] == 2


# -- 流式那条路：账上的数必须是真值（#8）---------------------------------------
#
# `core/agent/nodes.py` 里合并出来的那份 usage 是 真值 × 分块数，所以记账搬到了
# `core/agent/turn.py`（只有那儿看得见单个分块）。下面这三条钉的就是搬过去之后仍然成立的
# 那三件事：取最后一次累计、每次调用各记一笔、没报也要算一次调用。


class _ChunkStream:
    """假图：照 langgraph `messages` 模式的样子发块，每块带一份"累计到此"的 usage。

    这就是 #8 的成因本身：供应商逐块回累计值，langchain 合并 `AIMessageChunk` 时把
    `usage_metadata` 逐块相加 —— 实测一条"在吗"非流式 26 token、流式合并后 272,607。
    """

    def __init__(self, calls: int = 1, chunks: int = 3, *, report: bool = True) -> None:
        self.calls = calls
        self.chunks = chunks
        self.report = report

    def stream(self, *_a: Any, **_kw: Any) -> Any:
        for step in range(1, self.calls + 1):
            for i in range(self.chunks):
                # 三个键都得给：langchain 把 `usage_metadata` 当 TypedDict 校（`total_tokens`
                # 少一个就在构造期抛），而构造点在 `run_turn` 的 try 里 —— 异常会被翻成一条
                # error 事件并把整条流打断，测试于是看到"一次用量都没记上"。
                usage = (
                    {"input_tokens": 100, "output_tokens": i + 1, "total_tokens": 101 + i}
                    if self.report
                    else None
                )
                yield "messages", (
                    AIMessageChunk(content="嗯", usage_metadata=usage),
                    {"langgraph_node": "call_model", "langgraph_step": step},
                )
            yield "updates", {"call_model": {"messages": [AIMessage(content="嗯" * self.chunks)]}}


class _Sink:
    def __init__(self) -> None:
        self.usages: list[Any] = []

    def __call__(self, usage: Any) -> None:
        self.usages.append(usage)


def _run(graph: Any, sink: _Sink, tracer: _RecordingTracer) -> None:
    list(
        run_turn(
            graph,
            graph_input={},
            config={},
            role_summary={"role_id": "r", "role_name": "流式测试角色"},
            tracer=tracer,
            usage_recorder=sink,
        )
    )


def test_streamed_usage_is_the_last_cumulative_not_the_sum() -> None:
    """三个分块各报 1/2/3 输出 token ⇒ 记 3，不是 6；输入那份累计 100 也只记一次。"""
    sink, tracer = _Sink(), _RecordingTracer()
    _run(_ChunkStream(calls=1, chunks=3), sink, tracer)

    assert sink.usages == [TokenUsage(100, 3)], "合并值 300/6 就是那条被夸大的账"
    events = [e for e in tracer.events if getattr(e, "event", "") == "llm_usage"]
    assert len(events) == 1
    assert events[0].tokens == 103
    assert events[0].detail["prompt_tokens"] == 100


def test_each_model_call_in_a_tool_loop_is_ledged_separately() -> None:
    """一次用户轮次里两次模型调用（工具循环）⇒ 两笔账、两个 step，不能合成一笔。

    合成一笔会把"这一轮问了几次模型"这个数丢掉，而它正是排查递归与成本时的第一个问题。
    """
    sink, tracer = _Sink(), _RecordingTracer()
    _run(_ChunkStream(calls=2, chunks=2), sink, tracer)

    assert sink.usages == [TokenUsage(100, 2), TokenUsage(100, 2)]
    steps = [e.detail["step"] for e in tracer.events if e.event == "llm_usage"]
    assert steps == [1, 2]


def test_a_stream_that_never_reports_usage_still_counts_the_call(
    conn: sqlite3.Connection,
) -> None:
    """后端一个 token 数都不报 ⇒ 仍要记"发生过一次调用、没有数"。

    少了这一笔，`calls` 会静悄悄地跟着 `total` 一起变成 0，"今天 0 token"就又恢复了
    两种含义（没花钱 / 报了账但没数）—— 那正是 `unreported` 这一列存在的理由。
    """
    sink, tracer = _Sink(), _RecordingTracer()
    _run(_ChunkStream(calls=1, chunks=3, report=False), sink, tracer)

    assert sink.usages == [None]
    for usage in sink.usages:
        assert record_usage(conn, backend="cloud-a", usage=usage, day=local_day())
    (row,) = daily_usage(conn, day=local_day())
    assert row["calls"] == 1 and row["total"] == 0 and row["unreported"] == 1, row
