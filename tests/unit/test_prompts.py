"""Ordering tests for core/prompts.py.  Traceability: US-4, US-8.

These guard the *ordering* invariant, which IS the safety mechanism: if the global rules
ever end up before the role prompt, the safety layer silently stops working and nothing
else in the codebase would notice. Cheap test, high value.

Also serves as the template for how tests in this repo are written - see CONTRIBUTING §2.
"""

from __future__ import annotations

from typing import NamedTuple

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
    """US-4: unverified values must keep their marker."""
    assert "未经人工校验" in prompts.GLOBAL_SAFETY_PROMPT


# --------------------------------------------------------------------------- exemplars
#
# Reproducing a role takes rules AND examples. The examples must sit BETWEEN the role prompt
# and the global safety rules: they are strong enough to override the role text, which is
# exactly why they must not be able to override the safety text.


class _Exemplar(NamedTuple):
    user: str
    assistant: str


_EXEMPLARS = [
    _Exemplar(user="上次的结石直径是多少？", assistant="6.0 mm。【未经人工校验】"),
    _Exemplar(user="这个严重吗？", assistant="我不能评估病情严重程度。"),
]


def test_exemplars_sit_between_role_and_safety_rules() -> None:
    result = prompts.build_system_prompt("你是健康档案管理员。", _EXEMPLARS)
    role_at = result.index("你是健康档案管理员")
    exemplar_at = result.index("回答风格参考")
    safety_at = result.index("禁止输出任何疾病诊断")
    assert role_at < exemplar_at < safety_at


def test_exemplar_body_is_rendered_verbatim() -> None:
    result = prompts.build_system_prompt("角色说明", _EXEMPLARS)
    assert "上次的结石直径是多少？" in result
    assert "6.0 mm。【未经人工校验】" in result


def test_no_exemplars_matches_the_two_part_form() -> None:
    """The common case must not gain a stray empty section."""
    expected = f"角色说明\n\n{prompts.GLOBAL_SAFETY_PROMPT}"
    assert prompts.build_system_prompt("角色说明") == expected
    assert prompts.build_system_prompt("角色说明", []) == expected


def test_empty_role_prompt_with_exemplars_keeps_the_rules_last() -> None:
    result = prompts.build_system_prompt("", _EXEMPLARS)
    assert result.endswith(prompts.GLOBAL_SAFETY_PROMPT)
    assert "回答风格参考" in result
