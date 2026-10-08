"""Unit tests for the graph nodes.  Traceability: US-1, US-2, US-3.

Integration tests exercise the happy path through a real compiled graph; these pin the
decisions that are cheap to get wrong and expensive to notice: when to loop back into the
tools, which tools a turn may see, how a failing tool is retried, and how much history is
allowed into the prompt.

See 技术评审与决策.md §9 D2 - these had no unit coverage before.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.base.observability import NullTracer
from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.agent.nodes import (
    MAX_TOOL_RETRIES,
    TOOL_DENIED,
    TOOL_FAILED,
    TOOL_LOOP_BREAK,
    TOOL_OFFLINE,
    KernelContext,
    VisionNotSupported,
    _latest_image_data_url,
    call_model,
    execute_tools,
    route_after_model,
    trim_history,
    turn_context,
)
from rolecard_agent.core.agent.prompts import VOICE_DEPTH_PROMPT
from rolecard_agent.core.telemetry import probes
from rolecard_agent.core.tools.errors import ToolExecutionError  # noqa: F401 - 文档化分界用
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.core.tools.web import WebToolError
from rolecard_agent.roles.models import RoleCardCreate
from rolecard_agent.roles.service import RoleCards, RoleCardService

# --------------------------------------------------------------------------- routing


def cards(store: RoleCardService) -> RoleCards:
    """测试里"本机主人眼里的那些卡"的简写（M2a 之后每次读写都得说清为谁）。"""
    return store.scoped(DEFAULT_USER_ID)

def test_route_ends_when_the_model_answers_directly() -> None:
    state = {"messages": [HumanMessage(content="hi"), AIMessage(content="hello")]}
    assert route_after_model(state) == "end"


def test_route_loops_when_the_model_asks_for_a_tool() -> None:
    state = {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]),
        ]
    }
    assert route_after_model(state) == "tools"


def test_route_ends_on_an_empty_tool_call_list() -> None:
    """Some providers emit `tool_calls: []` instead of omitting it - that is not a call."""
    state = {"messages": [AIMessage(content="answer", tool_calls=[])]}
    assert route_after_model(state) == "end"


# --------------------------------------------------------------------------- tool visibility


@tool
def kernel_tool() -> str:
    """A kernel tool."""
    return "k"


@tool
def domain_tool() -> str:
    """A domain tool."""
    return "d"


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(kernel_tool)
    reg.register(domain_tool, domain="dom")
    return reg


def _ctx(
    registry: ToolRegistry,
    roles: RoleCardService,
    model: Any = None,
    *,
    enabled_domains: Sequence[str] = (),
    tool_epoch: int = 1,
    tracer: Any = None,
    model_resolver: Any = None,
) -> KernelContext:
    # `enabled_domains` / `tool_epoch` are callables on KernelContext (read live each turn),
    # so the test must supply them here rather than stuffing `enabled_domains` into state -
    # turn_context deliberately ignores the state copy and reads the callable.
    return KernelContext(
        model=model,
        registry=registry,
        roles=roles,
        tracer=tracer or NullTracer(),
        settings=Settings(),
        enabled_domains=lambda: list(enabled_domains),
        tool_epoch=lambda: tool_epoch,
        model_resolver=model_resolver,
    )


def test_turn_context_honours_both_stages(registry: ToolRegistry, roles: RoleCardService) -> None:
    cards(roles).create(
        RoleCardCreate(
            role_id="narrow", role_name="窄", system_prompt="x", tool_whitelist=["domain_tool"]
        )
    )
    state = {"current_role_id": "narrow"}
    tools, _domains = turn_context(state, _ctx(registry, roles, enabled_domains=["dom"]))
    assert [t.name for t in tools] == ["domain_tool"]


def test_unknown_role_yields_no_tools(registry: ToolRegistry, roles: RoleCardService) -> None:
    """A session pointing at a deleted role must not fall through to 'everything allowed'."""
    state = {"current_role_id": "ghost", "enabled_domains": ["dom"]}
    with pytest.raises(Exception, match="ghost"):
        turn_context(state, _ctx(registry, roles))


def test_plugin_switched_off_mid_turn_is_reported_offline_not_denied(
    registry: ToolRegistry, roles: RoleCardService, wide_role: str
) -> None:
    """P1-9：bind 时插件还开着（录制值 `enabled_domains=["dom"]`），执行时已经被关掉。
    真实答案是 **offline（这个插件关了）**，不是 denied（这个角色没权限）—— 给用户错的
    那一句比不给解释更糟。两个集合必须来自同一次实时读取。"""
    state = _state_with_call("domain_tool", wide_role)
    state["enabled_domains"] = ["dom"]  # 上一轮 bind 写下的历史记录，故意与实时值不一致
    out = execute_tools(state, _ctx(registry, roles, enabled_domains=[]))
    assert out["messages"][0].content == TOOL_OFFLINE


# --------------------------------------------------------------------------- tool retry


def _flaky_tool(fail_times: int) -> tuple[Any, list[int]]:
    calls = [0]

    @tool("flaky")
    def flaky() -> str:
        """Fails a fixed number of times, then succeeds."""
        calls[0] += 1
        if calls[0] <= fail_times:
            raise RuntimeError("transient")
        return "ok"

    return flaky, calls


def _state_with_call(name: str, role_id: str = "wide") -> dict[str, Any]:
    return {
        "messages": [AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": "c1"}])],
        "current_role_id": role_id,
        "enabled_domains": [],
        "thread_id": "t1",
    }


@pytest.fixture
def wide_role(roles: RoleCardService) -> str:
    """A role with `tool_whitelist=None`, i.e. every enabled tool is permitted.

    Retry and routing tests must not silently double as permission-filter tests: the built-in
    role has an explicit whitelist, so a tool invented inside a test would be denied before it
    was ever invoked - which is exactly what happened the first time these were written.
    """
    cards(roles).create(
        RoleCardCreate(role_id="wide", role_name="宽", system_prompt="x", tool_whitelist=None)
    )
    return "wide"


def test_transient_failure_is_retried_then_succeeds(roles: RoleCardService, wide_role: str) -> None:
    flaky, calls = _flaky_tool(fail_times=1)
    reg = ToolRegistry()
    reg.register(flaky, idempotent=True)  # 重试只对显式声明幂等的工具开放
    out = execute_tools(_state_with_call("flaky", wide_role), _ctx(reg, roles))

    assert calls[0] == 2
    assert out["messages"][0].content == "ok"
    assert out["retry_count"] == 1


def test_retry_is_bounded(roles: RoleCardService, wide_role: str) -> None:
    """A permanently broken tool must not be retried forever."""
    flaky, calls = _flaky_tool(fail_times=99)
    reg = ToolRegistry()
    reg.register(flaky, idempotent=True)
    out = execute_tools(_state_with_call("flaky", wide_role), _ctx(reg, roles))

    assert calls[0] == MAX_TOOL_RETRIES + 1
    assert out["messages"][0].content == TOOL_FAILED
    assert out["retry_count"] == MAX_TOOL_RETRIES


# -- 工具循环熔断（审查报告 P0-1 的配套 / 评测 authz-001 暴露） -----------------------


def _history_with_repeated_calls(times: int, *, human_last: bool = False) -> list[Any]:
    """构造「同一工具同参数连续调用 times 次」的历史，可选在末尾再补一句用户消息。"""
    history: list[Any] = [HumanMessage(content="问")]
    for i in range(times):
        history.append(
            AIMessage(content="", tool_calls=[{"name": "flaky", "args": {}, "id": f"c{i}"}])
        )
        history.append(ToolMessage(content="x", tool_call_id=f"c{i}", name="flaky"))
    if human_last:
        history.append(HumanMessage(content="再查一次"))
    history.append(AIMessage(content="", tool_calls=[{"name": "flaky", "args": {}, "id": "c9"}]))
    return history


def test_repeated_identical_calls_break_the_loop(roles: RoleCardService, wide_role: str) -> None:
    """同一个工具（同参数）连续调用到上限 → 不再执行，回「请直接回答」。

    实测 8B 模型在「插件停用」用例里会幻觉式地反复调用同一个被拒工具，每次约 12s，
    一直转到步数上限（300 秒白转，评测 authz-001 三遍全挂在这里）。熔断让一轮对话
    有可预期的上界，而不是指望模型自己醒。
    """
    flaky, calls = _flaky_tool(fail_times=0)
    reg = ToolRegistry()
    reg.register(flaky)

    # 历史里已有 2 次完全相同的调用；本次（最后一条 AI）是第 3 次 → 触发熔断
    out = execute_tools(
        {
            "messages": _history_with_repeated_calls(2),
            "current_role_id": wide_role,
            "enabled_domains": [],
            "thread_id": "t1",
        },
        _ctx(reg, roles),
    )

    assert calls[0] == 0, "熔断后不应再执行工具"
    assert out["messages"][0].content == TOOL_LOOP_BREAK
    assert "不要再调用任何工具" in out["messages"][0].content


def test_two_identical_calls_still_execute(roles: RoleCardService, wide_role: str) -> None:
    """上限是 3：历史里 1 次相同调用 + 本次第 2 次 → 照常执行（不误伤正常调用）。"""
    flaky, calls = _flaky_tool(fail_times=0)
    reg = ToolRegistry()
    reg.register(flaky)

    out = execute_tools(
        {
            "messages": _history_with_repeated_calls(1),
            "current_role_id": wide_role,
            "enabled_domains": [],
            "thread_id": "t1",
        },
        _ctx(reg, roles),
    )

    assert calls[0] == 1
    assert out["messages"][0].content == "ok"


def test_new_user_intent_resets_the_loop_counter(
    roles: RoleCardService, wide_role: str
) -> None:
    """用户再问一句 = 新意图：即使参数与之前完全相同，也从零开始计数。

    这是熔断"沿最近的连续工具循环回溯"这条规则的另一半：回溯遇到用户消息就停。
    """
    flaky, calls = _flaky_tool(fail_times=0)
    reg = ToolRegistry()
    reg.register(flaky)

    out = execute_tools(
        {
            "messages": _history_with_repeated_calls(3, human_last=True),
            "current_role_id": wide_role,
            "enabled_domains": [],
            "thread_id": "t1",
        },
        _ctx(reg, roles),
    )

    assert calls[0] == 1
    assert out["messages"][0].content == "ok"


def test_non_idempotent_tool_is_never_retried(roles: RoleCardService, wide_role: str) -> None:
    """**写工具不重试**（审查报告 M10）。

    默认 `idempotent=False`：`upload_medical_report` 这类会写台账的工具，重试一次不是
    "多花一次调用"，而是**多一条副作用**。修复前它对所有工具都重试 2 次。
    """
    flaky, calls = _flaky_tool(fail_times=1)  # 第二次本会成功
    reg = ToolRegistry()
    reg.register(flaky)  # 未声明幂等 → 不可重试
    out = execute_tools(_state_with_call("flaky", wide_role), _ctx(reg, roles))

    assert calls[0] == 1  # 只执行了一次，没有拿副作用去赌瞬时故障
    assert out["messages"][0].content == TOOL_FAILED
    assert "retry_count" not in out  # 没有发生重试，就不该有重试计数


def test_undeclared_tools_are_not_retryable() -> None:
    """注册表的默认值是安全的：没声明 = 不重试。"""
    flaky, _ = _flaky_tool(fail_times=0)
    reg = ToolRegistry()
    reg.register(flaky)
    assert reg.is_idempotent("flaky") is False
    assert reg.is_idempotent("never-registered") is False  # 未注册也按不可重试处理


def test_user_safe_tool_error_passes_reason_through(
    roles: RoleCardService, wide_role: str
) -> None:
    """预料内的工具错误（ToolExecutionError 子类）要把**原因**透传，而不是万能话。

    用户反馈：web_search 失败只看到"工具执行失败，请稍后重试或换一种问法"，连"是超时
    还是没配 key"都看不出。透传后模型能据此向用户解释，工具卡也显示真实原因。
    未知异常仍回 TOOL_FAILED（栈/内部路径不外泄）——分界见 core/tools/errors.py。
    """

    @tool("search_demo")
    def search_demo(query: str = "") -> str:
        """Fails with a designed, user-safe error."""
        raise WebToolError("搜索失败（TimeoutException）：上游搜索源超时。")

    reg = ToolRegistry()
    reg.register(search_demo)
    out = execute_tools(_state_with_call("search_demo", wide_role), _ctx(reg, roles))

    content = out["messages"][0].content
    assert "搜索失败（TimeoutException）" in content
    assert "上游搜索源超时" in content
    assert content != TOOL_FAILED
    # 透传不等于重试豁免：非幂等工具依然只执行一次。
    assert "retry_count" not in out


def test_a_clean_call_reports_no_retries(roles: RoleCardService, wide_role: str) -> None:
    """The field is only written when something actually went wrong."""
    reg = ToolRegistry()
    reg.register(kernel_tool)
    out = execute_tools(_state_with_call("kernel_tool", wide_role), _ctx(reg, roles))
    assert "retry_count" not in out
    assert isinstance(out["messages"][0], ToolMessage)


def test_retry_count_accumulates_across_turns(roles: RoleCardService, wide_role: str) -> None:
    flaky, _ = _flaky_tool(fail_times=99)
    reg = ToolRegistry()
    reg.register(flaky, idempotent=True)
    state = {**_state_with_call("flaky", wide_role), "retry_count": 5}
    out = execute_tools(state, _ctx(reg, roles))
    assert out["retry_count"] == 5 + MAX_TOOL_RETRIES


# --------------------------------------------------------------------------- call_model


class FakeModel:
    """Minimal ChatLike for node tests: bind_tools records the tools, invoke returns a canned
    message. No real provider, so the suite never needs a running Ollama."""

    def __init__(self, reply: Any) -> None:
        self._reply = reply
        self.bound_tools: list[Any] = []
        self.last_prompt: Any = None

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> FakeModel:
        self.bound_tools = list(tools)
        return self

    def invoke(self, prompt: Any, **kwargs: Any) -> Any:
        self.last_prompt = prompt
        return self._reply

    def stream(self, prompt: Any, **kwargs: Any) -> Any:
        """内核走的是流（`call_model` 要能在分块边界收手）：一次给整块 = 累加的恒等情形。"""
        self.last_prompt = prompt
        yield self._reply


class RecordingTracer:
    """Duck-typed Tracer: captures emitted events so a test can assert on them."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [getattr(e, "event", "") for e in self.events]


def _role(roles: RoleCardService, role_id: str = "r", model_name: str | None = None) -> str:
    cards(roles).create(
        RoleCardCreate(
            role_id=role_id,
            role_name=role_id,
            system_prompt="x",
            tool_whitelist=None,
            model_name=model_name,
        )
    )
    return role_id


def test_call_model_scrubs_her_repeats_from_the_prompt_copy_only(
    roles: RoleCardService,
) -> None:
    """套话清洗只改"送出去的那一份"：她说过的原话一个字段都不动。

    这条断言守的是整个设计的红线 —— 清洗的目的是让她**看不见**自己的口癖，
    不是让审计与界面看不见。一旦哪天有人图省事去改 state 里的对象，
    回放与"下次裁剪"看到的就是被改写过的历史，那是毁证据。
    """
    rid = _role(roles)
    first = AIMessage(content="（指尖轻点裙摆）今天风真好呀，你要不要一起去看看那条河？")
    again = AIMessage(content="（忽然凑近屏幕）今天风真好呀，我们去看河吧")
    original_again = again.content
    state_messages = [first, HumanMessage(content="好"), again, HumanMessage(content="再说说")]
    model = FakeModel(AIMessage(content="那我们去河边吧"))
    tracer = RecordingTracer()
    ctx = _ctx(ToolRegistry(), roles, model, tracer=tracer)

    out = call_model(
        {"messages": state_messages, "current_role_id": rid, "thread_id": "t"}, ctx
    )

    sent = [str(m.content) for m in model.last_prompt]
    assert sum("今天风真好呀" in text for text in sent) == 1, (
        "第一份要被留着（她确实说过），第二份要被抹掉（否则她看见两遍就照着续第三遍）"
    )
    # 进来的那批消息对象没有被改写 —— 它们同时是 checkpoint 那份历史的内存形态
    assert again.content == original_again
    assert all(m is not again for m in model.last_prompt), "送出去的必须是另一份拷贝，不是原对象"
    assert first.content != "" and again in state_messages
    assert out["messages"][0].content == "那我们去河边吧"
    assert tracer.kinds().count("history_scrubbed") == 1


def test_call_model_leaves_a_clean_history_untouched(roles: RoleCardService) -> None:
    """没有可判的重复 ⇒ 一份拷贝都不做、一条 trace 都不发（否则日志会被"清洗了个寂寞"刷满）。"""
    rid = _role(roles)
    first = AIMessage(content="（指尖轻点裙摆）今天风真好呀，你要不要一起去看看那条河？")
    second = AIMessage(content="（翻出小本子）上次你说的那家店我今天找到地图了")
    model = FakeModel(AIMessage(content="好"))
    tracer = RecordingTracer()
    ctx = _ctx(ToolRegistry(), roles, model, tracer=tracer)
    call_model(
        {
            "messages": [first, HumanMessage(content="嗯"), second, HumanMessage(content="哦")],
            "current_role_id": rid,
            "thread_id": "t",
        },
        ctx,
    )
    assert "history_scrubbed" not in tracer.kinds()
    # 没改动就该把原对象交出去，不要凭空造一份拷贝
    assert any(m is first for m in model.last_prompt)


def test_call_model_leaves_usage_out_of_node_end(roles: RoleCardService) -> None:
    """`call_model` 这一层**不许**记 token 账，也不许把用量写进 `node_end`（审计 §12.8/#8）。

    不是"取不到"——是这里取到的一定是错的：后端在每一个流式分块里都回一份"累计到此"的
    usage，langchain 合并时逐块相加，所以合并值 = 真值 × 分块数（实测一条"在吗"：
    非流式 26 token，流式合并后 272,607）。真值只有在分块层（`core/agent/turn.py`）才看得见。
    一个错的数比没有数有害：它会安静地喂给"今天花了多少"那个问题。
    """
    rid = _role(roles, "cloudy", model_name="cloud-a")

    class _UsageModel:
        def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
            return self

        def invoke(self, prompt: Any, **kwargs: Any) -> Any:
            return AIMessage(
                content="好",
                usage_metadata={"input_tokens": 900, "output_tokens": 37, "total_tokens": 937},
            )

        def stream(self, prompt: Any, **kwargs: Any) -> Any:
            yield self.invoke(prompt, **kwargs)

    tracer = RecordingTracer()
    ctx = _ctx(ToolRegistry(), roles, _UsageModel(), tracer=tracer)
    call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": rid, "thread_id": "t"}, ctx
    )

    assert not hasattr(ctx, "usage_recorder"), "落账点已经搬走，内核上下文不该再有这个字段"
    assert [e for e in tracer.events if getattr(e, "event", "") == "llm_usage"] == []
    end = next(e for e in tracer.events if getattr(e, "event", "") == "node_end")
    assert end.tokens is None, "这里的 null 是**如实**，不是漏报"
    assert "prompt_tokens" not in end.detail and "completion_tokens" not in end.detail


def _trace_of(ctx: KernelContext) -> list[Any]:
    return list(getattr(ctx.tracer, "events", []))


def test_call_model_resolves_role_backend(roles: RoleCardService) -> None:
    """US-8 后半：角色声明了后端名 → 该轮模型由解析器按名给出。"""
    rid = _role(roles, "cloudy", model_name="cloud-a")
    reg = ToolRegistry()
    reg.register(kernel_tool)
    picked: list[str | None] = []

    class CloudModel:
        def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> CloudModel:
            return self

        def invoke(self, prompt: Any, **kwargs: Any) -> Any:
            return AIMessage(content="from-cloud")

        def stream(self, prompt: Any, **kwargs: Any) -> Any:
            yield self.invoke(prompt, **kwargs)

    def resolver(name: str | None, **_kwargs: Any) -> Any:
        picked.append(name)
        return CloudModel()

    ctx = _ctx(
        reg,
        roles,
        FakeModel(AIMessage(content="default")),
        model_resolver=resolver,
    )
    out = call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": rid, "thread_id": "t"},
        ctx,
    )
    assert picked == ["cloud-a"]  # 角色声明的后端名原样传给解析器
    assert out["messages"][0].content == "from-cloud"  # 用的是解析出的模型，不是默认


def test_call_model_uses_default_when_role_has_no_backend(roles: RoleCardService) -> None:
    """model_name=None → 解析器收到 None 并由它返回默认模型（解析器契约含 None 分支）。"""
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    picked: list[str | None] = []

    def resolver(name: str | None, **_kwargs: Any) -> Any:
        picked.append(name)
        if name is None:
            return FakeModel(AIMessage(content="default"))
        return FakeModel(AIMessage(content="should-not-be-used"))

    ctx = _ctx(
        reg,
        roles,
        FakeModel(AIMessage(content="unused-built-model")),
        model_resolver=resolver,
    )
    out = call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": rid, "thread_id": "t"},
        ctx,
    )
    assert picked == [None]
    assert out["messages"][0].content == "default"


def test_call_model_injects_memory_when_enabled(roles: RoleCardService) -> None:
    """记忆开启 + 有文本 → 进 system prompt（角色之后、安全规则之前）。"""
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    model = FakeModel(AIMessage(content="hi"))
    ctx = _ctx(reg, roles, model)
    ctx.memory_provider = lambda _role_id=None, _thread_id=None, _user_id=None: "用户住在上海。"
    call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": rid, "thread_id": "t"},
        ctx,
    )
    system = model.last_prompt[0].content
    assert system.index("用户住在上海。") < system.index("禁止输出任何疾病诊断")
    assert "用户长期记忆" in system


def test_call_model_asks_the_provider_for_the_current_role(roles: RoleCardService) -> None:
    """接线本身：provider 收到的必须是**本轮角色**，不是 None。

    对话侧以前只取全局记忆，于是"设置→记忆里给某角色写的内容，聊天时模型看不到"
    （审计 §3 台账）。角色专属 → 全局的取法在 `core/memory.memory_for_turn`，与主动开口同源，
    这里只钉"内核把角色传出来了"这一环。

    顺带钉住**第三个参数**：本轮主人（`state["user_id"]`）也随调用显式传给 provider ——
    宿主按它读记忆，不再自己问 ContextVar（身份显式随 state 走的那一刀）。
    """
    rid = _role(roles, role_id="elysia")
    reg = ToolRegistry()
    reg.register(kernel_tool)
    model = FakeModel(AIMessage(content="hi"))
    ctx = _ctx(reg, roles, model)
    asked: list[tuple[str | None, str | None, str | None]] = []

    def provider(
        role_id: str | None = None,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        asked.append((role_id, thread_id, user_id))
        return "她记得自己喜欢蒲公英。"

    ctx.memory_provider = provider
    call_model(
        {
            "messages": [HumanMessage(content="q")],
            "current_role_id": rid,
            "thread_id": "t",
            # 用卡所在那个主人（`cards()` 按 DEFAULT_USER_ID 建）：role lookup 按
            # state["user_id"] 走 scoped，随便写一个就会撞 role_missing 提前返回。
            "user_id": DEFAULT_USER_ID,
        },
        ctx,
    )
    # 记的是**三元组**：第二条是本轮那条线程 —— 宿主拿它判"要不要再抄一份主动开口"
    # （09-26 那条跨线程记忆断口的修法依赖它）；第三条是本轮主人（state 里有就传值）。
    assert asked == [("elysia", "t", DEFAULT_USER_ID)]
    assert "她记得自己喜欢蒲公英。" in model.last_prompt[0].content


def test_call_model_skips_memory_when_disabled(roles: RoleCardService) -> None:
    """总开关 MEMORY_ENABLED=false → 提供者照常返回也不注入（双保险的第二道门）。"""
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    model = FakeModel(AIMessage(content="hi"))
    ctx = _ctx(reg, roles, model)
    ctx.settings = Settings(memory_enabled=False)
    ctx.memory_provider = lambda _role_id=None, _thread_id=None, _user_id=None: "用户住在上海。"
    call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": rid, "thread_id": "t"},
        ctx,
    )
    assert "用户长期记忆" not in model.last_prompt[0].content


def _image_human_msg() -> HumanMessage:
    """复刻 _user_message 的带图形态：多模态 content 块 + has_image 标记。"""
    return HumanMessage(
        content=[
            {"type": "text", "text": "这是什么？"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        additional_kwargs={"has_image": True},
    )


def test_latest_image_data_url_picks_most_recent_image() -> None:
    """从历史里取最近一张图的 data URL（反向图搜的输入）；纯文本轮 → None。"""
    assert _latest_image_data_url([_image_human_msg()]) == "data:image/png;base64,AAAA"
    assert _latest_image_data_url([HumanMessage(content="纯文本")]) is None
    assert _latest_image_data_url([]) is None
    # 多图取最近一条
    msgs = [_image_human_msg(), HumanMessage(content="中间"), _image_human_msg()]
    assert _latest_image_data_url(msgs) == "data:image/png;base64,AAAA"


def test_call_model_injects_image_grounding_when_turn_has_image(roles: RoleCardService) -> None:
    """本轮带图片 → 图像接地规则进 system（在安全规则之前）。"""
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(reg, roles, model)
    call_model(
        {"messages": [_image_human_msg()], "current_role_id": rid, "thread_id": "t"}, ctx
    )
    system = model.last_prompt[0].content
    assert "图像理解规则" in system
    assert system.index("图像理解规则") < system.index("禁止输出任何疾病诊断")


def test_call_model_no_image_grounding_for_text_only_turn(roles: RoleCardService) -> None:
    """纯文本轮次不注入图像接地规则（省额度、避免无谓指令）。"""
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(reg, roles, model)
    call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": rid, "thread_id": "t"}, ctx
    )
    assert "图像理解规则" not in model.last_prompt[0].content


def test_call_model_skips_tools_when_backend_disables_them(
    registry: ToolRegistry, roles: RoleCardService
) -> None:
    """后端 supports_tools=false（如某些云端 VLM 带 tools 会返回空）→ 本轮不绑工具，
    让"换模型"对所有角色统一生效，不必建特殊角色。"""
    rid = _role(roles)
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(registry, roles, model)
    ctx.settings = Settings(
        model_default="novl",
        model_backends={"novl": ModelBackend(model="m", provider="ollama", supports_tools=False)},
    )
    call_model(
        {
            "messages": [HumanMessage(content="q")],
            "current_role_id": rid,
            "thread_id": "t",
            "model_name": "novl",
        },
        ctx,
    )
    assert model.bound_tools == []  # 工具被跳过


def test_call_model_binds_tools_when_backend_supports_them(
    registry: ToolRegistry, roles: RoleCardService
) -> None:
    """对照组：supports_tools=true（默认）→ 照常绑上可见工具。"""
    rid = _role(roles)
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(registry, roles, model)
    ctx.settings = Settings(
        model_default="full",
        model_backends={"full": ModelBackend(model="m", provider="ollama", supports_tools=True)},
    )
    call_model(
        {
            "messages": [HumanMessage(content="q")],
            "current_role_id": rid,
            "thread_id": "t",
            "model_name": "full",
        },
        ctx,
    )
    assert [t.name for t in model.bound_tools] == ["kernel_tool"]


def test_execute_tools_denies_call_when_backend_disables_tools(
    registry: ToolRegistry, roles: RoleCardService, wide_role: str
) -> None:
    """P1-3 纵深防御：后端 supports_tools=false → 即便模型幻觉出 tool_call，执行侧也拒
    （工具不在 permitted），不会真去执行。turn_context 是 bind 与 permitted 的共同来源。"""
    flaky, calls = _flaky_tool(fail_times=0)
    reg = ToolRegistry()
    reg.register(flaky)
    ctx = _ctx(reg, roles)
    ctx.settings = Settings(
        model_default="novl",
        model_backends={"novl": ModelBackend(model="m", provider="ollama", supports_tools=False)},
    )
    state = _state_with_call("flaky", wide_role)
    state["model_name"] = "novl"
    out = execute_tools(state, ctx)
    assert calls[0] == 0  # 工具一次都没执行
    assert out["messages"][0].content == TOOL_DENIED


def test_call_model_injects_agent_plan_when_state_says_agent(roles: RoleCardService) -> None:
    """state.agent_mode == 'agent' → 注入规划指令（在安全规则之前）。"""
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(reg, roles, model)
    call_model(
        {
            "messages": [HumanMessage(content="q")],
            "current_role_id": rid,
            "thread_id": "t",
            "agent_mode": "agent",
        },
        ctx,
    )
    system = model.last_prompt[0].content
    assert system.index("智能体模式") < system.index("禁止输出任何疾病诊断")


def test_call_model_skips_agent_plan_outside_agent_mode(roles: RoleCardService) -> None:
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(reg, roles, model)
    call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": rid, "thread_id": "t"},
        ctx,
    )  # agent_mode 缺席 = 对话档
    assert "智能体模式" not in model.last_prompt[0].content


def test_call_model_writes_live_enabled_domains_and_epoch(roles: RoleCardService) -> None:
    """The enabled set and epoch are read live (callable), not from graph-build time."""
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    ctx = _ctx(
        reg,
        roles,
        FakeModel(AIMessage(content="hi")),
        enabled_domains=["health"],
        tool_epoch=7,
    )
    out = call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": rid, "thread_id": "t"}, ctx
    )
    assert out["messages"][0].content == "hi"
    assert out["enabled_domains"] == ["health"]
    assert out["tool_epoch"] == 7


def test_call_model_emits_epoch_drift_when_session_is_stale(roles: RoleCardService) -> None:
    """US-3 / C14: a session that outlived a plugin toggle carries an old epoch; report once."""
    rid = _role(roles)
    reg = ToolRegistry()
    reg.register(kernel_tool)
    tracer = RecordingTracer()
    ctx = _ctx(
        reg,
        roles,
        FakeModel(AIMessage(content="hi")),
        enabled_domains=[],
        tool_epoch=3,
        tracer=tracer,
    )
    out = call_model(
        {
            "messages": [HumanMessage(content="q")],
            "current_role_id": rid,
            "thread_id": "t",
            "tool_epoch": 1,  # the session was checkpointed before the toggle
        },
        ctx,
    )
    assert "tool_epoch_drift" in tracer.kinds()
    # refreshed so the drift event does NOT fire on every subsequent turn
    assert out["tool_epoch"] == 3


def test_call_model_degrades_missing_role_without_crashing(roles: RoleCardService) -> None:
    """A thread bound to a deleted role gets a sentence, not a 500."""
    reg = ToolRegistry()
    reg.register(kernel_tool)
    tracer = RecordingTracer()
    ctx = _ctx(reg, roles, FakeModel(AIMessage(content="hi")), tracer=tracer)
    out = call_model(
        {"messages": [HumanMessage(content="q")], "current_role_id": "ghost", "thread_id": "t"},
        ctx,
    )
    assert out["messages"][0].content  # non-empty refusal
    assert "role_missing" in tracer.kinds()


# --------------------------------------------------------------------------- 上下文预算（H3）


def _history(n: int, size: int) -> list[Any]:
    """n 轮 (user, assistant) 对话，每条内容 size 个字符。"""
    out: list[Any] = []
    for i in range(n):
        out.append(HumanMessage(content=f"ask-{i} " + "x" * size))
        out.append(AIMessage(content=f"answer-{i} " + "y" * size))
    return out


def test_trim_history_keeps_everything_inside_budget() -> None:
    """预算够 → 一条不丢。"""
    msgs = _history(2, 10)
    kept, dropped = trim_history(msgs, 100_000)
    assert kept == msgs and dropped == 0


def test_trim_history_drops_oldest_first() -> None:
    """超预算 → 从最旧的开始丢，最近的一定留下。"""
    msgs = _history(10, 200)
    kept, dropped = trim_history(msgs, 900)
    assert dropped > 0
    assert kept[-1] is msgs[-1]
    assert kept[0] is not msgs[0]
    assert len(kept) + dropped == len(msgs)


def test_trim_history_never_orphans_a_tool_message() -> None:
    """**关键不变量**：起点绝不能落在 ToolMessage 上。

    丢掉带 `tool_calls` 的 AIMessage、却留下它的 ToolMessage，多数供应商会直接 400 ——
    比"超窗"更难排查。这里构造 [AIMessage(tool_calls), ToolMessage] 对，并把预算调到
    刚好处在"会切在 ToolMessage 上"的位置。
    """
    call = AIMessage(
        content="", tool_calls=[{"name": "list_domains", "args": {}, "id": "c1"}]
    )
    result = ToolMessage(content="结果" * 50, tool_call_id="c1", name="list_domains")
    msgs = [HumanMessage(content="问" * 100), call, result]
    kept, _ = trim_history(msgs, 130)  # 预算只装得下 result 的大小

    assert kept, "至少保留一条"
    assert not isinstance(kept[0], ToolMessage), "起点不能是孤立的工具结果"
    # 保留 result 就必须同时保留发起它的 AIMessage
    if any(isinstance(m, ToolMessage) for m in kept):
        assert any(isinstance(m, AIMessage) and m.tool_calls for m in kept)


def test_trim_history_zero_means_no_trimming() -> None:
    """0/负数 = 显式关闭裁剪（调试用）。"""
    msgs = _history(5, 500)
    kept, dropped = trim_history(msgs, 0)
    assert kept == msgs and dropped == 0


def test_trim_history_survives_a_history_with_no_legal_start() -> None:
    """全是工具结果的（非法）历史：原样返回，不抛异常。

    这种输入只可能来自外部构造，但"裁剪函数把整轮对话搞崩"远糟于"发一段双方都不满意的
    历史"。所以这里是**容错**而不是断言失败。
    """
    msgs = [ToolMessage(content="a", tool_call_id="c1", name="x")]
    kept, dropped = trim_history(msgs, 1)
    assert kept == msgs and dropped == 0


def test_trim_history_advances_past_an_illegal_head() -> None:
    """首条非法、但后面有合法消息 → 向前推进到那个合法位置。

    构造：三个 ToolMessage（不能当起点）+ 一个 HumanMessage。
    回退到 0 仍然非法，于是向前找到 HumanMessage —— 代价是丢得比预算要求的更多，
    换来的是**发出的序列合法**。
    """
    msgs: list[Any] = [
        ToolMessage(content="x", tool_call_id=f"c{i}", name="t") for i in range(3)
    ]
    msgs.append(HumanMessage(content="最新的问题"))
    kept, dropped = trim_history(msgs, 52)
    assert kept == [msgs[3]]
    assert dropped == 3


def test_call_model_trims_the_prompt_and_reports_it(roles: RoleCardService) -> None:
    """裁剪必须**真的作用在 prompt 上**，并且留痕。

    断言两件事：送给模型的消息条数少于历史条数（说明裁剪生效）；`context_trimmed`
    事件被 emit（说明运维能看到"这次回答没有早期上下文"）。
    """
    model = FakeModel(AIMessage(content="ok"))
    tracer = RecordingTracer()
    ctx = _ctx(ToolRegistry(), roles, model, tracer=tracer)
    ctx.max_context_chars = 300  # 用很小的预算逼出裁剪
    history = _history(8, 200)
    state = {
        "messages": history,
        "current_role_id": "medical_archivist",
        "thread_id": "t",
    }
    call_model(state, ctx)

    assert "context_trimmed" in tracer.kinds()
    prompt = model.last_prompt
    assert prompt is not None
    # prompt = [SystemMessage] + 裁剪后的历史
    assert 1 < len(prompt) < 1 + len(history)
    assert prompt[-1].content == history[-1].content  # 最新的那条一定在


def test_call_model_without_pressure_does_not_trim(roles: RoleCardService) -> None:
    """没超预算就不该有 context_trimmed 噪音。"""
    model = FakeModel(AIMessage(content="ok"))
    tracer = RecordingTracer()
    ctx = _ctx(ToolRegistry(), roles, model, tracer=tracer)
    history = _history(2, 10)
    call_model(
        {"messages": history, "current_role_id": "medical_archivist", "thread_id": "t"}, ctx
    )
    assert "context_trimmed" not in tracer.kinds()
    # 1 条 system + 全量历史 + 1 条深度注入（写法要求贴在生成点再说一遍）
    assert len(model.last_prompt) == 2 + len(history)


# -------------------------------------------------------------------------- 深度注入（§8.2）


def _voice_index(prompt: list[Any]) -> int:
    """深度注入那条指令在 prompt 里的下标（找不到就直接失败，别用 -1 蒙过去）。"""
    hits = [
        i
        for i, m in enumerate(prompt)
        if isinstance(m, SystemMessage) and m.content == VOICE_DEPTH_PROMPT
    ]
    assert len(hits) == 1, f"深度注入应当恰好一条，实际 {len(hits)}"
    return int(hits[0])


def test_voice_directive_is_injected_after_history_not_inside_the_system_block(
    roles: RoleCardService,
) -> None:
    """活人感的写法要求要**贴着生成点**说，而不是塞进最前面那条 system。

    这是本节全部症状的机制性回应：她的历史里有「（动作）+哎呀」，而离生成点最远的那段
    指令盖不住它。SillyTavern 的 Author's Note 就是为此存在（默认 depth=4）。
    断言"不在系统消息里 + 落在历史中段之后"，改回一行 `[SystemMessage, *history]` 就会红。
    """
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(ToolRegistry(), roles, model)
    history = _history(6, 10)  # 12 条，倒数第 4 条的位置明显在历史里
    call_model(
        {"messages": history, "current_role_id": "medical_archivist", "thread_id": "t"}, ctx
    )
    prompt = model.last_prompt
    at = _voice_index(prompt)
    assert at > 1, "不能就是开头那条系统消息（那样等于没做深度注入）"
    assert at <= len(history), "必须落在历史中间或末尾之前，不能掉到最后一条之后"
    # 人设那条 system 里不该含这段：否则两遍重复，且第 0 条会随写法要求一起变长。
    assert VOICE_DEPTH_PROMPT not in prompt[0].content
    assert prompt[-1] is history[-1], "最新的用户消息仍然是最后一条"


def test_voice_injection_never_splits_a_tool_call_group(roles: RoleCardService) -> None:
    """**关键不变量**：插入点不能把 `AIMessage(tool_calls)` 和它的 `ToolMessage` 隔开。

    历史末尾正好是工具组时，"倒数第 4 条"这个朴素落点会插在两者中间 —— 供应商判非法序列
    （400），而且只在"上一轮调过工具"的那一轮复现。这里构造的 history 让朴素落点恰好压在
    ToolMessage 上，脚本必须往前退到工具组之外。
    """
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(ToolRegistry(), roles, model)
    call = AIMessage(content="", tool_calls=[{"name": "list_domains", "args": {}, "id": "c1"}])
    result = ToolMessage(content="结果", tool_call_id="c1", name="list_domains")
    history: list[Any] = [
        *[HumanMessage(content=f"问{i}") for i in range(4)],
        call,
        result,
        HumanMessage(content="那现在呢"),
        AIMessage(content="刚才答的"),
        HumanMessage(content="最后这句"),
    ]  # 9 条：朴素落点 9-4=5 正是 result
    call_model(
        {"messages": history, "current_role_id": "medical_archivist", "thread_id": "t"}, ctx
    )
    prompt = model.last_prompt
    at = _voice_index(prompt)
    call_at = prompt.index(call)
    result_at = prompt.index(result)
    assert not call_at < at < result_at, "把工具组和它的结果隔开了"
    # 顺带钉住"整段发出去的序列仍然合法"：起点之后第一条不能是孤立的 ToolMessage。
    assert all(
        not isinstance(m, ToolMessage) or prompt[i - 1] is call or prompt[i - 1].tool_calls
        for i, m in enumerate(prompt)
        if i
    )


def test_voice_injection_survives_a_history_shorter_than_depth(
    roles: RoleCardService,
) -> None:
    """历史不足 4 条：退化成贴在系统消息之后，序列照样合法、照样只注入一条。"""
    model = FakeModel(AIMessage(content="ok"))
    ctx = _ctx(ToolRegistry(), roles, model)
    history = [HumanMessage(content="就一句")]
    call_model(
        {"messages": history, "current_role_id": "medical_archivist", "thread_id": "t"}, ctx
    )
    prompt = model.last_prompt
    assert len(prompt) == 3
    assert _voice_index(prompt) == 1


# --------------------------------------------------------------------------- 工具超时（M10）


def test_a_hanging_tool_times_out_instead_of_blocking_the_turn(
    roles: RoleCardService, wide_role: str
) -> None:
    """挂住的工具必须在预算内放弃，而不是把图执行线程一直占着。

    修复前工具调用**没有总时长上限**：一个坏掉的重试循环就能让 SSE 请求永远不返回，
    而 SSE 跑在 Starlette 线程池上 —— 几个挂住的工具会让整个服务一起停摆。
    """

    @tool("hangs")
    def hangs() -> str:
        """Sleeps far longer than the budget."""
        time.sleep(5)
        return "never"

    reg = ToolRegistry()
    reg.register(hangs)  # 未声明幂等 → 不重试，超时只发生一次
    ctx = _ctx(reg, roles)
    ctx.tool_timeout_seconds = 0.05

    started = time.perf_counter()
    out = execute_tools(_state_with_call("hangs", wide_role), ctx)
    elapsed = time.perf_counter() - started

    assert out["messages"][0].content == TOOL_FAILED
    assert elapsed < 2, f"没有及时放弃：耗时 {elapsed:.1f}s"
    assert "retry_count" not in out


def test_tool_timeout_zero_disables_the_limit(roles: RoleCardService, wide_role: str) -> None:
    """`tool_timeout_seconds <= 0` = 不设上限，且退回"当前线程直接调用"的原路径。"""

    @tool("quick")
    def quick() -> str:
        """Returns immediately."""
        return "done"

    reg = ToolRegistry()
    reg.register(quick, idempotent=True)
    ctx = _ctx(reg, roles)
    ctx.tool_timeout_seconds = 0
    out = execute_tools(_state_with_call("quick", wide_role), ctx)
    assert out["messages"][0].content == "done"


def test_search_tool_sees_the_role_scopes_across_the_executor_thread(
    roles: RoleCardService,
) -> None:
    """**ContextVar 传播**：工具在独立线程里执行时，仍必须看得到角色的知识作用域。

    这是给 M10 加线程池时最容易踩的坑：ContextVar 不会自动传播到新线程，而
    `search_knowledge` 正是从 `role_knowledge_scopes_ctx` 读授权作用域的。不提交
    `copy_context()` 的话，检索工具会看到空作用域、直接回答"当前角色未授权任何知识作用域"
    —— 一个由并发实现引入的、**与权限相关**的静默故障。
    """
    from rolecard_agent.base.scopes import current_knowledge_scopes

    @tool("scope_probe")
    def scope_probe() -> str:
        """Reports the scopes visible to the tool layer."""
        return ",".join(current_knowledge_scopes())

    cards(roles).create(
        RoleCardCreate(
            role_id="scoped",
            role_name="带作用域",
            system_prompt="x",
            tool_whitelist=None,
            knowledge_scopes=["reports_2026"],
        )
    )
    reg = ToolRegistry()
    reg.register(scope_probe, idempotent=True)
    ctx = _ctx(reg, roles)
    ctx.tool_timeout_seconds = 5  # 非 0 → 走线程池路径

    out = execute_tools(_state_with_call("scope_probe", "scoped"), ctx)
    assert out["messages"][0].content == "reports_2026"


def test_one_turn_writes_all_three_injected_scopes_together(roles: RoleCardService) -> None:
    """轮次注入的三个 ContextVar 必须**作为一个整体**被写入、且每轮都重写。

    三条的注入点全在 `execute_tools` 开头那一段：知识作用域（检索授权）、当前角色
    （per-role 记忆归属）、本轮图片（反向图搜输入）。把它们合成一个变量属于跨模块 +
    `copy_context` 敏感的顺手重构、收益边缘，已判定不做（审计 §5）；真正的风险不是"有三个"，
    而是"**改了其中两个、忘第三个**"——不会有编译错误，症状只是某条工具静默降级或拿到上一轮的
    残留。所以这里钉的是同一个快照的两半：一轮跑完，三个读侧在**工具自己的线程里**看都与 state
    一致；换一轮（换角色、去掉图片）之后**一个都不许留**。
    """
    from rolecard_agent.base.scopes import current_knowledge_scopes, current_turn_image
    from rolecard_agent.core.memory import current_role_id_ctx

    @tool("turn_scope_probe")
    def turn_scope_probe() -> str:
        """Reports what the turn injected, as seen from the tool's own thread."""
        parts = [",".join(current_knowledge_scopes()), current_role_id_ctx.get()]
        parts.append(str(current_turn_image()))
        return "|".join(parts)

    cards(roles).create(
        RoleCardCreate(
            role_id="scoped2",
            role_name="带作用域",
            system_prompt="x",
            tool_whitelist=None,
            knowledge_scopes=["reports_2026"],
        )
    )
    cards(roles).create(
        RoleCardCreate(role_id="plain", role_name="干净", system_prompt="x", tool_whitelist=None)
    )
    reg = ToolRegistry()
    reg.register(turn_scope_probe, idempotent=True)
    ctx = _ctx(reg, roles)
    ctx.tool_timeout_seconds = 5  # 非 0 → 真的走线程池，读侧必须是 copy_context 之后的上下文

    def probe(role_id: str, messages: list[Any]) -> tuple[str, str, str]:
        state = {
            "messages": [*messages, AIMessage(content="", tool_calls=[
                {"name": "turn_scope_probe", "args": {}, "id": "c1"}])],
            "current_role_id": role_id,
            "thread_id": "t1",
        }
        scopes, role, image = execute_tools(state, ctx)["messages"][0].content.split("|")
        return scopes, role, image

    assert probe("scoped2", [_image_human_msg()]) == (
        "reports_2026",
        "scoped2",
        "data:image/png;base64,AAAA",
    )
    # 第二轮：换角色 + 没图。任何一项还留着上一轮的值，就是"忘了重写"。
    assert probe("plain", [HumanMessage(content="纯文本")]) == ("", "plain", "None")


# ---------------------------------------------------- P1-2 视觉的调用前拦截（只拦确定的否）


def _vision_ctx(
    roles: RoleCardService,
    *,
    declared: bool,
    probe: Any,
    provider: str = "ollama",
    tracer: Any = None,
    model_name: str = "some-model",
) -> tuple[KernelContext, FakeModel, RecordingTracer]:
    """一张带图能跑的 ctx：后端行的 `supports_vision` 与探测器答案都由用例点名。"""
    rec = tracer or RecordingTracer()
    model = FakeModel(AIMessage(content="ok"))
    reg = ToolRegistry()
    reg.register(kernel_tool)
    ctx = _ctx(reg, roles, model, tracer=rec)
    ctx.settings = Settings(
        model_default="vl",
        model_backends={
            "vl": ModelBackend(model=model_name, provider=provider, supports_vision=declared)
        },
    )
    ctx.vision_probe = probe  # type: ignore[assignment]
    return ctx, model, rec


def _image_state(rid: str) -> dict[str, Any]:
    return {"messages": [_image_human_msg()], "current_role_id": rid, "thread_id": "t"}


def test_vision_block_needs_both_evidences(roles: RoleCardService) -> None:
    """声明 false **且** 探测确认不能看 —— 才拦。抛的是专用异常，不是通用失败句。"""
    rid = _role(roles)
    ctx, model, rec = _vision_ctx(roles, declared=False, probe=lambda _b, _m: False)
    with pytest.raises(VisionNotSupported):
        call_model(_image_state(rid), ctx)
    assert model.last_prompt is None  # 一次真调用都没发生
    assert "vision_blocked_pre_call" in rec.kinds()


def test_vision_probe_overrides_a_stale_checkbox(roles: RoleCardService) -> None:
    """探测说能看 → 放行：勾选框是人的输入，会过期；误杀一个能看图的模型比多花一次
    往返严重得多，所以这里探针赢。"""
    rid = _role(roles)
    ctx, model, rec = _vision_ctx(roles, declared=False, probe=lambda _b, _m: True)
    call_model(_image_state(rid), ctx)
    assert model.last_prompt is not None
    assert "vision_blocked_pre_call" not in rec.kinds()


def test_vision_unknown_never_blocks(roles: RoleCardService) -> None:
    """探测器问不出答案（老版本 Ollama / 没接线 / 超时）= 不知道 → 放行。"""
    rid = _role(roles)
    ctx, model, _rec = _vision_ctx(roles, declared=False, probe=lambda _b, _m: None)
    call_model(_image_state(rid), ctx)
    assert model.last_prompt is not None


def test_vision_declared_capable_is_not_probed(roles: RoleCardService) -> None:
    """声明支持就不去探（省一次 HTTP），也不拦。"""
    rid = _role(roles)
    calls: list[str] = []

    def probe(_base: str | None, _model: str) -> bool:
        calls.append("probed")
        return False

    ctx, model, _rec = _vision_ctx(roles, declared=True, probe=probe)
    call_model(_image_state(rid), ctx)
    assert model.last_prompt is not None
    assert calls == []


def test_vision_gate_ignores_text_only_turns(roles: RoleCardService) -> None:
    """这一轮没图片 → 与视觉无关，照常走。"""
    rid = _role(roles)
    ctx, model, _rec = _vision_ctx(roles, declared=False, probe=lambda _b, _m: False)
    call_model({"messages": [HumanMessage(content="纯文本")], "current_role_id": rid}, ctx)
    assert model.last_prompt is not None


def test_vision_gate_does_not_guess_about_cloud_backends(roles: RoleCardService) -> None:
    """云端行探不了（要真发一张图，有成本）→ 未知 → 放行，维持 reactive 兜底。"""
    rid = _role(roles)
    ctx, model, _rec = _vision_ctx(
        roles, declared=False, provider="openai", probe=lambda _b, _m: False
    )
    call_model(_image_state(rid), ctx)
    assert model.last_prompt is not None


# 这台机器上拿来当"确定不能看"证据的纯文本模型（`ollama pull all-minilm`，45 MB）。
# 名字可经 env 换：任何 `/api/show` 的 capabilities 里不含 vision 的 Ollama 模型都行。
TEXT_ONLY_PROBE_MODEL = os.environ.get("ROLECARD_TEST_TEXT_MODEL", "all-minilm:latest")


@pytest.mark.live
def test_vision_gate_fires_on_a_real_text_only_model(roles: RoleCardService) -> None:
    """上面那批喂的是**假探针**；这条把真 `probes.vision_capability` 接进真闸门，是 P1-2
    `False` 那一支唯一的真机覆盖（本机原先只装了一个 VLM，那一支从来没有真实证据）。

    三态在这里各有一次真问答：纯文本模型 → `False`（闸门该落），VLM → `True`（该放行）。
    没装对应模型时**跳过而不是失败** —— 探针问不到 = "不知道"，那本来就是放行的正确答案，
    把环境问题报成断言失败会让这条用例在别人的机器上变成噪音。
    """
    blocked = probes.vision_capability(None, TEXT_ONLY_PROBE_MODEL, use_cache=False)
    if blocked is not False:
        pytest.skip(f"{TEXT_ONLY_PROBE_MODEL} 不在本机 Ollama 上（探针给 {blocked!r}）")
    rid = _role(roles)
    ctx, model, rec = _vision_ctx(
        roles,
        declared=False,
        probe=probes.vision_capability,
        model_name=TEXT_ONLY_PROBE_MODEL,
    )
    with pytest.raises(VisionNotSupported):
        call_model(_image_state(rid), ctx)
    assert model.last_prompt is None  # 拦住发生在调用前：一次真推理都没发出
    assert "vision_blocked_pre_call" in rec.kinds()

    # 反向半条：同一台机器上的 VLM 探到 True → 放行（探针赢过一次过期的勾选框）。
    if probes.vision_capability(None, "qwen3-vl:8b", use_cache=False) is True:
        ctx2, model2, rec2 = _vision_ctx(
            roles,
            declared=False,
            probe=probes.vision_capability,
            model_name="qwen3-vl:8b",
        )
        call_model(_image_state(rid), ctx2)
        assert model2.last_prompt is not None
        assert "vision_blocked_pre_call" not in rec2.kinds()


_STATUS_IN_TEXT = re.compile(r"status code: (\d{3})")


@pytest.mark.live
def test_real_backend_accepts_a_mid_conversation_system_message() -> None:
    """深度注入唯一没法在单元层证明的事：**服务端**收不收历史中间的 system 消息。

    客户端这一半已经钉住了（langchain-ollama 原样按顺序序列化成 role=system）。但 GGUF 的
    聊天模板是模型自带的，有些模板里写着"系统消息必须在最前面"那种断言，那种模型会直接 400
    —— 只能真发一次才知道。所以这里的分界按**状态码**划，不按异常名字猜：
    4xx = 服务端拒了这条消息序列 → **失败**（正是这条用例要抓的东西）；
    连不通 / 5xx / 模型没拉 = 没问到真服务 → **跳过**（本机 2026-09-22 就是这样：
    Ollama 没跑，请求被本地代理挡成 502，那不是消息序列的问题）。

    这条只证明"发得出去"。"她是否因此少说几句模板腔"不由断言管，那是
    `scripts/tools/persona_meter.py` 的活（一次采样不足以判质量）。
    """
    backend = Settings.from_env().backend()
    if not backend.base_url or "11434" not in backend.base_url:
        pytest.skip("默认后端不是本机 Ollama，这条真机用例的前提不成立")
    try:
        from langchain_ollama import ChatOllama
    except ImportError:  # pragma: no cover - 装了 provider=ollama 才有
        pytest.skip("langchain-ollama 未安装")

    model = ChatOllama(model=backend.model, base_url=backend.base_url, num_ctx=4096)
    prompt = [
        SystemMessage(content="你是一个陪聊角色，说话简短。"),
        HumanMessage(content="我回来了"),
        AIMessage(content="（转身）哎呀，你回来啦"),
        SystemMessage(content=VOICE_DEPTH_PROMPT),
        HumanMessage(content="今天好累"),
    ]
    try:
        reply = model.invoke(prompt)
    except Exception as exc:
        code = _http_status_of(exc)
        if code is not None and 400 <= code < 500:
            raise AssertionError(
                f"服务端拒了「历史中间的 system 消息」（{code}）——深度注入这个形态对它不成立："
                f"{exc}"
            ) from exc
        pytest.skip(f"没问到真服务（{type(exc).__name__}: {exc}），这条需要 Ollama 在跑")
    assert str(reply.content).strip(), "服务端收了，但回了一条空消息"


def _http_status_of(exc: BaseException) -> int | None:
    """从异常里抠出 HTTP 状态码：langchain 会把 ollama 的错误再包一层，字段不一定还在。"""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(response, int):
        return response
    found = _STATUS_IN_TEXT.search(str(exc))
    return int(found.group(1)) if found else None
