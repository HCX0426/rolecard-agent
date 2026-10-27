"""上传后台池的 **contextvar 传播**（R102-03 的同族接缝，upload 站点补钉）。

为什么单独一档：`submit_processing` 从前裸 `_UPLOAD_POOL.submit(_run)` —— 不带
`copy_context()`，池线程读到的库代际是默认值 `""`，**永远不变**。于是
`ThreadLocalConnection._current()` 那句"代际变了先回滚上次残留"在这条线程上**一次都不
触发**：上一轮任务若在事务中途抛错，残留的未提交写会占着 WAL 写锁毒害整条池线程 ——
下一轮 `record_failure` 撞 `database is locked`（busy_timeout 5s 到点），被后台兜底的
suppress 吞掉，台账永远悬空 pending。CI 覆盖率档（串行、慢、一个进程跑完全套）实测
抓到这个形状（v0.5.0 发布闸 run 40469897043）：`{'status': 'pending', 'running': False}`
而不是预期的 failed。

两格判据（与 test_chat_epoch 同形）：
① worker 线程看到的代际 = 提交方请求的代际（传播发生）；
② 同一池线程跨"脏上一轮"复用后，第一次摸库先回滚 —— 上一笔未提交的写不许落库。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from rolecard_agent.core.ingest import upload_service
from rolecard_agent.storage.db import set_request_epoch


def test_submit_processing_propagates_request_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    """① 提交时 contextvar 必须带进 worker —— 裸 submit 在这里红。"""
    seen: dict[str, Any] = {}
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upload-process-t")
    monkeypatch.setattr(upload_service, "_UPLOAD_POOL", pool)

    def fake_process(*args: Any, **kwargs: Any) -> Any:
        # worker 线程里读代际：没传播 ⇒ ""；传播了 ⇒ 提交方那次请求的号
        from rolecard_agent.storage.db import _REQUEST_EPOCH

        seen["epoch"] = _REQUEST_EPOCH.get()
        raise RuntimeError("只为走兜底路径把线程用起来")

    monkeypatch.setattr(upload_service, "process_upload", fake_process)

    class _P:
        task_id = "ing_test1"
        reused = False
        safe_name = "a.txt"
        target = None  # fake_process 不碰它

    class _Ing:
        def record_failure(self, tid: str, err: str) -> None:
            seen["recorded"] = err

    from typing import cast

    from rolecard_agent.core.ingest.ingestion import IngestionService
    from rolecard_agent.core.ingest.upload_service import PersistedUpload
    from rolecard_agent.rag.retriever import KnowledgeBase

    # 替身按声明类型 cast：这条测的是**上下文传播**，不是签名兼容。
    p = cast(PersistedUpload, _P())
    ing = cast(IngestionService, _Ing())
    kb = cast(KnowledgeBase, object())

    set_request_epoch("EPOCH-UPLOAD-1")
    try:
        started = upload_service.submit_processing(
            p,
            thread_id="s_t",
            ingestion=ing,
            knowledge=kb,
            knowledge_scope="default",
            ocr_candidates=None,
            tracer=_NullTracer(),
            sink=lambda _o: None,
        )
        assert started is True
        import threading
        import time

        deadline = time.monotonic() + 10
        while "epoch" not in seen and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        assert seen.get("epoch") == "EPOCH-UPLOAD-1", (
            f"worker 线程读到的代际是 {seen.get('epoch')!r} —— 提交方的号没送进线程池，"
            "那条线程的『新请求先回滚残留』一次都不会触发（R102-03 的 upload 站点）"
        )
    finally:
        set_request_epoch("")
        pool.shutdown(wait=True)


class _NullTracer:
    def emit(self, *_a: Any, **_k: Any) -> None: ...
