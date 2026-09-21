"""模型后端设置路由：读取（key 只写不回读）与保存（热重建）。

从 `main.py` 迁出的第 5 组（C1）。热重建通过 `ctx.rebuild_graph` —— 重建逻辑属于宿主
（create_app），router 只负责在保存成功后调用它。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context, value_error_to_http
from rolecard_agent.config import Settings
from rolecard_agent.core import runtime_settings
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
    # 用途（chat/embedding/rerank/ocr）：**过渡字段**，界面不再选它。写进来只翻译成
    # `service_endpoint` 的引用行（chat 由它决定，其余三类由服务页写），读出去是派生值。
    usage: str = "chat"
    # 本地 Ollama 的实际上下文窗口（tokens）；None = 引擎默认（常为 2048）。
    num_ctx: int | None = None
    # 后端能力位（换模型对所有角色统一生效）：视觉=能否收图（徽标 + 调用前拦截的一半证据，
    # 见 core/nodes._reject_unsupported_vision）；
    # 工具=工具调用是否可用（某些云端 VLM 带 tools 会返回空 → 关掉后该轮不绑工具）。
    supports_vision: bool = False
    supports_tools: bool = True


class ModelSettingsBody(BaseModel):
    default: str | None = None
    backends: list[BackendSpec]
    # None = 保留当前回退链（默认/回退的编辑入口已统一到「服务」页签的优先级列表；
    # 模型页保存不再顺带覆写，避免两个入口互相覆盖）。
    fallbacks: list[str] | None = None


def _models_payload(ctx: AppContext) -> dict[str, object]:
    """模型页的读形状（GET 与 PUT 响应同一份，前端不必猜两次不一样）。"""
    return {
        "default": ctx.model_settings.default_backend(),
        "providers": ctx.model_settings.list_providers(),
        "fallbacks": ctx.model_settings.list_fallbacks() or [],
    }


@router.get("/api/settings/models")
def get_model_settings(ctx: AppContext = Depends(get_context)) -> object:
    """模型设置。api_key 永不回明文 —— 只有 has_key 掩码预览。

    只有 `providers` 一个视图（批次②③ 之后旧 `backends` 平铺投影已随界面一起下线）：
    凭据组 → 组下模型行，`used_by` 是派生只读值。内部的 `list_backends()` 还在（服务页与
    工厂消费那套"后端行"形状），但它不再是对外契约。
    """
    return _models_payload(ctx)


@router.get("/api/settings/model-providers")
def get_model_providers() -> object:
    """供应商目录（动态扩展）：设置页「模型」页签的下拉从这里取，不再写死前端。"""
    return {"providers": provider_catalog()}


class ModelContextBody(BaseModel):
    """只改一个后端的上下文窗口；null = 回落到引擎默认。"""

    num_ctx: int | None = None


@router.patch("/api/settings/models/{name}/context")
def patch_model_context(
    name: str,
    body: ModelContextBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """单独改某后端的 num_ctx 并热重建（对话菜单悬浮面板的快速通道）。

    为什么不让前端走整表 PUT：那要求前端持有全部行与回退链，改一个数字却要重传整套
    配置——写放大且易把并发编辑互相覆盖。这里只改一列。名称不存在 → 404（KeyError）。
    """
    try:
        ctx.model_settings.set_num_ctx(name, body.num_ctx)
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail=f"后端 {name!r} 不存在。") from None
    ctx.roles.audit(
        actor=actor.id,
        action="update_model_context",
        target=name,
        detail={"num_ctx": body.num_ctx},
    )
    ctx.rebuild_runtime()
    return {"name": name, "num_ctx": body.num_ctx}


@router.put("/api/settings/models")
def put_model_settings(
    body: ModelSettingsBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """保存后端集合并热重建（下一轮对话即用新后端，无需重启进程）。

    api_key 语义：缺省/None = 保留**该凭据组**已存的 key；空串 = 清除 —— 否则每次没重输
    key 的保存都会把 key 抹掉。同一 (供应商, 端点) 下的多个模型共用一把 key，所以"在已有
    供应商下再加一个模型"不需要重填凭据。fallbacks = 失败自动回退链（≤2 级，按序尝试）。"""
    try:
        # 凭据校验前置：需要 key 的端点没有 key 时，保存即拒绝 —— 否则会存进一个
        # "重建时才炸"的配置（实测：热重建抛 Missing credentials）。
        for b in body.backends:
            if is_keyless_provider(b.provider):
                continue
            if b.api_key:
                continue
            if ctx.model_settings.has_key_for_endpoint(b.provider, b.base_url):
                continue  # 组里已有 key：这一行只是同端点的另一个模型，不必重输
            raise ModelSettingsError(
                f"后端 {b.name} 使用 {b.provider}，缺少 api_key（本地 Ollama 无需填写）。"
            )
        # default/fallbacks 缺省 = 保留当前值（编辑入口已统一到「服务」页签优先级列表）。
        current_default = ctx.model_settings.default_backend() or "local"
        ctx.model_settings.save(
            default=body.default or current_default,
            backends=[b.model_dump() for b in body.backends],
            fallbacks=body.fallbacks,
        )
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 管理面变更必须留痕（H3）。审计只记**结构**（名称/用途/默认/回退链），绝不记 key。
    ctx.roles.audit(
        actor=actor.id,
        action="update_model_settings",
        target=body.default or current_default,
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
    return _models_payload(ctx)


# ---------------------------------------------------------------- 逐条写入与探测（新模型页）


class TargetBody(BaseModel):
    """"给哪个端点问一次"的四种给法，按精确度排：

    1. 只给 `provider_id` —— 已配置的凭据组（key 由服务端取，从不在网络上往返）；
    2. 给 `provider` + `base_url` —— 同端点已被配置过则复用它的 key；
    3. 再带上 `api_key` —— 抽屉里正在填一把新 key（或换一个中转端点复用旧 key）；
    4. 什么都不给只给 `provider` —— 用该厂商的默认端点。
    """

    provider_id: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class CatalogBody(TargetBody):
    """拉模型列表。没有字段是刻意的：这一步只需要知道问谁。"""


@router.post("/api/settings/models/catalog")
def post_model_catalog(
    body: CatalogBody, ctx: AppContext = Depends(get_context)
) -> object:
    """这个端点提供哪些模型名（添加抽屉的第二步：选，而不是抄）。

    为什么是 **POST**：拉 OpenAI 兼容的 `/models` 要带 key，而 key 绝不进 query string
    （会被访问日志、代理日志与浏览器历史留下来）。原设计稿写的是 GET + query，落地时改了。

    **拉不到不是错误**：中转站经常给不全列表，所以这里回 200 + `detail`，界面据此退回
    "手填模型名"，而不是把一次列表失败显示成配置失败。
    """
    from rolecard_agent.core.model_probe import list_models, resolve_target

    try:
        target = resolve_target(
            ctx.model_settings,
            provider_id=body.provider_id,
            provider=body.provider,
            base_url=body.base_url,
            api_key=body.api_key,
        )
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    names, error = list_models(target)
    return {"models": names, "reachable": not error, "detail": error}


class ProbeBody(TargetBody):
    model: str = Field(min_length=1)
    # 两项默认 false：工具探测要烧一次配额，视觉探测要**把图片发出去**（红线动作）。
    # 界面上它们是确认卡里的两个按钮，不是"顺手全测"。
    test_tools: bool = False
    test_vision: bool = False


@router.post("/api/settings/models/probe")
def post_model_probe(
    body: ProbeBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """测连 + 可选的能力探测。**纯测量，不落库** —— 结论由界面确认后另走 capabilities。

    返回里 `calls_used`（真发出去几次推理）与 `vision_source`（free-metadata /
    uploaded-image / not-tested）是给人看的代价账：确认卡上写的"会发几次请求、发不发图片"
    与这里同源，不另编一份。
    """
    from rolecard_agent.core.model_probe import probe, resolve_target

    try:
        target = resolve_target(
            ctx.model_settings,
            provider_id=body.provider_id,
            provider=body.provider,
            base_url=body.base_url,
            api_key=body.api_key,
            model=body.model,
        )
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = probe(target, test_tools=body.test_tools, test_vision=body.test_vision).to_api()
    ctx.roles.audit(
        actor=actor.id,
        action="probe_model",
        target=body.model,
        detail={
            "provider": target.provider,
            "test_tools": body.test_tools,
            "test_vision": body.test_vision,
            "reachable": result["reachable"],
            "calls_used": result["calls_used"],
        },
    )
    return result


class AddModelBody(BaseModel):
    """添加一行模型（测连通过之后才允许调这里）。"""

    model: str = Field(min_length=1)
    provider_id: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    # 配置名（session/角色卡引用它）；缺省由 (供应商, 模型名) 生成
    name: str | None = None
    num_ctx: int | None = None
    supports_vision: bool | None = None
    supports_tools: bool | None = None


@router.post("/api/settings/models", status_code=201)
def post_add_model(
    body: AddModelBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """只加一行：不重排别的行、不覆写回退链（那是整表 `PUT` 的旧习惯）。

    加完热重建，下一轮对话就能选到它；如果这是整套配置里的第一行，它同时成为对话默认
    （否则"加完模型仍然不能对话"是最难查的那种空配置）。
    """
    try:
        added = ctx.model_settings.add_model(
            model=body.model,
            provider=body.provider,
            base_url=body.base_url,
            api_key=body.api_key,
            group_id=body.provider_id,
            name=body.name,
            num_ctx=body.num_ctx,
            supports_vision=body.supports_vision,
            supports_tools=body.supports_tools,
        )
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="add_model",
        target=added["name"],
        detail={
            "provider_id": added["provider_id"],
            "model": body.model,
            "key_given": bool(body.api_key),
        },
    )
    try:
        ctx.rebuild_runtime()
    except Exception as exc:  # noqa: BLE001 - 配置已写进去，生效失败必须说清而不是静默
        raise HTTPException(
            status_code=500, detail=f"配置已保存，但生效失败：{exc}"
        ) from exc
    return _models_payload(ctx) | {"added": added}


@router.delete("/api/settings/models/{name}", status_code=204)
def delete_model(
    name: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> None:
    """删一行模型。组里没别的模型时连凭据一起删；其他服务类别的引用呈现「失效」不动。"""
    try:
        ctx.model_settings.remove_model(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"后端 {name!r} 不存在。") from None
    ctx.roles.audit(actor=actor.id, action="delete_model", target=name, detail={})
    ctx.rebuild_runtime()


class CapabilityBody(BaseModel):
    """探测结论写回。`null` = 这一项没测过（保持 `?`），不是"测过且不支持"。

    只有**出现在请求里**的字段会被写：`{"supports_tools": null}` 是把工具改回"没测过"，
    不带这个键才是"别动它"。
    """

    supports_vision: bool | None = None
    supports_tools: bool | None = None


@router.patch("/api/settings/models/{name}/capabilities")
def patch_model_capabilities(
    name: str,
    body: CapabilityBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """写回能力位并热重建（徽标与调用前拦截都读这一处，界面不再自己判）。"""
    given = body.model_dump(exclude_unset=True)
    try:
        ctx.model_settings.set_capabilities(name, given)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"后端 {name!r} 不存在。") from None
    except ModelSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_model_capabilities",
        target=name,
        detail=dict(given),
    )
    ctx.rebuild_runtime()
    return next(
        b for b in ctx.model_settings.list_backends() if str(b["name"]) == name
    )


# ---------------------------------------------------------------- 运行环境（只读展示）

# 密钥类字段：只回掩码，绝不把明文送出进程（与模型页 has_key 纪律一致）。
_SECRET_FIELDS = frozenset(
    {
        "tavily_api_key",
        "saucenao_api_key",
        "langsmith_api_key",
        "auth_credentials",
        "auth_api_keys",
    }
)


def _display(value: object, *, secret: bool) -> str:
    """把配置值变成可展示文本：None → 未设置；密钥 → 掩码；路径/布尔原样。"""
    if value is None or value == "" or value == []:
        return "未设置"
    text = str(value)
    if secret:
        return f"…{text[-4:]}" if len(text) > 8 else "••••"
    return text


def runtime_payload(
    settings: Settings,
    overrides: dict[str, Any] | None = None,
    model_names: list[str] | None = None,
) -> dict[str, object]:
    """env 部署期配置的只读分组视图（运行环境页签的数据源）。

    边界（通用 vs 特有的归一化约定）：**模型后端 / 服务引用是操作员在线配置**
    （模型页 / 服务页签，DB 是事实面）；可编辑子集（EDITABLE 集合）走「运行环境」页签
    保存即热生效（DB 覆盖 env，rebuild 换装）；其余（认证 / 观测 / 路径）仍只读——
    它们随进程构建，改了也只能重启，界面上如实标注。
    """
    overrides = overrides or {}
    defaults = Settings()
    groups: list[dict[str, object]] = []

    def add(group_key: str, label: str, items: list[tuple[str, str, str, str | None]]) -> None:
        """items: (settings 字段名, env 键名, 中文名, 说明)。"""
        rows = []
        for field, env_key, row_label, note in items:
            value = getattr(settings, field)
            spec = runtime_settings.spec_of(field)
            secret = field in _SECRET_FIELDS
            overridden = field in overrides
            shown = _display(value, secret=secret)
            # 动态选项源：choices_from="models" → 用户配置的模型名（下拉/勾选不再手打）。
            choices: list[str] | None = None
            if spec and spec.choices:
                choices = list(spec.choices)
            elif spec and spec.choices_from == "models":
                choices = model_names or []
            rows.append(
                {
                    "key": env_key,
                    "field": field,
                    "label": row_label,
                    "value": shown,
                    "default": _display(getattr(defaults, field), secret=secret),
                    "changed": shown != _display(getattr(defaults, field), secret=secret),
                    # overridden = DB 覆盖在位（≠ changed：env 也可能和出厂默认不同）
                    "overridden": overridden,
                    # 秘钥永不出明文：覆盖在位时前端输入框只显示占位
                    "override_value": None if (secret or not overridden) else str(overrides[field]),
                    "kind": spec.kind if spec else "ro",
                    "choices": choices,
                    "note": note or "",
                }
            )
        groups.append({"key": group_key, "label": label, "items": rows})

    add(
        "web",
        "联网",
        [
            (
                "web_search_enabled",
                "WEB_SEARCH_ENABLED",
                "联网总闸",
                "0 = web_search / web_fetch 一律返回关闭说明",
            ),
            (
                "web_allowed_domains",
                "WEB_ALLOWED_DOMAINS",
                "域名白名单",
                "web_fetch 只允许名单内域名（子域匹配），空 = 不限",
            ),
            (
                "web_search_backend",
                "WEB_SEARCH_BACKEND",
                "搜索后端",
                "auto=配了 Tavily Key 走云端搜索，否则本地 ddgs",
            ),
            (
                "tavily_api_key",
                "TAVILY_API_KEY",
                "Tavily 云端搜索 Key",
                "本地搜索超时时配它（当前搜索走哪条路看上一行）",
            ),
            (
                "saucenao_api_key",
                "SAUCENAO_API_KEY",
                "SauceNAO 反向图搜 Key",
                "image_search 工具用（识别动漫/插画角色）；不配则该工具返回未配置提示",
            ),
        ],
    )
    ocr_python = settings.ocr_python or "(自动发现 .venv-ocr)"
    add(
        "ocr",
        "OCR",
        [
            ("ocr_python", "OCR_PYTHON", "Paddle 解释器", f"Paddle 独立解释器：{ocr_python}"),
            # 「用哪个 OCR 后端 / 云端 OCR 的 key」这里**不再有行**（P1-5 收口）：曾有的
            # OCR_BACKEND / OCR_API_KEY / OCR_API_URL 三项在生产上从不被读（服务页恒有一条
            # 启用的内置行 ⇒ 工厂的 env 分支不可达），留着就是三个假开关。
            # 事实面在「服务」页的 OCR 端点序 + 模型页的云端后端凭据（图片会外发第三方，
            # 只允许操作员显式配置，绝不从 env 默认启用）。
        ],
    )
    add(
        "rag",
        "检索与抽取",
        [
            # 嵌入/重排的**选型**同样不在这里：「服务」页的端点序是唯一事实面。
            # 这里只留一个真正被读的部署值 —— 云端行未填 base_url 时的兜底端点。
            # 凭据不出现在任何运行环境行里：模型页的 api_key 只写不回读（`has_key` 掩码）。
            (
                "siliconflow_base_url",
                "SILICONFLOW_BASE_URL",
                "云端嵌入/重排兜底端点",
                "行内未填 base_url 的云端端点用它兜底；改它需重启（随进程构建）",
            ),
            ("extract_backend", "EXTRACT_BACKEND", "抽取后端", None),
            ("extract_verify", "EXTRACT_VERIFY", "抽取校对", None),
        ],
    )
    add(
        "limit",
        "超时与预算",
        [
            ("model_timeout_seconds", "MODEL_TIMEOUT_SECONDS", "模型调用超时（秒）", "0=不限"),
            (
                "tool_timeout_seconds",
                "TOOL_TIMEOUT_SECONDS",
                "工具执行上限（秒）",
                "单次工具总时长",
            ),
            ("context_max_chars", "CONTEXT_MAX_CHARS", "历史字符预算", "送模型的历史上限"),
            (
                "consensus_enabled",
                "CONSENSUS_ENABLED",
                "多模型比对总闸",
                "0 = compare_model_answers 返回关闭说明（一次≈N 次调用，且发给多个供应商）",
            ),
        ],
    )
    add(
        "think",
        "思考模式",
        [
            (
                "model_thinking",
                "MODEL_THINKING",
                "思考总开关",
                "auto=按名单自动 / off=名单内也临时关",
            ),
            (
                "model_thinking_models",
                "MODEL_THINKING_MODELS",
                "思考模型名单",
                "名单内模型以 reasoning=True 调用",
            ),
        ],
    )
    add(
        "agent",
        "对话模式",
        [
            (
                "agent_default_mode",
                "AGENT_DEFAULT_MODE",
                "全局默认模式",
                "chat=一问一答 / agent=多步自主任务（步数上限放大、注入规划指令）",
            ),
            (
                "agent_max_steps",
                "AGENT_MAX_STEPS",
                "步数上限（对话档）",
                "单轮允许的图步数；agent 模式自动翻倍，0=库默认",
            ),
        ],
    )
    add(
        "reachout",
        "主动开口",
        [
            (
                "reachout_enabled",
                "REACHOUT_ENABLED",
                "全局总闸",
                "角色主动找你的总开关；谁真有资格主动看各角色卡的开关",
            ),
            (
                "file_watch_enabled",
                "FILE_WATCH_ENABLED",
                "文件事件触发",
                "开 = 轮询任务目录，有变化时该次开口先说变化（绕间隔一次，静默时段不放松）",
            ),
            (
                "reachout_interval_minutes",
                "REACHOUT_INTERVAL_MINUTES",
                "开口间隔（分钟）",
                "同一角色两次主动开口的最小间隔（防刷屏）；保存即热生效，排查时可临时调小",
            ),
            (
                "reachout_merge_days",
                "REACHOUT_MERGE_DAYS",
                "收件箱合并窗口（天）",
                "1/3/7 天：同一角色在一个窗口里的开口折成一行（未读数上角标）。"
                "**改它在「记忆与任务目录」的主动开口卡**，这里只读——一个设置只有一个写点。",
            ),
        ],
    )
    add(
        "run",
        "命令执行",
        [
            (
                "run_tools_enabled",
                "RUN_TOOLS_ENABLED",
                "命令执行总闸",
                "关掉 = run_command 一律返回关闭说明（1=开，0=关）",
            ),
            (
                "run_approval",
                "RUN_APPROVAL",
                "审批模式",
                "manual=命令要人批准才跑（推荐）；auto=无审批直接跑（仅自研/可信目录用）",
            ),
        ],
    )
    add(
        "auth",
        "访问控制",
        [
            ("auth_mode", "AUTH_MODE", "认证模式", "off / auto / on"),
            ("auth_credentials", "AUTH_CREDENTIALS", "Basic 凭据", None),
            ("auth_api_keys", "AUTH_API_KEYS", "API Key 列表", None),
        ],
    )
    add(
        "obs",
        "观测",
        [
            ("obs_backend", "OBS_BACKEND", "观测后端", None),
            ("obs_emit_raw_text", "OBS_EMIT_RAW_TEXT", "记录原文", None),
            ("langsmith_project", "LANGSMITH_PROJECT", "LangSmith 项目", None),
            ("langsmith_api_key", "LANGSMITH_API_KEY", "LangSmith Key", None),
        ],
    )

    return {
        "note": "标「可改」的项在本页保存即热生效（DB 覆盖 env，清空即回落 env 值）；"
        "只读项（认证 / 观测 / 路径 / OCR 解释器）随进程构建，需改 .env 重启。"
        "模型后端与服务引用请到「模型」「服务」页签。",
        "groups": groups,
    }


@router.get("/api/settings/runtime")
def get_runtime_settings(ctx: AppContext = Depends(get_context)) -> object:
    """运行环境视图：当前生效值（env + DB 覆盖叠加）vs 默认值，密钥只回掩码。"""
    return runtime_payload(
        ctx.settings,
        runtime_settings.load_overrides(ctx.conn),
        _model_names(ctx.conn),
    )


def _model_names(conn: object) -> list[str]:
    """用户在模型页配置的全部模型名（去重）：思考名单等动态下拉的选项源。"""
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT DISTINCT model FROM model_backend WHERE model IS NOT NULL ORDER BY model"
    ).fetchall()
    return [str(r["model"]) for r in rows]


class RuntimeUpdateBody(BaseModel):
    """字段名 → 提交值。None/空串 = 清除该覆盖（回落 env）；未出现的字段 = 不变。"""

    values: dict[str, str | None]


@router.put("/api/settings/runtime")
def put_runtime_settings(
    body: RuntimeUpdateBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """保存运行环境覆盖并热重建：联网/检索/超时/思考等即时生效，无需重启进程。

    校验失败（未知字段/非法枚举/坏数字）整体拒绝 400，不落半套配置；秘钥字段不回明文，
    输入留空 = 保持现状。
    """
    if not body.values:
        raise HTTPException(status_code=400, detail="没有要保存的配置项。")
    try:
        runtime_settings.save_overrides(ctx.conn, body.values)
    except ValueError as exc:
        raise value_error_to_http(exc) from exc
    ctx.roles.audit(
        actor=actor.id,
        action="update_runtime_settings",
        target="runtime",
        detail={"fields": sorted(body.values)},
    )
    ctx.rebuild_runtime()
    return runtime_payload(
        ctx.settings,
        runtime_settings.load_overrides(ctx.conn),
        _model_names(ctx.conn),
    )


# ---------------------------------------------------------------- 跨会话记忆


class MemoryBody(BaseModel):
    """记忆面板的保存负载：任选其一提交，缺省 = 该字段不变。

    `enabled` = 总开关（走 runtime 覆盖保存 + 热重建，下一轮对话即生效）；
    `content`  = 记忆全文（None = 不变；"" = 清空）。
    """

    enabled: bool | None = None
    content: str | None = None


def _require_role(ctx: AppContext, role_id: str) -> None:
    """角色作用域的守卫：角色不存在 → 404（避免凭空造出孤立的记忆桶）。"""
    if not ctx.roles.exists(role_id):
        raise HTTPException(status_code=404, detail=f"角色 {role_id!r} 不存在。")


def _memory_payload(ctx: AppContext, role_id: str | None = None) -> dict[str, object]:
    """记忆面板数据。role_id=None → 全局用户记忆；给定 → 该角色专属记忆。

    `items` 是事实面（逐条可编辑/钉住/删除），`content` 是按注入顺序渲染出来的同一份 ——
    两个字段不是两处真相：前者是行，后者是那几行的文本视图，界面两种用法都能拿。
    `over_limit` 用来提示"建议整理"（超限的保底淘汰在写入时已做，模型合并是显式动作）。
    """
    from rolecard_agent.core import memory as mem

    bucket = role_id if role_id else mem.GLOBAL_BUCKET
    items = mem.list_items(ctx.conn, bucket=bucket)
    content, _ = mem.render_memory(ctx.conn, bucket=bucket)
    active = [i for i in items if i["invalidated_at"] is None]
    return {
        "enabled": ctx.settings.memory_enabled,
        "role_id": role_id,
        "content": content,
        "items": items,
        "active_count": len(active),
        "limit": mem.MAX_ITEMS_PER_BUCKET,
        "over_limit": len(active) >= mem.MAX_ITEMS_PER_BUCKET,
    }


@router.get("/api/settings/memory")
def get_memory(
    ctx: AppContext = Depends(get_context),
    role_id: str | None = Query(None, max_length=64),
) -> object:
    """跨会话记忆：开关（有效值）+ 条目 + 渲染文本。`?role_id=` 切到该角色的专属记忆。"""
    if role_id:
        _require_role(ctx, role_id)
    return _memory_payload(ctx, role_id)


@router.put("/api/settings/memory")
def put_memory(
    body: MemoryBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
    role_id: str | None = Query(None, max_length=64),
) -> object:
    """保存记忆。全局作用域：开关变化走 runtime 覆盖（保存即热重建）+ 整段文本按行覆写条目；
    角色作用域（`?role_id=`）：只改该角色的桶，**不接受改开关**（开关是全局的）。

    整段覆写只动**未钉住**的条目 —— 用户特意钉的东西不该被一次整段保存抹掉。
    审计只记结构与体量（开关/字符数/作用域），不记记忆内容。
    """
    from rolecard_agent.core import memory as mem

    bucket = role_id if role_id else mem.GLOBAL_BUCKET
    if role_id:
        _require_role(ctx, role_id)
        if body.enabled is not None:
            raise HTTPException(
                status_code=400, detail="记忆注入开关是全局设置，不能在角色作用域下修改。"
            )
        if body.content is None:
            raise HTTPException(status_code=400, detail="没有要保存的内容。")
        mem.replace_bucket_from_text(ctx.conn, bucket=bucket, text=body.content)
        ctx.roles.audit(
            actor=actor.id,
            action="update_role_memory",
            target=f"memory:{role_id}",
            detail={"chars": len(body.content)},
        )
        return _memory_payload(ctx, role_id)

    if body.enabled is None and body.content is None:
        raise HTTPException(status_code=400, detail="没有要保存的内容。")
    saved_chars = 0 if body.content is None else len(body.content)
    if body.content is not None:
        mem.replace_bucket_from_text(ctx.conn, bucket=mem.GLOBAL_BUCKET, text=body.content)
    if body.enabled is not None:
        runtime_settings.save_overrides(ctx.conn, {"memory_enabled": "1" if body.enabled else "0"})
    ctx.roles.audit(
        actor=actor.id,
        action="update_memory",
        target="memory",
        detail={"enabled": body.enabled, "chars": saved_chars},
    )
    if body.enabled is not None:
        ctx.rebuild_runtime()
    return _memory_payload(ctx)


class MemoryItemBody(BaseModel):
    """新增一条记忆。`role_id` 走查询参数，与整桶接口同一个作用域口径。"""

    text: Annotated[str, Field(min_length=1, max_length=2000)]


class MemoryItemPatchBody(BaseModel):
    """改一条：文本与钉住状态任选其一。"""

    text: Annotated[str | None, Field(default=None, max_length=2000)] = None
    pinned: bool | None = None


@router.post("/api/settings/memory/item")
def post_memory_item(
    body: MemoryItemBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
    role_id: str | None = Query(None, max_length=64),
) -> object:
    """面板手动加一条事实（source=manual）。角色作用域必须先有该角色。"""
    from rolecard_agent.core import memory as mem

    if role_id:
        _require_role(ctx, role_id)
    bucket = role_id if role_id else mem.GLOBAL_BUCKET
    added = mem.add_item(ctx.conn, bucket=bucket, text=body.text, source="manual")
    if added is None:
        raise HTTPException(status_code=400, detail="传入的记忆内容为空。")
    ctx.roles.audit(
        actor=actor.id,
        action="add_memory_item",
        target=f"memory:{bucket}",
        detail={"chars": len(added["text"])},
    )
    return _memory_payload(ctx, role_id)


@router.patch("/api/settings/memory/item/{item_id}")
def patch_memory_item(
    item_id: int,
    body: MemoryItemPatchBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
    role_id: str | None = Query(None, max_length=64),
) -> object:
    """改一条的文本，或钉住/取消钉住。"""
    from rolecard_agent.core import memory as mem

    if mem.get_item(ctx.conn, item_id) is None:
        raise HTTPException(status_code=404, detail=f"记忆条目不存在：{item_id}")
    updated: dict[str, object] = {}
    if body.text is not None:
        updated = mem.edit_item(ctx.conn, item_id=item_id, text=body.text) or {}
    if body.pinned is not None:
        updated = mem.set_pinned(ctx.conn, item_id=item_id, pinned=body.pinned) or {}
    if not updated:
        raise HTTPException(status_code=400, detail="没有要保存的内容。")
    ctx.roles.audit(
        actor=actor.id,
        action="update_memory_item",
        target=f"memory_item:{item_id}",
        detail={"pinned": updated.get("pinned"), "chars": len(str(updated.get("text") or ""))},
    )
    return _memory_payload(ctx, role_id)


@router.delete("/api/settings/memory/item/{item_id}")
def remove_memory_item(
    item_id: int,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
    role_id: str | None = Query(None, max_length=64),
) -> object:
    """删除一条。用户明确删除 = **物理删**（留个"已删除"的行只是把隐私留在盘上）；
    自动退役才走 `invalidated_at`。"""
    from rolecard_agent.core import memory as mem

    if not mem.delete_item(ctx.conn, item_id=item_id):
        raise HTTPException(status_code=404, detail=f"记忆条目不存在：{item_id}")
    ctx.roles.audit(
        actor=actor.id, action="delete_memory_item", target=f"memory_item:{item_id}", detail={}
    )
    return _memory_payload(ctx, role_id)


@router.delete("/api/settings/memory")
def delete_memory(
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
    role_id: str | None = Query(None, max_length=64),
) -> object:
    """清空整个记忆桶（开关不动）。给定 role_id 只清该角色的。管理动作必须留痕。"""
    from rolecard_agent.core import memory as mem

    bucket = role_id if role_id else mem.GLOBAL_BUCKET
    if role_id:
        _require_role(ctx, role_id)
    rows = mem.list_items(ctx.conn, bucket=bucket, include_invalidated=True)
    for item in rows:
        mem.delete_item(ctx.conn, item_id=item["id"])
    ctx.roles.audit(
        actor=actor.id,
        action="clear_role_memory" if role_id else "clear_memory",
        target=f"memory:{bucket}",
        detail={"items": len(rows)},
    )
    return _memory_payload(ctx, role_id)


__all__ = ["router"]
