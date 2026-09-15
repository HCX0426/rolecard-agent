"""run_api 启动器：数据路径解析与 CWD 解耦。

审计中定位的"指错库"坑——config 的 `./data/...` 相对路径在从非项目根目录启动时
会落到错误位置（表现为 /api/knowledge 返回 []、会话/知识全空）。`_resolve_data_paths`
保证无论从哪里启动都指向同一份真实数据。
"""
import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_api.py"
ROOT = SCRIPT.parents[1]  # 项目根


@pytest.fixture
def run_api_mod():
    spec = importlib.util.spec_from_file_location("run_api_test", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate_env():
    saved = {k: os.environ.get(k) for k in ("SQLITE_PATH", "CHROMA_PATH", "UPLOAD_DIR")}
    for k in saved:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_unset_resolves_to_project_root(run_api_mod, tmp_path, monkeypatch):
    """未设置环境变量时，从非项目根 CWD 启动也指向项目根的默认绝对路径。"""
    monkeypatch.chdir(tmp_path)
    run_api_mod._resolve_data_paths()
    assert os.path.isabs(os.environ["SQLITE_PATH"])
    assert os.environ["SQLITE_PATH"] == str(ROOT / "data" / "sqlite" / "app.db")
    assert os.environ["CHROMA_PATH"] == str(ROOT / "data" / "chroma")
    assert os.environ["UPLOAD_DIR"] == str(ROOT / "data" / "uploads")


def test_relative_override_resolved_against_root(run_api_mod, tmp_path, monkeypatch):
    """用户给了相对路径 → 按项目根解析（CWD 无关），而不是按启动目录。"""
    monkeypatch.chdir(tmp_path)
    os.environ["CHROMA_PATH"] = "custom_chroma"
    run_api_mod._resolve_data_paths()
    assert os.environ["CHROMA_PATH"] == str(ROOT / "custom_chroma")
    # 清理函数顺带创建的空目录，避免污染仓库
    Path(os.environ["CHROMA_PATH"]).rmdir()


def test_absolute_override_preserved(run_api_mod, tmp_path, monkeypatch):
    """用户给了绝对路径 → 原样保留（显式覆盖优先于默认）。"""
    monkeypatch.chdir(tmp_path)
    abs_upload = str(tmp_path / "abs_uploads")
    os.environ["UPLOAD_DIR"] = abs_upload
    run_api_mod._resolve_data_paths()
    assert os.environ["UPLOAD_DIR"] == abs_upload
