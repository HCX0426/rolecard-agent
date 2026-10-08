"""`_init_model` 的 kwargs 组装契约（不触真实 provider，monkeypatch init_chat_model）。

锁住 num_ctx 的传递语义：**只有** native（Ollama）风格且配置了 num_ctx 才传——
云端窗口由服务商固定，传了是噪音；而不传的后果是 Ollama 只用 2048 窗口静默截断历史。
"""

from __future__ import annotations

from typing import Any

import pytest

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.agent import graph

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


# ------------------------------------------------------------------ 采样惩罚三栏
#
# 与 temperature 同一条铁律：**构造期**传（调用期 bind 会被 ChatOllama 放进请求顶层，
# Ollama 忽略）。这三条锁的是"传法"和"云端那一栏根本不出现"两件事。

_SAMPLING = {"repeat_penalty": 1.2, "frequency_penalty": 0.1, "presence_penalty": 0.2}


def _one(**fields: Any) -> Settings:
    return Settings(
        model_default="a",
        model_backends={"a": ModelBackend(model="m-a", **fields)},  # type: ignore[arg-type]
    )


def test_penalties_are_passed_at_construction_for_native() -> None:
    graph._init_model(_one(provider="ollama", **_SAMPLING), "a")
    assert captured["model_provider"] == "ollama"
    assert {k: captured[k] for k in _SAMPLING} == _SAMPLING


def test_unset_penalties_are_omitted_not_zeroed() -> None:
    """三个都没设 = **一个都不传**。Ollama 出厂 repeat_penalty=1.1，替它写 0 就是悄悄关掉它，
    而界面上那一栏显示的是"未设置"—— 那就是"界面说的和发出去的不是一回事"。"""
    graph._init_model(_one(provider="ollama"), "a")
    assert not (set(_SAMPLING) & set(captured))


def test_cloud_client_never_receives_repeat_penalty() -> None:
    """OpenAI 兼容体没有 repeat_penalty 这个标准字段。写侧 `set_sampling` 挡一道，这里是第二道
    —— env 里手写的 MODEL_BACKENDS 不经那道闸门，而它一旦漏进请求就是各家服务商行为不一。"""
    graph._init_model(_one(provider="siliconflow", **_SAMPLING), "a")
    assert captured["model_provider"] == "openai"
    assert "repeat_penalty" not in captured
    assert captured["frequency_penalty"] == 0.1 and captured["presence_penalty"] == 0.2
