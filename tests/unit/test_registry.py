"""Two-stage filtering: enabled plugins, then the role whitelist.  Traceability: US-2.

This is the test that matters most for the permission story. If filtering ever moves to
execution time, these assertions still pass - which is why the integration test asserts on
*what the model was bound with* rather than on what it was allowed to run.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from rolecard_agent.core.tools.registry import ToolRegistry, filter_specs


@tool
def alpha() -> str:
    """Domain A tool."""
    return "a"


@tool
def beta() -> str:
    """Domain B tool."""
    return "b"


@tool
def kernel() -> str:
    """Kernel tool, not owned by any domain."""
    return "k"


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(kernel)
    reg.register(alpha, domain="dom-a")
    reg.register(beta, domain="dom-b")
    return reg


def test_disabled_domain_hides_its_tools(registry: ToolRegistry) -> None:
    names = [t.name for t in registry.select(enabled_domains=["dom-a"], role_whitelist=None)]
    assert names == ["alpha", "kernel"]


def test_kernel_tools_survive_every_plugin_state(registry: ToolRegistry) -> None:
    names = [t.name for t in registry.select(enabled_domains=(), role_whitelist=None)]
    assert names == ["kernel"]


def test_whitelist_none_means_all_survivors(registry: ToolRegistry) -> None:
    names = [
        t.name for t in registry.select(enabled_domains=["dom-a", "dom-b"], role_whitelist=None)
    ]
    assert names == ["alpha", "beta", "kernel"]


def test_empty_whitelist_means_nothing_not_everything(registry: ToolRegistry) -> None:
    """`[]` and `None` are easy to confuse; this pins the difference."""
    assert registry.select(enabled_domains=["dom-a"], role_whitelist=[]) == []


def test_whitelist_cannot_widen_access(registry: ToolRegistry) -> None:
    """A role naming a tool from a disabled plugin still does not get it."""
    names = [
        t.name for t in registry.select(enabled_domains=["dom-a"], role_whitelist=["alpha", "beta"])
    ]
    assert names == ["alpha"]


def test_duplicate_registration_is_rejected(registry: ToolRegistry) -> None:
    """Silently overwriting would make a permission change look like a no-op."""
    with pytest.raises(ValueError, match="already registered"):
        registry.register(kernel)


def test_selection_order_is_stable(registry: ToolRegistry) -> None:
    first = [
        t.name for t in registry.select(enabled_domains=["dom-b", "dom-a"], role_whitelist=None)
    ]
    second = [
        t.name for t in registry.select(enabled_domains=["dom-a", "dom-b"], role_whitelist=None)
    ]
    assert first == second == ["alpha", "beta", "kernel"]


def test_filter_specs_is_pure(registry: ToolRegistry) -> None:
    specs = registry.specs()
    before = list(specs)
    filter_specs(specs, enabled_domains=["dom-a"], role_whitelist=["alpha"])
    assert list(specs) == before
