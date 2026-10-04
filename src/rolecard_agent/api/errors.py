"""错误响应的唯一出口：正文工厂 + 异常族→状态码注册表。

为什么单独立这一处（2026-10-04 快照的错误收口条目）：错误响应从前**裂成两半**——

  * 路由走 JSON 的 `{"detail": 中文}`，而认证 / 限流 / 操作员分级 / 来源护栏四个中间件
    走 `PlainTextResponse` 裸文本。前端 `request()` 只解析 JSON detail（读不到就退回
    `"403 Forbidden"` 这种状态行），于是精心写的中文文案**在中间件那半到不了用户**，
    `Retry-After` 也从不被读取；
  * 映射散落：异常→HTTP 的 helper 只有少数路由采用，settings / records 十几处同形
    catch 手写 `raise HTTPException(status=..., detail=str(exc))` —— 同一异常在不同
    路由可以得到不同状态码，而"该是几码"这件事没有任何一处声明过。

于是收两件东西进来：

  1. **`error_response(status, detail, headers)`** —— 唯一的错误正文工厂。中间件的四个
     返回点、`HTTPException`/各族 handler 全走它；头（401 的 `WWW-Authenticate`、
     429 的 `Retry-After`）原样带上 —— 换正文形状不换语义。
  2. **`register_error_handlers(app)`** —— 异常族→状态码的注册表，交 FastAPI 的
     exception_handler 机制执行（按异常类的 MRO 找 handler，具体族盖过泛族）。
     注册表里的族，路由的 try/except 整段删除：让异常自己走到这里，比"每个路由记得
     catch"可靠 —— 漏 catch 的代价曾是 500 空壳。

**刻意不注册裸 `ValueError`**：`pydantic.ValidationError` 是它的子类，全局翻译会把
框架/三方的内部 bug 也变成 400，把"我们写错了"伪装成"用户输入错了"（fail-closed 的
另一半是**不许把故障伪装成拒收**）。路由边界上的 `ValueError` / `KeyError` 是**显式
断言且带定制文案**的（"模型 X 不存在"），那不是映射是文案，留在原地 —— 它们 raise 的
`HTTPException` 同样经本模块的 handler 出去，正文仍然只有一处组装。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from rolecard_agent.core.approvals import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalUnauthorised,
)
from rolecard_agent.core.ingestion import IngestionNotFound
from rolecard_agent.core.model_settings import ModelSettingsError
from rolecard_agent.core.plugins import PluginError, UnknownPlugin
from rolecard_agent.core.thread_locks import ThreadBusy
from rolecard_agent.core.upload_service import UploadRejected, UploadUnreadable
from rolecard_agent.roles.service import (
    BuiltinRoleProtected,
    RoleAlreadyExists,
    RoleError,
    RoleNotFound,
)


def error_response(
    status: int, detail: Any, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """唯一的错误正文工厂：`{"detail": ...}` + 可选头。

    `detail` 收 `Any` 不是偷懒：FastAPI 自带的 422 校验错误就是**数组**形状的 detail，
    在这里字符串化会把前端 `readableDetail` 的分支整个骗过（它专门处理两种形状）。
    """
    return JSONResponse(
        status_code=status,
        content={"detail": detail},
        headers=dict(headers) if headers else None,
    )


#: 异常族 → 状态码。**一份声明，一个执行点**：漏 catch 的代价从"500 空壳"变成"正确的码"，
#: 同一异常在所有路由里的码也从此只有一处答案。
#: 第三列是"为什么是这个码"——码是判据不是习惯，改一个码要连理由一起改。
_FAMILIES: tuple[tuple[type[Exception], int, str], ...] = (
    # —— 角色卡族（与从前 `role_error_to_http` 的映射逐位一致，纯搬家）——
    (RoleNotFound, 404, "不存在（403 会把 id 面变成'猜中即存在'的清单，与 get_thread 同理）"),
    (RoleAlreadyExists, 409, "同名冲突，不是'没权限'"),
    (BuiltinRoleProtected, 409, "出厂卡不许删 —— 保护冲突，仍不是'不存在'"),
    (RoleError, 400, "族里其余：角色卡语义错（读方给的信息不合法）"),
    # —— 插件族（与 `plugin_error_to_http` 逐位一致）——
    (UnknownPlugin, 404, "插件 id 没登记过"),
    (PluginError, 400, "族里其余：启停/配置的语义错"),
    # —— 模型配置（settings 七处手写 catch 的合并，全部 400）——
    (ModelSettingsError, 400, "模型配置校验不过 —— 文案本来就是写给人看的"),
    # —— 审批族（approvals 三处手写 catch 的合并）——
    (ApprovalNotFound, 404, "这条审批不存在（猜自增 id 不该得到别的答案）"),
    (ApprovalAlreadyDecided, 400, "已经裁决过 —— 重复点击/并发裁决"),
    (
        ApprovalUnauthorised,
        403,
        "没持这条待批下发的令牌：403 而不是 401 —— 缺口不是'没登录'"
        "（单机形态本来就不登录），而是'没持有凭据'",
    ),
    # —— 摄取与上传 ——
    (IngestionNotFound, 404, "摄取任务不存在"),
    (UploadRejected, 400, "超限/空文件：什么都没登记，400 让用户改一改再传"),
    (UploadUnreadable, 500, "已登记但读不出来（台账已 failed）—— 要人来看，不是用户能修的"),
)


def register_error_handlers(app: FastAPI) -> None:
    """把注册表装到 app 上（`create_app` 里调一次）。

    `HTTPException` 也经 `error_response` 出去：FastAPI 默认 handler 拼的正文与我们
    同形，但**两处拼**就是两处真相 —— 而且默认 handler 不会经过本文件，401/429 从路由
    侧 raise 时带的头得靠这里透传（`exc.headers`）。
    """

    @app.exception_handler(HTTPException)
    async def _http_exception(_: Request, exc: HTTPException) -> JSONResponse:
        return error_response(exc.status_code, exc.detail, exc.headers)

    # 写检查点等不到会话锁 ⇒ 409 + 一句人话（六个写检查点的口子共用这一句，
    # 而不是各写各的 try —— 六种说法的下场在下面这些注册点里已经见过）。
    @app.exception_handler(ThreadBusy)
    async def _thread_busy(_: Request, exc: ThreadBusy) -> JSONResponse:
        return error_response(
            409, "这一轮还在跑 —— 先按「停止」或等它说完，再改这段历史。"
        )

    for family, status, _why in _FAMILIES:

        def _handler(_: Request, exc: Exception, *, _status: int = status) -> JSONResponse:
            return error_response(_status, str(exc))

        app.add_exception_handler(family, _handler)


def value_error_to_http(exc: ValueError) -> HTTPException:
    """路由边界的 `ValueError`（可读的用户输入/业务断言）→ 400。

    为什么不进注册表：见模块文档那句"不注册裸 ValueError"。它是**调用点自己 raise**
    的显式翻译（配定制文案时也先 raise 它），所以留在可 import 的这一处，而不是让每个
    路由重新拼 `HTTPException(400, ...)`。
    """
    return HTTPException(status_code=400, detail=str(exc))
