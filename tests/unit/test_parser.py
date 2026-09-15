"""rag/parser 单测：扩展名分派、PDF 抽文本、图片 OCR 不可用降级、不支持类型报错。

OCR 子进程路径（paddle）依赖独立 venv，本环境未装，故只测"未配置 → OcrUnavailable"
的合约；真实 OCR 在 .venv-ocr 就绪后由集成验证。
"""
from __future__ import annotations

from pathlib import Path

import pytest

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


def test_image_without_ocr_raises_ocr_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OCR_PYTHON", raising=False)
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
    assert ".bin" not in PARSEABLE_EXTENSIONS
