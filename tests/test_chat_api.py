"""聊天接入层测试（M4：会话管理 + SSE 流式对话）。

Traceability: US-1（会话切角色、历史保留）, US-3（插件启停即时生效的服务端部分）,
US-7（演示界面的后端）。US-7 的完整验收（60 秒演示视频）仍随演示录制。

全部离线：`ScriptedChat` 注入 `create_app`，SSE 流在 TestClient 中完整消费。
ScriptedChat 不产生令牌级回调，所以 token 事件不会出现 —— 助手文本经由
`message_replace`（权威文本对账）路径到达，这恰好覆盖了非流式模型 / guard 改写 /
角色缺失兜底句共用的那条"文本与令牌不一致"分支。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from tests.conftest import ScriptedChat


@pytest.fixture
def model() -> ScriptedChat:
    return ScriptedChat()


@pytest.fixture
def client(tmp_path: Path, model: ScriptedChat) -> Iterator[TestClient]:
    app = create_app(sqlite_path=tmp_path / "app.db", model=model)
    with TestClient(app) as c:
        yield c


def parse_sse(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def make_session(client: TestClient, **payload: object) -> dict[str, object]:
    res = client.post("/api/session", json=payload or {})
    assert res.status_code == 201
    out: dict[str, object] = res.json()
    return out


def chat(client: TestClient, thread_id: str, message: str) -> list[dict[str, object]]:
    res = client.post("/api/chat", json={"thread_id": thread_id, "message": message})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    return parse_sse(res.text)


def types_of(events: list[dict[str, object]]) -> list[str]:
    return [str(e["type"]) for e in events]


# -- session ----------------------------------------------------------------------


def test_create_session_defaults_to_builtin_role(client: TestClient) -> None:
    """不指定角色 → 绑定内置健康档案管理员。"""
    session = make_session(client)
    assert str(session["thread_id"]).startswith("s_")
    assert session["role_id"] == "medical_archivist"
    assert session["role_name"] == "健康档案管理员"


def test_create_session_unknown_role_404(client: TestClient) -> None:
    assert client.post("/api/session", json={"role_id": "ghost"}).status_code == 404


def test_get_session_reports_current_role(client: TestClient) -> None:
    session = make_session(client)
    res = client.get(f"/api/session/{session['thread_id']}")
    assert res.status_code == 200
    assert res.json()["role_name"] == "健康档案管理员"


def test_switch_session_role_keeps_thread(client: TestClient) -> None:
    """US-1：切角色只改 current_role_id，线程仍然存在且指向新角色。"""
    session = make_session(client)
    client.post(
        "/api/roles",
        json={"role_id": "greeter", "role_name": "迎宾", "system_prompt": "你是前台。"},
    )
    res = client.patch(f"/api/session/{session['thread_id']}", json={"role_id": "greeter"})
    assert res.status_code == 200
    assert res.json()["role_id"] == "greeter"

    after = client.get(f"/api/session/{session['thread_id']}").json()
    assert after["role_id"] == "greeter"

    # 目标角色不存在 / 线程不存在 → 404
    assert (
        client.patch(f"/api/session/{session['thread_id']}", json={"role_id": "ghost"}).status_code
        == 404
    )
    assert client.patch("/api/session/nope", json={"role_id": "greeter"}).status_code == 404


# -- chat -------------------------------------------------------------------------


def test_chat_delivers_authoritative_text(client: TestClient) -> None:
    """直答轮：即使模型脚本耗尽返回兜底句，文本也经 message_replace 完整到达并以 end 收尾。"""
    session = make_session(client)
    events = chat(client, str(session["thread_id"]), "你好")
    assert types_of(events) == ["start", "message_replace", "end"]
    assert events[0]["role"]["role_id"] == "medical_archivist"
    assert events[1]["text"] == "(script exhausted)"  # ScriptedChat 的耗尽兜底句


def test_chat_delivers_scripted_reply_exactly(client: TestClient, model: ScriptedChat) -> None:
    session = make_session(client)
    model.replies = [AIMessage(content="你好，我是健康档案管理员。")]
    events = chat(client, str(session["thread_id"]), "你好")
    replace = [e for e in events if e["type"] == "message_replace"]
    assert len(replace) == 1
    assert replace[0]["text"] == "你好，我是健康档案管理员。"


def test_chat_second_turn_sees_history(client: TestClient, model: ScriptedChat) -> None:
    """US-1 / 会话续接：第二轮模型的输入里必须能看见第一轮的问答（checkpoint 生效）。"""
    session = make_session(client)
    model.replies = [AIMessage(content="第一次回答"), AIMessage(content="第二次回答")]

    chat(client, str(session["thread_id"]), "你好")
    chat(client, str(session["thread_id"]), "再次提问")

    second_call_messages = model.calls[1]["messages"]
    contents = [getattr(m, "content", "") for m in second_call_messages]
    assert "你好" in contents  # 第一轮用户消息
    assert "第一次回答" in contents  # 第一轮助手回复（来自 checkpoint，而非本轮注入）
    assert "再次提问" in contents  # 本轮新消息


def test_chat_tool_roundtrip_uses_real_registry(client: TestClient, model: ScriptedChat) -> None:
    """US-3 / US-2：模型调 list_domains → 真实注册表里的内核工具执行 → 工具结果回给模型。"""
    session = make_session(client)
    model.replies = [
        AIMessage(content="", tool_calls=[{"name": "list_domains", "args": {}, "id": "c1"}]),
        AIMessage(content="当前启用的领域插件是 health。"),
    ]
    events = chat(client, str(session["thread_id"]), "你现在有哪些能力？")
    kinds = types_of(events)
    assert "tool_call" in kinds and "tool_result" in kinds

    call = next(e for e in events if e["type"] == "tool_call")
    assert call["name"] == "list_domains"
    result = next(e for e in events if e["type"] == "tool_result")
    assert "health" in str(result["content"])  # 来自真实 ToolRegistry，而不是桩

    replace = [e for e in events if e["type"] == "message_replace"]
    assert replace[0]["text"] == "当前启用的领域插件是 health。"
    assert kinds[-1] == "end"


def test_chat_guard_replaces_streaming_output(client: TestClient, model: ScriptedChat) -> None:
    """US-4：违规回复绝不外发 —— SSE 正文里没有触发原文，只有权威改写文本。"""
    session = make_session(client)
    model.replies = [AIMessage(content="好的，这种情况建议你服用阿司匹林。")]
    events = chat(client, str(session["thread_id"]), "我头疼吃什么药？")

    assert types_of(events) == ["start", "message_replace", "end"]
    body = json.dumps(events, ensure_ascii=False)
    assert "建议你服用" not in body
    assert "阿司匹林" not in body
    replace = [e for e in events if e["type"] == "message_replace"]
    assert "职责范围" in str(replace[0]["text"])  # guard 的 BLOCKED_RESPONSE


def test_chat_unknown_thread_404(client: TestClient) -> None:
    res = client.post("/api/chat", json={"thread_id": "nope", "message": "hi"})
    assert res.status_code == 404


def test_chat_rejects_empty_message(client: TestClient) -> None:
    session = make_session(client)
    res = client.post("/api/chat", json={"thread_id": session["thread_id"], "message": ""})
    assert res.status_code == 422
