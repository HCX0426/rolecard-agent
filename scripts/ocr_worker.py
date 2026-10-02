"""OCR worker —— 必须在【独立 OCR venv】里运行（见 requirements-ocr.txt 的现行理由：
主服务进程永不 import OCR 栈，运行树里不该有 cv2/omegaconf 这一族）。
由 `rolecard_agent/rag/ocr.py` 通过子进程调用。

协议（极简、易排错；换后端时这条协议一个字都没改）：
- argv[1] = 图片路径
- 成功：退出码 0，stdout = 识别出的文本（UTF-8，逐行；无文本则 stdout 为空串）
- 失败：非零退出码，stderr = 可读原因

RapidOCR（3.x）：`RapidOCR()(path)` → 结果对象的 `txts` 是识别出的字符串列表。
老的 `rapidocr-onnxruntime`（1.x）返回 `([...], [[box, text, score], ...])` —— 两种形状都认，
因为只在本机装的是 3.x 而 CI 镜像可能拉到另一条线（读法见 `_texts_from_result`）。

引擎初始化 / 推理会往 stdout 打日志（rapidocr 用 colorlog），本脚本把这段重定向到 stderr，
保证 stdout **只有**识别文本。置信度（`scores`）不参与输出：worker 的协议是纯文本，
从前 Paddle 那版也没筛，换了后端不该顺手改变"哪些字算识别出来"的口径。
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

# stdout/stderr 固定 UTF-8：否则中文 Windows 默认按 GBK 编码，父进程按 locale 解码会乱码。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def _build_ocr():  # noqa: ANN202 - 返回 RapidOCR 引擎，类型依赖外部库
    from rapidocr import RapidOCR

    return RapidOCR()


def _texts_from_result(res: Any) -> list[str]:
    """3.x：结果对象的 `txts`（或 dict 里的 `txts`）。1.x：`[[box, text, score], ...]`。"""
    if hasattr(res, "txts") and res.txts:
        return [str(t) for t in res.txts]
    if isinstance(res, dict) and res.get("txts"):
        return [str(t) for t in res["txts"]]

    rows = res[1] if isinstance(res, tuple) and len(res) > 1 else res
    out: list[str] = []
    for page in rows or []:
        for line in page if isinstance(page, list | tuple) else []:
            if isinstance(line, list | tuple) and len(line) > 1 and line[1]:
                out.append(str(line[1]))
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
        # 把引擎的初始化 / 日志输出赶到 stderr，stdout 只留给识别文本。
        with contextlib.redirect_stdout(sys.stderr):
            engine = _build_ocr()
            raw = engine(image_path)
        lines = _texts_from_result(raw)
    except ImportError:
        print(
            "rapidocr 未安装：在独立 venv 中执行 pip install -r requirements-ocr.txt",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # noqa: BLE001 - 推理侧可能抛各种后端错误
        print(f"OCR 推理失败：{exc}", file=sys.stderr)
        return 1
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
