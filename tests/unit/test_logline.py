"""人读日志唯一出口 `logline` 的形状（2026-10-04 审查快照"三条日志通道并存"收口的回归钉子）。

三条通道并存时的病不是难看，是**排障要先猜消息在哪条**：17 处裸 `print`（stdout/stderr
混着来）+ `core/tools/mcp.py` 一份英文 stdlib logging + Tracer 的结构化事件。收口后：

  * 结构化事件归 `Tracer.emit`（机器读，本文件不碰）；
  * 人读的一句话归 `logline(level, event, text)` —— 这三支用例钉的就是它的**分流与格式**，
    因为 16 处迁移全靠"级档决定走哪条流"这个约定成立（schema-migrate 事件必须仍在 stderr，
    R102-64 的旧约定由 `notice` 档承接）。
"""

from __future__ import annotations

import pytest

from rolecard_agent.base.observability import logline


def test_info_goes_to_stdout_with_level_and_event_prefix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`info`/`debug` 走 stdout，行形是 `[级档] [事件名] 内容`（前缀可 grep）。"""
    logline("info", "checkpoints", "归还尾部 3 个空页")
    captured = capsys.readouterr()
    assert "[info] [checkpoints] 归还尾部 3 个空页" in captured.out
    assert captured.err == "", "info 不该写 stderr（一次 readouterr 同时看两条流）"


def test_notice_warning_error_and_typos_all_go_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """迁移事件（`notice`）与告警落 stderr —— R102-64"迁移事件落 stderr"的旧约定。

    **拼错的档也落 stderr 而不是抛异常**：这个函数被包在 `except` 里调（迁移失败、审批线程
    死掉），日志函数自己炸是比漏一条日志更坏的故障；档名按字面打进那一行，第一眼就能看出
    拼错了。
    """
    for level in ("notice", "warning", "error", "warn_typo"):
        logline(level, "schema-migrate", f"内容 {level}")
        captured = capsys.readouterr()
        assert f"[{level}] [schema-migrate] 内容 {level}" in captured.err, level
        assert captured.out == "", f"{level} 不该写 stdout"


def test_a_broken_level_never_raises_inside_an_except_block() -> None:
    """就算调用方把它放进 `except` 里，日志自己也不许成为第二个异常。"""
    try:
        raise RuntimeError("原异常")
    except RuntimeError:
        logline("nonsense-level", "approvals", "兜底也要出声")  # 不许抛
