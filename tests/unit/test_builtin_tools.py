"""Kernel tools must exist and must be named what the whitelists say.  Traceability: US-2.

`roles/seed.py` already listed `list_domains` / `list_roles` in the built-in role's whitelist
while `core/tools/builtin.py` was still an empty docstring - the whitelist pointed at nothing
(技术评审与决策.md 「实现对齐核查」B1). The name assertions below are the regression guard.
"""

from __future__ import annotations

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.config import Settings
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


def test_builtin_role_whitelist_resolves(roles: RoleCardService, tmp_path) -> None:
    """Every name in the **built-in** role's whitelist must be a tool a real factory builds.

    判据是"白名单里每个名字都真能解析成一件工具"——写了个不存在的名字，模型拿到的是
    死链路。可解析集合取自**真实工厂**（与装配点 `build_registry` 同一批工厂）：
    内核工具、联网、工作区文件、跨会话记忆、检索。测试夹具自建的自定义卡
    （`note_keeper`）带的是本文件的假工具，不在此列。
    """
    from rolecard_agent.core.memory import make_memory_tool
    from rolecard_agent.core.tools.files import make_file_tools
    from rolecard_agent.core.tools.web import make_web_tools
    from rolecard_agent.rag.retriever import make_search_tool

    settings = Settings(sqlite_path=tmp_path / "t.db")
    resolvable = {t.name for t in make_kernel_tools(current_user=_who, roles=roles)}
    resolvable |= {t.name for t in make_web_tools(settings=settings)}
    resolvable |= {t.name for t in make_file_tools(settings=settings)}
    resolvable.add(make_memory_tool(settings=settings, conn=None).name)  # type: ignore[arg-type]
    resolvable.add(make_search_tool(None).name)  # type: ignore[arg-type]
    # 多模型比对的工厂在本文件的 import 面之外（它要模型解析器），名字显式登记在这里
    # 并注明出处；白名单里只剩它一件非工厂直出的。
    resolvable.add("compare_model_answers")
    whitelist = cards(roles).get("general_assistant").tool_whitelist or []
    assert set(whitelist) <= resolvable, set(whitelist) - resolvable


def test_list_domains_reports_what_is_enabled(roles: RoleCardService) -> None:
    tools = {
        t.name: t
        for t in make_kernel_tools(current_user=_who, roles=roles, enabled_domains=["notes"])
    }
    assert "notes" in tools["list_domains"].invoke({})


def test_list_domains_says_so_when_nothing_is_on(roles: RoleCardService) -> None:
    """An empty answer is still an answer - the model should not have to guess."""
    tools = {
        t.name: t for t in make_kernel_tools(current_user=_who, roles=roles, enabled_domains=[])
    }
    assert "没有启用" in tools["list_domains"].invoke({})


def test_list_roles_includes_the_builtin_with_its_id(roles: RoleCardService) -> None:
    tools = {t.name: t for t in make_kernel_tools(current_user=_who, roles=roles)}
    output = tools["list_roles"].invoke({})
    assert "note_keeper" in output
    assert "笔记管理员" in output


def test_kernel_tools_carry_descriptions_for_the_model(roles: RoleCardService) -> None:
    """The docstring is the tool's interface to the model, so an empty one is a defect."""
    for kernel_tool in make_kernel_tools(current_user=_who, roles=roles):
        assert kernel_tool.description.strip()
