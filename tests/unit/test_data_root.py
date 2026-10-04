"""一个数据根决定三样路径（M4：§4.1 那句"只切 SQLite 会得到记忆没了、向量库还在"）。

要买的东西：**换身份 = 换一份完整数据集**这件事只需要一个变量。库、向量索引、上传件
必须同进同出 —— 半换比不换更糟，因为它的症状长得像"我的数据坏了"。

四条各挡一个坏法：
  1. `DATA_ROOT` 真的能整份搬走（含打包态那条判定不被它绕过）；
  2. 三样的**布局**钉死（改布局=让装好的那份应用"凭空丢库"，而代码看起来只是重命名）；
  3. 半搬要出声、全搬与全不动都不出声（探针故意只换库做实验，那种用法不该被判红）；
  4. 启动器（`scripts/run_api.py`）真的用这份推导，而不是自己再抄一遍路径。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rolecard_agent.base.paths import (
    DATA_PATH_ENVS,
    data_paths,
    split_root_notice,
    user_data_root,
)

ROOT = Path(__file__).resolve().parents[2]


def _launcher(monkeypatch: pytest.MonkeyPatch):
    """按真路径加载 `scripts/run_api.py`（它不在包里，只能按文件加载）。"""
    spec = importlib.util.spec_from_file_location(
        "run_api_for_root_test", ROOT / "scripts" / "run_api.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in DATA_PATH_ENVS:
        monkeypatch.delenv(key, raising=False)
    return mod


def test_data_root_moves_the_whole_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = tmp_path / "second-instance"
    monkeypatch.setenv("DATA_ROOT", str(other))
    assert user_data_root() == other
    paths = data_paths()
    assert set(paths) == set(DATA_PATH_ENVS)
    for value in paths.values():
        assert value.is_relative_to(other), value
    # 一个根 = 一个身份的那份完整数据集：四条彼此同根，不需要再各设一遍。
    assert (
        paths["CHROMA_PATH"].parent == paths["UPLOAD_DIR"].parent
        == paths["WORKSPACE_DIR"].parent == other
    )


def test_the_installed_layout_is_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """三条的**子目录名**钉死：装好的那份应用就按这个布局找数据。

    改它不需要改任何代码调用点，症状却是"更新一次丢一次"—— 所以它必须红一次，
    逼改的人想起来要写迁移。
    """
    root = tmp_path / "root"
    monkeypatch.setenv("DATA_ROOT", str(root))
    assert data_paths() == {
        "SQLITE_PATH": root / "sqlite" / "app.db",
        "CHROMA_PATH": root / "chroma",
        "UPLOAD_DIR": root / "uploads",
        # 工作区也在内：文件工具与命令执行的落点。两个实例共用它 = 第二个人
        # 读得到第一个人的文件，这一条与向量索引同级，不是"顺便"。
        "WORKSPACE_DIR": root / "workspace",
    }


def test_half_moved_root_speaks_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    monkeypatch.setenv("DATA_ROOT", str(root))
    inside = data_paths()
    elsewhere = tmp_path / "elsewhere"

    # 全在根下 / 全搬出去：都不出声（后者是"整份换到别处"，本来就是合法形态）
    assert split_root_notice(**{k.lower(): v for k, v in inside.items()}) is None
    assert split_root_notice(
        sqlite_path=elsewhere / "app.db", chroma_path=elsewhere / "c",
        upload_dir=elsewhere / "u", workspace_dir=elsewhere / "w",
    ) is None
    # 只换库不换索引：正是 §4.1 那个半吊子状态，必须点名是哪几条在外面
    note = split_root_notice(
        sqlite_path=elsewhere / "app.db",
        chroma_path=inside["CHROMA_PATH"],
        upload_dir=inside["UPLOAD_DIR"],
        workspace_dir=inside["WORKSPACE_DIR"],
    )
    assert note is not None and "SQLITE_PATH" in note and "DATA_ROOT" in note


def test_the_launcher_uses_the_same_derivation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """启动器不再自己抄一份路径：设一个 `DATA_ROOT`，三条环境变量都落在它下面。"""
    import os

    mod = _launcher(monkeypatch)
    root = tmp_path / "instance-b"
    monkeypatch.setenv("DATA_ROOT", str(root))
    mod._resolve_data_paths()
    assert Path(os.environ["SQLITE_PATH"]) == root / "sqlite" / "app.db"
    assert Path(os.environ["CHROMA_PATH"]) == root / "chroma"
    assert Path(os.environ["UPLOAD_DIR"]) == root / "uploads"
    assert Path(os.environ["WORKSPACE_DIR"]) == root / "workspace"


def test_the_launcher_speaks_up_when_only_the_db_moves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """只显式换库（探针与实验的常见做法）：启动要出声，而不是让人以为整份都换了。"""
    mod = _launcher(monkeypatch)
    root = tmp_path / "instance-b"
    monkeypatch.setenv("DATA_ROOT", str(root))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "scratch.db"))
    mod._resolve_data_paths()
    out = capsys.readouterr().out
    assert "数据根被搬走了一半" in out and "SQLITE_PATH" in out, out
