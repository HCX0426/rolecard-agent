"""API 层测试（管理面：角色卡 CRUD + 插件启停）。

Traceability: US-1, US-2, US-3, US-8（US-7 由控制台页面端点覆盖）。

全部离线：用 `tmp_path` 里的临时库，不依赖 Ollama 或网络。每个测试独立建库。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(sqlite_path=tmp_path / "app.db")
    return TestClient(app)


# -- roles --------------------------------------------------------------------------


def test_list_roles_includes_builtin(client: TestClient) -> None:
    """Traceability: US-1 — 内置角色随库启动即存在且排在前面。"""
    res = client.get("/api/roles")
    assert res.status_code == 200
    roles = res.json()
    assert any(r["role_id"] == "medical_archivist" and not r["is_builtin"] for r in roles)


def test_create_role(client: TestClient) -> None:
    """Traceability: US-1, US-2 — 新建角色卡返回 201 并立刻可查。"""
    payload = {
        "role_id": "greeter",
        "role_name": "迎宾",
        "system_prompt": "你是前台迎宾。",
        "temperature": 0.6,
        "tool_whitelist": ["list_roles"],
    }
    res = client.post("/api/roles", json=payload)
    assert res.status_code == 201
    assert res.json()["role_id"] == "greeter"

    ids = {r["role_id"] for r in client.get("/api/roles").json()}
    assert "greeter" in ids


def test_create_duplicate_role_conflicts(client: TestClient) -> None:
    """Traceability: US-2 — 重复 role_id 返回 409，而不是静默覆盖。"""
    payload = {"role_id": "dup", "role_name": "dup", "system_prompt": "x"}
    assert client.post("/api/roles", json=payload).status_code == 201
    res = client.post("/api/roles", json=payload)
    assert res.status_code == 409


def test_update_role_applies_fields(client: TestClient) -> None:
    """Traceability: US-2, US-8 — PATCH 局部更新生效，含知识作用域列。"""
    create = client.post(
        "/api/roles",
        json={"role_id": "editor", "role_name": "e", "system_prompt": "原版"},
    )
    assert create.status_code == 201

    patch = {
        "system_prompt": "改版",
        "temperature": 0.3,
        "knowledge_scopes": ["reports_2026"],
    }
    res = client.patch("/api/roles/editor", json=patch)
    assert res.status_code == 200
    body = res.json()
    assert body["system_prompt"] == "改版"
    assert body["temperature"] == 0.3
    assert body["knowledge_scopes"] == ["reports_2026"]


def test_delete_builtin_rejected(client: TestClient) -> None:
    """Traceability: US-1 — 内置角色不可删（D6 保护）。

    内置只剩「通用助手」；档案管理员已降级为域种子角色（自定义类型），可删
    （test_delete_custom_role 覆盖删除路径）。
    """
    res = client.delete("/api/roles/general_assistant")
    assert res.status_code == 409


def test_delete_custom_role(client: TestClient) -> None:
    """Traceability: US-1 — 自定义角色可删，删除后列表不再包含。"""
    client.post("/api/roles", json={"role_id": "temp", "role_name": "t", "system_prompt": "x"})
    res = client.delete("/api/roles/temp")
    assert res.status_code == 204

    ids = {r["role_id"] for r in client.get("/api/roles").json()}
    assert "temp" not in ids


# -- plugins ------------------------------------------------------------------------


def test_list_plugins(client: TestClient) -> None:
    """Traceability: US-3 — 注册域以插件行呈现，默认启用。"""
    res = client.get("/api/plugins")
    assert res.status_code == 200
    plugins = res.json()
    health = next((p for p in plugins if p["plugin_id"] == "health"), None)
    assert health is not None
    assert health["enabled"] is True


def test_toggle_plugin_disables_and_bumps_epoch(client: TestClient) -> None:
    """Traceability: US-3 — 停用插件返回新 tool_epoch 且状态翻转；重复同态不递增。"""
    res = client.post("/api/plugins/health/toggle", json={"enabled": False})
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert body["tool_epoch"] == 2  # 初始为 1，首次切换 +1

    # 已经是 disabled，再发一次 disabled 不应再递增版本
    again = client.post("/api/plugins/health/toggle", json={"enabled": False})
    assert again.json()["tool_epoch"] == 2


def test_toggle_unknown_plugin_404(client: TestClient) -> None:
    """Traceability: US-3 — 命名未注册插件返回 404。"""
    res = client.post("/api/plugins/ghost/toggle", json={"enabled": True})
    assert res.status_code == 404


def test_tools_catalog_groups_by_domain(client: TestClient) -> None:
    """Traceability: US-9 — 工具目录按 内核/领域 分组并带一句话说明。"""
    res = client.get("/api/tools/catalog")
    assert res.status_code == 200
    body = res.json()
    kernel_names = {t["name"] for t in body["kernel"]}
    assert {"list_domains", "list_roles"} <= kernel_names
    health = {t["name"]: t for t in body["domains"]["health"]}
    assert set(health) == {
        "query_health_record",
        "compare_health_index",
        "list_reports",
        "upload_medical_report",
    }
    assert all(t["description"] for t in health.values())  # 每个工具都有一句人话说明


# -- records 数据管理 + audit（F2 / F3） ---------------------------------------------


def test_records_patch_and_audit_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2/F3 端到端：修正指标 → 审计可见；删除报告 → 级联清指标。"""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(sqlite_path=tmp_path / "rec.db")
    with TestClient(app) as c:
        # 用评测同款种子逻辑造一份可管理的数据
        import sqlite3 as s3

        from rolecard_agent.domains.health.service import HealthQueryService
        from rolecard_agent.storage.db import bootstrap

        conn = s3.connect(tmp_path / "rec.db")
        conn.row_factory = s3.Row
        bootstrap(conn, enabled_domains=("health",))
        conn.executescript(
            "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('local', 'd');"
            "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
            "  VALUES ('local-user', 'local', 'u');"
        )
        conn.commit()
        q = HealthQueryService(conn)
        q.create_report(
            user_id="local-user",
            report_type="超声",
            check_time="2026-03-12",
            indices=[{"index_name": "结石直径", "index_value": 6.0, "unit": "mm"}],
        )
        conn.close()

        records = c.get("/api/records").json()
        index_id = records[0]["indices"][0]["index_id"]
        report_id = records[0]["report_id"]

        patch = c.patch(
            f"/api/records/index/{index_id}",
            json={"index_value": 5.5, "is_verified": True},
        )
        assert patch.status_code == 200
        assert float(patch.json()["index_value"]) == 5.5
        assert patch.json()["is_verified"] == 1

        audit = c.get("/api/audit").json()
        assert any(a["action"] == "update_index" and a["target"] == index_id for a in audit)

        assert c.delete(f"/api/records/report/{report_id}").status_code == 204
        assert c.get("/api/records").json() == []
        audit = c.get("/api/audit").json()
        assert any(a["action"] == "delete_report" and a["target"] == report_id for a in audit)


def test_records_patch_unknown_index_404(client: TestClient) -> None:
    assert client.patch("/api/records/index/nope", json={"index_value": 1.0}).status_code == 404


# -- console ------------------------------------------------------------------------


def test_console_page_served(client: TestClient) -> None:
    """Traceability: US-7 / US-9 — GET / 服务前端构建产物（M5：React SPA 壳）。"""
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert '<div id="root">' in res.text  # React 挂载点


def test_console_fallback_when_dist_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-9：dist 未构建时返回回退提示页，后端 API 不受影响。"""
    monkeypatch.setenv("FRONTEND_DIST", str(tmp_path / "no-dist"))
    app = create_app(sqlite_path=tmp_path / "fb.db")
    with TestClient(app) as c:
        res = c.get("/")
        assert res.status_code == 200
        assert "管理控制台" in res.text
        assert "npm run build" in res.text
        assert c.get("/api/roles").status_code == 200  # API 照常工作
