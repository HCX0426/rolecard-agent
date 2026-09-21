"""模型两层 API：添加抽屉要用的四个端点（目录 / 测连 / 加一行 / 删一行 / 写回能力）。

Traceability: US-8、US-9（模型配置）+ docs/模型页设计稿.md §2/§3。

四条决定性断言：

  1. **凭据不在网络上往返**：只发 `provider_id` 就能拉到列表 / 完成测连（key 由服务端从
     组里取）；请求体里出现的 `api_key` 只用于"正在填一把新 key"这一种场景。
  2. **测连门禁不花配额就不花**：默认 `calls_used=0`；图片外发只有 `test_vision=true` 才发生。
  3. **加/删一行不碰别的行**：整表 `PUT` 那种"顺带覆写回退链"的老习惯不许回来（P0-2 的教训）。
  4. **删除的连带后果可见**：组里最后一行被删 → 凭据随组消失；删掉的是对话默认 → 序列里
     下一个顶上，绝不留下悬空默认。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self) -> object:
        return self._payload


class _FakeHttp:
    def __init__(self) -> None:
        self.replies: dict[str, Any] = {}
        self.gets: list[tuple[str, dict[str, str]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def _reply(self, url: str) -> _Response:
        for key, value in self.replies.items():
            if key in url:
                return value if isinstance(value, _Response) else _Response(value)
        return _Response({"error": "unexpected"}, status_code=404)

    def get(self, url: str, headers: dict[str, str] | None = None, timeout: float = 0) -> _Response:
        self.gets.append((url, headers or {}))
        return self._reply(url)

    def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 0,
    ) -> _Response:
        self.posts.append((url, json or {}))
        return self._reply(url)


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeHttp]:
    fake = _FakeHttp()
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "post", fake.post)
    yield fake


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """一个本地后端（默认端口指向必然拒绝的地址）+ 一个云端供应商组（带一把 key）。"""
    monkeypatch.setenv(
        "MODEL_BACKENDS",
        '{"local": {"model": "qwen3-vl:8b", "provider": "ollama",'
        ' "base_url": "http://127.0.0.1:9"}}',
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(sqlite_path=tmp_path / "layers.db")
    with TestClient(app) as c:
        c.put(
            "/api/settings/models",
            json={
                "default": "local",
                "backends": [
                    {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b",
                     "base_url": "http://127.0.0.1:9", "usage": "chat"},
                    {"name": "sf", "provider": "siliconflow",
                     "base_url": "https://api.siliconflow.cn/v1",
                     "model": "deepseek-ai/DeepSeek-V4-Flash", "api_key": "sk-group-secret"},
                ],
            },
        )
        yield c


def _group(client: TestClient, group_id: str) -> dict[str, Any]:
    providers = client.get("/api/settings/models").json()["providers"]
    return next(g for g in providers if g["id"] == group_id)


# --------------------------------------------------------------------------- catalog


def test_catalog_with_only_provider_id_keeps_the_key_off_the_wire(
    client: TestClient, fake_http: _FakeHttp
) -> None:
    fake_http.replies = {"/models": {"data": [{"id": "a"}, {"id": "b"}]}}
    res = client.post("/api/settings/models/catalog", json={"provider_id": "siliconflow"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["models"] == ["a", "b"] and body["reachable"] is True
    # 发出去的请求带了组里那把 key（服务端注入），而调用方一个字节都没上传 key
    assert fake_http.gets[0][1]["Authorization"] == "Bearer sk-group-secret"


def test_catalog_failure_is_a_200_with_a_reason_not_an_error(
    client: TestClient, fake_http: _FakeHttp
) -> None:
    """拉不到列表 ≠ 请求失败：中转站经常给不全，界面要能退回"手填模型名"。"""
    fake_http.replies = {"/models": _Response({"detail": "no"}, status_code=404)}
    body = client.post(
        "/api/settings/models/catalog",
        json={"provider": "openai", "base_url": "https://gateway.test/v1", "api_key": "sk-x"},
    ).json()
    assert body["models"] == [] and body["reachable"] is False
    assert "404" in body["detail"]


def test_catalog_rejects_non_http_base_url(client: TestClient) -> None:
    """M8 那条口径同样守住探测口：不然这就是一个让服务器替内网发请求的入口。"""
    assert client.post(
        "/api/settings/models/catalog", json={"provider": "openai", "base_url": "file:///x"}
    ).status_code == 400
    assert client.post(
        "/api/settings/models/catalog", json={"provider_id": "ghost"}
    ).status_code == 400


# --------------------------------------------------------------------------- probe


def test_probe_default_costs_no_quota_and_sends_no_image(
    client: TestClient, fake_http: _FakeHttp
) -> None:
    fake_http.replies = {"/models": {"data": [{"id": "deepseek-ai/DeepSeek-V4-Flash"}]}}
    res = client.post(
        "/api/settings/models/probe",
        json={"provider_id": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash"},
    )
    body = res.json()
    assert body["reachable"] is True and body["model_listed"] is True
    assert body["calls_used"] == 0 and body["tools"] is None and body["vision"] is None
    assert fake_http.posts == []  # 一次推理请求都没发
    audit = next(
        a for a in client.get("/api/audit?limit=50").json() if a["action"] == "probe_model"
    )
    detail = json.loads(audit["detail_json"])
    assert detail["test_vision"] is False and detail["calls_used"] == 0
    assert "sk-group-secret" not in json.dumps(audit, ensure_ascii=False)


def test_probe_with_vision_flag_uploads_the_miniature(
    client: TestClient, fake_http: _FakeHttp
) -> None:
    fake_http.replies = {
        "/models": {"data": [{"id": "m"}]},
        "/chat/completions": {"choices": [{"message": {"content": "red"}}]},
    }
    body = client.post(
        "/api/settings/models/probe",
        json={"provider_id": "siliconflow", "model": "m", "test_tools": True,
              "test_vision": True},
    ).json()
    assert body["vision"] is True and body["vision_source"] == "uploaded-image"
    assert body["tools"] is None  # 假回复里有内容、没 tool_calls → "不知道"，不是"不支持"
    assert body["calls_used"] == 2
    sent = json.dumps(fake_http.posts, ensure_ascii=False)
    assert "data:image/png;base64," in sent  # 图片确实外发了（所以才要显式确认）


# --------------------------------------------------------------------------- 加一行


def test_add_model_reuses_the_group_credential_and_its_own_name(
    client: TestClient, fake_http: _FakeHttp
) -> None:
    fake_http.replies = {"/models": {"data": [{"id": "Qwen/Qwen3-VL-30B-A3B-Instruct"}]}}
    res = client.post(
        "/api/settings/models",
        json={"provider_id": "siliconflow", "model": "Qwen/Qwen3-VL-30B-A3B-Instruct",
              "supports_vision": True, "supports_tools": False},
    )
    assert res.status_code == 201, res.text
    added = res.json()["added"]["name"]
    # 名字由 (供应商, 模型名) 生成（少填一格，同时仍然说得出它是谁），且是合法配置名。
    assert added.startswith("siliconflow-") and len(added) <= 32
    group = _group(client, "siliconflow")
    assert [m["name"] for m in group["models"]] == ["sf", added]
    assert group["key_masked"] == "sk-…cret"  # 一把 key，两个模型 —— 拆层要解决的就是这个
    new = next(m for m in group["models"] if m["name"] == added)
    assert new["supports_vision"] is True and new["supports_tools"] is False
    # 加完就能在对话页选到：默认没被抢走，新行进了序列尾部
    models = client.get("/api/services").json()["services"]
    order = next(s for s in models if s["key"] == "models")
    assert order["effective"] == "local"
    assert added in [c["id"] for c in order["candidates"]]


def test_add_model_creates_a_group_when_the_provider_is_new(
    client: TestClient, fake_http: _FakeHttp
) -> None:
    fake_http.replies = {"/models": {"data": [{"id": "glm-4"}]}}
    res = client.post(
        "/api/settings/models",
        json={"provider": "openai", "base_url": "https://mirror.test/v1",
              "api_key": "sk-mirror", "model": "glm-4"},
    )
    assert res.status_code == 201, res.text
    group = _group(client, "openai")
    assert group["base_url"] == "https://mirror.test/v1" and group["has_key"] is True
    assert [m["model"] for m in group["models"]] == ["glm-4"]


def test_first_model_becomes_the_chat_default_automatically(client: TestClient) -> None:
    """一套配置都没有时，加完第一行必须能直接对话（否则"加完仍然不能聊"最难查）。"""
    client.delete("/api/settings/models/sf")
    client.delete("/api/settings/models/local")
    body = client.get("/api/settings/models").json()
    assert body["default"] is None and body["backends"] == []
    added = client.post(
        "/api/settings/models",
        json={"provider": "ollama", "base_url": "http://127.0.0.1:9",
              "model": "qwen2.5:7b", "name": "first"},
    )
    assert added.status_code == 201, added.text
    after = client.get("/api/settings/models").json()
    assert after["default"] == "first" and after["fallbacks"] == []


def test_add_model_rejects_duplicate_name(client: TestClient) -> None:
    res = client.post(
        "/api/settings/models", json={"provider_id": "siliconflow", "model": "m", "name": "sf"}
    )
    assert res.status_code == 400 and "已经存在" in res.json()["detail"]


# --------------------------------------------------------------------------- 删一行


def test_generated_names_stay_unique_for_the_same_model_family(client: TestClient) -> None:
    """同一个模型名在两个供应商下、或同族两个模型，生成的配置名不许撞车。"""
    first = client.post(
        "/api/settings/models", json={"provider_id": "siliconflow", "model": "Qwen/Qwen3-VL"}
    ).json()["added"]["name"]
    second = client.post(
        "/api/settings/models", json={"provider_id": "siliconflow", "model": "Qwen/Qwen3-VL"}
    ).json()["added"]["name"]
    assert first != second and client.get("/api/settings/models").status_code == 200


def test_deleting_the_last_model_of_a_group_takes_the_credential(client: TestClient) -> None:
    client.post("/api/settings/models", json={"provider_id": "siliconflow", "model": "m-b"})
    client.delete("/api/settings/models/sf")
    group = _group(client, "siliconflow")
    assert [m["name"] for m in group["models"]] == ["siliconflow-m-b"]
    assert group["key_masked"] == "sk-…cret"  # 还剩一行 → 凭据仍在组上
    client.delete("/api/settings/models/siliconflow-m-b")
    ids = [g["id"] for g in client.get("/api/settings/models").json()["providers"]]
    # 组里没模型了就连凭据一起删：界面上已经没有它，留在盘上就是一处看不见的 key
    assert "siliconflow" not in ids


def test_deleting_the_default_promotes_the_next_in_line(client: TestClient) -> None:
    client.put(
        "/api/services/models", json={"order": ["sf", "local"]}
    )
    assert client.get("/api/settings/models").json()["default"] == "sf"
    assert client.delete("/api/settings/models/sf").status_code == 204
    body = client.get("/api/settings/models").json()
    assert body["default"] == "local"  # 默认绝不悬空
    assert body["backends"] and all(b["name"] != "sf" for b in body["backends"])


def test_delete_unknown_model_is_404(client: TestClient) -> None:
    assert client.delete("/api/settings/models/ghost").status_code == 404


# --------------------------------------------------------------------------- 能力写回


def test_capabilities_patch_writes_the_tri_state(client: TestClient) -> None:
    assert client.patch(
        "/api/settings/models/sf/capabilities",
        json={"supports_vision": True, "supports_tools": None},
    ).status_code == 200
    model = _group(client, "siliconflow")["models"][0]
    assert model["supports_vision"] is True
    assert model["supports_tools"] is None  # null = 没测过，界面渲染 `?`，不是"不支持"
    # 运行时那侧仍要有确定值（未探测 = 放行）
    row = next(b for b in client.get("/api/settings/models").json()["backends"]
               if b["name"] == "sf")
    assert row["supports_tools"] is True
    # 缺席的键不动：只写视觉，工具仍是"没测过"
    client.patch("/api/settings/models/sf/capabilities", json={"supports_vision": False})
    model = _group(client, "siliconflow")["models"][0]
    assert model["supports_vision"] is False and model["supports_tools"] is None


def test_capabilities_patch_rejects_an_empty_body_and_unknown_field(client: TestClient) -> None:
    assert client.patch("/api/settings/models/sf/capabilities", json={}).status_code == 400


def test_capabilities_patch_unknown_model_is_404(client: TestClient) -> None:
    assert client.patch(
        "/api/settings/models/ghost/capabilities", json={"supports_vision": True}
    ).status_code == 404
