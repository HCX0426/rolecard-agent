"""Kernel tools, domain-agnostic: list_domains / list_roles.

DECISION (resolves D2 vs the old placeholder, logged as C5):

    switch_role is NOT bound to the model.

Letting the model change its own permission boundary is self-authorization - a prompt
injection could switch the session into a role that has more tools. Role switching is an
API / UI action that writes session_thread.current_role_id (see core/schema.sql).

Role switching still preserves history: only the role id changes, never the messages.

search_knowledge is NOT here - it belongs to rag/ and is registered in v2.1.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from langchain_core.tools import BaseTool, tool

from rolecard_agent.roles.service import RoleCardService

# Either a snapshot (tests) or a live provider like `PluginService.enabled_domains` (the app).
# The callable form is what keeps `list_domains` honest across plugin toggles: the tool reports
# what is enabled NOW, not what was enabled when the graph was compiled.
DomainsLike = Sequence[str] | Callable[[], Sequence[str]]


def _current(domains: DomainsLike) -> tuple[str, ...]:
    return tuple(domains() if callable(domains) else domains)


def make_kernel_tools(
    *,
    roles: RoleCardService,
    current_user: Callable[[], str],
    enabled_domains: DomainsLike = ()
) -> list[BaseTool]:
    """Build the kernel tools, closing over the services they need.

    A factory rather than module-level `@tool` functions: both tools report *runtime* state
    (which plugins are on, which roles exist), and a module-level function has nowhere to read
    it from. Closures keep the tools stateless from the model's point of view while still being
    ordinary `BaseTool`s that `bind_tools` accepts.

    Both are read-only, and neither can move a permission boundary - that is the point.
    """
    provider = enabled_domains

    @tool("list_domains")
    def list_domains() -> str:
        """List the domain plugins currently enabled in this deployment.

        Use it when the user asks what you can do, or when a lookup returns nothing and you
        need to say which capabilities are actually switched on.
        """
        if not (enabled := _current(provider)):
            return "当前没有启用任何领域插件。"
        return "已启用的领域插件：" + "、".join(enabled)

    @tool("list_roles")
    def list_roles() -> str:
        """List the roles available in this deployment, one line each."""
        cards = roles.scoped(current_user()).list_roles()
        if not cards:
            return "当前没有可用的角色。"
        lines = [
            f"- {card.role_name}（{card.role_id}）：{card.description or '无描述'}"
            for card in cards
        ]
        return "可用角色：\n" + "\n".join(lines)

    return [list_domains, list_roles]
