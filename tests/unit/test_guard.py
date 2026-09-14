"""Rule-layer tests for core/guard.py. No model needed - this is the easy layer to test."""

from __future__ import annotations

import pytest

from rolecard_agent.core import guard
from rolecard_agent.core.prompts import GLOBAL_SAFETY_PROMPT


def test_plain_summary_is_allowed() -> None:
    result = guard.check("你 2026 年 3 月的结石直径是 6.0 mm，单位毫米。【未经人工校验】")
    assert result.allowed is True
    assert result.reasons == ()


def test_diagnosis_assertion_is_blocked() -> None:
    result = guard.check("你得了胆囊结石，不用太担心。")
    assert result.allowed is False
    assert "diagnosis_asserted" in result.reasons
    assert result.rewritten  # a safe replacement is always offered


def test_medication_advice_with_second_person_is_blocked() -> None:
    result = guard.check("根据这个数值，建议你服用熊去氧胆酸，先吃两个月看看。")
    assert result.allowed is False
    assert "medication_advice" in result.reasons


def test_severity_judgement_is_blocked() -> None:
    result = guard.check("你的指标偏高，情况比较严重，建议尽快就医。")
    assert result.allowed is False
    assert "severity_judgement" in result.reasons


def test_safety_prompt_itself_is_not_blocked() -> None:
    """The rule text necessarily mentions 诊断 / 用药 / 手术.

    This is the regression guard for the whole rule design: if the patterns were not anchored
    on a second-person subject, the guard would block the very prompt that defines the rules
    (and then block every refusal written to explain them).
    """
    result = guard.check(GLOBAL_SAFETY_PROMPT)
    assert result.allowed is True, result.reasons


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_input_is_allowed(text: object) -> None:
    assert guard.check(text).allowed is True  # type: ignore[arg-type]


def test_fails_closed_when_the_guard_itself_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard that fails open is worse than no guard: it creates false confidence."""

    class Exploding:
        def search(self, _text: str) -> object:
            raise RuntimeError("boom")

    monkeypatch.setattr(guard, "_ADVICE_PATTERNS", (("broken", Exploding()),))
    result = guard.check("一段普通的话")
    assert result.allowed is False
    assert result.reasons == ("guard_error",)


def test_blocked_response_does_not_itself_trip_the_guard() -> None:
    assert guard.check(guard.BLOCKED_RESPONSE).allowed is True
