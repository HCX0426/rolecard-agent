"""模型设置端点测试（M5 设置页的后端）。  Traceability: US-8, US-9.

三条决定性断言：
  1. **热切换是真的**：PUT 之后下一次对话用的是新构建的图 —— 用"每次构建返回不同回复"
     的 ScriptedChat 工厂证明（build-1 → build-2），不需要真实后端；
  2. **api_key 永不回读**：GET 只给 has_key；PUT 不带 key = 保留已存 key，空串 = 清除
     —— 否则每次没重输 key 的保存都会把 key 抹掉；
  3. **非法配置 400**：默认后端不在列表里 / 空列表 / 非法名字，都在写库前被拒。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from tests.conftest import ScriptedChat


@pytest.fixture
def counter() -> dict[str, int]:
    return {"n": 0}


@pytest.fixture
def factory(
    counter: dict[str, int],
    _backend_name: str | None = None,
    _temperature: float | None = None,
) -> object:
    """脚本化模型工厂 —— **不接触真实后端**，但必须忠实模拟真实工厂的契约。

    契约里有一条是载荷的：`core.graph.build_model` 对未知后端名抛 `KeyError`
    （`Settings.backend()` 的语义），而 `resolve_role_model` 正是**靠这个 KeyError**
    才实现"角色引用了被删除的后端 → 降级默认 + 留痕"。早先这个 fake 对任何名字都
    返回模型，于是"降级"测试从来没真正走到降级分支 —— 一个通过了的假阳性
    （代码审查报告（第二轮）新增断言时暴露）。
    """
    def _factory(
        settings: object,
        backend_name: str | None = None,
        temperature: float | None = None,
    ) -> ScriptedChat:
        known = getattr(settings, "model_backends", {}) or {}
        if backend_name is not None and backend_name not in known:
            raise KeyError(f"unknown model backend {backend_name!r}")
        counter["n"] += 1
        label = f"build-{counter['n']}"
        if backend_name:
            label += f"@{backend_name}"
        # 温度也打进标记：P1-2 的断言点是「角色卡的 temperature 真的传到了工厂」，
        # 而不是只看工厂被调用了几次。
        if temperature is not None:
            label += f"@t{temperature}"
        return ScriptedChat([AIMessage(content=label)])

    return _factory


@pytest.fixture
def client(tmp_path: Path, factory: object) -> Iterator[TestClient]:
    app = create_app(sqlite_path=tmp_path / "app.db", model_factory=factory)  # type: ignore[arg-type]
    with TestClient(app) as c:
        yield c


def parse_sse(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def authoritative_text(client: TestClient, thread_id: str, message: str) -> str:
    res = client.post("/api/chat", json={"thread_id": thread_id, "message": message})
    assert res.status_code == 200
    replace = [e for e in parse_sse(res.text) if e["type"] == "message_replace"]
    return str(replace[-1]["text"])


def test_fresh_startup_seeds_env_backends(client: TestClient) -> None:
    """首次启动把 env 后端迁移进表（模型页可见、可编辑）；迁移一次性。"""
    body = client.get("/api/settings/models").json()
    assert body["default"] == "local"  # env 默认随迁移一并接管
    assert [b["name"] for b in body["backends"]] == ["local"]
    # 无 key 供应商（Ollama）不存占位 key：启动归一化会清掉 env 里 "ollama" 这种占位值
    assert body["backends"][0]["has_key"] is False


def test_admin_audits_never_contain_api_key_material(client: TestClient) -> None:
    """H3 回归：模型设置与服务引用的管理面审计**绝不落 key 明文**（审计页浏览器可见）。"""
    secret = "sk-super-secret-value-9876"
    put = client.put(
        "/api/settings/models",
        json={
            "default": "siliconflow",
            "backends": [
                {
                    "name": "siliconflow",
                    "provider": "siliconflow",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V4-Flash",
                    "api_key": secret,
                },
                {"name": "local", "provider": "ollama", "model": "qwen2.5:7b"},
            ],
            "fallbacks": [],
        },
    )
    assert put.status_code == 200
    # 服务页引用刚配置的后端（引用模型：新增=选择，不复制配置）
    add = client.post("/api/services/embedding/endpoints", json={"ref_backend": "siliconflow"})
    assert add.status_code == 201, add.text
    assert "api_key" not in add.json()  # 引用行也永不回明文（凭据在模型页）

    audits = client.get("/api/audit").json()
    blob = json.dumps(audits, ensure_ascii=False)
    assert secret not in blob, "密钥明文泄漏进审计"
    actions = {a["action"] for a in audits}
    assert "update_model_settings" in actions  # 模型设置变更必须留痕（此前完全无审计）
    assert "add_service_endpoint" in actions
    settings_audit = next(a for a in audits if a["action"] == "update_model_settings")
    raw_detail = settings_audit["detail_json"]
    detail = json.loads(raw_detail) if isinstance(raw_detail, str) else raw_detail
    assert detail["backends"][0]["key_changed"] is True  # 只记"是否提供了 key"，不记值


def test_seed_is_one_way_env_never_comes_back(tmp_path: Path) -> None:
    """迁移一次性：首启把 env 的 local 迁入；操作员在 UI 换成 siliconflow 后重启，
    local 不会被 env 重新塞回来 —— 界面是唯一事实来源。"""
    app = create_app(sqlite_path=tmp_path / "seed.db")
    with TestClient(app) as c:
        assert [b["name"] for b in c.get("/api/settings/models").json()["backends"]] == ["local"]
        # 操作员在 UI 里换成 siliconflow（删除了 local）
        assert (
            c.put(
                "/api/settings/models",
                json={
                    "default": "siliconflow",
                    "backends": [
                        {
                            "name": "siliconflow",
                            "provider": "openai",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "model": "deepseek-ai/DeepSeek-V4-Flash",
                            "api_key": "sk-stored",
                        }
                    ],
                },
            ).status_code
            == 200
        )

    # 同库"重启"：迁移标记已落库，env 不再参与
    app2 = create_app(sqlite_path=tmp_path / "seed.db")
    with TestClient(app2) as c2:
        body = c2.get("/api/settings/models").json()
        assert {b["name"] for b in body["backends"]} == {"siliconflow"}
        assert body["default"] == "siliconflow"


def test_put_then_get_round_trip_without_key_exposure(client: TestClient) -> None:
    res = client.put(
        "/api/settings/models",
        json={
            "default": "siliconflow",
            "backends": [
                {
                    "name": "siliconflow",
                    "provider": "openai",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V4-Flash",
                    "api_key": "sk-demo-not-a-real-key",
                },
                {"name": "local", "provider": "ollama", "model": "qwen2.5:7b"},
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["default"] == "siliconflow"
    by_name = {b["name"]: b for b in body["backends"]}
    assert by_name["siliconflow"]["has_key"] is True
    assert "api_key" not in by_name["siliconflow"]  # 只写不回读
    # local 的占位 key（"ollama"）经启动归一化清除 —— 无 key 供应商永不存 key
    assert by_name["local"]["has_key"] is False

    fetched = client.get("/api/settings/models").json()
    assert fetched["default"] == "siliconflow"
    assert {b["name"] for b in fetched["backends"]} == {"siliconflow", "local"}


def test_omitted_key_is_preserved_not_erased(client: TestClient) -> None:
    """PUT 不带 api_key = 保留已存 key（GET 不回读，所以这是唯一的'不丢 key'方式）。"""
    payload = {
        "default": "cloud-a",
        "backends": [
            {"name": "cloud-a", "provider": "openai", "model": "m", "api_key": "sk-keep-me"}
        ],
    }
    assert client.put("/api/settings/models", json=payload).status_code == 200

    payload_no_key = {
        "default": "cloud-a",
        "backends": [{"name": "cloud-a", "provider": "openai", "model": "m"}],
    }
    assert client.put("/api/settings/models", json=payload_no_key).status_code == 200
    body = client.get("/api/settings/models").json()
    assert body["backends"][0]["has_key"] is True  # 没被抹掉

    payload_clear = {
        "default": "cloud-a",
        "backends": [{"name": "cloud-a", "provider": "openai", "model": "m", "api_key": ""}],
    }
    assert client.put("/api/settings/models", json=payload_clear).status_code == 200
    assert client.get("/api/settings/models").json()["backends"][0]["has_key"] is False


def test_save_hot_rebuilds_the_graph(client: TestClient) -> None:
    """US-9 核心：保存后**下一轮对话**就用新图 —— factory 每次构建返回 build-N 的模型。"""
    session = client.post("/api/session", json={}).json()
    tid = str(session["thread_id"])
    # 角色自带默认温度（0.7），所以第一轮就是"带温度的新实例"（build-2，启动默认
    # build-1 只有在温度缺失时才会被用到）。这个用例钉的契约是：**保存后下一轮换成
    # 新构建的实例** —— 所以比较序号，而不是硬编码标签。
    first = authoritative_text(client, tid, "一问")

    res = client.put(
        "/api/settings/models",
        json={
            "default": "cloud-a",
            "backends": [
                {"name": "cloud-a", "provider": "openai", "model": "m", "api_key": "sk-test"},
            ],
        },
    )
    assert res.status_code == 200
    second = authoritative_text(client, tid, "二问")  # 新图生效，无需重启
    n_first = int(first.split("@")[0].replace("build-", ""))
    n_second = int(second.split("@")[0].replace("build-", ""))
    assert n_second > n_first


def test_role_model_name_routes_to_declared_backend(client: TestClient) -> None:
    """US-8 后半：角色声明 model_name → 该角色的对话走声明的后端（全栈验证）。"""
    # 注册两个后端；factory 给每次构建打上 backend_name 标记
    assert (
        client.put(
            "/api/settings/models",
            json={
                "default": "cloud-a",
                "backends": [
                    {"name": "cloud-a", "provider": "openai", "model": "m-a", "api_key": "sk-test"},
                    {"name": "cloud-b", "provider": "openai", "model": "m-b", "api_key": "sk-test"},
                ],
            },
        ).status_code
        == 200
    )
    client.post(
        "/api/roles",
        json={
            "role_id": "b2user",
            "role_name": "B2 角色",
            "system_prompt": "x",
            "model_name": "cloud-b",
        },
    )
    session = client.post("/api/session", json={"role_id": "b2user"}).json()
    text = authoritative_text(client, str(session["thread_id"]), "你好")
    assert "@cloud-b" in text  # 该轮确实用了角色声明的后端，而非默认


def test_role_temperature_reaches_the_model(client: TestClient) -> None:
    """P1-2 回归：角色卡的 temperature 必须真的进到模型构造（此前是死配置）。

    顺带钉住缓存键语义：温度参与 (后端, 温度) 缓存 —— 两个不同温度的角色
    各自拿到自己的实例，而不是后拿到温度的那份污染先到的。
    """
    client.post(
        "/api/roles",
        json={
            "role_id": "cool",
            "role_name": "低温角色",
            "system_prompt": "x",
            "temperature": 0.3,
        },
    )
    client.post(
        "/api/roles",
        json={
            "role_id": "warm",
            "role_name": "高温角色",
            "system_prompt": "x",
            "temperature": 0.9,
        },
    )
    s1 = client.post("/api/session", json={"role_id": "cool"}).json()
    s2 = client.post("/api/session", json={"role_id": "warm"}).json()
    assert "@t0.3" in authoritative_text(client, str(s1["thread_id"]), "你好")
    assert "@t0.9" in authoritative_text(client, str(s2["thread_id"]), "你好")

    # 未声明后端名的会话仍走默认
    other = client.post("/api/session", json={}).json()
    other_text = authoritative_text(client, str(other["thread_id"]), "你好")
    assert "@cloud-b" not in other_text


def test_fallbacks_round_trip_and_validation(client: TestClient) -> None:
    """US-8 / §8.5：失败回退链在设置页配置，保存进同一事务且被校验。"""
    res = client.put(
        "/api/settings/models",
        json={
            "default": "cloud-a",
            "backends": [
                {"name": "cloud-a", "provider": "openai", "model": "m-a", "api_key": "sk-test"},
                {"name": "cloud-b", "provider": "openai", "model": "m-b", "api_key": "sk-test"},
            ],
            "fallbacks": ["cloud-b"],
        },
    )
    assert res.status_code == 200
    assert res.json()["fallbacks"] == ["cloud-b"]
    assert client.get("/api/settings/models").json()["fallbacks"] == ["cloud-b"]

    # 未知回退后端 → 400
    res = client.put(
        "/api/settings/models",
        json={
            "default": "cloud-a",
            "backends": [{"name": "cloud-a", "provider": "openai", "model": "m-a"}],
            "fallbacks": ["ghost"],
        },
    )
    assert res.status_code == 400

    # 超过两级 → 400
    res = client.put(
        "/api/settings/models",
        json={
            "default": "a",
            "backends": [
                {"name": "a", "provider": "openai", "model": "m"},
                {"name": "b", "provider": "openai", "model": "m"},
                {"name": "c", "provider": "openai", "model": "m"},
                {"name": "d", "provider": "openai", "model": "m"},
            ],
            "fallbacks": ["b", "c", "d"],
        },
    )
    assert res.status_code == 400


def test_role_model_name_must_be_a_configured_backend(client: TestClient) -> None:
    """**写时就大声**：角色声明一个不存在的后端名 → 400，而不是留到运行期降级。

    与会话级覆盖（`PATCH /api/session`）同一纪律。修复前角色侧不校验，拼错名字只能靠
    "回答质量看起来不对"来暴露（代码审查报告（第二轮）L3）。
    """
    res = client.post(
        "/api/roles",
        json={
            "role_id": "typo",
            "role_name": "拼错",
            "system_prompt": "x",
            "model_name": "cloud-a",  # 尚未配置
        },
    )
    assert res.status_code == 400
    assert "cloud-a" in res.json()["detail"]
    # 也覆盖 PATCH 路径
    client.post("/api/roles", json={"role_id": "ok", "role_name": "ok", "system_prompt": "x"})
    assert (
        client.patch("/api/roles/ok", json={"model_name": "nope"}).status_code == 400
    )


def test_unknown_role_backend_falls_back_to_default(client: TestClient) -> None:
    """角色引用了**被删除**的后端 → 降级到默认模型并正常回答，而不是 500。

    注：写时校验挡住的是"故意声明一个不存在的后端"；**后端被事后删掉**是另一条路径，
    必须仍然降级 —— 可用性 fail-soft、权限 fail-closed 的分工就体现在这里。
    """
    # 先让 cloud-a 存在，角色才可能（合法地）引用它
    client.put(
        "/api/settings/models",
        json={
            "default": "cloud-a",
            "backends": [
                {"name": "cloud-a", "provider": "openai", "model": "m-a", "api_key": "sk-a"}
            ],
        },
    )
    assert (
        client.post(
            "/api/roles",
            json={
                "role_id": "ghost_backend",
                "role_name": "幽灵",
                "system_prompt": "x",
                "model_name": "cloud-a",
            },
        ).status_code
        == 201
    )
    session = client.post("/api/session", json={"role_id": "ghost_backend"}).json()
    tid = str(session["thread_id"])
    # 第一轮：角色路由生效，用的是 cloud-a 上构建的模型
    assert "@cloud-a" in authoritative_text(client, tid, "一问")  # 角色默认温度也会跟着进来

    # 删掉 cloud-a：角色仍引用它 → 下一轮降级默认，对话不崩
    client.put(
        "/api/settings/models",
        json={
            "default": "cloud-b",
            "backends": [
                {"name": "cloud-b", "provider": "openai", "model": "m-b", "api_key": "sk-test"}
            ],
        },
    )
    text = authoritative_text(client, tid, "二问")
    assert text.startswith("build-")  # 仍是工厂构建的模型（默认），而非异常
    assert not text.endswith("@cloud-a")  # 已不再使用被删除的后端


def test_openai_backend_without_key_is_rejected_at_save(client: TestClient) -> None:
    """没凭据的 openai 后端必须在**保存时**拒绝 —— 否则会存进一个"热重建时才炸"
    的配置（实测 Missing credentials），配置与运行图还会不一致。

    注意：已存 key 的后端再次提交时不带 key = "保留"，那是合法的（下面一并验证）。
    """
    res = client.put(
        "/api/settings/models",
        json={
            "default": "cloud-a",
            "backends": [{"name": "cloud-a", "provider": "openai", "model": "m"}],
        },
    )
    assert res.status_code == 400 and "api_key" in res.json()["detail"]

    ok = client.put(
        "/api/settings/models",
        json={
            "default": "cloud-a",
            "backends": [
                {"name": "cloud-a", "provider": "openai", "model": "m", "api_key": "sk-test"}
            ],
        },
    )
    assert ok.status_code == 200 and ok.json()["backends"][0]["has_key"] is True

    # 已存 key 的后端再次提交不带 key = 保留原 key
    keep = client.put(
        "/api/settings/models",
        json={
            "default": "cloud-a",
            "backends": [{"name": "cloud-a", "provider": "openai", "model": "m"}],
        },
    )
    assert keep.status_code == 200 and keep.json()["backends"][0]["has_key"] is True

    # 本地 Ollama 不需要凭据
    local_ok = client.put(
        "/api/settings/models",
        json={
            "default": "local",
            "backends": [{"name": "local", "provider": "ollama", "model": "qwen2.5:7b"}],
        },
    )
    assert local_ok.status_code == 200


def test_invalid_default_backend_400(client: TestClient) -> None:
    res = client.put(
        "/api/settings/models",
        json={
            "default": "ghost",
            "backends": [{"name": "cloud-a", "provider": "openai", "model": "m"}],
        },
    )
    assert res.status_code == 400
    # 未写入：只剩启动时播种的 local，ghost 不存在
    body = client.get("/api/settings/models").json()
    assert {b["name"] for b in body["backends"]} == {"local"}
    assert body["default"] == "local"  # 播种时采纳的 env 默认


def test_empty_backend_list_400(client: TestClient) -> None:
    res = client.put("/api/settings/models", json={"default": "x", "backends": []})
    assert res.status_code == 400


def test_invalid_backend_name_400(client: TestClient) -> None:
    res = client.put(
        "/api/settings/models",
        json={
            "default": "Bad Name",
            "backends": [{"name": "Bad Name", "provider": "openai", "model": "m"}],
        },
    )
    assert res.status_code == 400


def test_duplicate_backend_name_400(client: TestClient) -> None:
    res = client.put(
        "/api/settings/models",
        json={
            "default": "a",
            "backends": [
                {"name": "a", "provider": "openai", "model": "m"},
                {"name": "a", "provider": "openai", "model": "m2"},
            ],
        },
    )
    assert res.status_code == 400
