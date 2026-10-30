"""角色卡导出（R1031-13，设计稿 §3「隐私出口」的实现验收）。

四波审计与产品代理同日点名的 P1 欠账：设计稿把"导出/分享角色卡时真删记忆行
（只带 pinned+active 且需显式勾选）"当既定规矩引用，实现却不存在 —— 卡只能进不能出。
这一文件钉四件事：V2 形状成立、记忆默认不出门、显式勾选只放 pinned+active、
越权与坏参数大声 4xx。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.storage.db import connect


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(sqlite_path=tmp_path / "app.db")
    with TestClient(app) as c:
        yield c


def _seed_memory(db_path: Path, role_id: str, *, text: str, pinned: bool) -> None:
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO role_memory_item (role_id, user_id, uid, text, source, importance, category,"
        " pinned, invalidated_at)"
        " VALUES (?, ?, ?, ?, 'manual', 2, 'longterm', ?, ?)",
        (role_id, DEFAULT_USER_ID, uuid.uuid4().hex, text, int(pinned), None),
    )
    conn.commit()
    conn.close()


def _seed_memory_invalidated(db_path: Path, role_id: str, *, text: str) -> None:
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO role_memory_item (role_id, user_id, uid, text, source, importance, category,"
        " pinned, invalidated_at)"
        " VALUES (?, ?, ?, ?, 'manual', 2, 'longterm', 1, CURRENT_TIMESTAMP)",
        (role_id, DEFAULT_USER_ID, uuid.uuid4().hex, text),
    )
    conn.commit()
    conn.close()


def test_export_roundtrips_v2_shape(client: TestClient) -> None:
    """导出是 ST V2 JSON：spec/data 嵌套、name/description/mes_example 都在。"""
    client.post(
        "/api/roles",
        json={
            "role_id": "exporter",
            "role_name": "出口测试员",
            "system_prompt": "你是出口测试员。",
            "description": "一份用于导出的卡",
            "exemplars": [{"user": "你好", "assistant": "嗨，我在。"}],
        },
    )
    res = client.get("/api/roles/exporter/export")
    assert res.status_code == 200, res.text[:200]
    assert res.headers["content-type"].startswith("application/json")
    assert "attachment" in res.headers.get("content-disposition", "")
    payload = json.loads(res.text)
    assert payload["spec"] == "chara_card_v2"
    data = payload["data"]
    assert data["name"] == "出口测试员"
    assert data["description"] == "你是出口测试员。"
    assert data["creator_notes"] == "一份用于导出的卡"
    # 范例反向拼回 ST 的宏形状（{{char}}/{{user}}），可被自家导入器再吃回去
    assert "{{user}}: 你好" in data["mes_example"]
    assert "{{char}}: 嗨，我在。" in data["mes_example"]


def test_export_memory_stays_home_by_default(client: TestClient, tmp_path: Path) -> None:
    """默认导出**不带任何记忆行** —— 设计稿 §3：导出是卡的出口，不是记忆的出口。"""
    db_path = tmp_path / "app.db"
    client.post("/api/roles", json={"role_id": "quiet", "role_name": "q", "system_prompt": "x"})
    _seed_memory(db_path, "quiet", text="对青霉素过敏", pinned=True)
    payload = json.loads(client.get("/api/roles/quiet/export").text)
    ext = payload["data"]["extensions"]["rolecard_agent"]
    assert "memory" not in ext


def test_export_opt_in_memory_carries_only_pinned_active(
    client: TestClient, tmp_path: Path
) -> None:
    """显式勾选 `?include_memory=pinned_active`：置顶且未失效的才出门，其余一律不留。"""
    db_path = tmp_path / "app.db"
    client.post("/api/roles", json={"role_id": "keeper", "role_name": "k", "system_prompt": "x"})
    _seed_memory(db_path, "keeper", text="置顶且活着", pinned=True)
    _seed_memory(db_path, "keeper", text="没置顶", pinned=False)
    _seed_memory_invalidated(db_path, "keeper", text="置顶但已失效")
    payload = json.loads(client.get("/api/roles/keeper/export?include_memory=pinned_active").text)
    memory = payload["data"]["extensions"]["rolecard_agent"]["memory"]
    assert [m["text"] for m in memory] == ["置顶且活着"]


def test_export_rejects_unknown_opt_in_and_missing_role(client: TestClient) -> None:
    assert client.get("/api/roles/keeper/export?include_memory=all").status_code == 400
    assert client.get("/api/roles/no-such-role/export").status_code == 404
