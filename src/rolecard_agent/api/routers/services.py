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
from rolecard_agent.api.deps import AppContext, get_actor, get_context, value_error_to_http
from rolecard_agent.core import outbound
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


def _row_or_404(ctx: AppContext, key: str, eid: str) -> object:
    """回写后的那一行。用 `next(..., None)` + 显式 404 —— 旧写法是裸 `next()`，
    未命中会抛 `StopIteration`，被 FastAPI 兜成 500 而不是可读的 404（审查报告 L2）。"""
    for e in ctx.services.rows(key):
        if e.id == eid:
            return e.to_api()
    raise HTTPException(status_code=404, detail=f"服务 {key} 下不存在端点 {eid!r}")


@router.get("/api/services")
def list_services(ctx: AppContext = Depends(get_context)) -> object:
    """运行时状态视图（轻检测：配置齐缺 + 本地探活，不发外部请求）。

    能力三类（ocr/embedding/rerank）是本机主人的（设备级）；模型推理序列按**请求的主人**
    过滤 chat 引用（多租户 B1b，方案 A）—— 各看各的对话默认。
    """
    from rolecard_agent.core.services import service_status_view

    return service_status_view(ctx.conn, ctx.settings, user_id=ctx.current_user())


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
        raise value_error_to_http(exc) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="add_service_endpoint",
        target=f"{key}/{body.ref_backend}",
        detail={"ref_backend": body.ref_backend},
    )
    _rebuild_if_runtime_affected(ctx, key)
    return _row_or_404(ctx, key, body.ref_backend)


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
        raise value_error_to_http(exc) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_service_endpoint",
        target=f"{key}/{eid}",
        detail={"enabled": body.enabled},
    )
    _rebuild_if_runtime_affected(ctx, key)
    return _row_or_404(ctx, key, eid)


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
        raise value_error_to_http(exc) from exc
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
    """全量写优先级（第 1 位生效）。嵌入/重排需重建 KnowledgeBase —— 复用热重建通道。

    `key == "models"` 是特例：这一条序列**就是**"哪些模型用于对话"的事实面（拆层后不再
    有 model_backend.usage 列），第 1 位 = 对话默认、其后 = 回退顺序（运行时截到
    `MAX_FALLBACKS` 级）。候选是模型页的全部行，不是"已经用于对话的行"—— 否则第一次
    把某个模型拖进对话就没有入口。改动后热重建默认模型与角色级模型缓存。
    """
    if key == "models":
        # 候选与校验都按**这次请求的主人**那一族后端（M2d）：这一页下面列出的就是这些名字，
        # 换一个集合去校验会出现"界面上有的，保存时说不在配置里"。
        # 写进去的序列也归这个人（多租户 B1b，方案 A 收了 §4.1 的尾巴）：chat 引用行按人，
        # A 存对话序列不会抹掉 B 的，B 的默认/回退各看各的。
        owner = ctx.current_user()
        known = {
            str(row["name"]) for row in ctx.model_settings.list_backends(user_id=owner)
        }
        unknown = [n for n in body.order if n not in known]
        if not body.order:
            raise HTTPException(status_code=400, detail="优先级列表不能为空。")
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"以下后端不在模型页配置里（或已被删除）：{', '.join(unknown[:3])}",
            )
        if len(set(body.order)) != len(body.order):
            raise HTTPException(status_code=400, detail="优先级列表出现了重复的后端名。")
        try:
            ctx.model_settings.save_chat_pool(body.order, user_id=owner)
        except Exception as exc:  # noqa: BLE001 - 服务层异常转可读 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.roles.audit(
            actor=actor.id,
            action="update_service_order",
            target=key,
            detail={"order": body.order},
        )
        ctx.rebuild_runtime()  # 默认模型与角色级模型缓存都随优先级变化
        return {"service_key": key, "order": body.order, "reloaded": True}

    try:
        ctx.services.reorder(key, body.order)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知服务类别：{key}") from None
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
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
    try:
        backend = ctx.app_state["effective"].backend(None)
    except KeyError as exc:
        # 默认后端名不在后端集里 = 配置错误。给可读的 400，而不是裸 KeyError → 500。
        raise HTTPException(
            status_code=400,
            detail=f"默认模型后端未配置：{exc}。请在「模型」页签检查默认后端。",
        ) from exc
    base = (backend.base_url or "http://localhost:11434").rstrip("/")
    # provider 是供应商 id；native 风格（Ollama 及别名）探 /api/tags，openai 兼容探 /models。
    is_ollama = client_style(backend.provider) == "native"
    probe_path = "/api/tags" if is_ollama else "/models"
    try:
        headers = (
            {"Authorization": f"Bearer {backend.api_key}"} if backend.api_key else {}
        )
        # 走 `core/outbound`：这一发可能带着**已存的 api_key**，且探的常是本机 Ollama。
        res = outbound.get(f"{base}{probe_path}", headers=headers, timeout=8.0)
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
    # M4：键名按 provider 语义返回（ollama / openai_compatible），不再一律叫 `ollama`，
    # 前端可按 provider 取对应字段，避免错取。
    probe_key = "ollama" if is_ollama else "openai_compatible"
    return {probe_key: ollama}


__all__ = ["router"]
