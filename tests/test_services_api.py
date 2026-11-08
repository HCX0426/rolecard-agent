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
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreachable_backend: str) -> TestClient:
    # 默认后端指向一个立即拒绝的后端：深度检测的断言因此与宿主机是否跑着 Ollama 无关。
    # （P3：不再用 :9 —— 本机网络栈拒它要 ~3.4s，夹具的 accept-即关端点 ~0.03s。）
    monkeypatch.setenv(
        "MODEL_BACKENDS",
        '{"local": {"model": "qwen3-vl:8b", "provider": "ollama",'
        f' "base_url": "{unreachable_backend}"}}}}',
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    return TestClient(create_app(sqlite_path=tmp_path / "services.db"))


def _category(body: dict[str, object], key: str) -> dict[str, object]:
    services = body["services"]
    assert isinstance(services, list)
    return next(s for s in services if s["key"] == key)


# -- 状态视图 ------------------------------------------------------------------


def test_status_view_lists_local_seed_and_the_editable_model_group(
    client: TestClient, unreachable_backend: str
) -> None:
    body = client.get("/api/services").json()
    assert _category(body, "ocr")["candidates"][0]["id"] == "rapidocr"
    embedding = _category(body, "embedding")
    assert [c["id"] for c in embedding["candidates"]] == ["hash"]
    assert embedding["effective"] == "hash"  # 第 1 位即生效
    # 模型推理 = 可调优先级（第 1 位 = 默认后端，其后 = 回退链，同一份 kernel_meta 存储）；
    # 候选只含 usage=chat 的行（usage=ocr 的行归 OCR 类别引用，不进推理列表）。
    models = _category(body, "models")
    assert models["readonly"] is False and models.get("order_only") is True
    # 生效项与默认是**两件事**（ENGI-36 A）：本 fixture 故意把 local 指到端口 9（必然不可达，
    # 让深度检测的断言与宿主机是否跑 Ollama 无关）。于是诚实的 `effective` 是 None —— 这正是
    # 修前要消灭的形状：从前这里报 effective=local、界面写着"一切正常"，运行时其实会悄悄滑向云端。
    assert models["default_backend"] == "local"  # 配置里的第 1 位（徽标钉它，稳定）
    assert models["effective"] is None  # 实际没有可用的生效者
    # 候选里不该混入 usage=ocr 的行（一个模型只出现一次）
    assert all(c["id"] in {"local", "siliconflow"} for c in models["candidates"])


def test_models_effective_follows_availability(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local 探活为真 ⇒ 生效=local；探活为假 ⇒ 生效 None 且默认仍是 local（两个字段各自说话）。"""
    from rolecard_agent.core.models import services as services_mod

    monkeypatch.setattr(services_mod, "vision_model_ready", lambda *a, **k: True)
    models = _category(client.get("/api/services").json(), "models")
    assert models["effective"] == "local"
    assert models["effective_kind"] == "local"
    assert models["degraded_from"] is None
    monkeypatch.setattr(services_mod, "vision_model_ready", lambda *a, **k: False)
    models = _category(client.get("/api/services").json(), "models")
    assert models["effective"] is None
    assert models["default_backend"] == "local"  # 默认不跟着漂走 —— 这正是分开两个字段的全部理由


def test_models_degrade_to_cloud_and_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认（本地）不可用 + 链里有可用云端 ⇒ 顺延到云端并标 `degraded_from`（ENGI-36 的盲区那格）。

    这就是当年"卸了 Ollama 还在用云端、界面三处无信号"的形状：修后它必须在服务页说出来。
    """
    from rolecard_agent.core.models import services as services_mod

    # local 不可达，其余（云端按 has_key 判，PUT 过的那条有 key）可达
    monkeypatch.setattr(services_mod, "vision_model_ready", lambda *a, **k: False)
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b"},
                {
                    "name": "cloud",
                    "provider": "siliconflow",
                    "model": "DeepSeek-V4-Flash",
                    "api_key": "sk-cloud-123456",
                },
            ],
            "fallbacks": ["cloud"],
        },
    )
    models = _category(client.get("/api/services").json(), "models")
    assert models["effective"] == "cloud"
    assert models["effective_kind"] == "cloud"  # 前端据此在横幅里追加"注意：发往云端"
    assert models["degraded_from"] == "local"
    assert models["default_backend"] == "local"


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


def test_add_then_remove_a_reference_never_touches_the_model_page(
    client: TestClient, unreachable_backend: str
) -> None:
    """**引用≠复制**：加引用不复制配置，删引用不动模型页。"""
    # 先在模型页建一个嵌入用的后端
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {
                    "name": "local",
                    "provider": "ollama",
                    "model": "qwen3-vl:8b",
                    "base_url": unreachable_backend,
                    "api_key": "x",
                },
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
        client.post(
            "/api/services/embedding/endpoints", json={"ref_backend": "embed_c"}
        ).status_code
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


def test_disable_then_reenable_when_another_candidate_exists(
    client: TestClient, unreachable_backend: str
) -> None:
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {
                    "name": "local",
                    "provider": "ollama",
                    "model": "qwen3-vl:8b",
                    "base_url": unreachable_backend,
                    "api_key": "x",
                },
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
    bootstrap(conn, enabled_domains=())
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


def test_deleting_a_model_on_the_model_page_removes_its_service_references(
    client: TestClient, unreachable_backend: str
) -> None:
    """模型页删模型（DELETE）→ 服务页对应引用行**同步消失**（用户 2026-11-08）。

    旧行为是留一行「引用已失效」占着名单：模型都没了，幽灵行只会让人误以为还能抢救。
    摘引用与删配置从此是同一个动作 —— chat 引用本就随行走（重写让位），这条钉能力三类。
    """
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {
                    "name": "local",
                    "provider": "ollama",
                    "model": "qwen3-vl:8b",
                    "base_url": unreachable_backend,
                    "api_key": "x",
                },
                {
                    "name": "gone",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-m3",
                    "api_key": "sk-c",
                },
            ],
        },
    )
    for cat in ("ocr", "embedding", "rerank"):
        res = client.post(f"/api/services/{cat}/endpoints", json={"ref_backend": "gone"})
        assert res.status_code == 201, res.text  # 后端还在时引用加得上
    assert client.delete("/api/settings/models/gone").status_code == 204
    body = client.get("/api/services").json()
    for cat in ("ocr", "embedding", "rerank"):
        ids = [c["id"] for c in _category(body, cat)["candidates"]]
        assert "gone" not in ids, f"{cat} 还留着已删模型的引用行：{ids}"
    models = _category(body, "models")
    assert all(c["id"] != "gone" for c in models["candidates"])
    assert models["default_backend"] == "local"  # 对话默认不悬空


def test_shrinking_backends_on_the_model_page_drops_its_service_references(
    client: TestClient, unreachable_backend: str
) -> None:
    """整表保存（PUT 缩表）与 DELETE 同一种结局：引用行随模型消失 —— 两条删除路径不许两种行为。"""
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {
                    "name": "local",
                    "provider": "ollama",
                    "model": "qwen3-vl:8b",
                    "base_url": unreachable_backend,
                    "api_key": "x",
                },
                {
                    "name": "gone",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-m3",
                    "api_key": "sk-c",
                },
            ],
        },
    )
    added = client.post("/api/services/rerank/endpoints", json={"ref_backend": "gone"})
    assert added.status_code == 201, added.text
    # 缩表：只剩 local —— gone 在这次保存里被删掉
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {
                    "name": "local",
                    "provider": "ollama",
                    "model": "qwen3-vl:8b",
                    "base_url": unreachable_backend,
                    "api_key": "x",
                }
            ],
        },
    )
    rerank_ids = [
        c["id"] for c in _category(client.get("/api/services").json(), "rerank")["candidates"]
    ]
    assert "gone" not in rerank_ids


def test_a_hand_inserted_dangling_reference_still_reads_as_stale(tmp_path: Path) -> None:
    """防御口径保留：绕过模型页手工插的引用行（历史库 / SQL）仍呈现「失效」，不静默跳过。

    级联删只覆盖模型页这条正规路；直接插库的孤儿行没人替它善后 —— 静默跳过会让操作员
    以为"这条还在用"，而真相是这个能力已经不在任何实现上。
    """
    from rolecard_agent.storage.db import bootstrap, connect

    db = tmp_path / "stale.db"
    conn = connect(db)
    bootstrap(conn, enabled_domains=())
    conn.execute(
        "INSERT INTO service_endpoint (category, id, kind, ref_backend, enabled, sort_order, "
        "builtin) VALUES ('rerank', 'ghost', 'cloud', 'ghost', 1, 0, 0)"
    )
    conn.commit()
    conn.close()
    c = TestClient(create_app(sqlite_path=db))
    rerank = _category(c.get("/api/services").json(), "rerank")
    stale = next(x for x in rerank["candidates"] if x["id"] == "ghost")
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


def test_reorder_changes_which_endpoint_is_effective(
    client: TestClient, unreachable_backend: str
) -> None:
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {
                    "name": "local",
                    "provider": "ollama",
                    "model": "qwen3-vl:8b",
                    "base_url": unreachable_backend,
                    "api_key": "x",
                },
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


def test_models_order_maps_to_default_and_fallbacks(
    client: TestClient, unreachable_backend: str
) -> None:
    """服务页调模型优先级：第 1 位写默认，其余写回退链 —— 与模型页同一份存储。"""
    # 种子环境只有 local 一个 chat 后端 → 先经模型页补一个云端行
    client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "m-local", "usage": "chat"},
                {
                    "name": "cloud",
                    "provider": "openai",
                    "model": "m-cloud",
                    "usage": "chat",
                    "api_key": "sk-x",
                },
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
        json={
            "default": "local",
            "backends": [{"name": "local", "provider": "ollama", "model": "m", "usage": "chat"}],
        },
    )
    assert client.put("/api/services/models", json={"order": ["ghost"]}).status_code == 400
    assert client.put("/api/services/models", json={"order": ["local", "local"]}).status_code == 400
    # 空列表在 schema 层被拒（ReorderBody min_length=1）→ FastAPI 校验错误 422
    assert client.put("/api/services/models", json={"order": []}).status_code == 422


def test_models_order_is_the_chat_pool_and_can_grow_shrink(
    client: TestClient, unreachable_backend: str
) -> None:
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


# -- 真实能力探测（POST /api/services/probe） ----------------------------------
#
# 用户 2026-11-08 的现场：硅基流动余额耗尽回 402，服务页徽标却写"可用"、对话必挂。
# 根因 = 云端可用性**只看配置齐缺**，而 `/models` 在欠费账号上照样 200 —— 判可用的
# 唯一诚实办法是真发一次能力请求。三条纪律由这组判据钉死：
#   1. 普通 GET 永不外发（配额是钱）；真请求只来自 POST /api/services/probe
#      （前端仅在首载与「重新检测」时调用，普通增删改只 GET）；
#   2. 探的是**能力**端点（embeddings / rerank / chat/completions），不是 /models；
#   3. 结论进台账：GET 读账不发请求；身份含 key 指纹，换了 key 旧结论自动作废；
#      没 key 的行跳过（没有可发的凭据，也没资格被记成"不可用"）。


def _fake_outbound(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, status: int = 402) -> None:
    """出站 POST 钉成固定回包并录下每次 url —— 测试离线，断言用 url 后缀钉**能力形状**。

    默认 402 = 硅基流动余额耗尽的真回包形状（2026-11-08 全程实测）。
    """
    import httpx

    def fake_post(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(str(url))
        return httpx.Response(
            status,
            json={"code": 30001, "message": "balance is insufficient"},
            request=httpx.Request("POST", str(url)),
        )

    monkeypatch.setattr("rolecard_agent.base.outbound.post", fake_post)


@pytest.fixture
def probe_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreachable_backend: str
) -> TestClient:
    """带**可探云端行**的服务页：本地默认（必然不可用）+ 对话云端 + 嵌入/重排云端。

    本地探活夹成 False —— 判据不与宿主机上是否跑着 Ollama 赌运气（与 `client` 同理，
    这里多一层"探测期间本地行不许真发探活"）。
    """
    from rolecard_agent.core.models import services as services_mod

    monkeypatch.setattr(services_mod, "vision_model_ready", lambda *_a, **_k: False)
    monkeypatch.setenv(
        "MODEL_BACKENDS",
        '{"local": {"model": "qwen3-vl:8b", "provider": "ollama",'
        f' "base_url": "{unreachable_backend}"}}}}',
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    c = TestClient(create_app(sqlite_path=tmp_path / "probe.db"))
    res = c.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {
                    "name": "local",
                    "provider": "ollama",
                    "model": "qwen3-vl:8b",
                    "base_url": unreachable_backend,
                },
                {
                    "name": "sfchat",
                    "provider": "siliconflow",
                    "model": "Qwen/Qwen3-8B",
                    "usage": "chat",
                    "api_key": "sk-chat-111111",
                },
                {
                    "name": "sfembed",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-m3",
                    "usage": "embedding",
                    "api_key": "sk-embed-222222",
                },
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert (
        c.post("/api/services/embedding/endpoints", json={"ref_backend": "sfembed"}).status_code
        == 201
    )
    assert (
        c.post("/api/services/rerank/endpoints", json={"ref_backend": "sfembed"}).status_code == 201
    )
    return c


def _candidate(body: dict[str, object], cat: str, rid: str) -> dict[str, object]:
    return next(c for c in _category(body, cat)["candidates"] if c["id"] == rid)  # type: ignore[union-attr]


def test_probe_posts_real_capability_requests_then_get_reads_ledger_offline(
    probe_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探测**真发能力请求**并记账；之后的普通 GET 只读账、一个外部请求都不发。

    三段各自钉一条纪律：前置 GET 零外发（配额是钱）→ POST probe 发的必须是
    `/embeddings`、`/rerank`、`/chat/completions`（`/models` 恒 200 测不出欠费）
    → 记账后的 GET 诚实（402 入账）且照旧零外发。
    """
    calls: list[str] = []
    _fake_outbound(monkeypatch, calls)

    # 0) 没探过：GET 只看配置，且绝不外发
    before = probe_client.get("/api/services").json()
    assert calls == [], "普通 GET（哪怕有 key 的云端行）也不许发外部请求"
    emb = _candidate(before, "embedding", "sfembed")
    assert emb["available"] is True and emb["reason"] == "已配置 API Key"
    chat = _candidate(before, "models", "sfchat")
    assert chat["available"] is True and chat["reason"] == "已配置 key"

    # 1) 首载/「重新检测」= 真发（这是唯一允许外发的入口）
    res = probe_client.post("/api/services/probe")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["probed"] == 3, "嵌入 + 重排 + 对话云端各一次（本地行/停用行不花配额）"
    pairs = {(r["category"], r["id"]) for r in body["results"]}
    assert pairs == {("embedding", "sfembed"), ("rerank", "sfembed"), ("models", "sfchat")}
    assert any(u.endswith("/embeddings") for u in calls), "嵌入必须发真 POST 能力端点"
    assert any(u.endswith("/rerank") for u in calls), "重排同理"
    assert any(u.endswith("/chat/completions") for u in calls), "对话行必须真问一次"

    # 2) 记账之后：GET 读账（诚实 402），照旧零外发
    calls.clear()
    after = probe_client.get("/api/services").json()
    assert calls == [], "记账后的 GET 同样只读账，绝不重复外发"
    emb2 = _candidate(after, "embedding", "sfembed")
    assert emb2["available"] is False
    assert "402" in emb2["reason"] and "额度" in emb2["reason"], emb2["reason"]


def test_probe_marks_chat_row_honest_on_402(
    probe_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对话行走同一本台账：欠费的云端不再拿"已配置 key"冒充可用，生效项随之改判。"""
    calls: list[str] = []
    _fake_outbound(monkeypatch, calls)
    probe_client.post("/api/services/probe")

    models = _category(probe_client.get("/api/services").json(), "models")
    sfchat = next(c for c in models["candidates"] if c["id"] == "sfchat")
    assert sfchat["available"] is False
    assert "402" in sfchat["reason"] and "额度" in sfchat["reason"], sfchat["reason"]
    # local 不可达 + sfchat 欠费 ⇒ 诚实的"无生效项"（而不是继续标云端可用）
    assert models["effective"] is None


def test_changing_the_key_invalidates_the_old_probe_verdict(
    probe_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换了 key = 另一把凭据：旧的 402 结论不许被继承（key 指纹进身份键的全部理由）。"""
    _fake_outbound(monkeypatch, [])
    assert probe_client.post("/api/services/probe").status_code == 200

    res = probe_client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b"},
                {
                    "name": "sfchat",
                    "provider": "siliconflow",
                    "model": "Qwen/Qwen3-8B",
                    "usage": "chat",
                    "api_key": "sk-chat-111111",
                },
                {
                    "name": "sfembed",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-m3",
                    "usage": "embedding",
                    "api_key": "sk-embed-REPLACED-333333",
                },
            ],
        },
    )
    assert res.status_code == 200, res.text
    emb = _candidate(probe_client.get("/api/services").json(), "embedding", "sfembed")
    assert emb["available"] is True and emb["reason"] == "已配置 API Key", (
        "换 key 后必须回到'还没测过'的缺省，不能继承旧 402"
    )

    # 第二段只动 base_url（key 换回原件）：身份键的**端点分量**同样生效 ——
    # 换到另一台端点上，旧端点的 402 结论也不许跟着过去。
    res = probe_client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b"},
                {
                    "name": "sfchat",
                    "provider": "siliconflow",
                    "model": "Qwen/Qwen3-8B",
                    "usage": "chat",
                    "api_key": "sk-chat-111111",
                },
                {
                    "name": "sfembed",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-m3",
                    "usage": "embedding",
                    "base_url": "https://another-endpoint.example/v1",
                    "api_key": "sk-embed-222222",
                },
            ],
        },
    )
    assert res.status_code == 200, res.text
    emb = _candidate(probe_client.get("/api/services").json(), "embedding", "sfembed")
    assert emb["available"] is True and emb["reason"] == "已配置 API Key", (
        "换端点后同样回到'还没测过'：旧端点的结论不继承"
    )

    # 第三段只动 model（key、端点都还原成被探过的原件）：身份键的**模型分量**生效 ——
    # 另一个模型没被问过，旧模型的 402 不许替它答。
    res = probe_client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [
                {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b"},
                {
                    "name": "sfchat",
                    "provider": "siliconflow",
                    "model": "Qwen/Qwen3-8B",
                    "usage": "chat",
                    "api_key": "sk-chat-111111",
                },
                {
                    "name": "sfembed",
                    "provider": "siliconflow",
                    "model": "BAAI/bge-reranker-v2-m3",
                    "usage": "embedding",
                    "base_url": None,
                    "api_key": "sk-embed-222222",
                },
            ],
        },
    )
    assert res.status_code == 200, res.text
    emb = _candidate(probe_client.get("/api/services").json(), "embedding", "sfembed")
    assert emb["available"] is True and emb["reason"] == "已配置 API Key", (
        "换模型后同样回到'还没测过'：旧模型的结论不继承"
    )


def test_probe_skips_rows_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没 key 的行没有可发的凭据：不发请求、不记账，状态保持"去模型页填"。

    绕开 API 种无 key 云端行（`PUT /api/settings/models` 会拒绝没有 key 的 openai
    兼容后端 —— 配置错误要当场大声；历史遗留的无效配置只能直接进库，同
    `test_unavailable_first_choice_degrades_and_says_so` 的手法）。
    """
    from rolecard_agent.storage.db import bootstrap, connect

    db = tmp_path / "keyless.db"
    conn = connect(db)
    bootstrap(conn, enabled_domains=())
    conn.execute(
        "INSERT INTO model_provider (id, provider, base_url, api_key, sort_order) "
        "VALUES ('skw', 'siliconflow', 'https://keyless.example/v1', NULL, 0)"
    )
    conn.execute(
        "INSERT INTO model_backend (name, provider_id, model, sort_order) "
        "VALUES ('sfkeyless', 'skw', 'BAAI/bge-m3', 0)"
    )
    conn.execute(
        "INSERT INTO service_endpoint (category, id, kind, ref_backend, enabled, sort_order, "
        "builtin) VALUES ('embedding', 'sfkeyless', 'cloud', 'sfkeyless', 1, 0, 0)"
    )
    conn.commit()
    conn.close()

    calls: list[str] = []
    _fake_outbound(monkeypatch, calls, status=200)

    c = TestClient(create_app(sqlite_path=db))

    # chat 引用行**引导之后**再种：引导期"对话默认"会拿它建真实 OpenAI 客户端，
    # 无 key 的组当场炸（openai.OpenAIError）。运行期探活/视图读的是活库，照常命中。
    # usage 是派生值：这条引用让 sfkeyless 同时是**对话池**成员（聊天路的无 key 判据）。
    conn = connect(db)
    conn.execute(
        "INSERT INTO service_endpoint "
        "(category, id, kind, ref_backend, enabled, sort_order, builtin, user_id) "
        "VALUES ('chat', 'sfkeyless', 'cloud', 'sfkeyless', 1, 99, 0, 'local-user')"
    )
    conn.commit()
    conn.close()

    body = c.get("/api/services").json()
    assert "sfkeyless" in [row["id"] for row in _category(body, "models")["candidates"]], (
        "前置条件：无 key 行必须在对话池里，否则聊天路的'没 key 不发'判据是空转"
    )

    res = c.post("/api/services/probe")
    assert res.status_code == 200, res.text
    assert res.json()["probed"] == 0, "没有可发凭据的行不算探测"
    assert not any("keyless.example" in u for u in calls), "无 key 行不许发请求"

    emb = _candidate(c.get("/api/services").json(), "embedding", "sfkeyless")
    assert emb["available"] is False
    assert "还没有 API Key" in emb["reason"], "没探过 ≠ 不可用：回到配置缺省判定"


def test_probe_translates_every_failure_into_an_action(
    probe_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """坏消息必须翻译成"下一步做什么"——不肯说人话的探测等于没探。

    四种坏法各钉一条分支：401（换 key）、404（查 base_url）、连不上（不可达，
    不是钱的问题）、200 但返回形状烂（如实说不可用，不许拿 200 冒充能用）。
    """
    import httpx

    for status, must_say in ((401, "Key 无效"), (404, "base_url")):

        def fake(url: str, _status: int = status, **_kwargs: object) -> httpx.Response:
            return httpx.Response(_status, json={}, request=httpx.Request("POST", str(url)))

        monkeypatch.setattr("rolecard_agent.base.outbound.post", fake)
        probe_client.post("/api/services/probe")
        emb = _candidate(probe_client.get("/api/services").json(), "embedding", "sfembed")
        assert emb["available"] is False, f"HTTP {status} 不该算可用"
        assert must_say in emb["reason"], (
            f"HTTP {status} 的处置话术缺了 {must_say!r}: {emb['reason']}"
        )

    # 连都没连上 → 不可达（与"额度不足"是两种病、两条处置）
    def raise_post(url: str, **_kwargs: object) -> httpx.Response:
        raise OSError("网线被我拔了")

    monkeypatch.setattr("rolecard_agent.base.outbound.post", raise_post)
    probe_client.post("/api/services/probe")
    emb = _candidate(probe_client.get("/api/services").json(), "embedding", "sfembed")
    assert emb["available"] is False
    assert "实测不可达" in emb["reason"] and "网线被我拔了" in emb["reason"], emb["reason"]

    # HTTP 200 但返回里没有 choices（对话行）→ 如实"不可用"，200 冒充不了能用
    def shape_post(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"nonsense": 1}, request=httpx.Request("POST", str(url)))

    monkeypatch.setattr("rolecard_agent.base.outbound.post", shape_post)
    probe_client.post("/api/services/probe")
    sfchat = _candidate(probe_client.get("/api/services").json(), "models", "sfchat")
    assert sfchat["available"] is False
    assert "实测不可用" in sfchat["reason"] and "choices" in sfchat["reason"], sfchat["reason"]
