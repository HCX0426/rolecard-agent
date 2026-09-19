"""健康报告的结构化抽取（v2.3）：把报告文本变成 `medical_index` 里的指标行。

与「知识库」的分工（见 docs/前端设计.md 的 IA 一节）：
  * 知识库让 AI 能**读**原文（语义检索，容错但不会算术）；
  * 抽取让 AI 能**算**数值（结构化指标 → 领域工具可查询/对比）。
两者刻意分开：SQL 做不了语义匹配，向量检索做不了精确算术。

## 三层校验（对齐行业做法，从便宜到贵）

  ① **schema 约束抽取**：强类型 JSON，缺失必须是 `null`（不许编），每项带原文片段与自评置信；
  ② **确定性校验**（零成本，最先做）：指标名/取值是否齐、数值量级、单位长度、日期是否合理；
     与历史值差异过大单独作为「软标记」（降低置信，但不直接丢弃）；
  ③ **原文锚定 grounding**：`raw_text` 必须能在原文里找到 —— 这一层挡掉大部分幻觉；
  ④ **第二模型交叉验证**（用户选的「AI 校对」）：换个 provider 的后端独立再抽一遍，逐字段比对。
     只有一个后端时降级为「同模型复查原文」，并把模式如实报告为 `self`（弱校对，界面必须说明）。

## 写入铁律

  * 一律 `is_verified=0`：没人核实过，界面必须显示【AI 抽取 · 未人工校验】；
  * **只有双方一致的项才进指标表**；不一致 / 只单方看到 / 未锚定 / 硬校验失败的项，
    一律进 `conflicts` 交给人确认，**不静默取一个**；
  * 幂等由调用方按 ingestion task 保证（`medical_report.ingestion_task_id`）。

本模块**不碰数据库**，模型调用通过 `ModelInvoker` 注入 —— 因此可以用假模型做单测。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from rolecard_agent.config import Settings
from rolecard_agent.core.observability import TraceEvent
from rolecard_agent.core.text import text_of

# 一次模型调用：prompt -> 原始文本回复。注入式，便于单测。
ModelInvoker = Callable[[str], str]

_MAX_INPUT_CHARS = 12000
_MAX_ABS_VALUE = 1e6
_MAX_UNIT_LEN = 12
_DRIFT_RATIO = 0.5  # 与历史值差异超过 50% → 软标记「可疑突变」


class ExtractError(Exception):
    """抽取失败（模型不可用 / 没返回 JSON / JSON 不合 schema）。可读原因，无栈。"""


class ExtractConfigError(ExtractError):
    """抽取后端配置错误（比如指定了不存在的后端名）—— 要大声，不要静默降级。"""


# ---------------------------------------------------------------- schema

_SCHEMA_HINT = """{
  "report_type": "报告类型，如 腹部超声 / 血常规",
  "check_time": "检查日期 YYYY-MM-DD；找不到就 null",
  "institution": "机构名；找不到就 null",
  "indices": [
    {
      "index_name": "指标名，如 结石直径 / 空腹血糖",
      "index_value": 数值（数字类型）或 null,
      "value_text": "非数值描述（如 未见异常）或 null",
      "unit": "单位或 null",
      "ref_range": "参考区间或 null",
      "raw_text": "该项在原文里的原句片段（必须逐字来自原文）",
      "confidence": "high | medium | low"
    }
  ]
}"""

_SYSTEM = (
    "你是医疗报告结构化抽取器。只输出 JSON，不要解释、不要 markdown 代码块。"
    "只抽取原文中确实存在的信息；原文没有的字段必须返回 null，绝不猜测或补全。"
)


class ExtractedIndex(BaseModel):
    """一项抽出来的指标。取值宽松（模型可能给字符串数字），由校验层负责把关。"""

    index_name: str = ""
    index_value: float | None = None
    value_text: str | None = None
    unit: str | None = None
    ref_range: str | None = None
    raw_text: str = ""
    confidence: str = "medium"


class ExtractedReport(BaseModel):
    report_type: str = ""
    check_time: str | None = None
    institution: str | None = None
    indices: list[ExtractedIndex] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExtractorPlan:
    """用哪两个后端抽取。`mode` 必须如实反映校对强度。"""

    primary: str
    verifier: str | None
    mode: str  # "cross"（不同 provider）| "self"（同模型复查，弱校对）| "off"（不做第二遍）


@dataclass(frozen=True, slots=True)
class Conflict:
    """没能通过校验 / 两次不一致的项 —— 交给人确认，绝不静默入库。"""

    index_name: str
    reason: str
    primary: ExtractedIndex | None = None
    verify: ExtractedIndex | None = None


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    report_type: str
    check_time: str
    institution: str | None
    agreed: tuple[ExtractedIndex, ...]
    conflicts: tuple[Conflict, ...]
    mode: str
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------- 后端选择


def plan_extractors(settings: Settings) -> ExtractorPlan | None:
    """按配置挑抽取后端：**本地优先**（provider=ollama），可显式指定；校对方尽量换 provider。

    返回 None 表示一个后端都没有（调用方应如实告诉用户"没有可用的模型"）。
    """
    backends = settings.model_backends
    if not backends:
        return None

    want = (settings.extract_backend or "auto").lower()
    if want != "auto":
        if want not in backends:
            known = ", ".join(sorted(backends))
            raise ExtractConfigError(f"未知的抽取后端 {want!r}；可用：{known}")
        primary = want
    else:
        local = [n for n, b in backends.items() if (b.provider or "").lower() == "ollama"]
        primary = sorted(local)[0] if local else settings.model_default
    if primary not in backends:  # 默认后端被删掉了：退到任意一个可用后端
        primary = sorted(backends)[0]

    if (settings.extract_verify or "auto").lower() == "off":
        return ExtractorPlan(primary=primary, verifier=None, mode="off")

    p_provider = (backends[primary].provider or "").lower()
    others = sorted(
        n for n, b in backends.items() if n != primary and (b.provider or "").lower() != p_provider
    )
    if others:
        return ExtractorPlan(primary=primary, verifier=others[0], mode="cross")
    # 只有一个 provider：降级为同模型复查，并**如实标注**为弱校对
    return ExtractorPlan(primary=primary, verifier=primary, mode="self")


def make_invoker(settings: Settings, backend_name: str) -> ModelInvoker:
    """构建单后端调用器。

    **刻意不用 `build_model`（带 fallback）**：fallback 会让"第二个模型"在主模型失败时
    悄悄变成同一个模型，交叉验证就名存实亡 —— 那比不做校对更危险（假的安心）。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from rolecard_agent.core.graph import _init_model

    try:
        model = _init_model(settings, backend_name)
    except Exception as exc:  # noqa: BLE001 - 缺 provider 包 / 配置错 → 可读的抽取失败
        raise ExtractError(f"无法初始化抽取后端 {backend_name!r}：{exc}") from exc

    def invoke(prompt: str) -> str:
        try:
            resp = model.invoke([SystemMessage(content=_SYSTEM), HumanMessage(content=prompt)])
            return text_of(resp)
        except ExtractError:
            raise
        except Exception as exc:
            raise ExtractError(f"模型调用失败：{exc}") from exc

    return invoke


# ---------------------------------------------------------------- 抽取（①）


def _user_prompt(text: str) -> str:
    clipped = text[:_MAX_INPUT_CHARS]
    note = "" if len(text) <= _MAX_INPUT_CHARS else "\n（原文过长，已截断）"
    return f"""从下面这份体检/检查报告里抽取结构化信息，输出 JSON：

{_SCHEMA_HINT}

规则：
- index_value 与 value_text 至少有一个非 null（数值填 index_value，描述填 value_text）。
- raw_text 必须逐字取自原文（用于原文核对），不要改写措辞。
- 报告里没有的指标不要编；没有的字段填 null。
{note}
原文：
---
{clipped}
---"""


def _parse_json(raw: str) -> dict[str, Any]:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ExtractError("模型没有返回 JSON（可能被安全策略拦截或返回了纯文本）")
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ExtractError(f"模型返回的 JSON 无法解析：{exc}") from exc


def _run_pass(invoke: ModelInvoker, text: str) -> ExtractedReport:
    """一次抽取：调用模型 → 解析 JSON → 校验 schema。失败一律转成可读的 ExtractError。"""
    try:
        raw = invoke(_user_prompt(text))
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001 - 模型未启动 / 网络失败 / 超时都归这里
        raise ExtractError(f"模型调用失败：{exc}") from exc
    try:
        return ExtractedReport.model_validate(_parse_json(raw))
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001 - schema 不匹配
        raise ExtractError(f"模型返回的数据不符合抽取 schema：{exc}") from exc


# ---------------------------------------------------------------- 校验（②③）


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _num_variants(v: float) -> list[str]:
    return [f"{v:g}", f"{v:.1f}", f"{v:.2f}"]


def _grounded(item: ExtractedIndex, source_norm: str) -> bool:
    """原文锚定：片段能原样找到最好；找不到时退一步要求「指标名 + 取值」都在原文里。

    完全不看原文就相信模型，是把幻觉直接写进档案；但过严又会把正常抽取全拒掉，
    所以给一个宽松兜底。
    """
    snippet = _norm(item.raw_text)
    if len(snippet) >= 2 and snippet in source_norm:
        return True
    name = _norm(item.index_name)
    if not name or name not in source_norm:
        return False
    if item.index_value is not None:
        return any(_norm(t) in source_norm for t in _num_variants(item.index_value))
    text_val = _norm(item.value_text or "")
    return bool(text_val) and text_val in source_norm


def _hard_issues(item: ExtractedIndex, *, report_date: date | None) -> list[str]:
    issues: list[str] = []
    if not item.index_name.strip():
        issues.append("缺少指标名")
    if item.index_value is None and not (item.value_text or "").strip():
        issues.append("既无数值也无文本值")
    if item.index_value is not None and abs(item.index_value) >= _MAX_ABS_VALUE:
        issues.append("数值量级异常")
    if item.unit and len(item.unit) > _MAX_UNIT_LEN:
        issues.append("单位字段过长（可能是模型把整句塞进来了）")
    if report_date is None:
        issues.append("报告缺少可解析的检查日期")
    return issues


def _soft_flags(item: ExtractedIndex, history: Mapping[str, float] | None) -> list[str]:
    flags: list[str] = []
    if item.confidence.lower() == "low":
        flags.append("模型自评低置信")
    if item.index_value is not None and history:
        prev = history.get(item.index_name.strip())
        if prev and abs(prev) > 1e-9 and abs(item.index_value - prev) / abs(prev) >= _DRIFT_RATIO:
            flags.append(f"与历史值差异过大（上次 {prev:g}）")
    return flags


def _parse_date(raw: str | None) -> date | None:
    """接受 YYYY-MM-DD / YYYY/MM/DD / YYYY-MM（模型给"YYYY-MM"是常见情况）。"""
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _date_plausible(d: date | None) -> bool:
    if d is None:
        return False
    today = date.today()
    return d <= today and d.year >= today.year - 50


def _same_value(a: ExtractedIndex, b: ExtractedIndex) -> bool:
    if a.index_value is not None and b.index_value is not None:
        return abs(a.index_value - b.index_value) < 1e-6
    return _norm(a.value_text or "") == _norm(b.value_text or "")


# ---------------------------------------------------------------- 编排


def extract_health_report(
    *,
    text: str,
    primary: ModelInvoker,
    verifier: ModelInvoker | None = None,
    mode: str = "off",
    known_history: Mapping[str, float] | None = None,
) -> ExtractionOutcome:
    """两次抽取 + 三层校验 + 合并。**只把双方一致的项放进 `agreed`**。"""
    source_norm = _norm(text)
    first = _run_pass(primary, text)

    second: ExtractedReport | None = None
    if verifier is not None and mode != "off":
        second = _run_pass(verifier, text)

    notes: list[str] = []
    report_date = _parse_date(first.check_time)
    if not _date_plausible(report_date):
        notes.append(f"检查日期不可用（模型给出 {first.check_time!r}），无法写入结构化数据")

    agreed: list[ExtractedIndex] = []
    conflicts: list[Conflict] = []

    second_by_key = {_norm(i.index_name): i for i in (second.indices if second else [])}

    for item in first.indices:
        name = item.index_name.strip()
        hard = _hard_issues(item, report_date=report_date)
        if hard:
            conflicts.append(Conflict(name, "；".join(hard), primary=item))
            continue
        if not _grounded(item, source_norm):
            conflicts.append(Conflict(name, "原文里找不到出处（疑似幻觉）", primary=item))
            continue
        if second is None:
            agreed.append(item)
            continue
        other = second_by_key.get(_norm(name))
        if other is None:
            conflicts.append(Conflict(name, "第二次识别没看到这一项", primary=item))
            continue
        if not _same_value(item, other):
            conflicts.append(Conflict(name, "两次识别取值不一致", primary=item, verify=other))
            continue
        flags = _soft_flags(item, known_history)
        if flags:
            conflicts.append(Conflict(name, "；".join(flags), primary=item, verify=other))
            continue
        agreed.append(item)

    for key, item in second_by_key.items():
        if key not in {_norm(i.index_name) for i in first.indices}:
            conflicts.append(
                Conflict(item.index_name.strip(), "第一次识别没看到这一项", verify=item)
            )

    if mode == "self" and second is not None:
        notes.append("校对模式为「同模型复查」：同一模型的自查，挡不住双方共同看错，属弱校对")
    if second is None:
        notes.append("未做第二遍识别（校对已关闭）：只做了确定性与原文锚定校验")

    return ExtractionOutcome(
        report_type=first.report_type.strip(),
        check_time=(report_date.isoformat() if report_date else ""),
        institution=(first.institution or None),
        agreed=tuple(agreed),
        conflicts=tuple(conflicts),
        mode=mode,
        notes=tuple(notes),
    )


# 探活结果的短 TTL 缓存（秒）。存在的理由（审查报告 M6）：探活本身是**一次真实的模型
# 调用**，而抽取是低频动作 —— 每次都探会让"省一次注定失败的昂贵调用"变成"每次固定多
# 一次调用"，本地 Ollama 还要额外吃一次模型换载（2~5s）。
# 缓存的是**成功**：60 秒内主后端被证明可用就不重复探；期间它挂了也只是这次抽取失败，
# 与"没做探活"的旧行为一致，不会更差。
_PROBE_TTL_SECONDS = 60.0
_probe_ok_at: dict[str, float] = {}
_probe_lock = threading.Lock()


def _probe_key(settings: Settings, backend_name: str) -> str:
    """缓存键 = 后端名 + 连接三要素。改了 base_url/model 就该重新探，不能沿用旧结论。"""
    try:
        b = settings.backend(backend_name)
    except KeyError:
        return f"{backend_name}|missing"
    return f"{backend_name}|{b.provider}|{b.base_url or ''}|{b.model}"


def _recently_probed(settings: Settings, backend_name: str) -> bool:
    with _probe_lock:
        at = _probe_ok_at.get(_probe_key(settings, backend_name))
    return at is not None and (time.monotonic() - at) < _PROBE_TTL_SECONDS


def _remember_probe(settings: Settings, backend_name: str) -> None:
    with _probe_lock:
        _probe_ok_at[_probe_key(settings, backend_name)] = time.monotonic()


def _failover_primary(settings: Settings, plan: ExtractorPlan) -> ExtractorPlan:
    """本地后端连不上时，自动降级到 verifier（云端）做主抽取，校对降级为 self。

    "本地优先"是个偏好，不是硬约束 —— Ollama 没跑时，不 502 而是用云端完成抽取。
    校对降级为 self（同一个后端自查 = 弱校对）：因为原主后端已经挂了，
    不能用它做校对。用最小说探（"回复 ok"）探活，成本极低但省掉一次注定失败的昂贵调用。

    探活结果有 60 秒 TTL 缓存（见 `_PROBE_TTL_SECONDS`）：这是一个**真实**的模型调用，
    不该在每次抽取时都付一遍。
    """
    try:
        if not _recently_probed(settings, plan.primary):
            make_invoker(settings, plan.primary)("回复 ok")
            _remember_probe(settings, plan.primary)
        return plan  # 主后端可用，不降级
    except Exception:  # noqa: BLE001 - 探活失败 = 后端不可用（含 httpx.ConnectError）
        pass
    # 主后端不可用 → 尝试 verifier
    if plan.verifier and plan.verifier != plan.primary:
        try:
            make_invoker(settings, plan.verifier)("回复 ok")
            # verifier 能通 → 用它做主抽取；校对降级为 self（它不能校对自己）
            return ExtractorPlan(primary=plan.verifier, verifier=None, mode="self")
        except Exception:  # noqa: BLE001
            pass
    return plan  # 原样返回（两个都挂了），extract_health_report 会报错


def run_extraction(
    *,
    text: str,
    settings: Settings,
    source: str = "parsed",  # noqa: ARG001 - 由调用方写入时使用
    known_history: Mapping[str, float] | None = None,
    tracer: object | None = None,
) -> ExtractionOutcome | None:
    """按配置跑完整抽取。返回 None = 没有可用后端（调用方应如实告知，不要假装成功）。"""
    # 顺序不能反（审查报告 M6）：`_failover_primary` 会解引用 `plan.primary`，把 None
    # 传进去会抛 AttributeError —— 旧代码里紧随其后的 `if plan is None` 因此是**死代码**，
    # 本该返回"没有可用模型"的路径变成 500。
    plan = plan_extractors(settings)
    if plan is None:
        return None
    plan = _failover_primary(settings, plan)
    outcome = extract_health_report(
        text=text,
        primary=make_invoker(settings, plan.primary),
        verifier=(make_invoker(settings, plan.verifier) if plan.verifier else None),
        mode=plan.mode,
        known_history=known_history,
    )
    if tracer is not None and hasattr(tracer, "emit"):
        tracer.emit(
            TraceEvent(
                event="health_extraction",
                detail={
                    "primary": plan.primary,
                    "verifier": plan.verifier,
                    "mode": plan.mode,
                    "agreed": len(outcome.agreed),
                    "conflicts": len(outcome.conflicts),
                },
            )
        )
    return outcome


def to_index_payload(item: ExtractedIndex, *, source: str) -> dict[str, object]:
    """把抽出来的一项转成 `create_report` 需要的指标行（一律未人工校验）。"""
    return {
        "index_name": item.index_name.strip(),
        "index_value": item.index_value,
        "value_text": (item.value_text or None),
        "unit": (item.unit or None),
        "ref_range": (item.ref_range or None),
        "is_verified": False,  # 铁律：AI 抽取的永远不算已核实
        "source": source,
        "raw_text": (item.raw_text or None),
    }
