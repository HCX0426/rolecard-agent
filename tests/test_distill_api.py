"""提取精华 / 整理记忆两条 HTTP 入口的用例（架构计划 ⑤-4）。

unit 层（tests/unit/test_memory_distill.py）钉的是解析与纪律，这里钉的是**按钮按下去之后
到底发生了什么**：

  1. 手动提取写进的是**这个会话所属角色的桶**（不是全局），并把游标推进；
  2. 审计只记**条数与结构**，绝不记提取出来的内容 —— 记忆是用户的事实，不该出现在
     给运维看的流水里；
  3. 模型失败：手动那条回 502 且一条没写；自动那条**对话照旧 200**（后台失败只留痕）；
  4. 自动兜底只在开关打开、且攒够轮数时才发起；
  5. 整理完直接回一份新的记忆视图（界面不用再发一次 GET）。

全部离线：`ChatAndDistill` 一个实例同时服务两条路 —— 对话链路收**消息列表**，提取/整理收
**字符串 prompt**，靠参数类型分派。所以测试可以随意改 `model.distill` 而不必猜"这是第几次
调用"（那正是 conftest 默认把自动提取关掉的原因）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from tests.conftest import ScriptedChat


class ChatAndDistill(ScriptedChat):
    """对话按脚本回放，提取/整理按 `distill` 那段固定文本回复（`boom=True` 则调用直接失败）。"""

    def __init__(self, *, distill: str = "NOOP", boom: bool = False) -> None:
        super().__init__([AIMessage(content=f"reply-{i}") for i in range(10)])
        self.distill = distill
        self.boom = boom
        self.prompts: list[str] = []

    def invoke(self, input: Any, **kwargs: Any) -> Any:  # noqa: A002
        if isinstance(input, str):
            self.prompts.append(input)
            if self.boom:
                raise RuntimeError("模型不干了")
            return AIMessage(content=self.distill)
        return super().invoke(input, **kwargs)


@pytest.fixture
def model() -> ChatAndDistill:
    return ChatAndDistill(distill="ADD 用户住在苏州")


@pytest.fixture
def client(tmp_path: Path, model: ChatAndDistill) -> Iterator[TestClient]:
    app = create_app(sqlite_path=tmp_path / "app.db", model=model)
    with TestClient(app) as c:
        yield c


def new_session(client: TestClient) -> tuple[str, str]:
    """→ (thread_id, role_id)：提取的归属角色由会话决定，所以每个用例都要记住它。"""
    body = client.post("/api/session", json={}).json()
    return str(body["thread_id"]), str(body["role_id"])


def chat(client: TestClient, thread_id: str, message: str) -> None:
    res = client.post("/api/chat", json={"thread_id": thread_id, "message": message})
    assert res.status_code == 200


def texts(client: TestClient, role_id: str | None = None) -> list[str]:
    url = "/api/settings/memory" + (f"?role_id={role_id}" if role_id else "")
    return [str(i["text"]) for i in client.get(url).json()["items"]]


# ---------------------------------------------------------------- 手动「提取精华」


def test_distill_writes_into_the_roles_bucket_not_global(client: TestClient) -> None:
    """提取的归属是**这个会话的角色**：对话是跟它说的，回忆以后也只该在它这里出现。"""
    tid, role_id = new_session(client)
    chat(client, tid, "我搬到苏州住了半年")

    res = client.post(f"/api/session/{tid}/distill")
    assert res.status_code == 200
    assert res.json()["report"]["added"] == 1
    assert texts(client, role_id) == ["用户住在苏州"]
    assert texts(client) == []  # 全局桶一条没动


def test_distill_prompt_carries_the_dialogue(client: TestClient, model: ChatAndDistill) -> None:
    """喂给模型的必须是对话原文 + 已有条目：少了前者抽不出事实，少了后者会反复 ADD 同一句。"""
    tid, _ = new_session(client)
    chat(client, tid, "我搬到苏州住了半年")
    client.post(f"/api/session/{tid}/distill")
    assert "用户：我搬到苏州住了半年" in model.prompts[0]
    assert "【已有条目】" in model.prompts[0]


def test_distill_advances_the_cursor(client: TestClient, tmp_path: Path) -> None:
    """点一次就把游标推到当前消息数：否则"再点一次"会把同一批事实再抽一遍。"""
    tid, _ = new_session(client)
    chat(client, tid, "我搬到苏州住了半年")
    assert client.post(f"/api/session/{tid}/distill").status_code == 200

    db = sqlite3.connect(tmp_path / "app.db")
    cursor = db.execute(
        "SELECT distilled_at_seq FROM session_thread WHERE thread_id = ?", (tid,)
    ).fetchone()[0]
    db.close()
    assert cursor == 2  # 一问一答两条


def test_distill_audit_never_contains_memory_text(client: TestClient) -> None:
    """审计里数得出"加了几条"，但搜不到记忆内容 —— 这条是隐私边界，不是风格问题。"""
    tid, _ = new_session(client)
    chat(client, tid, "我搬到苏州住了半年")
    assert client.post(f"/api/session/{tid}/distill").status_code == 200

    rows = client.get("/api/audit?limit=50").json()
    hit = next(r for r in rows if r["action"] == "extract_memory")
    assert json.loads(str(hit["detail_json"]))["added"] == 1
    assert "苏州" not in str(hit)


def test_distill_model_failure_is_502_and_writes_nothing(
    client: TestClient, model: ChatAndDistill
) -> None:
    tid, role_id = new_session(client)
    chat(client, tid, "随便说点什么")
    model.boom = True

    res = client.post(f"/api/session/{tid}/distill")
    assert res.status_code == 502 and "模型调用失败" in res.json()["detail"]
    assert texts(client, role_id) == []


def test_distill_with_memory_disabled_is_400_with_a_way_back(client: TestClient) -> None:
    """记忆总闸关着时按钮要给"去哪开"，而不是静默什么都不做。"""
    tid, _ = new_session(client)
    chat(client, tid, "我搬到苏州住了半年")
    assert client.put("/api/settings/memory", json={"enabled": False}).status_code == 200

    res = client.post(f"/api/session/{tid}/distill")
    assert res.status_code == 400
    assert "记忆与任务目录" in str(res.json()["detail"])


def test_distill_unknown_thread_404(client: TestClient) -> None:
    assert client.post("/api/session/ghost/distill").status_code == 404


# ---------------------------------------------------------------- 自动兜底


@pytest.fixture
def auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开自动提取，并把节奏调到"一轮就提"（每 N 轮 = 2N 条消息）。"""
    monkeypatch.setenv("MEMORY_EXTRACT_AUTO", "1")
    monkeypatch.setenv("MEMORY_EXTRACT_TURNS", "1")


@contextmanager
def auto_client(
    tmp_path: Path, model: Any, monkeypatch: pytest.MonkeyPatch, *, turns: str = "1"
) -> Iterator[TestClient]:
    monkeypatch.setenv("MEMORY_EXTRACT_AUTO", "1")
    monkeypatch.setenv("MEMORY_EXTRACT_TURNS", turns)
    with TestClient(create_app(sqlite_path=tmp_path / "app.db", model=model)) as c:
        yield c


def test_auto_extract_fires_after_the_reply(
    auto: None, tmp_path: Path, model: ChatAndDistill
) -> None:
    """攒够轮数 → 后台线程把提取做完。它在响应流完之后才跑，所以这里只能轮询等它。"""
    del auto
    model.distill = "ADD 用户每周五交周报"
    with TestClient(create_app(sqlite_path=tmp_path / "app.db", model=model)) as client:
        tid, role_id = new_session(client)
        chat(client, tid, "每周五要交周报")
        deadline = time.time() + 5
        while time.time() < deadline and not texts(client, role_id):
            time.sleep(0.05)
        assert texts(client, role_id) == ["用户每周五交周报"]


def test_auto_extract_failure_leaves_the_reply_alone(
    auto: None, tmp_path: Path, model: ChatAndDistill
) -> None:
    """后台提取炸了，用户那一轮仍是正常的回复 —— "绝不打扰对话"是这条路径的合同。"""
    del auto
    model.boom = True
    with TestClient(create_app(sqlite_path=tmp_path / "app.db", model=model)) as client:
        tid, role_id = new_session(client)
        chat(client, tid, "随便聊聊")
        time.sleep(0.3)  # 给后台线程一次失败的机会（失败不冒泡，所以只能等过再断言）
        assert texts(client, role_id) == []


def test_auto_extract_below_the_cadence_does_not_call(
    tmp_path: Path, model: ChatAndDistill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """节奏设成 3 轮时，一轮问答不该触发提取：一次提取 = 一次真调用，成本必须可预算。"""
    model.distill = "ADD 不该出现"
    with auto_client(tmp_path, model, monkeypatch, turns="3") as client:
        tid, role_id = new_session(client)
        chat(client, tid, "第一问")
        time.sleep(0.3)
        assert texts(client, role_id) == []
        assert model.prompts == []


def test_auto_extract_off_means_no_string_call(
    tmp_path: Path, model: ChatAndDistill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关掉（本仓库测试的默认）时，对话链路一次字符串调用都不发。"""
    monkeypatch.setenv("MEMORY_EXTRACT_AUTO", "0")
    with TestClient(create_app(sqlite_path=tmp_path / "app.db", model=model)) as client:
        tid, _ = new_session(client)
        chat(client, tid, "每周五要交周报")
        time.sleep(0.2)
        assert model.prompts == []


# ---------------------------------------------------------------- 「整理记忆」


def add_item(client: TestClient, text: str, *, role_id: str | None = None) -> int:
    """往某个桶里加一条并拿到它的 id（整理的行协议按 id 说话）。"""
    params = {"role_id": role_id} if role_id else None
    body = client.post("/api/settings/memory/item", params=params, json={"text": text}).json()
    live = [i for i in body["items"] if str(i["text"]) == text and i["invalidated_at"] is None]
    return int(str(live[-1]["id"]))


def test_consolidate_merges_and_returns_the_refreshed_view(
    client: TestClient, model: ChatAndDistill
) -> None:
    """整理完直接回一份新的记忆视图：按钮点完界面就有反馈，不用前端再 GET 一次。"""
    a = add_item(client, "用户养了一只猫")
    b = add_item(client, "用户的猫叫米")
    model.distill = f"MERGE {a},{b} 用户养了一只叫米的猫"

    res = client.post("/api/settings/memory/consolidate")
    assert res.status_code == 200
    report = res.json()["report"]
    assert report["merged"] == 1 and (report["before"], report["after"]) == (2, 1)
    assert [str(i["text"]) for i in res.json()["items"]] == ["用户养了一只叫米的猫"]
    assert texts(client) == ["用户养了一只叫米的猫"]


def test_consolidate_never_deletes_rows(client: TestClient, tmp_path: Path) -> None:
    """整理只写失效标记：被合掉的两条**还在表里**（回滚只需要清一个标记）。"""
    add_item(client, "用户养了一只猫")
    add_item(client, "用户的猫叫米")
    client.post("/api/settings/memory/consolidate")  # 默认回 ADD：看不懂的行忽略

    db = sqlite3.connect(tmp_path / "app.db")
    total = db.execute("SELECT COUNT(*) FROM role_memory_item").fetchone()[0]
    db.close()
    assert total == 2


def test_consolidate_scopes_to_one_role_bucket(client: TestClient, model: ChatAndDistill) -> None:
    """`?role_id=` 决定整理哪个桶：全局那条不能被角色卡的按钮顺手改掉。"""
    _, role_id = new_session(client)
    a = add_item(client, "它记得的猫", role_id=role_id)
    b = add_item(client, "猫叫米", role_id=role_id)
    add_item(client, "全局的一条")
    model.distill = f"MERGE {a},{b} 它记得一只叫米的猫"

    res = client.post("/api/settings/memory/consolidate", params={"role_id": role_id})
    assert res.json()["report"]["merged"] == 1
    assert texts(client, role_id) == ["它记得一只叫米的猫"]
    assert texts(client) == ["全局的一条"]  # 全局桶没被牵连


def test_consolidate_needs_two_items_and_names_the_reason(
    client: TestClient, model: ChatAndDistill
) -> None:
    """少于两条不发调用（没有可整理的东西，为什么要花一次真模型调用）。"""
    add_item(client, "只有一条")
    res = client.post("/api/settings/memory/consolidate")
    assert res.status_code == 200
    assert model.prompts == []
    assert "少于两条" in res.json()["report"]["detail"]


def test_consolidate_with_memory_disabled_is_400(client: TestClient) -> None:
    client.put("/api/settings/memory", json={"enabled": False})
    res = client.post("/api/settings/memory/consolidate")
    assert res.status_code == 400


def test_consolidate_unknown_role_404(client: TestClient) -> None:
    res = client.post("/api/settings/memory/consolidate", params={"role_id": "ghost_role"})
    assert res.status_code == 404
