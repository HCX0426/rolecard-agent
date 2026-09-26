"""在飞正文登记（R26-38）—— 桌宠那一轮正在说的字，第二个读者要立刻读得到。

用户报的症状："在桌宠那边发的，收到回答在对话界面同步得有些慢。"
副本库上自起后端量过一轮（419 字、云端档）：他那句 0.21 秒就可读，她的整句 10.49 秒
才进检查点，界面那个 5 秒网格把它推到 15.0 秒 —— 12.1 秒的落后里 7.6 秒是
**"第二个读者全程读不到她正在说"**，那不是轮询频率问题，所以登记要落在投送那一层。

这里钉的是登记的两条边界：
1. **不早于投送**：守卫扣住的尾巴（`WINDOW` 个字符）不进登记 —— 提前给另一个读者看
   等于绕过 fail-closed；
2. **不晚于投送、不漏一格**：整轮中间任何时刻读它都不是 None，否则"她在打字"那一格
   在界面上就是空的，而这正是用户等的那 7.6 秒里唯一可见的证据。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from rolecard_agent.core.graph import MODEL_NODE
from rolecard_agent.core.thread_locks import (
    inflight_append,
    inflight_begin,
    inflight_end,
    inflight_replace,
    inflight_text,
)
from rolecard_agent.core.turn import MessageReplace, run_turn


def _run(graph: Any, tid: str) -> list[Any]:
    return list(
        run_turn(
            graph,
            graph_input={},
            config={"configurable": {"thread_id": tid}},
            role_summary={"role_id": "r", "role_name": "在飞"},
        )
    )


class _Chunky:
    """一条假图流：按 `pieces` 投 `messages` 块，再把 `committed` 作为节点更新交出来。"""

    def __init__(self, body: str, *, piece: int = 10, commit: bool = True) -> None:
        self.body = body
        self.piece = piece
        self.commit = commit

    def stream(self, *_a: Any, **_kw: Any) -> Any:
        for i in range(0, len(self.body), self.piece):
            yield (
                "messages",
                (
                    AIMessageChunk(content=self.body[i : i + self.piece]),
                    {"langgraph_node": MODEL_NODE, "langgraph_step": 1},
                ),
            )
        if self.commit:
            yield (
                "updates",
                {MODEL_NODE: {"messages": [AIMessage(content=self.body)]}},
            )


def test_absent_is_not_the_same_as_running_with_no_text_yet() -> None:
    """"没人在生成"与"在生成、一个字都还没投送"必须是两个读数。

    分不清的话界面上那一格永远显不出来，而后者正是用户盯着空白的那几秒里唯一的事实。
    """
    assert inflight_text("i0") is None
    inflight_begin("i0")
    assert inflight_text("i0") == "", "键在 = 在飞，哪怕还没有字"
    inflight_append("i0", "第一段")
    inflight_append("i0", "")  # 空增量不该凭空造东西
    assert inflight_text("i0") == "第一段"
    inflight_replace("i0", "整条替换后的权威文本")
    assert inflight_text("i0") == "整条替换后的权威文本"
    inflight_end("i0")
    assert inflight_text("i0") is None


def test_helpers_tolerate_no_thread_id_and_a_stray_end() -> None:
    """与停旗同一套纪律：没有 thread_id 就没有可登记的对象，多清一次不该制造新失败。"""
    inflight_begin("")
    inflight_append("", "字")
    inflight_replace("", "字")
    inflight_end("")
    assert inflight_text("") is None
    inflight_end("从没 begin 过的会话")


def test_a_turn_is_registered_from_start_to_end_and_matches_the_emitted_text() -> None:
    body = "今天想先把那几份报告理一理，晚上再去跑步。" * 4
    emitted = ""
    steps: list[tuple[str, str | None]] = []
    for ev in run_turn(
        _Chunky(body),
        graph_input={},
        config={"configurable": {"thread_id": "i1"}},
        role_summary={"role_id": "r", "role_name": "在飞"},
    ):
        # 与客户端同一个契约：Token 往上加，MessageReplace 整条换
        if type(ev).__name__ == "Token":
            emitted += ev.text
        elif isinstance(ev, MessageReplace):
            emitted = ev.text
        steps.append((type(ev).__name__, inflight_text("i1")))
        if type(ev).__name__ != "End":
            # 逐格严格一致：读到第几个事件的那一刻，登记里正好是到那一步投出去的全部字
            assert inflight_text("i1") == emitted

    assert steps[0] == ("Start", ""), "Start 那一刻登记就该立起来（界面上是「她在打字」）"
    # End 那一格读到的还是旧的（登记在生成器 `finally` 里清，那时它尚未退出），
    # 所以「清干净」要在流跑完之后再查 —— 这正好也是另一个界面读它的那个时机。
    assert steps[-1][0] == "End"
    assert all(text is not None for _, text in steps), "整轮中间任何一格都不许读成「没人在生成」"
    assert inflight_text("i1") is None, "一轮跑完必须清掉"
    assert emitted == body, "投送出去的是全文（守卫的尾巴在提交那一刻放出来）"


def test_the_guarded_tail_never_enters_the_registry() -> None:
    """被 `WINDOW` 扣住的尾巴不进登记：那一段还没过完整式子的检查。

    变异核验的靶子：把 `run_turn` 改成登记守卫的原始累加器（`guard.buffer`）而不是投送
    出去的增量，这条当场红 —— 症状是"另一个界面先看见了还没过审的字"。
    """
    from rolecard_agent.core.turn import WINDOW

    body = "一二三四五六七八九十" * 6  # 60 字，跨过 WINDOW
    # 只投块、不给提交：这样 End 之前最后一次读数就是"投送出多少、登记里有多少"
    events = _run(_Chunky(body, commit=False), "i2")
    tokens = [e.text for e in events if type(e).__name__ == "Token"]
    assert tokens, "这段长度足够投送出东西，否则用例测不到边界"
    assert len("".join(tokens)) < len(body), "前提：尾巴确实被守卫扣住了"
    assert len(body) - len("".join(tokens)) <= WINDOW, "前提：扣住的不超过一个窗口"
    assert inflight_text("i2") is None, "登记随 End 清掉"


def test_message_replace_rewrites_the_whole_registered_line() -> None:
    """已提交文本与投送文本分叉时（guard 改写、兜底句），登记跟着整条换。

    不跟着换的话，第二个读者会停在一段**已经不算数**的字上 —— 桌宠那边整条替换掉的
    是同一件事，两边就该是同一份。
    """
    streamed = "模型逐块吐出来的原文，这一句其实并没有真的成立。" * 2
    authoritative = "换成了另一句权威文本。"

    class _Diverging:
        def stream(self, *_a: Any, **_kw: Any) -> Any:
            yield (
                "messages",
                (
                    AIMessageChunk(content=streamed),
                    {"langgraph_node": MODEL_NODE, "langgraph_step": 1},
                ),
            )
            yield (
                "updates",
                {MODEL_NODE: {"messages": [AIMessage(content=authoritative)]}},
            )

    seen: dict[str, str | None] = {}
    for ev in run_turn(
        _Diverging(),
        graph_input={},
        config={"configurable": {"thread_id": "i3"}},
        role_summary={"role_id": "r", "role_name": "在飞"},
    ):
        if isinstance(ev, MessageReplace):
            seen["at_replace"] = inflight_text("i3")
    assert seen.get("at_replace") == authoritative, (
        "整条替换之后登记里还留着被替换掉的那段 ⇒ 另一个界面会停在一段不算数的字上"
    )
    assert inflight_text("i3") is None
