"""`_init_model` 的 kwargs 组装契约（不触真实 provider，monkeypatch init_chat_model）。

锁住 num_ctx 的传递语义：**只有** native（Ollama）风格且配置了 num_ctx 才传——
云端窗口由服务商固定，传了是噪音；而不传的后果是 Ollama 只用 2048 窗口静默截断历史。
"""

from __future__ import annotations

from typing import Any

import pytest

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core import graph

captured: dict[str, Any] = {}


def _fake_init(**kwargs: Any) -> object:
    captured.clear()
    captured.update(kwargs)
    return object()


@pytest.fixture(autouse=True)
def _patch_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)


def _settings(num_ctx: int | None) -> Settings:
    return Settings(
        model_default="a",
        model_backends={
            "a": ModelBackend(model="m-a", provider="ollama", num_ctx=num_ctx),
        },
    )


def test_ollama_backend_passes_num_ctx() -> None:
    graph._init_model(_settings(8192), "a")
    assert captured["model_provider"] == "ollama"
    assert captured["num_ctx"] == 8192


def test_ollama_without_num_ctx_omits_the_kwarg() -> None:
    graph._init_model(_settings(None), "a")
    assert "num_ctx" not in captured  # 未配置 = 引擎默认，不要凭空补参数


def test_cloud_backend_never_gets_num_ctx() -> None:
    s = Settings(
        model_default="c",
        model_backends={
            "c": ModelBackend(
                model="deepseek-chat", provider="siliconflow", num_ctx=8192
            ),
        },
    )
    graph._init_model(s, "c")
    assert captured["model_provider"] == "openai"
    assert "num_ctx" not in captured  # 云端窗口固定，传了也会被忽略
