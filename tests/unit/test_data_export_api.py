"""全量备份端点（R1031-15）：应用内那条"把整台机器的她搬走"的路。

从前只有开发者口径（脚本 + 会命令行 + 有 python 环境）。这里钉端点这一侧的判据：

* **包里真的有主库且读得回来**（不是"下载成功"就算数 —— 那种坏包只在回滚那天被发现）；
* **主库不在数据根里时直接拒绝**（409）：那种包没有主库，回滚不了任何东西；
* **导出留审计**（谁在什么时候导出了整份数据）；
* 端点复用**同一份机制**（`features/data_backup.py`），不与脚本各做一套。

库的 schema 由 `create_app` 自己bootstrap 出来（判据问的是"这台机器真实的库进包了吗"，
手搓一张形似 `session_thread` 的假表只会在迁移对账时炸出真错误——2026-10-31 首跑现场）。
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.storage.db import SCHEMA_VERSION

ZIP_MIME = "application/zip"


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """这台机器的数据根：主库由 app 自己长出来，另有上传件与工作区文件。"""
    root = tmp_path / "dataroot"
    (root / "uploads").mkdir(parents=True)
    (root / "uploads" / "a.txt").write_text("原件", encoding="utf-8")
    (root / "workspace").mkdir()
    (root / "workspace" / "note.md").write_text("工作区", encoding="utf-8")
    # DATA_ROOT 一处重定向 → sqlite/chroma/uploads/workspace 全族跟着走（会话级同款机制）
    monkeypatch.setenv("DATA_ROOT", str(root))
    return root


@pytest.fixture()
def client(data_root: Path) -> TestClient:
    return TestClient(create_app(sqlite_path=data_root / "sqlite" / "app.db"))


def _zip_names(client: TestClient) -> set[str]:
    res = client.get("/api/data/export")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith(ZIP_MIME)
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        return set(zf.namelist())


def test_export_zip_carries_the_live_database_and_the_files(
    client: TestClient, tmp_path: Path
) -> None:
    """包里必须有主库（是真库的快照）+ 那些文件 —— 少一样都不叫备份。"""
    client.get("/api/audit?limit=1")  # 预热：让 app 真连一次库、把 schema 落盘
    res = client.get("/api/data/export")
    assert res.status_code == 200, res.text
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = set(zf.namelist())
        db_bytes = zf.read("sqlite/app.db")
    assert "sqlite/app.db" in names, f"主库没进包：{names}"
    assert "uploads/a.txt" in names and "workspace/note.md" in names, names
    # 快照读得回来，且是**这台机器真库**的形状（代际戳在对的位置、审计表在）——
    # "有个同名文件"不算数：那种坏包只在回滚那天才被打开。
    snap = tmp_path / "snapshot-check.db"
    snap.write_bytes(db_bytes)
    con = sqlite3.connect(snap)
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "audit_log" in tables, f"快照里没有 app 自己的表：{sorted(tables)[:8]}…"
    finally:
        con.close()


def test_export_writes_audit(client: TestClient) -> None:
    """导出整份数据是操作员动作，审计里得有它（谁在什么时候搬走了整台机器）。"""
    _zip_names(client)
    res = client.get("/api/audit?limit=50")
    actions = [r["action"] for r in res.json()]
    assert "export_data_backup" in actions, actions


def test_export_refuses_when_the_database_is_outside_the_root(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主库被 `SQLITE_PATH` 指到根外 ⇒ 导出会缺主库 ⇒ 当场说清为什么做不成（409）。

    这是"给一个看着能用的空壳备份，不如当场拒绝"那条纪律 —— 空壳备份只在回滚那天
    才被发现是坏的，而那时数据已经在新机器上被覆盖了。
    """
    outside = data_root.parent / "outside.db"
    monkeypatch.setenv("SQLITE_PATH", str(outside))
    client = TestClient(create_app(sqlite_path=outside))
    res = client.get("/api/data/export")
    assert res.status_code == 409, res.text
    assert "不在" in res.text, res.text
