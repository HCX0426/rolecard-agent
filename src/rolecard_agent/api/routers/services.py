"""「服务」子页签的路由：运行时状态视图 + 引用增删 + 优先级/启停 + 深度检测。

架构归一化（引用模型）：模型页（/api/settings/models）是云端端点配置的**唯一事实面**；
这里管理 OCR / 嵌入 / 重排的**引用**——新增 = 从模型页已配置的后端中选择，删除 = 仅把
该后端从本服务的优先级序列中摘除（绝不动模型页配置），配置的编辑只在模型页。
优先级 = 行序，第 1 位即生效。保存即热生效：嵌入/重排经 ctx.rebuild_runtime 重建
KnowledgeBase；OCR 在每次上传时实时读行序。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core.model_settings import client_style

router = APIRouter()


class EndpointCreateBody(BaseModel):
    """新增引用：从模型页已配置的后端中选择（不做任何配置复制）。"""

    ref_backend: str = Field(min_length=1, max_length=32)


class EndpointPatchBody(BaseModel):
    """启停一行。配置的编辑在模型页 —— 本路由只接受 enabled。"""

    enabled: bool


class ReorderBody(BaseModel):
    """全量优先级：该服务全部端点 id 的一个排列，第 1 位即生效。"""

    order: list[str] = Field(min_length=1)


def _rebuild_if_runtime_affected(ctx: AppContext, key: str) -> None:
    """嵌入/重排的实例在 KnowledgeBase 构造时注入，引用变更必须热重建；OCR 每次上传实时读。"""
    if key in ("embedding", "rerank"):
        ctx.rebuild_runtime()


@router.get("/api/services")
def list_services(ctx: AppContext = Depends(get_context)) -> object:
    """运行时状态视图（轻检测：配置齐缺 + 本地探活，不发外部请求）。"""
    from rolecard_agent.core.services import service_status_view

    return service_status_view(ctx.conn, ctx.settings)


@router.post("/api/services/{key}/endpoints", status_code=201)
def add_service_endpoint(
    key: str,
    body: EndpointCreateBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """新增一条引用（选择模型页已配置的后端；同类服务内一后端至多一条）。"""
    try:
        ctx.services.add(key, ref_backend=body.ref_backend)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="add_service_endpoint",
        target=f"{key}/{body.ref_backend}",
        detail={"ref_backend": body.ref_backend},
    )
    _rebuild_if_runtime_affected(ctx, key)
    row = next(e for e in ctx.services.rows(key) if e.id == body.ref_backend)
    return row.to_api()


@router.patch("/api/services/{key}/endpoints/{eid}")
def patch_service_endpoint(
    key: str,
    eid: str,
    body: EndpointPatchBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """启停一行（引用的连接配置在模型页编辑 —— 本路由只管参与与否）。写审计；嵌入/重排热生效。"""
    try:
        ctx.services.patch(key, eid, enabled=body.enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_service_endpoint",
        target=f"{key}/{eid}",
        detail={"enabled": body.enabled},
    )
    _rebuild_if_runtime_affected(ctx, key)
    row = next(e for e in ctx.services.rows(key) if e.id == eid)
    return row.to_api()


@router.delete("/api/services/{key}/endpoints/{eid}", status_code=204)
def delete_service_endpoint(
    key: str,
    eid: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    """移除一行：引用行只删引用（模型页配置不受影响）；本地实现行不可删。"""
    try:
        ctx.services.delete(key, eid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="remove_service_endpoint",
        target=f"{key}/{eid}",
        detail={"scope": "reference_only"},
    )
    _rebuild_if_runtime_affected(ctx, key)


@router.put("/api/services/{key}")
def put_service_order(
    key: str,
    body: ReorderBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """全量写优先级（第 1 位生效）。嵌入/重排需重建 KnowledgeBase —— 复用热重建通道。"""
    try:
        ctx.services.reorder(key, body.order)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_service_order",
        target=key,
        detail={"order": body.order},
    )
    _rebuild_if_runtime_affected(ctx, key)
    return {"service_key": key, "order": body.order, "reloaded": True}


@router.post("/api/services/check")
def deep_check(ctx: AppContext = Depends(get_context)) -> object:
    """深度检测：对**当前默认后端**真发一次轻请求（ollama → /api/tags；openai 兼容 → /models）。

    嵌入 / OCR 云端的真实连通性**刻意不在这里自动发起** —— 那会向第三方发请求并消耗
    配额；页面的轻检测（配置齐缺 + 本地探活）已覆盖绝大多数排查场景。
    """
    ollama: dict[str, object] = {"reachable": False, "detail": ""}
    backend = ctx.app_state["effective"].backend(None)
    base = (backend.base_url or "http://localhost:11434").rstrip("/")
    # provider 是供应商 id；native 风格（Ollama 及别名）探 /api/tags，openai 兼容探 /models。
    is_ollama = client_style(backend.provider) == "native"
    probe_path = "/api/tags" if is_ollama else "/models"
    try:
        import httpx

        headers = (
            {"Authorization": f"Bearer {backend.api_key}"} if backend.api_key else {}
        )
        res = httpx.get(f"{base}{probe_path}", headers=headers, timeout=8.0)
        ollama["reachable"] = res.status_code == 200
        ollama["detail"] = f"{res.status_code}"
        try:
            payload = res.json()
        except ValueError:
            payload = {}
        models: list[str] = []
        if is_ollama:
            models = [m.get("name") for m in payload.get("models", [])][:10]
        elif isinstance(payload.get("data"), list):
            models = [m.get("id") for m in payload["data"]][:10]
        if models:
            ollama["models"] = models
        if res.status_code != 200:
            ollama["detail"] = f"{res.status_code}（{backend.provider} · {probe_path}）"
    except Exception as exc:  # noqa: BLE001 - 探活失败本身就是检测结果
        ollama["detail"] = f"{type(exc).__name__}: {exc}"[:200]
    return {"ollama": ollama}


__all__ = ["router"]
