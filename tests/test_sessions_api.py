"""会话列表 / 历史消息 / 删除会话 端点测试（M5 对话页侧栏的后端）。  Traceability: US-9.

三个决定性断言：
  1. 首轮消息自动生成会话标题，且 updated_at 刷新把最近活跃的会话顶到列表最前；
  2. 历史消息从 checkpoint 回放（不是单独的聊天记录表），多轮顺序正确；
  3. 删除会话时 thread 行与 langgraph 的 checkpoints / writes **一起**清掉 —— 留下孤儿
     checkpoint 意味着"删掉的会话还能被 checkpoint 复活"，这是必须堵上的口子。

全部离线：ScriptedChat 注入。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from rolecard_agent.rag.parser import ParseError
from rolecard_agent.storage.db import thread_id_carriers
from tests.conftest import ScriptedChat


@pytest.fixture
def model() -> ScriptedChat:
    return ScriptedChat([AIMessage(content=f"reply-{i}") for i in range(10)])


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


def chat(client: TestClient, thread_id: str, message: str) -> list[dict[str, object]]:
    res = client.post("/api/chat", json={"thread_id": thread_id, "message": message})
    assert res.status_code == 200
    return parse_sse(res.text)


def authoritative_text(events: list[dict[str, object]]) -> str:
    replace = [e for e in events if e["type"] == "message_replace"]
    return str(replace[-1]["text"]) if replace else ""


def test_first_message_sets_title_and_bumps_session_to_top(
    client: TestClient,
) -> None:
    """US-9：首轮消息生成标题；活跃会话按毫秒级 updated_at 排到列表最前。"""
    older = client.post("/api/session", json={}).json()
    newer = client.post("/api/session", json={}).json()

    sessions = client.get("/api/sessions").json()
    # 同一秒内创建的两个会话，秒级时间戳会打平——这里只断言集合与标题，
    # 严格顺序断言放在聊天之后（毫秒级 updated_at 保证确定性）。
    assert {s["thread_id"] for s in sessions} == {
        newer["thread_id"],
        older["thread_id"],
    }
    assert all(s["title"] is None for s in sessions)

    chat(client, str(older["thread_id"]), "帮我查一下结石直径")

    sessions = client.get("/api/sessions").json()
    assert sessions[0]["thread_id"] == older["thread_id"]  # 活跃者置顶
    assert sessions[0]["title"] == "帮我查一下结石直径"  # 24 字内全量截取
    assert sessions[0]["role_name"] == "通用助手"


def test_long_message_title_is_truncated(client: TestClient) -> None:
    session = client.post("/api/session", json={}).json()
    chat(client, str(session["thread_id"]), "这是一条很长很长的消息，远远超过二十四个字的截取边界")
    sessions = client.get("/api/sessions").json()
    assert len(str(sessions[0]["title"])) == 24


def test_messages_replay_from_checkpoint_in_order(client: TestClient) -> None:
    """历史消息来自 checkpoint：两轮对话后回放顺序为 user/ai × 2。"""
    session = client.post("/api/session", json={}).json()
    tid = str(session["thread_id"])
    assert client.get(f"/api/session/{tid}/messages").json()["messages"] == []  # 新会话无历史

    chat(client, tid, "第一问")
    chat(client, tid, "第二问")

    messages = client.get(f"/api/session/{tid}/messages").json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "第一问"),
        ("assistant", "reply-0"),
        ("user", "第二问"),
        ("assistant", "reply-1"),
    ]


def test_messages_unknown_thread_404(client: TestClient) -> None:
    assert client.get("/api/session/nope/messages").status_code == 404


def test_messages_carries_the_inflight_line_for_other_readers(
    client: TestClient,
) -> None:
    """R26-38：历史回放端点要顺带说出"这一条此刻有没有正在生成的半句"。

    桌宠那一轮在飞的时候，对话界面靠的就是这一个字段 —— 它本来每 5 秒打一次
    `?limit=1` 的探针比 `total`，而那一段里 `total` 是不动的（助手整句还没进检查点），
    所以探针必须能读出"她在打字、打到哪儿了"，否则第二读者要空等整段生成
    （副本实测 7.6 秒）。附带钉两件事：探针那条路径（`limit=1`）也带，以及跑完就没了。
    """
    from rolecard_agent.core.thread_locks import inflight_append, inflight_begin, inflight_end

    session = client.post("/api/session", json={}).json()
    tid = str(session["thread_id"])
    assert client.get(f"/api/session/{tid}/messages").json()["inflight"] is None, (
        "没在飞时必须回 None，不能回一个空 dict —— 那会被界面读成「她在打字」"
    )

    inflight_begin(tid)
    try:
        full = client.get(f"/api/session/{tid}/messages").json()["inflight"]
        assert full == {"text": ""}, "在飞但还没投送出字：这一格要显出「她在说」"
        inflight_append(tid, "已经投送出去的一段")
        probe = client.get(f"/api/session/{tid}/messages?limit=1").json()["inflight"]
        assert probe == {"text": "已经投送出去的一段"}, (
            "探针那一路也要带 —— 对话界面每 5 秒读的就是它"
        )
    finally:
        inflight_end(tid)
    assert client.get(f"/api/session/{tid}/messages").json()["inflight"] is None, (
        "一轮收手之后不许留痕：否则界面上会挂着一个永远不会落地的气泡"
    )


def test_turn_probe_answers_without_touching_the_checkpointer(tmp_path: Path) -> None:
    """`/turn` 的便宜必须是真的：把图换成"一读检查点就炸"的桩，它照答，而 `/messages` 炸。

    这条测的不是功能，是**分工**。对话界面把 `/turn` 当每 0.8 秒一次的探针，看的就是
    "她在不在说"；哪天它顺路也去读检查点，那一拍就不再便宜，而症状只会以"长会话的界面
    开始发粘"这种查不出来的形式出现。所以把"不读检查点"钉成一条断言。
    """
    from rolecard_agent.core.thread_locks import (
        inflight_append,
        inflight_begin,
        inflight_end,
    )

    app = create_app(sqlite_path=tmp_path / "probe.db", model=ScriptedChat([]))
    ctx = app.state.ctx
    with TestClient(app) as client:
        tid = str(client.post("/api/session", json={}).json()["thread_id"])
        assert client.get(f"/api/session/{tid}/turn").json() == {"inflight": None}
        assert client.get("/api/session/nope/turn").status_code == 404

        inflight_begin(tid)
        inflight_append(tid, "说到一半")
        real = ctx.app_state["graph"]

        class _NoPeek:
            def get_state(self, *_a: object, **_kw: object) -> object:
                raise AssertionError("这一拍不许读检查点")

            def __getattr__(self, name: str) -> object:
                return getattr(real, name)

        try:
            assert client.get(f"/api/session/{tid}/turn").json() == {
                "inflight": {"text": "说到一半"}
            }
            ctx.app_state["graph"] = _NoPeek()
            assert client.get(f"/api/session/{tid}/turn").json() == {
                "inflight": {"text": "说到一半"}
            }, "/turn 读到了检查点 —— 它不再是一拍便宜的探针"
            with pytest.raises(AssertionError, match="这一拍不许读检查点"):
                client.get(f"/api/session/{tid}/messages?limit=1")
        finally:
            ctx.app_state["graph"] = real
            inflight_end(tid)
    assert client.get(f"/api/session/{tid}/messages").json()["inflight"] is None


def test_delete_session_removes_thread_and_checkpoints(client: TestClient, tmp_path: Path) -> None:
    """US-9：删除会话不留孤儿 —— thread 行与 checkpoint/writes 同时消失。"""
    session = client.post("/api/session", json={}).json()
    tid = str(session["thread_id"])
    chat(client, tid, "留过历史的会话")

    # 前置自检 + R102-26 的孤儿现场：挂一条 pending 审批到这条会话上 ——
    # 从前的删除写死 ("checkpoints","writes") 两张表，它恰好漏网。
    db = sqlite3.connect(tmp_path / "app.db")
    before_cp = db.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (tid,)
    ).fetchone()[0]
    assert before_cp > 0
    db.execute(
        "INSERT INTO command_approval (command, status, thread_id) "
        "VALUES ('echo delete-orphan', 'pending', ?)",
        (tid,),
    )
    db.commit()
    db.close()

    res = client.delete(f"/api/session/{tid}")
    assert res.status_code == 204

    assert client.get(f"/api/session/{tid}").status_code == 404
    assert client.get(f"/api/session/{tid}/messages").status_code == 404
    assert all(s["thread_id"] != tid for s in client.get("/api/sessions").json())

    db = sqlite3.connect(tmp_path / "app.db")
    db.row_factory = sqlite3.Row  # `thread_id_carriers` 按名取列（app 连接自带这个 row_factory）
    # 断言对齐权威名单 `thread_id_carriers()`（现数现用）而不是再抄一张表清单 ——
    # R102-51 的教训：docstring 承诺"不留孤儿"，断言却只数了两张表，孤儿就在眼皮底下活着。
    left_carriers = {
        table: db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (tid,)
        ).fetchone()[0]
        for table in thread_id_carriers(db)
    }
    db.close()
    assert all(n == 0 for n in left_carriers.values()), left_carriers


def test_delete_unknown_session_404(client: TestClient) -> None:
    assert client.delete("/api/session/nope").status_code == 404


def test_session_list_carries_the_two_flags_the_sidebar_needs(client: TestClient) -> None:
    """`is_blank` 与 `is_proactive`：侧栏分栏与"藏掉空白线程"用的是后端旗标，不是猜的。

    为什么值得钉住（`R26-06` 那一族：写进界面的推断没人复核）：旧前端拿"有没有标题"当
    "这条是不是空的"，而重命名过的空线程、深链刚建出来的线程都会被骗过去；线程 id 那个
    形状（`s_proactive_<uid>_<role>`，B2 起带身份）的事实归 `core/reachout/inbox.py`，前端自己
    拼就是第二个真相源。
    空白的判据是"这条线程写过 checkpoint 没有"—— 那个只有后端看得见。
    """
    blank = client.post("/api/session", json={}).json()
    talked = client.post("/api/session", json={}).json()
    chat(client, str(talked["thread_id"]), "帮我查一下结石直径")
    lane = client.post("/api/session/proactive", json={"role_id": "general_assistant"}).json()

    rows = {str(s["thread_id"]): s for s in client.get("/api/sessions").json()}
    assert rows[str(blank["thread_id"])]["is_blank"] is True
    assert rows[str(talked["thread_id"])]["is_blank"] is False
    assert rows[str(lane["thread_id"])]["is_proactive"] is True
    assert rows[str(talked["thread_id"])]["is_proactive"] is False
    # 报的是布尔而不是"条数"：一轮对话在 checkpoints 里是好几行，把行数当条数报出去就是骗界面
    assert "checkpoint_rows" not in rows[str(talked["thread_id"])]


def test_session_list_survives_role_deletion(client: TestClient) -> None:
    """会话指向的角色被删后，列表仍在且 role_name 降级为 None（不 500）。"""
    client.post(
        "/api/roles",
        json={"role_id": "temp", "role_name": "临时", "system_prompt": "x"},
    )
    session = client.post("/api/session", json={"role_id": "temp"}).json()
    assert client.delete("/api/roles/temp").status_code == 204

    sessions = client.get("/api/sessions").json()
    row = next(s for s in sessions if s["thread_id"] == session["thread_id"])
    assert row["role_id"] == "temp" and row["role_name"] is None


def test_role_name_degrades_on_get_and_patch_alike(client: TestClient) -> None:
    """读会话的两个端点在角色被删后**行为一致**：都回 200、role_name 都降级为 None。

    这是 M1 的两个现场：从前降级逻辑在 `get_session` 与 `patch_session` 各手写一份
    try/except，patch 漏了那格 ⇒ 用户"只改个标题"就撞上 500。收进
    `session_service.role_name_of` 之后两个端点共用一处，这条用例钉的是它们**同进同出** ——
    将来加第三个显示 role_name 的端点，忘了降级会先在这里红。
    """
    client.post(
        "/api/roles",
        json={"role_id": "soon_gone", "role_name": "将被删", "system_prompt": "x"},
    )
    session = client.post("/api/session", json={"role_id": "soon_gone"}).json()
    assert client.delete("/api/roles/soon_gone").status_code == 204
    tid = session["thread_id"]

    detail = client.get(f"/api/session/{tid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["role_name"] is None

    patched = client.patch(f"/api/session/{tid}", json={"title": "改个标题"})
    assert patched.status_code == 200, patched.text  # M1：这一步从前 500
    assert patched.json()["role_name"] is None
    assert patched.json()["title"] == "改个标题"  # 角色没了不影响会话本身可用


# -- upload（US-7 上传入口的真实落点） --------------------------------------------------


def _make_pdf_bytes(text: str) -> bytes:
    """最小合法 1 页 PDF（含文本），供上传链路真实解析测试。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    return bytes(out)


def test_upload_pdf_indexed_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-7 / A1 / v2.2：PDF 上传 → 真实解析入检索索引（status=indexed），且同一文件
    再传复用同一 intake 任务（不产生重复行）。"""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))  # 别污染演示库
    app = create_app(sqlite_path=tmp_path / "pdf.db")
    pdf_bytes = _make_pdf_bytes("Follow-up report: glucose 6.1, recheck advised.")
    with TestClient(app) as c:
        session = c.post("/api/session", json={}).json()
        tid = str(session["thread_id"])

        first = c.post(
            f"/api/session/{tid}/upload",
            files={"file": ("体检报告.pdf", pdf_bytes, "application/pdf")},
        )
        assert first.status_code == 201
        body = first.json()
        assert body["task_id"].startswith("ing_") and body["reused"] is False
        assert body["status"] == "indexed"

        again = c.post(
            f"/api/session/{tid}/upload",
            files={"file": ("体检报告.pdf", pdf_bytes, "application/pdf")},
        )
        assert again.json()["reused"] is True
        assert again.json()["task_id"] == body["task_id"]  # 同一任务，不是新行

        # 注入的说明消息进入 checkpoint 历史，模型后续轮次能看到
        messages = c.get(f"/api/session/{tid}/messages").json()["messages"]
        assert any("已建立检索索引" in str(m["content"]) for m in messages)
        knowledge = c.get("/api/knowledge").json()
        assert any("体检报告.pdf" in s["sources"] for s in knowledge)


def test_reupload_does_not_write_a_second_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**重复上传不重复落盘**（代码审查报告（第二轮）M4）。

    修复前每次上传都无条件写一份 `<uuid8>_<原名>`，于是"再传一次同一个文件"会不断往
    `uploads/` 里堆同样的字节、永不回收。现在先算 hash 查幂等键，命中就直接复用已有
    文件路径 —— 落盘次数 = 唯一文件数。
    """
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    app = create_app(sqlite_path=tmp_path / "dedupe.db")
    pdf_bytes = _make_pdf_bytes("Report: glucose 6.1.")
    with TestClient(app) as c:
        tid = str(c.post("/api/session", json={}).json()["thread_id"])
        for _ in range(3):
            res = c.post(
                f"/api/session/{tid}/upload",
                files={"file": ("报告.pdf", pdf_bytes, "application/pdf")},
            )
            assert res.status_code == 201

        written = [p for p in uploads.iterdir() if not p.name.endswith(".parsed.txt")]
        assert len(written) == 1, f"重复上传写了多份副本：{[p.name for p in written]}"


def test_upload_sanitizes_a_hostile_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件名消毒（代码审查报告（第二轮）A4）：控制字符/分隔符被替换、长度被截断。

    截断必须保住扩展名 —— 解析分派完全依赖后缀，丢掉后缀等于把文件变成"不支持的类型"。
    """
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    app = create_app(sqlite_path=tmp_path / "names.db")
    with TestClient(app) as c:
        tid = str(c.post("/api/session", json={}).json()["thread_id"])
        hostile = "a" * 400 + "\x00bad*.txt"
        res = c.post(
            f"/api/session/{tid}/upload",
            files={"file": (hostile, "纯文本内容".encode(), "text/plain")},
        )
        assert res.status_code == 201
        name = res.json()["file"]
        assert len(name) <= 120
        assert name.endswith(".txt")  # 后缀必须保住
        assert not set(name) & set('\x00<>:"|?*\\/')
        assert len(list(uploads.iterdir())) >= 1


def test_session_context_reports_that_nothing_was_trimmed_initially(
    client: TestClient,
) -> None:
    """新会话：没裁过历史 → `trimmed=0`，界面不该显示任何提示。

    这条守的是"别把正常情况也提示一遍"—— 一个永远亮着的警告等于没有警告。
    """
    tid = str(client.post("/api/session", json={}).json()["thread_id"])
    body = client.get(f"/api/session/{tid}/context").json()
    assert body["trimmed"] == 0
    assert body["budget"] > 0  # 当前配置值一并给出，界面不会误读


def test_trimmed_history_is_reported_to_the_client_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3 的界面部分：裁剪要**告诉客户端**，而且刷新页面后仍能查到。

    背景：`state["messages"]` 只增不减，而 prompt 有字符预算。裁剪本身在上一轮已修，
    但"用户完全不知道模型这次没看到早期对话" —— 表现就是"它怎么忘了我前面说的"。
    现在两条路都给到：当轮 SSE 事件（即时提示）+ checkpoint 里的 state（刷新后仍可查）。
    """
    monkeypatch.setenv("CONTEXT_MAX_CHARS", "120")  # 很小的预算，几轮就把历史挤出去
    app = create_app(
        sqlite_path=tmp_path / "trim.db",
        model=ScriptedChat([AIMessage(content="回复" + "字" * 40) for _ in range(10)]),
    )
    with TestClient(app) as c:
        tid = str(c.post("/api/session", json={}).json()["thread_id"])
        # 第一轮：只有一问一答，预算装得下 → 不该有裁剪事件
        first = chat(c, tid, "第一问")
        assert not [e for e in first if e["type"] == "context_trimmed"]
        assert c.get(f"/api/session/{tid}/context").json()["trimmed"] == 0

        # 多问几轮把历史推过预算
        events: list[dict[str, object]] = []
        for i in range(5):
            events = chat(c, tid, f"第 {i + 2} 问")
            if [e for e in events if e["type"] == "context_trimmed"]:
                break

        trimmed = [e for e in events if e["type"] == "context_trimmed"]
        assert trimmed, "历史超出预算后必须上报 context_trimmed"
        assert trimmed[0]["dropped"] >= 1
        assert trimmed[0]["kept"] >= 1
        # 一次用户轮次只报一次（工具循环里 call_model 会跑多次）
        assert len(trimmed) == 1

        # 刷新后（新请求）仍查得到同一个事实
        after = c.get(f"/api/session/{tid}/context").json()
        assert after["trimmed"] >= 1
        assert after["kept"] >= 1


def test_session_context_unknown_thread_is_404(client: TestClient) -> None:
    assert client.get("/api/session/s_nope/context").status_code == 404


def test_reupload_self_heals_when_the_stored_file_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**回归护栏**：台账在、磁盘文件不在时，重复上传要自愈，不能 500。

    这是"重复上传不重复落盘"（M4）与"文件可能被外部删除"叠加出来的真实缺陷：
    幂等命中后代码直接复用了 `source_file` 记录的路径，而那个文件已经不在了 ——
    解析抛 `FileNotFoundError`，用户看到 500。修法是：此时把这次的字节重新写下并
    把台账指过去（正常重复上传仍然零写入）。
    """
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    app = create_app(sqlite_path=tmp_path / "heal.db")
    body = b"Follow-up report: glucose 6.1."
    with TestClient(app) as c:
        tid = str(c.post("/api/session", json={}).json()["thread_id"])
        first = c.post(
            f"/api/session/{tid}/upload", files={"file": ("a.txt", body, "text/plain")}
        )
        assert first.status_code == 201
        stored = next(p for p in uploads.iterdir() if not p.name.endswith(".parsed.txt"))
        stored.unlink()  # 模拟人工删除 / 数据卷重置

        again = c.post(
            f"/api/session/{tid}/upload", files={"file": ("a.txt", body, "text/plain")}
        )
        assert again.status_code == 201, again.text
        assert again.json()["reused"] is True  # 仍然是同一个任务，没有重复登记
        assert again.json()["task_id"] == first.json()["task_id"]
        # 文件被补回来了，而且台账指向的是新路径
        files = [p for p in uploads.iterdir() if not p.name.endswith(".parsed.txt")]
        assert len(files) == 1 and files[0].exists()


def test_failed_task_recovers_when_the_same_file_is_reuploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**failed 不能是死胡同**：解析失败后重传同一份文件，任务必须能回到 indexed。

    背景：`_INGESTION_TRANSITIONS` 允许 failed → pending（"explicit restart only"），
    但此前**没有任何代码执行这条回边** —— 任务一旦 failed 就永远 failed，哪怕重传后
    解析、入库都成功了，台账还在说"失败"（状态与事实背离，比报错更难查）。

    用 monkeypatch 让"同一份字节"第一次解析失败、第二次成功，模拟的是**环境问题被修好**
    （磁盘满导致的截断文件被补齐、OCR 后端装好了）—— 字节相同而环境不同，是这条路径
    真实的发生方式。
    """
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    app = create_app(sqlite_path=tmp_path / "retry.db")
    body = b"Follow-up: glucose 6.1."

    # 解析编排随 upload_report 收进了 service（Router 不再认识 parse_document）——
    # 打靶点跟着搬家，这里瞄的就是"服务真的在用这条解析路"。
    import rolecard_agent.core.upload_service as upload_mod

    real_parse = upload_mod.parse_document
    calls = {"n": 0}

    def flaky_parse(*args: object, **kwargs: object) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ParseError("第一次解析失败（模拟环境问题）")
        return real_parse(*args, **kwargs)  # 第二次：环境修好了

    monkeypatch.setattr(upload_mod, "parse_document", flaky_parse)

    with TestClient(app) as c:
        tid = str(c.post("/api/session", json={}).json()["thread_id"])
        first = c.post(f"/api/session/{tid}/upload", files={"file": ("a.txt", body, "text/plain")})
        assert first.status_code == 500  # 解析失败以可读 500 呈现（而不是别的异常）
        assert "第一次解析失败" in first.json()["detail"]
        assert c.get(f"/api/session/{tid}/context").status_code == 200  # 站得住的失败

        again = c.post(f"/api/session/{tid}/upload", files={"file": ("a.txt", body, "text/plain")})
        assert again.status_code == 201, again.text
        assert again.json()["status"] == "indexed", (
            "重传成功后任务必须回到 indexed —— failed 不能是终点"
        )
        assert again.json()["reused"] is True  # 同一份字节，仍是同一个任务
        assert calls["n"] == 2


def test_thinking_content_reaches_the_client_as_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """思考模型的推理内容必须以 `thinking` 事件透出（qwen3 / deepseek-r1 类）。

    为什么要有独立事件而不是塞进正文：思考是"过程"，回答是"结论"——界面要分别渲染
    （折叠面板 vs 气泡）。事件与 `MODEL_THINKING_MODELS` 配套：模型在名单里才以
    reasoning=True 调用，不在名单里的模型（如 qwen2.5）零变化。
    """
    thinking_reply = AIMessage(
        content="答：需要先检索再回答。",
        additional_kwargs={"reasoning_content": "内心戏：这个问题要先想清楚边界。"},
    )
    app = create_app(sqlite_path=tmp_path / "think.db", model=ScriptedChat([thinking_reply]))
    with TestClient(app) as c:
        tid = str(c.post("/api/session", json={}).json()["thread_id"])
        events = chat(c, tid, "一个问题")

        thinking = [e for e in events if e["type"] == "thinking"]
        assert len(thinking) == 1, thinking  # 工具循环多次跑 call_model 也不能重复发
        assert "内心戏" in str(thinking[0]["text"])
        # 思考与正文分流：不混进 message_replace 的权威文本里
        assert "内心戏" not in authoritative_text(events)
        assert "答" in authoritative_text(events)


def test_plain_model_produces_no_thinking_events(client: TestClient) -> None:
    """非思考模型（qwen2.5 类）：零 thinking 事件 —— 不能凭空造一个空面板。"""
    events = chat(client, str(client.post("/api/session", json={}).json()["thread_id"]), "你好")
    assert [e for e in events if e["type"] == "thinking"] == []


def test_upload_unsupported_type_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2.2：暂不支持的类型（如 .bin）落地为 pending，并明确告知模型不可读，不假装读过。

    注意 .docx/.pptx/.xlsx 自 v2.2 起已支持解析，故这里特意用一个真正不支持的扩展名。
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(sqlite_path=tmp_path / "unsupported.db", model=ScriptedChat([]))
    with TestClient(app) as c:
        session = c.post("/api/session", json={}).json()
        tid = str(session["thread_id"])
        res = c.post(
            f"/api/session/{tid}/upload",
            files={"file": ("报告.bin", b"\x00\x01\x02binary", "application/octet-stream")},
        )
        assert res.status_code == 201
        assert res.json()["status"] == "pending"
        messages = c.get(f"/api/session/{tid}/messages").json()["messages"]
        assert any("暂不支持自动解析" in str(m["content"]) for m in messages)


def test_upload_rejects_empty_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(sqlite_path=tmp_path / "up2.db", model=ScriptedChat([]))
    with TestClient(app) as c:
        session = c.post("/api/session", json={}).json()
        res = c.post(
            f"/api/session/{session['thread_id']}/upload",
            files={"file": ("a.txt", b"", "text/plain")},
        )
        assert res.status_code == 400


def test_upload_txt_indexed_and_reupload_keeps_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2.1：.txt 上传直接建索引；重复上传复用任务且**不再推进状态机**
    （'indexed' -> 'parsed' 是非法跃迁，曾导致 500）。"""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    app = create_app(sqlite_path=tmp_path / "txt.db")
    with TestClient(app) as c:
        session = c.post("/api/session", json={}).json()
        tid = str(session["thread_id"])
        payload = ("随访须知：每半年复查。".encode(), "text/markdown")
        first = c.post(
            f"/api/session/{tid}/upload", files={"file": ("须知.md", payload[0], payload[1])}
        )
        assert first.status_code == 201 and first.json()["status"] == "indexed"
        again = c.post(
            f"/api/session/{tid}/upload", files={"file": ("须知.md", payload[0], payload[1])}
        )
        assert again.status_code == 201
        assert again.json()["reused"] is True
        assert again.json()["status"] == "indexed"  # 幂等：不重新解析、不非法跃迁

        knowledge = c.get("/api/knowledge").json()
        assert any("须知.md" in s["sources"] for s in knowledge)


def test_upload_unknown_thread_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(sqlite_path=tmp_path / "up3.db", model=ScriptedChat([]))
    with TestClient(app) as c:
        res = c.post(
            "/api/session/nope/upload",
            files={"file": ("a.txt", b"data", "text/plain")},
        )
        assert res.status_code == 404
# -- 编辑重生成 / 删除问答对（v2.4） ----------------------------------------------