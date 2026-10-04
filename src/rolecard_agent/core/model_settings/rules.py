from __future__ import annotations

import re
from urllib.parse import urlparse

from rolecard_agent.config import DEFAULT_SILICONFLOW_BASE_URL


class ModelSettingsError(Exception):
    """A settings write that would produce an unusable configuration. Message is user-safe."""


def validate_base_url(value: str | None) -> str | None:
    """归一并校验 base_url：仅接受 http/https，允许 localhost/私网（自用场景 Ollama 需要）。

    拒绝 file:///gopher 等异常 scheme 与无 scheme 的裸串，避免后续 httpx 把内容当请求发走
    （M8：base_url 此前无校验，错误地址可作内网探测入口）。空值视作"留空/自动"（如 Ollama）。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if "://" not in lowered:
        raise ModelSettingsError("base_url 必须包含协议（如 http:// 或 https://）。")
    parsed = urlparse(lowered)
    if parsed.scheme not in ("http", "https"):
        raise ModelSettingsError(f"base_url 仅支持 http/https，不支持 {parsed.scheme}://")
    if not parsed.hostname:
        raise ModelSettingsError("base_url 缺少主机名。")
    return text


# Backend names become keys in MODEL_BACKENDS-merged maps and UI list items: keep them tame.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# 供应商目录：设置页「模型」页签的下拉不再写死前端，改由后端提供（动态扩展）。
# id 是**供应商身份**（界面分组/展示用），style 才是运行时客户端风格：
#   native = Ollama 原生端点（base_url 不带 /v1）；openai = OpenAI 兼容（带 /v1）。
# 两者刻意分离：同一"OpenAI 兼容"风格下有很多厂商（SiliconFlow/DeepSeek/…），把厂商
# 写进 provider 才能让界面正确显示"硅基流动"，而不是一句无意义的"openai"。
MODEL_PROVIDERS: tuple[dict[str, object], ...] = (
    {
        "id": "ollama",
        "label": "本地 Ollama",
        "needs_key": False,
        "base_url_hint": "http://localhost:11434（可留空）",
        "style": "native",
        "default_base_url": "http://localhost:11434",
    },
    {
        "id": "openai",
        "label": "OpenAI 兼容",
        "needs_key": True,
        "base_url_hint": "https://api.openai.com/v1",
        "style": "openai",
        "default_base_url": "https://api.openai.com/v1",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动",
        "needs_key": True,
        # 端点不写第二遍（`R28-14`）：这格曾经是该 URL 在 src 里的第二处字面量，而它就在
        # `default_base_url` 隔壁一行 —— 换端点时静静留下一句过期的占位。由 single-source
        # literals 那条门禁看着。
        "base_url_hint": DEFAULT_SILICONFLOW_BASE_URL,
        "style": "openai",
        "default_base_url": DEFAULT_SILICONFLOW_BASE_URL,
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "needs_key": True,
        "base_url_hint": "https://api.deepseek.com/v1",
        "style": "openai",
        "default_base_url": "https://api.deepseek.com/v1",
    },
)

# 留空 base_url 的厂商 = 用它自己的默认端点。分组必须按**归一后的端点**算：否则
# "硅基流动 + 留空" 与 "硅基流动 + 显式 URL" 会成两个组，于是那把 key 又有了两个家 ——
# 正是拆层要消灭的东西（用户在旧界面加第二个模型时不重填 URL，这条路径天天会走到）。
_DEFAULT_BASE_URLS = {str(p["id"]): str(p["default_base_url"]) for p in MODEL_PROVIDERS}

# 历史 alias：旧数据/旧配置里的 "local" 一律视作 ollama（不再作为可选供应商出现）。
PROVIDER_ALIASES = {"local": "ollama"}

KEYLESS_PROVIDERS = frozenset({"ollama", "local"})

# 一行模型可以参与的服务。模型页**不再选用途**：它与其余三类一样是「服务」页签的一条引用
# 行，模型页只读回显 `used_by`（用途只有一个事实面）。
BACKEND_USAGES = frozenset({"chat", "embedding", "rerank", "ocr"})

# 「模型推理」在 service_endpoint 里的类别键。它刻意**不进** `SERVICE_CATEGORIES`（那三类各有
# 内置本地行、服务页可自由增删引用）：chat 引用只由本模块写，通用 REST 面
# （/api/services/{key}/...）因此碰不到它 —— 少一个能写出半套语义的入口。
CHAT_CATEGORY = "chat"

# `used_by` 的展示序：对话在前，其余按服务页的出现顺序。
USAGE_DISPLAY_ORDER = ("chat", "embedding", "rerank", "ocr")

# 没有被任何服务引用的一行模型。曾经它叫 "chat"（旧列的默认值），但那是撒谎：没人用它，
# 而消费方（对话页/角色页的后端下拉）会因为它是 "chat" 把它列进去。"还没配用途"是个真实
# 状态（刚添加的模型就是还没被任何服务引用），所以它需要自己的值。
UNASSIGNED_USAGE = "unassigned"


def _canonical(provider: str) -> str:
    p = (provider or "").strip().lower()
    return PROVIDER_ALIASES.get(p, p)


def normalize_provider(provider: str, base_url: str | None = None) -> str:
    """把历史/风格性 provider 值归一到供应商目录 id。

    此前云端种子只记端点风格（SiliconFlow 存成 "openai"），界面因此显示错误的供应商。
    这里按 alias 折叠 + base_url 厂商特征推断；识别不出就原样保留（自定义网关仍算 openai）。
    """
    p = _canonical(provider)
    url = (base_url or "").lower()
    if p in ("", "openai"):
        if "siliconflow" in url:
            return "siliconflow"
        if "deepseek" in url:
            return "deepseek"
    return p or "openai"


def client_style(provider: str) -> str:
    """供应商 id → 运行时客户端风格：native 走 Ollama 原生，其余走 OpenAI 兼容。

    `init_chat_model` 只认 "ollama"/"openai" 两类 provider；目录化之后界面上的
    siliconflow/deepseek 都映射到 openai 兼容客户端（base_url 指向各自厂商）。
    """
    p = _canonical(provider)
    for entry in MODEL_PROVIDERS:
        if entry["id"] == p:
            return str(entry["style"])
    return "openai"


def is_keyless_provider(provider: str) -> bool:
    """本地类 provider（Ollama 及其别名 local）不需要 api_key。"""
    return _canonical(provider) in KEYLESS_PROVIDERS


def provider_catalog() -> list[dict[str, object]]:
    """返回供应商目录（前端下拉用）。新增供应商只改这里，无需动前端。

    值类型是 object：`needs_key` 是布尔（`R102-16`），与其余 str 字段同表。"""
    return [dict(p) for p in MODEL_PROVIDERS]


def provider_label(provider: str) -> str:
    """供应商 id 的展示名；目录外的自定义网关显示 id 本身（不编一个假名字）。"""
    p = _canonical(provider)
    for entry in MODEL_PROVIDERS:
        if entry["id"] == p:
            return str(entry["label"])
    return p


def mask_key(raw: str | None) -> str | None:
    """回读的**掩码**密钥（如 `sk-…abcd`）；不足 9 字符全打点。

    `raw[:3]…raw[-4:]` 对 7 字符的 key 等于把整个 key 拼回来，掩码就成了回明文
    （审查报告 L2）。与 `core/services._mask_key` 同一纪律（那里是引用行的快照）。
    """
    if not raw:
        return None
    text = str(raw)
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:3]}…{text[-4:]}"
