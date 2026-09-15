"""OCR worker —— 必须在【独立 OCR venv】里运行（PaddleOCR 自带 numpy / OpenCV / onnxruntime，
与主服务环境冲突，见 requirements-ocr.txt）。由 `rolecard_agent/rag/parser.py` 通过子进程调用。

协议（极简、易排错）：
- argv[1] = 图片路径
- 成功：退出码 0，stdout = 识别出的文本（UTF-8，逐行）
- 失败：非零退出码，stderr = 可读原因

这样主服务进程永不 import paddle，保持轻量与可离线。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ocr_worker.py <image-path>", file=sys.stderr)
        return 2
    image_path = sys.argv[1]
    if not Path(image_path).exists():
        print(f"图片不存在：{image_path}", file=sys.stderr)
        return 2
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        print(
            "paddleocr 未安装：在独立 venv 中执行 "
            "pip install -r requirements-ocr.txt",
            file=sys.stderr,
        )
        return 3
    try:
        # use_angle_cls 处理中英文混排旋转；lang="ch" 覆盖中文为主场景。
        ocr = PaddleOCR(use_angle_cls=True, lang="ch")
        result = ocr.ocr(image_path, cls=True)
    except Exception as exc:  # paddle 可能在初始化 / 推理时抛各种错误
        print(f"OCR 推理失败：{exc}", file=sys.stderr)
        return 1

    lines: list[str] = []
    # paddleocr 返回结构：List[ page ]，每页 List[ (bbox, (text, score)) ]。
    for page in result or []:
        for line in page or []:
            if not line or len(line) < 2:
                continue
            text = line[1][0] if line[1] else ""
            if text:
                lines.append(text)
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
