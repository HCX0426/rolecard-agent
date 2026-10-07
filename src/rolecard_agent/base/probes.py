"""本地推理服务的探活原语（base 层：只认 httpx + `base.outbound`，不认任何上层）。

为什么在 base 而不是 core：服务页的可用性探测（`core/services.py`）、OCR 后端选择器
（`rag/ocr.py`）、主动开口的驻留/卸载（`api/routers/local_service.py`）必须对"这个本地
实现能不能用"给出**同一个答案** —— 此前两处各自实现且不同步，服务页显示"未知本地实现/
不可用"、选择器却真的会去试，用户看到自相矛盾的状态（2026-09-17）。
原语曾放在 `core/probes.py`，于是 `rag` 借用它就要反向 import 内核，那是审查点名要收口的
双向边之一；放到底层后两个上层都合法地往下依赖，一份判定照样共用。

**唯一不在这儿的探活**是"本机推理服务该问哪个地址"（`core/probes.py::local_inference_base_url`）：
它要读供应商目录判断 native/openai，那是 core 的知识，不下沉。

探活都只问元数据、不发推理请求，任何异常一律折成"不可用/不知道"（调用方顺延下一候选），
所以这一族的返回值要么是确定的判定，要么是如实的 False/None —— 没有"抛出去让整轮 500"的。
"""

from __future__ import annotations

import time

from rolecard_agent.base import outbound
from rolecard_agent.config import DEFAULT_LOCAL_BASE_URL

#: 出厂的本地推理服务地址。值从 config 现读（端口单源）—— 本模块只保留这个名字，
#: 因为它是 `core/probes.py` 对外再导出的既有称呼，改名的收益是零、涟漪是所有调用方。
DEFAULT_OLLAMA = DEFAULT_LOCAL_BASE_URL

# 探活结果的短 TTL 缓存（按 base_url+model 记）。
# 为什么需要：服务页一次渲染会对同一 (base_url, model) 探测多次（状态行 + 生效判定），
# Ollama 没在跑时每次都要等满 3s 网络超时 —— 用户看到的就是"服务页签转圈好几秒"
# （用户 2026-09-18）。10s 的代价是"恢复可用"最多延迟 10s 被看到，可接受；
# TTL 过短等于没缓存，过长则状态失真。dict 并发写最坏是重复探测一次，无害。
PROBE_TTL = 10.0
_PROBE_CACHE: dict[tuple[str, str], tuple[bool, float]] = {}


def vision_model_ready(base_url: str | None, model: str, *, use_cache: bool = True) -> bool:
    """Ollama 上某个（多模态）模型是否在位：/api/tags 里全名或去 tag 前缀命中。

    只探测、不发推理请求，3s 超时；任何异常一律视为不在位（调用方顺延下一候选）。
    结果带 TTL 缓存（`use_cache=False` 强制重探，供"刷新状态"类操作使用）。
    """
    base = (base_url or DEFAULT_OLLAMA).rstrip("/")
    cache_key = (base, model)
    if use_cache:
        cached = _PROBE_CACHE.get(cache_key)
        if cached is not None and time.monotonic() - cached[1] < PROBE_TTL:
            return cached[0]
    try:

        tags = outbound.get(f"{base}/api/tags", timeout=3.0).json()
    except Exception:  # noqa: BLE001 - 探活失败 = 不在位
        _PROBE_CACHE[cache_key] = (False, time.monotonic())
        return False
    names = [str(m.get("name", "")) for m in tags.get("models", [])]
    ok = any(n == model or n.split(":")[0] == model.split(":")[0] for n in names)
    _PROBE_CACHE[cache_key] = (ok, time.monotonic())
    return ok


# 能力探测单独一份缓存：键形状与 `vision_model_ready` 的 (base, model) 一样，共用一个 dict
# 会让两个语义互相覆盖（一个是"这个模型在不在位"，一个是"这个模型能不能看图"）。
_CAP_CACHE: dict[tuple[str, str], tuple[bool | None, float]] = {}


def vision_capability(
    base_url: str | None, model: str, *, use_cache: bool = True
) -> bool | None:
    """Ollama 这个模型**能不能看图** —— 读 `/api/show` 的 `capabilities`（实测形状：
    `["tools","thinking","completion","vision"]`）。只问元数据，不发推理请求。

    **三态是这条存在的全部理由**（口径是"只拦确定的否"）：
    `True` 能看 / `False` 明确不能看 / `None` **不知道**（老版本 Ollama 没这个字段、
    请求失败、超时、模型没装）。把"不知道"当成"不能看"，就会误杀那些其实能看、
    只是能力位没勾或 Ollama 暂时抽风的模型 —— 那比不拦更糟。
    """
    base = (base_url or DEFAULT_OLLAMA).rstrip("/")
    key = (base, model)
    if use_cache:
        cached = _CAP_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[1] < PROBE_TTL:
            return cached[0]
    verdict: bool | None
    try:

        payload = outbound.post(f"{base}/api/show", json={"model": model}, timeout=3.0).json()
        caps = payload.get("capabilities")
    except Exception:  # noqa: BLE001 - 问不到就是"不知道"，不是"不能"
        caps = None
    if not isinstance(caps, list):
        verdict = None
    else:
        lowered = {str(c).strip().lower() for c in caps}
        verdict = "vision" in lowered
    _CAP_CACHE[key] = (verdict, time.monotonic())
    return verdict


def ollama_keep(
    base_url: str | None,
    model: str,
    keep_alive: int = -1,
    num_ctx: int | None = None,
) -> bool:
    """把模型载入显存并按 keep_alive 常驻（-1 = 永不自动卸载）。

    Ollama 闲置默认 ~5 分钟卸载模型，下次请求要冷加载（8GB 卡上可能十几~几十秒），
    用户体感就是"首条消息很慢/服务页连不上"。这里 POST /api/generate 空 prompt + keep_alive
    触发/续期驻留。keep_alive 是 Ollama 原生参数，仅 native(Ollama) 后端有意义。
    加载本身可能很慢，给足超时；任何异常返回 False（调用方如实提示，不静默）。

    num_ctx 必须一起传：Ollama 在**加载时**按请求里的 options.num_ctx 定死上下文窗口，
    缺省回落到 4096。若常驻探针不带 num_ctx，就会把模型钉在 4096，随后第一条真实对话
    （带后端配置的 num_ctx）又触发一次重载——预热反而制造了一次冷加载（2026-09-19）。
    """

    base = (base_url or DEFAULT_OLLAMA).rstrip("/")
    payload: dict[str, object] = {"model": model, "prompt": "", "keep_alive": keep_alive}
    if num_ctx:
        payload["options"] = {"num_ctx": num_ctx}
    try:
        r = outbound.post(f"{base}/api/generate", json=payload, timeout=120.0)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 - 探活/驻留失败即结果
        return False


def ollama_unload(base_url: str | None, model: str) -> bool:
    """把一个模型从显存里卸载（keep_alive=0 的空请求，Ollama 原生语义）。

    为什么需要它而不只是"别常驻"：`ollama_keep(-1)` 钉住的模型**自己永远不会让出显存**
    （expires_at 被推到几百年后），而 8GB 卡上那就是整机不可用——用户要玩游戏、要跑别的
    模型，必须先有人替他按下这个按钮（2026-09-19 的真实场景）。与 `ollama_keep` 相反，
    这里要**短超时**：卸载是即时动作，等久了说明服务本来就没在跑，返回 False 让调用方
    如实提示，而不是把请求线程挂住。
    """

    base = (base_url or DEFAULT_OLLAMA).rstrip("/")
    try:
        r = outbound.post(
            f"{base}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0},
            timeout=10.0,
        )
        return r.status_code == 200
    except Exception:  # noqa: BLE001 - 卸不掉 / 连不上即失败，调用方如实报
        return False


def ollama_reachable(base_url: str | None) -> bool:
    """本地推理服务在不在跑（GET /api/tags，3s）。区分"没起"与"起了但没驻留模型"。"""

    base = (base_url or DEFAULT_OLLAMA).rstrip("/")
    try:
        return outbound.get(f"{base}/api/tags", timeout=3.0).status_code == 200
    except Exception:  # noqa: BLE001 - 连不上就是没在跑
        return False


def ollama_loaded(base_url: str | None) -> list[dict[str, object]]:
    """当前常驻显存的模型（GET /api/ps）。失败/无 → []。用于模型页显示"是否已常驻"。"""

    base = (base_url or DEFAULT_OLLAMA).rstrip("/")
    try:
        data = outbound.get(f"{base}/api/ps", timeout=5.0).json()
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "name": m.get("name"),
            "size": m.get("size"),
            "expires_at": m.get("expires_at"),
            "processor": m.get("processor"),
        }
        for m in data.get("models", [])
    ]
