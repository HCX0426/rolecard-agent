"""Kernel tools must exist and must be named what the whitelists say.  Traceability: US-2.

`roles/seed.py` already listed `list_domains` / `list_roles` in the built-in role's whitelist
while `core/tools/builtin.py` was still an empty docstring - the whitelist pointed at nothing
(技术评审与决策.md §9 B1). The name assertions below are the regression guard.
"""

from __future__ import annotations

from rolecard_agent.core.tools.builtin import make_kernel_tools
from rolecard_agent.roles.service import RoleCardService


def test_kernel_tool_names_are_stable(roles: RoleCardService) -> None:
    assert [t.name for t in make_kernel_tools(roles=roles)] == ["list_domains", "list_roles"]


def test_builtin_role_whitelist_resolves(roles: RoleCardService) -> None:
    """Every name in the seeded whitelist must be a kernel tool, a declared domain tool,
    or the kernel search_knowledge capability (v2.1: retrieval lives in rag/)."""
    from rolecard_agent.domains.health.tools import DOMAIN_TOOL_NAMES
    from rolecard_agent.rag.retriever import make_search_tool

    resolvable = {t.name for t in make_kernel_tools(roles=roles)} | set(DOMAIN_TOOL_NAMES)
    resolvable.add(make_search_tool(None).name)  # type: ignore[arg-type]
    whitelist = roles.get("medical_archivist").tool_whitelist or []
    assert set(whitelist) <= resolvable, set(whitelist) - resolvable


def test_list_domains_reports_what_is_enabled(roles: RoleCardService) -> None:
    tools = {t.name: t for t in make_kernel_tools(roles=roles, enabled_domains=["health"])}
    assert "health" in tools["list_domains"].invoke({})


def test_list_domains_says_so_when_nothing_is_on(roles: RoleCardService) -> None:
    """An empty answer is still an answer - the model should not have to guess."""
    tools = {t.name: t for t in make_kernel_tools(roles=roles, enabled_domains=[])}
    assert "没有启用" in tools["list_domains"].invoke({})


def test_list_roles_includes_the_builtin_with_its_id(roles: RoleCardService) -> None:
    tools = {t.name: t for t in make_kernel_tools(roles=roles)}
    output = tools["list_roles"].invoke({})
    assert "medical_archivist" in output
    assert "健康档案管理员" in output


def test_kernel_tools_carry_descriptions_for_the_model(roles: RoleCardService) -> None:
    """The docstring is the tool's interface to the model, so an empty one is a defect."""
    for kernel_tool in make_kernel_tools(roles=roles):
        assert kernel_tool.description.strip()
