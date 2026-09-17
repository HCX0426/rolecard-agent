"""`serialize_message` 的回放契约测试。

最关键的一条：**思考内容必须随消息一起回放**。一轮结束后前端会用 checkpoint 回放
**整体替换**消息区，乐观气泡（连同流式思考）被销毁——如果 reasoning 不进回放数据，
用户回答一看完就永远失去了推理过程（用户实测反馈）。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rolecard_agent.api.deps import serialize_message


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
