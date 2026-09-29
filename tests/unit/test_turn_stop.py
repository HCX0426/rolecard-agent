""""停止生成"要真的停（任务 #18 / 审计 §12.12②）。

钉的是三件事，每一件都是这一轮改动新增的行为：

1. **中途停 = 停在哪儿，历史就到哪儿**：`call_model` 现在自己拿住模型的流，看到取消旗就
   收手并把**已经生成的那半截**提交 —— 她看到的与检查点里的是同一份。顺带把半截的
   tool_calls 丢掉：按停止的意思是"别说了"，不是"拿没生成完的参数去执行工具"。
2. **流必须被 `close()`**：停之所以省东西，全靠关掉底层 HTTP 流。实测（同机 qwen3-vl:8b，
   长生成跑到第 3 块关连接）之后 1-token 探针 0.30 / 0.20 / 0.16 s，基线 0.12 s ——
   Ollama 不到 1s 就停了。不 close 就是"界面停了而它还在写"，那正是 #18 的原始症状。
3. **旗子的生命周期**：一轮开始清一次（上一句的停不顺延），没人要这一轮时（客户端关页面）
   由 `run_turn` 的 finally 补上停。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import tool

from rolecard_agent.config import Settings
from rolecard_agent.core.identity import DEFAULT_USER_ID
from rolecard_agent.core.nodes import (
    EmptyModelStream,
    KernelContext,
    TurnStopped,
    call_model,
)
from rolecard_agent.core.observability import NullTracer
from rolecard_agent.core.thread_locks import clear_stop, request_stop, stop_requested
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.core.turn import (
    _GENERIC_MODEL_FAILURE,
    EMPTY_STREAM_DETAIL,
    End,
    model_error_detail,
    run_turn,
)
from rolecard_agent.roles.service import RoleCardCreate, RoleCards, RoleCardService


def cards(store: RoleCardService) -> RoleCards:
    """测试里"本机主人眼里的那些卡"的简写（M2a 之后每次读写都得说清为谁）。"""
    return store.scoped(DEFAULT_USER_ID)

@pytest.fixture(autouse=True)
def _clean_flags() -> Iterator[None]:
    """旗子是进程内的：用例之间必须互不残留。"""
    clear_stop("t")
    yield
    clear_stop("t")


@tool
def list_roles() -> str:
    """一个够用的假工具。"""
    return "ok"


def _ctx(roles: RoleCardService, model: Any) -> KernelContext:
    return KernelContext(
        model=model,
        registry=ToolRegistry(),
        roles=roles,
        tracer=NullTracer(),
        settings=Settings(),
        enabled_domains=lambda: [],
        tool_epoch=lambda: 1,
    )


def _role(roles: RoleCardService, role_id: str = "stopper") -> str:
    cards(roles).create(
        RoleCardCreate(
            role_id=role_id,
            role_name=role_id,
            system_prompt="x",
            tool_whitelist=None,
            model_name=None,
        )
    )
    return role_id


class _StreamedFake:
    """一条真·分块流：按 `stop_after` 块之后自己按下"停止"，并记下有没有被 `close()`。

    生产里按停止的是用户（走 `/api/session/{tid}/stop`），这里让她出现在流的中间 —— 这样
    "边界上收手"这件事才有东西可收。
    """

    def __init__(
        self, chunks: list[Any], *, stop_after: int | None = None, thread_id: str = "t"
    ) -> None:
        self.chunks = chunks
        self.stop_after = stop_after
        self.thread_id = thread_id
        self.closed = False
        self.yielded = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> _StreamedFake:
        return self

    def invoke(self, prompt: Any, **kwargs: Any) -> Any:  # noqa: A002 - 与 ChatLike 同名
        return self.chunks[-1]

    def stream(self, prompt: Any, **kwargs: Any) -> Any:  # noqa: A002
        try:
            for chunk in self.chunks:
                yield chunk
                self.yielded += 1
                if self.stop_after is not None and self.yielded >= self.stop_after:
                    request_stop(self.thread_id)
        finally:
            self.closed = True


def _state(role_id: str) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="问一句")],
        "current_role_id": role_id,
        "thread_id": "t",
    }


# ------------------------------------------------------------------ 节点：中途收手


def test_stop_mid_stream_commits_the_partial_and_closes_the_stream(
    roles: RoleCardService,
) -> None:
    """停在哪儿，历史就到哪儿；而那条流必须被关掉（不关就是没停）。"""
    fake = _StreamedFake(
        [
            AIMessageChunk(content="雨后"),
            AIMessageChunk(content="她走了很远"),
            AIMessageChunk(content="这一段不该出现"),
        ],
        stop_after=2,
    )
    out = call_model(_state(_role(roles)), _ctx(roles, fake))
    committed = str(out["messages"][0].content)
    assert committed == "雨后她走了很远"
    assert "这一段不该出现" not in committed
    assert fake.closed is True, "没 close 的「停」只是不看了，模型还在往这条流里写"


def test_stop_drops_tool_calls_that_never_finished(roles: RoleCardService) -> None:
    """丢掉半截的工具调用之后，路由自然走向结束 —— 停不该顺手替她把事做了。"""
    fake = _StreamedFake(
        [
            AIMessageChunk(
                content="我想查一下",
                tool_calls=[{"name": "list_roles", "args": {}, "id": "c1", "type": "tool_call"}],
            ),
            AIMessageChunk(content="然后按了停止"),
            AIMessageChunk(content="后面还有"),
        ],
        stop_after=2,
    )
    out = call_model(_state(_role(roles)), _ctx(roles, fake))
    message = out["messages"][0]
    assert isinstance(message, AIMessage)
    assert not message.tool_calls
    assert "然后按了停止" in str(message.content)
    # 丢 tool_calls 的重建不丢"被叫停"的记号 —— 标记在 call_model 里跟着收尾一起写。
    assert message.additional_kwargs.get("stopped") is True


def test_the_partial_committed_by_a_stop_carries_a_persistent_marker(
    roles: RoleCardService,
) -> None:
    """R26-13 尾：半截进历史时**自带**"被叫停"的记号 —— 刷新后的回放才标得出来。

    界面那句提示原先只活在 SSE 的 `End.stopped` 与页面 state 上，一刷新就丢；
    写进落库那条消息的 `additional_kwargs` 才算进历史（与 reasoning 同一条道理）。
    """
    fake = _StreamedFake(
        [
            AIMessageChunk(content="雨后"),
            AIMessageChunk(content="她走了很远"),
            AIMessageChunk(content="这一段不该出现"),
        ],
        stop_after=2,
    )
    out = call_model(_state(_role(roles)), _ctx(roles, fake))
    (message,) = out["messages"]
    assert message.additional_kwargs.get("stopped") is True
    assert message.additional_kwargs.get("created_at"), "标记不能挤掉时间戳"


def test_a_finished_turn_carries_no_stop_marker(roles: RoleCardService) -> None:
    """说完了就是说完了：正常收尾的消息不带"被叫停"，回放不冤枉一句完整的话。"""
    fake = _StreamedFake(
        [AIMessageChunk(content="说完了"), AIMessageChunk(content="，就这样。")],
        stop_after=None,
    )
    out = call_model(_state(_role(roles)), _ctx(roles, fake))
    (message,) = out["messages"]
    assert "stopped" not in message.additional_kwargs


def test_stop_before_the_call_raises_and_commits_nothing(roles: RoleCardService) -> None:
    """停在模型调用**开始之前**（多半是工具还跑着）：不提交一条空 AI 消息 ——
    那会在界面上留下一个没人说过的气泡。"""
    fake = _StreamedFake([AIMessageChunk(content="根本不该被叫")])
    request_stop("t")
    with pytest.raises(TurnStopped):
        call_model(_state(_role(roles)), _ctx(roles, fake))
    assert fake.yielded == 0


# ------------------------------------------------------------------ 轮次：旗子与 End


class _OneNodeGraph:
    """够用的"图"：`stream()` 按 LangGraph 的形状吐 `(mode, payload)`，并记下有没有被关。"""

    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks
        self.closed = False

    def stream(self, _input: Any, *, config: Any = None, stream_mode: Any = None) -> Any:
        try:
            for chunk in self.chunks:
                yield ("messages", (chunk, {"langgraph_node": "call_model"}))
        finally:
            self.closed = True


def _end_event(events: list[Any]) -> End:
    ends = [e for e in events if isinstance(e, End)]
    assert ends, f"没有 End 事件：{[type(e).__name__ for e in events]}"
    return ends[-1]


def test_end_reports_stopped_when_the_user_called_it(roles: RoleCardService) -> None:
    """中途停：事件流照常收尾，但 `End.stopped=True` —— 界面据此标"已停止"而不是发错误。"""
    graph = _OneNodeGraph([AIMessageChunk(content="半句话", id="m1")])
    events: list[Any] = []
    for ev in run_turn(
        graph,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "t"}},
        role_summary={"role_id": "r", "role_name": "r"},
    ):
        events.append(ev)
        request_stop("t")  # 用户在她开口之后立刻按了停止
    assert _end_event(events).stopped is True


def test_abandoned_turn_asks_for_stop_so_the_model_does_not_run_on(
    roles: RoleCardService,
) -> None:
    """客户端关了页面（生成器被 close）⇒ 这一轮没人要了 ⇒ 立起取消旗。

    这是 #18 的另一半：用户以为"关掉窗口就停了"，而实测那边还在跑 —— 现在消费循环在
    下一个块边界就会看到旗子并收手（`call_model` 那半边由上面三条钉住）。
    """
    graph = _OneNodeGraph(
        [AIMessageChunk(content="第一块", id="m1"), AIMessageChunk(content="第二块", id="m1")]
    )
    gen = run_turn(
        graph,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "t"}},
        role_summary={"role_id": "r", "role_name": "r"},
    )
    next(gen)  # 只拿一个事件就走人（等价于浏览器 abort）
    gen.close()
    assert stop_requested("t") is True


def test_a_new_turn_clears_the_stale_stop(roles: RoleCardService) -> None:
    """上一句的"停"不顺延到下一句：新一轮开始就把旗子擦掉，否则第二次永远停在第一块上。"""
    request_stop("t")
    events = list(
        run_turn(
            _OneNodeGraph([AIMessageChunk(content="这次跑完了", id="m1")]),
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "t"}},
            role_summary={"role_id": "r", "role_name": "r"},
        )
    )
    assert _end_event(events).stopped is False
    assert stop_requested("t") is False


def test_the_stale_stop_is_cleared_only_after_the_write_lock_is_held(
    roles: RoleCardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R26-02：擦旗必须在**拿到写锁之后**，否则新一轮会白吃上一轮补下的那个"停"。

     races 的成因是顺序而不是运气：上一轮断连时那记 `request_stop` 是它自己 `finally` 里补的，
    而它跑在 `release_thread` **之前** —— 锁还在它手里。所以"先抢锁再擦旗"就把这件事变成
    了互斥保证的推论；反过来（原先那样先擦再抢）就留着一个窗口：擦完 → 上一轮补停 →
    这一轮抢到锁开跑 → 内核第一次检查旗子就 `TurnStopped`，整轮零产出。

    这里断的是**调用顺序**而不是并发：真起线程去踩那个窗口会是个看调度器脸色的用例，
    而这条要钉的恰恰就是"顺序"本身。
    """
    from rolecard_agent.core import turn as turn_mod

    order: list[str] = []
    real_try, real_clear = turn_mod.try_thread_write, turn_mod.clear_stop

    def try_w(tag: str, **kw: Any) -> bool:
        order.append("lock")
        return real_try(tag, **kw)

    def clear(tag: str) -> None:
        order.append("clear")
        real_clear(tag)

    monkeypatch.setattr(turn_mod, "try_thread_write", try_w)
    monkeypatch.setattr(turn_mod, "clear_stop", clear)
    request_stop("t")  # 上一轮留下的旗子

    events = list(
        run_turn(
            _OneNodeGraph([AIMessageChunk(content="这一轮该跑完", id="m1")]),
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "t"}},
            role_summary={"role_id": "r", "role_name": "r"},
        )
    )

    assert order[:2] == ["lock", "clear"], f"擦旗发生在抢锁之前：{order}"
    assert _end_event(events).stopped is False
    assert stop_requested("t") is False


def test_a_turn_that_never_got_the_lock_does_not_clear_the_inflight_stop(
    roles: RoleCardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R28-01：抢锁**超时**的那一轮不许擦旗 —— 那颗旗子是上一轮还在飞时用户按下的。

    上一条钉的是"抢到锁之后才清"的顺序，这一条钉它的前件：没抢到就什么都别清。
    漏了这一半的症状是"我按了停止她还在说"（旗子被下一轮抹掉，在飞那轮再也看不见它），
    而且下一轮还顺带在无互斥的情况下跟上一轮分叉同一个检查点。
    """
    from rolecard_agent.core import turn as turn_module

    cleared: list[str] = []
    monkeypatch.setattr(turn_module, "try_thread_write", lambda *a, **k: False)
    monkeypatch.setattr(turn_module, "clear_stop", cleared.append)
    request_stop("t")  # 上一轮按下的停

    list(
        run_turn(
            _OneNodeGraph([AIMessageChunk(content="这一轮本来就该让路", id="m1")]),
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "t"}},
            role_summary={"role_id": "r", "role_name": "r"},
        )
    )

    assert cleared == [], "没抢到锁也擦旗 = 把上一轮那个「停止」吞掉"


# ------------------------------------------------------------------ 节点：空响应


def test_an_empty_stream_raises_instead_of_committing_a_blank_message(
    roles: RoleCardService,
) -> None:
    """模型一个块都没吐 ⇒ 抛 `EmptyModelStream`，**不往历史里塞空气泡**（`R28-06`）。

    原来的代码在这里 `return AIMessage(content="")`，而它头顶那句注释写的是"这一轮没有内容
    可提交" —— 注释与代码是两件事。空串能过守卫，于是检查点里落一条谁都没说过的 AI 消息：
    界面上是一个点开什么都没有的气泡，下一轮的 prompt 还带着这条空白，而日志里这次调用
    看起来是**成功**的。
    """
    fake = _StreamedFake([], thread_id="t")
    with pytest.raises(EmptyModelStream):
        call_model(_state(_role(roles)), _ctx(roles, fake))
    assert fake.closed is True, "抛出去之前也要把底层那条流关掉"


def test_the_empty_stream_says_its_own_sentence_not_the_generic_one() -> None:
    """那句话必须是"没有返回任何内容 / 换个后端"，不是通用的"换一种问法"。

    通用那句会把人推向**重复问同一个问题**，而后端一个字都没吐不是问法的问题 ——
    这是文案，也是这条缺陷剩下的一半（另一半是不落空气泡，上面那条钉）。
    """
    assert model_error_detail(EmptyModelStream("模型返回了空响应（一个块都没有）")) == (
        EMPTY_STREAM_DETAIL
    )
    assert model_error_detail(EmptyModelStream("…")) != _GENERIC_MODEL_FAILURE
