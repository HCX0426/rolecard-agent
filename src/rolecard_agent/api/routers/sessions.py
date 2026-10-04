"""会话与对话路由：session CRUD / SSE 对话 / 历史回放 / 上传。

从 `main.py` 迁出的第 2 组（C1，最大的一组）。上传也归这里，因为它是"往会话里塞
东西"—— 注入的说明消息直接进会话的 checkpoint。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

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
    serialize_message,
)
from rolecard_agent.base.observability import TraceEvent
from rolecard_agent.base.text import text_of
from rolecard_agent.core import memory_distill, session_service, upload_service
from rolecard_agent.core.graph import build_graph_config
from rolecard_agent.core.proactive_thread import (
    PROACTIVE_THREAD_PREFIX,
    ensure_proactive_thread,
    proactive_thread_id,
)
from rolecard_agent.core.state import new_state, now_ts
from rolecard_agent.core.thread_locks import (
    inflight_text,
    request_stop,
    thread_write,
)
from rolecard_agent.core.usage import TokenUsage, record_usage

router = APIRouter()

class SessionCreate(BaseModel):
    """Create-session request. Omitting `role_id` binds the default assistant."""

    role_id: str | None = None


class ProactiveSessionBody(BaseModel):
    """桌宠面板的落点请求：只要角色 id，线程 id 是从它推出来的（不给客户端猜的机会）。"""

    role_id: str = Field(min_length=1, max_length=64)


class SessionPatch(BaseModel):
    """Session partial update: switch role / rename / set session model override /
    set conversation mode. At least one field required."""

    role_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=100)
    model_name: str | None = None
    # 会话级对话模式：'chat' / 'agent'（显式设置）；null / 空串 = 清除，回落全局默认
    # （settings.agent_default_mode）。
    agent_mode: str | None = None


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
    role = ctx.role_cards.get(role_id)
    thread_id = session_service.create(
        ctx.conn,
        user_id=ctx.current_user(),
        role_id=role_id,
        tool_epoch=ctx.plugins.tool_epoch(),
    )
    ctx.audit.log(
        actor=actor.id, action="create_session", target=thread_id, detail={"role_id": role_id}
    )
    return {"thread_id": thread_id, "role_id": role_id, "role_name": role.role_name}


@router.post("/api/session/proactive", status_code=201)
def open_proactive_session(
    body: ProactiveSessionBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """确保该角色的**主动会话**存在并返回它（设计稿 §7.2.1，桌宠面板的落点）。

    为什么桌宠要这个而不是 `POST /api/session`：这条线是"角色主动找我 + 我回它"的同一处
    历史，角色下次开口读的就是它（`deliver_proactive` 写进同一个线程）。另开一条随机线程
    会让两边各记一半 —— 用户拍的那句"不然和人交流就会断掉记忆了"就是这个意思。

    幂等：从没被主动找过的角色也能先在桌宠上聊起来；被删掉后再调一次会长回同一行
    （已提炼进角色记忆的事实不跟着走，那条边界有断言钉着）。
    """
    role = ctx.role_cards.get(body.role_id)
    thread_id = ensure_proactive_thread(
        ctx.conn, role=role, user_id=ctx.current_user(), tool_epoch=ctx.plugins.tool_epoch()
    )
    ctx.audit.log(
        actor=actor.id,
        action="open_proactive_session",
        target=thread_id,
        detail={"role_id": role.role_id},
    )
    return {"thread_id": thread_id, "role_id": role.role_id, "role_name": role.role_name}


@router.get("/api/session/proactive")
def proactive_session_of(
    role_id: str = Query(..., max_length=64),
    ctx: AppContext = Depends(get_context),
) -> object:
    """问一句"这个角色的主动会话在不在、id 是什么"—— **只读，不建行**。

    为什么要有这条（2026-09-23 用户报"桌宠的历史消息没记录了，切换角色也没"）：面板以前
    是从"最近一条主动消息"倒推线程 id 的，于是**清空抽屉**（`DELETE /api/reachouts`）之后
    行没了、面板就没了读的对象，而那条会话连同历史一直好好地在库里。读历史不该依赖投递记录。
    不建行的理由同 `ensure_proactive_session` 的反面：只是打开面板看一眼，不该在侧栏长出
    一条"从没被找过的角色 · 主动找你"。
    """
    tid = proactive_thread_id(role_id, user_id=ctx.current_user())
    return {"thread_id": tid if session_service.exists(ctx.conn, tid) else None, "role_id": role_id}


@router.get("/api/session/{thread_id}")
def get_session(thread_id: str, ctx: AppContext = Depends(get_context)) -> object:
    row = get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    # 会话指向已被删除的角色：会话本身还在，角色信息降级为空（图侧有同样的兜底）。
    # 降级在 service 里收成一处 —— patch 端点当年漏了这格，"只改个标题"也会 500。
    role_name = session_service.role_name_of(ctx.role_cards, str(row["current_role_id"]))
    return {
        "thread_id": row["thread_id"],
        "user_id": row["user_id"],
        "role_id": row["current_role_id"],
        "role_name": role_name,
        "model_name": row["model_name"],
        # 返回**有效**模式（会话覆盖 or 全局默认）：前端切换钮直接按它渲染当前状态。
        "agent_mode": session_service.resolve_agent_mode(row["agent_mode"], ctx.settings),
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
    thread = get_thread(conn, thread_id, user_id=ctx.current_user())
    touched = body.model_fields_set & {"role_id", "title", "model_name", "agent_mode"}
    if not touched:
        raise HTTPException(status_code=400, detail="没有任何要更新的字段。")

    if body.role_id:
        # 角色/线程不存在都抛 RoleNotFound → 注册表给 404（`api/errors.py` 的
        # `_FAMILIES`）；这里不再自己 catch —— 漏 catch 的下场是 500 空壳。
        ctx.roles.set_thread_role(
            thread_id, body.role_id, user_id=ctx.current_user(), actor=actor.id
        )

    if body.model_name is not None and body.model_name.strip() == "":
        body.model_name = None  # 空串 = 清除覆盖

    if "model_name" in body.model_fields_set:
        name = body.model_name
        if name is not None:
            # 可选项来自**这次调用真会花的那一族**（M2d 尾巴收口之后）：图按本轮主人取凭据
            # （`Runtime.effective_for`），所以校验必须按同一个人 —— 按实例主人过滤会放行一个
            # "存得下、却跑不动"的名字（他那份快照里压根没有这个后端）。
            effective = ctx.model_settings.effective_settings(
                ctx.settings, user_id=ctx.current_user()
            )
            if name not in effective.model_backends:
                known = ", ".join(sorted(effective.model_backends))
                raise HTTPException(status_code=400, detail=f"未知模型 {name!r}；可用：{known}")
        session_service.set_model(conn, thread_id, body.model_name)
        ctx.audit.log(
            actor=actor.id,
            action="set_session_model",
            target=thread_id,
            detail={"model_name": body.model_name},
        )

    if "agent_mode" in body.model_fields_set:
        mode = body.agent_mode
        if mode is not None and mode.strip() == "":
            mode = None  # 空串 = 清除覆盖，回落全局默认
        if mode is not None and mode not in session_service.MODE_CHOICES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"未知对话模式 {mode!r}；可用：{' / '.join(session_service.MODE_CHOICES)}"
                ),
            )
        session_service.set_mode(conn, thread_id, mode)
        ctx.audit.log(
            actor=actor.id,
            action="set_session_mode",
            target=thread_id,
            detail={"agent_mode": mode},
        )

    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空。")
        session_service.set_title(conn, thread_id, title)

    final_role_id = body.role_id or str(thread["current_role_id"])
    # 角色已被删除（角色 CRUD 的常规后果）：必须降级而不是抛 —— 从前 get_session 与这里
    # 各写一份 try/except，本端点漏了那份，于是"只改个标题"也会 500（审查报告 M1，已复现）。
    role_name = session_service.role_name_of(ctx.role_cards, final_role_id)
    row = session_service.display_row(conn, thread_id)
    return {
        "thread_id": thread_id,
        "role_id": final_role_id,
        "role_name": role_name,
        "title": row["title"] if row else None,
        "model_name": row["model_name"] if row else None,
        "agent_mode": (
            session_service.resolve_agent_mode(row["agent_mode"], ctx.settings)
            if row
            else "chat"
        ),
    }


@router.post("/api/chat")
# 刻意**不是** async def：函数体里跑的全是同步阻塞调用（sqlite / graph.get_state /
# checkpointer 读全量历史）。async 版本会把这些阻塞**放到事件循环上**，一次模型等待
# 就能卡住其它会话的 SSE。同步路由由 Starlette 放进线程池执行，而返回的
# StreamingResponse 内部是 async 生成器 —— 流式并不要求路由本身是 async
# （审查报告 P2：异步路由内的同步阻塞）。
def chat(body: ChatMessage, ctx: AppContext = Depends(get_context)) -> StreamingResponse:
    """SSE 流式对话。线程必须已存在（POST /api/session 创建）。

    首轮注入完整初始状态（`new_state`）；续轮只注入新消息 + 实时角色 —— 后者让
    PATCH /api/session 的切角色在下一轮立即生效，而 enabled_domains / tool_epoch 不进
    输入，让 checkpoint 里的旧值保留，`call_model` 的 epoch 漂移检测才能每个变化只报
    一次（C14）。图从 `app_state` 现取：设置页保存热重建后，下一次对话自动用新图。
    """
    conn = ctx.conn
    graph = ctx.app_state["graph"]
    thread = get_thread(conn, body.thread_id, user_id=ctx.current_user())
    role_id = str(thread["current_role_id"])
    user_id = str(thread["user_id"])
    session_model = thread["model_name"]  # 会话级覆盖（可 None），每轮实时读库
    # 会话级对话模式（有效值 = 会话覆盖 or 全局默认），每轮实时读库 + 实时回落：
    # 会话切「对话/智能体」或操作员改 AGENT_DEFAULT_MODE，下一轮即生效。
    session_mode = session_service.resolve_agent_mode(
        thread["agent_mode"], ctx.app_state["effective"]
    )
    role = ctx.role_cards.get(role_id)

    # 侧栏标题：首轮消息截断生成；updated_at 每轮刷新，会话列表按它倒序。
    # 纯图消息没有文本 → 标题用 "[图片]"，COALESCE 兜底空标题（首次就覆盖）。
    # 毫秒精度的理由与唯一出处见 `storage/threads._TOUCH_SQL`（`R102-62`），经 service 转发。
    title_fallback = "[图片]" if not body.message.strip() else body.message[:24]
    session_service.seed_title(conn, body.thread_id, title_fallback)

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
            usage_recorder=_usage_ledger(
                conn,
                ctx.runtime.effective_for(user_id).backend_name(
                    session_model or role.model_name
                ),
                ctx.tracer,
                user_id=user_id,
            ),
            # 轮后钩子（自动记忆提取）由 run_turn 的 finally 统一执行：断线轮次与正常
            # 轮次行为一致（从前挂在 async 生成器尾部，断线即丢 —— 收尾不对称条目）。
            # 提交进进程级提取池（连接复用、槽有界），run_turn 的收尾线程不背 122s 的提取。
            after_turn=lambda: _schedule_distill(
                ctx, thread_id=body.thread_id, role_id=role_id
            ),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/session/{thread_id}/stop")
def stop_turn(
    thread_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """叫停这一轮 —— 只做一件事：立一枚取消旗，别的一律交给正在消费流的那一层去看。

    为什么不"直接取消那个线程"：Python 没有安全的强杀，而 `await run_in_executor(next)` 被
    取消**并不中断**线程池里已经在跑的那次 `next()`（审计 §12.12② 实测）。所以停必须走到
    **分块边界**上：`call_model` 自己拿住了模型的流（`_collect_model_stream`），看见旗子就
    `close()` 收手 —— 实测关掉连接后 Ollama 不到 1s 就停止生成（1-token 探针 0.30/0.20/0.16s，
    基线 0.12s），云端同理是连接一断就不再计。

    幂等：按两次停止没有额外后果。这一轮已经跑完时旗子会留到**下一轮开始**才被清
    （`run_turn` 开头），所以这里不需要去问"那轮还在不在"。

    **但要先问这条线程是不是你的**（M4）：旗子是按 `thread_id` 立的，而
    `s_proactive_<uid>_<role_id>` 这种 id 是**可猜的**（角色 id 是公开短串）。不比对归属
    就等于"任何人都能打断别人那一轮" —— 读侧的 404 纪律在这里同样适用，别人名下的线程
    回 404，不承认它存在。
    """
    get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    request_stop(thread_id)
    ctx.audit.log(actor=actor.id, action="stop_turn", target=thread_id, detail={})
    return {"thread_id": thread_id, "requested": True}


# -- 提取精华（对话 → 记忆条目）------------------------------------------------


def _thread_model(ctx: AppContext, thread: dict, role_id: str) -> tuple[Any, str | None]:
    """提取这一步**真正在用**的模型与它的后端名。

    优先级：`MEMORY_EXTRACT_BACKEND`（运行环境里显式指定的提取后端）> 会话覆盖 > 角色覆盖 > 默认。

    为什么把"提取用谁"与"对话用谁"分开（审计 §12.5）：角色留在本地陪聊，不代表它的长期记忆
    也该由 8B 来判 —— 提取与整理是"决定什么将成为关于你的永久事实"那一步，判官强度可以单独选。
    （依据更正两次，见审计 §12.5：09-22 那句"本地提 0 条"复现不了；而把指令的捏造口子与
    "自动提取自我叠加"修掉之后，同一段八轮对话本地 9 条 / 云端 10 条，两边都 0 捏造 0 同义。
    所以这不是"有没有记忆"的开关，是延迟与判断力的取舍。）
    以前这里刻意跟随对话后端，理由是"换到更弱的模型上抽事实会悄悄变笨"；那个理由仍然成立，
    只是它现在有了一个显式的、可清空的答案，而不是只能靠把整个角色搬上云端来解决。
    **没配就是不动**：填了才出网（用户 2026-09-24 同意健康数据可云端分析，但默认值不该替他决定）。

    后端名一起返回（而不是让调用方再解一遍）：token 账要按后端分（审计 §12.8），
    而"谁在用哪个后端"这件事只该有一处答案。

    **模型必须按这条线程的主人构建**（用 `thread["user_id"]`）：这个函数从"响应流完之后"
    的**池线程**里被调（`memory_distill.after_turn`，经 `_schedule_distill` 提交），
    而 `bound_user` 是 `ContextVar`、不跨线程传播 —— 不显式传就会拿实例主人的凭据替别人
    抽记忆。`ctx.current_user()` 在这里反而是对的（它是请求视图上的备忘属性），
    所以别把两者混起来看。
    """
    owner = str(thread["user_id"])
    want = (ctx.settings.memory_extract_backend or "").strip()
    if want:
        return ctx.runtime.resolve_role_model(want, user_id=owner), want
    name = thread["model_name"]
    if not name:
        try:
            name = ctx.role_cards.get(role_id).model_name
        except Exception:  # noqa: BLE001 - 角色被删了就用默认，提取不该因此 500
            name = None
    return ctx.runtime.resolve_role_model(name, user_id=owner), name


def _usage_ledger(
    conn: Any, backend: str | None, tracer: Any, *, user_id: str
) -> Callable[[TokenUsage | None], None]:
    """这一轮对话的 token 落点（审计 §12.8/#8）。

    为什么账要由 `core/turn.py` 递出来、而不是在 `call_model` 里记：供应商在每一个流式
    分块里都回一份"累计到此"的 usage，langchain 合并时逐块相加 —— 节点里看到的值是
    真值 × 分块数（实测一条"在吗"：26 → 272,607）。后端名用这一轮**实际服务**的那个，
    否则云端与本地会在账上混成一行。**`user_id` = 这一轮花谁的 key**（多租户 B1a）：
    `backend` 是按 `effective_for(本轮主人)` 解析出来的，账就必须记在同一个主人名下。
    """

    def record(usage: TokenUsage | None) -> None:
        # 返回值不吃掉：记不上账不该影响这一轮（fail-open），而坏账的留声在 `record_usage` 里。
        record_usage(conn, backend=backend, usage=usage, user_id=user_id, tracer=tracer)

    return record


def _schedule_distill(ctx: AppContext, *, thread_id: str, role_id: str) -> None:
    """把这一轮的兜底提取提交进池（总闸关闭时是 no-op）。

    状态机整段在 `memory_distill.after_turn`（取行 → 历史 → 游标 → 在飞闸 → 提取 →
    留痕 → 归还），这里只绑它要的两样**数据**并按下提交：取历史要 graph、解析模型要
    runtime，那都是宿主的活；池线程里跑的是回调，不借 AppContext 的任何其它部分。
    池本身也归了服务（`memory_distill.DISTILL_POOL`）。
    """
    if not (ctx.settings.memory_enabled and ctx.settings.memory_extract_auto):
        return

    def load_context() -> tuple[Any, list[Any]]:
        # 归属校验与历史读取按原顺序在**池线程里**做（改模型的时机必须是提取那一刻，
        # 不是提交那一刻 —— 用户提交后、提取前换了模型，按提取时的算）。
        thread = get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
        _, messages = _history_messages(ctx, thread_id)
        return thread, messages

    memory_distill.DISTILL_POOL.submit(
        memory_distill.after_turn,
        ctx.conn,
        thread_id=thread_id,
        role_id=role_id,
        extract_turns=ctx.settings.memory_extract_turns,
        tracer=ctx.tracer,
        load_context=load_context,
        resolve_model=lambda thread: _thread_model(ctx, thread, role_id),
    )


@router.post("/api/session/{thread_id}/distill")
def distill_session(
    thread_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """手动「提取精华」：把这段会话抽成条目写进该角色的记忆桶（一次真模型调用）。

    为什么按钮在对话页而不在记忆卡：提取的输入是**这段对话**；记忆卡上那个手动按钮做的是
    另一件事（「整理记忆」，输入是已有条目）。两个按钮各自只需要自己那份输入，不互相冒充。
    写进的是该角色的桶（不是全局）—— 对话是跟这个角色说的，回忆也只在它这里被引用。
    """
    if not ctx.settings.memory_enabled:
        raise HTTPException(
            status_code=400,
            detail="跨会话记忆当前是关闭的 —— 先在「设置 → 记忆与任务目录」打开它。",
        )
    thread = get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    _, messages = _history_messages(ctx, thread_id)
    role_id = str(thread["current_role_id"])
    # 手动按钮同一条口径：抽的是"上次提取之后"的那几条，不是整段。第一次点（游标 0）时
    # 两者相等，所以行为不变；变的是"聊了一阵再点一次"——那时不该把老事实换个说法再记一遍。
    pending = memory_distill.pending_messages(ctx.conn, thread_id=thread_id, messages=messages)
    if not pending:
        return {
            "report": memory_distill.nothing_new(
                ctx.conn, user_id=str(thread["user_id"]), bucket=role_id
            ),
            "turns_since": 0,
        }
    model, backend = _thread_model(ctx, thread, role_id)
    outcome = memory_distill.extract(
        ctx.conn,
        user_id=str(thread["user_id"]),
        model=model,
        bucket=role_id,
        messages=pending,
        backend=backend,
        tracer=ctx.tracer,
    )
    report = outcome["report"]
    if outcome["ok"]:
        memory_distill.mark_extracted(ctx.conn, thread_id=thread_id, message_count=len(messages))
    # 审计只记**结构与条数**，绝不记提取出来的内容（那是用户的事实）。
    ctx.audit.log(
        actor=actor.id,
        action="extract_memory",
        target=f"memory:{role_id}",
        detail={"thread_id": thread_id, **{k: v for k, v in report.items() if v and k != "detail"}},
    )
    ctx.tracer.emit(
        TraceEvent(
            event="memory_extract",
            node="memory",
            thread_id=thread_id,
            role_id=role_id,
            tokens=report.get("tokens"),
            detail={"trigger": "manual", **{k: v for k, v in report.items() if v}},
        )
    )
    if not outcome["ok"]:
        raise HTTPException(status_code=502, detail=report["detail"] or "模型调用失败。")
    return {"report": report, "turns_since": 0}


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
    thread = get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    graph = ctx.app_state["graph"]
    # 这份 config 既用于 get_state / update_state，也直接喂给下面的 graph.stream ——
    # 所以步数上限在这里就必须带上（否则编辑重生成那条路仍是无上界的）。
    # agent 模式上限放大一倍（与 /api/chat 同一口径，见 core/graph.build_graph_config）。
    mode = session_service.resolve_agent_mode(thread["agent_mode"], ctx.app_state["effective"])
    config = build_graph_config(thread_id, ctx.app_state["effective"], agent_mode=mode == "agent")
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
    thread = get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    role_id = str(thread["current_role_id"])
    session_model = thread["model_name"]
    session_mode = session_service.resolve_agent_mode(
        thread["agent_mode"], ctx.app_state["effective"]
    )
    role = ctx.role_cards.get(role_id)

    target = next((m for m in messages if getattr(m, "id", None) == body.message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="消息不存在（可能已被删除或线程不匹配）。")
    if not isinstance(target, HumanMessage):
        raise HTTPException(status_code=400, detail="只能编辑自己发送的消息。")

    index = messages.index(target)
    # 目标及其之后的全部作废（RemoveMessage 按 id 精确删除，不触碰前面的历史）
    # 无 id 的消息无法被 RemoveMessage 定位（正常不会出现，防御性跳过）。
    doomed = [RemoveMessage(id=m.id) for m in messages[index:] if m.id is not None]
    # 改检查点要占住这条会话（审计 #12）：紧随其后的那一轮由 `run_turn` 自己持锁，
    # 而中间这一秒若被调度线程的主动投递插进来，两边会分叉同一个父检查点。
    with thread_write(thread_id, timeout=session_service.WRITE_WAIT):
        graph.update_state(config, {"messages": doomed})

    graph_input: dict[str, object] = {
        "messages": [_user_message(body.content, body.image, created_at=now_ts())],
        "current_role_id": role_id,
        "model_name": session_model,
        "agent_mode": session_mode,
    }
    session_service.touch(ctx.conn, thread_id)
    ctx.conn.commit()

    return StreamingResponse(
        chat_events(
            graph,
            graph_input=graph_input,
            config=config,
            role_summary={"role_id": role.role_id, "role_name": role.role_name},
            tracer=ctx.tracer,
            # 重新生成花的也是真钱：不记就等于"这一轮没发生"，账会静悄悄地少一截。
            # 后端名按**这条线程的主人**那份解析：拿实例主人那份去解别人的后端名会落回默认，
            # 账就记到别人头上了。
            usage_recorder=_usage_ledger(
                ctx.conn,
                ctx.runtime.effective_for(str(thread["user_id"])).backend_name(
                    session_model or role.model_name
                ),
                ctx.tracer,
                user_id=str(thread["user_id"]),
            ),
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
    # 删历史也要占住这条会话（R28-03）：这一句以前是裸 `update_state`，
    # 而用户那一轮正在往同一个父检查点追加 —— 两边后写谁赢，症状是"消息又凭空多回来一条
    # 或者少了一条"。等不到锁就 409（`thread_write` 现在会抛，不再把布尔丢给调用方）。
    with thread_write(thread_id, timeout=session_service.WRITE_WAIT):
        graph.update_state(config, {"messages": [RemoveMessage(id=i) for i in doomed_ids]})
    session_service.touch(ctx.conn, thread_id)
    ctx.conn.commit()
    return {"deleted": len(doomed_ids), "remaining": len(messages) - len(doomed_ids)}


@router.get("/api/sessions")
def list_sessions(ctx: AppContext = Depends(get_context)) -> list[object]:
    """会话列表（对话页侧栏）：只列**这次请求那个身份**名下的会话。"""
    rows = session_service.list_rows(ctx.conn, ctx.current_user())
    return [
        {
            # `has_state` 只当内部中间量、**不下线**（`R102-21`）：它在本仓产物/源码/壳三处
            # 0 命中，是一个没人守的线字段 —— 下一次改名没人知道该不该同步。判据仍是
            # "EXISTS 写过东西没有"，但暴露给界面的只有 is_blank 这一个名字。
            **{k: v for k, v in dict(r).items() if k != "has_state"},
            "agent_mode": session_service.resolve_agent_mode(r["agent_mode"], ctx.settings),
            # 侧栏分"她们那条线 / 临时话题"靠的是这个旗标，而不是前端自己拼线程 id 的前缀 ——
            # 那个形状（`s_proactive_<uid>_<role>`，B2 起带身份）的事实归 `core/reachout/inbox.py`，
            # 写第二处就会漂。
            "is_proactive": str(r["thread_id"]).startswith(PROACTIVE_THREAD_PREFIX),
            # "这一条里一个字的对话都没有"。只给布尔，**不给条数**：一轮对话在 `checkpoints`
            # 里是好几行（R26-07 那个平方级增长就是它），把行数当条数报出去就是骗界面；
            # 而要真条数得逐条线程回放（N 次 msgpack 反序列化），侧栏每次刷新都付一遍不值。
            # EXISTS 判的是"这条线写过东西没有"，正是界面要知道的那一件事。
            "is_blank": not bool(r["has_state"]),
        }
        for r in rows
    ]


def inflight_payload(thread_id: str) -> dict[str, object] | None:
    """这一条会话此刻在飞的那半句（`None` = 没人在生成）。

    只从进程内的登记读，不碰检查点：LangGraph 要到超步结束才写，而"她在打字"这件事
    恰好是那一段里唯一还活着的信息。键在不在登记里就是"在不在飞"，所以一个字都还没有时
    它也是 dict（`text=""`），界面上那格因此能立刻显出"她在说"而不是空着。
    """
    text = inflight_text(thread_id)
    return None if text is None else {"text": text}


@router.get("/api/session/{thread_id}/turn")
def get_session_turn(thread_id: str, ctx: AppContext = Depends(get_context)) -> dict[str, object]:
    """ "这一条此刻有没有人在说、说到哪儿了"——**只查进程内登记，不碰检查点**。

    为什么单独一个端点而不是让界面多打几次 `/messages?limit=1`：那一路每次都要
    `graph.get_state()` 把整份检查点快照反序列化回来（一条长会话的快照实测按 MB 计），
    把它当 1 秒一拍的探针用，等于为了问一句"她在吗"每次付一遍全量读的代价。
    这里查的是一次字典查找，跟停旗同一份进程内状态（同一个"这台上后端只有一个进程"的前提）。

    代价是它**看不见已经落地的东西**：没有 `total`，所以主动开口、编辑、删除、别的窗口
    跑完的那一轮全都问不出来 —— 那些还是得靠 `/messages` 那一拍。两个端点是**分工**不是重复：
    这里管"她在说"（要把发现延迟压到 1 秒），`/messages` 管"说完了什么"（5 秒一拍足够）。
    """
    get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    return {"inflight": inflight_payload(thread_id)}


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
    get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
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
        # 这一条会话此刻有没有"正在生成、还没进检查点"的那一句。`None` = 没有；
        # 有则是 `{"text": 已经投送出去的那段}`，一个字都还没有时是 `{"text": ""}`。
        # 为什么读它而不是把在飞的字提前写进历史：LangGraph 每个**超步**才落一次检查点，
        # 助手整句要等 `call_model` 返回才算一条消息 —— 副本实测那一轮里第二读者要空等
        # 7.6 秒（见 `core/thread_locks.py` 那节的数）。而这几个字是**已经过守卫投送**的，
        # 给第二个读者看它不绕过任何 fail-closed 纪律；写进 checkpoint 才是（那会造出
        # 一条"半句的历史"，停止生成与提取都会被它骗）。
        # 它随 `?limit=1` 那个探针一起回，所以对话界面不用多打一次请求就能知道"她在打字"。
        "inflight": inflight_payload(thread_id),
    }


class PromptEnhanceBody(BaseModel):
    # 与 ChatMessage.message 同一个上限：增强提示同样会进模型调用与轨迹，不给上限
    # 就等于允许一次请求把超大文本塞进 checkpoint（审查报告 P2）。
    text: str = Field(min_length=1, max_length=8000)


@router.post("/api/prompt/enhance")
def enhance_prompt(body: PromptEnhanceBody, ctx: AppContext = Depends(get_context)) -> object:
    """增强提示词：把草稿改写得更清晰具体（用户 2026-09-17 提出，照紧凑 IDE 的输入区观感）。

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
    get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    snapshot = ctx.app_state["graph"].get_state({"configurable": {"thread_id": thread_id}})
    values = snapshot.values or {}
    return {
        "trimmed": int(values.get("context_trimmed") or 0),
        "kept": int(values.get("context_kept") or 0),
        "budget": ctx.settings.context_max_chars,
    }


@router.delete("/api/session/{thread_id}", status_code=204)
def delete_session(thread_id: str, ctx: AppContext = Depends(get_context)) -> None:
    """删除会话：thread 行与**全部载体表**一并清掉，不留孤儿（`R102-26`）。

    名单与删除收在 `storage/threads.delete_thread_everywhere` 一处、现数现用，经
    `session_service.delete_everywhere` 持锁调用 —— 从前这里写死 `("checkpoints",
    "writes")` 两张表，`command_approval` 恰好漏掉，已删会话的待批审批就这么永远挂在
    队列上（点它是对一条不存在的会话做决定）。
    """
    get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    session_service.delete_everywhere(ctx.conn, thread_id)


@router.post("/api/session/{thread_id}/upload", status_code=201)
def upload_report(
    thread_id: str, file: UploadFile, ctx: AppContext = Depends(get_context)
) -> object:
    """US-7 上传入口：归属校验 → service 落盘/登记/解析/建索引 → 说明插回会话。

    **刻意声明为同步 `def`**：本端的重活（OCR 子进程最长 120 秒、嵌入、落盘）全是
    **阻塞式**调用。若写成 `async def`，它们会跑在事件循环里 —— 上传一张图片的几十秒
    内，整个进程（含其他会话的 SSE 对话）都不再响应。同步 `def` 让 FastAPI 把它丢进
    线程池，事件循环只负责调度。同理传给 service 的是 `file.file`（原样字节流），
    不 `await file.read()`。

    路由在这一层只做三件事：
      * 归属校验（不是你的会话 → 404，`get_thread` 统一判）；
      * 异常映射（超限/空文件 → 400；登记了但读不出来 → 500）；
      * 把 service 给的那句说明插回**这条会话**的检查点 —— 注入与用户这一轮写的是
        同一份，必须持写锁（R28-03）。

    落盘幂等、文件名消毒、OCR 编排、intake 状态机推进全在 `core/upload_service`，
    判据见那边的模块文档（先 spill 再幂等、OcrUnavailable≠ParseError 等四条）。
    """
    thread = get_thread(ctx.conn, thread_id, user_id=ctx.current_user())
    outcome = upload_service.ingest_upload(
        reader=file.file,
        filename=file.filename or "report.bin",
        thread_id=thread_id,
        user_id=str(thread["user_id"]),
        upload_dir=ctx.settings.upload_dir,
        ingestion=ctx.ingestion,
        knowledge=ctx.knowledge,
        knowledge_scope=ctx.health.knowledge_scope,
        ocr_candidates=ctx.ocr_candidates,
        tracer=ctx.tracer,
    )

    with thread_write(thread_id, timeout=session_service.WRITE_WAIT):
        ctx.app_state["graph"].update_state(
            {"configurable": {"thread_id": thread_id}},
            {"messages": [HumanMessage(content=outcome.note)]},
        )
    return outcome.response()


__all__ = ["router"]
