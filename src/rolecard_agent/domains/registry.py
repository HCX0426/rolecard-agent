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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only names: the runtime imports live inside build_registry, keeping this
    # module's import cost at "id list only" for callers (init_db) that only need DOMAINS.
    from rolecard_agent.core.ingestion import IngestionService
    from rolecard_agent.core.tools.builtin import DomainsLike
    from rolecard_agent.core.tools.registry import ToolRegistry
    from rolecard_agent.domains.health.service import HealthQueryService
    from rolecard_agent.rag.retriever import KnowledgeBase
    from rolecard_agent.roles.service import RoleCardService

# Registered domain ids. Each MUST match a directory under domains/ and the plugin.plugin_id
# in core/schema.sql. This is the single source of truth - previously scripts/init_db.py kept
# its own hardcoded copy, which is how two lists drift apart (技术评审与决策.md §9 A3).
#
# `finance` is a data-only domain (no LLM tools): it exists to exercise the generic
# `domain_data` CRUD path (前端 `GenericDomainData`) so multi-domain data management is real,
# not health-only. Add richer domains here the same way (models/service/tools/schema + factory).
DOMAINS: tuple[str, ...] = ("health", "finance")


def _lazy_settings() -> Any:
    """兜底默认配置（仅测试直接调用 build_registry 时会走到）。"""
    from rolecard_agent.config import Settings

    return Settings()


def build_registry(
    *,
    roles: RoleCardService,
    ingestion: IngestionService,
    query: HealthQueryService,
    knowledge: KnowledgeBase,
    enabled_domains: DomainsLike,
    current_user: Callable[[], str],
    upload_dir: Path,
    tracer: Any = None,
    settings: Any = None,
    memory_conn: Any = None,
) -> ToolRegistry:
    """Assemble the full tool registry: kernel tools + every registered domain's tools.

    This is the ONE place that knows how each domain's tool factory is wired, so the API layer
    never imports a concrete domain (that would re-couple the app surface to `health`).
    Registering a domain id without adding its factory here fails LOUDLY at startup - a domain
    whose tools silently never bind is the failure mode this project exists to prevent.

    `current_user` is resolved per tool invocation inside the factories; the model can never
    name who it is acting as. `knowledge` backs the kernel search_knowledge tool (v2.1):
    retrieval is a KERNEL capability — scope-authorised per role at invocation time, the
    model never names a collection.

    `upload_dir` 是域**写**工具的路径边界：这些工具的入参来自模型（因而也来自上传文档里
    的提示注入），不设边界就是"读任意主机文件 + 在任意目录写"（审查报告 H1）。

    `tracer` 必须传下去：`search_knowledge` 闭包持有 KB，而 KB 的 `search()` 只有拿到
    tracer 才会 emit `rag_search` / `rerank_fallback`（审查报告 M3）。

    `settings` 供联网工具（web_search / web_fetch：搜索后端与 TAVILY_API_KEY）、工作区
    文件工具（fs_read / fs_write / fs_list：WORKSPACE_DIR 路径边界）与跨会话记忆工具
    （memory_save：MEMORY_ENABLED 总闸）使用；None = 默认配置（仅测试场景）。

    `memory_conn` = 跨会话记忆读写用的 SQLite 连接（ThreadLocalConnection）。None =
    不注册 memory_save（测试 / 未接入记忆的宿主）：白名单引用了但工具不存在时，
    模型本轮看不到它，执行器按"未启用"处理 —— fail-closed。

    工具的 `idempotent` 标记是**执行器的重试开关**：只有显式声明"重复调用无副作用"的
    只读工具才允许重试（审查报告 M10 —— 旧实现对所有工具都重试 2 次，包括会写台账的
    `upload_medical_report`）。
    """
    from rolecard_agent.core.consensus import build_consensus_tool
    from rolecard_agent.core.memory import make_memory_tool
    from rolecard_agent.core.tools.builtin import make_kernel_tools
    from rolecard_agent.core.tools.files import make_file_tools
    from rolecard_agent.core.tools.registry import ToolRegistry as _ToolRegistry
    from rolecard_agent.core.tools.web import make_web_tools
    from rolecard_agent.domains.health.tools import WRITE_TOOL_NAMES, make_domain_tools
    from rolecard_agent.rag.retriever import make_search_tool

    registry = _ToolRegistry()
    # Kernel tools carry domain=None and survive every plugin toggle. Both are read-only.
    registry.register_many(
        make_kernel_tools(roles=roles, enabled_domains=enabled_domains), idempotent=True
    )
    registry.register(make_search_tool(knowledge, tracer=tracer), idempotent=True)

    # 联网与工作区工具（v2.4）：全部只读除 fs_write 外。web_search 后端缺失时仍注册，
    # 运行期返回可读的未配置说明 —— 白名单引用的工具必须真实存在（一致性校验的前提）。
    effective_settings = settings or _lazy_settings()
    for web_tool in make_web_tools(settings=effective_settings):
        registry.register(web_tool, idempotent=True)
    registry.register_many(
        [t for t in make_file_tools(settings=effective_settings) if t.name != "fs_write"],
        idempotent=True,
    )
    registry.register_many(
        [t for t in make_file_tools(settings=effective_settings) if t.name == "fs_write"],
        idempotent=False,
    )

    # 多模型比对（consensus，用户 2026-09-17 开工）：内核能力（domain=None）。
    # 一次比对 = N 次 LLM 调用，失败不重试（idempotent=False）—— 重试等于成倍烧 token。
    registry.register(build_consensus_tool(settings=effective_settings), idempotent=False)

    # 跨会话记忆写入工具（v2.5，内核能力）：AI 检测到用户明确说出的可复用事实时调用。
    # 写入类工具不声明幂等（执行器不重试）；未接入连接 = 不注册（fail-closed）。
    if memory_conn is not None:
        registry.register(
            make_memory_tool(settings=effective_settings, conn=memory_conn),
            idempotent=False,
        )

    # Explicit per-domain wiring: what each domain needs to construct its tools, visible here.
    factories = {
        "health": lambda: make_domain_tools(
            ingestion, query, current_user=current_user, upload_dir=upload_dir
        ),
        # Data-only domain: no LLM tools, just a `domain_data` bucket the UI manages directly.
        "finance": lambda: [],
    }
    for domain in DOMAINS:
        if domain not in factories:
            raise ValueError(
                f"domain {domain!r} is registered but has no tool factory in build_registry()"
            )
        # 读写分开注册：写工具的重试开关必须关掉（见上）。声明式的名字清单来自域自身，
        # 装配点只做分流，不重复维护列表。
        domain_tools = factories[domain]()
        registry.register_many(
            [t for t in domain_tools if t.name not in WRITE_TOOL_NAMES],
            domain=domain,
            idempotent=True,
        )
        registry.register_many(
            [t for t in domain_tools if t.name in WRITE_TOOL_NAMES],
            domain=domain,
            idempotent=False,
        )
    return registry
