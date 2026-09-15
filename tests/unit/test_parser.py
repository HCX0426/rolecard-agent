"""rag/parser 单测：扩展名分派、PDF 抽文本、图片 OCR 后端选择与降级、不支持类型报错。

OCR 子进程路径（paddle）依赖独立 venv，本环境可能未装，故这里只测**合约**：
- 后端不可用（无独立 venv / 未配 key）→ OcrUnavailable 降级；
- 选择器策略：Paddle 优先，云端 key 兜底；
真实 OCR 在 .venv-ocr 就绪后由集成验证（scripts/smoke_check.py / 手工上传图片）。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from rolecard_agent.config import Settings
from rolecard_agent.rag.ocr import (
    CloudApiBackend,
    LocalPaddleBackend,
    select_ocr_backend,
)
from rolecard_agent.rag.parser import (
    PARSEABLE_EXTENSIONS,
    OcrUnavailable,
    ParseError,
    parse_document,
)


def _make_pdf_bytes(text: str) -> bytes:
    """最小合法 1 页 PDF（含文本）。"""
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


def _zip_bytes(files: dict[str, str]) -> bytes:
    """把 {part 路径: XML 文本} 打包成一个 zip（OOXML 的最小载体）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>' for t in paragraphs
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    return _zip_bytes({"word/document.xml": doc})


def _make_pptx_bytes(slides: list[list[str]]) -> bytes:
    files: dict[str, str] = {}
    for i, texts in enumerate(slides, start=1):
        paras = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in texts)
        files[f"ppt/slides/slide{i}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            f"<p:cSld><p:spTree><p:sp><p:txBody>{paras}</p:txBody></p:sp></p:spTree></p:cSld>"
            "</p:sld>"
        )
    return _zip_bytes(files)


def _make_xlsx_bytes(strings: list[str]) -> bytes:
    sis = "".join(f"<si><t>{s}</t></si>" for s in strings)
    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{sis}</sst>"
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData/></worksheet>"
    )
    return _zip_bytes({"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet})


def test_text_extensions_read_directly(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello 世界", encoding="utf-8")
    (tmp_path / "b.md").write_text("# title", encoding="utf-8")
    assert parse_document(tmp_path / "a.txt") == "hello 世界"
    assert parse_document(tmp_path / "b.md") == "# title"


def test_pdf_extraction(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(_make_pdf_bytes("Hello PDF World 123"))
    out = parse_document(f)
    assert "Hello PDF World 123" in out


def test_pdf_empty_text_is_empty_string(tmp_path: Path) -> None:
    # 扫描件 / 无文本层：解析不报错，返回空串（调用方据其决定不索引）。
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF")  # 极简、无文本对象
    # pypdf 对残缺 PDF 可能抛错；允许 ParseError 或返回空串，关键是"不崩成 500 的意外异常"
    try:
        text = parse_document(f)
        assert text == ""
    except ParseError:
        pass


def test_unsupported_extension_raises_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    with pytest.raises(ParseError):
        parse_document(f)


def test_image_without_configured_backend_raises_ocr_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置任何 OCR 后端（无独立 venv / 无 OCR_PYTHON / 无云端 key）→ OcrUnavailable 降级。

    强制"自动发现不到默认解释器"，使断言不依赖本机是否装了 .venv-ocr。
    """
    monkeypatch.delenv("OCR_PYTHON", raising=False)
    # 注意：ocr.py 以 `from ... import _default_ocr_python` 绑定的是自己的名字，
    # 必须 patch ocr 模块里的引用，patch parser 里的原函数不会生效。
    monkeypatch.setattr("rolecard_agent.rag.ocr._default_ocr_python", lambda: None)
    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")  # 假 PNG 头，仅用于触发扩展名分派
    with pytest.raises(OcrUnavailable):
        parse_document(img)


def test_image_with_missing_ocr_exe_raises_ocr_unavailable(tmp_path: Path) -> None:
    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(OcrUnavailable):
        parse_document(img, ocr_python="C:/no/such/python.exe")


def test_parseable_extensions_constant() -> None:
    assert ".txt" in PARSEABLE_EXTENSIONS
    assert ".md" in PARSEABLE_EXTENSIONS
    assert ".pdf" in PARSEABLE_EXTENSIONS
    assert ".png" in PARSEABLE_EXTENSIONS
    assert {".docx", ".pptx", ".xlsx"} <= PARSEABLE_EXTENSIONS
    assert ".bin" not in PARSEABLE_EXTENSIONS


# -- OCR 后端选择策略：Paddle 优先，云端 key 兜底 ----------------------------------------


def test_select_ocr_backend_prefers_paddle(monkeypatch: pytest.MonkeyPatch) -> None:
    """两者都可用时，Paddle 优先（离线、数据不出本机）。"""
    monkeypatch.setattr(LocalPaddleBackend, "available", lambda self: True)
    monkeypatch.setattr(CloudApiBackend, "available", lambda self: True)
    backend = select_ocr_backend(Settings(ocr_api_key="sk-x"))
    assert isinstance(backend, LocalPaddleBackend)


def test_select_ocr_backend_falls_back_to_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paddle 不可用（未装独立 venv）→ 配了 key 则回退云端。"""
    monkeypatch.setattr(LocalPaddleBackend, "available", lambda self: False)
    monkeypatch.setattr(CloudApiBackend, "available", lambda self: True)
    backend = select_ocr_backend(Settings(ocr_api_key="sk-x"))
    assert isinstance(backend, CloudApiBackend)


def test_select_ocr_backend_none_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两路都不可用 → None（调用方降级为 OcrUnavailable，不假装已读）。"""
    monkeypatch.setattr(LocalPaddleBackend, "available", lambda self: False)
    monkeypatch.setattr(CloudApiBackend, "available", lambda self: False)
    assert select_ocr_backend(Settings()) is None


def test_select_ocr_backend_explicit_paddle_ignores_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 ocr_backend=paddle 时不回退云端 —— 明确要离线就不外发图片。"""
    monkeypatch.setattr(LocalPaddleBackend, "available", lambda self: False)
    monkeypatch.setattr(CloudApiBackend, "available", lambda self: True)
    assert select_ocr_backend(Settings(ocr_backend="paddle", ocr_api_key="sk-x")) is None


# -- Office OOXML：docx / pptx / xlsx（zip + XML，零依赖） --------------------------------


def test_docx_extraction(tmp_path: Path) -> None:
    f = tmp_path / "报告.docx"
    f.write_bytes(_make_docx_bytes(["随访须知", "每半年复查一次超声。"]))
    out = parse_document(f)
    assert "随访须知" in out
    assert "每半年复查一次超声。" in out
    assert "\n" in out  # 段落被保留为换行


def test_pptx_extraction(tmp_path: Path) -> None:
    f = tmp_path / "幻灯片.pptx"
    f.write_bytes(_make_pptx_bytes([["第一页标题"], ["第二页要点", "复查频率"]]))
    out = parse_document(f)
    assert "第一页标题" in out
    assert "复查频率" in out


def test_xlsx_extraction(tmp_path: Path) -> None:
    f = tmp_path / "指标.xlsx"
    f.write_bytes(_make_xlsx_bytes(["血糖", "6.1"]))
    out = parse_document(f)
    assert "血糖" in out and "6.1" in out


def test_broken_office_file_raises_parse_error(tmp_path: Path) -> None:
    """非 zip / 损坏的 .docx → ParseError（而不是 zipfile.BadZipFile 冒出来）。"""
    f = tmp_path / "坏.docx"
    f.write_bytes(b"not a zip at all")
    with pytest.raises(ParseError):
        parse_document(f)


def test_docx_without_document_xml_raises(tmp_path: Path) -> None:
    f = tmp_path / "缺件.docx"
    f.write_bytes(_zip_bytes({"other.xml": "<x/>"}))
    with pytest.raises(ParseError):
        parse_document(f)
