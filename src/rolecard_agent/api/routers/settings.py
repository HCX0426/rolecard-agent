"""模型后端设置路由：读取（key 只写不回读）与保存（热重建）。

从 `main.py` 迁出的第 5 组（C1）。热重建通过 `ctx.rebuild_graph` —— 重建逻辑属于宿主
（create_app），router 只负责在保存成功后调用它。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.config import Settings
from rolecard_agent.core import runtime_settings
from rolecard_agent.core.memory import (
    clear_memory_text,
    load_memory_text,
    load_role_memory_text,
    save_memory_text,
    save_role_memory_text,
)
from rolecard_agent.core.model_settings import (
    ModelSettingsError,
    client_style,
    is_keyless_provider,
    provider_catalog,
)
from rolecard_agent.core.probes import ollama_keep, ollama_loaded

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
    # 本地 Ollama 的实际上下文窗口（tokens）；None = 引擎默认（常为 2048）。
    num_ctx: int | None = None
    # 后端能力位（换模型对所有角色统一生效）：视觉=能否收图（决定发图按钮）；
    # 工具=工具调用是否可用（某些云端 VLM 带 tools 会返回空 → 关掉后该轮不绑工具）。
    supports_vision: bool = False
    supports_tools: bool = True


class ModelSettingsBody(BaseModel):
    default: str | None = None
    backends: list[BackendSpec]
    # None = 保留当前回退链（默认/回退的编辑入口已统一到「服务」页签的优先级列表；
    # 模型页保存不再顺带覆写，避免两个入口互相覆盖）。
    fallbacks: list[str] | None = None


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
    return {
        "default": ctx.model_settings.default_backend(),
        "backends": ctx.model_settings.list_backends(),
        "fallbacks": ctx.model_settings.list_fallbacks() or [],
    }


# ---------------------------------------------------------------- 运行环境（只读展示）

# 密钥类字段：只回掩码，绝不把明文送出进程（与模型页 has_key 纪律一致）。
_SECRET_FIELDS = frozenset(
    {
        "tavily_api_key",
        "saucenao_api_key",
        "ocr_api_key",
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
            (
                "ocr_backend",
                "OCR_BACKEND",
                "OCR 后端",
                "auto=Paddle 优先、云端兜底 / paddle / cloud",
            ),
            ("ocr_python", "OCR_PYTHON", "Paddle 解释器", f"Paddle 独立解释器：{ocr_python}"),
            ("ocr_api_key", "OCR_API_KEY", "云端 OCR Key", "未配则绝不外发图片"),
            ("ocr_api_url", "OCR_API_URL", "云端 OCR 端点", None),
        ],
    )
    add(
        "rag",
        "检索与抽取",
        [
            (
                "embedding_backend",
                "RAG_EMBEDDING",
                "嵌入后端",
                "auto=有 Key 走 bge-m3，否则离线 hash",
            ),
            ("rag_rerank", "RAG_RERANK", "重排", "auto=有 Key 精排，否则关闭"),
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
                "同一角色两次主动开口的最小间隔（防刷屏；低频项改它需重启）",
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    """角色作用域的守卫：角色不存在 → 404（避免凭空造出孤立的 role_memory 行）。"""
    if not ctx.roles.exists(role_id):
        raise HTTPException(status_code=404, detail=f"角色 {role_id!r} 不存在。")


def _memory_payload(ctx: AppContext, role_id: str | None = None) -> dict[str, object]:
    """记忆面板数据。role_id=None → 全局用户记忆；给定 → 该角色专属记忆。

    `enabled` 是**全局**记忆注入开关（memory_enabled），两作用域都回它供界面显示；
    但它只能在**全局**作用域下改（角色作用域改它 → 400）。角色作用域只读该角色自己
    存的那份，**不回退全局**（编辑/清空必须精确命中该角色的桶）。
    """
    content = (
        load_role_memory_text(ctx.conn, role_id)
        if role_id
        else load_memory_text(ctx.conn)
    )
    return {"enabled": ctx.settings.memory_enabled, "content": content, "role_id": role_id}


@router.get("/api/settings/memory")
def get_memory(
    ctx: AppContext = Depends(get_context),
    role_id: str | None = Query(None, max_length=64),
) -> object:
    """跨会话记忆：开关（有效值）与全文。`?role_id=` 切到该角色的专属记忆。"""
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
    """保存记忆。全局作用域：开关变化走 runtime 覆盖（保存即热重建）+ 文本写 kernel_meta；
    角色作用域（`?role_id=`）：只改该角色的 role_memory，**不接受改开关**（开关是全局的）。

    审计只记结构与体量（开关/字符数/作用域），不记记忆内容 —— 记忆可能含用户隐私事实，
    审计日志不该成为它的第二个拷贝。
    """
    if role_id:
        _require_role(ctx, role_id)
        if body.enabled is not None:
            raise HTTPException(
                status_code=400, detail="记忆注入开关是全局设置，不能在角色作用域下修改。"
            )
        if body.content is None:
            raise HTTPException(status_code=400, detail="没有要保存的内容。")
        save_role_memory_text(ctx.conn, role_id, body.content)
        ctx.roles.audit(
            actor=actor.id,
            action="update_role_memory",
            target=f"memory:{role_id}",
            detail={"chars": len(body.content)},
        )
        return _memory_payload(ctx, role_id)

    if body.enabled is None and body.content is None:
        raise HTTPException(status_code=400, detail="没有要保存的内容。")
    if body.content is not None:
        save_memory_text(ctx.conn, body.content)
    if body.enabled is not None:
        runtime_settings.save_overrides(ctx.conn, {"memory_enabled": "1" if body.enabled else "0"})
    ctx.roles.audit(
        actor=actor.id,
        action="update_memory",
        target="memory",
        detail={"enabled": body.enabled, "chars": len(body.content or "")},
    )
    if body.enabled is not None:
        ctx.rebuild_runtime()
    return _memory_payload(ctx)


@router.delete("/api/settings/memory")
def delete_memory(
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
    role_id: str | None = Query(None, max_length=64),
) -> object:
    """清空记忆全文（开关不动）。给定 role_id 只清该角色的记忆。管理动作必须留痕。"""
    if role_id:
        _require_role(ctx, role_id)
        save_role_memory_text(ctx.conn, role_id, "")
        ctx.roles.audit(
            actor=actor.id, action="clear_role_memory", target=f"memory:{role_id}", detail={}
        )
        return _memory_payload(ctx, role_id)
    clear_memory_text(ctx.conn)
    ctx.roles.audit(actor=actor.id, action="clear_memory", target="memory", detail={})
    return _memory_payload(ctx)


class KeepaliveBody(BaseModel):
    """预热/常驻参数。keep_alive = Ollama 原生秒数，-1 = 永不自动卸载（常驻）。"""

    keep_alive: int = -1
    model: str | None = None


def _default_backend(ctx: AppContext) -> Any:
    """默认对话后端（叠加运行环境覆盖后的有效配置）；未配置默认 → 可读 400。"""
    eff = ctx.app_state.get("effective") or ctx.settings
    try:
        return eff.backend(None)
    except KeyError as exc:
        raise HTTPException(
            status_code=400, detail=f"默认模型后端未配置：{exc}。请先在「模型」页签设置。"
        ) from exc


@router.get("/api/settings/models/resident")
def resident_models(ctx: AppContext = Depends(get_context)) -> object:
    """当前显存里常驻的模型（读 Ollama /api/ps）+ 默认模型是否已在其中。云端后端 → loaded 空。"""
    backend = _default_backend(ctx)
    is_local = client_style(backend.provider) == "native"
    loaded = ollama_loaded(backend.base_url) if is_local else []
    names = {str(m.get("name")) for m in loaded}
    return {
        "is_local": is_local,
        "model": backend.model,
        "loaded": loaded,
        "resident": backend.model in names or backend.model.split(":")[0] in {
            n.split(":")[0] for n in names
        },
    }


@router.post("/api/settings/models/keepalive")
def keepalive_model(
    body: KeepaliveBody,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> object:
    """把默认（或指定）模型载入显存并按 keep_alive 常驻（默认 -1 = 不再 5 分钟自动卸载）。

    只解决"冷加载慢"这一体感；模型是否真能跑仍取决于显存。仅本地 Ollama(native) 支持。
    """
    backend = _default_backend(ctx)
    if client_style(backend.provider) != "native":
        raise HTTPException(
            status_code=400,
            detail="仅本地 Ollama 后端支持常驻；云端后端无 keep_alive。",
        )
    model = (body.model or backend.model).strip()
    ok = ollama_keep(backend.base_url, model, body.keep_alive, num_ctx=backend.num_ctx)
    ctx.roles.audit(
        actor=actor.id,
        action="keepalive_model",
        target=model,
        detail={"keep_alive": body.keep_alive, "num_ctx": backend.num_ctx, "ok": ok},
    )
    return {"ok": ok, "model": model, "loaded": ollama_loaded(backend.base_url)}


__all__ = ["router"]
