"""数据管理端点测试：手动补录报告 + 知识作用域重建。

Traceability: US-3（数据变更写审计）、US-8（知识作用域）。

背景：**主流程是"上传报告/图片让 AI 解析"，手动补录只是兜底入口**（最小可用契约）。
全部离线：临时库 + 临时 chroma，绝不碰仓库里的 data/。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.domains.health.service import HealthQueryService
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"))


# -- 手动补录报告（最小可用） ----------------------------------------------------------


def test_create_report_minimal(client: TestClient) -> None:
    """最小可用：类型 + 检查时间 + 一行指标（名称 + 数值），单位等其余可省。"""
    res = client.post(
        "/api/records/report",
        json={
            "report_type": "腹部超声",
            "check_time": "2026-03-12",
            "indices": [{"index_name": "结石直径", "index_value": 6.1, "unit": "mm"}],
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["report_type"] == "腹部超声"
    assert len(body["indices"]) == 1
    row = body["indices"][0]
    assert row["index_name"] == "结石直径"
    assert float(row["index_value"]) == 6.1
    # 手填 ≠ 已核实：默认未校验（前端据此显示【未经人工校验】）
    assert not row["is_verified"]


def test_create_report_accepts_text_value(client: TestClient) -> None:
    """文本型指标（如"未见异常"）同样是最小可用的合法输入。"""
    res = client.post(
        "/api/records/report",
        json={
            "report_type": "腹部超声",
            "check_time": "2026-03",
            "indices": [{"index_name": "胆囊", "value_text": "未见异常"}],
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["indices"][0]["value_text"] == "未见异常"


def test_create_report_rejects_without_indices(client: TestClient) -> None:
    """没有指标行的报告无法被查询 —— 400（用户输入错误，不是 500）。"""
    res = client.post(
        "/api/records/report",
        json={"report_type": "腹部超声", "check_time": "2026-03-12", "indices": []},
    )
    assert res.status_code == 400
    assert "indicator" in res.json()["detail"]


def test_create_report_rejects_indicator_without_value(client: TestClient) -> None:
    res = client.post(
        "/api/records/report",
        json={
            "report_type": "腹部超声",
            "check_time": "2026-03-12",
            "indices": [{"index_name": "结石直径"}],
        },
    )
    assert res.status_code == 400
    assert "结石直径" in res.json()["detail"]


def test_create_report_requires_type_and_time(client: TestClient) -> None:
    """类型 / 检查时间缺失由 pydantic 拦成 422（字段级校验），不落到业务层。"""
    res = client.post("/api/records/report", json={"indices": []})
    assert res.status_code == 422


def test_create_report_is_audited(client: TestClient) -> None:
    client.post(
        "/api/records/report",
        json={
            "report_type": "腹部超声",
            "check_time": "2026-03-12",
            "indices": [{"index_name": "胆囊", "value_text": "未见异常"}],
        },
    )
    actions = {a["action"] for a in client.get("/api/audit?limit=50").json()}
    assert "create_report" in actions


def test_created_report_appears_in_records(client: TestClient) -> None:
    """补录后必须出现在数据管理列表里（写入路径闭环）。"""
    client.post(
        "/api/records/report",
        json={
            "report_type": "腹部超声",
            "check_time": "2026-03-12",
            "indices": [{"index_name": "结石直径", "index_value": 6.1}],
        },
    )
    records = client.get("/api/records").json()
    assert len(records) == 1
    assert records[0]["report_type"] == "腹部超声"


# -- 知识作用域重建 --------------------------------------------------------------------


def test_reset_knowledge_scope_removes_and_audits(client: TestClient) -> None:
    """清空作用域是破坏性动作：删掉集合 + 写审计（含清掉的分块数）。"""
    tid = client.post("/api/session", json={}).json()["thread_id"]
    uploaded = client.post(
        f"/api/session/{tid}/upload",
        files={"file": ("须知.md", "每半年复查一次超声。".encode(), "text/markdown")},
    )
    assert uploaded.status_code == 201
    assert client.get("/api/knowledge").json(), "上传后应已有作用域"

    res = client.delete("/api/knowledge/health_reports")
    assert res.status_code == 200, res.text
    assert res.json()["removed_chunks"] >= 1
    assert client.get("/api/knowledge").json() == []

    actions = {a["action"] for a in client.get("/api/audit?limit=50").json()}
    assert "reset_knowledge_scope" in actions


# -- 结构化抽取端点（v2.3） -----------------------------------------------------------


def test_extract_unknown_task_is_404(client: TestClient) -> None:
    res = client.post("/api/records/extract", json={"task_id": "ing_nope"})
    assert res.status_code == 404


def test_extract_requires_a_task_id(client: TestClient) -> None:
    assert client.post("/api/records/extract", json={}).status_code == 422


def test_extract_degrades_honestly_without_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型后端不可用 → 必须如实降级（502 或 200+skipped），**绝不能 500**，
    也不能假装抽取成功（那会把没校验过的数字塞进档案）。

    前提必须显式受控：本机 Ollama 在跑时默认后端是真实可用的，会把这条测试
    变成"真实抽取成功"路径。所以把后端指到一个必然拒绝连接的端口（:9），
    无论宿主机状态如何，测到的都是降级分支。"""
    monkeypatch.setenv(
        "MODEL_BACKENDS",
        '{"local": {"model": "qwen2.5vl:7b", "provider": "ollama",'
        ' "base_url": "http://127.0.0.1:9"}}',
    )
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    client = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    tid = client.post("/api/session", json={}).json()["thread_id"]
    uploaded = client.post(
        f"/api/session/{tid}/upload",
        files={"file": ("须知.md", "每半年复查一次超声。".encode(), "text/markdown")},
    )
    res = client.post("/api/records/extract", json={"task_id": uploaded.json()["task_id"]})
    assert res.status_code == 502, res.text
    # 失败也必须留痕：失败的抽取尝试写 extract_report_failed 审计。
    audit = client.get("/api/audit?limit=300").json()
    actions = {a["action"] for a in audit}
    assert "extract_report_failed" in actions

    # 且留痕内容**不得带内部端点**（代码审查报告（第二轮）A5 / M11）：
    # /api/audit 是前端可见接口，而连接类异常天然带着 base_url。
    # 注：异常文本因平台而异（Windows 是 WinError、类 Unix 是 httpx 的 URL 文案），
    # 所以这里断言的是**不变量**（不带 http 端点）而不是某一条具体文案；
    # 脱敏占位符本身的语义由 tests/unit/test_observability.py 直接覆盖。
    failed = next(a for a in audit if a["action"] == "extract_report_failed")
    detail = str(failed["detail_json"])
    assert "127.0.0.1:9" not in detail, detail
    assert "http://" not in detail, detail
    assert "https://" not in detail, detail


def test_audit_supports_cursor_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`before_id` 游标翻页（代码审查报告（第二轮）B6 / M11）。

    审计表只增不减，"每次倒序取 N 条"在表变大后既慢又取不全 —— 需要能稳定地往前翻。
    """
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    client = TestClient(create_app(sqlite_path=tmp_path / "audit.db"))

    for _ in range(5):
        assert client.post("/api/session", json={}).status_code == 201

    first_page = client.get("/api/audit?limit=2").json()
    assert len(first_page) == 2
    assert first_page[0]["id"] > first_page[1]["id"]  # 倒序
    assert all("id" in row for row in first_page)  # 游标字段必须回传

    cursor = first_page[-1]["id"]
    second_page = client.get(f"/api/audit?limit=2&before_id={cursor}").json()
    assert len(second_page) == 2
    assert all(row["id"] < cursor for row in second_page)  # 严格向前，不重叠
    assert not ({r["id"] for r in first_page} & {r["id"] for r in second_page})


def test_extract_is_idempotent_per_ingestion_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个 intake 已经抽出过报告 → 再抽不重复写（幂等靠 medical_report.ingestion_task_id）。"""
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    db = tmp_path / "app.db"

    conn = connect(db)
    bootstrap(conn, enabled_domains=("health",))
    conn.execute("INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('local','demo')")
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES ('local-user','local','demo')"
    )
    conn.commit()
    report_id = HealthQueryService(conn).create_report(
        user_id="local-user",
        report_type="腹部超声",
        check_time="2026-03-12",
        indices=[{"index_name": "结石直径", "index_value": 6.1, "source": "parsed"}],
    )
    ingestion = IngestionService(conn)
    ingestion.create(user_id="local-user", file_hash="h1", task_id="ing_x")
    ingestion.link_report("ing_x", report_id)
    conn.close()

    with TestClient(create_app(sqlite_path=db)) as c:
        res = c.post("/api/records/extract", json={"task_id": "ing_x"})
    assert res.status_code == 200, res.text
    assert res.json() == {"skipped": "already_extracted", "report_id": report_id}


# -- 角色 exemplars（前端表单要写入的字段，确认 API 端到端支持） ------------------------


def test_role_exemplars_round_trip(client: TestClient) -> None:
    """Traceability: US-8 — exemplars 经 API 存取往返一致。"""
    payload = {
        "role_id": "coach",
        "role_name": "教练",
        "system_prompt": "你是随访教练。",
        "exemplars": [{"user": "我该复查吗", "assistant": "每半年一次，别拖。"}],
    }
    assert client.post("/api/roles", json=payload).status_code == 201
    row = next(r for r in client.get("/api/roles").json() if r["role_id"] == "coach")
    assert row["exemplars"][0]["user"] == "我该复查吗"
    assert row["exemplars"][0]["assistant"] == "每半年一次，别拖。"
