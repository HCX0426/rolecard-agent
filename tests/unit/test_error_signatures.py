"""P2-① 错误签名（观测最小版的第一格）。

契约（开发流程.md「未完善清单」P2 行的验收）：
* 未捕获异常**计数 + 签名**（异常类型 + 顶部栈帧哈希）追加写 `error.jsonl`；
* `/api/health` 带 `error_count_recent`（24h 窗口内计数，进程内维护，重启归零——
  历史要查 jsonl，health 只管"现在要不要看一眼"）；
* 隐私面：jsonl 行里只有签名与类型/栈帧文件名行号，**没有消息文本、没有用户内容**
  （审计只记路径不记内容 的同族纪律；异常消息可能带病历正文）。

签名 = sha1(异常类型 + 最近一帧 file:line) 前 12 位 —— 同一处炸第二次的签名必须相同，
不同处必须不同；消息里带 id 不影响签名。
"""

from __future__ import annotations

import json
from pathlib import Path

from rolecard_agent.base import error_signatures as es


def _boom() -> None:
    raise ValueError("boom")


def test_signature_is_stable_and_frame_specific(tmp_path: Path) -> None:
    """同一处再炸 = 同签名；换一处炸 = 不同签名（判据是栈帧，不是消息）。"""
    es.reset_for_tests(tmp_path / "error.jsonl")
    try:
        _boom()
    except ValueError as exc:
        s1 = es.record(exc)
    try:
        _boom()
    except ValueError as exc:
        s2 = es.record(exc)
    assert s1 == s2
    try:
        raise ValueError("boom")  # 本行，不同文件行号
    except ValueError as exc:
        s3 = es.record(exc)
    assert s3 != s1
    assert len(s1) == 12


def test_record_appends_one_redacted_line(tmp_path: Path) -> None:
    """jsonl 每行一个事件；**不含消息正文**（ValueError 里塞的敏感文本不许落盘）。"""
    log = tmp_path / "error.jsonl"
    es.reset_for_tests(log)
    try:
        raise ValueError("患者姓名张三 病历编号 42")
    except ValueError as exc:
        es.record(exc)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["type"] == "ValueError"
    assert "sig" in row and "at" in row
    assert "张三" not in lines[0] and "病历编号" not in lines[0]


def test_recent_counts_within_window(tmp_path: Path) -> None:
    """计数是 24h 滑窗：窗口内累加，窗口外不计（用假时钟把过期那半钉死）。"""
    now = [1_000_000.0]
    es.reset_for_tests(tmp_path / "error.jsonl", clock=lambda: now[0])
    try:
        _boom()
    except ValueError as exc:
        es.record(exc)
    assert es.recent_count() == 1
    # 30h 后再来一发：第一发滑出窗口，计数只剩第二发。
    now[0] += 30 * 3600
    try:
        raise KeyError("k")
    except KeyError as exc:
        es.record(exc)
    assert es.recent_count() == 1


def test_write_failure_never_raises(tmp_path: Path) -> None:
    """观测不许把被观测的带走：日志目录不可写时 record 照常返回签名、recent 照常计数。"""
    log = tmp_path / "readonly" / "error.jsonl"
    (tmp_path / "readonly").write_text("不是目录")  # 路径被文件占住 ⇒ makedirs 必炸
    es.reset_for_tests(log)
    try:
        _boom()
    except ValueError as exc:
        assert es.record(exc)  # 不抛
    assert es.recent_count() == 1
