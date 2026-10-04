"""API 层测试（管理面：角色卡 CRUD + 插件启停）。

Traceability: US-1, US-2, US-3, US-8（US-7 由控制台页面端点覆盖）。

全部离线：用 `tmp_path` 里的临时库，不依赖 Ollama 或网络。每个测试独立建库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.core.migrations import MIGRATION_PLAN
from tests.conftest import model_rows


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
        bootstrap(conn, enabled_domains=("health",), plan=MIGRATION_PLAN)
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


def test_patch_model_context_roundtrip(client: TestClient) -> None:
    """改窗口要**读得回来**并留痕：本地推理服务的「常驻」档按这个数字下发给 Ollama，
    存了 8192、探针却按 4096 加载，用户看到的就是"预热完还要再等一次冷加载"。"""
    name = client.get("/api/settings/models").json()["default"]
    assert name
    res = client.patch(f"/api/settings/models/{name}/context", json={"num_ctx": 8192})
    assert res.status_code == 200 and res.json()["num_ctx"] == 8192
    rows = model_rows(client.get("/api/settings/models").json())
    row = next(r for r in rows if r["name"] == name)
    assert row["num_ctx"] == 8192
    actions = [a["action"] for a in client.get("/api/audit?limit=50").json()]
    assert "update_model_context" in actions


def test_memory_get_put_delete_roundtrip(client: TestClient) -> None:
    """跨会话记忆端点：读默认 → 整段保存 → 读回（渲染视图）→ 清空 → 审计留痕。

    `content` 现在是**条目渲染出来的文本**（带 `- ` 前缀），事实面是 `items`；
    两个字段同源，不是两处真相。
    """
    r0 = client.get("/api/settings/memory")
    assert r0.status_code == 200
    assert r0.json()["enabled"] is True  # 默认开
    assert r0.json()["content"] == ""
    assert r0.json()["items"] == []

    r1 = client.put("/api/settings/memory", json={"content": "用户住在上海。\n用户周五交周报。"})
    assert r1.status_code == 200
    # 顺序是"近因×频次"（同分时后写的在前），不是写入顺序：注入侧要的是这个顺序，
    # 面板列出来也是这个顺序 —— 一个顺序两处共用，才有"看到的就是注入的"。
    assert r1.json()["content"] == "- 用户周五交周报。\n- 用户住在上海。"
    assert [i["text"] for i in r1.json()["items"]] == ["用户周五交周报。", "用户住在上海。"]

    r2 = client.put("/api/settings/memory", json={"enabled": False})
    assert r2.status_code == 200
    assert r2.json()["enabled"] is False  # 覆盖已落库（runtime 覆盖），热重建后生效

    r3 = client.delete("/api/settings/memory")
    assert r3.status_code == 200
    assert r3.json()["content"] == ""
    assert r3.json()["items"] == []

    # 管理动作都进审计
    rows = client.get("/api/audit?limit=50").json()
    actions = [row["action"] for row in rows]
    assert "update_memory" in actions and "clear_memory" in actions


def test_memory_item_endpoints_crud_and_pin(client: TestClient) -> None:
    """逐条端点：加 → 钉住 → 改 → 删；钉住的条目不被整段覆写抹掉。"""
    added = client.post("/api/settings/memory/item", json={"text": "用户对花生过敏"})
    assert added.status_code == 200
    items = added.json()["items"]
    assert len(items) == 1
    item_id = items[0]["id"]
    assert items[0]["source"] == "manual"

    pinned = client.patch(f"/api/settings/memory/item/{item_id}", json={"pinned": True})
    assert pinned.json()["items"][0]["pinned"] is True
    # 载荷里就带显著性：界面那三档下拉读的是它，不是第二处接口。默认档 1 = 一般。
    assert pinned.json()["items"][0]["importance"] == 1

    # 「次要」= 0 是一个**要能显式写进去**的值：早先那种"空就不改"的写法会把它当成没给。
    demoted = client.patch(f"/api/settings/memory/item/{item_id}", json={"importance": 0})
    assert demoted.status_code == 200
    assert demoted.json()["items"][0]["importance"] == 0
    # 越界的数由 `clamp_importance` 钳进 0..2，不报错也不落库成 7。
    over = client.patch(f"/api/settings/memory/item/{item_id}", json={"importance": 7})
    assert over.json()["items"][0]["importance"] == 2
    assert client.patch(f"/api/settings/memory/item/{item_id}", json={}).status_code == 400

    edited = client.patch(
        f"/api/settings/memory/item/{item_id}", json={"text": "用户对花生严重过敏"}
    )
    assert edited.json()["items"][0]["text"] == "用户对花生严重过敏"

    # 整段覆写只动未钉住的：钉住是用户明确的表态
    overwritten = client.put("/api/settings/memory", json={"content": "另一条"})
    texts = [i["text"] for i in overwritten.json()["items"]]
    assert "用户对花生严重过敏" in texts and "另一条" in texts

    assert client.delete(f"/api/settings/memory/item/{item_id}").status_code == 200
    assert client.delete(f"/api/settings/memory/item/{item_id}").status_code == 404
    missing = client.patch("/api/settings/memory/item/999999", json={"pinned": True})
    assert missing.status_code == 404
    assert client.post("/api/settings/memory/item", json={"text": "  "}).status_code == 400


def test_memory_items_can_be_merged(tmp_path: Path) -> None:
    """S-3 的后端那一半：两条合一条，**被合掉的那条退役并指向留下的那条**。

    判据不是"列表少了一条"，而是那条链还在：`invalidated_at` + `superseded_by` 是
    "整理错了能回滚"的唯一依据（不变式 15：退役不物理删）。今天没有任何读端点会给出
    已退役的条目，所以这一步直接看库 —— 看的是落库形状，不是接口口径。
    """
    app = create_app(sqlite_path=tmp_path / "merge.db")
    with TestClient(app) as client:

        def id_of(payload: dict, text: str) -> int:
            return next(i["id"] for i in payload["items"] if i["text"] == text)

        first = client.post("/api/settings/memory/item", json={"text": "用户住在上海"}).json()
        second = client.post(
            "/api/settings/memory/item", json={"text": "用户在上海一家医院工作"}
        ).json()
        a, b = id_of(first, "用户住在上海"), id_of(second, "用户在上海一家医院工作")

        merged = client.post(
            f"/api/settings/memory/item/{a}/merge/{b}",
            json={"text": "用户住在上海，在一家医院工作"},
        )
        assert merged.status_code == 200
        items = merged.json()["items"]
        assert [i["text"] for i in items] == ["用户住在上海，在一家医院工作"]
        assert items[0]["id"] == a

        with sqlite3.connect(tmp_path / "merge.db") as conn:
            row = conn.execute(
                "SELECT invalidated_at, superseded_by FROM role_memory_item WHERE id = ?", (b,)
            ).fetchone()
        assert row is not None and row[0] is not None and row[1] == a

        # 不给句子 = 退回"两句拼一起"那个兜底写法（批量整理那条路没人逐条改句子）。
        third = client.post("/api/settings/memory/item", json={"text": "用户养了一只猫"}).json()
        c = id_of(third, "用户养了一只猫")
        joined = client.post(f"/api/settings/memory/item/{c}/merge/{a}", json={}).json()
        assert "用户养了一只猫" in joined["items"][0]["text"]
        assert "上海" in joined["items"][0]["text"]

        # 三种挡在门口的判定：自己跟自己合、不存在的条目、跨桶。
        assert client.post(f"/api/settings/memory/item/{c}/merge/{c}", json={}).status_code == 400
        assert (
            client.post("/api/settings/memory/item/99998/merge/99999", json={}).status_code == 404
        )
        role = client.post(
            "/api/settings/memory/item",
            json={"text": "她记得用户提过体检"},
            params={"role_id": "medical_archivist"},
        )
        assert role.status_code == 200
        cross = client.post(
            f"/api/settings/memory/item/{id_of(role.json(), '她记得用户提过体检')}/merge/{c}",
            json={},
        )
        assert cross.status_code == 400 and "同一个记忆桶" in cross.json()["detail"]


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
    assert r.json()["content"] == "- 用户爱喝美式。"
    assert [i["text"] for i in r.json()["items"]] == ["用户爱喝美式。"]
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


def test_reachouts_page_carries_why_each_role_is_quiet(client: TestClient) -> None:
    """`S-8`：收件箱那份负载里带一格"她此刻为什么静默"，抽屉与运行环境页共用它。

    为什么不单开 `/api/reachouts/status`：抽屉本来每 3 秒就在读这个端点，再开一条等于
    为了一句话新增一次轮询、一个新路由分级、一处会漂移的时刻源（判据见 `reachout.quiet_gate`）。
    """
    client.patch("/api/roles/general_assistant", json={"reachout_enabled": True})
    quiet = client.get("/api/reachouts").json()["quiet"]
    row = next(r for r in quiet if r["role_id"] == "general_assistant")
    assert row["role_name"]
    assert set(row) == {
        "role_id", "role_name", "why", "next_ok_at", "streak", "unread",
    }
    # 内置角色出厂静默 ⇒ 不该出现在这一格里（出现了就是承诺"她本来会来找你"）
    client.patch("/api/roles/general_assistant", json={"reachout_enabled": False})
    ids = [r["role_id"] for r in client.get("/api/reachouts").json()["quiet"]]
    assert "general_assistant" not in ids
    # 标记已读/清空那几条写路径回的是同一份形状，前端只认一个读法
    assert "quiet" in client.post("/api/reachouts/read-all").json()


def test_reachouts_list_and_mark_read_404(client: TestClient) -> None:
    """收件箱读取路径：列表形状（含 unread 计数 + 折叠窗口）；标记**不存在**的记录 → 404。

    `merge_days` 为什么在列表响应里：折叠是"这一摞怎么显示"的一部分，而前端去读
    「运行环境」拿它是越权（那条路由是 operator 档，收件箱是使用者档）。
    """
    r = client.get("/api/reachouts").json()
    assert "unread" in r and isinstance(r["items"], list)
    assert r["merge_days"] == 1  # 出厂：按天折一摞
    # 在线覆盖能改到它（写点在「记忆与任务目录」，保存即热生效）
    client.put("/api/settings/runtime", json={"values": {"reachout_merge_days": "7"}})
    assert client.get("/api/reachouts").json()["merge_days"] == 7
    client.put("/api/settings/runtime", json={"values": {"reachout_merge_days": ""}})  # 还原
    # 非法档（只有 1/3/7）当场拒绝，不悄悄接受一个没人能预判界面形状的窗口
    bad = client.put("/api/settings/runtime", json={"values": {"reachout_merge_days": "5"}})
    assert bad.status_code == 400
    assert client.post("/api/reachouts/999999/read").status_code == 404


def test_marking_an_already_read_reachout_is_not_an_error(client: TestClient) -> None:
    """点一条**已读**的历史不该弹"主动消息不存在"（用户 2026-09-20 报的症状）。

    收件箱列的是"未读 + 最近历史"，所以点已读的那几条是正常路径。以前"没东西可改"
    （已读）与"记录不存在"共用一个 False，于是列表里明明看得见的消息被报成不存在 ——
    一句谎话，还会把"这条点不开"渲染成"这条没了"。
    """
    conn = client.app.state.ctx.conn
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, state) VALUES (?, ?, ?, 'read')",
        ("general_assistant", "通用助手", "看过了的那条"),
    )
    conn.commit()
    rid = conn.execute("SELECT id FROM agent_reachout ORDER BY id DESC LIMIT 1").fetchone()["id"]

    res = client.post(f"/api/reachouts/{int(rid)}/read")
    assert res.status_code == 200
    assert res.json()["unread"] == 0
    assert any(i["id"] == int(rid) for i in res.json()["items"])  # 还在历史里，没被"标没了"




def test_reachouts_point_at_the_proactive_thread_and_read_by_role(client: TestClient) -> None:
    """收件箱的"能点进去"这条合同：会话存在才给跳转目标 + 按角色一次标完。

    为什么在 API 层再钉一次：单测证明了写入侧落了会话，而用户点的是这里的 `thread_id`
    —— 它一旦改名字或漏字段，前端只会表现成"点了没反应"，正是要修的那个症状。
    """
    from rolecard_agent.base.identity import DEFAULT_USER_ID
    from rolecard_agent.features.reachout import proactive_thread_id

    conn = client.app.state.ctx.conn
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text) VALUES ('general_assistant', ?, ?)",
        ("通用助手", "今天腰还酸吗"),
    )
    conn.commit()

    page = client.get("/api/reachouts").json()
    assert page["unread"] == 1
    tid = proactive_thread_id("general_assistant", user_id=DEFAULT_USER_ID)
    # 会话还不存在（这条消息是本功能上线前落的形状）→ 不给死链接。
    assert page["items"][0]["thread_id"] is None
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title)"
        " VALUES (?, ?, 'general_assistant', ?)",
        (tid, DEFAULT_USER_ID, "通用助手 · 主动找你"),
    )
    conn.commit()
    assert client.get("/api/reachouts").json()["items"][0]["thread_id"] == tid

    r = client.post("/api/reachouts/read-by-role", params={"role_id": "general_assistant"})
    assert r.status_code == 200
    assert r.json()["marked"] == 1 and r.json()["unread"] == 0

    # 没点角色的条目：无未读可标不是错误，也不该顺手把别人的未读带走。
    ghost = client.post("/api/reachouts/read-by-role", params={"role_id": "ghost"}).json()
    assert ghost["marked"] == 0 and ghost["unread"] == 0
    # 少了 role_id 就不能盲标（否则一次点击清掉所有人）。
    assert client.post("/api/reachouts/read-by-role").status_code == 422


def test_deleting_a_session_keeps_memory_and_the_inbox_ledger(client: TestClient) -> None:
    """删会话的边界（用户 2026-09-21）：**只删这条对话本身**，其余各有自己的主人。

    - 已进角色记忆的事实**留下** —— 那是从对话里提炼出来的、关于用户的东西，删对话不该
      把它一起销毁（否则"提取精华"提完再删会话 = 白做）；
    - 收件箱那几行**留下** —— 它们是"它哪天主动找过我"的账，桌宠气泡与未读数都读这里；
      会话没了之后它们不再有跳转目标（`thread_id` 回落 None），点一下只标已读，不给死链；
    - 走掉的只有 thread 行与它的 checkpoint / writes（不留可被复活的孤儿）。
    """
    from rolecard_agent.base.identity import DEFAULT_USER_ID
    from rolecard_agent.features.reachout import proactive_thread_id

    conn = client.app.state.ctx.conn
    tid = proactive_thread_id("general_assistant", user_id=DEFAULT_USER_ID)
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title)"
        " VALUES (?, ?, 'general_assistant', ?)",
        (tid, DEFAULT_USER_ID, "通用助手 · 主动找你"),
    )
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, state) "
        "VALUES ('general_assistant', '通用助手', '今天腰还酸吗', 'unread')"
    )
    conn.commit()
    assert client.get("/api/reachouts").json()["items"][0]["thread_id"] == tid
    added = client.post(
        "/api/settings/memory/item",
        params={"role_id": "general_assistant"},
        json={"text": "用户每周三晚上练琴"},
    )
    assert added.status_code == 200

    assert client.delete(f"/api/session/{tid}").status_code == 204

    assert client.get(f"/api/session/{tid}").status_code == 404
    kept = client.get("/api/settings/memory", params={"role_id": "general_assistant"}).json()
    assert [i["text"] for i in kept["items"]] == ["用户每周三晚上练琴"]  # 记忆活着
    page = client.get("/api/reachouts").json()
    assert page["unread"] == 1  # 未读账也活着（红点不该因为删会话而谎报"读过了"）
    assert page["items"][0]["thread_id"] is None  # 但不再给一个不存在的会话当链接
    orphans = conn.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (tid,)
    ).fetchone()[0]
    assert orphans == 0  # 没有可被"复活"的孤儿 checkpoint


def test_proactive_session_ensure_is_idempotent_and_recreatable(client: TestClient) -> None:
    """桌宠的落点（设计稿 §7.2.1）：**一个角色一条线**，调几次都是同一行。

    幂等要能扛住两件事：角色从没主动说过话（桌宠上先聊起来）、以及用户把这条会话删了
    （下一条主动消息或下一次桌宠发消息会把它长回来 —— 记忆不跟着走，见上一条用例）。
    """
    from rolecard_agent.features.reachout import proactive_thread_id

    first = client.post("/api/session/proactive", json={"role_id": "general_assistant"})
    assert first.status_code == 201
    tid = first.json()["thread_id"]
    assert tid == proactive_thread_id("general_assistant", user_id=DEFAULT_USER_ID)
    assert first.json()["role_name"] == "通用助手"

    again = client.post("/api/session/proactive", json={"role_id": "general_assistant"})
    assert again.json()["thread_id"] == tid  # 不会开出第二条线
    rows = client.get("/api/sessions").json()
    assert [s for s in rows if s["thread_id"] == tid].__len__() == 1
    assert any(s["title"] == "通用助手 · 主动找你" for s in rows)

    assert client.delete(f"/api/session/{tid}").status_code == 204
    assert client.get(f"/api/session/{tid}").status_code == 404
    assert client.post("/api/session/proactive", json={"role_id": "general_assistant"}).json()[
        "thread_id"
    ] == tid


def test_proactive_session_requires_a_real_role(client: TestClient) -> None:
    """凭空造一条指向不存在角色的线 = 给收件箱攒一个永远点不开的目标。"""
    assert client.post("/api/session/proactive", json={"role_id": "ghost"}).status_code == 404
    assert client.post("/api/session/proactive", json={}).status_code == 422


# -- 主动消息收件箱的删除（用户 2026-09-23："抽屉没删除功能，越堆越多"）----------------


def _inbox_conn(tmp_path: Path) -> Any:
    from rolecard_agent.storage.db import connect

    return connect(tmp_path / "app.db")


def _record(conn: Any, role_id: str, text: str, *, keep: int = 0) -> None:
    from rolecard_agent.features import reachout as svc
    from rolecard_agent.roles.models import RoleCard

    role = RoleCard(role_id=role_id, role_name="晚晴", system_prompt="x", reachout_keep=keep)
    svc.record_reachout(conn, role, text, user_id=DEFAULT_USER_ID)


def test_reachout_inbox_deletes_one_row(client: TestClient, tmp_path: Path) -> None:
    """删一行只让那一行从抽屉里消失；**她主动说出口的那句话不跟着没**（它在会话里）。"""
    conn = _inbox_conn(tmp_path)
    _record(conn, "wan", "第一条")
    _record(conn, "wan", "第二条")
    items = client.get("/api/reachouts").json()["items"]
    assert [i["text"] for i in items] == ["第二条", "第一条"]

    res = client.delete(f"/api/reachouts/{items[1]['id']}")
    assert res.status_code == 200 and res.json()["deleted"] == 1
    assert [i["text"] for i in res.json()["items"]] == ["第二条"]
    assert client.delete("/api/reachouts/999999").status_code == 404
    conn.close()


def test_reachout_inbox_clear_is_scoped_by_role(client: TestClient, tmp_path: Path) -> None:
    conn = _inbox_conn(tmp_path)
    _record(conn, "wan", "晚晴的")
    _record(conn, "bai", "白也的")
    assert client.delete("/api/reachouts?role_id=wan").json()["deleted"] == 1
    left = client.get("/api/reachouts").json()["items"]
    assert [i["role_id"] for i in left] == ["bai"], "清空一个角色不该动别的角色"
    assert client.delete("/api/reachouts").json()["deleted"] == 1
    assert client.get("/api/reachouts").json()["items"] == []
    conn.close()


def test_role_reachout_keep_prunes_on_write(client: TestClient, tmp_path: Path) -> None:
    """角色卡上写了"只留 N 条"，那就**每次落新行时**修剪：不为此再跑一个定时任务。"""
    role_id = "general_assistant"  # 新库里只有两个内置角色，`wan` 是不存在的（PATCH 会 404）
    res = client.patch(f"/api/roles/{role_id}", json={"reachout_keep": 2})
    assert res.status_code == 200 and res.json()["reachout_keep"] == 2, "新列要能过角色卡的 PATCH"

    conn = _inbox_conn(tmp_path)
    # 没有 `GET /api/roles/{id}` 这条路由（列表端点是唯一的读口），所以直接用 PATCH 的回执。
    keep = res.json()["reachout_keep"]
    for i in range(5):
        _record(conn, role_id, f"第{i}条", keep=keep)
    texts = [i["text"] for i in client.get(f"/api/reachouts?role_id={role_id}").json()["items"]]
    assert texts == ["第4条", "第3条"], f"只该留最近两条，实际 {texts}"
    conn.close()


def test_reachout_keep_zero_keeps_everything(client: TestClient, tmp_path: Path) -> None:
    """默认 0 = 不自动删 —— 这条钉的是"别默认替用户丢历史"。"""
    conn = _inbox_conn(tmp_path)
    for i in range(4):
        _record(conn, "wan", f"第{i}条", keep=0)
    assert len(client.get("/api/reachouts?role_id=wan").json()["items"]) == 4
    conn.close()


def test_proactive_thread_lookup_is_read_only(client: TestClient) -> None:
    """`GET /api/session/proactive` 只回答"在不在"，**不建行**（2026-09-23 桌宠历史那条）。"""
    ghost = client.get("/api/session/proactive", params={"role_id": "general_assistant"})
    assert ghost.status_code == 200 and ghost.json()["thread_id"] is None
    assert client.get("/api/sessions").json() == [], "只是问一句，侧栏不该因此多出一条会话"

    tid = client.post("/api/session/proactive", json={"role_id": "general_assistant"}).json()[
        "thread_id"
    ]
    got = client.get("/api/session/proactive", params={"role_id": "general_assistant"})
    assert got.json()["thread_id"] == tid
    assert client.get("/api/session/proactive", params={"role_id": "ghost"}).json()[
        "thread_id"
    ] is None


def test_read_all_marks_every_role(client: TestClient, tmp_path: Path) -> None:
    """进入对话界面 = 都看过了（用户 2026-09-23 定的口径）：read-all 跨角色一次标完。"""
    from rolecard_agent.features import reachout as svc
    from rolecard_agent.roles.models import RoleCard
    from rolecard_agent.storage.db import connect

    conn = connect(tmp_path / "app.db")
    for rid in ("wan", "bai"):
        svc.record_reachout(
        conn,
        RoleCard(role_id=rid, role_name=rid, system_prompt="x"),
        "在吗",
        user_id=DEFAULT_USER_ID,
    )

    assert client.get("/api/reachouts").json()["unread"] == 2
    body0 = client.get("/api/reachouts").json()
    # "某个角色有几条没读"由后端算一次（审计 §12.11 的"三份各算"那一格）
    assert body0["unread_by_role"] == {"wan": 1, "bai": 1}
    res = client.post("/api/reachouts/read-all")
    assert res.status_code == 200 and res.json()["marked"] == 2
    body = res.json()
    assert body["unread"] == 0 and len(body["items"]) == 2, "标已读不是删：行还要留在抽屉里"
    assert body["unread_by_role"] == {}
    conn.close()


def test_stop_turn_endpoint_only_raises_the_flag(client: TestClient) -> None:
    """`POST /api/session/{tid}/stop` 的全部职责就是立一枚取消旗（#18）。

    停一个"已经不跑了"的会话也回 200：用户的按钮不该因为手速比流快而报错，而旗子在下一轮
    开始时会被清掉（`run_turn` 开头），所以这里没有需要清理的状态。真正收手的是
    `call_model` 里那个分块循环 —— 由 `tests/unit/test_turn_stop.py` 钉住。
    """
    from rolecard_agent.core.thread_locks import clear_stop, stop_requested

    tid = client.post("/api/session", json={"role_id": "general_assistant"}).json()["thread_id"]
    try:
        assert stop_requested(tid) is False
        res = client.post(f"/api/session/{tid}/stop")
        assert res.status_code == 200
        assert res.json() == {"thread_id": tid, "requested": True}
        assert stop_requested(tid) is True
        assert client.post(f"/api/session/{tid}/stop").status_code == 200, "按两次不该有第二种结果"
        actions = [row["action"] for row in client.get("/api/audit?limit=50").json()]
        assert "stop_turn" in actions, "停止是一个会改变系统状态的动作，得留痕"
    finally:
        clear_stop(tid)


def test_runtime_bool_reads_back_in_wire_format(client: TestClient) -> None:
    """`R102-15`：布尔关掉之后设置页不许显示"已开启"。

    写侧收 "0"，读侧必须回 "0"（truthy/falsy 同一口径）—— 从前 `_display` 对 bool 走
    `str(value)`，线上发 `"False"`，前端判据 `value !== "0"` 两关都过。
    变异：把 `_display` 的 bool 分支摘掉 ⇒ 本条红（GET 回 "False"）。
    """
    res = client.put("/api/settings/runtime", json={"values": {"reachout_enabled": "0"}})
    assert res.status_code == 200
    payload = client.get("/api/settings/runtime").json()
    groups = payload["groups"] if isinstance(payload, dict) and "groups" in payload else payload
    rows = [r for g in groups for r in g["items"] if r["key"] == "REACHOUT_ENABLED"]
    assert rows, [g["key"] for g in groups]
    assert rows[0]["value"] == "0", rows[0]
    client.put("/api/settings/runtime", json={"values": {"reachout_enabled": ""}})  # 还原
