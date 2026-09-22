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


def test_reasoning_false_overrides_the_thinking_allowlist() -> None:
    """调用方按用途要"不思考"时，`MODEL_THINKING_MODELS` 名单让位。

    两者问的不是一个问题：名单 = "这个模型值不值得给它开思考"（对话侧要看思考过程），
    这个参数 = "这一次调用要不要付思考的代价"（主动开口只是要一句问候）。
    真机实测的代价：qwen3-vl 为一句 30 字问候想了 5919 token / 221 秒，
    还常把 num_ctx=8192 撑到 `done_reason=length`、正文一个字不留。
    """
    s = Settings(
        model_default="a",
        model_backends={"a": ModelBackend(model="qwen3-vl:8b", provider="ollama")},
        model_thinking_models=["qwen3-vl:8b"],
    )
    graph._init_model(s, "a")
    assert captured["reasoning"] is True  # 名单内：默认开
    captured.clear()
    graph._init_model(s, "a", reasoning=False)
    assert captured["reasoning"] is False


def test_reasoning_false_is_not_sent_to_cloud_backends() -> None:
    """与 num_ctx 同纪律：`reasoning` 是 Ollama 客户端的参数，云端不传（传了是噪音/400）。"""
    s = Settings(
        model_default="c",
        model_backends={"c": ModelBackend(model="deepseek-chat", provider="deepseek")},
    )
    graph._init_model(s, "c", reasoning=False)
    assert captured["model_provider"] == "openai"
    assert "reasoning" not in captured
