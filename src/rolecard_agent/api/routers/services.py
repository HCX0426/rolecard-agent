"""「服务」子页签的路由：运行时状态视图 + 端点增删改 + 优先级/启停 + 深度检测。

模型推理类的**编辑**在模型页签（后端 CRUD + 回退链）；这里管理 OCR / 嵌入 / 重排的
端点实例（`service_endpoint` 表）：云端行可增删改（各自 base_url/api_key/model，多账号
多厂商并存），本地实现行 builtin 不可删但可停用。优先级 = 行序，第 1 位即生效 —— 没有独立的
"首选"字段。保存即热生效：嵌入/重排经 ctx.rebuild_runtime 重建 KnowledgeBase；OCR 在每次
上传时实时读行序。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core.model_settings import client_style

router = APIRouter()


class EndpointCreateBody(BaseModel):
    """新增云端端点：名称 + 连接配置。id 省略时后端自动生成（`{key}-N`）。"""

    label: str = Field(min_length=1, max_length=64)
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    id: str | None = Field(default=None, max_length=32)


class EndpointPatchBody(BaseModel):
    """编辑端点。api_key 语义与 model_backend 一致：None=保留、""=清除、非空=设置。"""

    label: str | None = Field(default=None, max_length=64)
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    enabled: bool | None = None


class ReorderBody(BaseModel):
    """全量优先级：该服务全部端点 id 的一个排列，第 1 位即生效。"""

    order: list[str] = Field(min_length=1)


def _endpoint_dict(e: object) -> dict[str, object]:
    """行对象 → API 形状（api_key 永不回传，只回掩码）。"""
    from rolecard_agent.core.services import _mask_key

    return {
        "id": e.id,  # type: ignore[attr-defined]
        "label": e.label,  # type: ignore[attr-defined]
        "kind": e.kind,  # type: ignore[attr-defined]
        "base_url": e.base_url,  # type: ignore[attr-defined]
        "model": e.model,  # type: ignore[attr-defined]
        "enabled": e.enabled,  # type: ignore[attr-defined]
        "builtin": e.builtin,  # type: ignore[attr-defined]
        "key_masked": _mask_key(e.api_key),  # type: ignore[attr-defined]
    }


def _rebuild_if_runtime_affected(ctx: AppContext, key: str) -> None:
    """嵌入/重排的实例在 KnowledgeBase 构造时注入，行变更必须热重建；OCR 每次上传实时读。"""
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
    """新增一个云端端点实例（同类服务可并存多个账号/厂商，按优先级依次兜底）。"""
    try:
        e = ctx.services.add(
            key,
            label=body.label,
            base_url=body.base_url,
            api_key=body.api_key,
            model=body.model,
            eid=body.id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="add_service_endpoint",
        target=f"{key}/{e.id}",  # type: ignore[attr-defined]
        detail={"label": e.label},  # type: ignore[attr-defined]
    )
    _rebuild_if_runtime_affected(ctx, key)
    return _endpoint_dict(e)


@router.patch("/api/services/{key}/endpoints/{eid}")
def patch_service_endpoint(
    key: str,
    eid: str,
    body: EndpointPatchBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """编辑端点（云端行的连接配置 / 任意行的启停）。写审计，嵌入/重排热生效。"""
    try:
        e = ctx.services.patch(
            key,
            eid,
            label=body.label,
            base_url=body.base_url,
            api_key=body.api_key,
            model=body.model,
            enabled=body.enabled,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_service_endpoint",
        target=f"{key}/{eid}",
        # 审计绝不落密钥：api_key 一律以"是否提供"代替原值（审计页对浏览器可见）。
        detail={
            "fields": sorted(
                k for k, v in body.model_dump(exclude_none=True, exclude_unset=True).items()
                if k != "api_key"
            )
        },
    )
    _rebuild_if_runtime_affected(ctx, key)
    return _endpoint_dict(e)


@router.delete("/api/services/{key}/endpoints/{eid}", status_code=204)
def delete_service_endpoint(
    key: str,
    eid: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    """删除云端端点（builtin 本地实现不可删 —— 删了就没有本地兜底了）。"""
    try:
        ctx.services.delete(key, eid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(actor=actor.id, action="delete_service_endpoint", target=f"{key}/{eid}")
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
