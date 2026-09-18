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


# --------------------------------------------------------------------------- memory
#
# 跨会话记忆是**用户事实**，权威度在角色人设（风格）之上、在安全规则之下：角色可以
# 决定腔调，记忆决定"该知道什么"，全局安全规则决定"绝不能说/做什么"。所以顺序必须是
# 角色 -> 记忆 -> 范例 -> 安全，且记忆区自带"以用户最新说法为准"的降权声明。


def test_memory_sits_between_role_and_exemplars() -> None:
    result = prompts.build_system_prompt("你是通用助手。", _EXEMPLARS, memory="用户住在上海。")
    role_at = result.index("你是通用助手。")
    memory_at = result.index("用户长期记忆")
    exemplar_at = result.index("回答风格参考")
    safety_at = result.index("禁止输出任何疾病诊断")
    assert role_at < memory_at < exemplar_at < safety_at
    assert "用户住在上海。" in result


def test_memory_header_degrades_stale_facts() -> None:
    """记忆与用户当下说法冲突时，以当下为准 —— 这条声明必须随记忆一起出现。"""
    result = prompts.build_system_prompt("x", memory="旧说法")
    assert "以用户当下的说法为准" in result


def test_no_memory_adds_no_stray_section() -> None:
    """缺省 / 空记忆 = 与旧版完全一致的两段式或三段式，不出现空记忆区。"""
    assert prompts.build_system_prompt("角色说明") == (
        f"角色说明\n\n{prompts.GLOBAL_SAFETY_PROMPT}"
    )
    assert "用户长期记忆" not in prompts.build_system_prompt("角色说明", _EXEMPLARS)


def test_blank_memory_is_ignored() -> None:
    for memory in ("", "   ", "\n"):
        result = prompts.build_system_prompt("角色说明", memory=memory)
        assert "用户长期记忆" not in result
        assert result.endswith(prompts.GLOBAL_SAFETY_PROMPT)
