"""OCR worker —— 必须在【独立 OCR venv】里运行（PaddleOCR 自带 numpy / OpenCV / onnxruntime，
与主服务环境冲突，见 requirements-ocr.txt）。由 `rolecard_agent/rag/ocr.py` 通过子进程调用。

协议（极简、易排错）：
- argv[1] = 图片路径
- 成功：退出码 0，stdout = 识别出的文本（UTF-8，逐行；无文本则 stdout 为空串）
- 失败：非零退出码，stderr = 可读原因

兼容两代 PaddleOCR API（本机实测装的是 3.x）：
- **3.x**：`PaddleOCR(...).predict(path)` → 结果对象含 `rec_texts`（推荐，已装版本走此路）。
- **2.x**：`PaddleOCR(use_angle_cls=True).ocr(path, cls=True)`
  → `[[ [bbox, (text, score)], ... ]]`。

paddle 初始化 / 推理会往 stdout 打日志，本脚本把这段重定向到 stderr，保证 stdout **只有**识别文本。
主服务进程永不 import paddle，保持轻量与可离线。
"""
from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# PaddlePaddle 3.x 的 oneDNN(MKLDNN) CPU 后端在部分 OCR 模型上会命中未实现的 PIR 属性
# （ConvertPirAttribute2RuntimeAttribute not support ... onednn_instruction.cc），导致推理崩。
# 关闭 MKLDNN 走朴素 CPU 核，牺牲一点速度换取可用性。必须在 import paddle 之前设置。
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")

# stdout/stderr 固定 UTF-8：否则中文 Windows 默认按 GBK 编码，父进程按 locale 解码会乱码。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def _build_ocr():  # noqa: ANN202 - 返回 (ocr, is_v3)，类型依赖外部库
    """按已装版本构造 PaddleOCR；返回 (ocr, is_v3)。

    v3 有 `predict()`，v2 用 `ocr()`；两者构造参数不同
    （use_textline_orientation vs use_angle_cls）。
    """
    from paddleocr import PaddleOCR

    if hasattr(PaddleOCR, "predict"):  # 3.x
        try:
            ocr = PaddleOCR(
                lang="ch",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        except Exception:  # noqa: BLE001 - 参数名随小版本变；退回最简构造
            ocr = PaddleOCR(lang="ch")
        return ocr, True
    return PaddleOCR(use_angle_cls=True, lang="ch"), False  # 2.x


def _texts_from_v3(results: Iterable[Any] | None) -> list[str]:
    """3.x：每个 result 为 dict-like，取 `rec_texts`（识别出的字符串列表）。"""
    out: list[str] = []
    for res in results or []:
        texts = None
        if isinstance(res, dict):
            texts = res.get("rec_texts")
        else:
            try:
                texts = res["rec_texts"]  # OCRResult 支持下标访问
            except Exception:  # noqa: BLE001
                texts = getattr(res, "rec_texts", None)
        if texts:
            out.extend(str(t) for t in texts)
    return out


def _texts_from_v2(result: Iterable[Any] | None) -> list[str]:
    """2.x：List[page]，每页 List[(bbox, (text, score))]。"""
    out: list[str] = []
    for page in result or []:
        for line in page or []:
            if not line or len(line) < 2 or not line[1]:
                continue
            out.append(str(line[1][0]))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ocr_worker.py <image-path>", file=sys.stderr)
        return 2
    image_path = sys.argv[1]
    if not Path(image_path).exists():
        print(f"图片不存在：{image_path}", file=sys.stderr)
        return 2
    lines: list[str] = []
    try:
        # 把 paddle 的初始化 / 日志输出赶到 stderr，stdout 只留给识别文本。
        with contextlib.redirect_stdout(sys.stderr):
            ocr, is_v3 = _build_ocr()
            raw = ocr.predict(image_path) if is_v3 else ocr.ocr(image_path, cls=True)
            lines = _texts_from_v3(raw) if is_v3 else _texts_from_v2(raw)
    except ImportError:
        print(
            "paddleocr 未安装：在独立 venv 中执行 pip install -r requirements-ocr.txt",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # noqa: BLE001 - paddle 可能在初始化 / 推理时抛各种错误
        print(f"OCR 推理失败：{exc}", file=sys.stderr)
        return 1
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
