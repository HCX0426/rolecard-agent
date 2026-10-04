"""工具循环必须有上界（P0，审查报告 2026-09-17）。

问题：全项目从未设置 `recursion_limit`（`grep -rn recursion src/ tests/` 为空），
LangGraph 于是用默认的 **10007**。而本图是 `model -> tools -> model` 的环：
模型只要持续返回 tool_calls（提示注入、工具反复报错被重试、7B 模型钻进死胡同），
这一轮就**永远不会终止** —— 云端后端等于数千次真实计费调用，SSE 长时间无响应
且界面没有中断理由。

这一层钉三件事：
1. `build_graph_config` 一定带上 limit（默认值与 `Settings.agent_max_steps` 同源）；
2. 真的跑一个"永远要求调工具"的模型时，图**因超限停下**，而不是跑满库默认；
3. 停下来时给用户的是一句能照着做事的话，不是笼统的"模型调用失败"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError

from rolecard_agent.api.chat import sse
from rolecard_agent.base.observability import NullTracer
from rolecard_agent.config import Settings
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.graph import DEFAULT_AGENT_MAX_STEPS, build_graph_config, build_kernel
from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.state import new_state
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.core.turn import run_turn
from rolecard_agent.roles.models import RoleCardCreate
from rolecard_agent.roles.service import RoleCards, RoleCardService
from rolecard_agent.storage.db import bootstrap, connect
from tests.conftest import list_roles


def cards(store: RoleCardService) -> RoleCards:
    """测试里"本机主人眼里的那些卡"的简写（M2a 之后每次读写都得说清为谁）。"""
    return store.scoped("u1")

class _LoopingChat:
    """永远要求调用同一个工具的假模型：模拟"模型陷入了重复调用"。

    `ScriptedChat` 做不到这一点 —— 它的脚本放完就返回普通消息，循环会自然结束，
    于是"没有上界"这件事在测试里根本不会暴露。
    """

    def __init__(self) -> None:
        self.rounds = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> _LoopingChat:
        return self

    def invoke(self, prompt: Any, **kwargs: Any) -> AIMessage:  # noqa: A002
        self.rounds += 1
        return AIMessage(
            content="",
            tool_calls=[{"name": "list_roles", "args": {}, "id": f"c{self.rounds}"}],
        )

    def stream(self, prompt: Any, **kwargs: Any) -> Any:  # noqa: A002
        yield self.invoke(prompt, **kwargs)


class _RecursingGraph:
    """`.stream` 直接抛超限：把"翻译成人话"那一段单独钉住，不必真跑 25 步。"""

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise GraphRecursionError("Recursion limit of 25 reached without hitting a stop condition")


class _RecordingTracer:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [getattr(e, "event", "") for e in self.events]


def _kernel(db: Path, model: Any) -> Any:
    conn = connect(db)
    bootstrap(conn)
    roles = RoleCardService(conn)
    cards(roles).create(
        RoleCardCreate(
            role_id="r",
            role_name="循环测试角色",
            system_prompt="x",
            tool_whitelist=["list_roles"],
            model_name=None,
        )
    )
    plugins = PluginService(conn, known_plugins=[])
    reg = ToolRegistry()
    reg.register(list_roles)
    return build_kernel(
        model=model,
        registry=reg,
        roles=roles,
        tracer=NullTracer(),
        settings=Settings(),
        checkpointer=make_checkpointer(conn),
        plugins=plugins,
    )


def _state() -> dict[str, Any]:
    return {
        **new_state(thread_id="t", user_id="u1", current_role_id="r"),
        "messages": [HumanMessage(content="一直调工具试试")],
    }


# -- 1) 运行配置必须带上限 -----------------------------------------------------


def test_default_config_carries_a_step_limit() -> None:
    config = build_graph_config("t")

    assert config["configurable"] == {"thread_id": "t"}
    assert config["recursion_limit"] == DEFAULT_AGENT_MAX_STEPS
    # 库默认是 10007；拿了它就是没设，等于把上界交给了模型的心情。
    assert config["recursion_limit"] < 10007


def test_limit_comes_from_settings_and_can_be_relaxed_explicitly() -> None:
    assert build_graph_config("t", Settings(agent_max_steps=7))["recursion_limit"] == 7
    # <=0 = 显式退回库默认（仅调试用）：此时不该再塞这个键，否则语义变成"上限 0"
    assert "recursion_limit" not in build_graph_config("t", Settings(agent_max_steps=0))


def test_agent_mode_doubles_the_step_limit() -> None:
    """智能体模式 = 多步自主任务：上限翻倍（有界放大，熔断语义仍成立）。"""
    base = build_graph_config("t", Settings(agent_max_steps=25))["recursion_limit"]
    grown = build_graph_config("t", Settings(agent_max_steps=25), agent_mode=True)[
        "recursion_limit"
    ]
    assert grown == base * 2
    # 关闭上限（0=库默认）时不放大：`0 * 2` 会被当成"显式关"以外的神秘值
    assert "recursion_limit" not in build_graph_config(
        "t", Settings(agent_max_steps=0), agent_mode=True
    )


# -- 2) 真的会停下 -------------------------------------------------------------


def test_looping_tool_calls_are_stopped_by_the_step_limit(tmp_path: Path) -> None:
    model = _LoopingChat()
    graph = _kernel(tmp_path / "app.db", model)

    with pytest.raises(GraphRecursionError):
        graph.invoke(_state(), config=build_graph_config("t", Settings(agent_max_steps=5)))

    # 停在 5 步附近，而不是跑满 10007 —— 后者在真实后端上就是几千次计费调用。
    assert 2 <= model.rounds <= 4


# -- 3) 用户看到的是"怎么绕开"，不是"模型调用失败" ------------------------------


def test_recursion_limit_reaches_the_user_as_an_actionable_sentence() -> None:
    tracer = _RecordingTracer()
    # 轮次语义在 core/turn.py，帧化在 api/chat.py —— 这条用例要的是"用户看到的那句话"，
    # 所以走完整链路（事件 → SSE 帧），而不是只拿内核事件。
    events = [
        sse(e)
        for e in run_turn(
            _RecursingGraph(),
            graph_input={},
            config=build_graph_config("t", Settings(agent_max_steps=25)),
            role_summary={"role_id": "r", "role_name": "循环测试角色"},
            tracer=tracer,
        )
    ]
    payloads = [
        json.loads(e.removeprefix("data: ").strip()) for e in events if e.startswith("data:")
    ]

    errors = [p for p in payloads if p["type"] == "error"]
    assert errors, "超限必须让用户看得见"
    detail = errors[0]["detail"]
    assert "步" in detail and "上限" in detail
    assert "模型调用失败" not in detail, "模型一直在正常回话，说'调用失败'会把排查方向带偏"
    # 事件流仍以 end 收尾：前端靠它把"生成中"复位（否则输入框永远停用）
    assert payloads[-1]["type"] == "end"
    # 审计要能区分"超限"与其它失败
    assert tracer.kinds().count("chat_error") == 1
    assert getattr(tracer.events[0], "detail", {}).get("reason") == "recursion_limit"
