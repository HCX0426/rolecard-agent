"""本地基础设施的探活原语（core 层）。

为什么在 core：服务页的可用性探测（core/services.py）与选择器（rag/ocr.py）必须对
"这个本地实现能不能用"给出**同一个答案**——此前两处各自实现且不同步，服务页显示
"未知本地实现/不可用"、选择器却真的会去试，用户看到自相矛盾的状态（2026-09-17）。
原语放 core（方向合规：rag→core 允许，core→rag 禁止），两处共用一份判定。
"""

from __future__ import annotations

import time

# 探活结果的短 TTL 缓存（按 base_url+model 记）。
# 为什么需要：服务页一次渲染会对同一 (base_url, model) 探测多次（状态行 + 生效判定），
# Ollama 没在跑时每次都要等满 3s 网络超时 —— 用户看到的就是"服务页签转圈好几秒"
# （用户 2026-09-18）。10s 的代价是"恢复可用"最多延迟 10s 被看到，可接受；
# TTL 过短等于没缓存，过长则状态失真。dict 并发写最坏是重复探测一次，无害。
_PROBE_TTL = 10.0
_PROBE_CACHE: dict[tuple[str, str], tuple[bool, float]] = {}


def vision_model_ready(base_url: str | None, model: str, *, use_cache: bool = True) -> bool:
    """Ollama 上某个（多模态）模型是否在位：/api/tags 里全名或去 tag 前缀命中。

    只探测、不发推理请求，3s 超时；任何异常一律视为不在位（调用方顺延下一候选）。
    结果带 TTL 缓存（`use_cache=False` 强制重探，供"刷新状态"类操作使用）。
    """
    base = (base_url or "http://127.0.0.1:11434").rstrip("/")
    cache_key = (base, model)
    if use_cache:
        cached = _PROBE_CACHE.get(cache_key)
        if cached is not None and time.monotonic() - cached[1] < _PROBE_TTL:
            return cached[0]
    try:
        import httpx

        tags = httpx.get(f"{base}/api/tags", timeout=3.0).json()
    except Exception:  # noqa: BLE001 - 探活失败 = 不在位
        _PROBE_CACHE[cache_key] = (False, time.monotonic())
        return False
    names = [str(m.get("name", "")) for m in tags.get("models", [])]
    ok = any(n == model or n.split(":")[0] == model.split(":")[0] for n in names)
    _PROBE_CACHE[cache_key] = (ok, time.monotonic())
    return ok
