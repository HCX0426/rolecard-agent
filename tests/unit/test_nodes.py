"""Unit tests for the graph nodes.  Traceability: US-1, US-2, US-3.

Integration tests exercise the happy path through a real compiled graph; these pin the
decisions that are cheap to get wrong and expensive to notice: when to loop back into the
tools, which tools a turn may see, how a failing tool is retried, and how much history is
allowed into the prompt.

See 技术评审与决策.md §9 D2 - these had no unit coverage before.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from rolecard_agent.config import Settings
from rolecard_agent.core.nodes import (
    MAX_TOOL_RETRIES,
    TOOL_FAILED,
    KernelContext,
    call_model,
    execute_tools,
    route_after_model,
    tools_for_turn,
    trim_history,
)
from rolecard_agent.core.observability import NullTracer
from rolecard_agent.core.tools.errors import ToolExecutionError  # noqa: F401 - 文档化分界用
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.core.tools.web import WebToolError
from rolecard_agent.roles.models import RoleCardCreate
from rolecard_agent.roles.service import RoleCardService

# --------------------------------------------------------------------------- routing


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


def test_tools_for_turn_honours_both_stages(registry: ToolRegistry, roles: RoleCardService) -> None:
    roles.create(
        RoleCardCreate(
            role_id="narrow", role_name="窄", system_prompt="x", tool_whitelist=["domain_tool"]
        )
    )
    state = {"current_role_id": "narrow"}
    assert [
        t.name for t in tools_for_turn(state, _ctx(registry, roles, enabled_domains=["dom"]))
    ] == ["domain_tool"]


def test_unknown_role_yields_no_tools(registry: ToolRegistry, roles: RoleCardService) -> None:
    """A session pointing at a deleted role must not fall through to 'everything allowed'."""
    state = {"current_role_id": "ghost", "enabled_domains": ["dom"]}
    with pytest.raises(Exception, match="ghost"):
        tools_for_turn(state, _ctx(registry, roles))


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
    roles.create(
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


class RecordingTracer:
    """Duck-typed Tracer: captures emitted events so a test can assert on them."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [getattr(e, "event", "") for e in self.events]


def _role(roles: RoleCardService, role_id: str = "r", model_name: str | None = None) -> str:
    roles.create(
        RoleCardCreate(
            role_id=role_id,
            role_name=role_id,
            system_prompt="x",
            tool_whitelist=None,
            model_name=model_name,
        )
    )
    return role_id


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

    def resolver(name: str | None) -> Any:
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

    def resolver(name: str | None) -> Any:
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
    assert len(model.last_prompt) == 1 + len(history)


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
    from rolecard_agent.core.nodes import current_knowledge_scopes

    @tool("scope_probe")
    def scope_probe() -> str:
        """Reports the scopes visible to the tool layer."""
        return ",".join(current_knowledge_scopes())

    roles.create(
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
