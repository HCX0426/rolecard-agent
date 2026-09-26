"""她**主动**说过的话要跟着角色走 —— 但不抄进她自己在的那条主动会话（用户 09-26 拍的"并进来"）。

两臂各钉一次，因为这条修法的**全部风险**就压在那条分界上：

  · 控制台「新建对话」开出来的另一条线程里，system 要出现她那句主动开口的原文 ——
    不然"我提醒过你鞋带"这类话永远接不上（真库查过：注入侧原先只有 `role_memory_item`，
    `recent_own_texts` 只被 anti-repeat 用了一次）；
  · 而在那条主动会话（`s_proactive_<role>`）里**不许**出现：同一句话本来就在她的历史里，
    再抄一遍进 system 等于把复读喂回给模型（`nodes._scrub_own_repeats` 治的就是这个），
    还白花 token —— 本地档 `R26-29` 实测过 prompt 越长首字越慢。

全程离线：`ScriptedChat` 注入，不碰任何真模型。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, SystemMessage

from rolecard_agent.api.main import create_app
from rolecard_agent.storage.db import connect
from tests.conftest import ScriptedChat

ECHO = "今天出门前记得检查鞋带哦"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, ScriptedChat, Path]]:
    db = tmp_path / "echo.db"
    model = ScriptedChat([AIMessage(content=f"reply-{i}") for i in range(10)])
    app = create_app(sqlite_path=db, model=model)
    with TestClient(app) as client:
        client.post(
            "/api/roles",
            json={"role_id": "wan", "role_name": "苏晚晴", "system_prompt": "你是苏晚晴。"},
        )
        conn = connect(db)
        conn.execute(
            "INSERT INTO agent_reachout (role_id, role_name, text) VALUES (?, ?, ?)",
            ("wan", "苏晚晴", ECHO),
        )
        conn.commit()
        conn.close()
        yield client, model, db


def _system_seen_by_model(model: ScriptedChat) -> str:
    """最后一次调用里，模型实际看见的那些 system 文本（拼prompt 的唯一真相在 `call_model`）。"""
    messages = model.calls[-1]["messages"]
    return "\n".join(
        str(m.content)
        for m in messages
        if isinstance(m, SystemMessage) and isinstance(m.content, str)
    )


def _chat(client: TestClient, tid: str, message: str) -> None:
    assert client.post("/api/chat", json={"thread_id": tid, "message": message}).status_code == 200


def test_proactive_words_carry_into_another_thread(env: tuple) -> None:
    """另一条线程：她主动说过的那句要进 system，否则记忆断在两条线程之间。"""
    client, model, _db = env
    tid = str(client.post("/api/session", json={"role_id": "wan"}).json()["thread_id"])
    _chat(client, tid, "我鞋带系好了")
    assert ECHO in _system_seen_by_model(model)


def test_not_copied_into_her_own_proactive_thread(env: tuple) -> None:
    """那条主动会话本身：同一句已经在历史里，抄进 system 就是喂复读。"""
    client, model, _db = env
    tid = str(client.post("/api/session/proactive", json={"role_id": "wan"}).json()["thread_id"])
    assert tid == "s_proactive_wan"
    _chat(client, tid, "系好了")
    assert ECHO not in _system_seen_by_model(model)


def test_no_reachout_means_no_extra_block(env: tuple) -> None:
    """从没主动找过人的角色：不该凭空多出"这些是你最近主动说过的话"那一句指令。

    小模型会把没见过的东西当既成事实接下去 —— 没有语料时整段不出现，比出现一句空指示安全。
    """
    client, model, _db = env
    client.post(
        "/api/roles",
        json={"role_id": "quiet", "role_name": "阿静", "system_prompt": "你是阿静。"},
    )
    tid = str(client.post("/api/session", json={"role_id": "quiet"}).json()["thread_id"])
    _chat(client, tid, "嗨")
    assert "别重复它们说过的内容" not in _system_seen_by_model(model)
