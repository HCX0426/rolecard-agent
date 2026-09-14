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
def factory(counter: dict[str, int]) -> object:
    def _factory(_settings: object) -> ScriptedChat:
        counter["n"] += 1
        return ScriptedChat([AIMessage(content=f"build-{counter['n']}")])

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


def test_empty_settings_returns_env_default(client: TestClient) -> None:
    """从未保存过设置 = DB 空表，默认回落 env（model_default='local'）。"""
    body = client.get("/api/settings/models").json()
    assert body["default"] is None  # 未挑选过
    assert body["backends"] == []


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
    assert authoritative_text(client, tid, "一问") == "build-1"

    res = client.put(
        "/api/settings/models",
        json={
            "default": "cloud-a",
            "backends": [{"name": "cloud-a", "provider": "openai", "model": "m"}],
        },
    )
    assert res.status_code == 200
    assert authoritative_text(client, tid, "二问") == "build-2"  # 新图生效，无需重启


def test_invalid_default_backend_400(client: TestClient) -> None:
    res = client.put(
        "/api/settings/models",
        json={
            "default": "ghost",
            "backends": [{"name": "cloud-a", "provider": "openai", "model": "m"}],
        },
    )
    assert res.status_code == 400
    assert client.get("/api/settings/models").json()["backends"] == []  # 未写入


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
