"""「测试连接」与能力探测 —— 一次配置到底能不能用，问出来而不是猜（模型页的添加门禁）。

三条边界（为什么这一支要单独成模块，而不是塞进 `core/probes.py`）：
`probes.py` 是**本地基础设施**的探活（Ollama 在不在跑、模型在不在位），零成本、可以随便刷；
这里是**向端点要一次回答**，可能花配额、可能把数据发出去，所以必须显式点名才做。

  * 免费的两件默认做：端点可达（含凭据有效）、模型在不在列表里。
  * 花配额的（工具能力）与外发图片的（云端视觉）必须由调用方明确要求 —— 图片外发是本项目的
    红线（与云端 OCR、SauceNAO 同一条义务），绝不被一次"顺手全测"带过去。
  * 本地 Ollama 的视觉是**免费**的（`/api/show` 元数据，复用 `probes.vision_capability`），
    所以本地与云端的探测天生不对称：结果里带 `vision_source` 如实说明这个结论是怎么来的。
  * 任何网络/HTTP 异常都收敛成"结论 + 一句原因"，不往外抛：调用方是界面，它要原样显示后端
    给的那句话（本页既有惯例：错误文案只有一个来源）。
"""

from __future__ import annotations

import base64
import struct
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rolecard_agent.base import outbound
from rolecard_agent.config import DEFAULT_LOCAL_BASE_URL
from rolecard_agent.core.model_settings import client_style, endpoint_key
from rolecard_agent.core.probes import vision_capability

if TYPE_CHECKING:
    from rolecard_agent.core.model_settings import ModelSettingsService

# 探测用的超时：连通类要快（用户在等），生成类要给足（冷加载的本地模型首条可能十几秒）。
_LIST_TIMEOUT = 8.0
_CALL_TIMEOUT = 30.0

# 工具探测用的假工具：只要模型支持工具调用，它就该在回答里带出 tool_calls。
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "probe_get_time",
        "description": "Get the current local time.",
        "parameters": {"type": "object", "properties": {}},
    },
}
_PROBE_TOOL_PROMPT = "现在几点了？必须调用 probe_get_time 工具来回答。"
_PROBE_VISION_PROMPT = "这张图是什么颜色？只回答一个词。"


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    """一次探测的靶子：端点 + 凭据 + 模型名。

    凭据由调用方（服务层）从**组**里取 —— 拆层后 key 不属于某个模型行，探测也按组拿，
    这样"同 key 下新加一个模型"探测到的端点与运行时会用的是同一个。
    """

    provider: str
    base_url: str | None
    api_key: str | None
    model: str

    @property
    def style(self) -> str:
        return client_style(self.provider)

    @property
    def root(self) -> str:
        return (self.base_url or _default_root(self.provider)).rstrip("/")

    def auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        if self.style == "native":
            # Ollama 不需要 key；给了也不发（多带一个 Authorization 头等于把凭据交给本地端口）。
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """探测结论。三态字段与能力位列同语义：`None` = 不知道，不是"不行"。"""

    reachable: bool
    detail: str = ""
    model_listed: bool | None = None
    tools: bool | None = None
    vision: bool | None = None
    vision_source: str | None = None  # free-metadata | uploaded-image | not-tested
    calls_used: int = 0  # 真发出去几次**推理**请求（列表/元数据不计数，那两类免费）
    models: list[str] = field(default_factory=list)

    def to_api(self) -> dict[str, object]:
        return {
            "reachable": self.reachable,
            "detail": self.detail,
            "model_listed": self.model_listed,
            "tools": self.tools,
            "vision": self.vision,
            "vision_source": self.vision_source,
            "calls_used": self.calls_used,
            "models": self.models,
        }


def _default_root(provider: str) -> str:
    """留空 base_url 时该问哪个端点：与分组同一份归一（`endpoint_key`）。"""
    return endpoint_key(provider, None)[1] or DEFAULT_LOCAL_BASE_URL


def resolve_target(
    svc: ModelSettingsService,
    *,
    user_id: str,
    provider_id: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str = "",
) -> ProbeTarget:
    """把"要给哪个端点做一次探测"拼成靶子：已存的组提供凭据，请求里的值只做覆盖。

    三个细节必须有：
      * **前端只发 `provider_id` 就能测**（key 由服务端从组里取）—— 拆层后凭据不需要在
        网络上往返，也就不会出现在浏览器历史、代理日志或前端草稿状态里；
      * 临时给的 base_url 也要过 `validate_base_url`（M8 那条 SSRF 口径）：探测端点是
        一个"让服务器替你发请求"的口子，不校验就等于把它变成内网探测器。换端点复用同一把
        key 是中转站的正常用法，这里的取舍由操作员承担（本机自用，不另外加一道确认）；
      * 请求里带了 `api_key` 时**以它为准**（抽屉里正在填一把新 key 的场景）。

    探测是"让服务器替你发一次真请求"，所以它花的是**谁的凭据**必须有答案：`user_id` 把
    取组的范围限定在这个人的组里（M2d）。别人那个端点在这里查不到 ⇒ 报"供应商组不存在"，
    而不是替他烧一次配额。
    """
    from rolecard_agent.core.model_settings import (
        ModelSettingsError,
        validate_base_url,
    )

    group = svc.group_for(user_id=user_id, group_id=provider_id) if provider_id else None
    if provider_id and group is None:
        raise ModelSettingsError(f"供应商组 {provider_id!r} 不存在。")
    if group is None and provider:
        group = svc.group_for(user_id=user_id, provider=provider, base_url=base_url)
    catalog = str(group["provider"]) if group else (provider or "openai")
    raw_base = base_url or (str(group["base_url"]) if group and group["base_url"] else None)
    pinned = validate_base_url(raw_base)
    stored_key = str(group["api_key"]) if group and group["api_key"] else None
    return ProbeTarget(
        provider=catalog,
        base_url=pinned,
        api_key=(api_key.strip() or None) if api_key else stored_key,
        model=model,
    )


def list_models(target: ProbeTarget) -> tuple[list[str], str]:
    """拉这个端点的模型列表 → (名字, 错误说明)。成功时错误说明为空串。

    OpenAI 兼容读 `GET /models`，Ollama 原生读 `GET /api/tags`（同一个探活形状，与
    `/api/services/check` 的深度检测一致）。**中转站经常给不全列表**，所以"不在列表里"
    只当提示，不当否决（`model_listed` 是建议性的，见 `probe`）。
    """
    if target.style == "native":
        url, pick = f"{target.root}/api/tags", lambda d: [
            str(m.get("name", "")) for m in d.get("models", [])
        ]
    else:
        url, pick = f"{target.root}/models", lambda d: [
            str(m.get("id", "")) for m in d.get("data", [])
        ]
    try:
        # 走 `base/outbound`：这一发带着**已存的 api_key**，不许经手系统代理（R26-43）。
        res = outbound.get(url, headers=target.auth_headers(), timeout=_LIST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 连不上本身就是结论
        return [], f"{type(exc).__name__}: {exc}"[:180]
    if res.status_code != 200:
        return [], f"HTTP {res.status_code}（{url}）"
    try:
        payload = res.json()
    except ValueError:
        return [], "返回不是 JSON（端点可能不是模型服务）"
    if not isinstance(payload, dict):
        return [], "返回形状不对（没有模型列表字段）"
    return [n for n in pick(payload) if n], ""


def _chat_completion(
    target: ProbeTarget, messages: list[dict[str, object]], *, tools: bool = False
) -> tuple[dict[str, object], str]:
    """一次最小 chat completion → (响应 choices[0] 字典, 错误说明)。"""
    if target.style == "native":
        # Ollama 也服务 OpenAI 兼容层（/v1），但根上不带 /v1：这里显式拼出来。
        url = f"{target.root}/v1/chat/completions"
    else:
        url = f"{target.root}/chat/completions"
    body: dict[str, object] = {
        "model": target.model,
        "messages": messages,
        "max_tokens": 64,
        "temperature": 0,
    }
    if tools:
        body["tools"] = [_PROBE_TOOL]
        body["tool_choice"] = "auto"
    try:
        res = outbound.post(
            url, json=body, headers=target.auth_headers(), timeout=_CALL_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 - 调不通就是结论
        return {}, f"{type(exc).__name__}: {exc}"[:180]
    if res.status_code != 200:
        return {}, f"HTTP {res.status_code}：{res.text[:120]}"
    try:
        payload = res.json()
    except ValueError:
        return {}, "返回不是 JSON"
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return {}, "返回里没有 choices"
    first = choices[0]
    return (first if isinstance(first, dict) else {}), ""


def _message_of(first: dict[str, object]) -> dict[str, object]:
    """choices[0].message（形状不对就当没有 —— 判据会落到"没有 tool_calls / 没有内容"）。"""
    message = first.get("message")
    return message if isinstance(message, dict) else {}


def probe_tool_capability(target: ProbeTarget) -> tuple[bool | None, str]:
    """工具能力：带一个 no-op 工具发一次请求，看它怎么应对。**1 次调用，扣配额。**

    判定刻意偏保守（与 P1-2"只拦确定的否"同一条纪律），因为写回 `False` 有代价：
    那会让 `call_model` 从此不给这个模型绑工具 —— 一个健康模型被一次走运的探测误标成
    "不支持"，症状是"它突然不会用工具了"，比不测更难查。所以只有两种情况才给否：

      * 返回了 `tool_calls` → **True**；
      * **又没内容又没 tool_calls**（空返回）→ **False** —— 这正是要探的那个真实故障
        （某些云端 VLM 一附工具就返回空，用户 2026-09-19 标的第一次是手填的）；
      * 有内容但没调工具 → **None**（不知道：它完全可以只是直接回答）。
    """
    first, error = _chat_completion(
        target, [{"role": "user", "content": _PROBE_TOOL_PROMPT}], tools=True
    )
    if error:
        return None, error
    message = _message_of(first)
    if message.get("tool_calls"):
        return True, ""
    content = str(message.get("content") or "").strip()
    return (False, "") if not content else (None, "")


def probe_vision_with_image(target: ProbeTarget) -> tuple[bool | None, str]:
    """云端视觉：**会向该供应商上传一张 16×16 纯色测试图**（图片离开本机）。

    只在调用方显式要求时执行（`POST /api/settings/models/probe` 的 `test_vision=true`，
    界面上是确认卡里的第二个按钮，不是默认）。判据只有两条硬事实：端点**收不收** image
    （不收 = HTTP 4xx，确定的否），收了之后**有没有回话**（回了 = 大概率能看，`True`）。
    不判"答得对不对"：16 像素纯色图谁都答得对，判对错只会把"能收图"误标成"不能看"。
    """
    first, error = _chat_completion(
        target,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROBE_VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_MINIATURE_PNG}"},
                    },
                ],
            }
        ],
    )
    if error:
        return None, error
    text = str(_message_of(first).get("content") or "").strip()
    return bool(text), ""


def _minimal_png_b64() -> str:
    """一张 16×16 纯红 PNG（base64，约 90 字节）——现场构造，不放一个没人看得懂的 blob。

    为什么这么小：探测要的是"这个端点收不收图片、收之后答不答话"，不是识别质量。
    图片越大，"顺手把用户内容发出去"的担忧越成立 —— 探测用的图必须小到没有任何想象空间。
    """
    width = height = 16
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        crc = zlib.crc32(body) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + body + struct.pack(">I", crc)

    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    ).decode("ascii")


_MINIATURE_PNG = _minimal_png_b64()


def probe(
    target: ProbeTarget,
    *,
    test_tools: bool = False,
    test_vision: bool = False,
) -> ProbeResult:
    """跑一轮探测：免费的永远做，花钱的按参数点名做（界面在点之前要把代价写在按钮边上）。

    本地（native/Ollama）的视觉结论来自 `/api/show` 元数据 —— **免费且不出机**，所以本地
    永远走这条，即使显式要求 `test_vision` 也不会上传图片；云端要测视觉就必须真发一张
    16×16 测试图，那是红线动作，只有确认卡里点第二下才会发生。
    """
    names, error = list_models(target)
    if error:
        return ProbeResult(reachable=False, detail=error, vision_source="not-tested")
    listed: bool | None = None
    if names:
        wanted = target.model.lower()
        got = {n.lower() for n in names}
        listed = wanted in got or wanted.split(":")[0] in {n.split(":")[0] for n in got}
    notes: list[str] = []
    if listed is False:
        notes.append("模型不在这个端点返回的列表里（中转站经常给不全，确认名称无误就能继续）")

    calls = 0
    tools: bool | None = None
    vision: bool | None = None
    vision_source = "not-tested"
    if target.style == "native":
        vision, vision_source = vision_capability(target.root, target.model), "free-metadata"
    elif test_vision:
        vision, vdetail = probe_vision_with_image(target)
        calls += 1
        vision_source = "uploaded-image"
        if vdetail:
            notes.append(f"视觉探测失败：{vdetail}")
    if test_tools:
        tools, tdetail = probe_tool_capability(target)
        calls += 1
        if tdetail:
            notes.append(f"工具探测失败：{tdetail}")
    return ProbeResult(
        reachable=True,
        detail="；".join(notes),
        model_listed=listed,
        tools=tools,
        vision=vision,
        vision_source=vision_source,
        calls_used=calls,
        models=names[:50],
    )
