"""StreamingGuard 的单元测试 —— 流式输出审核的三个不变量。

Traceability: US-4（输出侧安全）在流式场景下的延续。整段审核由 `core/guard.check` 覆盖
（test_guard.py），这里只验证增量语义：

  1. 尾部保留窗之前的已审文本正常流出；
  2. 违规模式**完整地**出现在累计文本中的那一刻起，不再外发任何内容 ——
     已外发的文本整体再过一遍 check() 仍然 allowed（无完整违规模式）；
  3. blocked 之后永久静默，等待权威改写文本。
"""

from __future__ import annotations

from rolecard_agent.api.chat import WINDOW, StreamingGuard
from rolecard_agent.core.guard import check

# 足够长（代码点数 > WINDOW），保证在违规片段到达前已经有文本流出。
SAFE_PREFIX = "好的，我查到了你 2026-03-12 的报告：结石直径 6.0 mm，区间 0-5 mm，数据如下，"


def test_safe_text_streams_up_to_the_window() -> None:
    assert len(SAFE_PREFIX) > WINDOW  # 前提自检：短于窗口的前缀测不出"扣尾"行为
    g = StreamingGuard()
    delta = g.feed(SAFE_PREFIX)
    # 已流出 = 保留窗之前的前缀；尾部被扣住等待更多上下文
    assert delta == SAFE_PREFIX[: len(SAFE_PREFIX) - WINDOW]
    tail = g.flush_tail()
    assert delta + tail == SAFE_PREFIX


def test_short_text_emits_nothing_until_window_passes() -> None:
    g = StreamingGuard()
    assert g.feed("你好") == ""  # 少于窗口长度：一律扣住
    assert g.flush_tail() == "你好"  # 轮次结束且通过审核后补发


def test_violation_blocks_and_emitted_text_still_passes_full_check() -> None:
    g = StreamingGuard()
    emitted: list[str] = []
    for chunk in (SAFE_PREFIX, "这种情况建议你服用", "阿司匹林。"):
        emitted.append(g.feed(chunk))
    assert g.blocked
    joined = "".join(emitted)
    # 核心不变量：已外发的文本不含任何**完整**违规模式（整段 check 仍然通过）
    assert check(joined).allowed
    assert "服用" not in joined
    assert "建议" not in joined


def test_blocked_guard_stays_silent_until_reset() -> None:
    g = StreamingGuard()
    g.feed(SAFE_PREFIX)
    g.feed("建议你服用阿司匹林")
    assert g.blocked
    assert g.feed("后续内容也不许外发") == ""
    assert g.flush_tail() == ""
    g.reset()
    assert not g.blocked
    assert g.buffer == ""
    assert g.emitted == 0
