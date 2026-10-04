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
from rolecard_agent.domains.health import records as records_router
from rolecard_agent.domains.health.service import HealthQueryService
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # 本文件的 docstring 承诺"全部离线"，但抽取端点走的是**配置里的真实后端**（它按
    # MODEL_BACKENDS 自己建模型，不像对话端点那样有 model= 注入口）。宿主机 Ollama 在跑时
    # 默认后端就真的可用，于是 `test_extract_rejects_a_second_call_while_one_is_running`
    # 变成一次 8B 真实抽取（实测 117s，占全套 60%+）。指到一个必然拒绝连接的端口，
    # 让本文件测到的确实是它声称测的东西；真模型路径见下方 live 标记的用例。
    monkeypatch.setenv(
        "MODEL_BACKENDS",
        '{"local": {"model": "qwen2.5vl:7b", "provider": "ollama",'
        ' "base_url": "http://127.0.0.1:9"}}',
    )
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
    page = client.get("/api/records").json()
    records = page["items"]
    assert page["total"] == 1
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
    # 先建 intake 再建报告：`ingestion_task_id` 是**真外键**（PRAGMA foreign_keys=ON），
    # 指向不存在的任务会被数据库拒掉。
    IngestionService(conn).create(user_id="local-user", file_hash="h1", task_id="ing_x")
    report_id = HealthQueryService(conn).create_report(
        user_id="local-user",
        report_type="腹部超声",
        check_time="2026-03-12",
        indices=[{"index_name": "结石直径", "index_value": 6.1, "source": "parsed"}],
        ingestion_task_id="ing_x",  # intake 关联由域在插入时一起写（P1-1）
    )
    conn.close()

    with TestClient(create_app(sqlite_path=db)) as c:
        res = c.post("/api/records/extract", json={"task_id": "ing_x"})
    assert res.status_code == 200, res.text
    assert res.json() == {"skipped": "already_extracted", "report_id": report_id}


def test_extract_rejects_a_second_call_while_one_is_running(client: TestClient) -> None:
    """P1-5 回归：抽取是"读-判断-写"，中间隔着几十秒的模型调用 —— 双击/并发必须被挡住。

    第二个请求要**立刻**拿到 `in_progress`（前端 30s 就 abort，所以不能让它阻塞等待
    第一个跑完），而第一个结束后一切照常 —— 互斥不能变成"永久卡住"。
    """
    tid = client.post("/api/session", json={}).json()["thread_id"]
    uploaded = client.post(
        f"/api/session/{tid}/upload",
        files={"file": ("须知.md", "每半年复查一次超声。".encode(), "text/markdown")},
    )
    assert uploaded.status_code == 201, uploaded.text
    task_id = uploaded.json()["task_id"]

    assert records_router._claim_extraction(task_id) is True  # 模拟"第一个请求正在跑"
    try:
        res = client.post("/api/records/extract", json={"task_id": task_id})
        assert res.status_code == 200, res.text
        assert res.json()["skipped"] == "in_progress"
    finally:
        records_router._release_extraction(task_id)

    # 释放后恢复正常：本文件的后端必然拒绝连接 → 稳定的 502（可读失败，不是 500）。
    # 旧断言是 `in (200, 502)`，宿主机 Ollama 在跑时真实抽取必然 200 —— 于是这条用例
    # 无论实现对错都绿（架构审计报告 §6：顶层冒烟含真模型、断言接受任何结局）。
    after = client.post("/api/records/extract", json={"task_id": task_id})
    assert after.status_code == 502, after.text


@pytest.mark.live
def test_extract_with_a_real_model_writes_an_unverified_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真模型路径（**默认不跑**，`-m live` 才跑）：离线替身覆盖不了"模型真的输出 JSON"这件事。

    为什么单独一个标记而不是塞进常规套件：一次 8B 抽取实测 90~130s，比其余 640 个用例
    加起来还贵，而且结果取决于模型版本 —— 它是**验收**（scripts/run_eval.py 同一性质），
    不是回归门禁。需要本机 Ollama + 已拉取的默认模型。
    """
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("MODEL_BACKENDS", raising=False)  # 回落到出厂默认后端（真 Ollama）
    client = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    tid = client.post("/api/session", json={}).json()["thread_id"]
    report = (
        "# 腹部超声\n\n检查时间：2026-03-12\n\n"
        "结论：胆囊内见强回声团，后方伴声影。结石直径 6.0 mm（参考范围 0-5 mm）。"
    ).encode()
    uploaded = client.post(
        f"/api/session/{tid}/upload", files={"file": ("腹部超声.md", report, "text/markdown")}
    )
    assert uploaded.status_code == 201, uploaded.text

    res = client.post("/api/records/extract", json={"task_id": uploaded.json()["task_id"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert "mode" in body and "written" in body, body
    # 铁律：AI 抽取的一律未人工校验，且**关联回这次 intake**（幂等靠它）。
    for item in client.get("/api/records").json()["items"]:
        for index in item["indices"]:
            assert not index["is_verified"], index


def test_deleting_a_report_also_clears_its_knowledge_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-1 回归：报告行删了，它的**检索分块**必须一起删。

    两边是两处存储（SQLite / chroma）：只删行的话，"已经删掉"的病历原文仍会被模型
    检索到并引用，而界面上看不出任何异常 —— 这正是最危险的一类不一致。
    """
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    db = tmp_path / "app.db"

    # 在库外先造出「已抽取的报告 + intake 关联」：抽取本身要模型，这里只关心删除路径
    conn = connect(db)
    bootstrap(conn, enabled_domains=("health",))
    conn.execute("INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('local','demo')")
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES ('local-user','local','demo')"
    )
    conn.commit()
    IngestionService(conn).create(user_id="local-user", file_hash="h1", task_id="ing_x")
    report_id = HealthQueryService(conn).create_report(
        user_id="local-user",
        report_type="腹部超声",
        check_time="2026-03-12",
        indices=[{"index_name": "结石直径", "index_value": 6.1, "source": "parsed"}],
        ingestion_task_id="ing_x",
    )
    conn.close()

    with TestClient(create_app(sqlite_path=db)) as c:
        ctx = c.app.state.ctx
        scope = ctx.health.knowledge_scope  # 域自己声明的知识作用域（api 不 import 具体域）
        assert ctx.knowledge.index(scope, "ing_x", ARTICLE, source_name="须知.md") >= 1
        assert ctx.knowledge.scope_count(scope) >= 1

        res = c.delete(f"/api/records/report/{report_id}")

        assert res.status_code == 204, res.text
        assert ctx.knowledge.scope_count(scope) == 0, "删了报告，向量分块还在"
        assert c.get("/api/records").json()["items"] == []
        actions = {a["action"] for a in c.get("/api/audit?limit=20").json()}
        assert "delete_report" in actions


ARTICLE = "# 随访须知\n\n每半年复查一次超声；发现腹痛、发热或黄疸请及时就医。"


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
