"""领域数据路由（通用）：非首个富域的结构化数据增删改查。

首个富域有自己 richer 的报告/指标模型，走 `/api/records` 那一族 —— 那族端点在 2026-10-04
域机制收口后**住在它自己的域包里**（由 `DomainSpec.router_contrib` 交回宿主挂载），不在此
重复。本路由只负责**通用领域数据**——任意一个已注册/启用的域都可以在「数据」页挂上自己的
简单记录（标签 + 数值/文本 + 单位 + 备注），无需再写一套领域服务。这让「数据」页真正多领域
化：页签按 `/api/plugins` 遍历，有专属路由的域用原视图，其它域用这里的通用视图。

H4：路由只做"参数映射 + 异常→HTTP 映射"，所有 SQL 在 `core/domain/domain_data.py`。异常语义：
**找不到 = KeyError → 404**，**规则不允许 = ValueError → 400**。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import (
    AppContext,
    get_actor,
    get_context,
)
from rolecard_agent.api.errors import value_error_to_http
from rolecard_agent.contracts.handwritten_models import DomainRecordView
from rolecard_agent.contracts.response_models import DomainsView
from rolecard_agent.core.domain.domain_data import DomainDataService

router = APIRouter()


class DomainCreate(BaseModel):
    """新建一个域的请求体。id 走通用记录通路（纯数据域的形状），display_name 缺省同 id。"""

    id: str = Field(min_length=1, max_length=32)
    display_name: str | None = Field(default=None, max_length=64)


class DomainRecordCreate(BaseModel):
    """一条通用领域记录（最小可用：标签 + 数值或文本）。"""

    label: str = Field(min_length=1, max_length=100)
    value_text: str | None = None
    value_num: float | None = None
    unit: str | None = None
    note: str | None = None


class DomainRecordPatch(BaseModel):
    """通用记录的修正：每个字段都可单独更新（未给 = 不改）。"""

    label: str | None = None
    value_text: str | None = None
    value_num: float | None = None
    unit: str | None = None
    note: str | None = None


def _domain_service(ctx: AppContext) -> DomainDataService:
    return DomainDataService(ctx.conn)


def _require_live_domain(ctx: AppContext, domain: str) -> None:
    """通用记录通路的入口闸：这个域**在清单上活着**才允许读写它的记录。

    没有这道闸时，"删了域"只删了清单行与已有记录，端点照旧对一个已经不存在的名字服务
    —— 那正是本仓最忌讳的两套真相（界面里没有这个域，API 却还能往里写）。事实源是
    `list_plugins()`（与界面、`list_domains` 工具同一个投影），不是这里另建一份判断。
    已退役 => 404（"没有这个域"），与删除端点的 404 同一口径。
    """
    if domain not in {row["plugin_id"] for row in ctx.plugins.list_plugins()}:
        raise HTTPException(status_code=404, detail=f"域 {domain!r} 不存在或已删除")


def _domains_view(ctx: AppContext) -> list[object]:
    """`plugin` 表 → 域清单的投影（id + 展示名）。列表页与新建后的回读共用这一份。

    抽出来是因为 `list_domains` 与 `create_domain` 的返回体是同一个形状 —— 从前两处各写
    一遍字典推导，改投影要改两处（本仓最忌讳的重复）。事实源仍是 `list_plugins()`。
    返回类型写 `list[object]` 与两个调用点的声明一致：list 不变，`list[dict]` 顶不了它
    （内联时 mypy 拿返回上下文推断了，提成函数后这一格就得自己说清）。
    """
    return [
        {"id": row["plugin_id"], "display_name": row["display_name"]}
        for row in ctx.plugins.list_plugins()
    ]


@router.get(
    "/api/domains",
    response_model=DomainsView,
    response_model_exclude_unset=True,
)
def list_domains(ctx: AppContext = Depends(get_context)) -> list[object]:
    """已注册域的清单（id + 展示名）—— 资料页的域分组与「关于」面板的来源。

    2026-10-24 权限模型收口：域常开后清单就是 `plugin` 表的全部行（原 `/api/plugins`
    端点退役），"enabled" 字段随之失去意义，不再回传。用户在资料页删掉的域（retired=1）
    已被 `list_plugins` 滤掉，这里不再列出。
    """
    return _domains_view(ctx)


@router.post("/api/settings/domains", status_code=201, response_model=DomainsView)
def create_domain(
    body: DomainCreate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> list[object]:
    """用户新建一个**数据域**（资料页「＋ 新建域」）—— 把功能决定权交回用户。

    路径住 `/api/settings/domains` 而不是 `/api/domains`，是**访问档**的选择，不是分类洁癖：
    `/api/domains` 前缀整体归使用者（用户要在自己的域里增删记录），若新建/删除也挂在那儿，
    任一使用者凭据就能改写"这台机器有哪些能力域"的清单 —— 而域清单是操作员面
    （`domains/registry.py` 开头那条：运行时域集合绝不暴露给模型，自我扩权红线）。这与
    知识库"读侧 user、写侧 operator"（2026-10-30 安全审计 #2）是同一条纪律的两半。

    id 形状由 `DomainDataService.check_domain` 把关（与通用记录通路同一条正则，不在这里抄
    第二份）；撞已存在（且未退役）的名 → 409（显式新建撞名必须报错，不像播种那样静默跳过；
    撞一个已退役的名由 `create` 就地复活那一格兜住，见下）。新建的是纯数据域：没有代码侧的
    SPEC/工具/专属路由，通用记录通路（`/api/domains/{id}/records`）本就按 `domain_data` 表
    工作 —— 现在出厂的域**全部**是这个形状（清单完全归用户）。
    """
    try:
        DomainDataService.check_domain(body.id)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    name = body.id.strip()
    existing = {row["plugin_id"] for row in ctx.plugins.list_plugins()}
    if name in existing:
        raise HTTPException(status_code=409, detail=f"域 {name!r} 已存在。")
    ctx.plugins.create(name, display_name=(body.display_name or "").strip() or name)
    ctx.audit.log(
        actor=actor.id,
        action="create_domain",
        target=name,
        detail={"display_name": (body.display_name or "").strip() or name},
    )
    return _domains_view(ctx)


@router.delete("/api/settings/domains/{domain}", status_code=204)
def delete_domain(
    domain: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    """删掉一个域（资料页「删除这个域」）：置退役旗 + 清掉它的全部通用记录，**不留两套真相**。

    退役不是删行（`PluginService.retire` 的理由：播种 `ON CONFLICT DO NOTHING`，行留着才
    不会被 `seed_plugin_rows` 重新播回 —— 将来代码侧再出厂的域，用户删了也不复活）。
    清单读侧（`list_plugins`/`enabled_domains`）都滤 `retired=1`，删完界面上的页签与
    `list_domains` 工具立刻都不再列出它；装配根据 `retired_domain_ids()` 不挂它交回的
    专属路由（当前出厂域都是纯数据型，没有专属路由可摘；这道闸是给将来的富域留的）——
    "清单里没有"与"端点还在服务"绝不能同时成立。记录数据一并删（域是实例级资源，
    删对所有人删）。

    域不存在 / 已退役 → 404（`retire` 返回 0 行）。
    """
    try:
        DomainDataService.check_domain(domain)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    retired = ctx.plugins.retire(domain)
    if not retired:
        raise HTTPException(status_code=404, detail="域不存在或已删除")
    dropped = _domain_service(ctx).drop_domain_records(domain)
    ctx.audit.log(
        actor=actor.id,
        action="retire_domain",
        target=domain,
        detail={"records_dropped": dropped},
    )


@router.get(
    "/api/domains/{domain}/records",
    response_model=list[DomainRecordView],
)
def list_domain_records(
    domain: str,
    limit: int | None = Query(default=None, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: AppContext = Depends(get_context),
) -> list[object]:
    """列出某域的通用记录（归属演示用户）。

    `limit`/`offset` 可选（给 limit 才分页），缺省全量 —— 响应形状不动，
    契约面只有这两个查询参数；前端多取一条探"有没有下一页"。
    """
    _require_live_domain(ctx, domain)
    try:
        return _domain_service(ctx).list_records(
            domain, ctx.current_user(), limit=limit, offset=offset
        )  # type: ignore[return-value]
    except ValueError as exc:
        raise value_error_to_http(exc) from exc


@router.post(
    "/api/domains/{domain}/records",
    status_code=201,
    response_model=DomainRecordView,
)
def create_domain_record(
    domain: str,
    body: DomainRecordCreate,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """新增一条通用领域记录（兜底录入入口）。"""
    _require_live_domain(ctx, domain)
    try:
        row = _domain_service(ctx).create_record(
            domain,
            ctx.current_user(),
            label=body.label,
            value_text=body.value_text,
            value_num=body.value_num,
            unit=body.unit,
            note=body.note,
        )
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    ctx.audit.log(
        actor=actor.id,
        action="create_domain_record",
        target=f"{domain}/{row['id']}",
        detail={"label": body.label.strip()},
    )
    return row


@router.patch(
    "/api/domains/{domain}/records/{record_id}",
    response_model=DomainRecordView,
)
def patch_domain_record(
    domain: str,
    record_id: str,
    body: DomainRecordPatch,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """修正一条通用记录（写入审计）。"""
    _require_live_domain(ctx, domain)
    changes = body.model_dump(exclude_unset=True)
    try:
        row = _domain_service(ctx).patch_record(domain, ctx.current_user(), record_id, changes)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="记录不存在") from None
    ctx.audit.log(
        actor=actor.id,
        action="update_domain_record",
        target=f"{domain}/{record_id}",
        detail={"fields": sorted(changes)},
    )
    return row


@router.delete("/api/domains/{domain}/records/{record_id}", status_code=204)
def delete_domain_record(
    domain: str,
    record_id: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    _require_live_domain(ctx, domain)
    try:
        _domain_service(ctx).delete_record(domain, ctx.current_user(), record_id)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="记录不存在") from None
    ctx.audit.log(actor=actor.id, action="delete_domain_record", target=f"{domain}/{record_id}")


__all__ = ["router"]
