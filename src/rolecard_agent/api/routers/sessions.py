"""会话与对话路由：session CRUD / SSE 对话 / 历史回放 / 上传。

从 `main.py` 迁出的第 2 组（C1，最大的一组）。上传也归这里，因为它是"往会话里塞
东西"—— 注入的说明消息直接进会话的 checkpoint。
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.chat import chat_events
from rolecard_agent.api.deps import (
    DEFAULT_ROLE_ID,
    DEFAULT_USER_ID,
    AppContext,
    get_actor,
    get_context,
    get_thread,
    parsed_text_path,
    role_error_to_http,
    serialize_message,
)
from rolecard_agent.core.state import new_state
from rolecard_agent.rag.ocr import select_ocr_backend
from rolecard_agent.rag.parser import (
    IMAGE_EXTS,
    PARSEABLE_EXTENSIONS,
    OcrUnavailable,
    ParseError,
    parse_document,
)
from rolecard_agent.rag.retriever import KnowledgeDimensionMismatch
from rolecard_agent.roles.service import RoleError, RoleNotFound

router = APIRouter()


class SessionCreate(BaseModel):
    """Create-session request. Omitting `role_id` binds the default assistant."""

    role_id: str | None = None


class SessionPatch(BaseModel):
    """Session partial update: switch role / rename / set session model override.
    At least one field required."""

    role_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=100)
    model_name: str | None = None


class ChatMessage(BaseModel):
    """One user turn. Length-capped so a pasted novel cannot become a checkpoint bomb."""

    thread_id: str
    message: str = Field(min_length=1, max_length=8000)


# 上传大小上限：请求体整个读进内存算哈希，20MB 是演示负载的合理护栏。
UPLOAD_MAX_BYTES = 20 * 1024 * 1024


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
    }


@router.patch("/api/session/{thread_id}")
def patch_session(
    thread_id: str,
    body: SessionPatch,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """会话局部更新：切角色（US-1，不触碰历史）/ 重命名 / 设置会话级模型覆盖。

    model_name 语义（model_fields_set 区分"未提供"与"显式置空"）：
    未提供 = 不改；null = 清除覆盖（回落 角色.model_name → 默认）；名字 = 会话覆盖。
    覆盖名必须在有效后端列表里，否则 400（回退由模型解析器兜底，但配置错误仍要大声）。
    """
    conn = ctx.conn
    thread = get_thread(conn, thread_id)
    touched = body.model_fields_set & {"role_id", "title", "model_name"}
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
    role = ctx.roles.get(final_role_id)
    row = conn.execute(
        "SELECT title, model_name FROM session_thread WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    return {
        "thread_id": thread_id,
        "role_id": final_role_id,
        "role_name": role.role_name,
        "title": row["title"] if row else None,
        "model_name": row["model_name"] if row else None,
    }


@router.post("/api/chat")
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
    try:
        role = ctx.roles.get(role_id)
    except RoleNotFound as exc:
        raise role_error_to_http(exc) from exc

    # 侧栏标题：首轮消息截断生成；updated_at 每轮刷新，会话列表按它倒序。
    # 用毫秒精度（strftime %f）而非 CURRENT_TIMESTAMP（秒级）：同一秒内创建的两个
    # 会话需要靠"谁最近活跃"严格排序，秒级会打平、只能靠随机 thread_id 兜底。
    conn.execute(
        "UPDATE session_thread SET title = COALESCE(title, ?), "
        "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE thread_id = ?",
        (body.message[:24], body.thread_id),
    )
    conn.commit()

    graph_config = {"configurable": {"thread_id": body.thread_id}}
    snapshot = graph.get_state(graph_config)
    if snapshot.values:
        graph_input: dict[str, object] = {
            "messages": [HumanMessage(content=body.message)],
            "current_role_id": role_id,
            "model_name": session_model,  # 每轮实时注入：会话切模型下一轮即生效
        }
    else:
        graph_input = {
            **new_state(
                thread_id=body.thread_id,
                user_id=user_id,
                current_role_id=role_id,
                model_name=session_model,
                enabled_domains=ctx.plugins.enabled_domains(),
                tool_epoch=ctx.plugins.tool_epoch(),
            ),
            "messages": [HumanMessage(content=body.message)],
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


@router.get("/api/sessions")
def list_sessions(ctx: AppContext = Depends(get_context)) -> list[object]:
    """会话列表（对话页侧栏）。v1 单用户演示：只列演示身份名下的会话。"""
    rows = ctx.conn.execute(
        "SELECT s.thread_id, s.title, s.current_role_id AS role_id, r.role_name, "
        "s.updated_at FROM session_thread s "
        "LEFT JOIN role_card r ON r.role_id = s.current_role_id "
        "WHERE s.user_id = ? ORDER BY s.updated_at DESC, s.thread_id",
        (DEFAULT_USER_ID,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/session/{thread_id}/messages")
def get_session_messages(thread_id: str, ctx: AppContext = Depends(get_context)) -> list[object]:
    """历史消息回放（来源：checkpoint，而非单独的聊天记录表）——
    点击历史会话续聊时，前端用它恢复消息区。"""
    get_thread(ctx.conn, thread_id)
    snapshot = ctx.app_state["graph"].get_state({"configurable": {"thread_id": thread_id}})
    return [serialize_message(m) for m in (snapshot.values or {}).get("messages", [])]


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
    不可用时若有 OCR_API_KEY 回退云端，见 rag/ocr.py + requirements-ocr.txt）。
    解析失败的图片 / 不支持的类型保持 pending，并向会话注入一条说明消息（graph.update_state），
    让模型知道"有文件已登记但还不能读"，而不是假装读过。重复上传同一文件复用同一任务。
    """
    conn = ctx.conn
    settings = ctx.settings
    thread = get_thread(conn, thread_id)
    user_id = str(thread["user_id"])
    data = file.file.read()  # 同步端点读同步文件对象（见 docstring：不阻塞事件循环）
    if not data:
        raise HTTPException(status_code=400, detail="空文件。")
    if len(data) > UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 20MB 上限。")

    upload_dir = settings.upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "report.bin").name  # 去掉任何路径成分
    target = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    target.write_bytes(data)
    file_hash = hashlib.sha256(data).hexdigest()

    before = len(ctx.ingestion.list_for_user(user_id))
    task_id = ctx.ingestion.create(user_id=user_id, source_file=str(target), file_hash=file_hash)
    reused = len(ctx.ingestion.list_for_user(user_id)) == before
    existing = ctx.ingestion.get(task_id)

    # v2.2：统一解析入口——.txt/.md/.pdf 直接抽文本入检索索引；图片走 OCR 子进程
    # （独立 venv，见 requirements-ocr.txt）；其余类型保持 pending。
    suffix = target.suffix.lower()
    if suffix in PARSEABLE_EXTENSIONS:
        try:
            # 仅图片需要选 OCR 后端：按「服务」页签的端点顺序（默认 Paddle 优先）。
            backend = (
                select_ocr_backend(
                    settings,
                    order=[c.id for c in ctx.services.ordered_candidates("ocr")],
                    endpoints=ctx.services.endpoint_map("ocr"),
                )
                if suffix in IMAGE_EXTS
                else None
            )
            text = parse_document(target, backend=backend)
        except OcrUnavailable:
            # 后端未配置：图片保持 pending，明确告知模型不可读（不把 paddle 栈拖进主环境）。
            note = (
                f"[用户上传了图片报告：{safe_name}，已登记 intake 任务 {task_id}"
                f"（status={existing['status']}）。OCR 后端未配置"
                "（本地 Paddle 不可用，且未配置 OCR_API_KEY），当前不能读取图片内容，"
                "不要假装已经读过。]"
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
            with contextlib.suppress(OSError):
                parsed_text_path(target).write_text(text, encoding="utf-8")
            try:
                chunks = ctx.knowledge.index("health_reports", safe_name, text)
            except KnowledgeDimensionMismatch as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            chain = ("parsed", "extracted", "indexed")
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
