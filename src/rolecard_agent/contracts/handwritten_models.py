"""手写响应模型 —— **写端点那一族**（P1b 第四批，2026-10-29）。

为什么这一族不能自动生成（生成器闸①的形状学解释）：写端点的响应**键集随分支变**，
采样只能拿到"某一次调用的形状"——`/api/records/extract` 有 1 个成功体 + 3 个互斥的
降级体；`/api/mcp/servers/{id}/test` 的 `error` 只在失败路**整个键不存在**（不是 null
值）。生成器的表达能力是"键必填、值可空"，写不出"键可有可无"，所以这些端点从前
零契约、前端全靠手抄。这里按运行时真实返回逐键手抄一份。

**必填 vs 默认的判据**（对齐前端类型，别把"恒有"降级成"可选"）：
  * 该端点**在这一形状里恒发的键** ⇒ 必填（`x: T | None` 不带默认）——键总在、值可空，
    与生成物同一口径；漏了默认值让 TS 退化成 `x?:` 是撒谎（前端会当成可能没有）。
  * **随分支整个缺席的键**（extract 的成功体 vs 三条降级体互斥、check 的 ollama vs
    openai_compatible、test 的 error、connectivity 的 models）⇒ 带默认值，配
    `response_model_exclude_unset=True` 把没给的那支裁掉。响应与端点真返回逐字一致。

与生成物的分工：本文件**手写、会被人工评审**；漂移的防线不是比字节（那只管
response_models.py），是接线处每条都有集成/单元用例真打过响应体 + 覆盖率地板看着
本文件的行。新增写端点契约：在这里加模型、在路由装饰器接 `response_model=`，别进
生成器白名单（那会让"白名单↔接线表同集合"的锚点用例红）。

子模型能复用生成物的就复用（`SettingsMemoryViewItemsItem` 那族）——同一个事实只住
一个地址。
"""

from __future__ import annotations

from pydantic import BaseModel


class MemoryRowView(BaseModel):
    """记忆桶里的一行（`core/memory` 的 `_row_to_dict`/`list_items`，13 键恒有）。

    生成物那份 `SettingsMemoryViewItemsItem` 把 id/pinned/hit_count 都推成了可空、
    last_hit_at/invalidated_at/superseded_by 推成 unknown —— 因为**采样期那几格恒 null**
    （新记忆还没被命中/整理过）。运行时真相是 id 恒 int、pinned 恒 bool（列有默认）、
    三个时间戳可 null。手写这一份既如实又不逼前端到处 `?? 0`（items[] 用它的端点：
    consolidate 写响应；GET /api/settings/memory 那份生成物照旧，两不相扰）。
    """

    id: int
    user_id: str
    uid: str
    role_id: str
    text: str
    source: str
    pinned: bool
    hit_count: int
    importance: int
    last_hit_at: str | None
    invalidated_at: str | None
    superseded_by: int | None
    created_at: str


class ExtractedIndexModel(BaseModel):
    """抽取里冲突行的两侧读数（`ExtractedIndex.model_dump()` 恒 8 键，全量 dump）。"""

    index_name: str
    index_value: float | None
    value_text: str | None
    unit: str | None
    ref_range: str | None
    raw_text: str
    confidence: str


class ExtractWrittenModel(BaseModel):
    """抽取响应里 written[] 的元素 —— 只有 4 个键。

    ⚠️ 与库里的指标行（9 键的 SettingsMemoryViewItemsItem_indicesItem 同族）**不是**
    一个形状：回给客户端的是抽取侧的窄投影，写库的那份（8 键 payload）不出门。
    前端旧手写 ExtractWritten 错指到 9 键那份上，本模型把它拉回运行时事实。
    """

    index_name: str
    index_value: float | None
    value_text: str | None
    unit: str | None


class ExtractConflictModel(BaseModel):
    index_name: str
    reason: str
    primary: ExtractedIndexModel | None
    verify: ExtractedIndexModel | None


class ExtractOutcomeView(BaseModel):
    """`POST /api/records/extract` 的全部 4 条返回路径（键集互斥，靠 exclude_unset 裁）。

    这是本文件唯一"全键带默认"的模型：成功路只给 mode..notes 七键，三条降级路各给
    skipped + detail/report_id 之二——四套键集互斥，任一路都不该被另一端缺的键撑爆
    校验。前端按 `skipped` 有无分支消费。
    """

    mode: str | None = None
    report_type: str | None = None
    check_time: str | None = None
    institution: str | None = None
    written: list[ExtractWrittenModel] | None = None
    conflicts: list[ExtractConflictModel] | None = None
    notes: list[str] | None = None
    skipped: str | None = None
    detail: str | None = None
    report_id: str | None = None


class MemoryReportView(BaseModel):
    """`memory_distill._report` 的账本（12 键恒有；见 core/memory/memory_distill.py）。"""

    added: int
    updated: int
    similar: int
    merged: int
    invalidated: int
    noop: int
    skipped: int
    dup_echo: int
    detail: str
    tokens: int | None
    before: int | None
    after: int | None


class MemoryConsolidatedView(BaseModel):
    """`POST /api/settings/memory/consolidate`：report + _memory_payload 摊平的 9 键（恒有）。

    顶层**没有** turns_since —— 那是会话 distill（DistillOutcomeView）独有的键，
    前端旧 ConsolidateOutcome extends DistillOutcome 把不存在于响应的必填键抄了进来，
    本模型按运行时形状收口。items[] 用 MemoryRowView（比生成物那份更贴运行时：
    id/pinned 恒非空，采样期恒 null 的三格在这里如实标可空）。
    """

    report: MemoryReportView
    enabled: bool
    role_id: str | None
    content: str
    items: list[MemoryRowView]
    active_count: int
    limit: int
    over_limit: bool
    extract_turns: int


class DistillOutcomeView(BaseModel):
    """`POST /api/session/{thread_id}/distill`：{"report": …, "turns_since": …}（恒 2 键）。"""

    report: MemoryReportView
    turns_since: int


class ProbeOutcomeView(BaseModel):
    """`POST /api/settings/models/probe`（model_probe.ProbeResult.to_api() 恒 8 键）。

    model_listed/tools/vision 是**三态**（true/false/null=没测或测不出），键恒在、值可
    null ⇒ 必填 + `| None`，不降级成可选。
    """

    reachable: bool
    detail: str
    model_listed: bool | None
    tools: bool | None
    vision: bool | None
    vision_source: str
    calls_used: int
    models: list[str]


class ConnectivityProbeView(BaseModel):
    """`POST /api/services/check` 的内层：reachable/detail 恒有，models 是可选键（非空才写）。"""

    reachable: bool
    detail: str
    models: list[str] | None = None


class ServiceCheckView(BaseModel):
    """`POST /api/services/check`：顶层键名按 provider 风格二选一（ollama /
    openai_compatible），exclude_unset 把没走的那一支裁掉——两支互斥 ⇒ 都带默认。"""

    ollama: ConnectivityProbeView | None = None
    openai_compatible: ConnectivityProbeView | None = None


class McpTestResultView(BaseModel):
    """`POST /api/mcp/servers/{id}/test`：id/ok/tool_count/tools 恒有，error 是可选键
    （只在异常路出现，非 null 值）⇒ 前三必填、error 带默认。"""

    id: str
    ok: bool
    tool_count: int
    tools: list[str]
    error: str | None = None


class CleanupResultView(BaseModel):
    """`POST /api/uploads/cleanup`：恒 4 键，无 orphans/dangling（那是 GET 的形状）。"""

    deleted: int
    freed_bytes: int
    scanned: int
    referenced: int


class DomainRecordView(BaseModel):
    """`/api/domains/{domain}/records` 的通用记录行（GET 列表 / POST / PATCH 三个端点共用）。

    带 `{domain}` 路径参数的端点不进生成器白名单的理由：域 id 是**数据而不是形状**——
    样本只能证明 finance 一个域，契约却对任意注册域成立；手写模型的字段集取自
    `DomainDataService._row_to_dict`（8 键恒有），比一次采样诚实。
    value_num 在库里是 REAL 列、`_row_to_dict` 原样透传（float | None）。
    """

    id: str
    domain: str
    label: str
    value_text: str | None
    value_num: float | None
    unit: str | None
    note: str | None
    created_at: str
