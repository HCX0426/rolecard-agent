"""v2.2 可插拔 OCR 后端：本地 Paddle（首选）+ 云端 API key（兜底）。

设计（面试可讲，对应"离线优先 + 云端兜底 + 隐私分级"）：

- OCR 是重依赖，且涉及"图片是否离开本机"的隐私问题，所以做成后端可插拔：
  - `LocalPaddleBackend`：子进程调用**独立 venv** 里的 PaddleOCR（requirements-ocr.txt 安装
    约定）。离线、数据不出本机；numpy/OpenCV/onnxruntime 的摩擦被隔离在独立进程之外。
  - `CloudApiBackend`：走 HTTP 的 OCR API（默认 OCR.space，仅需 api_key，无 SDK）。数据会
    发往第三方——因此只作兜底，且 `available()` 严格要求显式配了 key，绝不悄悄外发。
- 选择器 `select_ocr_backend`：**Paddle 优先**；Paddle 不可用（未装独立 venv）时，若配了
  `OCR_API_KEY` 则回退云端；都没有 → 返回 None，调用方降级为 `OcrUnavailable`，而不是把重型
  依赖拖进主进程或偷偷把图片发到外网。

错误语义（与解析层一致）：后端"不可用"抛 `OcrUnavailable`（调用方降级为 pending）；"可用但
调用失败"抛 `ParseError`（调用方据上下文决定 500 还是提示）。
"""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from rolecard_agent.config import Settings
from rolecard_agent.rag.parser import OcrUnavailable, ParseError, _default_ocr_python

# ocr.py 位于 <root>/src/rolecard_agent/rag/，故项目根为 parents[3]；worker 在 <root>/scripts。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_OCR_WORKER = _PROJECT_ROOT / "scripts" / "ocr_worker.py"


class OcrBackend(Protocol):
    """一个 OCR 后端：能回答"是否就绪"并产出图片文本。"""

    name: str

    def available(self) -> bool:
        """本后端是否已配置就绪（可执行 / 已配 key）。未就绪不应调用 `ocr`。"""
        ...

    def ocr(self, image_path: Path) -> str:
        """把图片解析为纯文本；不可用或失败抛 `OcrUnavailable` / `ParseError`。"""
        ...


class LocalPaddleBackend:
    """子进程调用独立 venv 里的 PaddleOCR worker（见 scripts/ocr_worker.py）。

    绝不进主环境：OCR 栈的 numpy/OpenCV/onnxruntime 与主环境 chromadb 的 numpy 冲突，
    因此只在 `OCR_PYTHON`（默认 .venv-ocr/Scripts/python.exe）指向的独立进程里跑。
    """

    name = "paddle"

    def __init__(self, *, exe: str | None = None) -> None:
        self._exe = exe or os.environ.get("OCR_PYTHON") or _default_ocr_python()

    def available(self) -> bool:
        if not self._exe or not Path(self._exe).exists():
            return False
        return _OCR_WORKER.exists()

    def ocr(self, image_path: Path) -> str:
        if not self.available():
            raise OcrUnavailable(
                "OCR 后端未配置：按 requirements-ocr.txt 在独立 venv 安装 paddleocr，"
                "并设置 OCR_PYTHON 指向其 python（默认 .venv-ocr/Scripts/python.exe）。"
            )
        try:
            proc = subprocess.run(
                [self._exe, str(_OCR_WORKER), str(image_path)],
                capture_output=True,
                # 显式 UTF-8：worker 已 reconfigure 为 UTF-8；不能用 text=True（那样按 locale
                # 解码，中文 Windows = GBK，中文 OCR 文本会乱码）。
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - 启动失败 = 解析失败，由调用方决定降级
            raise ParseError(f"OCR 子进程启动失败：{exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
            raise ParseError(f"OCR 失败（退出码 {proc.returncode}）：{detail}")
        return (proc.stdout or "").strip()


class CloudApiBackend:
    """云端 OCR（默认 OCR.space）：仅需 api_key，无 SDK。仅作 Paddle 不可用时的兜底。

    隐私红线：图片会发往第三方。所以 `available()` 严格要求显式配了 `OCR_API_KEY`；未配则
    不启用，绝不悄悄把用户上传的病历图片发出去。调用失败抛 `ParseError`（不静默返回空）。
    """

    name = "cloud"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        provider: str = "ocrspace",
        api_url: str | None = None,
    ) -> None:
        self._key = api_key
        self._provider = provider
        self._url = api_url or "https://api.ocr.space/parse/image"

    def available(self) -> bool:
        return bool(self._key)

    def ocr(self, image_path: Path) -> str:
        import httpx

        if not self._key:
            raise OcrUnavailable("云端 OCR 未配置 OCR_API_KEY。")
        try:
            raw = Path(image_path).read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except Exception as exc:
            raise ParseError(f"读取图片失败：{exc}") from exc
        try:
            res = httpx.post(
                self._url,
                data={
                    "apikey": self._key,
                    "base64image": f"data:image/png;base64,{b64}",
                    "language": "chs",
                    "isOverlayRequired": "false",
                    "scale": "true",
                },
                timeout=30.0,
            )
            res.raise_for_status()
            payload = res.json()
        except Exception as exc:  # noqa: BLE001 - 网络/HTTP 失败 = 解析失败
            raise ParseError(f"云端 OCR 请求失败：{exc}") from exc
        # OCR.space 响应：ParsedResults[].ParsedText
        results = payload.get("ParsedResults") or []
        text = "\n".join((r.get("ParsedText") or "").strip() for r in results).strip()
        if not text:
            # 可能配额耗尽 / 参数错误：把 ErrorMessage 透出成可读失败，而不是静默返回空。
            err = payload.get("ErrorMessage") or payload.get("ErrorDetails")
            if err:
                raise ParseError(f"云端 OCR 返回错误：{err}")
        return text


def select_ocr_backend(
    settings: Settings,
    *,
    order: Sequence[str] | None = None,
    endpoints: Any = None,
) -> OcrBackend | None:
    """按策略选择 OCR 后端：默认 **Paddle 优先**，不可用时若有 key 回退云端，否则 None。

    `order` + `endpoints`（操作员在「服务」页签里维护的端点行，core/services.py）：
    给了就按序逐个试可用 —— `paddle` 探本地解释器；云端行按**行内** key/api_url 实例化
    （可并存多个云端 OCR 账号，谁排前面谁先被用）。`endpoints` 缺省时保留旧的字面 id
    解析（paddle/cloud + env key）。返回 None 时调用方应降级为 `OcrUnavailable`
    （保持 pending，不假装已读）。
    """
    paddle = LocalPaddleBackend(exe=settings.ocr_python)
    if order and endpoints:
        for cid in order:
            if cid == "paddle":
                if paddle.available():
                    return paddle
                continue
            cfg = endpoints.get(cid)
            if cfg is not None and cfg.api_key:
                return CloudApiBackend(api_key=cfg.api_key, api_url=cfg.base_url)
        return None
    cloud = CloudApiBackend(
        api_key=settings.ocr_api_key,
        provider=settings.ocr_provider,
        api_url=settings.ocr_api_url,
    )
    by_id = {"paddle": paddle, "cloud": cloud}
    mode = (settings.ocr_backend or "auto").lower()
    if order:
        # 操作员顺序优先于 env 档位：逐个试 available，谁就绪用谁（启停与优先级热生效）。
        for cid in order:
            backend = by_id.get(cid)
            if backend is not None and backend.available():
                return backend
        return None
    if mode == "paddle":
        return paddle if paddle.available() else None
    if mode == "cloud":
        return cloud if cloud.available() else None
    # auto（默认）：Paddle 优先，云端兜底
    if paddle.available():
        return paddle
    if cloud.available():
        return cloud
    return None
