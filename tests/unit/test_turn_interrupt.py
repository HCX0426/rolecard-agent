"""客户端中途断开时，"她实际说出口的那半句"该不该进历史（审计 §8.5 的 P3）。

钉四件事：

  1. **断开 → 补写**：消费到一半就 close 掉生成器（就是浏览器点"停止生成"时 SSE 那边
     发生的事），检查点里必须多出那半句 —— 否则它只存在于用户的屏幕上，她说"继续"时
     角色不知道刚才说到哪儿；
  2. **跑完 → 不补**：走到 End 的一轮什么都不写（写一次就是同一条话说两遍）；
  3. **模型节点已经提交过 → 不补**：那时历史里已经有她说过什么；
  4. **尾巴是省略号，不是机器标记**：§8.13 量过"她每句话都带（动作）"，成因就是历史里
     长那样。所以这里断言的是"内容里没有中括号/方括号标记"。

全部离线：假图直接给 `messages` 模式的原始块，形状与 langgraph 一致。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from rolecard_agent.core.turn import run_turn


class _Graph:
    """够用的假图：`.stream` 回放给定的块，`.get_state` / `.update_state` 记在案上。"""

    def __init__(self, chunks: list[Any], *, history: list[Any]) -> None:
        self._chunks = chunks
        self.history = list(history)
        self.writes: list[dict[str, Any]] = []

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        for payload in self._chunks:
            # 真图在 `updates` 那一步就把消息写进了检查点；假图也得照做，否则
            # "已经提交过就别再补一条"那条判断根本没有对象可判。
            if payload[0] == "updates":
                for update in (payload[1] or {}).values():
                    self.history = self.history + list((update or {}).get("messages") or [])
            yield payload

    def get_state(self, _config: Any) -> Any:
        return SimpleNamespace(values={"messages": self.history})

    def update_state(self, _config: Any, values: dict[str, Any]) -> None:
        self.writes.append(values)
        self.history = self.history + list(values.get("messages") or [])


class _Tracer:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [getattr(e, "event", "") for e in self.events]


def _tokens(*pieces: str) -> list[Any]:
    """messages 模式的块（元数据里那两个键正是 `_from_message_chunk` 记用量用的）。

    片段都取得**比守卫窗口（`WINDOW = 32`）长**：短于窗口的文本一条都不会投送出去，
    而这一组用例要的正是"投送出去的那部分"—— 没投出去的东西用户没看到，不该进历史。
    """
    meta = {"langgraph_node": "call_model", "langgraph_step": 3}
    return [("messages", (AIMessageChunk(content=part), meta)) for part in pieces]


#: 一段足够长的"她正在说的话"（>32 字，跨过守卫窗口）。
SPOKEN = "今天的风很温柔，像在替谁说着悄悄话呢，我趴在窗台上看楼下的车一辆一辆地开过去，"
NEXT = "忽然想起你说过的那句关于海的话，"
TAIL = "海那边你小时候住过的巷子"


def _committed(text: str) -> list[Any]:
    return [("updates", {"call_model": {"messages": [AIMessage(content=text)]}})]


_CONFIG = {"configurable": {"thread_id": "t_interrupt"}}
_ROLE = {"role_id": "r", "role_name": "断开测试"}


def _run(graph: _Graph, *, stop_after: int | None, tracer: Any = None) -> list[Any]:
    """跑一轮：`stop_after=None` 抽干（正常收尾），否则拿到第 N 条 Token 就断开。

    "断开"用的是 `gen.close()` —— 那正是浏览器点停止 / 关窗时 SSE 那半边对同步生成器做的事。
    """
    gen = run_turn(
        graph, graph_input={}, config=_CONFIG, role_summary=_ROLE, tracer=tracer
    )
    seen: list[Any] = []
    if stop_after is None:
        seen.extend(gen)
        return seen
    for event in gen:
        seen.append(event)
        if sum(1 for e in seen if e.sse_type == "token") >= stop_after:
            break
    gen.close()
    return seen


def _asked() -> list[Any]:
    return [HumanMessage(content="说一句长一点的话")]


def test_abandoned_turn_keeps_what_she_actually_said() -> None:
    graph = _Graph(_tokens(SPOKEN, NEXT, TAIL), history=_asked())
    tracer = _Tracer()
    events = _run(graph, stop_after=1, tracer=tracer)
    delivered = "".join(e.text for e in events if e.sse_type == "token")

    assert len(graph.writes) == 1
    written = graph.writes[0]["messages"][0]
    # 留下的正是"用户实际看到的那段"+ 省略号：没投出去的后面几句不该进历史。
    assert str(written.content) == delivered + "…"
    assert NEXT not in delivered and TAIL not in delivered
    assert "turn_interrupted" in tracer.kinds()
    assert graph.history[-1].type == "ai"  # 补完了，下一轮她看得见自己说到哪儿


def test_no_machine_marker_in_the_kept_text() -> None:
    """补写的那条只有省略号 —— 方括号标记会被她学着说出口（§8.13 那个教训）。"""
    graph = _Graph(_tokens(SPOKEN, NEXT), history=_asked())
    _run(graph, stop_after=1)
    content = str(graph.writes[0]["messages"][0].content)
    assert "[" not in content and "]" not in content and "（" not in content
    assert int(graph.writes[0]["messages"][0].additional_kwargs.get("interrupted") or 0) == 1


def test_completed_turn_writes_nothing() -> None:
    """正常跑完的一轮历史由图自己提交，这里一个字都不该再写。"""
    graph = _Graph(_tokens(SPOKEN) + _committed(SPOKEN + NEXT), history=_asked())
    tracer = _Tracer()
    events = _run(graph, stop_after=None, tracer=tracer)
    assert [e.sse_type for e in events][-1] == "end"
    assert graph.writes == []
    assert "turn_interrupted" not in tracer.kinds()


def test_aborted_after_the_node_committed_writes_nothing() -> None:
    """模型节点已经提交过（历史里已经有她的话）→ 再补一条就是同一句说两遍。"""
    graph = _Graph(
        _tokens(SPOKEN) + _committed(SPOKEN + NEXT) + _tokens(TAIL),
        history=_asked(),
    )
    _run(graph, stop_after=2)
    assert graph.writes == []


def test_nothing_spoken_yet_writes_nothing() -> None:
    """一个字都没投出去就断开：没有"实际说出口的部分"可留，别造一条空的 AI 消息。"""
    graph = _Graph(_tokens(SPOKEN), history=_asked())
    gen = run_turn(graph, graph_input={}, config=_CONFIG, role_summary=_ROLE)
    gen.close()  # 一次都没抽
    assert graph.writes == []


def test_write_failure_leaves_the_turn_ending_cleanly() -> None:
    """补历史本身坏了（真库里可能是检查点被别的进程占着）：只留痕，不往外抛。

    这段收尾跑在 `finally` 里 —— 在这里抛异常会把整轮的收尾变成一个看不懂的错误。
    """

    class _Boom(_Graph):
        def update_state(self, _config: Any, _values: Any) -> None:
            raise RuntimeError("检查点写不进去")

    graph = _Boom(_tokens(SPOKEN, NEXT), history=_asked())
    tracer = _Tracer()
    _run(graph, stop_after=1, tracer=tracer)
    assert "turn_interrupt_write_failed" in tracer.kinds()
    assert "turn_interrupted" not in tracer.kinds()
