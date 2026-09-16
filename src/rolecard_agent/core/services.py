"""运行时服务目录与策略 —— 「服务」子页签的数据层。

把散在环境变量里的三类**非模型**服务（OCR / 语义嵌入 / 检索重排）的候选、默认优先级、
启停策略收敛到这里；模型推理类的策略已由 `model_backend` 表 + 回退链承担，不在此重复建模
（服务页对模型类只做状态展示）。

三条设计规则：

  1. **候选在代码里定义**（CANDIDATES），DB 只存操作员的排序与启停 —— "存在哪些候选"
     是一次 commit，不是一行数据（与 domains/registry.py 的显式注册哲学一致）。
  2. **默认优先级按服务类型分别设定，不做"本地一律优先"的一刀切**：
     模型 / OCR 本地优先（隐私 + 免费 + 数据不出机）；语义嵌入云端优先（本地 Hash 兜底
     只有关键词命中，语义检索质量大打折扣）。
  3. **可用性分两级**：`check_availability` 是廉价静态检测（配置齐缺 + 本地文件探活），
     页面打开即跑；真正的远程调用由调用方按需发起（深度检测），避免一次 UI 刷新打爆
     外部 API。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rolecard_agent.config import Settings

# ---------------------------------------------------------------- 服务候选定义


@dataclass(frozen=True, slots=True)
class ServiceCandidate:
    """一个可选的服务实现。`local` 表示数据不出本机的实现。"""

    id: str
    label: str
    kind: str  # local | cloud
    needs: str  # 可用前提的人话描述（状态徽标的 tooltip）


@dataclass(frozen=True, slots=True)
class ServiceCategory:
    """一类服务（OCR / 嵌入 / 重排）：候选列表 + 默认首选 + 兜底说明。"""

    key: str
    title: str
    hint: str
    default_preferred: str
    candidates: tuple[ServiceCandidate, ...]


def _ocr_candidates() -> tuple[ServiceCandidate, ...]:
    return (
        ServiceCandidate("paddle", "PaddleOCR（独立进程）", "local", "本地 .venv-ocr 已安装"),
        ServiceCandidate("cloud", "OCR.space（云端）", "cloud", "已配置 OCR_API_KEY"),
    )


def _embedding_candidates() -> tuple[ServiceCandidate, ...]:
    return (
        ServiceCandidate(
            "siliconflow", "SiliconFlow bge-m3", "cloud", "已配置 SILICONFLOW_API_KEY"
        ),
        ServiceCandidate("hash", "Hash 离线", "local", "始终可用（仅关键词命中，无语义）"),
    )


def _rerank_candidates() -> tuple[ServiceCandidate, ...]:
    return (
        ServiceCandidate(
            "siliconflow", "SiliconFlow 重排", "cloud", "已配置 SILICONFLOW_API_KEY"
        ),
        ServiceCandidate("off", "关闭（按向量序）", "local", "始终可用"),
    )


SERVICE_CATEGORIES: tuple[ServiceCategory, ...] = (
    ServiceCategory(
        "ocr",
        "OCR（图片文字识别）",
        "本地优先：图片不出本机；本地不可用时才发往云端（有隐私代价）",
        "paddle",
        _ocr_candidates(),
    ),
    ServiceCategory(
        "embedding",
        "语义嵌入（知识库检索）",
        "云端优先：本地 Hash 兜底只有关键词命中，语义检索质量大打折扣",
        "siliconflow",
        _embedding_candidates(),
    ),
    ServiceCategory(
        "rerank",
        "检索重排（质量增强）",
        "云端增强：失败自动回退向量序，不会变成可用性故障",
        "siliconflow",
        _rerank_candidates(),
    ),
)

_CATEGORY_KEYS = {c.key for c in SERVICE_CATEGORIES}


def category(key: str) -> ServiceCategory:
    for c in SERVICE_CATEGORIES:
        if c.key == key:
            return c
    raise KeyError(f"unknown service category: {key!r}")


# ---------------------------------------------------------------- 策略读写


class ServicePolicyService:
    """`service_policy` 表的读写 + 候选排序。

    空表 = 全部走代码默认（向后兼容：升级前保存过设置的老库行为不变）。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _row(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT service_key, preferred, disabled FROM service_policy WHERE service_key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None

    def effective(self, key: str) -> tuple[str, frozenset[str]]:
        """返回 (生效首选候选 id, 禁用集合)。无策略记录时代码默认。"""
        cat = category(key)
        row = self._row(key)
        preferred = cat.default_preferred
        disabled: frozenset[str] = frozenset()
        if row is not None:
            raw = row["preferred"]
            if raw and any(c.id == raw for c in cat.candidates):
                preferred = str(raw)  # 策略里的首选必须仍是合法候选，否则回代码默认
            try:
                disabled = frozenset(json.loads(str(row["disabled"] or "[]")))
            except json.JSONDecodeError:
                disabled = frozenset()
        valid_disabled = {d for d in disabled if any(c.id == d for c in cat.candidates)}
        # 首选被禁用时，在剩余候选里保持"本地优先于云端"的次序兜底
        if preferred in valid_disabled:
            ordered = sorted(
                (c for c in cat.candidates if c.id not in valid_disabled),
                key=lambda c: (c.kind != "local", c.id),
            )
            preferred = ordered[0].id if ordered else cat.default_preferred
        return preferred, frozenset(valid_disabled)

    def ordered_candidates(self, key: str) -> list[ServiceCandidate]:
        """按「生效首选在前 + 本地先于云端」排序后的候选（供工厂与状态视图消费）。"""
        cat = category(key)
        preferred, disabled = self.effective(key)
        pool = [c for c in cat.candidates if c.id not in disabled]
        pool.sort(key=lambda c: (c.id != preferred, c.kind != "local", c.id))
        return pool

    def save(self, key: str, *, preferred: str, disabled: list[str]) -> None:
        """保存一类服务的策略。非法候选 / 禁用全部候选 / 禁用首选 都要大声拒绝。"""
        cat = category(key)
        if preferred not in {c.id for c in cat.candidates}:
            raise ValueError(f"未知候选 {preferred!r}（服务 {key}）")
        valid = {c.id for c in cat.candidates}
        bad = [d for d in disabled if d not in valid]
        if bad:
            raise ValueError(f"未知候选 {bad}（服务 {key}）")
        if set(disabled) >= valid:
            raise ValueError(f"服务 {key} 不能禁用全部候选 —— 至少保留一个可用实现。")
        self._conn.execute(
            "INSERT INTO service_policy (service_key, preferred, disabled, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(service_key) DO UPDATE SET preferred = excluded.preferred, "
            "  disabled = excluded.disabled, updated_at = CURRENT_TIMESTAMP",
            (key, preferred, json.dumps(sorted(set(disabled)))),
        )
        self._conn.commit()


# ---------------------------------------------------------------- 可用性检测


def check_availability(candidate_id: str, settings: Settings) -> tuple[bool, str]:
    """廉价静态检测：只看配置齐缺与本地文件探活，**绝不发网络请求**。

    返回 (是否可用, 人话原因)。远程真连通性属于"深度检测"，由端点按需发起。
    """
    if candidate_id == "paddle":
        from rolecard_agent.rag.parser import _default_ocr_python

        exe = settings.ocr_python or _default_ocr_python()
        if not exe or not Path(exe).exists():
            return False, "未找到独立 OCR 解释器（.venv-ocr）"
        return True, f"就绪：{Path(exe).name}"
    if candidate_id == "cloud_ocr" or candidate_id == "cloud":
        return bool(settings.ocr_api_key), (
            "已配置 OCR_API_KEY" if settings.ocr_api_key else "未配置 OCR_API_KEY"
        )
    if candidate_id == "siliconflow":
        from rolecard_agent.rag.retriever import _find_embedding_key

        return bool(_find_embedding_key(settings)), (
            "已配置 API Key" if _find_embedding_key(settings) else "未配置 SILICONFLOW_API_KEY"
        )
    if candidate_id in {"hash", "off"}:
        return True, "始终可用"
    return False, f"未知候选 {candidate_id!r}"


def service_status_view(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """「服务」页签的状态视图（每类服务：候选、顺序、启停、可用性、当前生效项）。

    组装在这里而不是 router：检测逻辑与策略语义都属于数据层，router 只做 HTTP 翻译。
    模型推理类**只读展示**（后端 CRUD 与回退链编辑在「模型」页签 —— 同一份数据不设两个
    编辑入口），候选从 `model_backend` 表动态读，不进 `SERVICE_CATEGORIES`。
    """
    out: list[dict[str, Any]] = []
    svc = ServicePolicyService(conn)
    for cat in SERVICE_CATEGORIES:
        preferred, disabled = svc.effective(cat.key)
        ordered = svc.ordered_candidates(cat.key)
        items: list[dict[str, Any]] = []
        for cand in cat.candidates:
            available, reason = check_availability(cand.id, settings)
            items.append(
                {
                    "id": cand.id,
                    "label": cand.label,
                    "kind": cand.kind,
                    "needs": cand.needs,
                    "available": available,
                    "reason": reason,
                    "disabled": cand.id in disabled,
                    "preferred": cand.id == preferred,
                    "order": ordered.index(cand) if cand in ordered else None,
                }
            )
        # 生效项 = 首选中可用者；首选不可用则按顺序取第一个可用者（降级发生在这里）。
        effective = next(
            (c for c in ordered if c.id == preferred and check_availability(c.id, settings)[0]),
            None,
        )
        fallback_from: str | None = None
        if effective is None:
            for c in ordered:
                avail, _ = check_availability(c.id, settings)
                if avail:
                    effective = c
                    break
        if effective is not None and effective.id != preferred:
            fallback_from = preferred
        out.append(
            {
                "key": cat.key,
                "title": cat.title,
                "hint": cat.hint,
                "preferred": preferred,
                "disabled": sorted(disabled),
                "degraded_from": fallback_from,
                "effective": effective.id if effective else None,
                "effective_kind": effective.kind if effective else None,
                "candidates": items,
            }
        )

    # 模型推理（只读）：候选 = model_backend 表的行；状态只看配置齐缺，不做网络探测。
    from rolecard_agent.core.model_settings import ModelSettingsService

    ms = ModelSettingsService(conn)
    default = ms.default_backend() or settings.model_default
    model_items: list[dict[str, Any]] = []
    for row in ms.list_backends():
        is_local = str(row.get("provider", "")).lower() == "ollama"
        model_items.append(
            {
                "id": row["name"],
                "label": f"{row['name']} · {row['model']}",
                "kind": "local" if is_local else "cloud",
                "needs": "本地 Ollama 运行中" if is_local else "已配置 API Key",
                "available": is_local or bool(row.get("has_key")),
                "reason": "本地端点（运行状态见深度检测）" if is_local else "已配置 key",
                "disabled": False,
                "preferred": str(row["name"]) == default,
                "order": row.get("sort_order"),
            }
        )
    out.insert(
        0,
        {
            "key": "models",
            "title": "模型推理（对话 · 工具）",
            "hint": "顺序即回退链；增删与 key 编辑在「模型」页签",
            "preferred": default,
            "disabled": [],
            "degraded_from": None,
            "effective": default,
            "effective_kind": None,
            "candidates": model_items,
        },
    )
    return {"services": out}


__all__ = [
    "SERVICE_CATEGORIES",
    "ServiceCandidate",
    "ServiceCategory",
    "ServicePolicyService",
    "category",
    "check_availability",
    "service_status_view",
]
