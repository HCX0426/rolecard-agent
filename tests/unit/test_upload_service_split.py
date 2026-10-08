"""P3-3 拆分的服务级判据：同步组合还活着、后台去重真的去重。

两条各自守一个"这次改动可能悄悄弄丢的东西"：

  1. `ingest_upload`（persist+process 的**同步组合**）在 HTTP 路由改走后台之后没人调了
     —— 没有这条用例，它就是死代码上的一段 docstring；有这条，它是被测着的
     非 HTTP 宿主契约（桌宠壳/探针要的"一次调用拿结果"）。**同时钉住"重活只有一份
     实现"**：同步与后台走的是同一个 `process_upload`，组合断言失败 = 两条路开始分叉。
  2. `submit_processing` 的"已有一个在跑就不再起"：同一任务被起两次 = 同一份文件被
     两路并发解析/入索引（幂等键救得回写库，救不回白烧的 OCR 与交错的状态推进）。
"""

from __future__ import annotations

import io
import pathlib
import sys
from concurrent.futures import Future

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import rolecard_agent.core.ingest.upload_service as us  # noqa: E402
from rolecard_agent.core.ingest.ingestion import (  # noqa: E402
    INGESTION_INDEXED,
    IngestionService,
)
from rolecard_agent.storage.db import bootstrap, connect  # noqa: E402


@pytest.fixture
def ing(tmp_path: pathlib.Path) -> IngestionService:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=["health"])
    c.executescript(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name)"
        "  VALUES ('u1', 't1', 'u1');"
    )
    c.commit()
    return IngestionService(c)


class _FakeKnowledge:
    """只实现被 process_upload 调用的那一个方法（测的是编排与状态机，不是嵌入）。"""

    def index(self, scope: str, task_id: str, text: str, *, source_name: str | None = None) -> int:
        return max(1, len(text) // 40)


class _Tracer:
    def emit(self, event: object) -> None:
        pass


def test_ingest_upload同步组合一路推到_indexed(
    tmp_path: pathlib.Path, ing: IngestionService
) -> None:
    """非 HTTP 门面（persist+process 同步组合）的端到端形状：一次调用拿终局。"""
    outcome = us.ingest_upload(
        reader=io.BytesIO("随访须知：每半年复查一次。".encode()),
        filename="须知.txt",
        thread_id="t_demo",
        user_id="u1",
        upload_dir=tmp_path / "uploads",
        ingestion=ing,
        knowledge=_FakeKnowledge(),  # type: ignore[arg-type]
        knowledge_scope="health_reports",
        ocr_candidates=None,
        tracer=_Tracer(),
    )
    assert outcome.status == "indexed", outcome
    assert "已建立检索索引" in outcome.note
    row = ing.get(outcome.task_id)
    assert row["status"] == INGESTION_INDEXED  # 台账与响应说的是同一件事

    # 终局复用：同字节再走一遍门面，不重跑、答终局。
    again = us.ingest_upload(
        reader=io.BytesIO("随访须知：每半年复查一次。".encode()),
        filename="须知.txt",
        thread_id="t_demo",
        user_id="u1",
        upload_dir=tmp_path / "uploads",
        ingestion=ing,
        knowledge=_FakeKnowledge(),  # type: ignore[arg-type]
        knowledge_scope="health_reports",
        ocr_candidates=None,
        tracer=_Tracer(),
    )
    assert again.reused is True and again.task_id == outcome.task_id
    assert again.status == "indexed"
    assert "无需重复处理" in again.note


def _pending_task(task_id: str = "ing_unit") -> us.PersistedUpload:
    return us.PersistedUpload(
        task_id=task_id,
        reused=False,
        target=pathlib.Path("x.txt"),
        safe_name="x.txt",
        status="pending",
        parseable=True,
    )


def test_submit_processing不给同一任务起第二个跑者() -> None:
    """running 的两用共一笔账：进度端点读它，提交侧也读它 —— 双跑必须被挡住。"""
    stuck: Future[None] = Future()  # 永不完成 ⇒ not done() ⇒ "正在跑"
    with us._RUNNING_GUARD:
        us._RUNNING["ing_unit"] = stuck
    try:
        assert us.is_running("ing_unit") is True
        assert (
            us.submit_processing(
                _pending_task(),
                thread_id="t",
                ingestion=None,  # type: ignore[arg-type]
                knowledge=None,  # type: ignore[arg-type]
                knowledge_scope="s",
                ocr_candidates=None,
                tracer=_Tracer(),
                sink=lambda o: None,
            )
            is False
        ), "同一任务已在跑，必须拒绝起第二个"
        stuck.set_result(None)
        assert us.is_running("ing_unit") is False  # 完成后 running 如实归 False
    finally:
        with us._RUNNING_GUARD:
            us._RUNNING.pop("ing_unit", None)


def test_is_running对陌生任务说假() -> None:
    assert us.is_running("ing_never_submitted") is False
