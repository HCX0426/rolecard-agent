"""Kernel tools must exist and must be named what the whitelists say.  Traceability: US-2.

`roles/seed.py` already listed `list_domains` / `list_roles` in the built-in role's whitelist
while `core/tools/builtin.py` was still an empty docstring - the whitelist pointed at nothing
(技术评审与决策.md §9 B1). The name assertions below are the regression guard.
"""

from __future__ import annotations

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.core.tools.builtin import make_kernel_tools
from rolecard_agent.roles.service import RoleCards, RoleCardService


def cards(store: RoleCardService) -> RoleCards:
    """测试里"本机主人眼里的那些卡"的简写（M2a 之后每次读写都得说清为谁）。"""
    return store.scoped(DEFAULT_USER_ID)


def _who() -> str:
    """这台实例的主人 = 本机那份（`make_kernel_tools` 要一个"现取身份"的提供者）。"""
    return DEFAULT_USER_ID

def test_kernel_tool_names_are_stable(roles: RoleCardService) -> None:
    names = [t.name for t in make_kernel_tools(current_user=_who, roles=roles)]
    assert names == ["list_domains", "list_roles"]


def test_builtin_role_whitelist_resolves(roles: RoleCardService) -> None:
    """Every name in the seeded whitelist must be a kernel tool, a declared domain tool,
    or the kernel search_knowledge capability (v2.1: retrieval lives in rag/)."""
    from rolecard_agent.domains.health.names import DOMAIN_TOOL_NAMES
    from rolecard_agent.rag.retriever import make_search_tool

    resolvable = {t.name for t in make_kernel_tools(current_user=_who, roles=roles)}
    resolvable |= set(DOMAIN_TOOL_NAMES)
    resolvable.add(make_search_tool(None).name)  # type: ignore[arg-type]
    whitelist = cards(roles).get("medical_archivist").tool_whitelist or []
    assert set(whitelist) <= resolvable, set(whitelist) - resolvable


def test_list_domains_reports_what_is_enabled(roles: RoleCardService) -> None:
    tools = {
        t.name: t
        for t in make_kernel_tools(current_user=_who, roles=roles, enabled_domains=["health"])
    }
    assert "health" in tools["list_domains"].invoke({})


def test_list_domains_says_so_when_nothing_is_on(roles: RoleCardService) -> None:
    """An empty answer is still an answer - the model should not have to guess."""
    tools = {
        t.name: t
        for t in make_kernel_tools(current_user=_who, roles=roles, enabled_domains=[])
    }
    assert "没有启用" in tools["list_domains"].invoke({})


def test_list_roles_includes_the_builtin_with_its_id(roles: RoleCardService) -> None:
    tools = {t.name: t for t in make_kernel_tools(current_user=_who, roles=roles)}
    output = tools["list_roles"].invoke({})
    assert "medical_archivist" in output
    assert "健康档案管理员" in output


def test_kernel_tools_carry_descriptions_for_the_model(roles: RoleCardService) -> None:
    """The docstring is the tool's interface to the model, so an empty one is a defect."""
    for kernel_tool in make_kernel_tools(current_user=_who, roles=roles):
        assert kernel_tool.description.strip()
