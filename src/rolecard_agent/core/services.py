"""运行时服务端点目录 —— 「服务」子页签的数据层。

把三类**非模型**服务（OCR / 语义嵌入 / 检索重排）的候选**实例**收敛到 `service_endpoint`
表。哲学修正（架构归一化）：旧设计"候选在代码里定义，DB 只存排序与启停"把每类服务钉死
在 2 个候选上 —— 操作员想加第 3 个云端条目（另一个 key 的 OCR、另一家嵌入商）只能改代码。
现在候选实例 = 行：

  * **云端行可增删改**：各自带 base_url / api_key / model，同一类服务可以并存多个云端实例
    （多账号 / 多厂商），谁排前面谁先被用 —— 多模型比对就是调这个顺序。
  * **本地实现是代码能力**（Paddle / Hash / off）：行 builtin=1 不可删，但同样参与排序与启停。
  * **优先级 = sort_order，第 1 位即生效**；没有独立的"首选"字段 —— 有顺序就不需要第二个
    真相源。启停 = enabled。

三条设计规则（延续）：

  1. **默认行一次性播种**（seed_once + kernel_meta flag）：云端行的 key 从 env 引导一次，
     此后 UI 是唯一事实源（与 model_backend.seed_from_env 同一哲学）；操作员删掉的行重启
     绝不复活。
  2. **默认优先级按服务类型分别设定**：模型 / OCR 本地优先（隐私 + 数据不出机）；语义嵌入
     云端优先（本地 Hash 兜底只有关键词命中）。
  3. **可用性分两级**：静态检测（配置齐缺 + 本地探活）页面打开即跑；真正的远程调用由调用方
     按需发起（深度检测），避免一次 UI 刷新打爆外部 API。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rolecard_agent.config import Settings

# ---------------------------------------------------------------- 服务类别定义


@dataclass(frozen=True, slots=True)
class ServiceCategory:
    """一类服务（OCR / 嵌入 / 重排）：标题 + 兜底说明。候选实例在 `service_endpoint` 表。"""

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


# ---------------------------------------------------------------- 端点实例（候选 = 行）


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """一个服务端点实例（`service_endpoint` 表的一行）。

    云端行 = 可增删改的配置条目（各自 key/base_url/model）；本地行 = 代码能力的引用
    （builtin，不可删，只可排序与启停）。
    """

    id: str
    label: str
    kind: str  # local | cloud
    base_url: str | None
    api_key: str | None
    model: str | None
    enabled: bool
    builtin: bool


_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _mask_key(raw: str | None) -> str | None:
    """掩码预览（如 `sk-…abcd`）；≤6 字符全 •。与 model_backend.key_masked 同一纪律。"""
    if not raw:
        return None
    raw = str(raw)
    if len(raw) <= 6:
        return "•" * len(raw)
    return f"{raw[:3]}…{raw[-4:]}"


class ServiceEndpointService:
    """`service_endpoint` 表的读写：候选实例的增删改 + 优先级（sort_order）+ 启停。"""

    SEED_FLAG = "service_endpoints_seeded"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- 播种 ------------------------------------------------------------------

    def seed_once(self, settings: Settings) -> int:
        """一次性播种默认端点行（flag 之后永不复活被删行）。

        云端行的 key/base_url 从 env 引导一次（SILICONFLOW_API_KEY / OCR_API_KEY）——
        此前这些 key 散在 env 里被工厂硬解析，现在进表后 UI 是唯一事实源。
        """
        flag = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (self.SEED_FLAG,)
        ).fetchone()
        if flag is not None:
            return 0
        import os

        sf_base = "https://api.siliconflow.cn/v1"
        sf_key = os.environ.get("SILICONFLOW_API_KEY")
        defaults: list[tuple[str, str, str, str, str | None, str | None, str | None, int]] = [
            ("ocr", "paddle", "PaddleOCR（独立进程）", "local", None, None, None, 1),
            ("ocr", "cloud", "OCR.space（云端）", "cloud",
             settings.ocr_api_url, settings.ocr_api_key, None, 0),
            ("embedding", "siliconflow", "SiliconFlow bge-m3", "cloud",
             sf_base, sf_key, "BAAI/bge-m3", 0),
            ("embedding", "hash", "Hash 离线", "local", None, None, None, 1),
            ("rerank", "siliconflow", "SiliconFlow 重排", "cloud",
             sf_base, sf_key, "BAAI/bge-reranker-v2-m3", 0),
            ("rerank", "off", "关闭（按向量序）", "local", None, None, None, 1),
        ]
        counters: dict[str, int] = {}
        for cat, eid, label, kind, base_url, api_key, model, builtin in defaults:
            order = counters.get(cat, 0)
            counters[cat] = order + 1
            self._conn.execute(
                "INSERT OR IGNORE INTO service_endpoint "
                "(category, id, label, kind, base_url, api_key, model, "
                " enabled, sort_order, builtin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (cat, eid, label, kind, base_url, api_key, model, order, builtin),
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO kernel_meta (key, value) VALUES (?, ?)",
            (self.SEED_FLAG, "1"),
        )
        self._conn.commit()
        return len(defaults)

    # -- 读 --------------------------------------------------------------------

    def _rows_raw(self, key: str) -> list[EndpointConfig]:
        rows = self._conn.execute(
            "SELECT category, id, label, kind, base_url, api_key, model, enabled, builtin "
            "FROM service_endpoint WHERE category = ? ORDER BY sort_order, id",
            (key,),
        ).fetchall()
        return [
            EndpointConfig(
                id=str(r["id"]),
                label=str(r["label"]),
                kind=str(r["kind"]),
                base_url=r["base_url"],  # type: ignore[arg-type]
                api_key=r["api_key"],  # type: ignore[arg-type]
                model=r["model"],  # type: ignore[arg-type]
                enabled=bool(r["enabled"]),
                builtin=bool(r["builtin"]),
            )
            for r in rows
        ]

    def rows(self, key: str) -> list[EndpointConfig]:
        """该类服务的全部端点行（含停用），按优先级序。"""
        return self._rows_raw(key)

    def ordered_candidates(self, key: str) -> list[EndpointConfig]:
        """仅启用的行，按优先级序（旧名保留：OCR 调用点与工厂 order 参数直接消费）。"""
        return [e for e in self._rows_raw(key) if e.enabled]

    def endpoint_map(self, key: str) -> dict[str, EndpointConfig]:
        """启用行 id → 配置（工厂按 id 取实例配置构造云端客户端）。"""
        return {e.id: e for e in self.ordered_candidates(key)}

    # -- 写 --------------------------------------------------------------------

    def _require_category(self, key: str) -> None:
        if key not in _CATEGORY_KEYS:
            raise ValueError(f"未知服务类别：{key!r}")

    def _get(self, key: str, eid: str) -> EndpointConfig:
        for e in self._rows_raw(key):
            if e.id == eid:
                return e
        raise ValueError(f"服务 {key} 下不存在端点 {eid!r}")

    def add(
        self,
        key: str,
        *,
        label: str,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        eid: str | None = None,
    ) -> EndpointConfig:
        """新增一个云端端点实例。id 省略时自动生成（`{key}-N` 第一个空位）。"""
        self._require_category(key)
        clean_label = (label or "").strip()
        if not clean_label:
            raise ValueError("端点名称不能为空。")
        if eid is None:
            n = 1
            existing = {e.id for e in self._rows_raw(key)}
            while f"{key}-{n}" in existing:
                n += 1
            eid = f"{key}-{n}"
        if not _SLUG_RE.match(eid):
            raise ValueError(
                f"端点 id {eid!r} 不合法：小写字母开头，只含小写字母/数字/下划线/连字符。"
            )
        if any(e.id == eid for e in self._rows_raw(key)):
            raise ValueError(f"端点 id 重复：{eid}")
        order = max((e_sort for e_sort in self._raw_sort_orders(key)), default=-1) + 1
        self._conn.execute(
            "INSERT INTO service_endpoint "
            "(category, id, label, kind, base_url, api_key, model, enabled, sort_order, builtin) "
            "VALUES (?, ?, ?, 'cloud', ?, ?, ?, 1, ?, 0)",
            (key, eid, clean_label, base_url, api_key, model, order),
        )
        self._conn.commit()
        return self._get(key, eid)

    def patch(
        self,
        key: str,
        eid: str,
        *,
        label: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        enabled: bool | None = None,
    ) -> EndpointConfig:
        """编辑端点。api_key 语义与 model_backend 一致：None=保留、""=清除、非空=设置。

        builtin 本地行只允许改 `enabled`（label/base_url 等是代码事实，不是配置）。
        停用不允许停掉该类服务最后一个启用行 —— 至少保留一个可用实现。
        """
        self._require_category(key)
        row = self._get(key, eid)
        sets: dict[str, Any] = {}
        if not row.builtin:
            if label is not None:
                clean = label.strip()
                if not clean:
                    raise ValueError("端点名称不能为空。")
                sets["label"] = clean
            if base_url is not None:
                sets["base_url"] = base_url.strip() or None
            if model is not None:
                sets["model"] = model.strip() or None
            if api_key is not None:
                sets["api_key"] = None if str(api_key).strip() == "" else str(api_key).strip()
        if enabled is not None:
            if not enabled:
                siblings = [e for e in self.ordered_candidates(key) if e.id != eid]
                if row.enabled and not siblings:
                    raise ValueError(f"服务 {key} 至少保留一个启用的端点。")
                sets["enabled"] = 0
            else:
                sets["enabled"] = 1
        if not sets:
            return row
        cols = ", ".join(f"{k} = ?" for k in sets)
        self._conn.execute(
            f"UPDATE service_endpoint SET {cols}, updated_at = CURRENT_TIMESTAMP "
            "WHERE category = ? AND id = ?",
            (*sets.values(), key, eid),
        )
        self._conn.commit()
        return self._get(key, eid)

    def delete(self, key: str, eid: str) -> None:
        """删除端点。builtin 本地实现不可删（删了就没有可用的本地兜底了）。"""
        self._require_category(key)
        row = self._get(key, eid)
        if row.builtin:
            raise ValueError(f"端点 {eid!r} 是内置本地实现，不可删除（可停用）。")
        self._conn.execute(
            "DELETE FROM service_endpoint WHERE category = ? AND id = ?", (key, eid)
        )
        self._conn.commit()

    def reorder(self, key: str, order: list[str]) -> None:
        """全量写优先级：`order` 必须是该类服务全部行 id 的一个排列（大声拒绝部分序）。"""
        self._require_category(key)
        existing = [e.id for e in self._rows_raw(key)]
        if sorted(order) != sorted(existing):
            raise ValueError("优先级序列必须包含该服务的全部端点（且不重复）。")
        for i, eid in enumerate(order):
            self._conn.execute(
                "UPDATE service_endpoint SET sort_order = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE category = ? AND id = ?",
                (i, key, eid),
            )
        self._conn.commit()

    def _raw_sort_orders(self, key: str) -> list[int]:
        return [
            int(r["sort_order"])
            for r in self._conn.execute(
                "SELECT sort_order FROM service_endpoint WHERE category = ?", (key,)
            ).fetchall()
        ]


# ---------------------------------------------------------------- 可用性检测


def check_availability(candidate_id: str, settings: Settings) -> tuple[bool, str]:
    """本地实现的廉价静态检测：只看配置齐缺与本地文件探活，**绝不发网络请求**。

    云端行的可用性只取决于行内是否配了 key（`endpoint_available`），不走这里。
    """
    if candidate_id == "paddle":
        from rolecard_agent.rag.parser import _default_ocr_python

        exe = settings.ocr_python or _default_ocr_python()
        if not exe or not Path(exe).exists():
            return False, "未找到独立 OCR 解释器（.venv-ocr）"
        return True, f"就绪：{Path(exe).name}"
    if candidate_id in {"hash", "off"}:
        return True, "始终可用"
    return False, f"未知本地实现 {candidate_id!r}"


def endpoint_available(e: EndpointConfig, settings: Settings) -> tuple[bool, str]:
    """端点行的可用性：云端 = 行内配了 key；本地 = 代码实现的静态探活。"""
    if e.kind == "cloud":
        return bool(e.api_key), ("已配置 API Key" if e.api_key else "未配置 API Key（编辑填入）")
    return check_availability(e.id, settings)


def service_status_view(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """「服务」页签的状态视图（每类服务：端点行、优先级、启停、可用性、当前生效项）。

    生效项 = 优先级第 1 位的**可用**端；第 1 位不可用则顺延到下一个可用者（降级发生在这里，
    并以 degraded_from 留痕）。模型推理类仍只读展示（编辑在「模型」页签）。
    """
    svc = ServiceEndpointService(conn)
    out: list[dict[str, Any]] = []
    for cat in SERVICE_CATEGORIES:
        all_rows = svc.rows(cat.key)
        enabled = [e for e in all_rows if e.enabled]
        items: list[dict[str, Any]] = []
        for e in all_rows:
            available, reason = endpoint_available(e, settings)
            items.append(
                {
                    "id": e.id,
                    "label": e.label,
                    "kind": e.kind,
                    "available": available,
                    "reason": reason,
                    "enabled": e.enabled,
                    "builtin": e.builtin,
                    "key_masked": _mask_key(e.api_key) if e.kind == "cloud" else None,
                    "base_url": e.base_url,
                    "model": e.model,
                    "order": enabled.index(e) if e.enabled else None,
                }
            )
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

    # 模型推理（只读）：候选 = model_backend 表的行；状态只看配置齐缺，不做网络探测。
    # 编辑在「模型」页签 —— 同一份数据不设两个编辑入口（视图形状与端点行对齐，前端零分叉）。
    from rolecard_agent.core.model_settings import ModelSettingsService, client_style

    ms = ModelSettingsService(conn)
    default = ms.default_backend() or settings.model_default
    model_items: list[dict[str, Any]] = []
    for i, row in enumerate(ms.list_backends()):
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
                "order": i,
            }
        )
    out.append(
        {
            "key": "models",
            "title": "模型推理（对话与抽取）",
            "hint": "增删与 key 在「模型」页签；这里只读展示",
            "effective": default,
            "effective_kind": next(
                ("local" if client_style(str(b.get("provider", ""))) == "native" else "cloud"
                 for b in ms.list_backends() if b["name"] == default),
                None,
            ),
            "degraded_from": None,
            "readonly": True,
            "candidates": model_items,
        }
    )
    return {"services": out}
