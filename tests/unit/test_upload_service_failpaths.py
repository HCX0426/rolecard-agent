"""upload_service 的失败路径用例（0.7.0 定版时该文件滑到地板 85% 之下，补的是**判据**）。

每段都对应模块 docstring 里"不能动的判据"或审查报告点名过的形状：

  * 超限与消毒：`UploadRejected` 在落台账**之前**抛出，spill 不留尸体（A4/M5）；
  * 并发竞态：输掉 `create` 约束回读的那一路必须删掉自己的孤儿文件、改用赢家（P3-3）；
  * `OcrUnavailable` ≠ `ParseError`：前者答 200 + `parsed: false`、图片保持 pending
    可后补；后者记 failed 并抛 `UploadUnreadable`（判据 3 的两半）；
  * 解析副本写失败：不再静默 —— 轨迹里必须查得到"为什么抽取又要跑 OCR"（E2）；
  * 嵌入炸：管理员可修复的状态，`record_failure` 后如实上抛（P0 同族）；
  * 后台兜底的兜底：`record_failure` 自己失败也要留 trace，不许无声悬空 pending
    （2026-10-26 CI 现场：run 40379234206 悬空 34 秒、轨迹一行都没有）；
  * sink 炸（投回会话失败）：产物已入库是事实，轨迹记下投送失败，不吞也不 500。

单元级喂 Fake，不碰 HTTP 路由（路由层的 400/500 形状自有 test_uploads_api 钉）。
"""

from __future__ import annotations

import io
import pathlib
from concurrent.futures import Future

import pytest

import rolecard_agent.core.ingest.upload_service as us
from rolecard_agent.rag.parser import OcrUnavailable, ParseError
from rolecard_agent.rag.retriever import EmbedError


class FakeTracer:
    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event: object) -> None:
        self.events.append(str(getattr(event, "event", event)))


class FakeIng:
    """只实现 upload_service 用到的那几个方法（测编排，不测状态机的合法性）。"""

    def __init__(self, status: str = "pending", source_file: str | None = None) -> None:
        self.status = status
        self.source_file = source_file
        self.failures: list[str] = []
        self.advances: list[str] = []
        self.break_record = False

    def get(self, task_id: str) -> dict[str, object]:
        return {"status": self.status, "source_file": self.source_file}

    def record_failure(self, task_id: str, error: str) -> None:
        if self.break_record:
            raise RuntimeError("db is locked")
        self.failures.append(error)

    def advance(self, task_id: str, status: str) -> None:
        self.advances.append(status)

    def relink_source(self, task_id: str, source_file: str) -> None:
        pass

    def find_by_hash(self, user_id: str, file_hash: str) -> dict[str, object] | None:
        return None

    def create(self, *, user_id: str, source_file: str, file_hash: str) -> str:
        return "ing_new"


class FakeKnowledge:
    def __init__(self, chunks: int = 2, boom: Exception | None = None) -> None:
        self.chunks = chunks
        self.boom = boom

    def index(self, scope: str, task_id: str, text: str, *, source_name: str | None = None) -> int:
        if self.boom is not None:
            raise self.boom
        return self.chunks


def _p(tmp_path: pathlib.Path, name: str = "x.txt", **kw: object) -> us.PersistedUpload:
    return us.PersistedUpload(
        task_id=str(kw.get("task_id", "ing_fp")),
        reused=bool(kw.get("reused", False)),
        target=tmp_path / name,
        safe_name=name,
        status=str(kw.get("status", "pending")),
        parseable=bool(kw.get("parseable", True)),
    )


def _process(p: us.PersistedUpload, ing: FakeIng, tr: FakeTracer | None = None):
    return us.process_upload(
        p,
        thread_id="t",
        ingestion=ing,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        knowledge_scope="s",
        ocr_candidates=None,
        tracer=tr or FakeTracer(),
    )


# -- 纯函数三小件 ------------------------------------------------------------------------


def test_消毒兜底与来源标签() -> None:
    assert us.sanitize_filename("   ") == "report.bin"  # 全被剥掉 → 兜底名
    assert us.source_kind(pathlib.Path("a.png")) == "ocr"  # 图片后缀走 OCR 标签
    assert us.source_kind(pathlib.Path("a.md")) == "parsed"


def test_响应只在早退路带_parsed_键() -> None:
    plain = us.UploadOutcome(task_id="t", reused=False, file="f", status="indexed", note="n")
    assert "parsed" not in plain.response()  # 其余路径逐键与搬家前一致
    early = us.UploadOutcome(
        task_id="t", reused=False, file="f", status="pending", note="n", parsed=False
    )
    assert early.response()["parsed"] is False


def test_现场重解析把读不出如实成_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path: object, **_kw: object) -> str:
        raise ParseError("坏了")

    monkeypatch.setattr(us, "parse_document", boom)
    assert us.read_source_text(pathlib.Path("a.pdf")) is None
    monkeypatch.setattr(us, "parse_document", lambda _p, **_kw: "文本")
    assert us.read_source_text(pathlib.Path("a.pdf")) == "文本"


# -- persist_upload：拒绝与竞态 ----------------------------------------------------------


def test_超限当场拒绝且不留spill(tmp_path: pathlib.Path) -> None:
    updir = tmp_path / "uploads"
    with pytest.raises(us.UploadRejected, match="上限"):
        us.persist_upload(
            reader=io.BytesIO(b"0" * 64),
            filename="big.txt",
            user_id="u1",
            upload_dir=updir,
            ingestion=FakeIng(),  # type: ignore[arg-type]
            max_bytes=8,
        )
    assert not list(updir.glob(".*.part"))  # except 分支收掉半截文件


def test_并发输家丢弃孤儿改用赢家(tmp_path: pathlib.Path) -> None:
    """`create` 约束回读返回别人的行 ⇒ 自己刚落的盘是孤儿，删掉、统一用赢家。"""
    updir = tmp_path / "uploads"
    updir.mkdir()
    winner = updir / "aa11_winner.txt"
    winner.write_text("dup", encoding="utf-8")

    class LoserIng(FakeIng):
        def create(self, *, user_id: str, source_file: str, file_hash: str) -> str:
            return "ing_winner"  # 另一路先立了台账

        def get(self, task_id: str) -> dict[str, object]:
            return {"status": "pending", "source_file": str(winner)}

    p = us.persist_upload(
        reader=io.BytesIO(b"dup"),
        filename="dup.txt",
        user_id="u1",
        upload_dir=updir,
        ingestion=LoserIng(),  # type: ignore[arg-type]
    )
    assert p.reused is True and p.target == winner
    assert not list(updir.glob("*_dup.txt"))  # 自己那份孤儿已删净


# -- process_upload：三条失败路 ------------------------------------------------------------


def test_图片无ocr答pending并可后补(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_path: object, **_kw: object) -> str:
        raise OcrUnavailable("没配")

    monkeypatch.setattr(us, "parse_document", boom)
    ing = FakeIng(status="pending")
    out = _process(_p(tmp_path, "shot.png"), ing)
    assert out.parsed is False and out.status == "pending"
    assert "OCR" in out.note and ing.advances == [] and ing.failures == []


def test_解析硬失败记账并上抛(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path: object, **_kw: object) -> str:
        raise ParseError("PDF 坏了")

    monkeypatch.setattr(us, "parse_document", boom)
    ing = FakeIng()
    with pytest.raises(us.UploadUnreadable, match="PDF 坏了"):
        _process(_p(tmp_path), ing)
    assert ing.failures == ["PDF 坏了"]  # failed 不落账就永远悬在 pending


def test_解析副本写失败留轨迹索引照旧(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(us, "parse_document", lambda _p, **_k: "有内容的正文")
    blocked = tmp_path / "blocked_dir"  # write_text 撞上目录 ⇒ OSError
    blocked.mkdir()
    monkeypatch.setattr(us, "parsed_text_path", lambda _t: blocked)
    tr = FakeTracer()
    out = _process(_p(tmp_path), FakeIng(), tr)
    assert "已建立检索索引" in out.note and tr.events == ["parsed_text_write_failed"]


def test_嵌入炸了记失败并如实报(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us, "parse_document", lambda _p, **_k: "正文")
    ing = FakeIng()
    with pytest.raises(us.UploadUnreadable, match="402"):
        us.process_upload(
            _p(tmp_path),
            thread_id="t",
            ingestion=ing,  # type: ignore[arg-type]
            knowledge=FakeKnowledge(boom=EmbedError("额度不足 402")),  # type: ignore[arg-type]
            knowledge_scope="s",
            ocr_candidates=None,
            tracer=FakeTracer(),
        )
    assert ing.failures == ["额度不足 402"]


def test_解析出空文本只到_parsed(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us, "parse_document", lambda _p, **_k: "   ")
    ing = FakeIng(status="pending")
    out = _process(_p(tmp_path), ing)
    assert "扫描件" in out.note and ing.advances == ["parsed"]
    # 复用路：台账已 indexed 就不再推进状态机
    ing2 = FakeIng(status="indexed")
    _process(_p(tmp_path), ing2)
    assert ing2.advances == []


# -- 后台兜底的兜底 ------------------------------------------------------------------------


@pytest.fixture
def inline_pool(monkeypatch: pytest.MonkeyPatch):
    """把 submit_with_context 换成就地执行：后台线程的异常路径在同步测试里可断言。"""

    def fake(_pool: object, fn: object) -> Future:
        fut: Future = Future()
        fut.set_result(None)
        # 调用方（submit_*）此刻正握着 `_RUNNING_GUARD`，而 `_run` 的 finally 也要它 ——
        # 锁不可重入，就地跑之前必须先交还、跑完再拿回，进出 with 的配对原样成立。
        us._RUNNING_GUARD.release()
        try:
            fn()  # type: ignore[operator]
        finally:
            us._RUNNING_GUARD.acquire()
        return fut

    monkeypatch.setattr(us, "submit_with_context", fake)
    yield
    with us._RUNNING_GUARD:
        us._RUNNING.clear()


def test_兜底记账也失败时必须留trace(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, inline_pool: None
) -> None:
    def boom(*_a: object, **_kw: object) -> us.UploadOutcome:
        raise ValueError("编程错")

    monkeypatch.setattr(us, "process_upload", boom)
    ing = FakeIng()
    ing.break_record = True
    tr = FakeTracer()
    assert us.submit_processing(
        _p(tmp_path),
        thread_id="t",
        ingestion=ing,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        knowledge_scope="s",
        ocr_candidates=None,
        tracer=tr,
        sink=lambda _o: None,
    )
    assert tr.events == ["upload_task_stuck_pending"]  # 无声悬空是最坏的形状


def test_sink炸轨迹留痕但产物算成(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, inline_pool: None
) -> None:
    monkeypatch.setattr(
        us,
        "process_upload",
        lambda *a, **kw: us.UploadOutcome(
            task_id="ing_fp", reused=False, file="x.txt", status="indexed", note="n"
        ),
    )
    tr = FakeTracer()

    def sink(_o: object) -> None:
        raise RuntimeError("写锁丢了")

    us.submit_processing(
        _p(tmp_path),
        thread_id="t",
        ingestion=FakeIng(),  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        knowledge_scope="s",
        ocr_candidates=None,
        tracer=tr,
        sink=sink,
    )
    assert tr.events == ["upload_completion_note_failed"]


# -- 知识库直导（process_scope_import） ------------------------------------------------------


def test_直导无ocr给可补话(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_p: object, **_kw: object) -> str:
        raise OcrUnavailable("没配")

    monkeypatch.setattr(us, "parse_document", boom)
    r = us.process_scope_import(
        _p(tmp_path, "a.png"),
        scope="s",
        ingestion=FakeIng(),  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        ocr_candidates=None,
        tracer=FakeTracer(),
    )
    assert r["chunks"] == 0 and "重新导入" in str(r["note"])


def test_直导空文本只到_parsed(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us, "parse_document", lambda _p, **_k: "")
    ing = FakeIng(status="pending")
    r = us.process_scope_import(
        _p(tmp_path),
        scope="s",
        ingestion=ing,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        ocr_candidates=None,
        tracer=FakeTracer(),
    )
    assert r["status"] == "parsed" and ing.advances == ["parsed"]


def test_直导写副本失败与嵌入失败(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us, "parse_document", lambda _p, **_k: "有内容" * 10)
    blocked = tmp_path / "dir"
    blocked.mkdir()
    monkeypatch.setattr(us, "parsed_text_path", lambda _t: blocked)
    tr = FakeTracer()
    r = us.process_scope_import(
        _p(tmp_path),
        scope="s",
        ingestion=FakeIng(),  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        ocr_candidates=None,
        tracer=tr,
    )
    assert r["status"] == "indexed" and tr.events == ["parsed_text_write_failed"]

    ing = FakeIng()
    with pytest.raises(us.UploadUnreadable, match="维度"):
        us.process_scope_import(
            _p(tmp_path, task_id="ing_boom"),
            scope="s",
            ingestion=ing,  # type: ignore[arg-type]
            knowledge=FakeKnowledge(boom=EmbedError("维度不符")),  # type: ignore[arg-type]
            ocr_candidates=None,
            tracer=FakeTracer(),
        )
    assert ing.failures == ["维度不符"]


def test_直导后台吞可读失败与兜底记账(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, inline_pool: None
) -> None:
    def unreadable(*_a: object, **_kw: object) -> dict:
        raise us.UploadUnreadable("读不出")

    monkeypatch.setattr(us, "process_scope_import", unreadable)
    assert us.submit_scope_import(
        _p(tmp_path, task_id="ing_a"),
        scope="s",
        ingestion=FakeIng(),  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        ocr_candidates=None,
        tracer=FakeTracer(),
    )  # UploadUnreadable 静默 return：台账已 failed，进度端点读得到

    ing = FakeIng()

    def crash(*_a: object, **_kw: object) -> dict:
        raise RuntimeError("炸了")

    monkeypatch.setattr(us, "process_scope_import", crash)
    assert us.submit_scope_import(
        _p(tmp_path, task_id="ing_b"),
        scope="s",
        ingestion=ing,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        ocr_candidates=None,
        tracer=FakeTracer(),
    )
    assert ing.failures  # 兜底把编程错落成 failed，不留悬空
