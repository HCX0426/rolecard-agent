"""「服务」子页签的路由：运行时状态视图 + 策略保存 + 深度检测。

模型推理类的**编辑**在模型页签（后端 CRUD + 回退链）；这里只做状态展示。OCR / 嵌入 /
重排三类的优先级与启停在这里调整，保存即热生效（嵌入/重排经 ctx.rebuild_runtime 重建
KnowledgeBase；OCR 在每次上传时实时读策略）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core.services import category

router = APIRouter()


class ServicePolicyBody(BaseModel):
    """一类服务的策略：首选候选 + 禁用列表（JSON 数组的候选 id）。"""

    preferred: str = Field(min_length=1, max_length=64)
    disabled: list[str] = Field(default_factory=list)


@router.get("/api/services")
def list_services(ctx: AppContext = Depends(get_context)) -> object:
    """运行时状态视图（轻检测：配置齐缺 + 本地探活，不发外部请求）。"""
    from rolecard_agent.core.services import service_status_view

    return service_status_view(ctx.conn, ctx.settings)


@router.put("/api/services/{key}")
def put_service_policy(
    key: str,
    body: ServicePolicyBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """保存一类服务的策略并热生效。非法候选 / 全禁用一律 400（配置错误要大声）。"""
    try:
        cat = category(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    try:
        ctx.services.save(key, preferred=body.preferred, disabled=body.disabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_service_policy",
        target=key,
        detail={"preferred": body.preferred, "disabled": sorted(body.disabled)},
    )
    # 嵌入/重排的策略变更需要重建 KnowledgeBase（嵌入器/重排器在构造时注入）；
    # 复用模型设置的热重建通道 —— rebuild_runtime 会连图一起按最新策略重造。
    ctx.rebuild_runtime()
    return {
        "service_key": key,
        "title": cat.title,
        "preferred": body.preferred,
        "disabled": sorted(body.disabled),
        "reloaded": True,
    }


@router.post("/api/services/check")
def deep_check(ctx: AppContext = Depends(get_context)) -> object:
    """深度检测：对**当前默认后端**真发一次轻请求（ollama → /api/tags；openai 兼容 → /models）。

    嵌入 / OCR 云端的真实连通性**刻意不在这里自动发起** —— 那会向第三方发请求并消耗
    配额；页面的轻检测（配置齐缺 + 本地探活）已覆盖绝大多数排查场景。
    """
    ollama: dict[str, object] = {"reachable": False, "detail": ""}
    backend = ctx.app_state["effective"].backend(None)
    base = (backend.base_url or "http://localhost:11434").rstrip("/")
    is_ollama = (backend.provider or "").lower() == "ollama"
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
