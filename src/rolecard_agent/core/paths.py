"""项目内本地路径发现（core 层，向下无依赖）。

此前 `default_ocr_python` 定义在 `rag/parser.py`，导致 `core/services.py` 反向 import `rag`
（core→rag 跨层耦合，见审查 M10）。OCR 解释器只是"项目根的独立 venv"路径问题，与 rag 无关，
故下沉到 core 层；`rag` 仍可 import core（向下依赖合规）。
"""

from __future__ import annotations

from pathlib import Path


def default_ocr_python() -> str | None:
    """默认 OCR 解释器：项目根下的独立 venv（requirements-ocr.txt 的安装约定）。

    本文件位于 <root>/src/rolecard_agent/core/，故项目根为 parents[3]。
    """
    cand = Path(__file__).resolve().parents[3] / ".venv-ocr" / "Scripts" / "python.exe"
    return str(cand) if cand.exists() else None
