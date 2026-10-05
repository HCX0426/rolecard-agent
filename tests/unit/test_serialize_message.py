"""`serialize_message` 的回放契约测试。

最关键的一条：**思考内容必须随消息一起回放**。一轮结束后前端会用 checkpoint 回放
**整体替换**消息区，乐观气泡（连同流式思考）被销毁——如果 reasoning 不进回放数据，
用户回答一看完就永远失去了推理过程（用户实测反馈）。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rolecard_agent.api.message_view import serialize_message


def test_plain_ai_message_has_no_reasoning_field() -> None:
    row = serialize_message(AIMessage(content="答"))
    assert row["role"] == "assistant"
    assert row["content"] == "答"
    assert "reasoning" not in row  # 非思考模型不该带空字段去污染前端


def test_thinking_is_replayed_with_the_message() -> None:
    """思考模型：reasoning 进入回放数据 —— 这是"思考完还能看"的前提。"""
    row = serialize_message(
        AIMessage(
            content="答：先想后答。",
            additional_kwargs={"reasoning_content": "内心戏：边界要想清楚。"},
        )
    )
    assert row["reasoning"] == "内心戏：边界要想清楚。"
    assert row["content"] == "答：先想后答。"


def test_stopped_marker_is_replayed_with_the_message() -> None:
    """被叫停的半句带 stopped 标记进回放（R26-13 尾）：刷新后才标得出"没说完"。

    界面那句提示原先只活在 SSE 的 `End.stopped` 与页面 state 上，一刷新就丢；
    写进检查点的才是历史的一部分（与 reasoning 同一条道理）。
    """
    row = serialize_message(AIMessage(content="说到一半的", additional_kwargs={"stopped": True}))
    assert row["stopped"] is True


def test_finished_message_has_no_stopped_field() -> None:
    """正常收尾不带这个键：不给前端一个要判空的字段，也不冤枉一句完整的话。"""
    row = serialize_message(AIMessage(content="说完了。"))
    assert "stopped" not in row


def test_tool_calls_are_still_listed_alongside_reasoning() -> None:
    """带工具调用的思考轮次：tools 与 reasoning 要同时保留（两者都是可观测性的一部分）。"""
    message = AIMessage(
        content="查到了。",
        additional_kwargs={"reasoning_content": "先检索知识库。"},
        tool_calls=[{"name": "search_knowledge", "args": {"query": "随访"}, "id": "call-1"}],
    )
    row = serialize_message(message)
    assert row["tools"] == ["search_knowledge"]
    assert row["reasoning"] == "先检索知识库。"


def test_other_message_shapes_are_unchanged() -> None:
    user_row = serialize_message(HumanMessage(content="问"))
    assert user_row["role"] == "user" and user_row["content"] == "问"
    assert "id" in user_row  # 前端编辑/删除需要按 id 寻址
    tool_row = serialize_message(
        ToolMessage(content="结果", name="search_knowledge", tool_call_id="call-1")
    )
    assert tool_row["role"] == "tool" and tool_row["content"] == "结果"
    assert "id" in tool_row


def test_tool_message_carries_call_args_when_paired() -> None:
    """工具行回放要能显示"搜了什么"：call_args 按 tool_call_id 配对进 args（用户反馈）。"""
    row = serialize_message(
        ToolMessage(content="搜索结果…", tool_call_id="call_1", name="web_search"),
        call_args={"call_1": {"query": "崩坏3 最新版本"}},
    )
    assert row["role"] == "tool"
    assert row["args"] == {"query": "崩坏3 最新版本"}


def test_tool_message_without_pairing_has_no_args_field() -> None:
    """没配对（旧数据/异常路径）不带空 args 字段去污染前端。"""
    row = serialize_message(
        ToolMessage(content="结果", tool_call_id="call_9", name="web_search"),
    )
    assert "args" not in row


def test_multimodal_user_message_replays_text_and_image() -> None:
    """多模态传图（2026-09-18）：text + image_url 块 → 文本进 content、图进 image。"""
    row = serialize_message(
        HumanMessage(
            content=[
                {"type": "text", "text": "这张图是什么？"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ]
        )
    )
    assert row["role"] == "user"
    assert row["content"] == "这张图是什么？"
    assert row["image"] == "data:image/png;base64,AAA"


def test_plain_user_message_has_no_image_field() -> None:
    """纯文本用户消息不带 image 字段（不污染旧消息渲染）。"""
    row = serialize_message(HumanMessage(content="纯文本"))
    assert row["role"] == "user"
    assert row["content"] == "纯文本"
    assert "image" not in row
