"""QQ 官方机器人渠道 —— 把这套 agent 接到 QQ 开放平台（bot.qq.com，bot 账号）。

**定位与边界**（2026-10-24，2b）：这是"个人助理在 QQ 上的一扇门"，不是又一个机器人框架 ——
人设、工具、记忆全在本机内核里，这里只做三件事：收消息、跑一轮、把回答发回去。

  * **官方平台，bot 账号**（q.qq.com 注册）：零封号风险；代价是群聊只有 @ 消息、
    主动消息有平台配额 —— 这是产品语义，不是适配器缺陷。
  * **WS 出站连接**：本机无需公网 IP；REST 半边只对 `QQ_API_BASE` / `QQ_TOKEN_URL`
    两个**操作员配置的**地址出网（它自己的出网边界，不进 URL 工具那套允许清单 ——
    那是给模型用的）。
  * **宿主接线**：`api/main.py` 经 `register_background("qq-channel", …)` 注册；总闸
    （`QQ_CHANNEL_ENABLED`）与凭证缺口在 `start()` 里挡，缺什么留痕什么，不静默。
  * **依赖**：`websockets` 懒加载（spec 的 RUNTIME_PACKAGES 整族收；它已在运行依赖里
    —— langgraph-sdk 的传递依赖）。不在场 = 渠道停摆 + 一条 warning，不炸宿主。
  * **轮次不在事件循环上跑**：入站消息交给单 worker 池（全局串行，同一对端天然保序），
    槽满 = 这条不接并留痕 —— QQ 的被动回复窗就几十秒，排到能跑时早发不出去了。

协议事实（对着开放平台 WS 文档写的，改动前先对文档）：

  * `GET /gateway` → `{"url": "wss://…"}`；连上先收 **op=10 Hello**（带 heartbeat_interval）；
  * 回 **op=2 Identify**（`token: "QQey <access_token>"`、intents 位、shard `[0,1]`）；
  * 之后 **op=0 Dispatch**（业务事件：`t` 是事件名、`s` 是序号）与 **op=11** 心跳 ACK；
    客户端每 interval 发 **op=1** 心跳（`d` = 最后一条 dispatch 的 `s`），服务器下发
    op=1 时也要立即回一跳；
  * intents：群/单聊事件位 = `1<<25`（GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE），
    公域频道 @ = `1<<30`（AT_MESSAGE_CREATE）。位对不上的症状是"连上了但收不到消息"；
  * **每次断线全新 Identify，不做 RESUME**：掉线窗口里的消息平台不补投，resume 只会把
    "断线期间发生过什么"变成另一套要维护的状态 —— 重连即 fresh，丢的就是丢的。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from langchain_core.messages import HumanMessage

from rolecard_agent.base.identity import resolve_instance_identity
from rolecard_agent.base.observability import TraceEvent, Tracer, logline
from rolecard_agent.core.agent.graph import build_graph_config
from rolecard_agent.core.agent.state import new_state, now_ts
from rolecard_agent.core.agent.turn import Error as TurnError
from rolecard_agent.core.agent.turn import MessageReplace, run_turn
from rolecard_agent.core.bootstrap import Runtime
from rolecard_agent.core.common.pools import make_pool
from rolecard_agent.core.common.usage import TokenUsage, record_usage
from rolecard_agent.core.sessions import session_service
from rolecard_agent.features.channels.binding import ensure_channel_session
from rolecard_agent.roles.seed import DEFAULT_ROLE_ID
from rolecard_agent.roles.service import RoleNotFound

#: 渠道 id（`channel_session.channel` 列）。第二个渠道出现时它才变成配置。
CHANNEL_ID = "qq"

# 协议位（module docstring 的"协议事实"那节有完整账）。
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
DEFAULT_INTENTS = INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES

#: WS recv 的空转间隔：`stop()` 的响应上限约等于它（等不到消息也要醒过来看旗）。
RECV_POLL_SECONDS = 5.0

#: 重连退避的上限（1s 起步、每次翻倍）。
RECONNECT_BACKOFF_MAX_SECONDS = 60.0

#: 同时在跑的渠道轮次上限。入站消息由单 worker 池串行消费，这个闸防的是"队列悄悄
#: 涨到几百条" —— 满了就不收，比无限排队诚实。
MAX_INFLIGHT_TURNS = 2


# ---------------------------------------------------------------- 事件与载荷（纯函数）


@dataclass(frozen=True, slots=True)
class QQInbound:
    """一条入站消息（已从 dispatch 事件里拆出）。`kind` 决定回信路径。"""

    kind: str  # "group" | "c2c" | "channel"
    peer: str  # group_openid / user_openid / channel_id
    msg_id: str  # 被动回复要引用的消息 id（平台据此放行）
    text: str


def parse_event(payload: dict[str, Any]) -> QQInbound | None:
    """dispatch 事件 → 入站消息。其余（Hello / 心跳 ACK / 生命周期 / 非消息）→ None。

    防御式取字段：形状不对（缺 openid / 缺 msg_id）= 这条不接 —— 发不回去的消息接了
    也白接，与其半路炸不如在门口拒。
    """
    if payload.get("op") != 0:
        return None
    event = str(payload.get("t") or "")
    data = payload.get("d")
    if not isinstance(data, dict):
        return None
    if event == "GROUP_AT_MESSAGE_CREATE":
        kind, peer = "group", str(data.get("group_openid") or "")
    elif event == "C2C_MESSAGE_CREATE":
        kind, peer = "c2c", str(data.get("user_openid") or "")
    elif event == "AT_MESSAGE_CREATE":
        kind, peer = "channel", str(data.get("channel_id") or "")
    else:
        return None
    msg_id = str(data.get("id") or "")
    text = str(data.get("content") or "").strip()
    if not peer or not msg_id:
        return None
    return QQInbound(kind=kind, peer=peer, msg_id=msg_id, text=text)


def send_path(kind: str, peer: str) -> str:
    """回信端点：群/单聊是 v2 形（按 openid），频道是 v1 形（按 channel_id）。"""
    if kind == "group":
        return f"/v2/groups/{peer}/messages"
    if kind == "c2c":
        return f"/v2/users/{peer}/messages"
    return f"/channels/{peer}/messages"


def outbound_body(text: str, *, kind: str, msg_id: str, msg_seq: int) -> dict[str, Any]:
    """被动回复体。群/单聊是 v2 形（要 `msg_type`），频道是 v1 形（不吃这个键）。"""
    body: dict[str, Any] = {"content": text, "msg_id": msg_id, "msg_seq": msg_seq}
    if kind in ("group", "c2c"):
        body["msg_type"] = 0
    return body


# ---------------------------------------------------------------- token 与 REST 半边

TokenTransport = Callable[[str, dict[str, str]], dict[str, Any]]


def _post_token(url: str, payload: dict[str, str]) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        res = client.post(url, json=payload)
        res.raise_for_status()
        return res.json()


class QQTokenManager:
    """AppAccessToken：获取、缓存、到期前刷新（`POST QQ_TOKEN_URL`）。

    平台返回 `expires_in`（秒；字符串形态的数字也见过）—— 一律按"提前 5 分钟过期"处理，
    绝不把"还剩 1 秒"的 token 发出去。缓存上锁：WS 线程与 REST 调用会同时来要。
    """

    #: 到期前多少秒就算旧。
    _EXPIRY_MARGIN_SECONDS = 300.0

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        token_url: str,
        transport: TokenTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_url = token_url
        self._transport = transport or _post_token
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._cached and now < self._expires_at:
                return self._cached
            payload = self._transport(
                self._token_url,
                {"appId": self._app_id, "clientSecret": self._app_secret},
            )
            token = str(payload.get("access_token") or "")
            if not token:
                raise RuntimeError("token 端点没有回 access_token（检查 AppID / AppSecret）")
            try:
                ttl = float(payload.get("expires_in") or 7200)
            except ValueError:
                ttl = 7200.0
            self._cached = token
            self._expires_at = now + max(ttl - self._EXPIRY_MARGIN_SECONDS, 60.0)
            return token


@dataclass(frozen=True, slots=True)
class QQCredentials:
    """渠道凭证与端点（从 Settings 拼一次，下游不再摸 Settings）。"""

    app_id: str
    app_secret: str
    api_base: str
    token_url: str


class QQApi:
    """REST 半边（同步 —— 从工作线程调用）：取网关地址 + 发被动回复。"""

    def __init__(
        self,
        *,
        creds: QQCredentials,
        tokens: QQTokenManager,
        client: httpx.Client | None = None,
    ) -> None:
        self._creds = creds
        self._tokens = tokens
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"QQey {self._tokens.token()}"}

    def token(self) -> str:
        """当前 access_token（缓存住在 token manager；Identify 载荷用它）。"""
        return self._tokens.token()

    def gateway_url(self) -> str:
        res = self._client.get(f"{self._creds.api_base}/gateway", headers=self._headers())
        res.raise_for_status()
        url = str(res.json().get("url") or "")
        if not url:
            raise RuntimeError("/gateway 没有回 url")
        return url

    def send(self, inbound: QQInbound, text: str, *, msg_seq: int) -> None:
        body = outbound_body(
            text, kind=inbound.kind, msg_id=inbound.msg_id, msg_seq=msg_seq
        )
        res = self._client.post(
            f"{self._creds.api_base}{send_path(inbound.kind, inbound.peer)}",
            headers=self._headers(),
            json=body,
        )
        if res.status_code >= 400:
            # 平台错误体带 code/message：原样带出来 —— 被动窗过期、频控这些排查不用再猜。
            raise RuntimeError(f"QQ 发送失败 HTTP {res.status_code}: {res.text[:300]}")


# ---------------------------------------------------------------- WS 半边


class _WSLike(Protocol):
    """网关连接需要的最小面（真实现 = `websockets.WebSocketClientConnection`）。"""

    async def send(self, data: str) -> None: ...

    async def recv(self) -> str: ...


async def _default_connect(url: str) -> Any:
    import websockets  # noqa: PLC0415 - 懒加载：不在场时 start() 已提前留痕，走不到这里

    return await websockets.connect(url)


class QQGatewayLoop:
    """WS 半边：网关地址 → 连接 → Identify → 心跳 → dispatch → 断线重连。

    `_connect` 与 `api` 都可注入：单测用脚本化的假 ws 与假 REST 走完
    Hello → Identify → dispatch → 心跳整条链，不碰真网络。
    """

    def __init__(
        self,
        *,
        api: QQApi,
        tracer: Tracer,
        intents: int = DEFAULT_INTENTS,
        connect: Callable[[str], Any] | None = None,
    ) -> None:
        self._api = api
        self._tracer = tracer
        self._intents = intents
        self._connect = connect or _default_connect

    async def run(
        self, *, stopping: Callable[[], bool], on_message: Callable[[QQInbound], None]
    ) -> None:
        """连接循环：断了退避重连（全新 Identify，见 module docstring），停旗置上就收。"""
        backoff = 1.0
        while not stopping():
            try:
                await self._session(stopping, on_message)
                backoff = 1.0  # 正常退出（停机）不算事故
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 一条连接的死活不该带走渠道
                self._tracer.emit(
                    TraceEvent(
                        event="qq_channel_reconnect",
                        node="channel",
                        detail={"error": f"{type(exc).__name__}: {exc}", "backoff_s": backoff},
                    )
                )
            if stopping():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)

    async def _session(
        self, stopping: Callable[[], bool], on_message: Callable[[QQInbound], None]
    ) -> None:
        url = await asyncio.to_thread(self._api.gateway_url)
        ws = await self._connect(url)
        try:
            hello = json.loads(await ws.recv())
            interval = (
                float((hello.get("d") or {}).get("heartbeat_interval") or 30000) / 1000.0
            )
            await ws.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {
                            "token": f"QQey {self._api.token()}",
                            "intents": self._intents,
                            "shard": [0, 1],
                        },
                    }
                )
            )
            last_seq = 0
            heartbeat = asyncio.create_task(self._heartbeats(ws, interval, lambda: last_seq))
            try:
                async for raw in self._recv(ws, stopping):
                    payload = json.loads(raw)
                    seq = payload.get("s")
                    if seq is not None:
                        last_seq = int(seq)
                    if payload.get("op") == 1:
                        # 服务器要求立即心跳（不等周期那一跳）。
                        await ws.send(json.dumps({"op": 1, "d": last_seq}))
                        continue
                    inbound = parse_event(payload)
                    if inbound is not None and inbound.text:
                        on_message(inbound)
            finally:
                heartbeat.cancel()
        finally:
            await ws.close()

    async def _heartbeats(
        self, ws: _WSLike, interval: float, last_seq: Callable[[], int]
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": 1, "d": last_seq()}))

    async def _recv(self, ws: Any, stopping: Callable[[], bool]) -> Any:
        """逐条收消息，但**每 5s 醒一次看停旗**：`recv()` 会一直阻塞，不轮询的话
        stop() 就要等下一条服务器消息才生效（安静的群里那是几分钟到永远）。"""
        while True:
            if stopping():
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_POLL_SECONDS)
            except TimeoutError:  # asyncio.TimeoutError 的 3.11+ 别名
                continue
            yield raw


# ---------------------------------------------------------------- 宿主接线的后台任务


class QQChannelSource:
    """渠道后台任务（`BackgroundTask` 形状）—— 宿主在 `api/main.py` 注册。

    `start()` 自带三道闸（总闸 / 凭证 / 依赖），每一道都留痕；跑起来之后 WS 线程收事件，
    轮次在单 worker 池里串行跑，回答经 REST 发回。
    """

    def __init__(
        self,
        *,
        runtime: Runtime,
        tracer: Tracer | None = None,
        api: QQApi | None = None,
        connect: Callable[[str], Any] | None = None,
    ) -> None:
        self._runtime = runtime
        self._tracer = tracer or runtime.assembly.tracer
        self._api_override = api
        self._connect_override = connect
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._slots = threading.BoundedSemaphore(MAX_INFLIGHT_TURNS)
        self._api: QQApi | None = api
        self._seq_guard = threading.Lock()
        self._seq: dict[str, int] = {}

    # -- BackgroundTask 形状 ------------------------------------------------

    def start(self) -> None:
        settings = self._runtime.effective
        if not settings.qq_channel_enabled:
            self._emit("qq_channel_disabled", "总闸 QQ_CHANNEL_ENABLED=0，渠道不启")
            return
        if not settings.qq_app_id or not settings.qq_app_secret:
            self._emit(
                "qq_channel_disabled", "凭证缺口：QQ_APP_ID / QQ_APP_SECRET 没配齐"
            )
            return
        if importlib.util.find_spec("websockets") is None:
            logline(
                "warning",
                "channel",
                "配了 QQ 渠道但没装 websockets —— pip install websockets 后重启",
            )
            self._emit("qq_channel_disabled", "websockets 依赖不在场")
            return
        creds = QQCredentials(
            app_id=settings.qq_app_id,
            app_secret=settings.qq_app_secret,
            api_base=settings.qq_api_base,
            token_url=settings.qq_token_url,
        )
        tokens = QQTokenManager(
            app_id=creds.app_id, app_secret=creds.app_secret, token_url=creds.token_url
        )
        self._api = self._api_override or QQApi(creds=creds, tokens=tokens)
        gateway = QQGatewayLoop(
            api=self._api, tracer=self._tracer, connect=self._connect_override
        )
        self._pool = make_pool("qq-turn", 1)
        thread = threading.Thread(
            target=self._run_loop, args=(gateway,), name="qq-channel", daemon=True
        )
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=RECV_POLL_SECONDS * 2 + 5)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        if self._api is not None:
            self._api.close()
        # 复位状态：restart() 要在同一个对象上再 start()——不置停旗的话新循环一进来
        # 就看见旗子直接收场（实测："保存配置后渠道永远连不上"就是漏了这一行）。
        self._stop = threading.Event()
        self._thread = None
        self._pool = None
        self._api = None

    def restart(self) -> None:
        """按**当前有效配置**重启渠道（2026-10-25 渠道可配置化：运行环境页保存后调用）。

        stop（复位状态）→ start（现读 `runtime.effective`）。总闸关着/凭证缺口时
        start 自己会留痕并保持停止 —— 所以这一条对"关掉渠道"同样正确。
        """
        self.stop()
        self.start()

    # -- 渠道状态（工具页的"渠道"区块读这里） ---------------------------------

    def status(self) -> dict[str, Any]:
        """渠道的**运行状态快照**：给管理界面的状态灯与禁用原因。

        三个判定段（总闸 → 凭证 → 依赖）与 `start` 的闸序一致 —— "为什么不连"
        在界面上就能答，不用翻日志。连接态看线程活着没有；收发计数是进程内的，
        重启归零（如实）。
        """
        settings = self._runtime.effective
        running = bool(
            self._thread is not None and self._thread.is_alive()
        )
        if not settings.qq_channel_enabled:
            why = "总闸关闭（QQ_CHANNEL_ENABLED=0）"
        elif not settings.qq_app_id or not settings.qq_app_secret:
            why = "凭证缺口：AppID / AppSecret 没配齐"
        elif importlib.util.find_spec("websockets") is None:
            why = "websockets 依赖不在场"
        else:
            why = None
        with self._seq_guard:
            sent = sum(self._seq.values())
        return {
            "enabled": settings.qq_channel_enabled,
            "running": running,
            "blocked_reason": why,
            "role_id": settings.qq_channel_role_id or DEFAULT_ROLE_ID,
            "inflight_turns": MAX_INFLIGHT_TURNS - self._slots._value,  # noqa: SLF001 - 有界信号量的当前占用，BoundedSemaphore 无公开读法
            "messages_sent": sent,
        }

    def _run_loop(self, gateway: QQGatewayLoop) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                gateway.run(stopping=self._stop.is_set, on_message=self._on_message)
            )
        finally:
            loop.close()

    # -- 消息流水线 ---------------------------------------------------------

    def _on_message(self, msg: QQInbound) -> None:
        """WS 线程里调：入站消息交给单 worker 池排队（全局串行 = 同一对端保序）。"""
        pool = self._pool
        if pool is None or not self._slots.acquire(blocking=False):
            self._emit(
                "qq_channel_busy", f"在跑的轮次已满 {MAX_INFLIGHT_TURNS}，这条不接"
            )
            return

        def job() -> None:
            try:
                self._handle_inbound(msg)
            finally:
                self._slots.release()

        pool.submit(job)

    def _handle_inbound(self, msg: QQInbound) -> None:
        """一条入站消息的完整轮次：绑会话 → 跑图 → 发回。**工作线程**里执行。"""
        runtime = self._runtime
        settings = runtime.effective
        conn = runtime.assembly.conn
        owner = resolve_instance_identity(settings)
        role_id = settings.qq_channel_role_id or DEFAULT_ROLE_ID
        try:
            role = runtime.assembly.roles.scoped(owner).get(role_id)
        except RoleNotFound:
            self._emit(
                "qq_channel_role_missing", f"QQ_CHANNEL_ROLE_ID={role_id!r} 不存在"
            )
            return
        api = self._api
        if api is None:  # pragma: no cover - start() 没跑过就到不了这里（形状护栏）
            return
        thread_id = ensure_channel_session(
            conn,
            channel=CHANNEL_ID,
            peer=msg.peer,
            user_id=owner,
            role_id=role_id,
            tool_epoch=runtime.assembly.plugins.tool_epoch(),
        )
        graph = runtime.state["graph"]
        config = build_graph_config(thread_id, settings, agent_mode=False)
        snapshot = graph.get_state(config)
        human = HumanMessage(
            content=msg.text,
            additional_kwargs={
                "created_at": now_ts(),
                "channel": f"{CHANNEL_ID}:{msg.peer}",
            },
        )
        if snapshot.values:
            graph_input: dict[str, Any] = {
                "messages": [human],
                "current_role_id": role_id,
                "model_name": None,  # 渠道会话没有会话级覆盖 —— 跟角色卡走
                "agent_mode": "chat",  # 渠道侧不做 agent 模式：审批面板在 QQ 里没法点
            }
        else:
            graph_input = {
                **new_state(
                    thread_id=thread_id,
                    user_id=owner,
                    current_role_id=role_id,
                    model_name=None,
                    agent_mode="chat",
                    enabled_domains=runtime.assembly.plugins.enabled_domains(),
                    tool_epoch=runtime.assembly.plugins.tool_epoch(),
                ),
                "messages": [human],
            }

        tracer = runtime.assembly.tracer
        backend = runtime.effective_for(owner).backend_name(role.model_name)

        def _record(usage: TokenUsage | None) -> None:
            record_usage(conn, backend=backend, usage=usage, user_id=owner, tracer=tracer)

        def _after_turn(appended: int) -> None:
            # 冗余计数的增量维护（与会话路由同一条纪律）—— 渠道轮次也是真对话。
            session_service.bump_message_count(conn, thread_id, appended)
            conn.commit()

        reply = ""
        for ev in run_turn(
            graph,
            graph_input=graph_input,
            config=config,
            role_summary={"role_id": role.role_id, "role_name": role.role_name},
            tracer=tracer,
            usage_recorder=_record,
            after_turn=_after_turn,
        ):
            if isinstance(ev, MessageReplace):
                reply = ev.text  # 权威文本（guard 改写 / 兜底句都在这一条里）
            elif isinstance(ev, TurnError):
                self._emit("qq_channel_turn_error", ev.detail)
                reply = ev.detail  # 内核的人话失败帧照发 —— 沉默才是"为什么不回我"
        reply = reply.strip()
        if not reply:
            return
        try:
            api.send(msg, reply, msg_seq=self._next_seq(msg.msg_id))
        except Exception as exc:  # noqa: BLE001 - 发送失败留痕；这句话留在会话历史里不丢
            self._emit("qq_channel_send_failed", f"{type(exc).__name__}: {exc}")

    def _next_seq(self, msg_id: str) -> int:
        """同一 msg_id 的被动回复序号（平台要求从 1 单调；同一条消息最多回几条）。"""
        with self._seq_guard:
            nxt = self._seq.get(msg_id, 0) + 1
            self._seq[msg_id] = nxt
            if len(self._seq) > 512:  # msg_id 是一次性引用，账本有界即可
                self._seq.clear()
            return nxt

    def _emit(self, event: str, detail: str) -> None:
        self._tracer.emit(
            TraceEvent(event=event, node="channel", detail={"detail": detail})
        )
