"""本地推理服务的可见性与显存控制（架构审计报告 §11.1 的「状态 / 常驻 / 卸载」三档）。

为什么值得做成界面功能：默认模型以 `keep_alive=-1` 常驻（`expires_at` 落在几百年后），
**它自己永远不会让出显存**。8 GB 卡上那 5.8 GB 就是"电脑突然不能干活"的全部原因，而用户
要看一眼状态、按一下卸载，现在只能敲 curl。这一张卡把那个动作交回用户手里。

为什么这里也是"常驻/预热"的家（原先在 `routers/settings.py`）：常驻与卸载是**同一个资源
的相反两面**，分居两个端点就各自维护一份"什么在显存里"的判断——那正是审计报告反复抓的
"两处事实面不同步"。合并后状态只有一份读法，而且 `pin` 和 `unload` 一样能影响主机，
必须吃同一道回环门禁（原先它挂在设置路由上，公网部署时任何人都能让我们加载 5.8 GB）。

`停止服务 / 启动服务` 两档**不在这一层**：那是"起停本机进程"，进程归属（谁 spawn 的）与重启路径
是桌面壳的职责，所以落在壳侧 `startOllama()` / `stopOllama()`（2026-09-19 已实现并实机验过，
且只停本壳自己 spawn 的那个 —— 见架构审计 §11.1）。HTTP 层只提供"释放显存"那一档原语。

⚠️ 安全边界：这几个端点能影响主机，所以**只接受回环来源调用** —— 只看 TCP 对端地址，不采信任何
转发头，也**不随 `AUTH_MODE` 放开**。（`AUTH_MODE=off` 是默认值，如果只挂普通鉴权依赖，
等于所有人都能调它。）这一条是**终态而不是待办**：P0-3 的 operator 分族（2026-09-21 闭环）
自己的第 3 条就是"本机来源永远不看角色"，而桌面壳走的正是 127.0.0.1 —— 给这里加角色判定
挡不住任何新攻击者（能回环打进来的人已经在机器上了），只会把主人自己的启停按钮锁掉。
详见架构审计 §11.1 的 2026-09-25 回访。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor, is_loopback
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.config import ModelBackend
from rolecard_agent.core.model_settings import client_style
from rolecard_agent.core.probes import (
    local_inference_base_url,
    ollama_keep,
    ollama_loaded,
    ollama_reachable,
    ollama_unload,
)

router = APIRouter()

#: `keep_alive=-1` 的指纹：Ollama 把 expires_at 推到极远的未来。年份过线就当"常驻"。
PINNED_YEAR = 2100


def require_loopback(request: Request) -> None:
    """主机影响类端点的门禁。只认 TCP 对端，不看 `X-Forwarded-For`（那是客户端可自由
    设置的头，采信它等于给回环判定开后门 —— 与 `auth.auto` 档当年的漏洞同源）。"""
    peer = request.client.host if request.client else ""
    if not is_loopback(peer):
        raise HTTPException(
            status_code=403,
            detail="Ollama 的控制端点只允许本机（回环）调用。",
        )


def _default_backend(ctx: AppContext) -> ModelBackend | None:
    """默认对话后端（叠加运行环境覆盖后的有效配置）；没配默认 → None。

    状态端点容许"没有默认后端"这一中间态（服务照样可能在跑，用户照样可能要卸载它），
    所以它不能因为配置不全就 400 —— 那会把"看一眼状态"变成"先去设置页修配置"。
    """
    try:
        return ctx.settings.backend(None)
    except KeyError:
        return None


def _resident_rows(models: list[dict[str, object]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for m in models:
        expires = str(m.get("expires_at") or "")
        size = m.get("size")
        rows.append(
            {
                "name": m.get("name"),
                # /api/ps 的 size 是字节数（JSON 里可能是 int 或 None）；不是数字就当 0，
                # 而不是让一次上游字段形状变化把整张状态卡打成 500。
                "size_bytes": int(size) if isinstance(size, int | float) else 0,
                "expires_at": expires or None,
                # 常驻（不会自动卸载）与"用完 5 分钟后自己退出"是两回事，界面上必须分开，
                # 否则用户以为等着就行，而它等的正是永远不会来的那次释放。
                "pinned": expires[:4].isdigit() and int(expires[:4]) >= PINNED_YEAR,
            }
        )
    return rows


@router.get("/api/local-service", dependencies=[Depends(require_loopback)])
def local_service_status(ctx: AppContext = Depends(get_context)) -> object:
    """本地推理服务的状态：在不在跑、驻留了哪些模型、占多少显存、默认模型能不能常驻。

    只读，但同样限回环 —— 状态里带着端口与模型名，公网部署时那也是侦察信息。
    """
    base = local_inference_base_url(ctx.settings)
    rows = _resident_rows(ollama_loaded(base))
    backend = _default_backend(ctx)
    return {
        "base_url": base,
        # 区分三种状态：没在跑 / 在跑但没驻留模型 / 在跑且驻留。前两种的可操作动作不同。
        # 有驻留模型就说明服务一定在跑，不必再花 3s 探一次。
        "running": bool(rows) or ollama_reachable(base),
        "resident": rows,
        "resident_bytes": sum(int(r["size_bytes"]) for r in rows),
        "pinned": any(bool(r["pinned"]) for r in rows),
        # is_local / model 只服务"预热/常驻"这一档：云端后端没有 keep_alive 可言。
        "is_local": backend is not None and client_style(backend.provider) == "native",
        "model": backend.model if backend is not None else None,
    }


class PinRequest(BaseModel):
    """`keep_alive` = Ollama 原生秒数，-1 = 永不自动卸载（常驻）。`model` 省略 = 默认模型。"""

    keep_alive: int = -1
    model: str | None = Field(default=None, max_length=200)


@router.post("/api/local-service/pin", dependencies=[Depends(require_loopback)])
def pin_local_model(
    body: PinRequest,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """把默认（或指定）模型载入显存并按 keep_alive 常驻（默认 -1 = 不再 5 分钟自动卸载）。

    `num_ctx` 必须随请求一起下发：Ollama 在**加载时**定死上下文窗口，缺省回落到 4096，
    那会让预热反而多制造一次冷加载（`core/probes.ollama_keep`）。失败按 502 如实报，
    不返回 200 说"已常驻"——加载失败多半就是显存不够，而"显存不够"正是用户此刻的问题。
    """
    backend = _default_backend(ctx)
    if backend is None:
        raise HTTPException(
            status_code=400,
            detail="还没配置默认的模型 —— 请先在「服务」页把对话优先级的第一位选好。",
        )
    if client_style(backend.provider) != "native":
        raise HTTPException(
            status_code=400,
            detail="只有本地 Ollama 的模型支持常驻；云端没有 keep_alive。",
        )
    base = local_inference_base_url(ctx.settings)
    model = (body.model or backend.model).strip()
    if not ollama_keep(base, model, body.keep_alive, num_ctx=backend.num_ctx):
        raise HTTPException(
            status_code=502,
            detail=(
                f"常驻失败：连不上本机 Ollama（{base}），"
                f"或模型 {model!r} 加载不了（多为显存不足）。"
            ),
        )
    ctx.roles.audit(
        actor=actor.id,
        action="local_service_pin",
        target=model,
        detail={"keep_alive": body.keep_alive, "num_ctx": backend.num_ctx},
    )
    return {"model": model, "resident": _resident_rows(ollama_loaded(base))}


class UnloadRequest(BaseModel):
    """`model` 省略 = 卸掉当前驻留的全部模型（用户要的通常就是这个：把显存还回来）。"""

    model: str | None = Field(default=None, max_length=200)


@router.post("/api/local-service/unload", dependencies=[Depends(require_loopback)])
def unload_local_model(
    body: UnloadRequest,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """把模型从显存里卸载（`keep_alive=0`）。失败如实 502，不假装释放了。"""
    base = local_inference_base_url(ctx.settings)
    if body.model:
        targets = [str(body.model)]
    else:
        targets = [str(r["name"]) for r in _resident_rows(ollama_loaded(base))]
    if not targets:
        return {"unloaded": [], "detail": "本地没有驻留的模型，无需释放。"}
    done = [m for m in targets if ollama_unload(base, m)]
    if not done:
        raise HTTPException(
            status_code=502,
            detail=f"释放失败：连不上本机 Ollama（{base}），或模型 {targets[0]!r} 卸不掉。",
        )
    # 审计只记"释放了哪个模型"，不记任何对话内容（与全局审计纪律一致）。
    ctx.roles.audit(
        actor=actor.id,
        action="local_service_unload",
        target=base,
        detail={"models": done},
    )
    return {"unloaded": done, "skipped": [m for m in targets if m not in done]}


__all__ = ["router"]
