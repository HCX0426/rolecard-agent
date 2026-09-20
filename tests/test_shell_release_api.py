"""桌面壳安装包端点（api/routers/shell_release.py）。 Traceability: US-5（可运营）。

钉的是"下载入口不许说谎"这一族：

  1. **没配托管目录** → available=False（B/S 默认形态，界面上不该出现按钮）。
  2. **配了目录但里面没有产物** → 仍然 available=False。判据是文件在不在，不是开关开没开
     —— 部署方忘了拷产物，留下的就是一个点了没反应的死按钮。
  3. **只认产物命名**：electron-builder 在同目录还会写 `.yml` 清单与 `.blockmap`，把它们
     当安装包返回，用户下到一个 300 字节的 yaml。
  4. 多个产物时取**最新**那个，下载回来的字节与文件名要对得上。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.deps import get_context
from rolecard_agent.api.main import create_app
from rolecard_agent.config import Settings

_OLD = "rolecard-agent-0.2.0-x64.exe"
_NEW = "rolecard-agent-0.3.0-x64.exe"


@dataclass
class _Ctx:
    settings: Settings


def _client(release_dir: Path | None, tmp_path: Path) -> TestClient:
    """建一个只为了这两个端点的 app。`sqlite_path` 必须显式给：`create_app()` 不传就会
    bootstrap **仓库里的演示库**（data/sqlite/app.db），于是一个只读设置端点的测试开始改
    真数据（本次拆层的迁移就是这样被撞出来的）。
    """
    app = create_app(sqlite_path=tmp_path / "runtime" / "shell-release.db")
    app.dependency_overrides[get_context] = lambda: _Ctx(
        Settings(shell_release_dir=release_dir)
    )
    return TestClient(app)


def _artifact(directory: Path, name: str, payload: bytes, mtime: float) -> Path:
    path = directory / name
    path.write_bytes(payload)
    os.utime(path, (mtime, mtime))
    return path


def test_not_configured_reports_unavailable(tmp_path: Path) -> None:
    """目录都没配：available 与 configured 都是 False，下载 404。"""
    client = _client(None, tmp_path)
    body = client.get("/api/shell-release").json()
    assert body == {"available": False, "configured": False}
    assert client.get("/api/shell-release/download").status_code == 404


def test_configured_but_empty_is_still_unavailable(tmp_path: Path) -> None:
    client = _client(tmp_path, tmp_path)
    body = client.get("/api/shell-release").json()
    assert body == {"available": False, "configured": True}
    assert client.get("/api/shell-release/download").status_code == 404


def test_non_artifact_files_do_not_count(tmp_path: Path) -> None:
    """builder 的副产品（latest.yml、blockmap、别的 exe 命名）都不算安装包。"""
    (tmp_path / "latest.yml").write_text("version: 0.3.0", encoding="utf-8")
    (tmp_path / f"{_NEW}.blockmap").write_bytes(b"x")
    (tmp_path / "setup.exe").write_bytes(b"x")
    body = _client(tmp_path, tmp_path).get("/api/shell-release").json()
    assert body == {"available": False, "configured": True}


def test_newest_artifact_wins_and_reports_size(tmp_path: Path) -> None:
    _artifact(tmp_path, _OLD, b"old-old", 1_700_000_000)
    _artifact(tmp_path, _NEW, b"new-package-bytes", 1_800_000_000)
    body = _client(tmp_path, tmp_path).get("/api/shell-release").json()
    assert body["available"] is True
    assert body["file_name"] == _NEW
    assert body["size_bytes"] == len(b"new-package-bytes")
    # built_at 是产物文件的 mtime（ISO 串，带时区）——界面上"这份是哪天打的"就靠它。
    assert datetime.fromisoformat(cast(str, body["built_at"])).utcoffset() is not None


def test_download_streams_the_artifact_it_described(tmp_path: Path) -> None:
    _artifact(tmp_path, _NEW, b"binary-payload", 1_800_000_000)
    client = _client(tmp_path, tmp_path)
    name = client.get("/api/shell-release").json()["file_name"]
    response = client.get("/api/shell-release/download")
    assert response.status_code == 200
    assert response.content == b"binary-payload"
    assert name in response.headers["content-disposition"]


def test_missing_directory_config_does_not_explode(tmp_path: Path) -> None:
    """配置指向一个不存在的路径：报"没有产物"而不是 500（部署方删目录是常态）。"""
    body = _client(tmp_path / "gone", tmp_path).get("/api/shell-release").json()
    assert body == {"available": False, "configured": True}


@pytest.mark.parametrize("path", ["/api/shell-release", "/api/shell-release/download"])
def test_endpoints_answer_without_credentials_when_auth_off(tmp_path: Path, path: str) -> None:
    """默认 AUTH_MODE=off 的本地形态下这两个都是**只读**端点：不碰主机，也不接受路径输入。"""
    assert _client(tmp_path, tmp_path).get(path).status_code in {200, 404}
