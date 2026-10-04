"""「服务」页签端点测试（OCR / 嵌入 / 重排的引用、优先级、启停、深度检测）。

Traceability: US-9。

为什么单独一个文件：这组端点在审查里覆盖率为 **38%**（`api/routers/services.py` 101 行、
63 行未执行），是整仓最大的单点盲区 —— 而它管理的是"OCR 走本地还是云端""嵌入用哪把 key"
这类**涉及数据出不出本机**的配置。引用模型的三个承诺必须有机器守护：

  1. **引用≠复制**：删引用行绝不动模型页配置；
  2. **每类服务至少留一个启用端点**：不允许把整类服务停成不可用；
  3. **保存即热生效**：嵌入/重排变更要触发热重建（OCR 每次上传实时读）。

全部离线：默认种子只包含本地实现行（rapidocr / hash / off），不触碰网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from tests.conftest import model_rows


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # 默认后端指向一个必然拒绝连接的端口：深度检测的断言因此与宿主机是否跑着 Ollama 无关。
    monkeypatch.setenv(
        "MODEL_BACKENDS",
        '{"local": {"model": "qwen3-vl:8b", "provider": "ollama",'
        ' "base_url": "http://127.0.0.1:9"}}',
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    return TestClient(create_app(sqlite_path=tmp_path / "services.db"))


def _category(body: dict[str, object], key: str) -> dict[str, object]:
    services = body["services"]
    assert isinstance(services, list)
    return next(s for s in services if s["key"] == key)


# -- 状态视图 ------------------------------------------------------------------


def test_status_view_lists_local_seed_and_the_editable_model_group(client: TestClient) -> None:
    body = client.get("/api/services").json()
    assert _category(body, "ocr")["candidates"][0]["id"] == "rapidocr"
    embedding = _category(body, "embedding")
    assert [c["id"] for c in embedding["candidates"]] == ["hash"]
    assert embedding["effective"] == "hash"  # 第 1 位即生效
    # 模型推理 = 可调优先级（第 1 位 = 默认后端，其后 = 回退链，同一份 kernel_meta 存储）；
    # 候选只含 usage=chat 的行（usage=ocr 的行归 OCR 类别引用，不进推理列表）。
    models = _category(body, "models")
    assert models["readonly"] is False and models.get("order_only") is True
    assert models["effective"] == "local"
    # 候选里不该混入 usage=ocr 的行（一个模型只出现一次）
    assert all(c["id"] in {"local", "siliconflow"} for c in models["candidates"])


def test_local_endpoints_report_availability_without_network(client: TestClient) -> None:
    """轻检测只看配置齐缺与本地探活，**绝不发网络请求**（页面打开即跑）。"""
    body = client.get("/api/services").json()
    hash_row = _category(body, "embedding")["candidates"][0]
    assert hash_row["available"] is True and hash_row["reason"] == "始终可用"


# -- 引用增删 ------------------------------------------------------------------


def test_unknown_category_is_404(client: TestClient) -> None:
    assert client.post("/api/services/nope/endpoints", json={"ref_backend": "x"}).status_code == 404
    assert client.patch("/api/services/nope/endpoints/x", json={"enabled": True}).status_code == 404
    assert client.delete("/api/services/nope/endpoints/x").status_code == 404
    assert client.put("/api/services/nope", json={"order": ["x"]}).status_code == 404


def test_adding_a_reference_requires_a_configured_backend(client: TestClient) -> None:
    """引用只能指向**模型页已配置**的后端 —— 不允许在服务页凭空造配置。"""
    res = client.post("/api/services/embedding/endpoints", json={"ref_backend": "ghost"})
    assert res.status_code == 400
    assert "模型页" in res.json()["detail"]


def test_add_then_remove_a_reference_never_touches_the_model_page(client: TestClient) -> None:
    """**引用≠复制**：加引用不复制配置，删引用不动模型页。"""
    # 先在模型页建一个嵌入用的后端
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b", "api_key": "x"},
                {
                    "name": "embed_c",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-m3",
                    "api_key": "sk-embed-123456",
                },
            ],
        },
    )
    added = client.post("/api/services/embedding/endpoints", json={"ref_backend": "embed_c"})
    assert added.status_code == 201
    body = added.json()
    assert body["ref_backend"] == "embed_c" and body["stale"] is False
    assert body["key_masked"].startswith("sk-")  # 只回掩码，绝不回明文

    # 同类服务内一后端至多一条引用
    assert (
        client.post("/api/services/embedding/endpoints", json={"ref_backend": "embed_c"})
        .status_code
        == 400
    )

    assert client.delete("/api/services/embedding/endpoints/embed_c").status_code == 204
    # 模型页配置必须原样还在
    names = {r["name"] for r in model_rows(client.get("/api/settings/models").json())}
    assert "embed_c" in names


def test_builtin_local_rows_cannot_be_deleted(client: TestClient) -> None:
    """本地实现行（rapidocr/hash/off）固定存在：可停用、可排序，不可删。"""
    res = client.delete("/api/services/embedding/endpoints/hash")
    assert res.status_code == 400
    assert "不可删除" in res.json()["detail"]


# -- 启停（每类至少保留一个） --------------------------------------------------


def test_disabling_the_last_enabled_endpoint_is_refused(client: TestClient) -> None:
    """不允许把整类服务停成"没有实现可用"。"""
    res = client.patch("/api/services/embedding/endpoints/hash", json={"enabled": False})
    assert res.status_code == 400
    assert "至少保留一个" in res.json()["detail"]


def test_disable_then_reenable_when_another_candidate_exists(client: TestClient) -> None:
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b", "api_key": "x"},
                {
                    "name": "r1",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-m3",
                    "api_key": "sk-a",
                },
            ],
        },
    )
    client.post("/api/services/embedding/endpoints", json={"ref_backend": "r1"})

    off = client.patch("/api/services/embedding/endpoints/hash", json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False
    embedding = _category(client.get("/api/services").json(), "embedding")
    # 停用是把该行移出"启用序列"，生效项自然变成剩下的第一位
    assert embedding["effective"] == "r1"
    assert embedding["degraded_from"] is None  # 不是因为"第一位不可用"而降级
    assert [c["order"] for c in embedding["candidates"] if c["id"] == "hash"] == [None]

    on = client.patch("/api/services/embedding/endpoints/hash", json={"enabled": True})
    assert on.status_code == 200 and on.json()["enabled"] is True
    assert _category(client.get("/api/services").json(), "embedding")["effective"] == "hash"


def test_unavailable_first_choice_degrades_and_says_so(tmp_path: Path) -> None:
    """第一位**已启用但不可用** → 顺延到下一个可用者，并如实标注 `degraded_from`。

    这是"降级发生时要以 degraded_from 留痕"这条承诺的直接验证。构造方式必须绕开 API：
    `PUT /api/settings/models` 会**拒绝**没有 key 的 openai 兼容后端（配置错误要当场大声），
    所以这里直接在库里种一行无 key 的云端后端 —— 正好也是"历史遗留的无效配置"的真实形态。
    """
    from fastapi.testclient import TestClient as _TestClient

    from rolecard_agent.storage.db import bootstrap, connect

    db = tmp_path / "degraded.db"
    conn = connect(db)
    bootstrap(conn, enabled_domains=("health",))
    conn.execute(
        "INSERT INTO model_provider (id, provider, base_url, api_key, sort_order) "
        "VALUES ('siliconflow', 'siliconflow', NULL, NULL, 0)"
    )
    conn.execute(
        "INSERT INTO model_backend (name, provider_id, model, sort_order) "
        "VALUES ('cloud_nokey', 'siliconflow', 'BAAI/bge-m3', 0)"
    )
    conn.execute(
        "INSERT INTO service_endpoint (category, id, kind, ref_backend, enabled, sort_order, "
        "builtin) VALUES ('embedding', 'cloud_nokey', 'cloud', 'cloud_nokey', 1, 0, 0)"
    )
    conn.execute(
        "INSERT INTO service_endpoint (category, id, kind, ref_backend, enabled, sort_order, "
        "builtin) VALUES ('embedding', 'hash', 'local', NULL, 1, 1, 1)"
    )
    conn.commit()
    conn.close()

    c = _TestClient(create_app(sqlite_path=db))
    embedding = _category(c.get("/api/services").json(), "embedding")
    assert embedding["effective"] == "hash"  # 第一位不可用 → 顺延
    assert embedding["degraded_from"] == "cloud_nokey"
    first = embedding["candidates"][0]
    assert first["available"] is False
    assert "API Key" in first["reason"]


def test_reference_to_a_deleted_backend_shows_up_as_stale(client: TestClient) -> None:
    """被引用后端在模型页删掉 → 引用行呈现"失效"，**不静默跳过**。

    静默跳过会让操作员以为"这条还在用"，而真相是这个能力已经不在任何实现上。
    """
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b", "api_key": "x"},
                {
                    "name": "gone",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-m3",
                    "api_key": "sk-c",
                },
            ],
        },
    )
    client.post("/api/services/rerank/endpoints", json={"ref_backend": "gone"})
    # 模型页把 gonE 删掉（保留 local 作为默认）
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b", "api_key": "x"}
            ],
        },
    )
    rerank = _category(client.get("/api/services").json(), "rerank")
    stale = next(c for c in rerank["candidates"] if c["id"] == "gone")
    assert stale["stale"] is True
    assert stale["available"] is False
    assert "模型页删除" in stale["reason"]


def test_patching_a_missing_endpoint_is_404(client: TestClient) -> None:
    """不存在 → 404；规则不允许 → 400。两者不能混（服务层用 KeyError / ValueError 区分）。"""
    res = client.patch("/api/services/ocr/endpoints/ghost", json={"enabled": True})
    assert res.status_code == 404


# -- 优先级 --------------------------------------------------------------------


def test_reorder_requires_a_complete_permutation(client: TestClient) -> None:
    """优先级写入必须是该类服务**全部行**的一个排列 —— 拒绝部分序。"""
    both = client.put("/api/services/embedding", json={"order": ["hash", "ghost"]})
    assert both.status_code == 400
    assert client.put("/api/services/embedding", json={"order": ["hash"]}).status_code == 200


def test_reorder_changes_which_endpoint_is_effective(client: TestClient) -> None:
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b", "api_key": "x"},
                {"name": "r2", "provider": "siliconflow", "model": "m", "api_key": "sk-b"},
            ],
        },
    )
    client.post("/api/services/embedding/endpoints", json={"ref_backend": "r2"})
    assert _category(client.get("/api/services").json(), "embedding")["effective"] == "hash"

    res = client.put("/api/services/embedding", json={"order": ["r2", "hash"]})
    assert res.status_code == 200 and res.json()["reloaded"] is True
    assert _category(client.get("/api/services").json(), "embedding")["effective"] == "r2"


# -- 深度检测 ------------------------------------------------------------------


def test_deep_check_reports_unreachable_backend_instead_of_raising(client: TestClient) -> None:
    """探活失败**本身就是检测结果**，不是 500。"""
    res = client.post("/api/services/check")
    assert res.status_code == 200
    assert res.json()["ollama"]["reachable"] is False


def test_deep_check_shares_the_one_probe_implementation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """深度检测**不再自己写第二份"探端点 + 列模型"**（审查快照的三处已漂那条的验收现场）。

    从前这里手抄过一份与 `core/model_probe.list_models` 平行的实现，而它已经漂出行为差：
    取名字时**没过滤空名**。假端点故意回一行缺 `name` 的怪模型 —— 抄的那份会在列表里留一个
    `None`（界面显出一个空位），共用的一份不会。这一支护的是"两页对同一次探测说同一句话"。
    """

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> object:
            return {"models": [{"name": "qwen3-vl:8b"}, {"name": ""}, {"no_name": 1}]}

    seen: list[str] = []

    def fake_get(url: str, **_kw: object) -> _Resp:
        seen.append(url)
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    body = client.post("/api/services/check").json()
    probe = body["ollama"]
    assert probe["reachable"] is True
    # 空名与缺名都被滤掉：只剩那一条真有名字的（`/api/tags` 的路径判定也在这条里照到）。
    assert probe["models"] == ["qwen3-vl:8b"]
    assert seen and seen[0].endswith("/api/tags")


# -- 审计 ----------------------------------------------------------------------


def test_service_changes_are_audited(client: TestClient) -> None:
    """服务配置变更属于管理动作，必须留痕（与插件启停同一纪律）。"""
    client.put("/api/services/embedding", json={"order": ["hash"]})
    actions = {a["action"] for a in client.get("/api/audit?limit=100").json()}
    assert "update_service_order" in actions
# -- 模型推理优先级（= 默认 + 回退链） ------------------------------------------


def test_models_order_maps_to_default_and_fallbacks(client: TestClient) -> None:
    """服务页调模型优先级：第 1 位写默认，其余写回退链 —— 与模型页同一份存储。"""
    # 种子环境只有 local 一个 chat 后端 → 先经模型页补一个云端行
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "m-local", "usage": "chat"},
                {"name": "cloud", "provider": "openai", "model": "m-cloud",
                 "usage": "chat", "api_key": "sk-x"},
            ],
        },
    )
    res = client.put("/api/services/models", json={"order": ["cloud", "local"]})
    assert res.status_code == 200, res.text
    saved = client.get("/api/settings/models").json()
    assert saved["default"] == "cloud"
    assert saved["fallbacks"] == ["local"]

    # 视图顺序跟随优先级；调回 local 优先
    view = client.get("/api/services").json()["services"]
    models = next(s for s in view if s["key"] == "models")
    assert [c["id"] for c in models["candidates"]][0] == "cloud"
    client.put("/api/services/models", json={"order": ["local", "cloud"]})
    saved = client.get("/api/settings/models").json()
    assert saved["default"] == "local" and saved["fallbacks"] == ["cloud"]


def test_models_order_rejects_unknown_and_duplicate(client: TestClient) -> None:
    client.put(
        "/api/settings/models",
        json={"default": "local",
              "backends": [{"name": "local", "provider": "ollama", "model": "m", "usage": "chat"}]},
    )
    assert client.put("/api/services/models", json={"order": ["ghost"]}).status_code == 400
    assert client.put(
        "/api/services/models", json={"order": ["local", "local"]}
    ).status_code == 400
    # 空列表在 schema 层被拒（ReorderBody min_length=1）→ FastAPI 校验错误 422
    assert client.put("/api/services/models", json={"order": []}).status_code == 422


def test_models_order_is_the_chat_pool_and_can_grow_shrink(client: TestClient) -> None:
    """「模型推理」那一节的序列**就是**"谁用于对话"：能加进来、能摘出去，配置本身不受影响。

    拆层前这里是死循环（候选只给 usage=chat 的行，而 usage 只能从模型页写）；批次③ 之后
    服务页给的就是宇宙 = 模型页全部行，所以一个原本只服务嵌入的后端也能被拖进对话。
    """
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "m", "usage": "chat"},
                {"name": "emb", "provider": "ollama", "model": "bge-m3", "usage": "embedding"},
            ],
        },
    )
    models = _category(client.get("/api/services").json(), "models")
    assert [c["id"] for c in models["candidates"]] == ["local"]  # emb 还没参与对话

    res = client.put("/api/services/models", json={"order": ["local", "emb"]})
    assert res.status_code == 200, res.text
    body = client.get("/api/settings/models").json()
    assert body["default"] == "local" and body["fallbacks"] == ["emb"]

    # 摘掉第 1 位：下一位顶上，emb 那行配置仍在（摘引用 ≠ 删配置）
    assert client.put("/api/services/models", json={"order": ["emb"]}).status_code == 200
    body = client.get("/api/settings/models").json()
    assert body["default"] == "emb" and body["fallbacks"] == []
    assert {r["name"] for r in model_rows(body)} == {"local", "emb"}
    # 现在轮到 local 被摘出去：它还在模型页，只是不再用于对话
    assert client.put("/api/services/models", json={"order": ["local"]}).status_code == 200
    body = client.get("/api/settings/models").json()
    assert {r["name"] for r in model_rows(body)} == {"local", "emb"}  # 两行配置都还在
    emb = next(r for r in model_rows(body) if r["name"] == "emb")
    assert emb["used_by"] == []  # 派生用途跟着序列走，没有第二处答案
