"""出厂角色的「承诺在场」两把尺（R102-37 拍板后的补刀）。

为什么需要：`roles/seed.py` 里同时存在三种话 —— 白名单（允许什么）、`knowledge_scopes`
（能检索什么）、提示词/描述（**对用户承诺**什么）。三者从前没有任何尺子互相钉住，
于是"提示词说上传完直接提问、作用域也配了、白名单里却没有检索工具"这种半截状态
一路绿着出厂（2026-11-03 架构审查 A 行前的那条 WARN 就是这么来的）。

两把尺各挡一种掉线：

  1. **声明了作用域就必须给检索工具** —— 作用域是"能看哪些库"，工具是"伸手去查"，
     少了后者，作用域是配了个寂寞，而模型侧只会看到"没有这个工具"；
  2. **提示词里点了名的工具必须在白名单里** —— 点名 = 对用户承诺了这条能力，
     白名单漏一件，承诺就变成模型答"我做不了"。

白名单为 `None` 的角色（= 全给）两把尺都跳过：它没有"漏"这一说。
"""

from __future__ import annotations

import re
from pathlib import Path

from rolecard_agent.domains.registry import domain_seed_roles
from rolecard_agent.roles.seed import BUILTIN_ROLES

TOOL_DECL = re.compile(r'@tool\("(\w+)"\)')


def _all_roles():
    """出厂两族：代码内置 + 域随域包出厂（当前为空集，但尺子按全集写）。"""
    return (*BUILTIN_ROLES, *domain_seed_roles())


def _declared_tools() -> set[str]:
    """代码里真实声明过的工具名（与一致性门禁同一把尺：扫 `@tool("...")`）。"""
    names: set[str] = set()
    src = Path(__file__).resolve().parents[2] / "src" / "rolecard_agent"
    for py in src.rglob("*.py"):
        names.update(TOOL_DECL.findall(py.read_text(encoding="utf-8")))
    return names


def test_declared_knowledge_scopes_carry_the_retrieval_tool() -> None:
    """声明了 `knowledge_scopes` 的角色，白名单必须含 `search_knowledge`。

    为什么不是反过来"没有作用域就别配工具"：工具自己会在调用时拒绝无作用域的请求
    （`rag.retriever.make_search_tool` 的一手防线），配多了只是空转；而**少了**这半边
    是对外承诺的塌方 —— 角色卡写着"能检索"、提示词教用户"直接提问"，模型手里却没有
    那支工具。检索是读 + 作用域内网闸的行为，不属于 `run_command` 那种审批门豁免，
    出厂白名单该给就给。
    """
    offenders = [
        r.role_id
        for r in _all_roles()
        if r.knowledge_scopes
        and r.tool_whitelist is not None
        and "search_knowledge" not in r.tool_whitelist
    ]
    assert not offenders, (
        f"这些角色声明了知识作用域，白名单却没有检索工具（承诺了却给不了）: {sorted(offenders)}"
    )


def test_tools_named_in_the_prompt_are_whitelisted() -> None:
    """提示词/描述里**点了名**的工具必须在白名单里 —— 点名即承诺。"""
    declared = _declared_tools()
    assert declared, "扫不到任何 @tool 声明，这把尺子本身失效了"
    missing: dict[str, list[str]] = {}
    for role in _all_roles():
        if role.tool_whitelist is None:
            continue
        text = f"{role.system_prompt}\n{role.description}"
        named = sorted(t for t in declared if re.search(rf"\b{re.escape(t)}\b", text))
        gaps = [t for t in named if t not in role.tool_whitelist]
        if gaps:
            missing[role.role_id] = gaps
    assert not missing, f"提示词点了名、白名单却没给的工具: {missing}"
