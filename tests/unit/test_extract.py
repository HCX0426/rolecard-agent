"""domains/health/extract 单测：schema 解析 → 三层校验 → 双模型合并。

全部用**假 invoker**（返回固定 JSON）—— 离线、不依赖真模型，测的是"校验与合并逻辑"，
不是模型本身（这条项目铁律在这里同样适用）。

Traceability: US-4（未校验数据必须带标记）、US-5（可观测）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.domains.health.extract import (
    ExtractConfigError,
    ExtractError,
    extract_health_report,
    plan_extractors,
)

SOURCE = (
    "腹部超声报告\n检查日期：2026-03-12　机构：市第一医院\n"
    "胆囊结石，结石直径 6.1 mm（参考 <5mm）。\n建议三个月后复查。"
)


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "report_type": "腹部超声",
        "check_time": "2026-03-12",
        "institution": "市第一医院",
        "indices": [
            {
                "index_name": "结石直径",
                "index_value": 6.1,
                "value_text": None,
                "unit": "mm",
                "ref_range": "<5mm",
                "raw_text": "结石直径 6.1 mm",
                "confidence": "high",
            }
        ],
    }
    base.update(over)
    return base


def _invoke(payload: dict[str, Any]):
    def call(_prompt: str) -> str:
        return json.dumps(payload, ensure_ascii=False)

    return call


def _raw(text: str):
    def call(_prompt: str) -> str:
        return text

    return call


def _run(primary, verifier=None, *, mode: str = "cross", **kw: Any):  # noqa: ANN001, ANN201
    """短调用封装：测试里 90% 的调用都是"同一份原文 + 两个 invoker"。"""
    return extract_health_report(
        text=SOURCE, primary=primary, verifier=verifier, mode=mode, **kw
    )


# -- 抽取 + 合并 ------------------------------------------------------------------------


def test_two_passes_agree_goes_to_agreed() -> None:
    out = extract_health_report(
        text=SOURCE, primary=_invoke(_payload()), verifier=_invoke(_payload()), mode="cross"
    )
    assert [i.index_name for i in out.agreed] == ["结石直径"]
    assert out.agreed[0].index_value == 6.1
    assert out.conflicts == ()
    assert out.check_time == "2026-03-12"
    assert out.mode == "cross"


def test_disagreeing_values_become_conflict_not_silent_pick() -> None:
    """两次不一致 → 进 conflicts，**绝不静默取一个**（用户明确的选择）。"""
    other = _payload()
    other["indices"][0]["index_value"] = 7.2
    out = extract_health_report(
        text=SOURCE, primary=_invoke(_payload()), verifier=_invoke(other), mode="cross"
    )
    assert out.agreed == ()
    assert len(out.conflicts) == 1
    assert "不一致" in out.conflicts[0].reason
    assert out.conflicts[0].primary is not None and out.conflicts[0].verify is not None


def test_item_missing_in_second_pass_is_conflict() -> None:
    out = extract_health_report(
        text=SOURCE,
        primary=_invoke(_payload()),
        verifier=_invoke(_payload(indices=[])),
        mode="cross",
    )
    assert out.agreed == ()
    assert "第二次识别没看到" in out.conflicts[0].reason


def test_item_only_in_second_pass_is_conflict() -> None:
    out = extract_health_report(
        text=SOURCE,
        primary=_invoke(_payload(indices=[])),
        verifier=_invoke(_payload()),
        mode="cross",
    )
    assert out.agreed == ()
    assert "第一次识别没看到" in out.conflicts[0].reason


def test_off_mode_writes_agreed_and_says_so() -> None:
    out = extract_health_report(text=SOURCE, primary=_invoke(_payload()), verifier=None, mode="off")
    assert len(out.agreed) == 1
    assert any("未做第二遍识别" in n for n in out.notes)


def test_self_mode_is_labelled_weak() -> None:
    """同模型复查是弱校对 —— 必须在 notes 里如实说明，不能让用户以为是交叉验证。"""
    out = extract_health_report(
        text=SOURCE, primary=_invoke(_payload()), verifier=_invoke(_payload()), mode="self"
    )
    assert any("弱校对" in n for n in out.notes)


# -- 三层校验 --------------------------------------------------------------------------


def test_ungrounded_item_is_rejected() -> None:
    """原文里找不到出处 → 判为疑似幻觉，不入库（第三层：原文锚定）。"""
    bad = _payload()
    bad["indices"][0]["index_name"] = "胆囊壁厚度"
    bad["indices"][0]["raw_text"] = "胆囊壁厚度 3 mm"
    bad["indices"][0]["index_value"] = 3.0
    out = _run(_invoke(bad), _invoke(bad))
    assert out.agreed == ()
    assert "找不到出处" in out.conflicts[0].reason


def test_grounding_accepts_name_plus_value_fallback() -> None:
    """片段没逐字对上，但「指标名 + 数值」都在原文里 → 放行（宽松兜底，避免全拒）。"""
    softer = _payload()
    softer["indices"][0]["raw_text"] = "（模型改写过的片段）"
    out = extract_health_report(
        text=SOURCE, primary=_invoke(softer), verifier=_invoke(softer), mode="cross"
    )
    assert len(out.agreed) == 1


def test_unparseable_check_time_blocks_write() -> None:
    """日期不可解析 → 写不了报告，必须如实说明（而不是编一个日期）。"""
    out = extract_health_report(
        text=SOURCE,
        primary=_invoke(_payload(check_time="去年三月")),
        verifier=_invoke(_payload(check_time="去年三月")),
        mode="cross",
    )
    assert out.check_time == ""
    assert out.agreed == ()
    assert any("检查日期不可用" in n for n in out.notes)


def test_missing_value_is_hard_issue() -> None:
    bad = _payload()
    bad["indices"][0]["index_value"] = None
    bad["indices"][0]["value_text"] = None
    out = _run(_invoke(bad), _invoke(bad))
    assert out.agreed == ()
    assert "既无数值也无文本值" in out.conflicts[0].reason


def test_absurd_magnitude_is_hard_issue() -> None:
    bad = _payload()
    bad["indices"][0]["index_value"] = 12345678.0
    bad["indices"][0]["raw_text"] = "结石直径 12345678 mm"
    out = extract_health_report(
        text=SOURCE + "\n结石直径 12345678 mm",
        primary=_invoke(bad),
        verifier=_invoke(bad),
        mode="cross",
    )
    assert out.agreed == ()
    assert "量级异常" in out.conflicts[0].reason


def test_history_drift_is_flagged_not_written() -> None:
    """与历史值差异过大 → 软标记进待确认（不直接丢，也不直接写）。"""
    out = extract_health_report(
        text=SOURCE,
        primary=_invoke(_payload()),
        verifier=_invoke(_payload()),
        mode="cross",
        known_history={"结石直径": 1.0},
    )
    assert out.agreed == ()
    assert "差异过大" in out.conflicts[0].reason


def test_low_confidence_is_flagged() -> None:
    low = _payload()
    low["indices"][0]["confidence"] = "low"
    out = _run(_invoke(low), _invoke(low))
    assert out.agreed == ()
    assert "低置信" in out.conflicts[0].reason


# -- 模型输出解析 ----------------------------------------------------------------------


def test_markdown_fenced_json_is_parsed() -> None:
    fenced = "```json\n" + json.dumps(_payload(), ensure_ascii=False) + "\n```"
    out = _run(_raw(fenced), _raw(fenced))
    assert len(out.agreed) == 1


def test_non_json_output_raises_extract_error() -> None:
    with pytest.raises(ExtractError, match="没有返回 JSON"):
        _run(_raw("抱歉，我无法处理"), mode="off")


def test_numeric_string_is_coerced() -> None:
    """模型常把数值写成字符串 —— 宽松 schema 负责转，而不是直接失败。"""
    loose = _payload()
    loose["indices"][0]["index_value"] = "6.1"
    out = _run(_invoke(loose), _invoke(loose))
    assert out.agreed[0].index_value == 6.1


# -- 后端选择 --------------------------------------------------------------------------


def _settings(**over: Any) -> Settings:
    backends = {
        "local": ModelBackend(model="qwen2.5:7b", provider="ollama"),
        "cloud": ModelBackend(
            model="deepseek-ai/DeepSeek-V4-Flash", provider="openai", api_key="sk-x"
        ),
    }
    return Settings(model_backends=backends, model_default="cloud", **over)


def test_plan_prefers_local_and_crosses_provider() -> None:
    plan = plan_extractors(_settings())
    assert plan is not None
    assert plan.primary == "local"  # 本地优先：报告内容不出本机
    assert plan.verifier == "cloud"
    assert plan.mode == "cross"


def test_plan_degrades_to_self_when_single_provider() -> None:
    one = Settings(
        model_backends={"only": ModelBackend(model="m", provider="openai", api_key="k")},
        model_default="only",
    )
    plan = plan_extractors(one)
    assert plan is not None
    assert plan.verifier == plan.primary
    assert plan.mode == "self"


def test_plan_respects_explicit_backend_and_off_switch() -> None:
    plan = plan_extractors(_settings(extract_backend="cloud", extract_verify="off"))
    assert plan is not None
    assert plan.primary == "cloud"
    assert plan.verifier is None
    assert plan.mode == "off"


def test_plan_rejects_unknown_backend_loudly() -> None:
    with pytest.raises(ExtractConfigError, match="未知的抽取模型配置"):
        plan_extractors(_settings(extract_backend="nope"))


def test_plan_returns_none_without_backends() -> None:
    assert plan_extractors(Settings(model_backends={})) is None


# -- 降级：本地后端挂了 → 自动换云端做主抽取 --------------------------------------------


def test_failover_to_verifier_when_primary_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama 连不上 → 自动降级到云端后端做主抽取，校对降级为 self。"""
    from rolecard_agent.domains.health.extract import (
        ExtractorPlan,
        _failover_primary,
    )

    plan = ExtractorPlan(primary="local", verifier="cloud", mode="cross")

    def fake_make(s: Settings, name: str):
        def invoke(prompt: str) -> str:
            if name == "local":
                raise ExtractError("模型调用失败：connection refused")
            return json.dumps(_payload(), ensure_ascii=False)
        return invoke

    monkeypatch.setattr(
        "rolecard_agent.domains.health.extract.make_invoker", fake_make
    )
    result = _failover_primary(_settings(), plan)
    assert result.primary == "cloud"
    assert result.mode == "self"  # 降级为弱校对（原主后端挂了，不能校对自己）
