"""`core/update_check.py` —— 检查更新的比对判据（全离线，出网面走注入）。

钉的是设计稿《更新机制与OCR随包》§3.1 的承诺：
- 双事实源的优先级：本地 release_dir > GitHub repo > none；
- 出网失败**永远收敛成 error 读数**，端点不 500（"检查更新失败"不许长得像"应用坏了"）；
- 版本比较不引 packaging（运行时五族没有它），形状外的 tag 判"比不了"而不是猜。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rolecard_agent.core.update_check import check_update, parse_version


def test_parse_version_shape() -> None:
    assert parse_version("0.4.1") == (0, 4, 1)
    assert parse_version("v0.4.1") == (0, 4, 1)  # tag 前缀的 v 与裸版本同义
    assert parse_version("10.2.30") > parse_version("9.9.9")  # 逐段数值比，不是字典序
    assert parse_version("0.4.1-rc1") == ()  # 形状外：判"比不了"，不装懂
    assert parse_version("") == ()


def _write_artifact(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"MZ fake installer")
    return path


def test_release_dir_is_preferred_and_parses_version_from_filename(tmp_path: Path) -> None:
    _write_artifact(tmp_path, "rolecard-agent-0.4.1-x64.exe")
    _write_artifact(tmp_path, "rolecard-agent-0.9.0-x64.exe")  # 取最新那个，不取 mtime

    def explode(url: str) -> dict[str, Any]:
        raise AssertionError(f"本地源在场就不该出网，却被叫去 {url}")

    info = check_update(
        current="0.4.0",
        enabled=True,
        repo="someone/else",
        release_dir=tmp_path,
        fetcher=explode,
    )
    assert info.source == "release_dir"
    assert info.latest == "0.9.0" and info.has_update is True
    assert info.file_name == "rolecard-agent-0.9.0-x64.exe"
    assert info.url == "/api/shell-release/download"


def test_release_dir_without_update(tmp_path: Path) -> None:
    _write_artifact(tmp_path, "rolecard-agent-0.4.0-x64.exe")
    info = check_update(
        current="0.4.1", enabled=True, repo="", release_dir=tmp_path, fetcher=None
    )
    assert info.source == "release_dir" and info.has_update is False


def test_github_source_with_injected_fetcher() -> None:
    seen: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        seen.append(url)
        return {"tag_name": "v0.5.0", "html_url": "https://github.com/o/r/releases/tag/v0.5.0"}

    info = check_update(
        current="0.4.1", enabled=True, repo="o/r", release_dir=None, fetcher=fake
    )
    assert seen == ["https://api.github.com/repos/o/r/releases/latest"]
    assert info.source == "github" and info.latest == "0.5.0" and info.has_update is True
    assert info.url == "https://github.com/o/r/releases/tag/v0.5.0"


@pytest.mark.parametrize("tag", ["", "nightly"])
def test_github_tag_not_a_version_reports_not_comparable(tag: str) -> None:
    info = check_update(
        current="0.4.1",
        enabled=True,
        repo="o/r",
        fetcher=lambda url: {"tag_name": tag},
    )
    assert info.source == "github" and info.error and info.has_update is False


def test_two_segment_tag_still_compares() -> None:
    # 元组比较对"前缀式"版本语义正确：(0,5) > (0,4,1) 恰是 0.5 > 0.4.1。
    # 本仓的 tag 恒为三段（version parity 尺子钉着），但判据不该被这个巧合绑死。
    info = check_update(
        current="0.4.1", enabled=True, repo="o/r", fetcher=lambda url: {"tag_name": "v0.5"}
    )
    assert info.latest == "0.5" and info.has_update is True and info.error is None


def test_network_failure_never_raises() -> None:
    def broken(url: str) -> dict[str, Any]:
        raise TimeoutError("connect timed out")

    info = check_update(current="0.4.1", enabled=True, repo="o/r", fetcher=broken)
    assert info.source == "none" and "TimeoutError" in (info.error or "")
    assert info.has_update is False  # 问不到 ≠ 有更新


def test_disabled_makes_no_call() -> None:
    def explode(url: str) -> dict[str, Any]:
        raise AssertionError("disabled 不该出网")

    info = check_update(
        current="0.4.1", enabled=False, repo="o/r", fetcher=explode
    )
    assert info.source == "disabled" and info.error is None


def test_no_source_at_all_says_none(tmp_path: Path) -> None:
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    info = check_update(current="0.4.1", enabled=True, repo="", release_dir=empty)
    assert info.source == "none" and info.error == "未配置更新源"
