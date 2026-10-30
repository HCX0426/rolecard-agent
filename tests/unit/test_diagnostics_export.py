"""P2-② 诊断包端点：一键 zip（用户报 bug 附带现场）。

契约（开发流程.md P2 行）：zip = error.jsonl + 双日志副本（有则收）+ 版本指纹 +
环境摘要。三条硬判据在这里钉死：

* 操作员门禁（与 deep health 同一条 require_operator 语义）；
* **不含秘密**：凭据、口令类环境变量的值绝不进包（名单制，不是"去掉已知敏感键"）；
* 导出留审计（谁在什么时候导出现场是"权限三重过滤 + 审计"覆盖的面）。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.base import error_signatures as es

ZIP_MIME = "application/zip"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # 错误日志与壳日志都挪进 tmp_path：诊断包收的是"这台机器现在的现场"。
    monkeypatch.setenv("ROLECARD_ERROR_LOG", str(tmp_path / "errors" / "error.jsonl"))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "backend.log").write_text("后端一行\n", encoding="utf-8")
    (log_dir / "shell.log").write_text("壳一行\n", encoding="utf-8")
    monkeypatch.setenv("ROLECARD_LOG_DIR", str(log_dir))
    es.reset_for_tests()
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"))


def _zip_rows(client: TestClient) -> dict[str, str]:
    res = client.get("/api/diagnostics/export")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith(ZIP_MIME)
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        return {name: zf.read(name).decode("utf-8", "replace") for name in zf.namelist()}


def test_export_contains_the_scene(client: TestClient) -> None:
    """一次真异常 + 一次导出：包里有 error.jsonl（含签名行）、双日志副本、manifest。"""
    try:
        raise RuntimeError("现场复现失败：第 42 步")
    except RuntimeError as exc:
        es.record(exc)
    rows = _zip_rows(client)
    assert any("error.jsonl" in n for n in rows)
    assert "RuntimeError" in rows[[n for n in rows if "error.jsonl" in n][0]]
    assert "后端一行" in rows["logs/backend.log"]
    assert "壳一行" in rows["logs/shell.log"]
    manifest = json.loads(rows["manifest.json"])
    assert manifest["version"]  # 版本指纹在场
    assert manifest["build"]["sha"] is not None


def test_export_never_carries_secrets(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """名单制隐私闸：把秘密塞进 env，包内任何一条都不许含它的值。"""
    monkeypatch.setenv("AUTH_CREDENTIALS", "operator:super-s3cret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-another-s3cret")
    rows = _zip_rows(client)
    for name, text in rows.items():
        assert "super-s3cret" not in text, f"{name} 泄漏了凭据"
        assert "sk-another-s3cret" not in text, f"{name} 泄漏了 API key"


def test_export_requires_operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """开了鉴权后，使用者（非 operator）拿到 403 —— 诊断包含数据根路径，不是人人都该导。"""
    monkeypatch.setenv("AUTH_MODE", "on")
    monkeypatch.setenv("AUTH_CREDENTIALS", "alice:pw,operator:bob:pw")
    c = TestClient(create_app(sqlite_path=tmp_path / "auth.db"), client=("203.0.113.9", 50000))
    res = c.get("/api/diagnostics/export", auth=("alice", "pw"))
    assert res.status_code == 403


def test_export_is_audited(client: TestClient) -> None:
    """谁导出现场必须可查（审计只记动作与目标，不记包内容）。"""
    _zip_rows(client)
    rows = client.get("/api/audit").json()
    assert any(r["action"] == "export_diagnostics" for r in rows)


def test_zip_contains_db_health_readings(client: TestClient) -> None:
    """R1031-26：诊断包必须带库健康两读数（user_version + quick_check）——
    排障最常问的"库现在什么状态"以前要人另开 sqlite 客户端现查。"""
    import io
    import zipfile

    res = client.get("/api/diagnostics/export")
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        health = json.loads(zf.read("db-health.json"))
    assert health["present"] is True
    assert isinstance(health["user_version"], int)
    assert health["quick_check"] == "ok"
