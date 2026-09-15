"""v2.2 文档 / 图片解析：把上传文件变成可入检索索引的纯文本。

统一入口 `parse_document(path)`，按扩展名分派：
- `.txt` / `.md`：直接按 UTF-8 读取（v2.1 已有行为，这里收敛到同一入口）。
- `.pdf`：用 `pypdf` 抽文本。纯 Python、轻量，可与主环境共存（chromadb 已带入 numpy /
  onnxruntime，pypdf 不新增二进制依赖）。
- 图片（`.png/.jpg/.jpeg/.bmp/.gif/.tiff/.webp`）：走 OCR。按 `requirements-ocr.txt` 的硬规则，
  OCR 必须在【独立 venv / 进程】里跑（PaddleOCR 自带 numpy / OpenCV 与主环境冲突），因此通过
  子进程调用一个独立的 OCR Python（`OCR_PYTHON`，默认 `.venv-ocr/Scripts/python.exe`）。未配置
  或该 venv 不可用 → 抛 `OcrUnavailable`，由上传端点降级为 pending，**绝不**把 paddle 栈拖进主环境。

解析失败一律抛 `ParseError`（可读原因、不含栈），调用方据其决定 500 还是降级。
"""
from __future__ import annotations

from pathlib import Path

TEXT_EXTS: frozenset[str] = frozenset({".txt", ".md"})
PDF_EXTS: frozenset[str] = frozenset({".pdf"})
IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}
)
PARSEABLE_EXTENSIONS: frozenset[str] = TEXT_EXTS | PDF_EXTS | IMAGE_EXTS


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
