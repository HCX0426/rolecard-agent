"""`check_changelog` 读版本号这一格（P3-9「tomllib 化」）。

钉的是**正则版做不到**的两种写法：版本**缩进**写、以及用**单引号**写。旧版是
`re.search(r'(?m)^version = "([^"]+)"')` —— 既要求行首零缩进又只认双引号，匹配不到时
**静默返回 None**，于是"当前版本在 CHANGELOG 里没有节"这条就恒绿了（判据变摆设，
与 `_imported_modules` AST 化那次同一个坑）。按 TOML 结构读 `[project].version` 才是
在问"这一格的值"，不是在文本里找一个长得像的行。

用例形状照 `test_bump_version.py`：假仓库 + 注入 ROOT/out/fails，不碰真仓库。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import consistency.checks_meta as cm  # noqa: E402


def _run_in_fake_repo(
    tmp_path: pathlib.Path, pyproject_text: str, changelog_text: str
) -> list[str]:
    """把判据本体指向一个假仓库，返回它判出来的问题列表。"""
    (tmp_path / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
    real_root, real_out, real_fails = cm.ROOT, cm.out, cm.fails
    captured: list[str] = []
    try:
        cm.ROOT = tmp_path
        cm.out = lambda *a, **k: None  # type: ignore[assignment]
        cm.fails = captured  # type: ignore[assignment]
        cm.check_changelog()
    finally:
        cm.ROOT, cm.out, cm.fails = real_root, real_out, real_fails
    return captured


_CLOG_WITH_VERSION = "# 变更史\n\n## [Unreleased]\n\n## [0.3.0] - 2026-01-01\n"
_CLOG_WITHOUT_VERSION = "# 变更史\n\n## [Unreleased]\n\n## [0.2.0] - 2026-01-01\n"


def test_缩进写的版本照样读得到(tmp_path: pathlib.Path) -> None:
    """`[project]` 下缩进两格的 version —— 正则版要求行首零缩进，会读不到。"""
    problems = _run_in_fake_repo(
        tmp_path,
        '[project]\n  version = "0.3.0"\n',
        _CLOG_WITHOUT_VERSION,  # 故意没有 0.3.0 那一节
    )
    # 读得到版本 ⇒ 应该判"CHANGELOG 里没有当前版本那一节"
    assert any("0.3.0" in p for p in problems), f"版本没被读出来 ⇒ 判据恒绿：{problems}"


def test_单引号写的版本照样读得到(tmp_path: pathlib.Path) -> None:
    """TOML 里单引号是合法字符串 —— 正则版只认双引号，会读不到。"""
    problems = _run_in_fake_repo(
        tmp_path,
        "[project]\nversion = '0.3.0'\n",
        _CLOG_WITHOUT_VERSION,
    )
    assert any("0.3.0" in p for p in problems), f"单引号版本没被读出来 ⇒ 判据恒绿：{problems}"


def test_版本有节时判绿(tmp_path: pathlib.Path) -> None:
    """正向那一半：CHANGELOG 里有当前版本的节，不许报。"""
    problems = _run_in_fake_repo(
        tmp_path,
        '[project]\nversion = "0.3.0"\n',
        _CLOG_WITH_VERSION,
    )
    assert not problems, problems


@pytest.mark.parametrize("clog", [_CLOG_WITHOUT_VERSION])
def test_普通双引号版本仍然照旧工作(tmp_path: pathlib.Path, clog: str) -> None:
    """保底：仓库现在这种写法（双引号、零缩进）不许回归。"""
    problems = _run_in_fake_repo(
        tmp_path,
        '[project]\nversion = "0.3.0"\n',
        clog,
    )
    assert any("0.3.0" in p for p in problems), problems
