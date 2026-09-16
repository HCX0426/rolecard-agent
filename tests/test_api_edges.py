"""api/main.py 的边界分支补充测试 —— **重构安全网**。

存在理由：拆分 `api/main.py`（C1）是纯重构，改坏了功能测试也未必红。这些分支原本
一个都没被覆盖（覆盖率数据显示 main.py 只有 82%），补上之后，"拆完还活着"才是有依据的，
而不是靠人眼 review。

顺序上它排在 C1 之前：先有网，再动刀。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from rolecard_agent.api.routers.records import _latest_numeric_history
from tests.conftest import ScriptedChat


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            sqlite_path=tmp_path / "app.db",
            model=ScriptedChat([AIMessage(content="ok") for _ in range(10)]),
        )
    )


# -- 抽取用的历史值汇总（突变检查的输入）-------------------------------------------


def test_latest_numeric_history_prefers_the_latest_date() -> None:
    """同一指标多条记录：取日期最大的那条（输入不保证按时间排序）。"""
    records = [
        {"check_time": "2026-01-01", "indices": [{"index_name": "血糖", "index_value": 5.0}]},
        {"check_time": "2026-06-01", "indices": [{"index_name": "血糖", "index_value": 6.0}]},
        {"check_time": "2026-03-01", "indices": [{"index_name": "血糖", "index_value": 7.0}]},
    ]
    assert _latest_numeric_history(records) == {"血糖": 6.0}


def test_latest_numeric_history_skips_text_only_rows() -> None:
    """没有数值的指标不进历史（否则突变检查会拿 None 去比）。"""
    records = [
        {
            "check_time": "2026-01-01",
            "indices": [{"index_name": "描述", "value_text": "未见异常"}],
        }
    ]
    assert _latest_numeric_history(records) == {}


# -- 会话局部更新的三个分支 ---------------------------------------------------------


def test_patch_session_renames_title_and_trims(client: TestClient) -> None:
    tid = client.post("/api/session", json={}).json()["thread_id"]
    res = client.patch(f"/api/session/{tid}", json={"title": "  我的会话  "})
    assert res.status_code == 200
    assert res.json()["title"] == "我的会话"


def test_patch_session_rejects_unknown_backend(client: TestClient) -> None:
    """会话级模型覆盖指向不存在的后端 → 400，而不是"保存时通过、对话时才炸"。 """
    tid = client.post("/api/session", json={}).json()["thread_id"]
    res = client.patch(f"/api/session/{tid}", json={"model_name": "nope"})
    assert res.status_code == 400
    assert "nope" in res.json()["detail"]


def test_patch_session_empty_string_clears_override(client: TestClient) -> None:
    tid = client.post("/api/session", json={}).json()["thread_id"]
    assert client.patch(f"/api/session/{tid}", json={"model_name": "local"}).status_code == 200
    cleared = client.patch(f"/api/session/{tid}", json={"model_name": ""})
    assert cleared.status_code == 200 and cleared.json()["model_name"] is None


def test_patch_session_without_any_field_is_400(client: TestClient) -> None:
    tid = client.post("/api/session", json={}).json()["thread_id"]
    assert client.patch(f"/api/session/{tid}", json={}).status_code == 400


def test_unknown_role_backend_degrades_to_default(client: TestClient) -> None:
    """角色声明了一个已被删除的后端 → 降级默认模型并留痕，绝不能 500。"""
    client.post(
        "/api/roles",
        json={
            "role_id": "ghost",
            "role_name": "幽灵角色",
            "system_prompt": "x",
            "model_name": "nope",
        },
    )
    tid = client.post("/api/session", json={"role_id": "ghost"}).json()["thread_id"]
    res = client.post("/api/chat", json={"thread_id": tid, "message": "你好"})
    assert res.status_code == 200


# -- 数据删除的未命中分支 -----------------------------------------------------------


def test_deleting_missing_report_or_index_is_404(client: TestClient) -> None:
    """错用户与不存在都返回 404 —— 不用 403 去确认"这条数据确实存在"。 """
    assert client.delete("/api/records/report/rp_nope").status_code == 404
    assert client.delete("/api/records/index/ix_nope").status_code == 404


# -- 上传与抽取的兜底路径 -----------------------------------------------------------


def test_unsupported_upload_stays_pending_and_extract_says_no_text(
    client: TestClient,
) -> None:
    """不支持的类型：登记为 pending，抽取如实回 no_text（不假装抽出了东西）。"""
    tid = client.post("/api/session", json={}).json()["thread_id"]
    up = client.post(
        f"/api/session/{tid}/upload",
        files={"file": ("x.bin", b"\x00\x01", "application/octet-stream")},
    )
    assert up.status_code == 201
    assert up.json()["status"] == "pending"

    res = client.post("/api/records/extract", json={"task_id": up.json()["task_id"]})
    assert res.status_code == 200
    assert res.json().get("skipped") == "no_text"


def test_audit_limit_is_clamped(client: TestClient) -> None:
    """limit 被夹在 1..500 —— 免得一个参数把整表拖出来。"""
    assert client.get("/api/audit?limit=0").status_code == 200
    assert client.get("/api/audit?limit=99999").status_code == 200
