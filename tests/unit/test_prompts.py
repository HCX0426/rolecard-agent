"""Ordering tests for core/prompts.py.

These guard the *ordering* invariant, which IS the safety mechanism: if the global rules
ever end up before the role prompt, the safety layer silently stops working and nothing
else in the codebase would notice. Cheap test, high value.

Also serves as the template for how tests in this repo are written - see CONTRIBUTING §2.
"""

from __future__ import annotations

from rolecard_agent.core import prompts


def test_safety_rules_come_last() -> None:
    result = prompts.build_system_prompt("你是健康档案管理员，负责查询已入库的指标。")
    assert result.index("你是健康档案管理员") < result.index("禁止输出任何疾病诊断")


def test_safety_rules_survive_every_role_prompt() -> None:
    for role_prompt in ("", "   ", "任意角色人设", "你是一个乐于助人的通用助手。"):
        assert "禁止输出任何疾病诊断" in prompts.build_system_prompt(role_prompt)


def test_empty_role_prompt_degrades_to_safety_only() -> None:
    """A role card with an empty prompt must still be constrained."""
    assert prompts.build_system_prompt("") == prompts.GLOBAL_SAFETY_PROMPT
    assert prompts.build_system_prompt("   ") == prompts.GLOBAL_SAFETY_PROMPT


def test_unverified_marker_instruction_is_present() -> None:
    """docs/需求与验收标准.md US-4: unverified values must keep their marker."""
    assert "未经人工校验" in prompts.GLOBAL_SAFETY_PROMPT
