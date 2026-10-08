"""会话正忙时，改历史的 HTTP 口子必须**拒掉并且一个字都不写**（09-28 轮 `R28-02`/`R28-03`）。

症状长什么样（不是假想，是本轮取证读到的形状）：这几条路由里有一处写的是
`with thread_write(tid): graph.update_state(...)` —— `with` 拿不到那个 yield 出来的布尔，
所以**没抢到锁也照样写**；另外几处更干脆，压根没持锁。`update_state` 是
"读最新检查点 → 追加 → 写回"，它读到的若是这一轮开始前的那个父节点，写出来的分支就把这一轮
已经落进去的消息盖掉了：界面上那条消息凭空消失，而检查点里那个分支还在，所以翻不到也说不清。

这里钉三件事，缺一不可：
  1. 忙时 409，而且带一句人话（前端原样显示 detail）；
  2. **409 之后检查点逐字没变** —— 只断状态码会把"拒了但还是写了"当成通过；
  3. 锁放开之后同一句请求照旧成功（不是把门焊死）。

全部离线：`ScriptedChat` 注入，不打真模型、不打用户的 :8000。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from rolecard_agent.core.common.thread_locks import release_thread, try_thread_write
from tests.conftest import ScriptedChat


@pytest.fixture
def client() -> Iterator[TestClient]:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="rc_write_lock_"))
    app = create_app(sqlite_path=tmp / "app.db", model=ScriptedChat([AIMessage(content="回答一")]))
    with TestClient(app) as c:
        yield c


def _seed(client: TestClient) -> tuple[str, list[str]]:
    """开一条会话、走一轮对话，返回 thread_id 与当时落库的消息 id 列表。"""
    made = client.post("/api/session", json={"role_id": "general_assistant"})
    assert made.status_code == 201, made.text
    tid = str(made.json()["thread_id"])
    assert client.post("/api/chat", json={"thread_id": tid, "message": "腰疼"}).status_code == 200
    return tid, _ids(client, tid)


def _ids(client: TestClient, tid: str) -> list[str]:
    body = client.get(f"/api/session/{tid}/messages").json()
    return [str(m["id"]) for m in body["messages"]]


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("messages/edit", "edit"),
        ("messages/delete", "delete"),
    ],
    ids=["edit", "delete"],
)
def test_busy_session_refuses_and_writes_nothing(
    client: TestClient, route: str, payload: str
) -> None:
    tid, before = _seed(client)
    assert len(before) >= 2, f"夹具没造出至少一轮问答：{before}"

    # 两条路由都以 `message_id` 定位；edit 还带一句新的正文，delete 带一组 id。
    body = (
        {"message_id": before[0], "content": "改过的问题"}
        if payload == "edit"
        else {"message_ids": [before[0]]}
    )

    # 模拟"用户那一轮正持有这条会话的写锁"
    assert try_thread_write(tid, timeout=0.0), "夹具没能占住这把锁"
    try:
        res = client.post(f"/api/session/{tid}/{route}", json=body)
    finally:
        release_thread(tid)

    assert res.status_code == 409, f"{route} 忙时该 409，实得 {res.status_code}：{res.text}"
    detail = str(res.json()["detail"])
    assert "这一轮还在跑" in detail, f"detail 该是一句能照做的人话，实得：{detail}"

    # 决定性的一半：历史逐字没动。只断状态码会放过"拒了但还是写了"。
    assert _ids(client, tid) == before, "409 之后检查点被改了 —— 那正是这条缺陷本身"


def test_the_same_request_succeeds_once_the_lock_is_free(client: TestClient) -> None:
    """门不是焊死的：没人持锁时，改历史照旧走得通并且真的落库。"""
    tid, before = _seed(client)

    res = client.post(
        f"/api/session/{tid}/messages/edit",
        json={"message_id": before[0], "content": "改过的问题"},
    )
    assert res.status_code == 200, res.text

    after = _ids(client, tid)
    assert after != before, "编辑成功了但历史没变 —— 这条路由的另一半契约也没了"
