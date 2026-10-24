"""QQ 官方渠道（`features/channels/qq.py`）的单元测试。

全部离线：token 走注入的假 transport、REST 走 httpx.MockTransport、WS 走脚本化的假连接，
端到端那一条用 `create_app(model=ScriptedChat)` 的真内核跑 `_handle_inbound`。钉住：

* 协议纯函数 —— dispatch 事件解析（三种消息事件收、其余拒）、被动回复体（v2 带
  msg_type / v1 不带）与回信路径；
* token —— 取一次缓存复用、过期重取（假时钟），缺 access_token 是 loud 失败；
* WS 网关 —— Hello → Identify（QQey token + intents + shard）→ dispatch 转交 →
  服务器要心跳立即回 → 断线退避重连 → 停旗收场；
* 绑定表 —— 同一对端永远同一条线、会话被删后原地改绑、不同对端不同线；
* 宿主任务 —— 三道闸（总闸 / 凭证 / 依赖）逐格钉住；端到端：入站文本 → 内核轮次 →
  回答发出 + 绑定落表 + 冗余计数维护。
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage

from rolecard_agent.api.main import create_app
from rolecard_agent.base.identity import DEFAULT_USER_ID, ensure_identity_row
from rolecard_agent.base.observability import TraceEvent, Tracer
from rolecard_agent.config import Settings
from rolecard_agent.features.channels import binding as binding_mod
from rolecard_agent.features.channels.binding import bound_thread, ensure_channel_session
from rolecard_agent.features.channels.qq import (
    DEFAULT_INTENTS,
    QQApi,
    QQChannelSource,
    QQCredentials,
    QQGatewayLoop,
    QQInbound,
    QQTokenManager,
    outbound_body,
    parse_event,
    send_path,
)

ME = DEFAULT_USER_ID


class _RecordingTracer(Tracer):
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        self.events.append(event)

    def names(self) -> list[str]:
        return [e.event for e in self.events]


# ---------------------------------------------------------------- 协议纯函数


def test_parse_event_accepts_the_three_message_kinds() -> None:
    group = parse_event(
        {
            "op": 0,
            "t": "GROUP_AT_MESSAGE_CREATE",
            "s": 7,
            "d": {"group_openid": "G1", "id": "m1", "content": " 在吗 "},
        }
    )
    assert group == QQInbound(kind="group", peer="G1", msg_id="m1", text="在吗")
    c2c = parse_event(
        {
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {"user_openid": "U1", "id": "m2", "content": "hi"},
        }
    )
    assert c2c is not None and c2c.kind == "c2c" and c2c.peer == "U1"
    guild = parse_event(
        {
            "op": 0,
            "t": "AT_MESSAGE_CREATE",
            "d": {"channel_id": "C1", "id": "m3", "content": "yo", "author": {"id": "a"}},
        }
    )
    assert guild is not None and guild.kind == "channel" and guild.peer == "C1"


def test_parse_event_rejects_non_message_and_malformed() -> None:
    assert parse_event({"op": 10, "d": {"heartbeat_interval": 30000}}) is None  # Hello
    assert parse_event({"op": 11}) is None  # 心跳 ACK
    assert parse_event({"op": 0, "t": "READY", "d": {"user": {"id": "x"}}}) is None
    # 缺对端 / 缺 msg_id：发不回去 = 这条不接
    assert parse_event({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {"id": "m"}}) is None
    assert parse_event(
        {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {"group_openid": "G"}}
    ) is None
    assert parse_event({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": "not-a-dict"}) is None


def test_outbound_body_v2_has_msg_type_v1_does_not() -> None:
    assert outbound_body("早", kind="group", msg_id="m1", msg_seq=1) == {
        "content": "早",
        "msg_id": "m1",
        "msg_seq": 1,
        "msg_type": 0,
    }
    assert outbound_body("早", kind="channel", msg_id="m2", msg_seq=1) == {
        "content": "早",
        "msg_id": "m2",
        "msg_seq": 1,
    }


def test_send_path_per_kind() -> None:
    assert send_path("group", "G1") == "/v2/groups/G1/messages"
    assert send_path("c2c", "U1") == "/v2/users/U1/messages"
    assert send_path("channel", "C1") == "/channels/C1/messages"


# ---------------------------------------------------------------- token 与 REST


def test_token_manager_caches_until_expiry() -> None:
    calls: list[dict[str, str]] = []
    now = {"t": 1000.0}

    def transport(url: str, payload: dict[str, str]) -> dict[str, Any]:
        calls.append(dict(payload))
        return {"access_token": "tok-1", "expires_in": "7200"}

    tokens = QQTokenManager(
        app_id="app",
        app_secret="sec",
        token_url="https://fake/token",
        transport=transport,
        clock=lambda: now["t"],
    )
    assert tokens.token() == "tok-1"
    assert tokens.token() == "tok-1"  # 缓存命中，transport 不再被打
    assert len(calls) == 1
    assert calls[0] == {"appId": "app", "clientSecret": "sec"}
    # 提前量（300s）过后重取：expires_in 7200 − 300 = 6900s 有效
    now["t"] = 1000.0 + 6900 + 1
    assert tokens.token() == "tok-1"
    assert len(calls) == 2


def test_token_manager_missing_token_is_loud() -> None:
    tokens = QQTokenManager(
        app_id="a",
        app_secret="b",
        token_url="https://fake/token",
        transport=lambda url, payload: {"error": "bad"},
    )
    with pytest.raises(RuntimeError, match="access_token"):
        tokens.token()


def _fake_api(transport: httpx.MockTransport) -> QQApi:
    creds = QQCredentials(
        app_id="a",
        app_secret="b",
        api_base="https://fake-qq",
        token_url="https://fake/token",
    )
    tokens = QQTokenManager(
        app_id="a", app_secret="b", token_url="https://fake/token",
        transport=lambda url, payload: {"access_token": "tok", "expires_in": "7200"},
    )
    return QQApi(creds=creds, tokens=tokens, client=httpx.Client(transport=transport))


def test_qq_api_gateway_and_send() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/gateway":
            return httpx.Response(200, json={"url": "wss://fake-gw"})
        return httpx.Response(200, json={"code": 0})

    api = _fake_api(httpx.MockTransport(handler))
    assert api.gateway_url() == "wss://fake-gw"
    assert requests[0].headers["Authorization"] == "QQey tok"
    msg = QQInbound(kind="group", peer="G1", msg_id="m1", text="在吗")
    api.send(msg, "在的", msg_seq=1)
    sent = requests[1]
    assert sent.url.path == "/v2/groups/G1/messages"
    body = json.loads(sent.content)
    assert body == {"content": "在的", "msg_id": "m1", "msg_seq": 1, "msg_type": 0}


def test_qq_api_send_error_is_readable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 11253, "message": "msg_id 过期"})

    api = _fake_api(httpx.MockTransport(handler))
    msg = QQInbound(kind="c2c", peer="U1", msg_id="old", text="x")
    with pytest.raises(RuntimeError, match="msg_id 过期"):
        api.send(msg, "回", msg_seq=1)


# ---------------------------------------------------------------- WS 网关


class FakeWS:
    """脚本化的假连接：按序吐 recv，收下的 send 全记录；脚本吐完即断线。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if not self.script:
            await asyncio.sleep(3600)  # 永等：由 stop 旗（recv 轮询）收场
            raise ConnectionError("idle forever without stop")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


def test_gateway_loop_identify_dispatch_heartbeat_reconnect() -> None:
    tracer = _RecordingTracer()
    received: list[QQInbound] = []
    connections: list[FakeWS] = []
    counter = {"n": 0}

    async def connect(url: str) -> Any:
        counter["n"] += 1
        if counter["n"] == 1:
            script: list[Any] = [
                json.dumps({"op": 10, "d": {"heartbeat_interval": 5000}}),
                json.dumps({"op": 0, "s": 1, "t": "READY", "d": {}}),
                json.dumps(
                    {
                        "op": 0,
                        "s": 2,
                        "t": "GROUP_AT_MESSAGE_CREATE",
                        "d": {"group_openid": "G1", "id": "m1", "content": "在吗"},
                    }
                ),
                json.dumps({"op": 0, "s": 3, "t": "RESUMED", "d": {}}),  # 非消息：不转交
                json.dumps({"op": 1, "d": None}),  # 服务器要求立即心跳
                ConnectionError("connection dropped"),
            ]
        else:
            # 重连后的第二条：只给 Hello，随后靠停旗收场（recv 轮询醒来看旗）
            script = [json.dumps({"op": 10, "d": {"heartbeat_interval": 5000}})]
        ws = FakeWS(script)
        connections.append(ws)
        return ws

    # 第二条连接建立后置停旗：验证"断线重连"与"停机收场"两件事
    def stopping() -> bool:
        return len(connections) > 1

    api = _fake_api(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"url": "wss://fake-gw"})
        )
    )
    gateway = QQGatewayLoop(api=api, tracer=tracer, connect=connect)
    asyncio.run(
        asyncio.wait_for(
            gateway.run(stopping=stopping, on_message=received.append), timeout=10
        )
    )
    assert len(connections) >= 2  # 断线后真的重连过
    first = connections[0]
    identify = json.loads(first.sent[0])
    assert identify["op"] == 2
    assert identify["d"]["token"] == "QQey tok"
    assert identify["d"]["intents"] == DEFAULT_INTENTS
    assert identify["d"]["shard"] == [0, 1]
    # dispatch 转交了消息事件、没转交 READY / RESUMED
    assert [m.text for m in received] == ["在吗"]
    # 服务器 op=1 → 立即回一跳（d = 最后序号 3）
    assert json.loads(first.sent[1]) == {"op": 1, "d": 3}
    # 断线留了痕
    assert "qq_channel_reconnect" in tracer.names()


def test_gateway_loop_stop_wakes_a_silent_connection() -> None:
    """安静的连接（recv 永等）：置旗后 recv 轮询醒来看旗，整个 loop 自己收场。"""
    tracer = _RecordingTracer()
    flag = {"stop": False}

    async def connect(url: str) -> Any:
        return FakeWS([json.dumps({"op": 10, "d": {"heartbeat_interval": 5000}})])

    creds = QQCredentials("a", "b", "https://fake-qq", "https://fake/token")
    tokens = QQTokenManager(
        app_id="a", app_secret="b", token_url="https://fake/token",
        transport=lambda url, payload: {"access_token": "tok"},
    )
    api = QQApi(creds=creds, tokens=tokens)
    gateway = QQGatewayLoop(api=api, tracer=tracer, connect=connect)

    async def scenario() -> None:
        task = asyncio.create_task(
            gateway.run(stopping=lambda: flag["stop"], on_message=lambda m: None)
        )
        await asyncio.sleep(0.3)  # 进 Identify + recv 轮询（recv 永等）
        assert task.get_loop() is asyncio.get_running_loop()
        flag["stop"] = True
        await asyncio.wait_for(task, timeout=10)  # 不置旗就收不了场 —— 这就是被钉的行为

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


# ---------------------------------------------------------------- 绑定表


def test_binding_same_peer_same_thread(conn, roles) -> None:
    ensure_identity_row(conn)  # session_thread.user_id 的外键要有对象可指
    first = ensure_channel_session(
        conn, channel="qq", peer="G1", user_id=ME, role_id="general_assistant", tool_epoch=1
    )
    second = ensure_channel_session(
        conn, channel="qq", peer="G1", user_id=ME, role_id="general_assistant", tool_epoch=1
    )
    assert first == second
    assert bound_thread(conn, channel="qq", peer="G1") == first
    other = ensure_channel_session(
        conn, channel="qq", peer="G2", user_id=ME, role_id="general_assistant", tool_epoch=1
    )
    assert other != first


def test_binding_rebinds_after_session_deleted(conn, roles) -> None:
    ensure_identity_row(conn)
    first = ensure_channel_session(
        conn, channel="qq", peer="G1", user_id=ME, role_id="general_assistant", tool_epoch=1
    )
    conn.execute("DELETE FROM session_thread WHERE thread_id = ?", (first,))
    conn.commit()
    second = ensure_channel_session(
        conn, channel="qq", peer="G1", user_id=ME, role_id="general_assistant", tool_epoch=1
    )
    assert second != first  # 原地改绑到新会话，不再指向墓碑
    assert bound_thread(conn, channel="qq", peer="G1") == second


def test_binding_race_falls_back_to_winner(monkeypatch, conn, roles) -> None:
    """并发建行的输家：INSERT 撞主键 → 以库里的为准（自己刚建的会话行留成孤儿，无害）。"""
    ensure_identity_row(conn)
    winner = ensure_channel_session(
        conn, channel="qq", peer="G1", user_id=ME, role_id="general_assistant", tool_epoch=1
    )
    # 读取侧失明（模拟"读的时候还没有、写的时候撞上"的那扇窗口）——只瞎**第一次**：
    # ensure_channel_session 入口那读要看到 None 才会走到 INSERT；回退路径里的重读必须
    # 是真读，否则输家永远拿不到赢家那一行。
    real_bound = binding_mod.bound_thread
    reads = {"n": 0}

    def blind_once(conn: Any, *, channel: str, peer: str) -> str | None:
        reads["n"] += 1
        if reads["n"] == 1:
            return None
        return real_bound(conn, channel=channel, peer=peer)

    monkeypatch.setattr(binding_mod, "bound_thread", blind_once)
    loser = ensure_channel_session(
        conn, channel="qq", peer="G1", user_id=ME, role_id="general_assistant", tool_epoch=1
    )
    assert loser == winner
    assert bound_thread(conn, channel="qq", peer="G1") == winner


# ---------------------------------------------------------------- 宿主任务


def test_source_disabled_is_a_traced_noop() -> None:
    rec = _RecordingTracer()
    runtime = SimpleNamespaceRuntime(settings=Settings(qq_channel_enabled=False), tracer=rec)
    source = QQChannelSource(runtime=runtime, tracer=rec)
    source.start()
    assert source._thread is None
    assert "qq_channel_disabled" in rec.names()


def test_source_missing_credentials_is_traced() -> None:
    rec = _RecordingTracer()
    runtime = SimpleNamespaceRuntime(
        settings=Settings(qq_channel_enabled=True, qq_app_id="", qq_app_secret=""),
        tracer=rec,
    )
    source = QQChannelSource(runtime=runtime, tracer=rec)
    source.start()
    assert source._thread is None
    assert "qq_channel_disabled" in rec.names()


class SimpleNamespaceRuntime:
    """渠道任务三道闸只需要 `effective` 与 `assembly.tracer` 两个读穿（测试桩）。"""

    def __init__(self, *, settings: Settings, tracer: Tracer) -> None:
        self.effective = settings

        class _A:
            pass

        self.assembly = _A()
        self.assembly.tracer = tracer


def test_source_starts_and_stops_with_fake_transport() -> None:
    rec = _RecordingTracer()
    runtime = SimpleNamespaceRuntime(
        settings=Settings(
            qq_channel_enabled=True,
            qq_app_id="a",
            qq_app_secret="b",
        ),
        tracer=rec,
    )

    class _Idle:
        async def send(self, data: str) -> None: ...

        async def recv(self) -> str:
            await asyncio.sleep(3600)  # 永等：recv 轮询每 5s 醒一次看旗

        async def close(self) -> None: ...

    async def _idle_connect(url: str) -> Any:
        return _Idle()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": "wss://fake-gw"})

    # REST 半边喂 MockTransport —— 真网络在一次离线测试里都不许出现
    source = QQChannelSource(
        runtime=runtime, tracer=rec, api=_fake_api(httpx.MockTransport(handler)),
        connect=_idle_connect,
    )
    source.start()
    thread = source._thread
    assert thread is not None and thread.is_alive()
    source.stop()
    assert not thread.is_alive()  # 停旗唤醒了 recv 轮询，join 收干净


# ---------------------------------------------------------------- 端到端（真内核）


def test_inbound_message_runs_a_turn_and_replies(tmp_path: Path) -> None:
    app = create_app(model=ScriptedReplies(), sqlite_path=tmp_path / "qq.db")
    runtime = app.state.ctx.runtime
    rec = _RecordingTracer()
    sent: list[tuple[str, str, int]] = []

    class FakeApi:
        def send(self, inbound: QQInbound, text: str, *, msg_seq: int) -> None:
            sent.append((inbound.peer, text, msg_seq))

    source = QQChannelSource(runtime=runtime, tracer=rec, api=FakeApi())
    source._handle_inbound(QQInbound(kind="group", peer="G1", msg_id="m1", text="你好"))
    assert sent == [("G1", "嗨，我在", 1)]
    # 绑定落表 + 会话行存在 + 绑定默认落通用助手
    conn = runtime.assembly.conn
    thread_id = bound_thread(conn, channel="qq", peer="G1")
    assert thread_id is not None
    row = conn.execute(
        "SELECT current_role_id, message_count FROM session_thread WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    assert row is not None and row["current_role_id"] == "general_assistant"
    # 冗余计数：本轮 1 条用户消息 + 1 条回答 = 2
    assert int(row["message_count"]) == 2
    # 第二条消息走同一条线（绑定复用），序号递增
    source._handle_inbound(QQInbound(kind="group", peer="G1", msg_id="m2", text="还在吗"))
    assert sent[-1][2] == 1  # 不同 msg_id → 序号重新从 1 算（被动回复按消息计）
    again = bound_thread(conn, channel="qq", peer="G1")
    assert again == thread_id


class ScriptedReplies:
    """conftest.ScriptedChat 的最小内联版（本文件只吃一条回复，不值得跨文件 import）。"""

    def __init__(self) -> None:
        self._replies: list[AIMessage] = [
            AIMessage(content="嗨，我在"),
            AIMessage(content="还在"),
        ]

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedReplies:
        return self

    def stream(self, input: Any, **kwargs: Any) -> Any:
        yield self._replies.pop(0)

    def invoke(self, input: Any, **kwargs: Any) -> Any:
        return self._replies.pop(0)


def test_turn_error_is_sent_as_readable_reply(tmp_path: Path) -> None:
    """内核给出的人话失败帧照发给 QQ —— 沉默才是"它为什么不回我"。"""
    app = create_app(model=ExhaustedModel(), sqlite_path=tmp_path / "qq2.db")
    runtime = app.state.ctx.runtime
    rec = _RecordingTracer()
    sent: list[str] = []

    class FakeApi:
        def send(self, inbound: QQInbound, text: str, *, msg_seq: int) -> None:
            sent.append(text)

    source = QQChannelSource(runtime=runtime, tracer=rec, api=FakeApi())
    source._handle_inbound(QQInbound(kind="c2c", peer="U1", msg_id="m9", text="在吗"))
    assert len(sent) == 1 and sent[0]  # 有人话、不是空串


class ExhaustedModel:
    """模型侧直接抛错：run_turn 把异常翻成人话 Error 帧 —— 渠道把它发出去。"""

    def bind_tools(self, tools: Any, **kwargs: Any) -> ExhaustedModel:
        return self

    def stream(self, input: Any, **kwargs: Any) -> Any:
        raise RuntimeError("backend exploded")

    def invoke(self, input: Any, **kwargs: Any) -> Any:
        raise RuntimeError("backend exploded")


# ------------------------------------------------- 失败分支补测（diff 覆盖率，2026-10-24）


def test_post_token_default_transport(monkeypatch) -> None:
    """默认 token 传输（自建 httpx.Client）的成功态；expires_in 非数字走兜底。"""
    from rolecard_agent.features.channels import qq as qq_mod

    real_client = httpx.Client

    def patched_client(**kwargs: Any) -> httpx.Client:
        return real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"access_token": "tok", "expires_in": "abc"}
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(qq_mod.httpx, "Client", patched_client)
    token = qq_mod._post_token("https://fake/token", {"appId": "a", "clientSecret": "b"})
    assert token["access_token"] == "tok"


def test_post_token_error_status_raises(monkeypatch) -> None:
    from rolecard_agent.features.channels import qq as qq_mod

    real_client = httpx.Client

    def patched_client(**kwargs: Any) -> httpx.Client:
        return real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "bad secret"})
            ),
            **kwargs,
        )

    monkeypatch.setattr(qq_mod.httpx, "Client", patched_client)
    with pytest.raises(httpx.HTTPStatusError):
        qq_mod._post_token("https://fake/token", {"appId": "a", "clientSecret": "b"})


def test_gateway_url_without_url_is_loud() -> None:
    api = _fake_api(httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1})))
    with pytest.raises(RuntimeError, match="gateway"):
        api.gateway_url()


def test_api_close_releases_owned_client() -> None:
    creds = QQCredentials("a", "b", "https://fake-qq", "https://fake/token")
    tokens = QQTokenManager(
        app_id="a",
        app_secret="b",
        token_url="https://fake/token",
        transport=lambda url, payload: {"access_token": "tok"},
    )
    api = QQApi(creds=creds, tokens=tokens)  # 自建 client（owns=True）
    api.close()  # 不抛 = 释放了自家的连接


def test_default_connect_uses_websockets(monkeypatch) -> None:
    """懒加载那条路：websockets.connect 被调用且结果原样返回。"""
    import websockets as ws_mod

    from rolecard_agent.features.channels import qq as qq_mod

    calls: list[str] = []

    async def fake_connect(url: str) -> str:
        calls.append(url)
        return "connected"

    monkeypatch.setattr(ws_mod, "connect", fake_connect)
    assert asyncio.run(qq_mod._default_connect("wss://fake-gw")) == "connected"
    assert calls == ["wss://fake-gw"]


def test_periodic_heartbeat_fires_on_interval() -> None:
    """心跳任务：到了 interval 就按最后序号发 op=1（20ms 短间隔让它在测试里真跳一次）。"""
    tracer = _RecordingTracer()
    connections: list[FakeWS] = []

    async def connect(url: str) -> Any:
        ws = FakeWS(
            [
                json.dumps({"op": 10, "d": {"heartbeat_interval": 20}}),
                json.dumps({"op": 0, "s": 5, "t": "READY", "d": {}}),
            ]
        )
        connections.append(ws)
        return ws

    flag = {"stop": False}
    api = _fake_api(httpx.MockTransport(lambda r: httpx.Response(200, json={"url": "wss://f"})))
    gateway = QQGatewayLoop(api=api, tracer=tracer, connect=connect)

    async def scenario() -> None:
        task = asyncio.create_task(
            gateway.run(stopping=lambda: flag["stop"], on_message=lambda m: None)
        )
        await asyncio.sleep(0.3)  # 远超 20ms 间隔：心跳必已发过
        flag["stop"] = True
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))
    heartbeats = [json.loads(s) for s in connections[0].sent if json.loads(s)["op"] == 1]
    assert heartbeats and heartbeats[0]["d"] == 5  # d = 最后一条 dispatch 的序号


def test_recv_poll_wakes_on_timeout_before_stop(monkeypatch) -> None:
    """recv 轮询的"空转一拍"路径：timeout → continue，下一圈才看到旗。"""
    from rolecard_agent.features.channels import qq as qq_mod

    monkeypatch.setattr(qq_mod, "RECV_POLL_SECONDS", 0.05)

    class _Idle:
        async def send(self, data: str) -> None: ...

        async def recv(self) -> str:
            if not getattr(_Idle, "greeted", False):
                _Idle.greeted = True
                return json.dumps({"op": 10, "d": {"heartbeat_interval": 5000}})
            await asyncio.sleep(3600)

        async def close(self) -> None: ...

    async def connect(url: str) -> Any:
        return _Idle()

    flag = {"stop": False}
    api = _fake_api(httpx.MockTransport(lambda r: httpx.Response(200, json={"url": "wss://f"})))
    gateway = QQGatewayLoop(api=api, tracer=_RecordingTracer(), connect=connect)

    async def scenario() -> None:
        task = asyncio.create_task(
            gateway.run(stopping=lambda: flag["stop"], on_message=lambda m: None)
        )
        await asyncio.sleep(0.15)  # 至少空转两拍
        flag["stop"] = True
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))


def test_source_disabled_when_dependency_missing(monkeypatch) -> None:
    """依赖闸：websockets 不在场 → 渠道停摆 + 留痕，不起线程。"""
    import importlib.util

    rec = _RecordingTracer()
    runtime = SimpleNamespaceRuntime(
        settings=Settings(qq_channel_enabled=True, qq_app_id="a", qq_app_secret="b"),
        tracer=rec,
    )
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "websockets" else real_find_spec(name),
    )
    source = QQChannelSource(runtime=runtime, tracer=rec)
    source.start()
    assert source._thread is None
    assert "qq_channel_disabled" in rec.names()


def test_source_queues_inbound_and_rejects_when_slots_full(tmp_path: Path) -> None:
    app = create_app(model=ScriptedReplies(), sqlite_path=tmp_path / "qq3.db")
    runtime = app.state.ctx.runtime
    rec = _RecordingTracer()
    sent: list[str] = []

    class FakeApi:
        def send(self, inbound: QQInbound, text: str, *, msg_seq: int) -> None:
            sent.append(text)

    source = QQChannelSource(runtime=runtime, tracer=rec, api=FakeApi())
    source._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qq-test")
    source._on_message(QQInbound(kind="group", peer="G1", msg_id="m1", text="你好"))
    # 等单 worker 把这一轮跑完（槽位被 job 归还即收口）
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not sent:
        time.sleep(0.02)
    assert sent == ["嗨，我在"]
    # 占满全部槽位 → 下一条被拒并留痕
    source._slots.acquire()
    source._slots.acquire()
    source._on_message(QQInbound(kind="group", peer="G1", msg_id="m2", text="第二条"))
    assert "qq_channel_busy" in rec.names()
    source._slots.release()
    source._slots.release()
    source._pool.shutdown(wait=False)


def test_handle_inbound_send_failure_is_traced_not_raised(tmp_path: Path) -> None:
    app = create_app(model=ScriptedReplies(), sqlite_path=tmp_path / "qq4.db")
    runtime = app.state.ctx.runtime
    rec = _RecordingTracer()

    class FailingApi:
        def send(self, inbound: QQInbound, text: str, *, msg_seq: int) -> None:
            raise RuntimeError("HTTP 400: msg_id 过期")

    source = QQChannelSource(runtime=runtime, tracer=rec, api=FailingApi())
    source._handle_inbound(QQInbound(kind="group", peer="G9", msg_id="mx", text="你好"))
    assert "qq_channel_send_failed" in rec.names()


def test_role_missing_is_traced_and_message_skipped(tmp_path: Path) -> None:
    app = create_app(model=ScriptedReplies(), sqlite_path=tmp_path / "qq5.db")
    runtime = app.state.ctx.runtime
    rec = _RecordingTracer()
    source = QQChannelSource(runtime=runtime, tracer=rec, api=_NoApi())
    # 指向一张不存在的角色卡（effective 是 state 槽位的读穿 —— 改 state 即改配置）
    runtime.state["effective"] = runtime.effective.model_copy(
        update={"qq_channel_role_id": "no_such_role"}
    )
    source._handle_inbound(QQInbound(kind="group", peer="G1", msg_id="m1", text="你好"))
    assert "qq_channel_role_missing" in rec.names()


class _NoApi:
    def send(self, inbound: QQInbound, text: str, *, msg_seq: int) -> None:  # pragma: no cover
        raise AssertionError("不该走到发送")


def test_next_seq_overflows_its_ledger() -> None:
    rec = _RecordingTracer()
    runtime = SimpleNamespaceRuntime(settings=Settings(qq_channel_enabled=False), tracer=rec)
    source = QQChannelSource(runtime=runtime, tracer=rec)
    for i in range(600):
        source._next_seq(f"m{i}")  # 600 个不同 msg_id：账本在 512 处清空一次
    assert source._next_seq("m") == 1  # "m" 从没进过清空后的账本 → 从 1 算


def test_binding_race_with_no_winner_reraises(monkeypatch, conn, roles) -> None:
    """竞态回退里连重读都拿到 None（行真的没了）→ 原样抛 IntegrityError，不吞。"""
    import sqlite3 as _sq

    ensure_identity_row(conn)
    conn.execute(
        "INSERT INTO channel_session (channel, peer, thread_id, user_id) "
        "VALUES ('qq', 'G1', 's_deadbeef', 'someone')"
    )
    conn.commit()
    monkeypatch.setattr(binding_mod, "bound_thread", lambda conn, *, channel, peer: None)
    with pytest.raises(_sq.IntegrityError):
        ensure_channel_session(
            conn, channel="qq", peer="G1", user_id=ME, role_id="general_assistant", tool_epoch=1
        )
