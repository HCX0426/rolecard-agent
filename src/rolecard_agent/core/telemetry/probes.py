""""本机推理服务该问哪个地址" —— 这一条要读供应商目录，所以它留在 core。

探活原语本身（模型在不在位、能不能看图、驻留/卸载/列出）是**纯 httpx + `base.outbound`**
的判定，已下沉到 `base/probes.py`：服务页探测（`core/services.py`）与 OCR 选择器
（`rag/ocr.py`）要用同一个答案，而 `rag` 借内核的东西就是反向依赖（2026-10-04 依赖收口）。
这里 `from ... import *` 之外的显式再导出是为了让既有调用点（含 monkeypatch 的靶位）
一个字都不必改 —— 判定只有一份住在 base，本模块不复制实现。
"""

from __future__ import annotations

from rolecard_agent.base.probes import (
    _CAP_CACHE,
    _PROBE_CACHE,
    DEFAULT_OLLAMA,
    ollama_keep,
    ollama_loaded,
    ollama_reachable,
    ollama_unload,
    vision_capability,
    vision_model_ready,
)
from rolecard_agent.config import Settings
from rolecard_agent.core.model_settings import client_style

__all__ = [
    "DEFAULT_OLLAMA",
    "_CAP_CACHE",
    "_PROBE_CACHE",
    "local_inference_base_url",
    "ollama_keep",
    "ollama_loaded",
    "ollama_reachable",
    "ollama_unload",
    "vision_capability",
    "vision_model_ready",
]


def local_inference_base_url(settings: Settings) -> str:
    """本机推理服务（Ollama）的地址该问哪儿。

    默认后端是 native(Ollama) 时用它配的那个 `base_url`（用户可能把 Ollama 装在别的端口/
    机器上）；默认后端是云端时，本地服务仍然是出厂那个地址——"停掉本地服务"这件事与
    "当前用哪个后端对话"是两码事，不能混成一个。
    """
    try:
        backend = settings.backend(None)
    except KeyError:  # 没配默认后端：出厂本地地址
        return DEFAULT_OLLAMA
    if client_style(backend.provider) == "native" and backend.base_url:
        return str(backend.base_url)
    return DEFAULT_OLLAMA
