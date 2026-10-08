"""接入层的共享上下文与依赖 —— 把 `create_app` 大闭包里的东西变成可注入的。

拆 `main.py` 的真正难点不是"把函数搬走"，而是那 24 个端点共享的闭包状态（连接、服务、
图句柄、热重建函数）。把它们收进一个 `AppContext`，由依赖注入传递，路由模块就不再需要
闭包，**每个 router 都能独立读、独立测**。

`AppContext` 里的服务持有的是 `ThreadLocalConnection`（storage/db.py）：对外表现为一条
连接，内部按线程分发 —— 所以跨 router 共享同一个实例是安全的。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request

from rolecard_agent.api.auth import Actor
from rolecard_agent.base.audit import AuditTrail
from rolecard_agent.base.identity import resolve_identity
from rolecard_agent.base.observability import Tracer
from rolecard_agent.config import Settings
from rolecard_agent.core.bootstrap import Runtime
from rolecard_agent.core.common.approvals import ApprovalService
from rolecard_agent.core.domain_service import DomainQueryService
from rolecard_agent.core.ingest.ingestion import IngestionService
from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.services import ServiceEndpointService
from rolecard_agent.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from rolecard_agent.rag.ocr import OcrBackend
from rolecard_agent.core.session_service import get_row
from rolecard_agent.rag.retriever import KnowledgeBase
from rolecard_agent.roles.service import (
    RoleCards,
    RoleCardService,
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
    row = get_row(conn, thread_id)
    if row is None or str(row["user_id"]) != user_id:
        raise HTTPException(status_code=404, detail=f"对话不存在：{thread_id}")
    return row


@dataclass(slots=True)
class AppContext:
    """端点注入用的应用上下文 —— **内核 `Runtime` 的读穿视图**。

    以前它是 13 个字段的容器，其中 `settings` / `knowledge` / `registry` 三件在热重建时被
    逐个显式换新（`ctx.settings = eff` …）。那就是同一份可变量存在两处，漏换任何一个字段
    就是一个"改了不生效"的接缝。装配根下沉到 `core/bootstrap.py` 之后可变量只剩一份，
    这里全部改成属性读穿，端点侧 `ctx.roles` / `ctx.knowledge` 的写法一字不变。

    `settings` 读到的是**有效配置**（模型页 DB 配置 ⊕ 运行环境覆盖），不是裸 env 快照：
    OCR / 抽取 / 比对在请求时读它，「运行环境」页签保存后要立刻生效。
    **api 层 import 具体域的接缝已经清零**（2026-10-04 域机制收口，快照 P1-5）：宿主域接线
    改读各域 `SPEC`（`main.py`），域专属路由改由 `DomainSpec.router_contrib` 交回宿主挂载
    （原 `routers/records.py` 搬进 `domains/health/records.py`）。纪律在 `core/` 那半边由
    `core no domain token` 管，在 api 这半边由 `api domain seams` 管 —— 后者的登记名单**现在
    是空的**，谁再往 api 里 import 一个具体域就红。

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
            self._user_id = resolve_identity(self.conn, known, fallback=self.runtime.identity)
        return self._user_id

    @property
    def settings(self) -> Settings:
        """**实例主人**那份有效配置：喂知识库、工具闭包与那些设备级的读法。

        它**不是**"这一轮花谁的 key"那个答案 —— 那个问题由
        `Runtime.effective_for(本轮主人)` 回答，图在节点内部现取（`core.nodes.turn_settings`，
        M2d 尾巴的收口）。两者在单机形态下是同一份，所以这里一切照旧；差别只在同一个库上
        住了两个身份、各配了自己的 provider 时才显出来。
        """
        return self.runtime.effective

    @property
    def conn(self) -> ThreadLocalConnection:
        return self.runtime.conn

    @property
    def roles(self) -> RoleCardService:
        return self.runtime.roles

    @property
    def audit(self) -> AuditTrail:
        """审计咽喉（`R102-07`）：写审计不再借 `ctx.roles.audit` —— 那条链上没有一件是角色卡。"""
        return self.runtime.audit

    @property
    def role_cards(self) -> RoleCards:
        """这次请求的主人眼里的那些角色卡 —— 路由碰 `role_card` 的**唯一**写法。

        为什么要给视图单独开一个属性：`ctx.roles.get(...)` 长得和"能读"一样，只有
        `ctx.role_cards.get(...)` 在拼写上就逼你回答"以谁的身份"。少一种写法就少一种忘法。
        """
        return self.roles.scoped(self.current_user())

    @property
    def plugins(self) -> PluginService:
        return self.runtime.plugins

    @property
    def ingestion(self) -> IngestionService:
        return self.runtime.ingestion

    @property
    def health(self) -> DomainQueryService:
        """health 域的查询服务（按域 id 取，见 `Runtime.query_service`）。

        属性名保留 `health`：端点侧写的是"我在用哪个域"，这一层点名是**刻意的**（路由本来
        就为这个域服务）。不认识的域 id 会 loud 报错，不会静默拿到别域的服务。
        """
        return self.runtime.query_service("health")

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
