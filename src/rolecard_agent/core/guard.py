"""Output-side guard: the SECOND layer of the safety design (A3, C10).

The safety design has two layers, and the project originally only had the first:

  Layer 1 - core/prompts.py GLOBAL_SAFETY_PROMPT
            A *soft* constraint. It raises the cost of a bad answer but the model can
            be talked out of it. Never treat it as a security boundary.

  Layer 2 - this module
            A *hard* gate. Every model response passes through check() before it reaches
            the caller. v1 implements the rule layer (deny-list + pattern match); an
            intent classifier can be added later behind the same interface.

Contract:
    check(text: str) -> GuardResult
        GuardResult.allowed   bool
        GuardResult.reasons   list[str]      why it was blocked
        GuardResult.rewritten str | None     optional sanitised replacement

Fail-closed: if the guard itself raises, the response is BLOCKED, not passed through.
A guard that fails open is worse than no guard at all, because it creates false
confidence.

Known limitation, stated rather than hidden: a rule layer has false positives. Every pattern
below is anchored on a second-person subject or an explicit diagnosis verb, so that the rule
text in GLOBAL_SAFETY_PROMPT itself (which necessarily mentions 诊断/用药/手术) is not
blocked. That anchoring is what makes the guard usable - and it is also the reason the guard
is a backstop, not the primary control.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BLOCKED_RESPONSE = (
    "抱歉，这项内容超出了我的职责范围。我只能查询、汇总、对比你已经存入档案的数据，"
    "不能做诊断、评估病情，也不能给出用药或治疗建议。"
    "如果你想了解某项指标在某次报告里的具体数值，我可以帮你查。"
)

# (reason, pattern). The reason is surfaced to the caller and logged; it is never a stack
# trace and never the raw match, which could itself contain sensitive text.
#
# `_NEG` is load-bearing. The rule text in GLOBAL_SAFETY_PROMPT necessarily says things like
# "不能推荐药物、手术方案" - without the negation guard, the advice patterns would block the
# very sentence that defines the rules. This was found by the first run of the test suite,
# which is why `test_safety_prompt_itself_is_not_blocked` exists.
_NEG = r"(?<!不能)(?<!不可)(?<!禁止)(?<!不得)(?<!无法)(?<!不要)"
_ADVICE_VERBS = r"(服用|注射|加量|减量|停药|手术|化疗|放疗|住院)"

_ADVICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("diagnosis_asserted", re.compile(r"(你|您)[^。！？\n]{0,12}(患|得)了")),
    ("diagnosis_asserted", re.compile(r"(确诊为|诊断为|可以确诊)")),
    (
        "medication_advice",
        re.compile(_NEG + r"(建议|推荐)(你|您)?[^。！？\n]{0,8}" + _ADVICE_VERBS),
    ),
    (
        "medication_advice",
        re.compile(
            _NEG + r"(你|您)[^。！？\n]{0,8}(可以|应该|建议|需要|必须)"
            r"[^。！？\n]{0,8}" + _ADVICE_VERBS
        ),
    ),
    (
        "severity_judgement",
        re.compile(
            r"(你|您)[^。！？\n]{0,8}(情况|病情|指标)[^。！？\n]{0,8}(很|比较|非常|挺)?严重"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Outcome of one check. `rewritten` is the safe replacement to surface instead."""

    allowed: bool
    reasons: tuple[str, ...] = ()
    rewritten: str | None = None


def check(text: str) -> GuardResult:
    """Gate a model response. Fail-closed: an internal error blocks rather than passes.

    Duplicate reasons are collapsed so a response tripping two patterns of the same kind
    reports one reason, which keeps logs readable.
    """
    try:
        if text is None or not text.strip():
            return GuardResult(allowed=True)
        reasons = tuple(
            dict.fromkeys(label for label, pattern in _ADVICE_PATTERNS if pattern.search(text))
        )
        if reasons:
            return GuardResult(allowed=False, reasons=reasons, rewritten=BLOCKED_RESPONSE)
        return GuardResult(allowed=True)
    except Exception:  # noqa: BLE001 - any failure here must block, not leak
        return GuardResult(allowed=False, reasons=("guard_error",), rewritten=BLOCKED_RESPONSE)
