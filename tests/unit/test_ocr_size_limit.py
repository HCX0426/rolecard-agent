"""OCR 图片大小上限（rag/ocr.py，M3）的单元测试。

验证整图 base64 内联进请求体前先卡大小：超大图在「读文件/发网络」之前就抛可读
`ParseError`（不静默吞掉，也不把超大扫描件发往云端 OCR）。
"""

from __future__ import annotations

import pytest

from rolecard_agent.rag.ocr import MAX_OCR_IMAGE_BYTES, CloudApiBackend, _read_image_b64
from rolecard_agent.rag.parser import ParseError


def test_read_image_b64_rejects_oversize(tmp_path) -> None:
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG" + b"\x00" * (MAX_OCR_IMAGE_BYTES + 10))
    with pytest.raises(ParseError, match="图片过大"):
        _read_image_b64(big)


def test_read_image_b64_accepts_exact_limit(tmp_path) -> None:
    ok = tmp_path / "ok.png"
    ok.write_bytes(b"\x89PNG" + b"\x00" * (MAX_OCR_IMAGE_BYTES - 4))
    # 边界内：通过大小闸门（后续网络/解析另行处理）。
    assert _read_image_b64(ok)


def test_cloud_ocr_rejects_oversize_before_network(tmp_path) -> None:

    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG" + b"\x00" * (MAX_OCR_IMAGE_BYTES + 10))
    # 云端 OCR 同样先卡大小：超大图在发出 HTTP 请求前就抛 ParseError（不悄悄外发）。
    backend = CloudApiBackend(api_key="sk-x")
    with pytest.raises(ParseError, match="图片过大"):
        backend.ocr(big)
