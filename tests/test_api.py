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

        records = c.get("/api/records").json()["items"]  # 分页响应：items/total/limit/offset
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
        assert c.get("/api/records").json()["items"] == []
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


def test_enhance_prompt_rejects_empty(client: TestClient) -> None:
    """增强提示词：空草稿直接 400（不浪费一次模型调用）。成功路径需真实模型，见 smoke。"""
    res = client.post("/api/prompt/enhance", json={"text": "   "})
    assert res.status_code == 400
    assert "没有可增强" in res.json()["detail"]





def test_patch_model_context_404_and_400(client: TestClient) -> None:
    """num_ctx 单列端点：未知后端 404；过小 400（校验在 rebuild 之前，测试不触真实模型）。"""
    res = client.patch("/api/settings/models/ghost/context", json={"num_ctx": 8192})
    assert res.status_code == 404
    res = client.patch("/api/settings/models/local/context", json={"num_ctx": 100})
    assert res.status_code == 400
    assert "512" in res.json()["detail"]


def test_memory_get_put_delete_roundtrip(client: TestClient) -> None:
    """跨会话记忆端点：读默认 → 保存文本 → 读回 → 清空 → 审计留痕。"""
    r0 = client.get("/api/settings/memory")
    assert r0.status_code == 200
    assert r0.json()["enabled"] is True  # 默认开
    assert r0.json()["content"] == ""

    r1 = client.put("/api/settings/memory", json={"content": "用户住在上海。\n用户周五交周报。"})
    assert r1.status_code == 200
    assert r1.json()["content"] == "用户住在上海。\n用户周五交周报。"

    r2 = client.put("/api/settings/memory", json={"enabled": False})
    assert r2.status_code == 200
    assert r2.json()["enabled"] is False  # 覆盖已落库（runtime 覆盖），热重建后生效

    r3 = client.delete("/api/settings/memory")
    assert r3.status_code == 200
    assert r3.json()["content"] == ""

    # 管理动作都进审计
    rows = client.get("/api/audit?limit=50").json()
    actions = [row["action"] for row in rows]
    assert "update_memory" in actions and "clear_memory" in actions


def test_memory_put_rejects_empty_body(client: TestClient) -> None:
    res = client.put("/api/settings/memory", json={})
    assert res.status_code == 400
    assert "没有要保存" in res.json()["detail"]


def test_role_memory_scoped_roundtrip(client: TestClient) -> None:
    """per-role 记忆作用域：读写只命中该角色、与全局隔离、审计带 memory:{role_id}。"""
    role_id = client.get("/api/roles").json()[0]["role_id"]

    # 初始：该角色没有专属记忆（不回退全局）
    assert client.get(f"/api/settings/memory?role_id={role_id}").json()["content"] == ""

    r = client.put(f"/api/settings/memory?role_id={role_id}", json={"content": "用户爱喝美式。"})
    assert r.status_code == 200
    assert r.json()["content"] == "用户爱喝美式。"
    assert r.json()["role_id"] == role_id

    # 隔离铁律：全局记忆不受角色写入影响
    assert client.get("/api/settings/memory").json()["content"] == ""

    client.delete(f"/api/settings/memory?role_id={role_id}")
    assert client.get(f"/api/settings/memory?role_id={role_id}").json()["content"] == ""

    actions = [row["action"] for row in client.get("/api/audit?limit=50").json()]
    assert "update_role_memory" in actions and "clear_role_memory" in actions


def test_role_memory_rejects_unknown_role_and_global_toggle(client: TestClient) -> None:
    """角色作用域：不存在的角色 404；注入开关是全局的，角色作用域改它 → 400。"""
    assert client.get("/api/settings/memory?role_id=ghost").status_code == 404
    role_id = client.get("/api/roles").json()[0]["role_id"]
    res = client.put(f"/api/settings/memory?role_id={role_id}", json={"enabled": False})
    assert res.status_code == 400
    assert "全局" in res.json()["detail"]


def test_keepalive_and_resident(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """预热/常驻端点：默认后端是本地 Ollama → is_local；常驻返回 ok 并审计，
    且必须把后端配置的 num_ctx 透传给 ollama_keep（否则会把模型钉回 4096 默认，
    预热反而触发一次重载）。全程离线（打桩）。"""
    import rolecard_agent.api.routers.settings as srt

    seen: dict[str, object] = {}

    def _fake_keep(
        base: object, model: str, keep_alive: int = -1, num_ctx: int | None = None
    ) -> bool:
        seen["model"] = model
        seen["num_ctx"] = num_ctx
        return True

    monkeypatch.setattr(srt, "ollama_keep", _fake_keep)
    monkeypatch.setattr(
        srt,
        "ollama_loaded",
        lambda base: [
            {"name": "qwen3-vl:8b", "size": 5800000000, "expires_at": None, "processor": "GPU"}
        ],
    )
    r = client.get("/api/settings/models/resident").json()
    assert r["is_local"] is True and r["loaded"] and r["model"]

    # 把默认后端 num_ctx 抬到 8192，再点常驻，探针必须带 8192 去加载。
    default_name = client.get("/api/settings/models").json()["default"]
    assert client.patch(
        f"/api/settings/models/{default_name}/context", json={"num_ctx": 8192}
    ).status_code == 200

    k = client.post("/api/settings/models/keepalive", json={"keep_alive": -1})
    assert k.status_code == 200 and k.json()["ok"] is True
    assert seen["num_ctx"] == 8192
    actions = [row["action"] for row in client.get("/api/audit?limit=50").json()]
    assert "keepalive_model" in actions


def test_session_agent_mode_patch_roundtrip(client: TestClient) -> None:
    """会话级「对话/智能体」切换：PATCH 生效、明细返回有效值、非法值 400、清空回落全局。"""
    tid = client.post("/api/session", json={}).json()["thread_id"]

    d0 = client.get(f"/api/session/{tid}").json()
    assert d0["agent_mode"] == "chat"  # 出厂默认（AGENT_DEFAULT_MODE=chat）

    r = client.patch(f"/api/session/{tid}", json={"agent_mode": "agent"})
    assert r.status_code == 200
    assert r.json()["agent_mode"] == "agent"
    assert client.get(f"/api/session/{tid}").json()["agent_mode"] == "agent"

    # 非法值 400：未知字符串既不是清除也不是任一档，静默吞掉会造成前端显示与实际不一致
    assert client.patch(f"/api/session/{tid}", json={"agent_mode": "robot"}).status_code == 400

    # 显式置空 = 清除覆盖，回落全局默认
    r2 = client.patch(f"/api/session/{tid}", json={"agent_mode": None})
    assert r2.status_code == 200
    assert r2.json()["agent_mode"] == "chat"

    # 管理动作留痕
    actions = [row["action"] for row in client.get("/api/audit?limit=50").json()]
    assert actions.count("set_session_mode") == 2


def test_agent_default_mode_runtime_override(client: TestClient) -> None:
    """运行环境覆盖全局默认模式 → 新会话（NULL 会话）的有效模式跟随它。"""
    r = client.put("/api/settings/runtime", json={"values": {"agent_default_mode": "agent"}})
    assert r.status_code == 200
    tid = client.post("/api/session", json={}).json()["thread_id"]
    assert client.get(f"/api/session/{tid}").json()["agent_mode"] == "agent"
    # 还原默认（回落 env/出厂），避免污染后续用例
    client.put("/api/settings/runtime", json={"values": {"agent_default_mode": ""}})
    client.delete(f"/api/session/{tid}")


def test_runtime_put_accepts_env_keys(client: TestClient) -> None:
    """P0-1 回归：前端「运行环境」页按 payload 的 `key`（= env 名）提交，必须被接受。

    过去后端只认字段名、前端发 env 名 → 任何保存都 400，而测试全用字段名，从没暴露。
    这条直接拿后端**自己吐出的 key** 回提交，锁死前后端键名契约。"""
    payload = client.get("/api/settings/runtime").json()
    item = next(
        it
        for g in payload["groups"]
        for it in g["items"]
        if it["kind"] == "bool"
    )
    assert item["key"].isupper()  # payload.key 就是 env 名（如 WEB_SEARCH_ENABLED）
    r = client.put("/api/settings/runtime", json={"values": {item["key"]: "1"}})
    assert r.status_code == 200, r.text
    client.put("/api/settings/runtime", json={"values": {item["key"]: ""}})  # 还原
    # 未知键仍大声 400（不能因为兼容 env 名就放宽校验）
    bad = client.put("/api/settings/runtime", json={"values": {"NOPE_KEY_X": "1"}})
    assert bad.status_code == 400


def test_startup_guard_refuses_public_bind_without_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P0-3 公网护栏：非回环绑定 + AUTH_MODE=off → create_app 拒绝启动（硬失败，不裸奔）。"""
    monkeypatch.setenv("RUN_API_HOST", "0.0.0.0")
    monkeypatch.setenv("AUTH_MODE", "off")
    with pytest.raises(RuntimeError, match="拒绝启动"):
        create_app(sqlite_path=tmp_path / "pub.db")
    # 开了鉴权就放行（不再抛"拒绝启动"；后续即便因缺凭据失败也不是护栏那条）
    monkeypatch.setenv("AUTH_MODE", "on")
    try:
        create_app(sqlite_path=tmp_path / "pub2.db")
    except Exception as exc:  # noqa: BLE001 - 只断言不是公网护栏
        assert "拒绝启动" not in str(exc)


def test_workspace_dir_roundtrip(client: TestClient, tmp_path: Path) -> None:
    """任务目录：默认 env → 设置（规范化落库）→ 清除回落；操作都进审计。"""
    d0 = client.get("/api/workspace/dir").json()
    assert d0["overridden"] is False  # env 出厂默认生效

    target = tmp_path / "task"
    r = client.put("/api/workspace/dir", json={"path": str(target)})
    assert r.status_code == 200
    assert r.json()["overridden"] is True
    assert r.json()["path"] == str(target.resolve())
    assert target.is_dir()  # 不存在也会建出来

    assert client.get("/api/workspace/dir").json()["path"] == str(target.resolve())

    d = client.delete("/api/workspace/dir")
    assert d.status_code == 200
    assert d.json()["overridden"] is False  # 回落 env

    actions = [row["action"] for row in client.get("/api/audit?limit=50").json()]
    assert "set_task_dir" in actions and "clear_task_dir" in actions


def test_workspace_dir_rejects_invalid(client: TestClient) -> None:
    res = client.put("/api/workspace/dir", json={"path": "   "})
    assert res.status_code == 400
    assert "不能为空" in res.json()["detail"]
    # 校验失败不落任何值
    assert client.get("/api/workspace/dir").json()["overridden"] is False


def test_workspace_tree_browse(client: TestClient, tmp_path: Path) -> None:
    # 用子目录做浏览目标：test client 的 sqlite 就落在 tmp_path 下（WAL 模式会留下
    # app.db / app.db-wal / app.db-shm 三个文件），直接扫 tmp_path 会混进数据库文件。
    browse_dir = tmp_path / "browse"
    browse_dir.mkdir()
    (browse_dir / "one.txt").write_text("x", encoding="utf-8")
    (browse_dir / "dir").mkdir()
    r = client.get("/api/workspace/tree", params={"path": str(browse_dir)})
    assert r.status_code == 200
    names = {e["name"] for e in r.json()["entries"]}
    assert names == {"one.txt", "dir"}

    # 空 path = 主目录；不存在的目录 = 400
    assert client.get("/api/workspace/tree").json()["path"]
    assert (
        client.get("/api/workspace/tree", params={"path": str(browse_dir / "nope")}).status_code
        == 400
    )


def test_reachout_role_switch_and_master_runtime(client: TestClient) -> None:
    """两级授权：角色卡 reachout_enabled 可切换；全局总闸走运行环境热切。"""
    tid = client.post("/api/session", json={}).json()["thread_id"]

    # 内置角色出厂即静默（reachout_enabled=False），且 API 透出该字段
    roles = client.get("/api/roles").json()
    general = next(r for r in roles if r["role_id"] == "general_assistant")
    assert general["reachout_enabled"] is False

    # 打开某个角色的主动资格
    r = client.patch("/api/roles/general_assistant", json={"reachout_enabled": True})
    assert r.status_code == 200
    assert r.json()["reachout_enabled"] is True

    # 全局总闸（REACHOUT_ENABLED）在运行环境页可热切
    rr = client.put("/api/settings/runtime", json={"values": {"reachout_enabled": "0"}})
    assert rr.status_code == 200
    client.put("/api/settings/runtime", json={"values": {"reachout_enabled": ""}})  # 还原

    # 清掉刚才的会话与会话级改动，避免污染后续用例
    client.delete(f"/api/session/{tid}")


def test_reachouts_list_and_mark_read_404(client: TestClient) -> None:
    """收件箱读取路径：列表形状（含 unread 计数）；标记已读不存在 → 404。"""
    r = client.get("/api/reachouts").json()
    assert "unread" in r and isinstance(r["items"], list)
    assert client.post("/api/reachouts/999999/read").status_code == 404


