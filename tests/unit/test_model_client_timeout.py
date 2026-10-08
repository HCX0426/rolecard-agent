"""超时必须**真的落到客户端上**，不是"我们传了"就算数（P0，审查报告 2026-09-17）。

为什么单独一个文件：`tests/unit/test_model_kwargs.py` 把 `init_chat_model` 换成了假函数，
只能证明 kwargs 字典里有什么。而这条 bug 恰恰是"传了但客户端不认"——
`ChatOllama` 没有 `timeout` 字段，参数被**静默丢弃**：

    ChatOllama(model=..., timeout=120)        → client_kwargs={}      → httpx Timeout(None)
    ChatOllama(model=..., client_kwargs={...}) → client_kwargs={'timeout': 120} → httpx Timeout(120)

后果不是"慢一点"：本地模型挂起（显存不足 / 引擎换入换出 / 服务假死）时 SSE 与
`with_fallbacks` 会一起永久等待，`_CHAT_POOL` 的 8 个线程被逐个占死 → 整个对话服务拖停。

这里**不 patch 任何东西**：直接构造真实客户端并读底层 httpx 超时值（不发任何请求）。
"""

from __future__ import annotations

import httpx

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.agent import graph


def _settings(provider: str, **backend_kwargs: object) -> Settings:
    return Settings(
        model_default="a",
        model_backends={
            "a": ModelBackend(model="m-a", provider=provider, **backend_kwargs),  # type: ignore[arg-type]
        },
    )


def _http_client_timeout(model: object) -> httpx.Timeout:
    """取出 ChatOllama 底层 httpx 客户端的超时（langchain_ollama 的 client 是懒建的）。"""
    ollama_client = model._client  # type: ignore[attr-defined]
    return ollama_client._client.timeout  # type: ignore[attr-defined,no-any-return]


def test_ollama_client_really_receives_the_configured_timeout() -> None:
    model = graph._init_model(_settings("ollama"), "a")

    timeout = _http_client_timeout(model)

    # 用 is-None 而不是断言具体数值：读不出超时才是这条 P0 的原始症状。
    assert timeout.read == 120, (
        f"本地后端的超时没有生效（底层 httpx = {timeout}）——"
        "模型挂起时这一轮会永久等待，回退链也触发不了"
    )
    assert timeout.connect == 120


def test_cloud_backend_still_uses_its_own_timeout_parameter() -> None:
    """云端走的是另一条装配路径，别在修本地时把它弄坏。"""
    model = graph._init_model(
        _settings("siliconflow", base_url="https://api.example.com/v1", api_key="sk-x"), "a"
    )

    assert model.request_timeout == 120  # type: ignore[attr-defined]


def test_no_timeout_configured_leaves_the_client_alone() -> None:
    """0 = 显式不设超时（仅调试用）：不该凭空补一个值。"""
    settings = Settings(
        model_default="a",
        model_backends={"a": ModelBackend(model="m-a", provider="ollama")},
        model_timeout_seconds=0,
    )
    model = graph._init_model(settings, "a")

    assert graph._init_model(settings, "a").client_kwargs == {}  # type: ignore[attr-defined]
    assert _http_client_timeout(model).read is None
