"""运行时服务端点目录 —— 「服务」子页签的数据层（引用模型）。

架构归一化：**模型页（model_backend 表）是云端端点配置的唯一事实面**。本表
（service_endpoint）只存「哪些配置参与这类服务、以什么优先级、是否启用」—— 绝不复制
key/base_url/model：

  * **引用行**（builtin=0）：ref_backend → model_backend.name，id = ref_backend（每类
    服务内一后端至多一条引用）。服务页「新增」= 从模型页已配置的后端中选择；「移除」
    只删引用行，**绝不动模型页配置**；配置的编辑只在模型页。后端被模型页删除时，引用
    行在视图中呈现「失效」（不静默跳过）。
  * **本地行**（builtin=1）：paddle / hash / off 等代码能力，id 固定、不可删，同样参与
    排序与启停。
  * **优先级 = sort_order，第 1 位即生效**；启停 = enabled。没有独立的"首选"字段。

三条设计规则（延续）：

  1. **默认行一次性播种**（seed_once + kernel_meta flag）：本地行 + 对已存在后端的默认
     引用；操作员删掉的行重启绝不复活。
  2. **默认优先级按服务类型分别设定**：模型 / OCR 本地优先（隐私 + 数据不出机）；语义
     嵌入云端优先（本地 Hash 兜底只有关键词命中）。
  3. **可用性分两级**：静态检测（配置齐缺 + 本地探活）页面打开即跑；真正的远程调用由
     调用方按需发起（深度检测），避免一次 UI 刷新打爆外部 API。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rolecard_agent.config import Settings
from rolecard_agent.core.model_settings import ModelSettingsService, client_style
from rolecard_agent.storage.db import SqlConnection

# ---------------------------------------------------------------- 服务类别定义


@dataclass(frozen=True, slots=True)
class ServiceCategory:
    """一类服务（OCR / 嵌入 / 重排）：标题 + 兜底说明。候选 = 引用行 + 本地实现行。"""

    key: str
    title: str
    hint: str


SERVICE_CATEGORIES: tuple[ServiceCategory, ...] = (
    ServiceCategory(
        "ocr",
        "OCR（图片文字识别）",
        "本地优先：图片不出本机；本地不可用时才发往云端（有隐私代价）",
    ),
    ServiceCategory(
        "embedding",
        "语义嵌入（知识库检索）",
        "云端优先：本地 Hash 兜底只有关键词命中，语义检索质量大打折扣",
    ),
    ServiceCategory(
        "rerank",
        "检索重排（质量增强）",
        "云端增强：失败自动回退向量序，不会变成可用性故障",
    ),
)

_CATEGORY_KEYS = {c.key for c in SERVICE_CATEGORIES}


def category(key: str) -> ServiceCategory:
    for c in SERVICE_CATEGORIES:
        if c.key == key:
            return c
    raise KeyError(f"unknown service category: {key!r}")


# ---------------------------------------------------------------- 端点行（引用 + 本地实现）

# 引用行未指定专用模型时，各能力使用的默认模型名（嵌入/重排模型 ≠ 对话模型）。
_CAPABILITY_DEFAULT_MODEL: dict[str, str | None] = {
    "embedding": "BAAI/bge-m3",
    "rerank": "BAAI/bge-reranker-v2-m3",
    "ocr": None,
}


def _mask_key(raw: str | None) -> str | None:
    """掩码预览（如 `sk-…abcd`）；≤6 字符全 •。与 model_backend.key_masked 同一纪律。

    不足 7 字符时**全打点**：`raw[:3]…raw[-4:]` 对 7 字符的 key 会把整个 key 拼回来
    （审查报告 L2）。这里宁可少显示一个字符，也不把可用的凭据还原出来。
    """
    if not raw:
        return None
    text = str(raw)
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:3]}…{text[-4:]}"


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """视图/工厂消费的端点形态：本地实现行，或引用行解析到 model_backend 后的快照。

    引用行携带的是**被引用后端**的连接配置（base_url/api_key）；model 按「后端用途与
    服务类别一致用后端模型名；类别有能力默认模型（嵌入/重排）则回落默认；否则（OCR
    走视觉 LLM）用后端自己的模型」解析 —— 所以一个对话后端可以同时服务对话与视觉 OCR，
    无需重复建行（用户 2026-09-17："一个名字不能干两件事？"）。
    """

    id: str
    label: str
    kind: str  # local | cloud
    base_url: str | None
    api_key: str | None
    model: str | None
    enabled: bool
    builtin: bool
    ref_backend: str | None = None
    stale: bool = False  # 引用的后端已被模型页删除

    def to_api(self) -> dict[str, Any]:
        """API 形状（api_key 永不回传，只回掩码）。"""
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "base_url": self.base_url,
            "model": self.model,
            "enabled": self.enabled,
            "builtin": self.builtin,
            "ref_backend": self.ref_backend,
            "stale": self.stale,
            "key_masked": _mask_key(self.api_key) if self.kind == "cloud" else None,
        }


class ServiceEndpointService:
    """`service_endpoint` 表的读写：引用的增删 + 优先级（sort_order）+ 启停。

    异常语义（接入层据此映射 HTTP 状态，两边必须一致）：

      * `KeyError`   —— **找不到**：未知服务类别、未知端点行 → 404；
      * `ValueError` —— **规则不允许**：引用不存在的后端、要停掉最后一个启用行、
        内置行不可删、优先级序列不是全排列 → 400。

    此前这两类都抛 `ValueError`，于是路由里 `except KeyError: 404` 的分支**永远走不到**
    —— 一个从不执行的分支比没有更糟，它让人以为"不存在返回 404"这件事已经被测过
    （代码审查报告（第二轮）补服务端点测试时发现）。
    """

    SEED_FLAG = "service_endpoints_seeded"

    def __init__(self, conn: SqlConnection) -> None:
        self._conn = conn

    # -- 播种 ------------------------------------------------------------------

    def seed_once(self) -> int:
        """一次性播种默认行（flag 之后永不复活被删行）。

        本地实现行必有；默认引用仅在对应后端已存在时创建（嵌入/重排 → `siliconflow`
        后端——对话后端的凭据可直接被嵌入/重排引用，模型名回落到能力默认）。OCR 默认
        无云端引用：OCR.space 账号需显式在模型页建凭据行后再引用（隐私红线，绝不默认）。
        """
        flag = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (self.SEED_FLAG,)
        ).fetchone()
        has_rows = self._conn.execute(
            "SELECT 1 FROM service_endpoint LIMIT 1"
        ).fetchone() is not None
        if flag is not None and has_rows:
            return 0
        # flag 在但表全空 = 表曾被整体重建（迁移）而 flag 未清 —— 自愈重播。
        # builtin 行不可删，合法状态下表永不为空，所以这个分支不会误伤操作员。
        ms = ModelSettingsService(self._conn)
        backends = {str(b["name"]) for b in ms.list_backends()}
        defaults: list[tuple[str, str, str, str | None, int]] = [
            ("ocr", "paddle", "local", None, 1),
            ("embedding", "hash", "local", None, 1),
            ("rerank", "off", "local", None, 1),
        ]
        for ref_name in ("siliconflow",):
            if ref_name in backends:
                defaults.append(("embedding", ref_name, "cloud", ref_name, 0))
                defaults.append(("rerank", ref_name, "cloud", ref_name, 0))
        counters: dict[str, int] = {}
        for cat, eid, kind, ref, builtin in defaults:
            order = counters.get(cat, 0)
            counters[cat] = order + 1
            self._conn.execute(
                "INSERT OR IGNORE INTO service_endpoint "
                "(category, id, kind, ref_backend, enabled, sort_order, builtin) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (cat, eid, kind, ref, order, builtin),
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO kernel_meta (key, value) VALUES (?, ?)",
            (self.SEED_FLAG, "1"),
        )
        self._conn.commit()
        return len(defaults)

    # -- 读 --------------------------------------------------------------------

    def _backend_map(self) -> dict[str, dict[str, Any]]:
        # raw_backends 含 api_key 明文：进程内解析引用行凭据用，绝不进 API 响应
        # （对外形状仍由 list_backends/to_api 的掩码纪律保证）。
        return {str(b["name"]): b for b in ModelSettingsService(self._conn).raw_backends()}

    def _resolved(self, key: str) -> list[EndpointConfig]:
        """行 + 引用解析 → EndpointConfig 列表（按优先级序，含失效引用的可见态）。"""
        rows = self._conn.execute(
            "SELECT category, id, kind, ref_backend, enabled, builtin "
            "FROM service_endpoint WHERE category = ? ORDER BY sort_order, id",
            (key,),
        ).fetchall()
        backends = self._backend_map()
        out: list[EndpointConfig] = []
        for r in rows:
            eid = str(r["id"])
            if str(r["kind"]) == "local":
                out.append(
                    EndpointConfig(
                        id=eid, label=eid, kind="local", base_url=None, api_key=None,
                        model=None, enabled=bool(r["enabled"]), builtin=bool(r["builtin"]),
                    )
                )
                continue
            ref = r["ref_backend"] and str(r["ref_backend"])
            b = backends.get(ref or "")
            if b is None:
                out.append(
                    EndpointConfig(
                        id=eid, label=f"{eid}（引用已失效）", kind="cloud", base_url=None,
                        api_key=None, model=None, enabled=bool(r["enabled"]),
                        builtin=False, ref_backend=ref, stale=True,
                    )
                )
                continue
            is_local = client_style(str(b.get("provider", ""))) == "native"
            default_model = _CAPABILITY_DEFAULT_MODEL.get(key)
            # 模型名解析（用户 2026-09-17："一个名字不能干两件事？"）：
            #   * 后端用途与服务类别一致 → 用后端模型名；
            #   * 类别有能力默认模型（embedding/rerank 要专用模型）→ 只借凭据，回落默认；
            #   * 类别**没有**能力默认（ocr 走视觉 LLM，用的就是后端自己的多模态模型）
            #     → 用后端模型名。这样一个对话后端可同时服务对话与视觉 OCR，无需重复建行。
            model = (
                str(b["model"])
                if (str(b.get("usage", "chat")) == key or default_model is None)
                and b.get("model")
                else default_model
            )
            out.append(
                EndpointConfig(
                    id=eid,
                    label=f"{b['name']} · {b['model']}",
                    kind="local" if is_local else "cloud",
                    base_url=b.get("base_url"),
                    api_key=str(b["api_key"]) if b.get("api_key") else None,
                    model=model,
                    enabled=bool(r["enabled"]),
                    builtin=False,
                    ref_backend=ref,
                )
            )
        return out

    def rows(self, key: str) -> list[EndpointConfig]:
        """该类服务的全部端点（含停用与失效引用），按优先级序。"""
        return self._resolved(key)

    def ordered_candidates(self, key: str) -> list[EndpointConfig]:
        """仅启用的端点，按优先级序（OCR 调用点与工厂 order 参数直接消费）。"""
        return [e for e in self._resolved(key) if e.enabled]

    def endpoint_map(self, key: str) -> dict[str, EndpointConfig]:
        """启用端点 id → 配置快照（工厂按 id 取连接配置实例化客户端）。"""
        return {e.id: e for e in self.ordered_candidates(key)}

    # -- 写 --------------------------------------------------------------------

    def _require_category(self, key: str) -> None:
        if key not in _CATEGORY_KEYS:
            # KeyError（而不是 ValueError）：调用方据此回 404 —— "类别不存在"是找不到资源，
            # 不是"参数格式不对"。见类 docstring 的异常语义。
            raise KeyError(f"未知服务类别：{key!r}")

    def _has_row(self, key: str, eid: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM service_endpoint WHERE category = ? AND id = ?", (key, eid)
            ).fetchone()
            is not None
        )

    def add(self, key: str, *, ref_backend: str) -> None:
        """新增一条引用（从模型页已配置的后端中选择；每类服务内一后端至多一条）。"""
        self._require_category(key)
        ref = (ref_backend or "").strip()
        if not ref:
            raise ValueError("必须选择模型页里已配置的后端。")
        backends = self._backend_map()
        if ref not in backends:
            raise ValueError(f"后端 {ref!r} 不在模型页配置里 —— 请先在「模型」页签新增。")
        if self._has_row(key, ref):
            raise ValueError(f"后端 {ref!r} 已在本服务中。")
        order = max(
            (
                int(r["sort_order"])
                for r in self._conn.execute(
                    "SELECT sort_order FROM service_endpoint WHERE category = ?", (key,)
                ).fetchall()
            ),
            default=-1,
        ) + 1
        self._conn.execute(
            "INSERT INTO service_endpoint "
            "(category, id, kind, ref_backend, enabled, sort_order, builtin) "
            "VALUES (?, ?, 'cloud', ?, 1, ?, 0)",
            (key, ref, ref, order),
        )
        self._conn.commit()

    def patch(self, key: str, eid: str, *, enabled: bool | None = None) -> None:
        """启停一行。配置的编辑在模型页 —— 本表只管「参与与否」。

        停用不允许停掉该类服务最后一个启用行（至少保留一个可用实现）。
        """
        self._require_category(key)
        if not self._has_row(key, eid):
            raise KeyError(f"服务 {key} 下不存在端点 {eid!r}。")
        if enabled is not None:
            if not enabled:
                remaining = [
                    r
                    for r in self._conn.execute(
                        "SELECT id FROM service_endpoint "
                        "WHERE category = ? AND enabled = 1 AND id != ?",
                        (key, eid),
                    ).fetchall()
                ]
                if not remaining:
                    raise ValueError(f"服务 {key} 至少保留一个启用的端点。")
                self._conn.execute(
                    "UPDATE service_endpoint SET enabled = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE category = ? AND id = ?",
                    (key, eid),
                )
            else:
                self._conn.execute(
                    "UPDATE service_endpoint SET enabled = 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE category = ? AND id = ?",
                    (key, eid),
                )
        self._conn.commit()

    def delete(self, key: str, eid: str) -> None:
        """移除一行。引用行只删**引用**——模型页配置不受影响；本地实现行不可删。"""
        self._require_category(key)
        row = self._conn.execute(
            "SELECT builtin FROM service_endpoint WHERE category = ? AND id = ?", (key, eid)
        ).fetchone()
        if row is None:
            raise KeyError(f"服务 {key} 下不存在端点 {eid!r}。")
        if bool(row["builtin"]):
            raise ValueError(f"端点 {eid!r} 是内置本地实现，不可删除（可停用）。")
        self._conn.execute(
            "DELETE FROM service_endpoint WHERE category = ? AND id = ?", (key, eid)
        )
        self._conn.commit()

    def reorder(self, key: str, order: list[str]) -> None:
        """全量写优先级：`order` 必须是该类服务全部行 id 的一个排列（大声拒绝部分序）。"""
        self._require_category(key)
        existing = [str(r["id"]) for r in self._conn.execute(
            "SELECT id FROM service_endpoint WHERE category = ? ORDER BY sort_order, id", (key,)
        ).fetchall()]
        if sorted(order) != sorted(existing):
            raise ValueError("优先级序列必须包含该服务的全部端点（且不重复）。")
        for i, eid in enumerate(order):
            self._conn.execute(
                "UPDATE service_endpoint SET sort_order = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE category = ? AND id = ?",
                (i, key, eid),
            )
        self._conn.commit()


# ---------------------------------------------------------------- 可用性检测


def check_availability(candidate_id: str, settings: Settings) -> tuple[bool, str]:
    """本地实现的廉价静态检测：只看配置齐缺与本地文件探活，**绝不发网络请求**。

    云端引用行的可用性只取决于被引用后端是否配了 key（`endpoint_available`）。
    """
    if candidate_id == "paddle":
        from rolecard_agent.core.paths import default_ocr_python

        exe = settings.ocr_python or default_ocr_python()
        if not exe or not Path(exe).exists():
            return False, "未找到独立 OCR 解释器（.venv-ocr）"
        return True, f"就绪：{Path(exe).name}"
    if candidate_id in {"hash", "off"}:
        return True, "始终可用"
    return False, f"未知本地实现 {candidate_id!r}"


def endpoint_available(e: EndpointConfig, settings: Settings) -> tuple[bool, str]:
    """端点的可用性：失效引用 > 云端 key 齐缺 > 本地探活。

    本地引用行分两种，探测必须与 `select_ocr_backend` 的选择语义**同一份判定**
    （原语在 core/probes.py）——此前探测不认识视觉模型引用行，UI 显示"未知本地
    实现/不可用"而选择器实际会去试，服务页状态自相矛盾（用户 2026-09-17 反馈）：
      * 带模型的视觉引用行（usage=ocr 的后端引用，如 qwen3-vl）→ 探 Ollama /api/tags
        （3s 网络探测；服务页行数个位数，代价可接受）；
      * 纯本地实现（paddle/hash/off）→ 维持廉价静态探活。
    """
    if e.stale:
        return False, "引用的后端已在模型页删除 —— 请移除本行或重新配置后端"
    if e.kind == "cloud":
        return bool(e.api_key), (
            "已配置 API Key" if e.api_key else "后端未配置 API Key（去模型页填写）"
        )
    if e.model:
        from rolecard_agent.core.probes import vision_model_ready

        if vision_model_ready(e.base_url, e.model):
            return True, f"就绪：本地视觉模型 {e.model}"
        return False, f"本地视觉模型不可达或未加载（{e.model}，确认 Ollama 在跑）"
    return check_availability(e.id, settings)


def service_status_view(conn: SqlConnection, settings: Settings) -> dict[str, Any]:
    """「服务」页签的状态视图（每类服务：端点引用、优先级、启停、可用性、当前生效项）。

    生效项 = 优先级第 1 位的**可用**端；第 1 位不可用则顺延到下一个可用者（降级发生在这里，
    并以 degraded_from 留痕）。模型推理类仍只读展示（编辑在「模型」页签）。
    """
    svc = ServiceEndpointService(conn)
    out: list[dict[str, Any]] = []
    for cat in SERVICE_CATEGORIES:
        all_rows = svc.rows(cat.key)
        enabled = [e for e in all_rows if e.enabled]
        # 位置用**身份**而不是相等性：`EndpointConfig` 是 frozen dataclass，默认 `eq=True`，
        # 两个字段完全相同的行（同一后端被两类服务引用、标签一致）在 `list.index()` 下
        # 会被判为同一个 —— 排序号会串（审查报告 L2）。
        order_of = {id(e): i for i, e in enumerate(enabled)}
        items: list[dict[str, Any]] = []
        for e in all_rows:
            available, reason = endpoint_available(e, settings)
            item = e.to_api()
            item["available"] = available
            item["reason"] = reason
            item["order"] = order_of.get(id(e)) if e.enabled else None
            items.append(item)
        effective: EndpointConfig | None = None
        for e in enabled:
            if endpoint_available(e, settings)[0]:
                effective = e
                break
        fallback_from: str | None = None
        if enabled and effective is not None and effective.id != enabled[0].id:
            fallback_from = enabled[0].id
        out.append(
            {
                "key": cat.key,
                "title": cat.title,
                "hint": cat.hint,
                "effective": effective.id if effective else None,
                "effective_kind": effective.kind if effective else None,
                "degraded_from": fallback_from,
                "readonly": False,
                "candidates": items,
            }
        )

    # 模型推理：**可调优先级** —— 第 1 位 = 对话默认后端，其后 = 回退链（写回
    # kernel_meta 的 model_default / model_fallbacks，与「模型」页签同一份存储）。
    # 候选只含 usage=chat 的后端行（usage=ocr 的行归「OCR」类别的引用，不进推理优先级，
    # 否则同一个模型会出现两行 —— 用户实测反馈）。增删与 key 仍在「模型」页签：
    # 同一份数据不设两个编辑入口，这里只调顺序（order_only）。
    ms = ModelSettingsService(conn)
    backends = ms.list_backends()
    default = ms.default_backend() or settings.model_default
    fallbacks = ms.list_fallbacks() or []
    chat_rows = [row for row in backends if str(row.get("usage", "chat")) == "chat"]
    ordered_names = [default, *fallbacks]
    ordered_names = [n for n in ordered_names if n] + [
        str(row["name"]) for row in chat_rows if str(row["name"]) not in ordered_names
    ]
    by_name = {str(row["name"]): row for row in chat_rows}
    model_items: list[dict[str, Any]] = []
    for name in ordered_names:
        row = by_name.get(name)
        if row is None:  # 回退链引用了已删除的后端 —— 不展示，保存时也会被校验拦下
            continue
        # provider 是供应商 id；native 风格 = 本地 Ollama（不外发），openai 兼容 = 云端。
        is_local = client_style(str(row.get("provider", ""))) == "native"
        model_items.append(
            {
                "id": row["name"],
                "label": f"{row['name']} · {row['model']}",
                "kind": "local" if is_local else "cloud",
                "available": is_local or bool(row.get("has_key")),
                "reason": "本地端点（运行状态见深度检测）" if is_local else "已配置 key",
                "enabled": True,
                "builtin": False,
                "key_masked": row.get("key_masked"),
                "base_url": row.get("base_url"),
                "model": row.get("model"),
            }
        )
    effective_backend = by_name.get(default)
    effective_kind = (
        "local"
        if effective_backend
        and client_style(str(effective_backend.get("provider", ""))) == "native"
        else "cloud"
    ) if effective_backend else None
    out.append(
        {
            "key": "models",
            "title": "模型推理（对话与抽取）",
            "hint": "第 1 位 = 对话默认后端，其后依次回退（仅建流阶段失败会回退，最多 2 级）。"
            "增删与 key 在「模型」页签。",
            "effective": default,
            "effective_kind": effective_kind,
            "degraded_from": None,
            "readonly": False,
            "order_only": True,
            "candidates": model_items,
        }
    )
    return {"services": out}
