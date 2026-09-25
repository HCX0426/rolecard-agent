"""全功能冒烟自测（离线、无真实模型）：把每个端点与关键行为各走一遍。

为什么存在：前面的功能分批落地（M1 内核 → M5 前端 → v2.1 RAG → 会话级模型），
单测覆盖的是"单元"，这里覆盖的是"接线"——端点挂没挂、字段回没回、审计写没写、
切换生不生效。一句话：回答"前面所有功能是否都正确实现了"。

用法：

    .venv\\Scripts\\python.exe scripts\\smoke_check.py     # 退出码非 0 = 有功能断了

设计：**冒烟测接线，不测模型**。create_app + TestClient（真实 API 路径，与前端同源），
模型走两层替身 —— 对话链路用鸭子类型的假模型（`model=` + `model_factory=`，后者保证
设置页保存后的**热重建**也拿不到真后端），抽取链路没有模型注入口（它按配置自建调用器），
所以把 `MODEL_BACKENDS` 指到一个必然拒绝连接的端口，让"模型不可用"成为确定的前提。
RAG 用 hash 嵌入器（无 key 时的默认，离线确定）。真模型路径不在这里，见
`pytest -m live` 与 scripts/run_eval.py。最后一项是真机 UI 冒烟（依赖外部服务，可跳过）。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

# 仓库根：真机 UI 冒烟需要以仓库根为 cwd 调用 scripts/ui_smoke.js
ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 终端在 GBK 代码页（中文 Windows 默认）下无法编码 emoji/✅❌，捕获到文件也需 UTF-8。
# 早一点把 stdout/stderr 固定成 UTF-8：直接跑终端可能显示方框，但绝不会崩。
import contextlib  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from rolecard_agent.api.main import create_app  # noqa: E402


def _make_pdf_bytes(text: str) -> bytes:
    """构造一个最小但合法的 1 页 PDF（含文本），供上传链路测试真实解析用。

    仅 ASCII（基础 14 字体 + latin-1 编码足以承载）；中文 PDF 文本由解析测试另覆盖。
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    return bytes(out)


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    """构造一个最小合法 .docx(OOXML = zip + word/document.xml)，供上传链路验证 Office 解析。"""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>' for t in paragraphs
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


class FakeChat:
    """最小 ChatLike：记录绑定过的工具，返回固定答复 —— 用于验证链路而非模型。"""

    def __init__(self) -> None:
        self.bound: list[str] = []

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN201, ARG002
        self.bound = [t.name for t in tools]
        return self

    def invoke(self, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return AIMessage(content="【冒烟答复】已收到你的问题。")

    def stream(self, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        # 内核现在走流（#18：停止要能落在分块边界上）。一次给整块 = 累加的恒等情形。
        yield self.invoke(prompt)


def _flat_models(payload: dict) -> dict:
    """分组视图 → `name → 行`（`has_key` 在组上，抄到每行，读侧写法与拆层前一致）。

    GET/PUT /api/settings/models 早就只剩 `providers` 这一个视图（平铺的 `backends`
    投影随旧界面一起删了），而写侧的请求体仍叫 `backends` —— 这里改的是**读**。
    """
    out: dict[str, dict] = {}
    for group in payload["providers"]:
        for row in group["models"]:
            out[str(row["name"])] = {**row, "has_key": group["has_key"]}
    return out


RESULTS: list[tuple[str, bool, str]] = []


def run_check(name: str, fn: Callable[[], None]) -> None:
    """跑一项并把结论记进 RESULTS —— 冒烟要把任何异常都记成"功能断了"，而不是崩掉整轮。

    失败说明里带上**断言在哪一行断的**：项内十几条断言不每条都写得出消息（写了也重复），
    而只报"断言失败"的冒烟等于让人回去读代码才知道是哪一步断了。
    """
    try:
        fn()
    except AssertionError as exc:
        where = ""
        tb = exc.__traceback__
        while tb is not None:
            where = f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
            tb = tb.tb_next
        RESULTS.append((name, False, f"{str(exc) or '断言失败'}（{where}）"))
    except Exception as exc:  # noqa: BLE001 - 冒烟要把任何异常记为"功能断了"
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
    else:
        RESULTS.append((name, True, ""))


def check(name: str) -> None:  # 装饰器：把函数的 docstring 当作判定依据
    def wrap(fn):
        run_check(name, fn)
        return fn

    return wrap


def _sse(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def main() -> int:
    model = FakeChat()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db_path = root / "smoke.db"
        # 关键：把数据目录也指向临时目录，否则会污染演示库（data/chroma / data/uploads）
        from rolecard_agent.domains.health.service import HealthQueryService
        from rolecard_agent.storage.db import bootstrap, connect

        os.environ["CHROMA_PATH"] = str(root / "chroma")
        os.environ["UPLOAD_DIR"] = str(root / "uploads")
        # 抽取链路按**配置**自建模型调用器（没有 model= 注入口），所以把后端指到一个必然
        # 拒绝连接的端口：真模型不在冒烟的职责范围内（一次 8B 抽取实测 90~130s，且结果
        # 随模型版本漂移），而"模型不可用要如实降级"这条契约需要**确定的前提**才能钉死。
        os.environ["MODEL_BACKENDS"] = (
            '{"local": {"model": "qwen2.5vl:7b", "provider": "ollama",'
            ' "base_url": "http://127.0.0.1:9"}}'
        )
        # 预热是真 POST /api/generate —— 冒烟不跑推理，不需要把模型钉进显存。
        os.environ["MODEL_PIN_ON_STARTUP"] = "0"
        # 档案数据在起服务前注入：避免另开连接与应用的连接互相锁库
        conn = connect(db_path)
        bootstrap(conn, enabled_domains=("health",))  # 建表（create_app 内的 bootstrap 幂等）
        conn.executescript(  # 报告外键指向 app_user
            "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('local', 'demo');"
            "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
            "  VALUES ('local-user', 'local', 'demo user');"
        )
        conn.commit()
        HealthQueryService(conn).create_report(
            user_id="local-user",
            report_type="超声",
            check_time="2026-04-01",
            indices=[
                {
                    "index_name": "结石直径",
                    "index_value": 7.0,
                    "unit": "mm",
                    "ref_range": "0-5",
                    "is_verified": False,
                }
            ],
        )
        conn.close()
        # `model_factory` 同样要给假模型：设置页保存后 rebuild_runtime 用它重建（此前只传
        # `model=`，于是热重建把假模型换成**真后端** —— `_settings` 那一项因此偷偷打了
        # 一次真 Ollama，与"离线冒烟"的自述不符（架构审计报告 §6））。
        app = create_app(sqlite_path=db_path, model=model, model_factory=lambda *_a, **_k: model)
        with TestClient(app) as c:
            run_all(c, db_path)
    # 真机 UI 冒烟放**最后**：它依赖外部服务与本机浏览器，且是整套里最贵的一项。
    # 此前它是模块级 `@check` 装饰的函数 —— 装饰即执行，于是实际跑在**最前**（顺序反了，
    # 且离线项还没跑就可能被它带崩）。现在显式在离线项之后调用。
    run_check(
        "真机 UI 冒烟（浏览器打开控制台：可发消息 / 思考过程保留 / 刷新后历史仍在）",
        console_ui_smoke,
    )
    return report()


def run_all(c: TestClient, db_path: Path) -> None:  # noqa: C901 - 冒烟脚本宁可平铺
    @check("控制台页面（静态托管 SPA）")
    def _console() -> None:
        res = c.get("/")
        assert res.status_code == 200, res.status_code
        assert '<div id="root">' in res.text, "未挂载 React 入口"

    @check("角色卡：内置 / 新建 / 编辑 / 删除")
    def _roles() -> None:
        builtin = c.get("/api/roles").json()
        assert any(r["role_id"] == "medical_archivist" for r in builtin)
        created = c.post(
            "/api/roles",
            json={
                "role_id": "smoke_role",
                "role_name": "冒烟角色",
                "system_prompt": "x",
                "model_name": "local",
                "tool_whitelist": ["list_reports"],
            },
        )
        assert created.status_code == 201, created.text
        patched = c.patch("/api/roles/smoke_role", json={"description": "改过"})
        assert patched.status_code == 200 and patched.json()["description"] == "改过"
        assert c.delete("/api/roles/smoke_role").status_code == 204

    @check("工具目录：内核能力 + 域工具（含 search_knowledge）")
    def _catalog() -> None:
        body = c.get("/api/tools/catalog").json()
        assert "search_knowledge" in [t["name"] for t in body["kernel"]], body
        health = {t["name"] for t in body["domains"].get("health", [])}
        assert {"query_health_record", "compare_health_index"} <= health

    @check("插件启停：停用递增 tool_epoch，启停即时生效")
    def _plugins() -> None:
        # finance 域加入后插件不止一个且按 plugin_id 排序 —— 必须显式点名 health，
        # 不能假设列表第 0 项是它（多域后 f < h，[0] 会拿到 finance）。
        before = next(
            p for p in c.get("/api/plugins").json() if p["plugin_id"] == "health"
        )
        off = c.post("/api/plugins/health/toggle", json={"enabled": False}).json()
        assert int(off["tool_epoch"]) > 1, off
        on = c.post("/api/plugins/health/toggle", json={"enabled": True}).json()
        assert on["enabled"] is True and before["plugin_id"] == "health"

    @check("会话：新建 / 列表 / 详情 / 历史 / 重命名 / 模型覆盖")
    def _sessions() -> None:
        s = c.post("/api/session", json={}).json()
        tid = s["thread_id"]
        # 默认"无角色"：general_assistant（纯对话，不接工具与档案）
        assert s["role_id"] == "general_assistant", s
        assert any(x["thread_id"] == tid for x in c.get("/api/sessions").json())
        assert c.get(f"/api/session/{tid}").json()["model_name"] is None
        assert c.get(f"/api/session/{tid}/messages").json()["messages"] == []
        renamed = c.patch(f"/api/session/{tid}", json={"title": "冒烟会话"})
        assert renamed.json()["title"] == "冒烟会话", renamed.text
        overridden = c.patch(f"/api/session/{tid}", json={"model_name": "local"})
        assert overridden.json()["model_name"] == "local", overridden.text
        assert c.get(f"/api/session/{tid}").json()["model_name"] == "local"
        bad = c.patch(f"/api/session/{tid}", json={"model_name": "ghost"})
        assert bad.status_code == 400, bad.status_code

    @check("对话：SSE 流式 + checkpoint 回放 + 标题自动生成")
    def _chat() -> None:
        s = c.post("/api/session", json={}).json()
        res = c.post("/api/chat", json={"thread_id": s["thread_id"], "message": "冒烟提问"})
        assert res.status_code == 200
        events = _sse(res.text)
        assert [e for e in events if e["type"] == "end"], events
        msgs = c.get(f"/api/session/{s['thread_id']}/messages").json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"], msgs
        listing = c.get("/api/sessions").json()
        titled = [x for x in listing if x["thread_id"] == s["thread_id"]][0]
        assert titled["title"] == "冒烟提问", titled
        assert c.delete(f"/api/session/{s['thread_id']}").status_code == 204

    @check("上传：.txt/.pdf/.docx 建索引 / 重复复用 / 空文件拒绝")
    def _upload() -> None:
        s = c.post("/api/session", json={}).json()
        tid = s["thread_id"]
        doc = "# 随访须知\n\n每半年复查一次超声。".encode()  # bytes 只能 ASCII，故编码
        first = c.post(
            f"/api/session/{tid}/upload",
            files={"file": ("须知.md", doc, "text/markdown")},
        )
        assert first.status_code == 201 and first.json()["status"] == "indexed", first.text
        again = c.post(
            f"/api/session/{tid}/upload",
            files={"file": ("须知.md", doc, "text/markdown")},
        )
        assert again.json()["reused"] is True and again.json()["task_id"] == first.json()["task_id"]
        # v2.2：PDF 真实解析入索引（不再是 pending）
        pdf = c.post(
            f"/api/session/{tid}/upload",
            files={
                "file": (
                    "报告.pdf",
                    _make_pdf_bytes("Follow-up: glucose 6.1, recheck."),
                    "application/pdf",
                )
            },
        )
        assert pdf.json()["status"] == "indexed", pdf.text
        # v2.2：Office（.docx）文本抽取入索引（OOXML = zip + XML，零依赖）
        docx = c.post(
            f"/api/session/{tid}/upload",
            files={
                "file": (
                    "随访.docx",
                    _make_docx_bytes(["复查须知", "每半年复查一次超声。"]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert docx.json()["status"] == "indexed", docx.text
        empty = c.post(f"/api/session/{tid}/upload", files={"file": ("a.txt", b"", "text/plain")})
        assert empty.status_code == 400
        # 注入的说明消息应进入会话历史（模型下一轮知道有文件已索引）
        msgs = c.get(f"/api/session/{tid}/messages").json()["messages"]
        assert any("已建立检索索引" in str(m.get("content", "")) for m in msgs), msgs
        # v2.3 结构化抽取端点：未知任务 404；模型不可用时**如实 502**（可读失败，不是 500，
        # 也不是 200+假装成功）。本冒烟把后端指向死端口，所以"模型不可用"是确定前提 ——
        # 旧断言写成 `in (200, 502)`，在装了 Ollama 的机器上真实抽取必然 200，于是恒绿。
        assert c.post("/api/records/extract", json={"task_id": "ing_nope"}).status_code == 404
        ex = c.post("/api/records/extract", json={"task_id": first.json()["task_id"]})
        assert ex.status_code == 502, ex.text

    @check("上传：同名不同内容的文件互不覆盖（索引身份 = task id，不是文件名）")
    def _same_name_upload() -> None:
        """P0 回归（审查报告 2026-09-17）。

        改前上传路径把**原始文件名**当索引身份，而分块 id 由 `md5(f"{source}:{i}")` 推导：
        两份都叫「报告.md」的文件会共享同一批 id，第二份 upsert 直接覆盖第一份 ——
        旧文档的索引永久消失，且界面/接口上没有任何提示。这里用真实上传链路证明
        两份都在库里（分块数 +2，而不是 +1）。
        """

        def scope_chunks(name: str) -> int:
            for item in c.get("/api/knowledge").json():
                if item["scope"] == name:
                    return int(item["chunks"])
            return 0

        tid = c.post("/api/session", json={}).json()["thread_id"]
        before = scope_chunks("health_reports")
        first = c.post(
            f"/api/session/{tid}/upload",
            files={
                "file": (
                    "报告.md",
                    "# 超声随访\n\n2026-01 复查：结石 6.0 mm。".encode(),
                    "text/markdown",
                )
            },
        )
        second = c.post(
            f"/api/session/{tid}/upload",
            files={
                "file": (
                    "报告.md",
                    "# 血脂随访\n\n2026-02 复查：低密度脂蛋白 3.2 mmol/L。".encode(),
                    "text/markdown",
                )
            },
        )
        assert first.status_code == 201 and second.status_code == 201, (first.text, second.text)
        # 内容不同 → 不是同一次上传，不能被去重成同一个任务（那也会让两份合成一份）
        assert first.json()["reused"] is False and second.json()["reused"] is False
        assert first.json()["task_id"] != second.json()["task_id"]

        after = scope_chunks("health_reports")
        assert after - before >= 2, f"同名文件互相覆盖了：分块数 {before} → {after}"
        # 展示名仍应是用户看到的文件名（身份与展示名分离）
        sources = [
            x["sources"] for x in c.get("/api/knowledge").json() if x["scope"] == "health_reports"
        ][0]
        assert "报告.md" in sources, sources

    @check("知识库概览：作用域 / 分块数 / 来源 / 嵌入器 / 重建作用域")
    def _knowledge() -> None:
        body = c.get("/api/knowledge").json()
        scope = [x for x in body if x["scope"] == "health_reports"]
        assert scope, body
        assert scope[0]["chunks"] >= 1 and "须知.md" in scope[0]["sources"], scope
        assert scope[0]["embedder"], scope
        # 重建（清空作用域）：破坏性管理动作 —— 删集合 + 返回清掉的分块数
        reset = c.delete("/api/knowledge/health_reports")
        assert reset.status_code == 200, reset.text
        assert reset.json()["removed_chunks"] >= 1
        assert c.get("/api/knowledge").json() == []

    @check("检索延迟细分：P50/P95/P99 按阶段（/api/rag/metrics）")
    def _rag_metrics() -> None:
        body = c.get("/api/rag/metrics").json()
        assert "samples" in body and body["embedder"], body
        assert isinstance(body["rerank_enabled"], bool), body
        stages = {"embed_ms", "vector_ms", "rerank_ms", "total_ms"}
        for label in ("p50", "p95", "p99"):
            assert set(body[label]) == stages, body

    @check("数据管理：列表 / 补录 / 修正指标 / 删除指标与报告")
    def _records() -> None:
        # /api/records 是分页响应（items/total/limit/offset）
        recs = c.get("/api/records").json()["items"]  # 数据在起服务前已注入
        assert recs and recs[0]["indices"], recs
        idx = recs[0]["indices"][0]
        patched = c.patch(
            f"/api/records/index/{idx['index_id']}", json={"index_value": 6.5, "is_verified": True}
        )
        assert patched.status_code == 200, patched.text
        assert float(patched.json()["index_value"]) == 6.5 and patched.json()["is_verified"] == 1
        assert c.delete(f"/api/records/index/{idx['index_id']}").status_code == 204
        for r in c.get("/api/records").json()["items"]:
            assert c.delete(f"/api/records/report/{r['report_id']}").status_code == 204
        assert c.get("/api/records").json()["items"] == []
        # 手动补录（最小可用）：类型 + 检查时间 + 一行指标
        created = c.post(
            "/api/records/report",
            json={
                "report_type": "腹部超声",
                "check_time": "2026-03-12",
                "indices": [{"index_name": "结石直径", "index_value": 6.1, "unit": "mm"}],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["report_type"] == "腹部超声"
        assert not created.json()["indices"][0]["is_verified"], "手填 ≠ 已核实"
        # 输入错误翻译成 400，而不是 500
        bad = c.post(
            "/api/records/report", json={"report_type": "t", "check_time": "2026-03-12"}
        )
        assert bad.status_code == 400, bad.text

    @check("审计：数据变更与模型切换都留痕")
    def _audit() -> None:
        actions = {a["action"] for a in c.get("/api/audit?limit=200").json()}
        assert {
            "update_index",
            "delete_report",
            "create_report",
            "reset_knowledge_scope",
            "set_session_model",
        } <= actions, actions

    @check("模型设置：后端 CRUD + 回退链 + api_key 只写不回读 + 热重建")
    def _settings() -> None:
        body = {
            "default": "cloud-a",
            "backends": [
                {
                    "name": "cloud-a",
                    "provider": "openai",
                    "base_url": "https://x/v1",
                    "model": "m-a",
                    "api_key": "sk-demo",
                },
                {
                    "name": "cloud-b",
                    "provider": "openai",
                    "base_url": "https://y/v1",
                    "model": "m-b",
                    "api_key": "sk-demo-b",
                },
            ],
            "fallbacks": ["cloud-b"],
        }
        put = c.put("/api/settings/models", json=body)
        assert put.status_code == 200, put.text
        got = c.get("/api/settings/models").json()
        assert got["default"] == "cloud-a" and got["fallbacks"] == ["cloud-b"], got
        backend = _flat_models(got)
        assert backend["cloud-a"]["has_key"] is True
        assert "api_key" not in backend["cloud-a"]  # 只写不回读
        # 缺凭据的 openai 后端必须被拒（否则会存进一个"重建时才炸"的配置）。
        # 注意：已存 key 的后端再次提交时不带 key = "保留"，是合法的，故用新名字测。
        keyless = c.put(
            "/api/settings/models",
            json={
                "default": "cloud-a",
                "backends": [
                    {"name": "cloud-a", "provider": "openai", "model": "m-a"},  # 保留已存 key
                    {"name": "cloud-c", "provider": "openai", "model": "m-c"},  # 无 key → 拒绝
                ],
            },
        )
        assert keyless.status_code == 400 and "api_key" in keyless.json()["detail"], keyless.text
        # 同上：已存 key 的后端不带 key 提交应通过（保留语义）。
        # **必须带上它的 base_url**：key 挂在 (供应商, 端点) 那个组上，不是挂在名字上 ——
        # 不带端点的那一行属于"该供应商的默认端点"，那个组确实没有 key（真实 UI 从来
        # 不会发这种形状，它把组的 base_url 一起摊平回去）。
        keep = c.put(
            "/api/settings/models",
            json={
                "default": "cloud-a",
                "backends": [
                    {
                        "name": "cloud-a",
                        "provider": "openai",
                        "base_url": "https://x/v1",
                        "model": "m-a",
                    }
                ],
            },
        )
        assert keep.status_code == 200
        assert _flat_models(keep.json())["cloud-a"]["has_key"] is True, keep.text
        assert (
            c.put(
                "/api/settings/models",
                json={
                    "default": "ghost",
                    "backends": [
                        {
                            "name": "cloud-a",
                            "provider": "openai",
                            "model": "m",
                            "api_key": "sk-demo",
                        }
                    ],
                },
            ).status_code
            == 400
        )
        s = c.post("/api/session", json={}).json()
        after = c.post("/api/chat", json={"thread_id": s["thread_id"], "message": "热重建后再问"})
        assert after.status_code == 200, after.status_code


def console_ui_smoke() -> None:
    """用本机 Chrome/Edge 真跑一遍界面交互（scripts/ui_smoke.js）；由 main() 在末项调用。

    **它自己起一个后端、打的是库副本**（2026-09-25 改的，之前默认打 127.0.0.1:8000）。
    原因不是洁癖：那一轮 full 门禁跑完，用户真库里多了三条标题为
    "用一句话解释：为什么冬天白天比夏天短？" 的会话 —— 那是 `ui_smoke.js` 的固定问句。
    冒烟写进真实数据，违反的是这个项目自己那条不变式（实验脚本只走副本），
    而且它安静地改动了"她记得什么"：下一次对话她会引用这些从没发生过的提问。

    为什么放在最后且允许跳过：它依赖本机浏览器（node + playwright-core + Chrome/Edge），
    缺任何一样就打印跳过说明并计为通过 —— 环境差异不该把冒烟变红，但**跑到了就必须全绿**。

    `SMOKE_SKIP_UI=1`：本地快速迭代的显式逃生门（UI 段约占整套冒烟一半时长）。
    与"缺依赖"不同，这是**主动选择不跑**，所以跳过说明里必须带上原因，防止误读成全绿。
    """
    if os.environ.get("SMOKE_SKIP_UI"):
        print("（跳过：SMOKE_SKIP_UI=1，本轮未跑真机 UI 冒烟——提交/发布前请跑一次完整冒烟）")
        return
    node = shutil.which("node")
    if not node:
        print("（跳过：未找到 node，无法跑真机 UI 冒烟）")
        return
    script = ROOT / "scripts" / "ui_smoke.js"
    if not script.exists():
        raise AssertionError("缺少 scripts/ui_smoke.js")

    import socket  # noqa: PLC0415

    import scratch_db  # noqa: PLC0415

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    copy = Path(tempfile.mkdtemp(prefix="ui_smoke_")) / "app.db"
    scratch_db.copy_of_live_db(copy)
    env = {
        **os.environ,
        "SQLITE_PATH": str(copy),
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",  # 冒烟不该顺手改她的记忆
        "PYTHONIOENCODING": "utf-8",
    }
    server = subprocess.Popen(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "run_api.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_for_health(base, server)
        proc = subprocess.run(  # noqa: S603
            [node, str(script), base],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(ROOT),
            encoding="utf-8",
            errors="replace",
        )
    finally:
        server.terminate()
        with contextlib.suppress(Exception):
            server.wait(timeout=15)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.strip())
    print(f"（UI 冒烟跑在副本上：{copy}，端口 {port}；真库一行未动）")
    if proc.returncode != 0:
        raise AssertionError("真机 UI 冒烟存在失败项（见上）")


def _wait_for_health(base: str, server: subprocess.Popen) -> None:
    """等自己起的那个后端就绪；它半路死了就直接失败，别把"没起来"演成"界面坏了"。"""
    deadline = time.time() + 90
    while time.time() < deadline:
        if server.poll() is not None:
            raise AssertionError(f"冒烟用的后端起不来（退出码 {server.returncode}）")
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as r:  # noqa: S310
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001 - 还没就绪，继续等
            time.sleep(1.0)
    raise AssertionError("冒烟用的后端 90s 内没就绪")


def report() -> int:
    print("\n=== 全功能冒烟 ===")
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}" + (f"  → {detail}" if detail else ""))
    print(f"\n通过 {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
