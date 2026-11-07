"""知识库管理页（2026-10-24）的端到端用例：建库 → 导文档 → 进度 → 概览 → 审计。

走真 HTTP（TestClient + ScriptedChat 离线内核），钉住管理页依赖的四件事：
* 新建作用域立即出现在全局清单与角色卡下拉的数据源里；
* 文档导入走**后台**（201 回 task id），进度端点与会话上传同源；
* 导入完成后 `describe` 的分块数增长，且作用域名合法（SCOPE_PATTERN 拒绝坏名字）;
* 两个新动作都写审计（管理面变更必须留痕）。
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app


def _wait_indexed(client: TestClient, task_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/uploads/tasks/{task_id}").json()
        if not body.get("running"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"task never finished: {task_id}")


def test_scope_create_list_and_invalid_name(tmp_path: Path) -> None:
    app = create_app(sqlite_path=tmp_path / "kb.db")
    with TestClient(app) as client:
        res = client.post("/api/knowledge/scopes", json={"name": "my_notes"})
        assert res.status_code == 201
        assert res.json() == {"scope": "my_notes", "chunks": 0, "sources": []}
        # 幂等：同名重建原样返回（不重置、不报错）
        again = client.post("/api/knowledge/scopes", json={"name": "my_notes"})
        assert again.status_code == 201
        # 清单里可见（角色卡作用域下拉的数据源）
        scopes = client.get("/api/knowledge/scopes").json()["scopes"]
        assert "my_notes" in scopes
        # 坏名字 → 422（pydantic Field pattern 先拒；fastapi 的标准校验错误码，
        # 前端 readableDetail 会把 422 数组解成可读文本）
        bad = client.post("/api/knowledge/scopes", json={"name": "1 Bad Name!"})
        assert bad.status_code == 422


def test_document_import_lands_in_scope(tmp_path: Path) -> None:
    app = create_app(sqlite_path=tmp_path / "kb2.db")
    with TestClient(app) as client:
        client.post("/api/knowledge/scopes", json={"name": "trip_notes"})
        res = client.post(
            "/api/knowledge/scopes/trip_notes/documents",
            files={
                "file": (
                    "kyoto.md",
                    "# 京都\n\n巴士一日券 700 円，地铁巴士通票 1100 円。岚山在西路。".encode(),
                    "text/markdown",
                )
            },
        )
        assert res.status_code == 201
        task_id = res.json()["task_id"]
        assert res.json()["scope"] == "trip_notes"

        task = _wait_indexed(client, task_id)
        assert task["status"] == "indexed"
        # 分块数不在进度端点里（台账只记状态）—— 页面数据源是概览端点
        described = {row["scope"]: row for row in client.get("/api/knowledge").json()}
        assert described["trip_notes"]["chunks"] >= 1
        assert any("kyoto" in s for s in described["trip_notes"]["sources"])

        # 角色卡作用域下拉的数据源（所见即所得）
        assert "trip_notes" in client.get("/api/knowledge/scopes").json()["scopes"]


def test_document_import_rejects_empty_and_bad_scope(tmp_path: Path) -> None:
    app = create_app(sqlite_path=tmp_path / "kb3.db")
    with TestClient(app) as client:
        empty = client.post(
            "/api/knowledge/scopes/my_notes/documents",
            files={"file": ("x.txt", b"", "text/plain")},
        )
        assert empty.status_code == 400
        bad = client.post(
            "/api/knowledge/scopes/1 Bad/documents",
            files={"file": ("x.txt", b"content", "text/plain")},
        )
        assert bad.status_code == 400


def test_knowledge_management_writes_audit(tmp_path: Path) -> None:
    app = create_app(sqlite_path=tmp_path / "kb4.db")
    with TestClient(app) as client:
        client.post("/api/knowledge/scopes", json={"name": "audited"})
        client.post(
            "/api/knowledge/scopes/audited/documents",
            files={"file": ("note.txt", "内容".encode(), "text/plain")},
        )
        rows = client.get("/api/audit?limit=50").json()
        actions = {r["action"] for r in rows}
        assert "create_knowledge_scope" in actions
        assert "import_knowledge_document" in actions


def test_document_delete_removes_only_that_document(tmp_path: Path) -> None:
    """删某一份文档：它的全部分块走、同库别的文档不受影响，且写审计（用户报的缺口）。

    概览只给展示名，删除按名解析身份下刀 —— 这正是「有删除某个结构化知识的按钮吗」
    此前找不到地方那一问的后端出口。用导入回来的真实展示名，不猜 safe_name 的写法。
    """
    app = create_app(sqlite_path=tmp_path / "kb5.db")
    with TestClient(app) as client:
        client.post("/api/knowledge/scopes", json={"name": "trip_notes"})
        first = client.post(
            "/api/knowledge/scopes/trip_notes/documents",
            files={"file": ("kyoto.md", "京都巴士一日券 700 円。".encode(), "text/markdown")},
        )
        _wait_indexed(client, first.json()["task_id"])
        second = client.post(
            "/api/knowledge/scopes/trip_notes/documents",
            files={"file": ("osaka.md", "大阪城公园门票 600 円。".encode(), "text/markdown")},
        )
        _wait_indexed(client, second.json()["task_id"])

        described = {row["scope"]: row for row in client.get("/api/knowledge").json()}
        sources = described["trip_notes"]["sources"]
        kyoto = next(s for s in sources if "kyoto" in s)
        osaka = next(s for s in sources if "osaka" in s)
        before = described["trip_notes"]["chunks"]

        res = client.delete(f"/api/knowledge/trip_notes/documents/{kyoto}")
        assert res.status_code == 200
        assert res.json()["removed_chunks"] >= 1

        after = {row["scope"]: row for row in client.get("/api/knowledge").json()}["trip_notes"]
        assert after["chunks"] == before - res.json()["removed_chunks"]
        assert kyoto not in after["sources"], "删掉的文档还在来源名单里"
        assert osaka in after["sources"], "不该连带删掉别的文档"

        actions = {r["action"] for r in client.get("/api/audit?limit=50").json()}
        assert "delete_knowledge_document" in actions


def test_document_delete_is_idempotent_and_validates_scope(tmp_path: Path) -> None:
    """删不存在的名 = 200 删 0 段（重复点删除不该报错）；坏作用域名 = 400（与建库同口径）。"""
    app = create_app(sqlite_path=tmp_path / "kb6.db")
    with TestClient(app) as client:
        client.post("/api/knowledge/scopes", json={"name": "trip_notes"})
        gone = client.delete("/api/knowledge/trip_notes/documents/从没入过库.pdf")
        assert gone.status_code == 200
        assert gone.json()["removed_chunks"] == 0

        bad = client.delete("/api/knowledge/1 Bad/documents/x.pdf")
        assert bad.status_code == 400
