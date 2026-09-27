"""接入层的共享上下文与依赖 —— 把 `create_app` 大闭包里的东西变成可注入的。

拆 `main.py` 的真正难点不是"把函数搬走"，而是那 24 个端点共享的闭包状态（连接、服务、
图句柄、热重建函数）。把它们收进一个 `AppContext`，由依赖注入传递，路由模块就不再需要
闭包，**每个 router 都能独立读、独立测**。

`AppContext` 里的服务持有的是 `ThreadLocalConnection`（storage/db.py）：对外表现为一条
连接，内部按线程分发 —— 所以跨 router 共享同一个实例是安全的。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rolecard_agent.api.auth import Actor
from rolecard_agent.config import Settings
from rolecard_agent.core.approvals import ApprovalService
from rolecard_agent.core.bootstrap import Runtime
from rolecard_agent.core.domain_service import DomainQueryService
from rolecard_agent.core.identity import resolve_identity
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.observability import Tracer
from rolecard_agent.core.plugins import PluginError, PluginService, UnknownPlugin
from rolecard_agent.core.services import ServiceEndpointService
from rolecard_agent.core.text import text_of
from rolecard_agent.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from rolecard_agent.rag.ocr import OcrBackend
from rolecard_agent.rag.retriever import KnowledgeBase
from rolecard_agent.roles.service import (
    BuiltinRoleProtected,
    RoleAlreadyExists,
    RoleCardService,
    RoleNotFound,
)
from rolecard_agent.storage.db import ThreadLocalConnection

# 默认"无角色"：纯对话，不接工具与检索。
DEFAULT_ROLE_ID = "general_assistant"


def get_thread(conn: ThreadLocalConnection, thread_id: str, *, user_id: str):
    """按 id 取会话行；**不存在或不是你的**都 404。多个 router 共用（sessions / chat / upload）。

    `user_id` 是必填的关键字参数，不是可选：一个"忘了传就不校验"的归属校验，早晚会在某个
    新端点上被忘掉 —— 而这条判断恰恰是唯一挡在"任何 thread_id 都解析得开"面前的东西
    （`session_thread.thread_id` 是 `s_<12 hex>`，形状可猜，归属此前没人查）。

    为什么 404 而不是 403：403 等于承认"这条会话存在，只是你不该看"。会话 id 一旦泄露，
    别人的线程就从一个不可知的空集变成一份可验证的清单，那是靶子而不是护栏。
    """
    row = conn.execute(
        "SELECT thread_id, user_id, current_role_id, model_name, agent_mode "
        "FROM session_thread WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if row is None or str(row["user_id"]) != user_id:
        raise HTTPException(status_code=404, detail=f"对话不存在：{thread_id}")
    return row


def serialize_message(
    message: object, call_args: dict[str, dict[str, Any]] | None = None
) -> dict[str, object]:
    """Checkpoint message -> JSON shape for the frontend history replay.

    `id` 是前端**编辑 / 删除某条消息**时的寻址依据：LangGraph 的 `add_messages`
    按 id 去重与删除（`RemoveMessage(id=...)`），没有 id 就无法精确改动历史中的一条。
    `call_args`（tool_call_id → 入参）由调用方从消息序列里预先配对 —— 工具行回放时
    才能显示"搜了什么"（单条 ToolMessage 自己看不到入参）。
    """
    created_at = (getattr(message, "additional_kwargs", None) or {}).get("created_at")
    row: dict[str, object]
    if isinstance(message, HumanMessage):
        row = {
            "role": "user",
            "content": text_of(message),
            "id": message.id,
        }
        # 多模态传图（2026-09-18）：content 是 text + image_url 块时，把图透出给前端
        # 回放（用户气泡显示小图 + 点击放大）。只有文本时无此键。
        image = _image_of(message)
        if image:
            row["image"] = image
    elif isinstance(message, ToolMessage):
        trow: dict[str, object] = {
            "role": "tool",
            "name": message.name,
            "content": text_of(message),
            "id": message.id,
        }
        if args := (call_args or {}).get(message.tool_call_id):
            trow["args"] = args  # 历史工具卡显示"搜了什么"（用户 2026-09-17 反馈）
        if created_at:
            trow["ts"] = str(created_at)
        return trow
    elif isinstance(message, AIMessage):
        tools = [tc.get("name") for tc in (message.tool_calls or [])]
        row = {
            "role": "assistant",
            "content": text_of(message),
            "tools": tools,
            "id": message.id,
        }
        # 思考内容随消息一起回放。为什么不只在前台的 live 气泡里显示：一轮结束后前端会以
        # checkpoint 回放**整体替换**消息区（乐观气泡连同思考一起被销毁），用户就再也看不到
        # 推理过程了。把 reasoning 放进回放数据，思考过程才和回答一样是历史的一部分。
        reasoning = (message.additional_kwargs or {}).get("reasoning_content")
        if reasoning:
            row["reasoning"] = reasoning
    else:
        row = {
            "role": "assistant",
            "content": text_of(message),
            "id": getattr(message, "id", None),
        }
    if created_at:
        row["ts"] = str(created_at)  # 旧消息没有该字段 → 不显示时间
    return row


def _image_of(message: object) -> str | None:
    """从多模态 content 块里取图片 data URL（有图才返回，否则 None）。

    与 `text_of` 同哲学：宽容处理形状意外的块 —— 回放循环里一个怪块不该让整页渲染挂掉。
    """
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return None
    for part in content:
        if not isinstance(part, dict):
            continue
        url = None
        iu = part.get("image_url")
        if isinstance(iu, str):
            url = iu
        elif isinstance(iu, dict) and isinstance(iu.get("url"), str):
            url = iu["url"]
        if url:
            return url
    return None


def group_turns(messages: Sequence[object]) -> list[list[int]]:
    """把消息序列切成"一轮问答"：`[用户消息, (工具消息…), 助手回答(可无)]`。

    为什么要有这个函数：删除一条消息时，只删用户消息会留下孤立的助手回答，只删助手回答
    会留下没有答案的提问，而**工具消息与发起它的 AI 消息必须同生共死**（切断配对会被
    供应商判为非法序列）。所以"选中一条 = 选中整轮"由后端统一执行，前端只传 id。

    规则：每遇到一条用户消息就开启新一轮；首条不是用户消息时（历史被删过），
    它自成一轮，保证删除不会漏掉孤儿消息。
    """
    turns: list[list[int]] = []
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage) or not turns:
            turns.append([index])
        else:
            turns[-1].append(index)
    return turns


def expand_to_turns(messages: Sequence[object], ids: Sequence[str]) -> list[str]:
    """把"用户选中的若干 id"扩展为**整轮的 id 集合**（含配对的助手回答与工具消息）。"""
    wanted = set(ids)
    turns = group_turns(messages)
    out: list[str] = []
    for turn in turns:
        indices = {getattr(messages[i], "id", None) for i in turn}
        if indices & wanted:
            out.extend(str(i) for i in indices if i is not None)
    # 保持原有顺序，便于按序删除
    order = {getattr(m, "id", None): n for n, m in enumerate(messages)}
    return sorted(set(out), key=lambda i: order.get(i, 0))


def parsed_text_path(target: Path) -> Path:
    """解析文本的落点：`<上传文件>.parsed.txt`（与上传文件同目录，随 uploads/ 一起被 gitignore）。

    为什么落盘：结构化抽取需要原文，而图片的解析要走 OCR 子进程（很贵）。上传时顺手存一份，
    抽取就不必再跑一次 OCR。
    """
    return target.with_name(target.name + ".parsed.txt")


@dataclass(slots=True)
class AppContext:
    """端点注入用的应用上下文 —— **内核 `Runtime` 的读穿视图**。

    以前它是 13 个字段的容器，其中 `settings` / `knowledge` / `registry` 三件在热重建时被
    逐个显式换新（`ctx.settings = eff` …）。那就是同一份可变量存在两处，漏换任何一个字段
    就是一个"改了不生效"的接缝。装配根下沉到 `core/bootstrap.py` 之后可变量只剩一份，
    这里全部改成属性读穿，端点侧 `ctx.roles` / `ctx.knowledge` 的写法一字不变。

    `settings` 读到的是**有效配置**（模型页 DB 配置 ⊕ 运行环境覆盖），不是裸 env 快照：
    OCR / 抽取 / 比对在请求时读它，「运行环境」页签保存后要立刻生效。
    `health` 只依赖域查询抽象，不持有具体域类（api 层不 import 具体域）。

    `actor` / `_user_id` 是**每次请求一份**的那两个字段（`for_request` 填）：其余全是读穿
    Runtime 的共享视图，只有这两个属于"这次是谁"。它们不能记在共享实例上，理由见 `for_request`。
    """

    runtime: Runtime
    actor: Actor | None = None
    _user_id: str | None = None

    def for_request(self, request: Request) -> AppContext:
        """给这次请求一个新视图：读同一份 Runtime，但带着**自己的** actor。

        为什么不直接往 `app.state.ctx` 上写 actor：那个实例全应用共享，在它上面记"这次是谁"
        等于让两个并发请求互相看见对方的身份 —— 与 `_begin_db_request` 那条注释说的
        "在事件循环线程里 rollback，清的是别的线程的连接"是同一族错法（把请求级的东西
        放在进程级的对象上）。
        """
        actor = getattr(request.state, "actor", None)
        return AppContext(runtime=self.runtime, actor=cast("Actor | None", actor))

    def current_user(self) -> str:
        """这次请求读写数据所用的身份 —— 接入层**唯一**该用它的地方是取数据/落数据。

        懒解析并备忘在本请求的视图上：常驻轮询那几个端点（红点计数、在飞探针）压根不问
        身份，就不该为它们多付一次 `app_user` 查询。
        """
        if self._user_id is None:
            actor = self.actor
            known = None if actor is None or actor.is_anonymous else actor.id
            self._user_id = resolve_identity(
                self.conn, known, fallback=self.runtime.identity
            )
        return self._user_id

    @property
    def settings(self) -> Settings:
        return self.runtime.effective

    @property
    def conn(self) -> ThreadLocalConnection:
        return self.runtime.conn

    @property
    def roles(self) -> RoleCardService:
        return self.runtime.roles

    @property
    def plugins(self) -> PluginService:
        return self.runtime.plugins

    @property
    def ingestion(self) -> IngestionService:
        return self.runtime.ingestion

    @property
    def health(self) -> DomainQueryService:
        return self.runtime.query

    @property
    def model_settings(self) -> ModelSettingsService:
        return self.runtime.model_settings

    @property
    def services(self) -> ServiceEndpointService:
        return self.runtime.services

    @property
    def knowledge(self) -> KnowledgeBase:
        return self.runtime.knowledge

    @property
    def registry(self) -> ToolRegistry:
        return self.runtime.registry

    @property
    def tracer(self) -> Tracer:
        return self.runtime.tracer

    @property
    def approvals(self) -> ApprovalService:
        return self.runtime.approvals

    @property
    def app_state(self) -> dict[str, Any]:
        """图 / 有效配置 / 默认模型的槽位：设置页热重建后这里被整体换掉，端点每请求读它。"""
        return self.runtime.state

    @property
    def rebuild_runtime(self) -> Callable[[], None]:
        """热重建入口（模型设置或服务策略保存时调用）—— 实现在 `Runtime.rebuild`。"""
        return self.runtime.rebuild

    def ocr_candidates(self) -> OcrBackend | None:
        """按「服务」页的 OCR 端点序选一个可用后端（L3：两处重复调用收拢到此）。

        records（兜底现场解析）与 sessions（上传解析）此前各写一遍同样的
        `select_ocr_backend(settings, order=…, endpoints=…)` —— 抽成方法后调用方只剩
        一行，选择策略改动只碰这里。惰性 import 避免 api → rag 的模块级耦合。
        """
        from rolecard_agent.rag.ocr import select_ocr_backend

        return select_ocr_backend(
            self.settings,
            order=self.runtime.candidate_ids("ocr"),
            endpoints=self.services.endpoint_map("ocr"),
        )


def get_context(request: Request) -> AppContext:
    """取应用上下文。端点通过 `Depends(get_context)` 拿到全部服务，不需闭包。

    `app.state` 上的属性在类型系统里是 Any（Starlette 的动态属性），这里显式收敛成
    AppContext —— 比留一个"看起来在防 Any 其实没生效"的 ignore 更诚实。共享的那份再经
    `for_request` 套一层**本请求**的视图（身份属于请求，不属于进程）。
    """
    shared = cast("AppContext", request.app.state.ctx)
    return shared.for_request(request)


def get_actor(request: Request) -> Actor:
    """取认证中间件解析好的身份 —— 端点用它把真实 actor 写进审计日志。"""
    from rolecard_agent.api.auth import Actor as _Actor

    return getattr(request.state, "actor", _Actor())


# ---------------------------------------------------------------- 领域错误 → HTTP


def role_error_to_http(exc: Exception) -> HTTPException:
    """Map a domain error to the right HTTP status. Never leaks a stack trace."""
    if isinstance(exc, RoleNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (RoleAlreadyExists, BuiltinRoleProtected)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def plugin_error_to_http(exc: PluginError) -> HTTPException:
    if isinstance(exc, UnknownPlugin):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def value_error_to_http(exc: ValueError) -> HTTPException:
    """路由层的 `ValueError`（可读的用户输入/业务错误）统一翻译成 400。

    与 `role_error_to_http` / `plugin_error_to_http` 同族；收口各路由里重复了十几遍的
    `raise HTTPException(status_code=400, detail=str(exc)) from exc`。
    """
    return HTTPException(status_code=400, detail=str(exc))
