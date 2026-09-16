"""模型后端设置路由：读取（key 只写不回读）与保存（热重建）。

从 `main.py` 迁出的第 5 组（C1）。热重建通过 `ctx.rebuild_graph` —— 重建逻辑属于宿主
（create_app），router 只负责在保存成功后调用它。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.core.model_settings import (
    ModelSettingsError,
    is_keyless_provider,
    provider_catalog,
)

router = APIRouter()


class BackendSpec(BaseModel):
    """One model backend row from the settings page.

    `api_key` is write-only: omitted/None = keep the stored key for this name; "" = clear it.
    The GET endpoint never returns keys, so this round-trip rule is what keeps saves from
    silently erasing them.
    """

    name: str = Field(min_length=1, max_length=32)
    provider: str = Field(min_length=1)
    base_url: str | None = None
    model: str = Field(min_length=1)
    api_key: str | None = None
    # 模型页是云端端点配置的唯一事实面：usage 标记该行服务谁（chat/embedding/rerank/ocr），
    # 服务页按用途引用。对话菜单与角色路由只消费 chat 行。
    usage: str = "chat"


class ModelSettingsBody(BaseModel):
    default: str
    backends: list[BackendSpec]
    fallbacks: list[str] = Field(default_factory=list)


@router.get("/api/settings/models")
def get_model_settings(ctx: AppContext = Depends(get_context)) -> object:
    """模型后端设置。api_key 永不回明文 —— 只有 has_key 标志 + 掩码预览。"""
    return {
        "default": ctx.model_settings.default_backend(),
        "backends": ctx.model_settings.list_backends(),
        "fallbacks": ctx.model_settings.list_fallbacks() or [],
    }


@router.get("/api/settings/model-providers")
def get_model_providers() -> object:
    """供应商目录（动态扩展）：设置页「模型」页签的下拉从这里取，不再写死前端。"""
    return {"providers": provider_catalog()}


@router.put("/api/settings/models")
def put_model_settings(
    body: ModelSettingsBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """保存后端集合并热重建（下一轮对话即用新后端，无需重启进程）。

    api_key 语义：缺省/None = 保留已存 key；空串 = 清除 —— 否则每次没重输 key 的
    保存都会把 key 抹掉。fallbacks = 失败自动回退链（≤2 级，按序尝试）。"""
    try:
        # 凭据校验前置：需要 key 的 provider（openai 类）没有 key 时，保存即拒绝 ——
        # 否则会存进一个"重建时才炸"的配置（实测：热重建抛 Missing credentials）。
        for b in body.backends:
            if is_keyless_provider(b.provider):
                continue
            if not (b.api_key or ctx.model_settings.stored_api_key(b.name)):
                raise ModelSettingsError(
                    f"后端 {b.name} 使用 {b.provider}，缺少 api_key（本地 Ollama 无需填写）。"
                )
        ctx.model_settings.save(
            default=body.default,
            backends=[b.model_dump() for b in body.backends],
            fallbacks=body.fallbacks,
        )
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 管理面变更必须留痕（H3）。审计只记**结构**（名称/用途/默认/回退链），绝不记 key。
    ctx.roles.audit(
        actor=actor.id,
        action="update_model_settings",
        target=body.default,
        detail={
            "backends": [
                {
                    "name": b.name,
                    "provider": b.provider,
                    "usage": b.usage,
                    "key_changed": b.api_key is not None,
                }
                for b in body.backends
            ],
            "fallbacks": body.fallbacks,
        },
    )
    try:
        ctx.rebuild_runtime()
    except Exception as exc:  # noqa: BLE001 - 构建失败要给出可读原因，而不是 500 空壳
        raise HTTPException(status_code=500, detail=f"模型后端构建失败：{exc}") from exc
    return {
        "default": ctx.model_settings.default_backend(),
        "backends": ctx.model_settings.list_backends(),
        "fallbacks": ctx.model_settings.list_fallbacks() or [],
    }


__all__ = ["router"]
