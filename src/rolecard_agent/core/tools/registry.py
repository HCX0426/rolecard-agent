"""Tool registry + two-stage filtering.

Stage 1: keep only tools belonging to ENABLED domains (the plugin switch).
Stage 2: narrow to the current role's tool_whitelist BEFORE `bind_tools` is called, so the
         model never even sees a tool it is not allowed to call.

Why the filter runs here rather than at execution time: filtering only at execution still
lets the model *see* the tool and try to call it, which burns turns and produces "I am unable
to use that" explanations. Narrowing before `bind_tools` makes the boundary invisible instead
of merely enforced (D1).

Kernel tools carry `domain=None` and always survive stage 1 - they exist for every domain.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from langchain_core.tools import BaseTool


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A registered tool plus the domain that owns it (`None` = kernel tool).

    `idempotent` 是执行器的**重试开关**，默认 False（安全默认）：只有显式声明"同一参数
    重复调用不产生新副作用"的只读工具才允许被重试。旧实现对所有工具一律重试 2 次，
    于是"上传并写台账"这类**写型域工具**在失败后可能被执行 3 遍 —— 一次模型调用
    产生多条记录（审查报告 M10）。
    """

    name: str
    tool: BaseTool
    domain: str | None = None
    idempotent: bool = False


def filter_specs(
    specs: Sequence[ToolSpec],
    *,
    enabled_domains: Iterable[str],
    role_whitelist: Sequence[str] | None,
) -> list[ToolSpec]:
    """Pure two-stage filter. Kept separate from the registry so it is trivially testable
    without constructing a graph or a model."""
    enabled = set(enabled_domains)
    allowed = set(role_whitelist) if role_whitelist is not None else None

    stage_one = [s for s in specs if s.domain is None or s.domain in enabled]
    if allowed is None:
        return stage_one
    return [s for s in stage_one if s.name in allowed]


class ToolRegistry:
    """Name -> ToolSpec map. Registration is explicit; there is no discovery."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(
        self, tool: BaseTool, *, domain: str | None = None, idempotent: bool = False
    ) -> ToolSpec:
        """Register one tool. Duplicate names are rejected rather than overwritten: a
        silent overwrite would make a permission change look like a no-op.

        `idempotent` 默认 False —— 未声明即为不可重试，见 `ToolSpec`。
        """
        name = tool.name
        if name in self._specs:
            raise ValueError(f"tool already registered: {name}")
        spec = ToolSpec(name=name, tool=tool, domain=domain, idempotent=idempotent)
        self._specs[name] = spec
        return spec

    def register_many(
        self,
        tools: Iterable[BaseTool],
        *,
        domain: str | None = None,
        idempotent: bool = False,
    ) -> list[ToolSpec]:
        return [self.register(t, domain=domain, idempotent=idempotent) for t in tools]

    def get(self, name: str) -> BaseTool | None:
        spec = self._specs.get(name)
        return None if spec is None else spec.tool

    def is_idempotent(self, name: str) -> bool:
        """未注册的名字按**不可重试**处理（fail-safe）。"""
        spec = self._specs.get(name)
        return bool(spec and spec.idempotent)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[n] for n in self.names()]

    def select(
        self,
        *,
        enabled_domains: Iterable[str] = (),
        role_whitelist: Sequence[str] | None = None,
    ) -> list[BaseTool]:
        """The tools to bind for this turn. Order is stable (sorted by name) so that prompt
        caching and test assertions both stay deterministic."""
        selected = filter_specs(
            self.specs(), enabled_domains=enabled_domains, role_whitelist=role_whitelist
        )
        return [s.tool for s in selected]
