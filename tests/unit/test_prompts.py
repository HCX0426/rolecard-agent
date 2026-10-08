"""Ordering tests for core/agent/prompts.py.  Traceability: US-4, US-8.

These guard the *ordering* invariant, which IS the safety mechanism: if the global rules
ever end up before the role prompt, the safety layer silently stops working and nothing
else in the codebase would notice. Cheap test, high value.

Also serves as the template for how tests in this repo are written - see CONTRIBUTING §2.
"""

from __future__ import annotations

from typing import NamedTuple

from rolecard_agent.core.agent import prompts


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


# --------------------------------------------------------------------------- agent mode
#
# 智能体模式 = 行为指引（先规划、一步一步来、别暴力重试、最后总结），权威度在角色人设
# 之上、在范例与安全规则之下：它约束"怎么做"，不能覆盖"绝不能说/做什么"。


def test_agent_plan_sits_below_examples_and_safety() -> None:
    result = prompts.build_system_prompt(
        "角色说明", _EXEMPLARS, memory="记忆", agent=True
    )
    role_at = result.index("角色说明")
    memory_at = result.index("用户长期记忆")
    agent_at = result.index("智能体模式")
    exemplar_at = result.index("回答风格参考")
    safety_at = result.index("禁止输出任何疾病诊断")
    assert role_at < memory_at < agent_at < exemplar_at < safety_at


def test_chat_mode_has_no_agent_section() -> None:
    result = prompts.build_system_prompt("角色说明", agent=False)
    assert "智能体模式" not in result
    assert result == "角色说明\n\n" + prompts.GLOBAL_SAFETY_PROMPT


# --------------------------------------------------------------------------- image grounding
#
# 多模态角色最危险的失效模式：人设 / 检索资料盖过图片真实像素，把无关图"识别"成设定内容
# （2026-09-19 爱莉希雅事故）。图像接地规则必须排在人设/记忆/范例之后、安全规则之前——
# 压得住人设，又永远不被安全规则降级。


def test_image_grounding_outranks_persona_and_sits_below_safety() -> None:
    result = prompts.build_system_prompt(
        "角色说明", _EXEMPLARS, memory="记忆", agent=True, has_image=True
    )
    role_at = result.index("角色说明")
    exemplar_at = result.index("回答风格参考")
    image_at = result.index("图像理解规则")
    safety_at = result.index("禁止输出任何疾病诊断")
    assert role_at < exemplar_at < image_at < safety_at


def test_no_image_adds_no_grounding_section() -> None:
    assert "图像理解规则" not in prompts.build_system_prompt("角色说明")
    assert "图像理解规则" not in prompts.build_system_prompt("角色说明", has_image=False)


def test_grounding_rule_encodes_persona_interprets_not_fabricates() -> None:
    """规则要点：以图为准 + 角色只解读不捏造 + 不确定联网查 + 查不到承认不知道。"""
    result = prompts.build_system_prompt("角色说明", has_image=True)
    assert "以图为准" in result
    assert "不决定「图里有什么」" in result  # 人设管"怎么看"，不管"看到什么"
    assert "web_search" in result  # 不明白就联网搜
    assert "不知道" in result  # 搜不到就承认不知道
