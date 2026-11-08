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
from typing import Protocol

from langchain_core.tools import BaseTool, tool

# Either a snapshot (tests) or a live provider like `PluginService.enabled_domains` (the app).
# The callable form is what keeps `list_domains` honest across plugin toggles: the tool reports
# what is enabled NOW, not what was enabled when the graph was compiled.
DomainsLike = Sequence[str] | Callable[[], Sequence[str]]


class RoleSummary(Protocol):
    """`list_roles` 这一条工具真正要读的三格。角色卡的其余字段与它无关。"""

    role_id: str
    role_name: str
    description: str | None


class RoleCardView(Protocol):
    """`scoped(user_id)` 换出来的那个视图：本工具只问它要清单。"""

    def list_roles(self) -> Sequence[RoleSummary]: ...


class RoleReader(Protocol):
    """内核工具对"角色卡服务"的全部要求：换个主人，列出角色。

    为什么是 Protocol 而不是 `roles.service.RoleCardService`：`core` 去 import 一个具体的
    上层服务类，等于让内核认识实现者（本仓的横向抓取就是这么长出来的）。按形状要东西，
    实现方无需知道自己被谁用，测试也能直接喂一个假视图。
    """

    def scoped(self, user_id: str) -> RoleCardView: ...


def _current(domains: DomainsLike) -> tuple[str, ...]:
    return tuple(domains() if callable(domains) else domains)


def make_kernel_tools(
    *, roles: RoleReader, current_user: Callable[[], str], enabled_domains: DomainsLike = ()
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
        """列出本部署当前启用的领域插件。

        用户问"你能做什么"，或某次查找一无所获、需要你说明到底开着哪些能力时用它的返回。
        """
        if not (enabled := _current(provider)):
            return "当前没有启用任何领域插件。"
        return "已启用的领域插件：" + "、".join(enabled)

    @tool("list_roles")
    def list_roles() -> str:
        """列出本部署可用的角色卡，每行一个（名称、id、简介）。"""
        cards = roles.scoped(current_user()).list_roles()
        if not cards:
            return "当前没有可用的角色。"
        lines = [
            f"- {card.role_name}（{card.role_id}）：{card.description or '无描述'}"
            for card in cards
        ]
        return "可用角色：\n" + "\n".join(lines)

    return [list_domains, list_roles]
