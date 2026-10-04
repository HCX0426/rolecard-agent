"""打包态路径发现（base/paths.py）。

D②-4 把后端打进桌面安装包，全部风险都集中在"路径解析错"这一族，而且它错得很安静：
数据根指到安装目录 → 装到 Program Files 时第一次写库就崩；相对路径基准换掉 → 用户
"没改过任何配置"却发现库是空的。所以这里钉的是**两种形态各自的落点**，而不是函数返回值。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rolecard_agent.base import paths


@pytest.fixture
def frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把解释器伪装成 PyInstaller onedir 的冻结态，返回"随包资源根"（等价 sys._MEIPASS）。"""
    bundle = tmp_path / "_internal"
    bundle.mkdir()
    local = tmp_path / "LocalAppData"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return bundle


def test_dev_shape_keeps_the_repo_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """开发态：一切按仓库根，与打包改造之前一模一样。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    root = paths.repo_root()
    assert paths.is_frozen() is False
    assert paths.bundle_root() == root
    assert paths.user_data_root() == root / "data"
    assert paths.dotenv_path() == root / ".env"


def test_relative_config_path_is_resolved_against_the_repo_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.env.example 写的是 `./data/sqlite/app.db`：基准换成数据根会让它变成 `data/data/...`，
    表现是"路径没改过，库却空了"。这条钉住那个不能回退的语义。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    resolved = paths.path_from_config("./data/sqlite/app.db")
    assert resolved == paths.repo_root() / "data" / "sqlite" / "app.db"


def test_absolute_config_path_passes_through_untouched(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "app.db"
    assert paths.path_from_config(str(absolute)) == absolute


def test_frozen_shape_moves_writes_out_of_the_install_dir(frozen: Path) -> None:
    """冻结态：随包资源在 `_MEIPASS`，可写的用户数据在 `%LOCALAPPDATA%\\rolecard-agent`。"""
    assert paths.is_frozen() is True
    assert paths.bundle_root() == frozen
    data = paths.user_data_root()
    assert data.parent.name == "LocalAppData" and data.name == "rolecard-agent"
    # 配置也不在安装目录里（升级会整目录替换它）。
    assert paths.dotenv_path().parent == data.parent


def test_frozen_relative_path_follows_the_data_root(frozen: Path) -> None:
    """打包态没有仓库根可言，相对路径只能按数据根解析 —— 但绝对路径仍然原样通过。"""
    assert paths.path_from_config("./data/app.db") == paths.user_data_root() / "data" / "app.db"
    elsewhere = frozen.parent / "somewhere" / "app.db"
    assert paths.path_from_config(str(elsewhere)) == elsewhere


def test_bundled_console_dist_resolves_inside_the_bundle(frozen: Path) -> None:
    assert paths.bundle_root() / "frontend" / "dist" == frozen / "frontend" / "dist"
