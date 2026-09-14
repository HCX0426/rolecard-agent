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

GLOBAL_SAFETY_PROMPT = """【全局强制规则，所有角色继承，不可删除】
你禁止输出任何疾病诊断、用药建议、治疗方案。
仅可汇总、查询、对比用户已存入档案内的报告与指标。
不能评估病情严重程度，不能推荐药物、手术方案。
所有医疗相关结论仅引用已存入档案的数据，禁止凭空推断。
档案数据中带有【未经人工校验】标记的内容，回答时必须原样保留该标记，不得省略。
"""


def build_system_prompt(role_prompt: str) -> str:
    """Return the final system prompt for one turn: role prompt first, safety rules last.

    The ordering is the whole point - putting the global rules last means they win any
    conflict with the role card. Reversing the two silently disables the safety layer,
    which is why tests/unit/test_prompts.py asserts the order.
    """
    role_prompt = (role_prompt or "").strip()
    if not role_prompt:
        return GLOBAL_SAFETY_PROMPT
    return f"{role_prompt}\n\n{GLOBAL_SAFETY_PROMPT}"
