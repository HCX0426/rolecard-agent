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
    """云端走**直接构造**的捕获子类（不走 init_chat_model，2026-10-31 思考捕获改造）：
    断言对象属性而不是构造 kwargs —— `model_provider` 这个内部键已经不进构造参数。"""
    s = Settings(
        model_default="c",
        model_backends={
            "c": ModelBackend(
                model="deepseek-chat",
                provider="siliconflow",
                num_ctx=8192,
                api_key="sk-test",
            ),
        },
    )
    obj = graph._init_model(s, "c")
    assert "num_ctx" not in (obj.model_kwargs or {})  # 云端窗口固定，传了也会被忽略
    assert not hasattr(obj, "num_ctx")


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
    obj = graph._init_model(_one(provider="siliconflow", api_key="sk-test", **_SAMPLING), "a")
    assert "repeat_penalty" not in (obj.model_kwargs or {})
    assert obj.frequency_penalty == 0.1 and obj.presence_penalty == 0.2


# ------------------------------------------------------------------ 思考开关联动
#
# 2026-10-31 用户报"对话界面上看不到思考过程了"。根因有两半：
#   ① 思考开关从前只对 native（Ollama）发 —— OpenAI 兼容路径从不发 `enable_thinking`，
#      SiliconFlow 的思考静默关闭（实测：不传 → reasoning_tokens:0，连 reasoning_content
#      增量都没有；传了才有）；
#   ② langchain-openai 不映射 `reasoning_content` 增量 → 捕获子类补（openai_reasoning.py）。
# 名单护栏语义不变：名单外的模型不支持思考，加进去会 400，责任在名单。


def _cloud(model: str, **over: Any) -> Settings:
    return Settings(
        model_default="c",
        model_backends={
            "c": ModelBackend(model=model, provider="siliconflow", api_key="sk-test", **over)  # type: ignore[arg-type]
        },
    )


def test_cloud_thinking_model_gets_enable_thinking_switch() -> None:
    """名单内的 OpenAI 兼容模型：构造出捕获子类，且请求体带上 enable_thinking。"""
    s = _cloud("deepseek-ai/DeepSeek-V4-Flash")
    s = s.model_copy(update={"model_thinking_models": ["deepseek-ai/DeepSeek-V4-Flash"]})
    obj = graph._init_model(s, "c")
    from rolecard_agent.core.agent.openai_reasoning import ReasoningAwareChatOpenAI

    assert isinstance(obj, ReasoningAwareChatOpenAI)
    # 开关走 SDK 的 extra_body（并进请求体）；model_kwargs 装它会被当方法参数展开而炸。
    assert (obj.extra_body or {}).get("enable_thinking") is True


def test_cloud_non_thinking_model_stays_plain() -> None:
    """名单外的模型：请求体不带开关（护栏语义 —— 传了会 400）。捕获子类对所有
    OpenAI 兼容模型统一使用（行为纯增量），"plain" 指的是不发开关。"""
    s = _cloud("some/plain-model")
    obj = graph._init_model(s, "c")
    assert not (obj.extra_body or {})


def test_cloud_thinking_off_disables_the_switch_even_for_listed_models() -> None:
    """总闸 off：名单内的模型也不发开关 —— 是"把它藏起来"，不是"把它停掉"。"""
    s = _cloud("deepseek-ai/DeepSeek-V4-Flash")
    s = s.model_copy(
        update={"model_thinking": "off", "model_thinking_models": ["deepseek-ai/DeepSeek-V4-Flash"]}
    )
    obj = graph._init_model(s, "c")
    assert not (obj.extra_body or {})
