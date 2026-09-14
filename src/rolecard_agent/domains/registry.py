"""Explicit domain plugin list - no dynamic loading, no discovery magic.

Two different things, deliberately kept apart:

  * **REGISTERED** - which domains exist, i.e. which code is present. This file. Changing it
    is a commit.
  * **ENABLED** - which registered domains are currently switched on. The `plugin` table.
    Changing it is an operator action.

`scripts/init_db.py` applies the schema of every REGISTERED domain, so a table always exists
and re-enabling a plugin never needs DDL.

To add a domain: implement models.py / service.py / tools.py / schema.sql under
domains/<name>/ and append its id to DOMAINS below. M3 adds the tool registry on top of this;
today it is the id list only.

IMPORTANT: runtime enable/disable is an OPERATOR action backed by the plugin table.
It is never exposed as an LLM-callable tool (self-authorization risk, same as switch_role).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only names: the runtime imports live inside build_registry, keeping this
    # module's import cost at "id list only" for callers (init_db) that only need DOMAINS.
    from rolecard_agent.core.ingestion import IngestionService
    from rolecard_agent.core.tools.builtin import DomainsLike
    from rolecard_agent.core.tools.registry import ToolRegistry
    from rolecard_agent.roles.service import RoleCardService

# Registered domain ids. Each MUST match a directory under domains/ and the plugin.plugin_id
# in core/schema.sql. This is the single source of truth - previously scripts/init_db.py kept
# its own hardcoded copy, which is how two lists drift apart (技术评审与决策.md §9 A3).
DOMAINS: tuple[str, ...] = ("health",)


def build_registry(
    *,
    roles: RoleCardService,
    ingestion: IngestionService,
    enabled_domains: DomainsLike,
    current_user: Callable[[], str],
) -> ToolRegistry:
    """Assemble the full tool registry: kernel tools + every registered domain's tools.

    This is the ONE place that knows how each domain's tool factory is wired, so the API layer
    never imports a concrete domain (that would re-couple the app surface to `health`).
    Registering a domain id without adding its factory here fails LOUDLY at startup - a domain
    whose tools silently never bind is the failure mode this project exists to prevent.

    `current_user` is resolved per tool invocation inside the factories; the model can never
    name who it is acting as.
    """
    from rolecard_agent.core.tools.builtin import make_kernel_tools
    from rolecard_agent.core.tools.registry import ToolRegistry as _ToolRegistry
    from rolecard_agent.domains.health.tools import make_domain_tools

    registry = _ToolRegistry()
    # Kernel tools carry domain=None and survive every plugin toggle.
    registry.register_many(make_kernel_tools(roles=roles, enabled_domains=enabled_domains))

    # Explicit per-domain wiring: what each domain needs to construct its tools, visible here.
    factories = {
        "health": lambda: make_domain_tools(ingestion, current_user=current_user),
    }
    for domain in DOMAINS:
        if domain not in factories:
            raise ValueError(
                f"domain {domain!r} is registered but has no tool factory in build_registry()"
            )
        registry.register_many(factories[domain](), domain=domain)
    return registry
