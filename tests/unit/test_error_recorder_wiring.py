"""P2-① 接线：health 的 error_count_recent + 未捕获异常出口真的记账。

判据两条：
* `/api/health`（免鉴权那格）带 `error_count_recent`，且值跟着 record 走；
* 一条真 500（路由里冒出来的未映射异常）会在 error.jsonl 落一行、health 计数 +1 ——
  即"记账挂在异常真正的出口上"，不是只给 core 一个没人调的 API。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rolecard_agent.api import diagnostics
from rolecard_agent.base import error_signatures as es


@pytest.fixture()
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "errors" / "error.jsonl"
    es.reset_for_tests(log)
    return log


def test_health_carries_recent_error_count(_isolate: Path) -> None:
    from rolecard_agent.api.main import create_app

    c = TestClient(create_app(sqlite_path=_isolate.parent / "app.db"))
    assert c.get("/api/health").json()["error_count_recent"] == 0
    try:
        raise RuntimeError("x")
    except RuntimeError as exc:
        es.record(exc)
    assert c.get("/api/health").json()["error_count_recent"] == 1


def test_unmapped_500_gets_recorded(_isolate: Path) -> None:
    """500 出口是记账点：路由里冒出来的异常（没在 errors 注册表里）必须落签名行。"""
    app = FastAPI()
    diagnostics.install_unhandled_recorder(app)

    @app.get("/api/boom")
    def _boom() -> None:
        raise ZeroDivisionError("分母是运行时算出来的，不该为零")

    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/boom").status_code == 500
    lines = _isolate.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[-1])
    assert row["type"] == "ZeroDivisionError"
    assert es.recent_count() >= 1
