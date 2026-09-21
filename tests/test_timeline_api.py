"""事件簿端点的用例（`GET /api/roles/{role_id}/timeline`）。

轴本身（合并、顺序、游标）在 `tests/unit/test_timeline.py` 里钉；这里只钉**接口边界**：
未知角色 404、非法 `kinds` 400（不是静默忽略）、纯读**不写审计**（这条是设计稿 §6.1 的
承诺：谁翻了时间线不该进操作流水，而轴上全是用户自己的对话与事实）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from tests.conftest import ScriptedChat


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    model = ScriptedChat([AIMessage(content="好")])
    app = create_app(sqlite_path=tmp_path / "app.db", model=model)
    with TestClient(app) as c:
        yield c


def seed(client: TestClient, role_id: str) -> str:
    """给这个角色留下一条主动消息 + 一条记忆，返回它的主动会话 id。"""
    thread = client.post("/api/session", json={"role_id": role_id}).json()["thread_id"]
    client.post(
        "/api/settings/memory/item", params={"role_id": role_id}, json={"text": "用户住在苏州"}
    )
    return str(thread)


def test_timeline_returns_events_for_the_role(client: TestClient) -> None:
    seed(client, "general_assistant")
    res = client.get("/api/roles/general_assistant/timeline")
    assert res.status_code == 200
    body: dict[str, Any] = res.json()
    assert body["role_id"] == "general_assistant"
    # 同一条会话的锚点也在轴上（它就是 `seed` 里刚开的那个会话）；种类并列时记忆排在会话前
    assert [(i["kind"], i["text"]) for i in body["items"]] == [
        ("memory", "用户住在苏州"),
        ("thread", "新对话"),
    ]
    assert body["next_cursor"] is None and body["truncated"] is False


def test_timeline_unknown_role_is_404(client: TestClient) -> None:
    assert client.get("/api/roles/ghost_role/timeline").status_code == 404


def test_timeline_rejects_unknown_kinds_with_the_allowed_list(client: TestClient) -> None:
    """非法种类要当场报错而不是静默忽略：静默忽略的表现是"筛选没生效"，没人能看出为什么。"""
    res = client.get("/api/roles/general_assistant/timeline", params={"kinds": "chat,secret"})
    assert res.status_code == 400
    assert "chat" in res.json()["detail"] and "reachout" in res.json()["detail"]


def test_timeline_is_a_pure_read_no_audit_and_user_tier(client: TestClient) -> None:
    seed(client, "general_assistant")
    before = len(client.get("/api/audit?limit=100").json())
    assert client.get("/api/roles/general_assistant/timeline").status_code == 200
    assert len(client.get("/api/audit?limit=100").json()) == before  # 翻轴不写审计
    actions = [r["action"] for r in client.get("/api/audit?limit=100").json()]
    assert "timeline" not in " ".join(actions)


def test_timeline_limit_and_cursor_page_through(client: TestClient) -> None:
    for text in ("用户住在苏州", "用户每周三练琴", "用户养了一只叫米的猫"):
        client.post(
            "/api/settings/memory/item",
            params={"role_id": "general_assistant"},
            json={"text": text},
        )
    first = client.get(
        "/api/roles/general_assistant/timeline",
        params={"limit": 2, "kinds": "memory"},
    ).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get(
        "/api/roles/general_assistant/timeline",
        params={"limit": 2, "kinds": "memory", "before": first["next_cursor"]},
    ).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    seen = [i["text"] for page in (first, second) for i in page["items"]]
    assert len(set(seen)) == 3  # 翻页不重不漏
