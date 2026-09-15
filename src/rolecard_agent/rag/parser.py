"""v2.2 文档 / 图片解析：把上传文件变成可入检索索引的纯文本。

统一入口 `parse_document(path)`，按扩展名分派：
- `.txt` / `.md`：直接按 UTF-8 读取（v2.1 已有行为，这里收敛到同一入口）。
- `.pdf`：用 `pypdf` 抽文本。纯 Python、轻量，可与主环境共存（chromadb 已带入 numpy /
  onnxruntime，pypdf 不新增二进制依赖）。
- Office OOXML（`.docx` / `.pptx` / `.xlsx`）：本质是 ZIP + XML，用**标准库** `zipfile` +
  `xml.etree.ElementTree` 抽文本，**零新增依赖**（刻意不引 python-docx / openpyxl / lxml，
  避免再给主环境加二进制依赖 —— 与 pypdf 的取舍一致）。
- 图片（`.png/.jpg/.jpeg/.bmp/.gif/.tiff/.webp`）：走 OCR。按 `requirements-ocr.txt` 的硬规则，
  OCR 必须在【独立 venv / 进程】里跑（PaddleOCR 自带 numpy / OpenCV 与主环境冲突），因此通过
  子进程调用一个独立的 OCR Python（`OCR_PYTHON`，默认 `.venv-ocr/Scripts/python.exe`）。未配置
  或该 venv 不可用 → 抛 `OcrUnavailable`，由上传端点降级为 pending，**绝不**把 paddle 栈拖进主环境。

解析失败一律抛 `ParseError`（可读原因、不含栈），调用方据其决定 500 还是降级。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

TEXT_EXTS: frozenset[str] = frozenset({".txt", ".md"})
PDF_EXTS: frozenset[str] = frozenset({".pdf"})
IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}
)
DOCX_EXTS: frozenset[str] = frozenset({".docx"})
PPTX_EXTS: frozenset[str] = frozenset({".pptx"})
XLSX_EXTS: frozenset[str] = frozenset({".xlsx"})
OFFICE_EXTS: frozenset[str] = DOCX_EXTS | PPTX_EXTS | XLSX_EXTS
PARSEABLE_EXTENSIONS: frozenset[str] = TEXT_EXTS | PDF_EXTS | OFFICE_EXTS | IMAGE_EXTS

# OOXML 命名空间（各部件 XML 的标签都带前缀，须以完整限定名查找）。
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SS_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class ParseError(Exception):
    """解析失败（含 OCR 不可用）。携带可读原因，绝不含栈或内部路径。"""


class OcrUnavailable(ParseError):
    """OCR 后端未配置 / 不可用：图片当前无法解析，应保持 pending。"""


def parse_document(
    path: str | Path, *, ocr_python: str | None = None, backend: object | None = None
) -> str:
    """把文件解析为纯文本。返回空字符串表示无文本（不报错，由调用方决定如何处理）。

    相对路径按当前工作目录解析；上传端点传入的是已落盘的绝对 / 相对路径。
    `backend` 为上层按策略选好的 OCR 后端（见 rag/ocr.select_ocr_backend）；未传时图片走
    本地 Paddle 默认路径（仍离线优先）。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in TEXT_EXTS:
        return p.read_text(encoding="utf-8", errors="ignore")
    if suffix in PDF_EXTS:
        return _parse_pdf(p)
    if suffix in DOCX_EXTS:
        return _parse_docx(p)
    if suffix in PPTX_EXTS:
        return _parse_pptx(p)
    if suffix in XLSX_EXTS:
        return _parse_xlsx(p)
    if suffix in IMAGE_EXTS:
        return _parse_image(p, ocr_python=ocr_python, backend=backend)
    fallback = suffix or "(无扩展名)"
    raise ParseError(f"不支持的文件类型：{fallback}")


def _parse_pdf(p: Path) -> str:
    """用 pypdf 抽取每页文本。导入延迟到调用点——主环境未装 pypdf 时不影响模块导入。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失由 requirements 保证
        raise ParseError("未安装 pypdf：pip install -r requirements-rag.txt") from exc
    try:
        reader = PdfReader(str(p))
        parts: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
        return "\n\n".join(parts)
    except ParseError:
        raise
    except Exception as exc:  # pypdf 可能抛各种内部错误（加密 / 损坏）
        raise ParseError(f"PDF 解析失败：{exc}") from exc


# ---------------------------------------------------------------- Office OOXML（zip + XML，零依赖）


def _open_ooxml(p: Path) -> zipfile.ZipFile:
    """打开 OOXML(zip)；非 zip / 损坏 → 可读 ParseError，而不是 zipfile 的原始异常。"""
    try:
        return zipfile.ZipFile(p)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ParseError(f"不是有效的 Office 文件（{p.suffix} 损坏或非 OOXML）：{exc}") from exc


def _xml_root(xml: bytes) -> ET.Element:
    try:
        return ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ParseError(f"OOXML 部件 XML 损坏：{exc}") from exc


def _text_from_xml(xml: bytes, *, para_tag: str, text_tag: str) -> str:
    """按段落聚合：每段落内所有 <t> 顺序拼接为一行，丢弃空段落。docx 与 pptx 共用此式。"""
    lines: list[str] = []
    for para in _xml_root(xml).iter(para_tag):
        line = "".join((t.text or "") for t in para.iter(text_tag)).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _parse_docx(p: Path) -> str:
    """docx 正文：`word/document.xml` 的段落（表格单元格内也是 <w:p>，故一并覆盖）。"""
    with _open_ooxml(p) as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError as exc:
            raise ParseError("不是有效的 .docx：缺少 word/document.xml") from exc
    return _text_from_xml(xml, para_tag=f"{_W_NS}p", text_tag=f"{_W_NS}t")


def _parse_pptx(p: Path) -> str:
    """pptx 各页：按 slide 序号排序，逐页抽 <a:t> 文本，页间空行分隔。"""
    with _open_ooxml(p) as z:
        slides = sorted(n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        if not slides:
            raise ParseError("不是有效的 .pptx：没有 ppt/slides/slide*.xml")
        parts = [
            _text_from_xml(z.read(name), para_tag=f"{_A_NS}p", text_tag=f"{_A_NS}t")
            for name in slides
        ]
    return "\n\n".join(x for x in parts if x)


def _parse_xlsx(p: Path) -> str:
    """xlsx 文本：共享字符串（+ 内联字符串）。**只做文本抽取，不重建表结构行列**。"""
    with _open_ooxml(p) as z:
        names = z.namelist()
        sheets = [n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
        if not sheets:
            raise ParseError("不是有效的 .xlsx：没有 xl/worksheets/sheet*.xml")
        lines: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in _xml_root(z.read("xl/sharedStrings.xml")).iter(f"{_SS_NS}si"):
                s = "".join((t.text or "") for t in si.iter(f"{_SS_NS}t")).strip()
                if s:
                    lines.append(s)
        for name in sheets:  # 少数写入器用 inlineStr
            for is_el in _xml_root(z.read(name)).iter(f"{_SS_NS}is"):
                s = "".join((t.text or "") for t in is_el.iter(f"{_SS_NS}t")).strip()
                if s:
                    lines.append(s)
    return "\n".join(lines)


def _default_ocr_python() -> str | None:
    """默认 OCR 解释器：项目根下的独立 venv（requirements-ocr.txt 的安装约定）。

    parser.py 位于 <root>/src/rolecard_agent/rag/，故项目根为 parents[3]。
    """
    cand = Path(__file__).resolve().parents[3] / ".venv-ocr" / "Scripts" / "python.exe"
    return str(cand) if cand.exists() else None


def _parse_image(
    p: Path, *, ocr_python: str | None = None, backend: object | None = None
) -> str:
    """图片 OCR：优先用上层按策略选好的 `backend`（见 rag/ocr.select_ocr_backend）；
    否则按 `ocr_python` 构造本地 Paddle 后端，再不行自动发现默认 .venv-ocr 解释器（仍离线优先）。

    后端不可用 → 抛 `OcrUnavailable`（调用方降级为 pending）；可用但调用失败 → 抛 `ParseError`。
    后端选择逻辑在 rag/ocr.py，避免主环境直接依赖 paddle 栈。
    """
    if backend is None and ocr_python is not None:
        from rolecard_agent.rag.ocr import LocalPaddleBackend

        backend = LocalPaddleBackend(exe=ocr_python)
    if backend is None:
        from rolecard_agent.rag.ocr import LocalPaddleBackend

        backend = LocalPaddleBackend()  # 自动发现默认路径（首选）
    if not backend.available():
        raise OcrUnavailable(
            "OCR 后端未配置：按 requirements-ocr.txt 在独立 venv 安装 paddleocr，"
            "并设置 OCR_PYTHON 指向其 python（默认 .venv-ocr/Scripts/python.exe）；"
            "或配置 OCR_API_KEY 走云端兜底。"
        )
    try:
        return backend.ocr(p)
    except OcrUnavailable:
        raise
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - 后端意外异常统一成解析失败
        raise ParseError(f"OCR 失败：{exc}") from exc
