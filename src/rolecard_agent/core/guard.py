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
"""
