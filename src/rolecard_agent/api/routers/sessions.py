"""会话与对话路由：session CRUD / SSE 对话 / 历史回放 / 上传。

从 `main.py` 迁出的第 2 组（C1，最大的一组）。上传也归这里，因为它是"往会话里塞
东西"—— 注入的说明消息直接进会话的 checkpoint。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from langchain_core.messages import AnyMessage, HumanMessage, RemoveMessage
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.chat import chat_events
from rolecard_agent.api.deps import (
    DEFAULT_ROLE_ID,
    AppContext,
    expand_to_turns,
    get_actor,
    get_context,
    get_thread,
    parsed_text_path,
    role_error_to_http,
    serialize_message,
)
from rolecard_agent.config import Settings
from rolecard_agent.core.graph import build_graph_config
from rolecard_agent.core.identity import DEFAULT_USER_ID
from rolecard_agent.core.ingestion import INGESTION_FAILED, INGESTION_PENDING
from rolecard_agent.core.observability import TraceEvent
from rolecard_agent.core.state import new_state, now_ts
from rolecard_agent.core.text import text_of
from rolecard_agent.rag.parser import (
    IMAGE_EXTS,
    PARSEABLE_EXTENSIONS,
    OcrUnavailable,
    ParseError,
    parse_document,
)
from rolecard_agent.rag.retriever import (
    EmbedError,
    KnowledgeDimensionMismatch,
)
from rolecard_agent.roles.service import RoleError, RoleNotFound

router = APIRouter()


# 文件名消毒（审查报告 A4）：模型/浏览器给的 `filename` 不可信。`Path().name` 已经挡掉
# 路径成分，这里再处理长度与控制字符 —— 超长名或含 `\x00` 的名字会让 `write_bytes` 抛
# OSError，用户拿到的是一个没有任何说明的 500。
_FILENAME_MAX = 120
_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"|?*\\/]")


def _sanitize_filename(name: str) -> str:
    cleaned = _UNSAFE_NAME_CHARS.sub("_", name).strip(" .")
    if not cleaned:
        cleaned = "report.bin"
    if len(cleaned) <= _FILENAME_MAX:
        return cleaned
    # 截断但保住扩展名：解析分派完全依赖后缀，丢后缀等于把文件变成"不支持的类型"。
    suffix = Path(cleaned).suffix[:16]
    return cleaned[: _FILENAME_MAX - len(suffix)] + suffix


class SessionCreate(BaseModel):
    """Create-session request. Omitting `role_id` binds the default assistant."""

    role_id: str | None = None


class SessionPatch(BaseModel):
    """Session partial update: switch role / rename / set session model override /
    set conversation mode. At least one field required."""

    role_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=100)
    model_name: str | None = None
    # 会话级对话模式：'chat' / 'agent'（显式设置）；null / 空串 = 清除，回落全局默认
    # （settings.agent_default_mode）。
    agent_mode: str | None = None


MODE_CHOICES = ("chat", "agent")


def resolve_agent_mode(raw: object, settings: Settings) -> str:
    """会话级 agent_mode 的有效值：显式设置('chat'/'agent') 优先生效；
    NULL / 未知值 = 回落全局默认（settings.agent_default_mode）。"""
    if str(raw or "") in MODE_CHOICES:
        return str(raw)
    return settings.agent_default_mode


class ChatMessage(BaseModel):
    """One user turn. Length-capped so a pasted novel cannot become a checkpoint bomb."""

    thread_id: str
    message: str = Field(default="", max_length=8000)
    # 多模态传图（用户 2026-09-18）：data URL（data:image/...;base64, ...）。
    # None = 纯文本。上限 15MB（与 OCR 上传一致）：base64 会放大 ~33%，前端读文件前检查。
    # 是否真正接受取决于**当前生效后端**是否支持视觉 —— 由模型能力决定，界面按探测禁用。
    image: str | None = Field(default=None, max_length=20 * 1024 * 1024)

    @field_validator("message")
    @classmethod
    def _text_or_image(cls, v: str, info: ValidationInfo) -> str:
        """至少得有一样：纯文字 or 文字+图 or 纯图。空消息没有任何文本时必须有图，
        否则是无效轮次（防止"点发送无事发生"的静默失败）。"""
        if v.strip():
            return v
        if (info.data.get("image") or "").strip():
            return v
        raise ValueError("message 为空且未附图片")


# 上传大小上限：请求体整个读进内存算哈希，20MB 是演示负载的合理护栏。
UPLOAD_MAX_BYTES = 20 * 1024 * 1024


def _user_message(text: str, image: str | None, *, created_at: str) -> HumanMessage:
    """构造用户消息：纯文本 or 文本 + 图片（多模态 content 块，langchain 会按模型能力解析）。

    langchain_ollama 把 `{"type": "image_url", "image_url": {"url": data_url}}` 转成
    Ollama 的 images 数组（源码 verified）；openai 兼容路径走标准 image_url。
    **现状**：两条路并存 ——
    ① 调用前拦截（P1-2，2026-09-20 落地）：这一轮要送出去的内容含图片、且"该行声明
    `supports_vision=false`"与"Ollama `/api/show` 实测 capabilities 不含 vision"**两条同时
    成立**时，`core/nodes._reject_unsupported_vision` 直接拒，不发那次调用；
    ② 反应式兜底仍然留着：云端行探不了视觉、探测问不到答案（老版本 Ollama / 超时）时
    一律放行，由供应商 400 再翻成同一句可读答复（`core/turn._VISION_MISMATCH_SIGNALS`）。
    前端仍只渲染「视觉」徽标、**发图按钮不 disabled** —— 拦与不拦的判定在后端，界面不
    复制一份（复制就是两处事实面）。
    """
    if not image:
        return HumanMessage(content=text, additional_kwargs={"created_at": created_at})
    return HumanMessage(
        content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image}},
        ],
        additional_kwargs={"created_at": created_at, "has_image": True},
    )


@router.post("/api/session", status_code=201)
def create_session(
    body: SessionCreate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """Create a session thread bound to a role. The thread row is what makes the
    LangGraph `thread_id` answerable to "who is talking" (core/schema.sql A2)."""
    role_id = body.role_id or DEFAULT_ROLE_ID
    try:
        role = ctx.roles.get(role_id)
    except RoleNotFound as exc:
        raise role_error_to_http(exc) from exc
    thread_id = f"s_{uuid.uuid4().hex[:12]}"
    ctx.conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch) "
        "VALUES (?, ?, ?, ?)",
        (thread_id, DEFAULT_USER_ID, role_id, ctx.plugins.tool_epoch()),
    )
    ctx.conn.commit()
    ctx.roles.audit(
        actor=actor.id, action="create_session", target=thread_id, detail={"role_id": role_id}
    )
    return {"thread_id": thread_id, "role_id": role_id, "role_name": role.role_name}


@router.get("/api/session/{thread_id}")
def get_session(thread_id: str, ctx: AppContext = Depends(get_context)) -> object:
    row = get_thread(ctx.conn, thread_id)
    try:
        role = ctx.roles.get(str(row["current_role_id"]))
        role_name: str | None = role.role_name
    except RoleNotFound:
        # 会话指向已被删除的角色：会话本身还在，角色信息降级为空（图侧有同样的兜底）。
        role_name = None
    return {
        "thread_id": row["thread_id"],
        "user_id": row["user_id"],
        "role_id": row["current_role_id"],
        "role_name": role_name,
        "model_name": row["model_name"],
        # 返回**有效**模式（会话覆盖 or 全局默认）：前端切换钮直接按它渲染当前状态。
        "agent_mode": resolve_agent_mode(row["agent_mode"], ctx.settings),
    }


@router.patch("/api/session/{thread_id}")
def patch_session(
    thread_id: str,
    body: SessionPatch,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """会话局部更新：切角色（US-1，不触碰历史）/ 重命名 / 设置会话级模型覆盖 /
    切换对话模式（chat/agent，下一轮生效）。

    model_name 语义（model_fields_set 区分"未提供"与"显式置空"）：
    未提供 = 不改；null = 清除覆盖（回落 角色.model_name → 默认）；名字 = 会话覆盖。
    覆盖名必须在有效后端列表里，否则 400（回退由模型解析器兜底，但配置错误仍要大声）。

    agent_mode 同款语义：未提供 = 不改；'chat'/'agent' = 显式覆盖；null/空串 = 清除
    （回落全局默认 AGENT_DEFAULT_MODE）。非法值 400 —— 未知字符串既不等于"清除"也
    不等于任一档，静默吞掉会让前端显示与实际生效值不一致。
    """
    conn = ctx.conn
    thread = get_thread(conn, thread_id)
    touched = body.model_fields_set & {"role_id", "title", "model_name", "agent_mode"}
    if not touched:
        raise HTTPException(status_code=400, detail="没有任何要更新的字段。")

    if body.role_id:
        try:
            ctx.roles.set_thread_role(thread_id, body.role_id, actor=actor.id)
        except RoleError as exc:
            raise role_error_to_http(exc) from exc  # 角色/线程不存在都是 404

    if body.model_name is not None and body.model_name.strip() == "":
        body.model_name = None  # 空串 = 清除覆盖

    if "model_name" in body.model_fields_set:
        name = body.model_name
        if name is not None:
            effective = ctx.model_settings.effective_settings(ctx.settings)
            if name not in effective.model_backends:
                known = ", ".join(sorted(effective.model_backends))
                raise HTTPException(status_code=400, detail=f"未知后端 {name!r}；可用：{known}")
        conn.execute(
            "UPDATE session_thread SET model_name = ?, "
            "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE thread_id = ?",
            (body.model_name, thread_id),
        )
        conn.commit()
        ctx.roles.audit(
            actor=actor.id,
            action="set_session_model",
            target=thread_id,
            detail={"model_name": body.model_name},
        )

    if "agent_mode" in body.model_fields_set:
        mode = body.agent_mode
        if mode is not None and mode.strip() == "":
            mode = None  # 空串 = 清除覆盖，回落全局默认
        if mode is not None and mode not in MODE_CHOICES:
            raise HTTPException(
                status_code=400, detail=f"未知对话模式 {mode!r}；可用：{' / '.join(MODE_CHOICES)}"
            )
        conn.execute(
            "UPDATE session_thread SET agent_mode = ?, "
            "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE thread_id = ?",
            (mode, thread_id),
        )
        conn.commit()
        ctx.roles.audit(
            actor=actor.id,
            action="set_session_mode",
            target=thread_id,
            detail={"agent_mode": mode},
        )

    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空。")
        conn.execute(
            "UPDATE session_thread SET title = ?, "
            "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE thread_id = ?",
            (title, thread_id),
        )
        conn.commit()

    final_role_id = body.role_id or str(thread["current_role_id"])
    try:
        role = ctx.roles.get(final_role_id)
    except RoleNotFound:
        # 会话指向的角色已被删除（角色 CRUD 的常规后果）：这里**必须**降级而不是抛 ——
        # `get_session` 与 `chat` 都有同样的兜底，本端点此前漏了，于是"只改个标题"也会
        # 500（审查报告 M1，已复现）。降级后角色信息为空，会话本身仍然可用。
        role_name: str | None = None
    else:
        role_name = role.role_name
    row = conn.execute(
        "SELECT title, model_name, agent_mode FROM session_thread WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    return {
        "thread_id": thread_id,
        "role_id": final_role_id,
        "role_name": role_name,
        "title": row["title"] if row else None,
        "model_name": row["model_name"] if row else None,
        "agent_mode": resolve_agent_mode(row["agent_mode"], ctx.settings) if row else "chat",
    }


@router.post("/api/chat")
# 刻意**不是** async def：函数体里跑的全是同步阻塞调用（sqlite / graph.get_state /
# checkpointer 读全量历史）。async 版本会把这些阻塞**放到事件循环上**，一次模型等待
# 就能卡住其它会话的 SSE。同步路由由 Starlette 放进线程池执行，而返回的
# StreamingResponse 内部是 async 生成器 —— 流式并不要求路由本身是 async
#（审查报告 P2：异步路由内的同步阻塞）。
def chat(body: ChatMessage, ctx: AppContext = Depends(get_context)) -> StreamingResponse:
    """SSE 流式对话。线程必须已存在（POST /api/session 创建）。

    首轮注入完整初始状态（`new_state`）；续轮只注入新消息 + 实时角色 —— 后者让
    PATCH /api/session 的切角色在下一轮立即生效，而 enabled_domains / tool_epoch 不进
    输入，让 checkpoint 里的旧值保留，`call_model` 的 epoch 漂移检测才能每个变化只报
    一次（C14）。图从 `app_state` 现取：设置页保存热重建后，下一次对话自动用新图。
    """
    conn = ctx.conn
    graph = ctx.app_state["graph"]
    thread = get_thread(conn, body.thread_id)
    role_id = str(thread["current_role_id"])
    user_id = str(thread["user_id"])
    session_model = thread["model_name"]  # 会话级覆盖（可 None），每轮实时读库
    # 会话级对话模式（有效值 = 会话覆盖 or 全局默认），每轮实时读库 + 实时回落：
    # 会话切「对话/智能体」或操作员改 AGENT_DEFAULT_MODE，下一轮即生效。
    session_mode = resolve_agent_mode(thread["agent_mode"], ctx.app_state["effective"])
    try:
        role = ctx.roles.get(role_id)
    except RoleNotFound as exc:
        raise role_error_to_http(exc) from exc

    # 侧栏标题：首轮消息截断生成；updated_at 每轮刷新，会话列表按它倒序。
    # 用毫秒精度（strftime %f）而非 CURRENT_TIMESTAMP（秒级）：同一秒内创建的两个
    # 会话需要靠"谁最近活跃"严格排序，秒级会打平、只能靠随机 thread_id 兜底。
    # 纯图消息没有文本 → 标题用 "[图片]"，COALESCE 兜底空标题（首次就覆盖）。
    title_fallback = "[图片]" if not body.message.strip() else body.message[:24]
    conn.execute(
        "UPDATE session_thread SET title = COALESCE(title, ?), "
        "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE thread_id = ?",
        (title_fallback, body.thread_id),
    )
    conn.commit()

    # 步数上限随运行配置一起带上：没有它，模型持续返回 tool_calls 时这一轮不会终止。
    # agent 模式上限放大一倍（见 core/graph.build_graph_config）。
    graph_config = build_graph_config(
        body.thread_id,
        ctx.app_state["effective"],
        agent_mode=session_mode == "agent",
    )
    snapshot = graph.get_state(graph_config)
    # created_at 随消息入库（additional_kwargs）：历史回放显示时间（用户 2026-09-17）。
    created_at = now_ts()
    if snapshot.values:
        graph_input: dict[str, object] = {
            "messages": [_user_message(body.message, body.image, created_at=created_at)],
            "current_role_id": role_id,
            "model_name": session_model,  # 每轮实时注入：会话切模型下一轮即生效
            "agent_mode": session_mode,  # 每轮实时注入：会话切模式下一轮即生效
        }
    else:
        graph_input = {
            **new_state(
                thread_id=body.thread_id,
                user_id=user_id,
                current_role_id=role_id,
                model_name=session_model,
                agent_mode=session_mode,
                enabled_domains=ctx.plugins.enabled_domains(),
                tool_epoch=ctx.plugins.tool_epoch(),
            ),
            "messages": [_user_message(body.message, body.image, created_at=created_at)],
        }

    return StreamingResponse(
        chat_events(
            graph,
            graph_input=graph_input,
            config=graph_config,
            role_summary={"role_id": role.role_id, "role_name": role.role_name},
            tracer=ctx.tracer,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -- 编辑重生成 / 删除问答对 ---------------------------------------------------


class _MessageTarget(BaseModel):
    message_id: str = Field(min_length=1)


class EditMessageBody(_MessageTarget):
    content: str = Field(default="", max_length=8000)  # 与新消息同一上限
    # 编辑/重新生成时保留原图（多模态传图，2026-09-18）：编辑只改文本，图随原消息走。
    image: str | None = Field(default=None, max_length=20 * 1024 * 1024)

    @field_validator("content")
    @classmethod
    def _text_or_image(cls, v: str, info: ValidationInfo) -> str:
        if v.strip() or (info.data.get("image") or "").strip():
            return v
        raise ValueError("message 为空且未附图片")


class DeleteMessagesBody(BaseModel):
    message_ids: list[str] = Field(min_length=1)


def _history_messages(ctx: AppContext, thread_id: str) -> tuple[dict, list[AnyMessage]]:
    """取会话的图配置与 checkpoint 消息列表（类型为 AnyMessage：可安全访问 .id）。"""
    thread = get_thread(ctx.conn, thread_id)
    graph = ctx.app_state["graph"]
    # 这份 config 既用于 get_state / update_state，也直接喂给下面的 graph.stream ——
    # 所以步数上限在这里就必须带上（否则编辑重生成那条路仍是无上界的）。
    # agent 模式上限放大一倍（与 /api/chat 同一口径，见 core/graph.build_graph_config）。
    mode = resolve_agent_mode(thread["agent_mode"], ctx.app_state["effective"])
    config = build_graph_config(
        thread_id, ctx.app_state["effective"], agent_mode=mode == "agent"
    )
    snapshot = graph.get_state(config)
    return config, list((snapshot.values or {}).get("messages") or [])


@router.post("/api/session/{thread_id}/messages/edit")
# 同上：函数体里的 sqlite / graph.update_state 都是阻塞调用，保持同步路由。
def edit_message_and_regenerate(
    thread_id: str, body: EditMessageBody, ctx: AppContext = Depends(get_context)
):
    """编辑**自己发过的某条消息**并从那里重新生成回答。

    语义（与主流 AI 客户端一致）：改完回车 = **该条之后的历史全部作废**，用它作为新的
    提问重新跑一轮。所以这里先把该条及其之后的消息从 checkpoint 移除，再以编辑后的
    文本作为新输入流式生成 —— 返回的是与 `/api/chat` 完全相同的 SSE 事件流
    （包含 thinking / token / tool_call / message_replace / end），前端无需分叉处理。

    只允许编辑 **user 消息**：编辑助手回答等于伪造模型输出，会让审计与"数据可追溯"失效。
    """
    config, messages = _history_messages(ctx, thread_id)
    graph = ctx.app_state["graph"]
    thread = get_thread(ctx.conn, thread_id)
    role_id = str(thread["current_role_id"])
    session_model = thread["model_name"]
    session_mode = resolve_agent_mode(thread["agent_mode"], ctx.app_state["effective"])
    try:
        role = ctx.roles.get(role_id)
    except RoleNotFound as exc:
        raise role_error_to_http(exc) from exc

    target = next((m for m in messages if getattr(m, "id", None) == body.message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="消息不存在（可能已被删除或线程不匹配）。")
    if not isinstance(target, HumanMessage):
        raise HTTPException(status_code=400, detail="只能编辑自己发送的消息。")

    index = messages.index(target)
    # 目标及其之后的全部作废（RemoveMessage 按 id 精确删除，不触碰前面的历史）
    # 无 id 的消息无法被 RemoveMessage 定位（正常不会出现，防御性跳过）。
    doomed = [RemoveMessage(id=m.id) for m in messages[index:] if m.id is not None]
    graph.update_state(config, {"messages": doomed})

    graph_input: dict[str, object] = {
        "messages": [_user_message(body.content, body.image, created_at=now_ts())],
        "current_role_id": role_id,
        "model_name": session_model,
        "agent_mode": session_mode,
    }
    ctx.conn.execute(
        "UPDATE session_thread SET updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
        "WHERE thread_id = ?",
        (thread_id,),
    )
    ctx.conn.commit()

    return StreamingResponse(
        chat_events(
            graph,
            graph_input=graph_input,
            config=config,
            role_summary={"role_id": role.role_id, "role_name": role.role_name},
            tracer=ctx.tracer,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/session/{thread_id}/messages/delete")
def delete_messages(
    thread_id: str, body: DeleteMessagesBody, ctx: AppContext = Depends(get_context)
) -> dict[str, int]:
    """删除选中的**一或多个问答对**，其余历史不受影响。

    "选中我的或他的，就带上配对的那一问一答"由后端按 `expand_to_turns` 统一扩展：
    用户消息 ↔ 助手回答 ↔ 期间的工具消息属于同一轮，必须整轮增删。
    """
    config, messages = _history_messages(ctx, thread_id)
    graph = ctx.app_state["graph"]
    known = {getattr(m, "id", None) for m in messages}
    unknown = [i for i in body.message_ids if i not in known]
    if unknown:
        raise HTTPException(status_code=404, detail=f"消息不存在：{', '.join(unknown[:3])}")

    doomed_ids = expand_to_turns(messages, list(body.message_ids))
    graph.update_state(config, {"messages": [RemoveMessage(id=i) for i in doomed_ids]})
    ctx.conn.execute(
        "UPDATE session_thread SET updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
        "WHERE thread_id = ?",
        (thread_id,),
    )
    ctx.conn.commit()
    return {"deleted": len(doomed_ids), "remaining": len(messages) - len(doomed_ids)}


@router.get("/api/sessions")
def list_sessions(ctx: AppContext = Depends(get_context)) -> list[object]:
    """会话列表（对话页侧栏）。v1 单用户演示：只列演示身份名下的会话。"""
    rows = ctx.conn.execute(
        "SELECT s.thread_id, s.title, s.current_role_id AS role_id, r.role_name, "
        "s.agent_mode, s.updated_at FROM session_thread s "
        "LEFT JOIN role_card r ON r.role_id = s.current_role_id "
        "WHERE s.user_id = ? ORDER BY s.updated_at DESC, s.thread_id",
        (DEFAULT_USER_ID,),
    ).fetchall()
    return [
        {**dict(r), "agent_mode": resolve_agent_mode(r["agent_mode"], ctx.settings)} for r in rows
    ]


@router.get("/api/session/{thread_id}/messages")
def get_session_messages(
    thread_id: str,
    ctx: AppContext = Depends(get_context),
    limit: int = Query(default=500, ge=0),
) -> dict[str, object]:
    """历史消息回放（来源：checkpoint，而非单独的聊天记录表）——
    点击历史会话续聊时，前端用它恢复消息区。

    `limit`：默认只回**最近 500 条**（0 = 全部）。长对话一次全量返回既慢又没用
    （界面本来也只从底部看起）；`total`/`truncated` 让前端能如实说明"只显示了最近 N 条"
    （审查报告 P2：无分页）。
    """
    get_thread(ctx.conn, thread_id)
    snapshot = ctx.app_state["graph"].get_state({"configurable": {"thread_id": thread_id}})
    raw = (snapshot.values or {}).get("messages", [])
    # tool_call_id → 入参：历史工具行要能显示"搜了什么"（单条 ToolMessage 看不到入参）。
    call_args: dict[str, dict] = {}
    for m in raw:
        for tc in getattr(m, "tool_calls", None) or []:
            if tc.get("id"):
                call_args[str(tc["id"])] = dict(tc.get("args") or {})
    rows = [serialize_message(m, call_args) for m in raw]
    total = len(rows)
    if limit and total > limit:
        rows = rows[-limit:]
    return {
        "messages": rows,
        "total": total,
        "limit": limit,
        "truncated": len(rows) < total,
    }


class PromptEnhanceBody(BaseModel):
    # 与 ChatMessage.message 同一个上限：增强提示同样会进模型调用与轨迹，不给上限
    # 就等于允许一次请求把超大文本塞进 checkpoint（审查报告 P2）。
    text: str = Field(min_length=1, max_length=8000)


@router.post("/api/prompt/enhance")
def enhance_prompt(
    body: PromptEnhanceBody, ctx: AppContext = Depends(get_context)
) -> object:
    """增强提示词：把草稿改写得更清晰具体（对齐 WorkBuddy，用户 2026-09-17）。

    用默认对话模型做一次纯改写调用——不建会话、不入历史。失败给可读 502，
    空文本 400。这是"工具性请求"，所以不写审计（审计留痕的是管理面变更）。
    """
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="没有可增强的内容。")
    model = ctx.app_state["default_model"]
    prompt = (
        "你是提示词工程师。把下面的用户草稿改写为更清晰、具体、信息完整的提示词："
        "补全模糊指代、明确期望的输出与格式；若草稿已足够清晰则做最小润色。"
        "只输出改写后的文本本身，不要解释、不要加引号。保持原语言。\n\n草稿：\n" + text
    )
    try:
        out = model.invoke(prompt)
    except Exception as exc:  # noqa: BLE001 - 模型侧失败给可读原因，不抛栈
        raise HTTPException(
            status_code=502, detail=f"增强失败（模型调用错误）：{type(exc).__name__}"
        ) from exc
    # 分块回复（多模态模型的常态形态）必须走唯一的取值实现：`str(content)` 会把 Python
    # repr 原样贴回用户的输入框（架构审计报告 P1-8）。
    enhanced = text_of(out).strip()
    if not enhanced:
        raise HTTPException(status_code=502, detail="增强失败：模型没有返回内容。")
    return {"text": enhanced}


@router.get("/api/session/{thread_id}/context")
def get_session_context(thread_id: str, ctx: AppContext = Depends(get_context)) -> object:
    """这一会话最近一轮的**上下文预算事实**：模型实际看到了多少条历史、被裁掉多少条。

    为什么需要它（而不是只靠 SSE 的 `context_trimmed` 事件）：事件只在当轮到达浏览器，
    刷新页面就没了；而"早期对话已经被裁掉"是一个**持续为真**的状态 —— 用户重新打开会话
    时同样应该看得到。数值来自 checkpoint 里的 state，所以进程重启也还在。

    `budget` 回的是当前配置值：它可能和当时那一轮不同（操作员改过 `CONTEXT_MAX_CHARS`），
    所以两个数字一起给出，界面不会误导。
    """
    get_thread(ctx.conn, thread_id)
    snapshot = ctx.app_state["graph"].get_state({"configurable": {"thread_id": thread_id}})
    values = snapshot.values or {}
    return {
        "trimmed": int(values.get("context_trimmed") or 0),
        "kept": int(values.get("context_kept") or 0),
        "budget": ctx.settings.context_max_chars,
    }


@router.delete("/api/session/{thread_id}", status_code=204)
def delete_session(thread_id: str, ctx: AppContext = Depends(get_context)) -> None:
    """删除会话：thread 行 + 该线程的 checkpoint / writes 一并清掉，不留孤儿。"""
    get_thread(ctx.conn, thread_id)
    ctx.conn.execute("DELETE FROM session_thread WHERE thread_id = ?", (thread_id,))
    for table in ("checkpoints", "writes"):  # langgraph SqliteSaver 的两张表
        ctx.conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
    ctx.conn.commit()


@router.post("/api/session/{thread_id}/upload", status_code=201)
def upload_report(
    thread_id: str, file: UploadFile, ctx: AppContext = Depends(get_context)
) -> object:
    """US-7 上传入口的真实落点：存文件 + 登记 intake 任务（幂等键 sha256）。

    **刻意声明为同步 `def`**：本端的重活（OCR 子进程最长 120 秒、嵌入、落盘）全是
    **阻塞式**调用。若写成 `async def`，它们会跑在事件循环里 —— 上传一张图片的几十秒
    内，整个进程（含其他会话的 SSE 对话）都不再响应。同步 `def` 让 FastAPI 把它丢进
    线程池，事件循环只负责调度。同理用 `file.file.read()` 而不是 `await file.read()`。

    v2.2 起解析在此完成：.txt/.md/.pdf/.docx/.pptx/.xlsx 直接抽文本入
    `health_reports` 检索索引；图片走 **可插拔 OCR**（本地 Paddle 优先，独立 venv 子进程；
    排在其后的候选由「服务」页的 OCR 端点序决定，见 rag/ocr.select_ocr_backend）。
    解析失败的图片 / 不支持的类型保持 pending，并向会话注入一条说明消息（graph.update_state），
    让模型知道"有文件已登记但还不能读"，而不是假装读过。重复上传同一文件复用同一任务。

    **落盘顺序（审查报告 M4）**：先算 sha256 → 查幂等键 → 只有确实是新文件才落盘。
    旧实现每次上传都无条件写一份 `<uuid8>_<原名>`，于是"重复上传"会不断往 uploads/
    里堆同样的字节、永不回收。现在重复上传不产生新文件。
    """
    conn = ctx.conn
    settings = ctx.settings
    thread = get_thread(conn, thread_id)
    user_id = str(thread["user_id"])
    upload_dir = settings.upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "report.bin").name  # 去掉任何路径成分
    # 文件名消毒（审查报告 A4）：截断 + 去掉控制字符/分隔符 —— 超长名会让文件系统直接报错，
    # 而报错发生在写盘之后就成了 500 而不是可读的 400。
    safe_name = _sanitize_filename(safe_name)

    # M5：流式读 + 增量哈希 + 增量落盘到临时 spill，**不再把整文件读进内存**（20MB 峰值消失）。
    # 先落 spill 是为了拿到 sha256 去做幂等查重；最终按幂等结果 rename 到正式路径或丢弃，
    # 避免重复落盘（正常重复上传零写入）。
    spill = upload_dir / f".{uuid.uuid4().hex[:12]}.part"
    hasher = hashlib.sha256()
    size = 0
    try:
        with spill.open("wb") as out:
            while True:
                chunk = file.file.read(1 << 20)  # 1 MB 一块
                if not chunk:
                    break
                size += len(chunk)
                if size > UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=400, detail="文件超过 20MB 上限。")
                hasher.update(chunk)
                out.write(chunk)
    except HTTPException:
        spill.unlink(missing_ok=True)
        raise
    except Exception:
        spill.unlink(missing_ok=True)
        raise
    if size == 0:
        spill.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="空文件。")
    file_hash = hasher.hexdigest()

    # 幂等：同一份字节只登记一次、只落盘一次。
    prior = ctx.ingestion.find_by_hash(user_id, file_hash)
    if prior is not None:
        task_id = str(prior["task_id"])
        reused = True
        existing = prior
        target = Path(str(prior["source_file"] or ""))
        if not target.is_file():
            # 台账在、文件没了（人工删除 / 数据卷重置）。**自愈**：把这次上传的字节写下来，
            # 并把台账指过去 —— 否则"重复上传同一个文件"会一路走到解析失败（实测 500）。
            # 注意这不是"重复落盘"：只有在原文件确实缺失时才写，正常重复上传仍然零写入。
            target = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
            spill.replace(target)  # 原子改名到正式路径
            ctx.ingestion.relink_source(task_id, str(target))
        else:
            spill.unlink(missing_ok=True)  # 已有文件：丢弃 spill（零写入）
        if existing["status"] == INGESTION_FAILED:
            # **failed 必须是一条可走出的路**：重传同一份文件 = 用户在重试。状态机唯一允许的
            # 回边是 failed → pending，而此前没有任何代码执行它 —— 于是任务永远停在 failed，
            # 哪怕这次已经重新解析并入库成功，台账还在说"失败"（与事实背离）。
            # 显式重启后走下面的正常推进链。
            ctx.ingestion.advance(task_id, INGESTION_PENDING)
            existing["status"] = INGESTION_PENDING
    else:
        target = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        spill.replace(target)
        task_id = ctx.ingestion.create(
            user_id=user_id, source_file=str(target), file_hash=file_hash
        )
        reused = False
        existing = ctx.ingestion.get(task_id)

    # v2.2：统一解析入口——.txt/.md/.pdf 直接抽文本入检索索引；图片走 OCR 子进程
    # （独立 venv，见 requirements-ocr.txt）；其余类型保持 pending。
    suffix = target.suffix.lower()
    if suffix in PARSEABLE_EXTENSIONS:
        try:
            # 仅图片需要选 OCR 后端：按「服务」页签的端点顺序（默认 Paddle 优先）。
            # L3：选择逻辑收拢到 AppContext.ocr_candidates()（原与 records.py 重复）。
            backend = ctx.ocr_candidates() if suffix in IMAGE_EXTS else None
            text = parse_document(target, backend=backend)
        except OcrUnavailable:
            # 后端未配置：图片保持 pending，明确告知模型不可读（不把 paddle 栈拖进主环境）。
            note = (
                f"[用户上传了图片报告：{safe_name}，已登记 intake 任务 {task_id}"
                f"（status={existing['status']}）。OCR 后端未配置"
                "（本地 Paddle 不可用，且「服务」页没有就绪的云端 OCR / 视觉模型端点），"
                "当前不能读取图片内容，不要假装已经读过。]"
            )
            graph_config = {"configurable": {"thread_id": thread_id}}
            ctx.app_state["graph"].update_state(
                graph_config, {"messages": [HumanMessage(content=note)]}
            )
            return {
                "task_id": task_id,
                "reused": reused,
                "file": safe_name,
                "status": existing["status"],
                "parsed": False,
            }
        except ParseError as exc:
            # 解析硬失败 → 任务标记 failed（否则永远停在 pending），并返回可读的 500
            ctx.ingestion.record_failure(task_id, str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        if text.strip():
            # 存一份解析文本：结构化抽取复用它，避免对同一张图片再跑一次 OCR（OCR 很贵）。
            try:
                parsed_text_path(target).write_text(text, encoding="utf-8")
            except OSError as exc:
                # 不再静默吞掉（审查报告 E2）：有现场重解析兜底，但"为什么抽取又跑了 OCR"
                # 必须能在轨迹里查到原因。
                ctx.tracer.emit(
                    TraceEvent(
                        event="parsed_text_write_failed",
                        thread_id=thread_id,
                        error=f"{type(exc).__name__}: {exc}",
                        detail={"target": target.name},
                    )
                )
            try:
                # 索引身份用 task_id（按内容 hash 去重 → 同一份字节重建、不同内容彼此
                # 独立），**不能用 safe_name**：同名文件会互相覆盖，旧文档索引静默丢失
                # （审查报告 P0）。文件名只作展示名，引用里显示的仍是它。
                chunks = ctx.knowledge.index(
                    ctx.health.knowledge_scope, task_id, text, source_name=safe_name
                )
            except (KnowledgeDimensionMismatch, EmbedError) as exc:
                # 两者都是"管理员可修复"的状态，且都发生在**索引没被破坏**之后
                # （index() 已改为先嵌入再写库，见审查报告 M4）。给出可操作的原因。
                ctx.ingestion.record_failure(task_id, str(exc))
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            chain: tuple[str, ...] = ("parsed", "extracted", "indexed")
            note = (
                f"[用户上传了文档：{safe_name}（{chunks} 段），已建立检索索引"
                f"（任务 {task_id}，status=indexed）。注意：能否检索到取决于当前角色的"
                "knowledge_scopes 授权；未授权时请提示用户切换角色，不要假装已经读过。]"
            )
        else:
            # 解析出空文本（扫描件 / 无文本层的 PDF）：解析到 parsed 即止，不入索引。
            chain = ("parsed",)
            note = (
                f"[用户上传了文件：{safe_name}，已解析但未提取到文本（可能为扫描件）。"
                f"已登记任务 {task_id}（status={existing['status']}），暂不入检索。]"
            )
        if (existing["status"] or "pending") == "pending":
            # 幂等：重复上传同一文件会复用已 indexed 的任务，不能再推进状态机。
            for next_status in chain:
                ctx.ingestion.advance(task_id, next_status)
            existing = ctx.ingestion.get(task_id)
    else:
        note = (
            f"[用户上传了报告文件：{safe_name}，已登记 intake 任务 {task_id}"
            f"（status={existing['status']}）。文件类型暂不支持自动解析（v2.2 支持 "
            ".txt/.md/.pdf/.docx/.pptx/.xlsx 及图片 OCR），当前不能读取其中内容，"
            "不要假装已经读过。]"
        )
    graph_config = {"configurable": {"thread_id": thread_id}}
    ctx.app_state["graph"].update_state(graph_config, {"messages": [HumanMessage(content=note)]})
    return {
        "task_id": task_id,
        "reused": reused,
        "file": safe_name,
        "status": existing["status"],
    }


__all__ = ["router"]
