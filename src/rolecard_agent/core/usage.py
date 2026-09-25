"""模型调用的 token 账（审计 §12.8）。

为什么要有这个模块：路由改成云端之后，对话、主动开口、记忆提取三条路都在花真钱，
而项目里**没有任何一个数**能回答"今天花了多少"。`node_end` 事件的 `tokens` 字段一直存在、
一直是 null；`reachout_sent` 只带 `chars`（字数不是钱）。

三件事，都在这一个文件里：

  1. `parse_usage` —— 从一次回复里取 token 数。两家后端的键名不一样（Ollama 是
     `prompt_eval_count`/`eval_count`，OpenAI 兼容口是 `prompt_tokens`/`completion_tokens`），
     而且 langchain 新旧两版分别放在 `usage_metadata` 与 `response_metadata["token_usage"]`。
     这四个形状必须**一处**认全：在两个地方各写一半键名，就是"某家的数永远是 0"那种 bug。
     流式那条路**不走** `parse_usage`（它拿到的合并值被逐块相加污染过，见 #8），而是用
     `usage_from_metadata` 在分块层按 (节点, 步) 取最后一次累计 —— 认键的规矩两边共用一个。
  2. `record_usage` —— 按 (本地日期, 后端) 累计。不落库的账等于没有账：
     trace 默认写 stderr（`OBS_LOG_PATH` 没配就哪儿都不留），而 `persona_meter.py` 这类
     只读尺子读的是 sqlite。
  3. `daily_usage` —— 读数。按后端分行，因为"云端花了多少"与"本地花了多少"是两件不同的事
     （本地不花钱但花显存与时间）。

**拿不到就是拿不到**：`parse_usage` 返回 None，`record_usage` 只累加 `calls`，
不编一个 0 当成"这次没花钱"——那会让一天的真实开销看起来是零。

表只有一行/天/后端，所以这张表永远不会成为运维问题（对比：把每次调用都写成一行审计，
就是给一个只增不减的表加噪声）。
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, NamedTuple

from rolecard_agent.core.observability import TraceEvent
from rolecard_agent.storage.db import SqlConnection


class TokenUsage(NamedTuple):
    """一次调用的用量。每个字段都是"后端报了才有"，没报就是 None（不是 0）。

    `reasoning` 是思考模型输出里"想"的那一段，**它是 `completion` 的子集**（不是第三种开销），
    所以 `total` 仍然只加输入与输出。单列它的唯一理由是：一条"在吗"回 616 个输出 token
    其中 590 是想出来的（架构审计 §12.8 第二条）—— 没有这一列，账只能回答"今天花了多少"，
    回答不了"其中多少是想出来的"，而"这个思考值不值"就是没法判。
    """

    prompt: int | None
    completion: int | None
    reasoning: int | None = None

    @property
    def total(self) -> int | None:
        parts = [v for v in (self.prompt, self.completion) if v is not None]
        return sum(parts) if parts else None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _reasoning_of(container: Any) -> int | None:
    """从一份 `*_token_details` 里认"想"的那个键。两家的键名不同，认不出一律给 None。

    单独一处的理由与 `parse_usage` 同一个：**键名认两遍就是两处事实面**，一处加了另一处
    没加，症状就是"这一路的 reasoning 永远是 0"，而没有任何东西会红。
    """
    if not isinstance(container, dict):
        return None
    for key in ("reasoning", "reasoning_tokens"):
        found = _as_int(container.get(key))
        if found is not None:
            return found
    return None


def parse_usage(reply: Any) -> TokenUsage | None:
    """从模型回复里认全四种形状；一个都没认出来给 None。"""
    meta = getattr(reply, "usage_metadata", None)
    if isinstance(meta, dict):
        prompt = _as_int(meta.get("input_tokens"))
        completion = _as_int(meta.get("output_tokens"))
        reasoning = _reasoning_of(meta.get("output_token_details"))
        if prompt is not None or completion is not None:
            return TokenUsage(prompt, completion, reasoning)
        total = _as_int(meta.get("total_tokens"))
        if total is not None:
            return TokenUsage(None, total, reasoning)

    usage = ((getattr(reply, "response_metadata", None) or {}).get("token_usage")) or {}
    if not isinstance(usage, dict):
        return None
    prompt = _as_int(usage.get("prompt_tokens"))
    if prompt is None:
        prompt = _as_int(usage.get("prompt_eval_count"))  # Ollama 的叫法
    completion = _as_int(usage.get("completion_tokens"))
    if completion is None:
        completion = _as_int(usage.get("eval_count"))  # 同上
    reasoning = _reasoning_of(usage.get("completion_tokens_details"))
    total = _as_int(usage.get("total_tokens"))
    if completion is None and prompt is not None and total is not None:
        completion = max(0, total - prompt)
    if prompt is None and completion is None:
        # **只报总数**这一种形状必须认得出来（`{"token_usage": {"total_tokens": 123}}`）。
        # 第一版在这里漏了分支，直接把 123 报成 None —— 而 `memory_distill` 一直依赖它，
        # 于是一次改写的回归把一条既有测试打红了。总数记在 completion 上：
        # 宁可标"分不清输入输出"，也不能把已知的量丢掉。
        return TokenUsage(None, total, reasoning) if total is not None else None
    return TokenUsage(prompt, completion, reasoning)


def usage_from_metadata(meta: Any) -> TokenUsage | None:
    """把 langchain 的 `usage_metadata`（`input_tokens`/`output_tokens`）转成 TokenUsage。

    给"手上已经是一份 usage 字典、没有消息对象"的调用方用 —— 流式那条路径就是这么取的：
    每个增量块都带**累计值**，所以要按调用取**最后一次**出现的（第一次的 output 恒为 0）。
    """
    if not isinstance(meta, dict):
        return None
    prompt = _as_int(meta.get("input_tokens"))
    completion = _as_int(meta.get("output_tokens"))
    reasoning = _reasoning_of(meta.get("output_token_details"))
    if prompt is None and completion is None:
        total = _as_int(meta.get("total_tokens"))
        return TokenUsage(None, total, reasoning) if total is not None else None
    return TokenUsage(prompt, completion, reasoning)


def local_day(now: datetime | None = None) -> str:
    """记账用的"天"：**本地**日期。问"今天花了多少"的人用的是他自己的日历，不是 UTC。"""
    return (now or datetime.now()).strftime("%Y-%m-%d")


def record_usage(
    conn: SqlConnection,
    *,
    backend: str | None,
    usage: TokenUsage | None = None,
    calls: int = 1,
    day: str | None = None,
    tracer: Any = None,
) -> bool:
    """把一次调用的用量累进 (今天, 这个后端)。返回 False = 没记上（本轮照样该走完）。

    失败为什么不抛：这条账是**观测**，不是业务规则。它坏了最贵的代价是"某天少了一条调用"，
    而抛出去的代价是用户这一句话没回答完 —— 两件事不在一个量级上。
    但它**必须留下声音**（`usage_record_failed`）：一条没人看见的坏账本比没有账本更糟，
    那会让人以为"今天没花 token"。
    """
    stamp = day or local_day()
    reported = usage is not None and (usage.prompt is not None or usage.completion is not None)
    try:
        conn.execute(
            """
            INSERT INTO token_usage_day (
                day, backend, calls, prompt_tokens, completion_tokens,
                reasoning_tokens, unreported
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, backend) DO UPDATE SET
                calls = calls + excluded.calls,
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                unreported = unreported + excluded.unreported
            """,
            (
                stamp,
                backend or "",
                calls,
                (usage.prompt if usage else None) or 0,
                (usage.completion if usage else None) or 0,
                (usage.reasoning if usage else None) or 0,
                0 if reported else calls,  # "没数"是要单独记的一件事，不是 0 token
            ),
        )
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 见上面那条取舍
        if tracer is not None:
            with contextlib.suppress(Exception):  # 留声本身坏了就到此为止
                tracer.emit(
                    TraceEvent(
                        event="usage_record_failed",
                        node="usage",
                        detail={"error": type(exc).__name__, "day": stamp, "calls": calls},
                    )
                )
        return False


def daily_usage(conn: SqlConnection, *, day: str | None = None) -> list[dict[str, Any]]:
    """某天各后端的累计（按总量倒序）。没数据给空表。"""
    rows = conn.execute(
        "SELECT backend, calls, prompt_tokens, completion_tokens, reasoning_tokens, unreported "
        "FROM token_usage_day WHERE day = ? "
        "ORDER BY (prompt_tokens + completion_tokens) DESC, backend",
        (day or local_day(),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        prompt = int(row["prompt_tokens"] or 0)
        completion = int(row["completion_tokens"] or 0)
        out.append(
            {
                "backend": str(row["backend"] or "默认"),
                "calls": int(row["calls"] or 0),
                "prompt": prompt,
                "completion": completion,
                # 子集，不进 total：读的人要的是"其中想"，不是第三段账单。
                "reasoning": int(row["reasoning_tokens"] or 0),
                "total": prompt + completion,
                "unreported": int(row["unreported"] or 0),
            }
        )
    return out


def usage_days(conn: SqlConnection, *, limit: int = 14) -> list[dict[str, Any]]:
    """最近若干天每天的总量（含"报了多少次没报"的 calls）—— 给"一天多少 token"那一问。"""
    rows = conn.execute(
        "SELECT day, SUM(calls) AS calls, SUM(prompt_tokens) AS prompt, "
        "SUM(completion_tokens) AS completion, SUM(reasoning_tokens) AS reasoning, "
        "SUM(unreported) AS unreported "
        "FROM token_usage_day GROUP BY day ORDER BY day DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "day": str(r["day"]),
            "calls": int(r["calls"] or 0),
            "prompt": int(r["prompt"] or 0),
            "completion": int(r["completion"] or 0),
            "reasoning": int(r["reasoning"] or 0),
            "total": int(r["prompt"] or 0) + int(r["completion"] or 0),
            "unreported": int(r["unreported"] or 0),
        }
        for r in rows
    ]


__all__ = [
    "TokenUsage",
    "daily_usage",
    "local_day",
    "parse_usage",
    "record_usage",
    "usage_days",
    "usage_from_metadata",
]
