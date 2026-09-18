"""System prompt assembly.

INVARIANT (do not break this, ever):

  * GLOBAL_SAFETY_PROMPT is defined HERE, in code - never in the database. A role card
    therefore cannot remove, weaken, or override it.
  * It is appended AFTER the role's system_prompt, so it is the last instruction the
    model reads.
  * build_system_prompt() is the ONLY supported way to assemble a system prompt. No node
    may concatenate prompts on its own.

This module is deliberately implemented rather than left as a TODO: it is on the
safety-critical path, and safety-critical code should not be delegated.

Scope note: this is the *soft* layer. The hard gate is core/guard.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

GLOBAL_SAFETY_PROMPT = """【全局强制规则，所有角色继承，不可删除】
你禁止输出任何疾病诊断、用药建议、治疗方案。
不能评估病情严重程度，不能推荐药物、手术方案。
涉及健康指标的表述，只能引用用户已存入档案的数据，禁止凭空推断或用常识补充。
档案数据中带有【未经人工校验】标记的内容，回答时必须原样保留该标记，不得省略。
"""

EXEMPLAR_HEADER = "【回答风格参考】以下是本角色的回答范例，用于对齐口径与语气。"

MEMORY_HEADER = (
    "【用户长期记忆】以下是你对用户的长期事实记忆，回答时应参照它们。"
    "若与用户当前的明确说法冲突，一律以用户当下的说法为准。"
)


class ExemplarLike(Protocol):
    """Structural type for a role exemplar.

    Deliberately structural rather than importing `roles.models.RoleExemplar`: the kernel
    prompt builder should not depend on the roles package, and any object with these two
    attributes is a valid exemplar.
    """

    user: str
    assistant: str


def render_exemplars(exemplars: Sequence[ExemplarLike] | None) -> str:
    """Format exemplars as a prompt section. Returns "" when there are none."""
    if not exemplars:
        return ""
    blocks = [EXEMPLAR_HEADER, ""]
    for item in exemplars:
        blocks.append(f"用户：{item.user.strip()}")
        blocks.append(f"你：{item.assistant.strip()}")
        blocks.append("")
    return "\n".join(blocks).strip()


def render_memory(memory: str | None) -> str:
    """Format stored memory as a prompt section. Returns "" when there is none."""
    if not memory or not memory.strip():
        return ""
    return f"{MEMORY_HEADER}\n{memory.strip()}"


def build_system_prompt(
    role_prompt: str,
    exemplars: Sequence[ExemplarLike] | None = None,
    *,
    memory: str | None = None,
) -> str:
    """Return the final system prompt for one turn.

    Order is the whole point, and it is the order from least to most authoritative:

        角色人设  ->  长期记忆  ->  回答范例  ->  全局安全规则

    Putting the global rules last means they win any conflict with the role card, with a
    stored memory, or with an example. Reversing either pair silently disables the safety
    layer without raising anything, which is why tests/unit/test_prompts.py asserts the
    ordering explicitly.

    Examples sit between the two on purpose: they are style references, and they must not be
    able to contradict the safety rules. Memory sits above the examples: it is user-supplied
    fact, closer to the user's intent than a style template — but it still yields to the
    safety rules, and its own header makes it yield to the user's latest explicit statement.
    """
    sections = [
        part
        for part in (
            (role_prompt or "").strip(),
            render_memory(memory),
            render_exemplars(exemplars),
        )
        if part
    ]
    sections.append(GLOBAL_SAFETY_PROMPT)
    return "\n\n".join(sections)
