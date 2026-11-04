"""`/api/health` 报出 `data_root` —— "这一进程读的库在哪"的唯一问答（A-4）。

09-24 搬迁之后世界上有两份数据根（仓库 `data/` 与安装态系统目录），外加 `DATA_ROOT`
整体搬家那条路（M4）—— 并存的世界里"探活看着好好的，怎么数据不对"没法从 health 归因
到目录，支持/运维只能猜。判据钉两件事：

* health 报的**逐字等于** `user_data_root()`（唯一出处，认 `DATA_ROOT` 覆盖）——
  钉成 `repo_root() / "data"` 之类的硬编码就是第二套解析逻辑，正是这条要消灭的；
* `DATA_ROOT` 指过去时 health **跟着走** —— 只断言"键在"防不住把默认根烤死。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.base.paths import user_data_root


def test_health_data_root_is_user_data_root(tmp_path: Path) -> None:
    """默认形态：health 报的目录 = `user_data_root()` 原话（不做任何加工/归一）。"""
    client = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    body = client.get("/api/health").json()
    assert body["data_root"] == str(user_data_root())


def test_health_data_root_follows_data_root_override(tmp_path: Path, monkeypatch) -> None:
    """`DATA_ROOT` 整体搬家（M4）时 health 必须跟着走 —— 这一格的本职就是报"现读哪份"。"""
    moved = tmp_path / "moved"
    monkeypatch.setenv("DATA_ROOT", str(moved))
    client = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    body = client.get("/api/health").json()
    assert body["data_root"] == str(user_data_root())
    assert str(moved) in body["data_root"]
