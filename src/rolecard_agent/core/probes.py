"""本地基础设施的探活原语（core 层）。

为什么在 core：服务页的可用性探测（core/services.py）与选择器（rag/ocr.py）必须对
"这个本地实现能不能用"给出**同一个答案**——此前两处各自实现且不同步，服务页显示
"未知本地实现/不可用"、选择器却真的会去试，用户看到自相矛盾的状态（2026-09-17）。
原语放 core（方向合规：rag→core 允许，core→rag 禁止），两处共用一份判定。
"""

from __future__ import annotations


def vision_model_ready(base_url: str | None, model: str) -> bool:
    """Ollama 上某个（多模态）模型是否在位：/api/tags 里全名或去 tag 前缀命中。

    只探测、不发推理请求，3s 超时；任何异常一律视为不在位（调用方顺延下一候选）。
    """
    try:
        import httpx

        base = (base_url or "http://127.0.0.1:11434").rstrip("/")
        tags = httpx.get(f"{base}/api/tags", timeout=3.0).json()
    except Exception:  # noqa: BLE001 - 探活失败 = 不在位
        return False
    names = [str(m.get("name", "")) for m in tags.get("models", [])]
    return any(n == model or n.split(":")[0] == model.split(":")[0] for n in names)
