"""多模型比对工具（core/consensus.py，用户 2026-09-17 开工）的单元测试。

全离线：build 注入假模型，验证并行收集、聚合接线、缺席标注与"单后端无从比对"。
"""

from __future__ import annotations

from typing import Any

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.consensus import build_consensus_tool


class FakeLLM:
    """记录 prompt、回放固定 content 的假模型（比对只消费 .content）。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        return self


def _settings(*, fallbacks: list[str]) -> Settings:
    return Settings(
        model_default="a",
        model_fallbacks=fallbacks,
        model_backends={
            "a": ModelBackend(model="m-a", provider="ollama"),
            "b": ModelBackend(model="m-b", provider="ollama"),
        },
    )


def test_consensus_aggregates_via_default_backend() -> None:
    fakes = {"a": FakeLLM("聚合结论：两后端一致"), "b": FakeLLM("回答B")}
    tool = build_consensus_tool(settings=_settings(fallbacks=["b"]), build=lambda n: fakes[n])

    out = tool.invoke({"question": "结石 6mm 严重吗"})

    assert "参与比对的后端：a、b" in out
    assert "聚合结论：两后端一致" in out  # 聚合器（默认后端 a）的产出进了结果
    # 参与者 b 收到的是**问题本身**；聚合器 a 收到的是**问题 + 各后端回答**。
    assert fakes["b"].prompts[0] == "结石 6mm 严重吗"
    assert fakes["a"].prompts[-1].startswith("你是事实核查员")
    assert "【b】" in fakes["a"].prompts[-1] and "回答B" in fakes["a"].prompts[-1]


def test_consensus_marks_failed_backend() -> None:
    fakes = {"a": FakeLLM("结论仅基于 a")}

    def build(name: str) -> Any:
        if name == "b":
            raise RuntimeError("connection refused")
        return fakes[name]

    tool = build_consensus_tool(settings=_settings(fallbacks=["b"]), build=build)
    out = tool.invoke({"question": "q"})

    assert "缺席后端：b" in out
    assert "RuntimeError" in out  # 类型名可见，细节不外泄


def test_consensus_with_single_backend_explains() -> None:
    tool = build_consensus_tool(settings=_settings(fallbacks=[]), build=lambda n: FakeLLM("x"))
    out = tool.invoke({"question": "q"})
    assert "无从比对" in out
