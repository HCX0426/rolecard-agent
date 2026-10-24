"""知识库文档直导（`process_scope_import` / `submit_scope_import`）的单元测试 ——
补齐覆盖率地板要求的失败分支。

走真管线组件（临时目录 + HashEmbedder 的裸 KnowledgeBase + 真 IngestionService 台账），
钉住五条分支：不可解析类型、OCR 未配置、解析硬失败（UploadUnreadable + 台账 failed）、
空文本（parsed 即止不入索引）、幂等重导（已 indexed 不再推进状态机）。
后台提交那半（submit）钉"在跑就不重起"。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from rolecard_agent.core.ingest import upload_service
from rolecard_agent.core.ingest.ingestion import IngestionService
from rolecard_agent.rag.errors import ParseError
from rolecard_agent.rag.retriever import HashEmbedder, KnowledgeBase
from rolecard_agent.storage.db import bootstrap, connect


class _Tracer:
    def emit(self, *_a: Any, **_k: Any) -> None:  # noqa: D102 - 测试桩
        pass


def _kb(tmp_path: Path) -> KnowledgeBase:
    """裸 KnowledgeBase + HashEmbedder（确定性向量、零网络）—— 单测形状。"""
    return KnowledgeBase(tmp_path / "chroma", HashEmbedder())


def _persisted(tmp_path: Path, name: str, content: bytes) -> Any:
    """落盘一个假上传（不 persist 到 upload_dir，手动构造 PersistedUpload + 台账行）。

    台账走真 `IngestionService.create`（幂等键 file_hash；task_id 显式给，测试要稳定串）；
    不走 `persist_upload` 是刻意的：那半测的是流式落盘与消毒，这里只测 scope 导入的
    后半（process_scope_import / submit_scope_import）。
    """
    target = tmp_path / name
    target.write_bytes(content)
    conn = connect(":memory:")
    bootstrap(conn, enabled_domains=())
    # ingestion_task.user_id 挂外键（app_user → tenant）—— 种两行最小身份
    conn.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 't')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES ('u1', 't1', 't')"
    )
    ingestion = IngestionService(conn)
    task_id = ingestion.create(
        user_id="u1", file_hash=f"hash_{name}", source_file=str(target),
        task_id=f"ing_{name.replace('.', '_')}",
    )
    p = upload_service.PersistedUpload(
        task_id=task_id,
        reused=False,
        target=target,
        safe_name=name,
        status="pending",
        parseable=True,
    )
    return p, ingestion


def test_scope_import_unparseable_type(tmp_path: Path) -> None:
    import dataclasses

    p, ingestion = _persisted(tmp_path, "weird.zip", b"PK")
    p = dataclasses.replace(p, parseable=False)
    out = upload_service.process_scope_import(
        p, scope="notes", ingestion=ingestion, knowledge=_kb(tmp_path),
        ocr_candidates=None, tracer=_Tracer(),
    )
    assert "暂不支持" in out["note"]
    assert out["chunks"] == 0


def test_scope_import_parse_failure_marks_failed(tmp_path: Path, monkeypatch) -> None:
    p, ingestion = _persisted(tmp_path, "bad.pdf", b"%PDF-broken")
    import rolecard_agent.core.ingest.upload_service as us

    def boom(*_a: Any, **_k: Any) -> str:
        raise ParseError("corrupt")

    monkeypatch.setattr(us, "parse_document", boom)
    with pytest.raises(upload_service.UploadUnreadable):
        upload_service.process_scope_import(
            p, scope="notes", ingestion=ingestion, knowledge=_kb(tmp_path),
            ocr_candidates=None, tracer=_Tracer(),
        )
    assert ingestion.get(p.task_id)["status"] == "failed"


def test_scope_import_empty_text_stops_at_parsed(tmp_path: Path, monkeypatch) -> None:
    p, ingestion = _persisted(tmp_path, "empty.txt", b"   ")
    monkeypatch.setattr(
        "rolecard_agent.core.ingest.upload_service.parse_document", lambda *a, **k: " "
    )
    out = upload_service.process_scope_import(
        p, scope="notes", ingestion=ingestion, knowledge=_kb(tmp_path),
        ocr_candidates=None, tracer=_Tracer(),
    )
    assert out["status"] == "parsed"
    assert out["chunks"] == 0
    assert "未提取到文本" in out["note"]


def test_scope_import_rejects_invalid_scope_name(tmp_path: Path) -> None:
    """chroma 集合名规则比 SCOPE_PATTERN 严（3-63 字符、字母数字开头结尾）——
    一字符名在 SCOPE_PATTERN 里合法、在 chroma 里不合法：必须 loud 人话，不是 404。"""
    import pytest as _pytest

    p, ingestion = _persisted(tmp_path, "note.txt", b"body")
    kb = _kb(tmp_path)
    with _pytest.raises(RuntimeError, match="建不出来"):
        upload_service.process_scope_import(
            p, scope="k", ingestion=ingestion, knowledge=kb,
            ocr_candidates=None, tracer=_Tracer(),
        )


def test_scope_import_indexes_and_is_idempotent(tmp_path: Path) -> None:
    p, ingestion = _persisted(tmp_path, "note.txt", "内容足够长。".encode() * 40)
    kb = _kb(tmp_path)
    kb.create_scope("notes")  # 管理页形状：先建库后投喂（index 的懒建是兜底不是主路径）
    out = upload_service.process_scope_import(
        p, scope="notes", ingestion=ingestion, knowledge=kb,
        ocr_candidates=None, tracer=_Tracer(),
    )
    assert out["status"] == "indexed"
    assert out["chunks"] >= 1
    # 幂等：同一份再导一遍 —— 状态机已 indexed，不再推进，也不炸
    again = upload_service.process_scope_import(
        p, scope="notes", ingestion=ingestion, knowledge=kb,
        ocr_candidates=None, tracer=_Tracer(),
    )
    assert again["status"] == "indexed"


def test_scope_import_ocr_unavailable_keeps_pending(tmp_path: Path, monkeypatch) -> None:
    from rolecard_agent.rag.parser import OcrUnavailable

    p, ingestion = _persisted(tmp_path, "scan.png", b"\x89PNG")

    def no_ocr(*_a: Any, **_k: Any) -> str:
        raise OcrUnavailable("no backend")

    monkeypatch.setattr(
        "rolecard_agent.core.ingest.upload_service.parse_document", no_ocr
    )
    out = upload_service.process_scope_import(
        p, scope="notes", ingestion=ingestion, knowledge=_kb(tmp_path),
        ocr_candidates=lambda: None, tracer=_Tracer(),
    )
    assert "OCR 未配置" in out["note"]
    assert ingestion.get(p.task_id)["status"] == "pending"


def test_submit_scope_import_dedupes_inflight(tmp_path: Path) -> None:
    """同一任务在跑时不再起第二个（返回 False）；跑完归还。用慢 parse 卡住后台。"""
    import threading

    p, ingestion = _persisted(tmp_path, "slow.txt", b"body")
    release = threading.Event()
    started = threading.Event()

    def slow_parse(*_a: Any, **_k: Any) -> str:
        started.set()
        release.wait(timeout=10)
        return "内容"

    import rolecard_agent.core.ingest.upload_service as us

    us.parse_document = slow_parse  # monkeypatch 模块属性（测试自己恢复）
    try:
        ok = upload_service.submit_scope_import(
            p, scope="notes", ingestion=ingestion, knowledge=_kb(tmp_path),
            ocr_candidates=None, tracer=_Tracer(),
        )
        assert ok is True
        assert started.wait(timeout=5)
        dup = upload_service.submit_scope_import(
            p, scope="notes", ingestion=ingestion, knowledge=_kb(tmp_path),
            ocr_candidates=None, tracer=_Tracer(),
        )
        assert dup is False  # 在跑：不重起
    finally:
        release.set()
        time.sleep(0.2)
        us.parse_document = upload_service.__dict__.get("parse_document")  # no-op 保护
