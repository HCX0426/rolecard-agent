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


def test_second_distill_sees_only_what_is_new(client: TestClient, model: ChatAndDistill) -> None:
    """第二次按「提取精华」不该把整段对话再喂一遍：喂料由游标决定，不只由"要不要跑"决定。

    实测过的形状（2026-09-24 副本库）：固定八轮对话里自动提取跑了 3 次，每次喂整段 ⇒
    弱模型换个说法重抽同一批事实，**35 条 / 只有 8 个不同事实**；注入窗口只有 8 条，
    重复条目真正挤掉的是**别的事实**。所以这里钉两件事：第二次不发调用、且第一次的原话
    不再出现在第二次的 prompt 里。
    """
    tid, role_id = new_session(client)
    chat(client, tid, "我搬到苏州住了半年")
    assert client.post(f"/api/session/{tid}/distill").status_code == 200
    assert len(model.prompts) == 1

    again = client.post(f"/api/session/{tid}/distill")
    assert again.status_code == 200
    assert again.json()["report"]["added"] == 0
    assert "没有新内容" in again.json()["report"]["detail"]
    assert len(model.prompts) == 1, "没有新消息却还是花了一次真调用"
    assert texts(client, role_id) == ["用户住在苏州"]

    # 又聊了一轮 ⇒ 第二次提取只该看到那一句新的（第一句的原话不该再进 prompt）
    chat(client, tid, "另外我开始学弹琴了")
    model.distill = "ADD 用户在学弹琴"
    assert client.post(f"/api/session/{tid}/distill").status_code == 200
    assert len(model.prompts) == 2
    assert "学弹琴" in model.prompts[1]
    assert "搬到苏州" not in model.prompts[1]
    assert set(texts(client, role_id)) == {"用户住在苏州", "用户在学弹琴"}


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


def test_auto_extract_does_not_stack_up_while_one_is_running(
    tmp_path: Path, model: ChatAndDistill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次提取还在跑时，下一轮**不再另起一次**，而且用户那句话不能被它拖慢。

    游标只在那次调用结束时推进，而一次调用要 10–120 秒。用户在这期间继续聊 ⇒ 每一轮都看到
    "该提取了"，同一个窗口被并发提取多次，每次都把 prompt 里的【已有条目】抄一点回来
    （实测八轮对话跑了 4 次自动提取，桶里 32 条 / 23 对同义，而 `fed` 一直是全量）。

    两条断言各管一边：`prompts == []` 管"别再起一次"，耗时那条管**去重不能用会话写入锁**
    —— 第一版我正是这么写的，于是这一句 chat 等了 158 秒才回来（`run_turn` 等锁上限 150s），
    把 §12.7 量过的"自动提取不拖慢下一轮"直接弄坏。那条误设计就是这条断言要挡的。
    """
    from rolecard_agent.core.common.thread_locks import end_extraction, try_extraction

    with auto_client(tmp_path, model, monkeypatch, turns="1") as client:
        tid, role_id = new_session(client)
        assert try_extraction(tid), "前提：这条会话可以先占住「提取在飞」这枚标记"
        started = time.time()
        try:
            chat(client, tid, "每周五要交周报")
            time.sleep(0.4)  # 给后台那条线程一次"发现自己不是唯一"的机会
        finally:
            end_extraction(tid)
        assert time.time() - started < 20, "提取的在飞标记把对话那一轮挡住了"
        assert model.prompts == [], "上一次提取还在跑，却又起了第二次（会抄清单）"
        assert texts(client, role_id) == []


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

# ---------------------------------------------------------------- 自动那一档在线可改
#
# 一次提取 = 一次真模型调用，所以"要不要自动跑、多久跑一次"必须能在界面上改 ——
# 一个会自己花钱的行为不该只活在一个要重启才生效的 env 里。


def test_extract_cadence_roundtrip(client: TestClient, tmp_path: Path) -> None:
    """设成 6 轮就读得回 6；设成 0 = 关掉自动那条（按钮仍在，只是不再自己跑）。"""
    # 起始是 0 而不是出厂的 12：conftest 那条离线守卫把 AUTO 关了，
    # 而这里的回显读的是**有效值**（关着就是 0，不会假报一个跑起来的节奏）。
    assert client.get("/api/settings/memory").json()["extract_turns"] == 0

    assert client.put("/api/settings/memory", json={"extract_turns": 6}).status_code == 200
    assert client.get("/api/settings/memory").json()["extract_turns"] == 6
    assert client.put("/api/settings/memory", json={"extract_turns": 0}).status_code == 200
    assert client.get("/api/settings/memory").json()["extract_turns"] == 0

    # 关自动时**只**动那个总闸：节奏值留着，下次打开还是原来那一档（不是被重置成 12）。
    # bool 覆盖按 `str(False)` 落库（读侧 `_parse` 认 false/0/off，见 runtime_settings）。
    db = sqlite3.connect(tmp_path / "app.db")
    rows = db.execute(
        "SELECT key, value FROM kernel_meta WHERE key LIKE 'runtime:memory_%'"
    ).fetchall()
    db.close()
    stored = {str(r[0]): str(r[1]) for r in rows}
    assert stored["runtime:memory_extract_auto"] == "False"
    assert stored["runtime:memory_extract_turns"] == "6"


def test_extract_cadence_rejects_absurd_values(client: TestClient) -> None:
    """负数与"每 500 轮"都是确认的坏输入（一次提取是一次真调用），不该被写进配置。"""
    assert client.put("/api/settings/memory", json={"extract_turns": -1}).status_code == 422
    assert client.put("/api/settings/memory", json={"extract_turns": 500}).status_code == 422


def test_extract_cadence_is_global_only(client: TestClient) -> None:
    """角色作用域下改它 = 400：那一档是所有会话共用的行为，不属于某个桶。"""
    _, role_id = new_session(client)
    res = client.put(
        "/api/settings/memory", params={"role_id": role_id}, json={"extract_turns": 6}
    )
    assert res.status_code == 400 and "全局" in res.json()["detail"]


# ------------------------------------------------ 记忆那两步用哪个后端（审计 §12.5）
#
# 钉的是"提取精华 / 整理记忆这两步算在哪个后端的账上"。为什么拿 token 账当读数：后端名
# 一路从 `_thread_model`（或 consolidate）传到 `record_usage`，所以账上的名字就是这一步
# **实际走的那条链**，比对着注释断言可靠。
# 会话固定在 `local` 上，这样"跟随会话"与"指定另一个后端"在账上是两个不同的名字。

BACKEND_ROWS = [
    {"name": "local", "provider": "ollama", "model": "qwen3-vl:8b",
     "base_url": "http://127.0.0.1:9", "usage": "chat"},
    {"name": "sf", "provider": "siliconflow", "base_url": "https://api.siliconflow.cn/v1",
     "model": "deepseek-ai/DeepSeek-V4-Flash", "api_key": "sk-test"},
]


def ledger_calls(db_path: Path) -> dict[str, int]:
    """token 账 → {后端名: 调用次数}。假模型不报 token，但**次数**照样记（没数≠没花）。"""
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT backend, calls FROM token_usage_day").fetchall()
    db.close()
    return {str(r[0]): int(r[1]) for r in rows}


@contextmanager
def memory_backend(
    tmp_path: Path,
    model: ChatAndDistill,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extract_backend: str | None = None,
    tag: str = "default",
) -> Iterator[tuple[TestClient, Path]]:
    """两个后端（local + sf）+ 会话挂在 local；`extract_backend` 就是那个旋钮（走 env 路径）。

    `None` = 完全不设这个环境变量（今天的形状），`""` = 显式设成空 —— 两条都该"不动"。
    让库路径出来（`tag` 区分同一 `tmp_path` 下的多份库），因为这几条用例钉的就是**账**。
    """
    if extract_backend is not None:
        monkeypatch.setenv("MEMORY_EXTRACT_BACKEND", extract_backend)
    db = tmp_path / f"app-{tag}.db"
    with TestClient(create_app(sqlite_path=db, model=model)) as c:
        assert c.put(
            "/api/settings/models", json={"default": "local", "backends": BACKEND_ROWS}
        ).status_code == 200
        yield c, db


def session_on_local(client: TestClient) -> tuple[str, str]:
    """新建一条会话并把它的后端钉在 local 上（不钉就没法区分"跟随会话"与"另有指定"）。"""
    tid, role_id = new_session(client)
    assert client.patch(f"/api/session/{tid}", json={"model_name": "local"}).status_code == 200
    return tid, role_id


def test_extract_backend_sends_the_distill_to_that_backend(
    tmp_path: Path, model: ChatAndDistill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """填了它 ⇒ 提取那一次算在它名下，而不是会话正在用的那个后端。

    这正是这个旋钮存在的全部理由：会话留在本地（陪聊不想出网），而"把对话抽成永久事实"
    那一步可以单独交给选定的后端 —— 2026-09-24 实测同一段对话本地提 9 条、云端 10 条，
    两边都提得出（修完指令与提取自我叠加之后），所以这里钉的是**"这一步到底走了谁"**，
    不是"只有云端才提得出"。
    """
    with memory_backend(tmp_path, model, monkeypatch, extract_backend="sf") as (client, db):
        tid, role_id = session_on_local(client)
        chat(client, tid, "我搬到苏州住了半年")
        assert client.post(f"/api/session/{tid}/distill").status_code == 200

        calls = ledger_calls(db)
        # 只有提取这一笔：对话那一路走流式，而假模型不报 usage ⇒ 流式 flush 只记它看见的
        # （见 test_usage 那几条），所以这里数得到的是"这一步用了谁"，正好是要钉的东西。
        assert calls == {"sf": 1}
        assert texts(client, role_id) == ["用户住在苏州"]


def test_empty_extract_backend_keeps_today_wiring(
    tmp_path: Path, model: ChatAndDistill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没填（env 空 / 根本没这个变量）= 一切照旧：提取跟会话的后端，整理跟默认后端。

    "填了才出网"是隐私口径，所以这一半比上面那半更要钉住 —— 默认值不该替用户决定出不出网。
    """
    for value in (None, "", "   "):  # 没设 / 设成空 / 只有空格，三种都是"没填"
        with memory_backend(
            tmp_path, model, monkeypatch, extract_backend=value, tag=repr(value)
        ) as (client, db):
            tid, _ = session_on_local(client)
            chat(client, tid, "我搬到苏州住了半年")
            assert client.post(f"/api/session/{tid}/distill").status_code == 200
            add_item(client, "用户养了一只猫")
            add_item(client, "用户的猫叫米")
            assert client.post("/api/settings/memory/consolidate").status_code == 200

            calls = ledger_calls(db)
            # 提取跟着会话（local），整理跟着默认后端（未指名 = 账上那行空名字），
            # 而 `sf` 一个字节都没沾上 —— 没填就是不出网。
            assert calls == {"local": 1, "": 1}, value


def test_consolidate_follows_the_same_knob(
    tmp_path: Path, model: ChatAndDistill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「整理记忆」和「提取精华」共用一个旋钮：答案只该有一个 —— 哪个模型碰过我的记忆文本。

    两个入口分开配会留出一个最坏的组合：提取走云端、整理走本地 8B，而整理恰恰是"判断谁
    顶替谁"那一步，弱模型在这里最容易被同义改述骗过（§12.9 量过的正是这件事）。
    """
    model.distill = "NOOP"
    with memory_backend(tmp_path, model, monkeypatch, extract_backend="sf") as (client, db):
        add_item(client, "用户养了一只猫")
        add_item(client, "用户的猫叫米")
        assert client.post("/api/settings/memory/consolidate").status_code == 200

        calls = ledger_calls(db)
        assert calls["sf"] == 1
        assert calls.get("", 0) == 0  # 不再有一笔"整理挂在未指名下"的账
