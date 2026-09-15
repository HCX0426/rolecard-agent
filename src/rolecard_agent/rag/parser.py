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

import os
import subprocess
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


def parse_document(path: str | Path, *, ocr_python: str | None = None) -> str:
    """把文件解析为纯文本。返回空字符串表示无文本（不报错，由调用方决定如何处理）。

    相对路径按当前工作目录解析；上传端点传入的是已落盘的绝对 / 相对路径。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in TEXT_EXTS:
        return p.read_text(encoding="utf-8", errors="ignore")
    if suffix in PDF_EXTS:
        return _parse_pdf(p)
    if suffix in IMAGE_EXTS:
        return _parse_image(p, ocr_python=ocr_python)
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
    """默认 OCR 解释器：项目根下的独立 venv（requirements-ocr.txt 的安装约定）。"""
    cand = Path(__file__).resolve().parents[1] / ".venv-ocr" / "Scripts" / "python.exe"
    return str(cand) if cand.exists() else None


def _parse_image(p: Path, *, ocr_python: str | None = None) -> str:
    """图片 OCR：子进程调用独立 OCR venv 里的 worker 脚本，协议为纯文本 stdout / 非零退出码。"""
    exe = ocr_python or os.environ.get("OCR_PYTHON") or _default_ocr_python()
    if not exe or not Path(exe).exists():
        raise OcrUnavailable(
            "OCR 后端未配置：按 requirements-ocr.txt 在独立 venv 安装 paddleocr，"
            "并设置 OCR_PYTHON 指向其 python（默认 .venv-ocr/Scripts/python.exe）。"
        )
    worker = Path(__file__).resolve().parents[1] / "scripts" / "ocr_worker.py"
    if not worker.exists():
        raise OcrUnavailable(f"OCR worker 脚本缺失：{worker}")
    try:
        proc = subprocess.run(
            [exe, str(worker), str(p)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        raise ParseError(f"OCR 子进程启动失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        raise ParseError(f"OCR 失败（退出码 {proc.returncode}）：{detail}")
    return (proc.stdout or "").strip()
