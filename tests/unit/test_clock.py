"""`core/common/clock.py` 的政策与分族解析（"双时区体系"那一格的判据）。

为什么单给这一小段写用例：它守的是**形状**与**跨族可比**两件静默的东西。消息时间戳
从前是本地 naive 串（docstring 写着"自用单时区"），换 UTC 之后存量数据里两族并存 ——
任何一处"拿消息时间去比库表时间/拿新旧消息互减"的代码，族分错了不报错、结果错。
所以这里钉的是：新写入的形状（UTC ISO-Z）、两族各自的真实语义、以及**同一时刻
两种写法解析出同一瞬间**（跨族比较正确的充要判据，机器时区无关）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from rolecard_agent.core.agent.state import now_ts
from rolecard_agent.core.common import clock


def test_utc_now_shape_and_value() -> None:
    """新写入必须是 UTC ISO-Z：形状钉死（消费侧按它分族），值与真实 UTC 挂钟对得上。"""
    stamp = clock.utc_now()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp), stamp
    parsed = datetime.strptime(stamp, clock.TS_FORMAT).replace(tzinfo=UTC)
    drift = abs((datetime.now(UTC) - parsed).total_seconds())
    assert drift < 5, f"utc_now 与挂钟差 {drift}s —— 它量到别的地方去了"


def test_writer_goes_through_the_one_outlet() -> None:
    """state.now_ts（全部 8 个写点的入口）委托 clock —— 政策只有一个出处。"""
    stamp = now_ts()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp), stamp


def test_parse_new_format_is_utc() -> None:
    """纪元后的串（带 T）按 UTC 解：同样的钟面，无论本机在哪个时区，瞬间唯一。"""
    parsed = clock.parse_message_ts("2026-10-07T02:30:00Z")
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") == "2026-10-07 02:30:00"
    # 无 Z 的 T 串按政策也当 UTC（本模块不产它；容忍它是为了别把"差半格格式"读成另一个时区）。
    assert clock.parse_message_ts("2026-10-07T02:30:00") == parsed


def test_parse_legacy_format_is_local() -> None:
    """纪元前的串（空格分隔）按**本地** naive 解 —— 那是旧写法的真实语义。"""
    legacy = "2026-10-07 10:30:00"
    parsed = clock.parse_message_ts(legacy)
    assert parsed.tzinfo is not None
    expected = datetime.strptime(legacy, "%Y-%m-%d %H:%M:%S").astimezone()
    assert parsed == expected, "本地串必须按本机时区解，机器时区无关地成立"


def test_the_same_instant_parses_equal_across_families() -> None:
    """跨族可比的充要判据：同一时刻，旧写法与新写法解析出**同一瞬间**（±2s）。

    切版本瞬间一条线程里两族共存（升级前的用户消息 + 升级后的回答）：
    这条成立，"回答耗时"与任何未来的跨族比较才不是碰运气。
    """
    legacy_now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    utc_now = clock.utc_now()
    across = clock.parse_message_ts(utc_now) - clock.parse_message_ts(legacy_now)
    gap = abs(across.total_seconds())
    assert gap < 2, f"两族解析差 {gap}s —— 跨族比较不可信"
