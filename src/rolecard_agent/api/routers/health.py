"""健康深探与指标端点。

这一格与 `/api/health` 的关系写在 `core/deep_health` 的模块注释里：那条免鉴权、秒级、
给编排器；这里这条**要鉴权**、慢、给人排障用。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from rolecard_agent.api.auth import ROLE_OPERATOR, Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.base.metrics import COUNTS
from rolecard_agent.base.observability import logline
from rolecard_agent.core.probes.readiness import readiness_report

router = APIRouter()


def require_operator(actor: Actor = Depends(get_actor)) -> Actor:
    """诊断类端点的门禁：只回操作员。

    判据与装配根算 `roles_in_effect` 那句**同一形状**（`actor.is_anonymous or
    actor.role == ROLE_OPERATOR`）：`AUTH_MODE=off` 的本地自用形态里没有凭据，对端是回环，
    actor 就是匿名的 —— 那里不该因为"没登录"而看不到自己的诊断。开了鉴权之后"匿名"不再
    等于本人，这条路自然收紧成 operator 专属。
    """
    if actor.is_anonymous or actor.role == ROLE_OPERATOR:
        return actor
    raise HTTPException(status_code=403, detail="诊断端点只对操作员开放。")


@router.get("/api/health/deep", dependencies=[Depends(require_operator)])
def health_deep(ctx: AppContext = Depends(get_context)) -> object:
    """真依赖开不开：sqlite `quick_check` / chroma 心跳 / 模型配置存在性。

    **红的时候回 503**：编排器与告警只看状态码就能判"起不来了"，表照样放在 body 里给人看
    （`status=degraded` 与逐项 detail）。`/api/health` 那条不受影响 —— 它答的是另一个问题
    （进程活着没有），这正是两格分开的理由：chroma 目录坏了，容器**不应该**因此被反复重启。
    """
    settings = ctx.settings
    backends: list[dict[str, object]] | None
    try:
        backends = ctx.model_settings.list_backends(user_id=ctx.current_user())
    except Exception as exc:  # noqa: BLE001 - 读不出来也是一种诊断结果，别把整张表带走
        logline("warning", "deep_health.models_unreadable", f"读模型配置失败：{exc}")
        backends = None
    report = readiness_report(
        sqlite_path=settings.sqlite_path,
        chroma_path=settings.chroma_path,
        model_backends=backends,
    )
    if report["status"] != "ok":
        return JSONResponse(status_code=503, content=report)
    return report


@router.get("/api/metrics", dependencies=[Depends(require_operator)])
def metrics() -> PlainTextResponse:
    """Prometheus 文本（`text/plain; version=0.0.4`）。

    与深探同一条门禁：它暴露的是**运行时形状**（哪些事件在发生、多频繁、有没有异常尖峰），
    而那对未鉴权的公网访客没有理由可见。格式与"为什么只有计数没有直方图"写在
    `base/metrics` 的模块注释里；`Content-Type` 里必须带版本号 —— Prometheus 认它。
    """
    return PlainTextResponse(
        COUNTS.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
    )
