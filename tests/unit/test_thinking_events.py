"""思考（reasoning）事件的发出纪律：一条流里同一段思考**只许出现一次**。

为什么单独一个文件（2026-11-08）：思考这一族前后修过两次（10-31 补 OpenAI 兼容端的开关与
捕获、11-08 补 `delta.reasoning` 这个第二个键名），而 `run_turn` 里那两条发出通路
（流式增量 / committed 兜底）**从来没有一条用例同时走过**。`tests/test_sessions_api.py`
那条只走兜底 —— 它用的 `ScriptedChat.stream()` 交出的是一整条 `AIMessage`（不是
`AIMessageChunk`），流式那一路当场被 `isinstance` 挡掉，于是 `len(thinking) == 1` 恒真。

结果就是 11-08 那次补完捕获之后，真机实测立刻变成**双份**：

    187 个 thinking 事件、累计 1802 字，而落库那条只有 901 字 —— 恰好 2×；
    最后一个块 717 字，与前面所有块的拼接**逐字相等**。

成因在 `turn.py` 那对旗子：`think_emitted` 只由兜底那支置位，流式那支在
`_from_message_chunk` 里发完就走、**没有把手伸回来置位**（那个函数收不到这个局部变量）。
所以只要模型真的流式吐了思考，兜底那支就会把整段再发一遍。前端
`stream.ts` 的归约是 `thinking: bubble.thinking + ev.text`（纯拼接），面板里就是同一段
内心戏连写两遍。

三条判据：
  ① 流式带思考 ⇒ 每个增量一块、**且 committed 不再重发整段**（这一条就是那个双份）；
  ② 只有 committed 带（非流式后端 / 评测脚本）⇒ 兜底照旧发一条整段，不能被 ① 的修法吞掉；
  ③ 工具循环里多次调用 ⇒ 每次各论各的，前一次发过不能把后一次的思考吞掉。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from rolecard_agent.core.agent.turn import Thinking, run_turn

REASON = "内心戏：先把边界想清楚。"


def _events(graph: Any) -> list[Any]:
    return list(
        run_turn(
            graph,
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "t"}},
            role_summary={"role_id": "r", "role_name": "思考测试角色"},
        )
    )


def _thinking(events: list[Any]) -> list[str]:
    return [e.text for e in events if isinstance(e, Thinking)]


class _StreamThenCommit:
    """一条真·流式调用：先交分块（messages 模式），再交提交（updates 模式）。

    节点名用 `MODEL_NODE`（"model"）—— 写成 "call_model" 会让 updates 那支整个不进分支，
    兜底通路一次都不跑，测试就只是在演"没测到东西"（11-08 现场就差点这么演过去）。
    """

    def __init__(self, calls: list[tuple[list[AIMessageChunk], AIMessage]]) -> None:
        self.calls = calls

    def stream(self, _input: Any, *, config: Any = None, stream_mode: Any = None) -> Any:
        for chunks, committed in self.calls:
            for chunk in chunks:
                yield "messages", (chunk, {"langgraph_node": "model"})
            yield "updates", {"model": {"messages": [committed]}}


def _delta(text: str, *, content: str = "", reasoning: str | None = None) -> AIMessageChunk:
    kwargs = {"reasoning_content": reasoning} if reasoning else {}
    return AIMessageChunk(content=content, additional_kwargs=kwargs)


def test_streamed_thinking_is_not_repeated_by_the_committed_message() -> None:
    """① 流式已经发过的思考，committed 兜底**不能再发一遍整段**。

    判据不是"事件条数"这种脆弱形状，而是**拼接结果**：面板里那段字必须等于思考本身，
    不多不少 —— 双份的症状恰恰是"字数翻倍、内容重复"，条数断言（>=1）抓不住它。
    """
    events = _events(
        _StreamThenCommit(
            [
                (
                    [_delta("", reasoning="第一段。"), _delta("", reasoning="第二段。")],
                    AIMessage(
                        content="答", additional_kwargs={"reasoning_content": "第一段。第二段。"}
                    ),
                )
            ]
        )
    )
    joined = "".join(_thinking(events))
    assert joined == "第一段。第二段。", f"思考被发了不止一遍：{joined!r}"
    assert len(_thinking(events)) == 2, "增量各自一块，不该再多出一条整段"


def test_non_streaming_reply_still_gets_its_one_thinking_event() -> None:
    """② 兜底那一路不能被修法吞掉：只有 committed 带思考时，必须发**恰好一条**整段。

    评测脚本与非流式后端走的就是这一路（`ScriptedChat.stream()` 一次交出整条
    `AIMessage`，不是 `AIMessageChunk`）。只修 ① 而把兜底整条删掉，症状会从"双份"
    变成"非流式后端永远看不到思考"—— 那是把一个可见缺陷换成一个静默缺陷。
    """
    events = _events(
        _StreamThenCommit(
            [
                (
                    [_delta("答")],
                    AIMessage(content="答", additional_kwargs={"reasoning_content": REASON}),
                )
            ]
        )
    )
    assert _thinking(events) == [REASON]


def test_each_call_in_a_tool_loop_gets_its_own_thinking() -> None:
    """③ 工具循环里两次模型调用：第一次流式发过，不能把第二次的思考一起吞掉。

    旗子必须是**每次调用各论各的**，不是一轮一把。写成"这一轮发过就不再发"的修法在这里
    会红：第二次调用没走增量（评测/非流式混在同一个工具循环里），思考就静默消失了。
    """
    events = _events(
        _StreamThenCommit(
            [
                (
                    [_delta("", reasoning="第一轮的思考。")],
                    AIMessage(
                        content="",
                        additional_kwargs={"reasoning_content": "第一轮的思考。"},
                        tool_calls=[{"name": "t", "args": {}, "id": "c1"}],
                    ),
                ),
                (
                    [_delta("答")],
                    AIMessage(
                        content="答", additional_kwargs={"reasoning_content": "第二轮的思考。"}
                    ),
                ),
            ]
        )
    )
    assert _thinking(events) == ["第一轮的思考。", "第二轮的思考。"]
