"""域注册表：**目录枚举 + 每域自描述 `SPEC`** —— 中心代码从此不点名任何具体域。

两件不同的事仍刻意分开（原实现如此，本文件保持）：

  * **REGISTERED** —— 哪些域存在，即哪些代码在场。判据是 `domains/` 下的**目录**：每个
    子包导出一份 `DomainSpec`（`SPEC`），由 `discover_specs()` 枚举出来。新增一个域 =
    新增一个目录，本文件、`api/main.py`、`core/bootstrap.py`、`roles/seed.py` 一行不改；
    目录缺 `SPEC` 或 `spec.id` 与目录名不等 ⇒ **启动即红**（域的工具永远绑不上是本项目
    最不能接受的那种静默失败）。
  * **ENABLED** —— 注册域里哪些当前被打开。`plugin` 表，操作员动作。

`scripts/init_db.py` 与 `storage.db.bootstrap` 对每个注册域套用其 `schema.sql`，表因此
永远存在，重新启用插件不需要 DDL。

To add a domain: implement models / service / tools / schema.sql under `domains/<name>/`,
add a `seed.py` if it ships roles, and export `SPEC` from `domains/<name>/__init__.py`.
Nothing outside that directory needs to change.

IMPORTANT: runtime enable/disable is an OPERATOR action backed by the plugin table.
It is never exposed as an LLM-callable tool (self-authorization risk, same as switch_role).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rolecard_agent.domains.spec import DomainSpec, DomainToolContext

if TYPE_CHECKING:
    from rolecard_agent.core.domain_service import DomainQueryService
    from rolecard_agent.core.ingest.ingestion import IngestionService
    from rolecard_agent.core.tools.builtin import DomainsLike
    from rolecard_agent.core.tools.registry import ToolRegistry
    from rolecard_agent.rag.retriever import KnowledgeBase
    from rolecard_agent.roles.models import RoleCardCreate
    from rolecard_agent.roles.service import RoleCardService
    from rolecard_agent.storage.db import SqlConnection

#: 本包在 `sys.modules` 里的名字 —— 目录枚举按 `<本包>.<目录名>` 导入各域。
_PACKAGE = __package__ or "rolecard_agent.domains"
_PACKAGE_DIR = Path(__file__).resolve().parent


def _domain_dirs(root: Path | None = None) -> list[Path]:
    """`domains/` 下的候选目录（排序稳定 ⇒ 域顺序稳定）。

    下划线/点开头的一律跳过（`__pycache__` 与约定俗成的私有目录）；其余每个目录都**必须
    是包** —— 一个只有 schema.sql 却没有 `__init__.py` 的域会在这里报错，而不是等到
    `schema_files()` 才 FileNotFoundError。
    """
    base = root or _PACKAGE_DIR
    dirs: list[Path] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if not (child / "__init__.py").is_file():
            raise ValueError(
                f"域目录 {child.name!r} 不是包（缺 __init__.py）—— "
                "注册域必须导出 DomainSpec，见 domains/spec.py"
            )
        dirs.append(child)
    return dirs


def _load_spec(name: str) -> DomainSpec:
    """导入一个域包并取它的 `SPEC`。缺声明 / id 对不上目录名一律 loud。"""
    module = importlib.import_module(f"{_PACKAGE}.{name}")
    spec = getattr(module, "SPEC", None)
    if not isinstance(spec, DomainSpec):
        raise ValueError(
            f"域包 {name!r} 没有导出 SPEC（DomainSpec）—— "
            "注册域必须自描述（工具工厂 / 查询服务 / 种子角色 / 写工具名），见 domains/spec.py"
        )
    if spec.id != name:
        raise ValueError(f"域 {name!r} 的 SPEC.id={spec.id!r} 与目录名不一致")
    return spec


def discover_specs(root: Path | None = None) -> tuple[DomainSpec, ...]:
    """目录枚举出全部注册域的 `SPEC`（按目录名排序；重复 id 不可能出现，id 即目录名）。"""
    specs = tuple(_load_spec(d.name) for d in _domain_dirs(root))
    ids = [s.id for s in specs]
    if len(set(ids)) != len(ids):
        raise ValueError(f"重复的域 id: {ids}")
    return specs


#: 注册域 id。**不是手写清单** —— 它是 `discover_specs()` 的投影（2026-10-04 审查快照的
#: 域机制条目：从前这里是一行手写元组，与 factories 字典、`api/main.py` 的接线、
#: `roles/seed.py` 的工具名共六处登记，新增一域要改六处）。每个 id 必须同时匹配
#: `domains/<id>/` 目录与 `core/schema.sql` 的 plugin 表（后者由 `seed_plugin_rows` 播种）。
DOMAINS: tuple[str, ...] = tuple(spec.id for spec in discover_specs())


def domain_seed_roles() -> tuple[RoleCardCreate, ...]:
    """全部注册域的出厂种子角色 —— 由各域 `SPEC.seed_roles` 聚合。

    `roles/seed.py` 从此前的域角色清单里解放出来：内核不认识任何域名，域角色是**域的
    概念**，跟着自己的 SPEC 出厂。装配根（`core.bootstrap`）拿宿主注入的这一份去播种。
    """
    return tuple(role for spec in discover_specs() for role in spec.seed_roles)


def build_query_services(conn: SqlConnection) -> dict[str, DomainQueryService]:
    """按域 id 建各自的查询服务（无查询服务的域不进这张映射）。

    这是 `api/main.py` 里那句 isinstance 接线的替身：装配点不再"把唯一一个服务喂给唯一
    一个工厂"，每个域的工具工厂按自己的 `spec.id` 取自己那份 —— 喂错域没有中间态。
    """
    return {
        spec.id: spec.query_service_factory(conn)
        for spec in discover_specs()
        if spec.query_service_factory is not None
    }


def _lazy_settings() -> Any:
    """兜底默认配置（仅测试直接调用 build_registry 时会走到）。"""
    from rolecard_agent.config import Settings

    return Settings()


def build_registry(
    *,
    roles: RoleCardService,
    ingestion: IngestionService,
    query_services: Mapping[str, DomainQueryService],
    knowledge: KnowledgeBase,
    enabled_domains: DomainsLike,
    current_user: Callable[[], str],
    upload_dir: Path,
    tracer: Any = None,
    settings: Any = None,
    memory_conn: Any = None,
    fs_conn: Any = None,
    specs: Sequence[DomainSpec] | None = None,
) -> ToolRegistry:
    """Assemble the full tool registry: kernel tools + every registered domain's tools.

    各域的工具怎么拼，是**该域自己的 `SPEC.tool_factory`** 说的（本文件只照单执行）；
    装配点不再 import 任何具体域，也不再维护 factories 字典 —— 那份字典正是"注册了 id
    却忘了接工厂"的静默失败源，如今缺声明在 `discover_specs()` 就已经红了。

    `query_services` 是"域 id → 该域查询服务"的映射（宿主用 `build_query_services` 建）：
    每个域的工厂只拿到**自己 id** 下的那一件，喂错域没有中间态。

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

    `fs_conn` = 工作区文件工具的 SQLite 连接：既供「任务目录」DB 覆盖的**实时解析**
    （保存即生效，见 core/common/workspace.py），也供 fs 工具的**审计写入**（actor="agent"）。
    None = fs 工具回落 env workspace_dir 且不审计（测试场景）。

    `specs` 缺省时现场目录枚举；测试可用它注入一个假域，验证"新增域零改中心代码"。

    工具的 `idempotent` 标记是**执行器的重试开关**：只有显式声明"重复调用无副作用"的
    只读工具才允许重试（审查报告 M10 —— 旧实现对所有工具都重试 2 次，包括会写台账的
    `upload_medical_report`）。
    """
    from rolecard_agent.core.consensus import build_consensus_tool
    from rolecard_agent.core.memory import make_memory_tool
    from rolecard_agent.core.tools.builtin import make_kernel_tools
    from rolecard_agent.core.tools.files import make_file_tools
    from rolecard_agent.core.tools.registry import ToolRegistry as _ToolRegistry
    from rolecard_agent.core.tools.run import make_run_tool
    from rolecard_agent.core.tools.web import make_web_tools
    from rolecard_agent.rag.retriever import make_search_tool

    registry = _ToolRegistry()
    # Kernel tools carry domain=None and survive every plugin toggle. Both are read-only.
    registry.register_many(
        make_kernel_tools(
            roles=roles, current_user=current_user, enabled_domains=enabled_domains
        ),
        idempotent=True
    )
    registry.register(make_search_tool(knowledge, tracer=tracer), idempotent=True)

    # 联网与工作区工具（v2.4）：全部只读除 fs_write 外。web_search 后端缺失时仍注册，
    # 运行期返回可读的未配置说明 —— 白名单引用的工具必须真实存在（一致性校验的前提）。
    # fs 工具在 v2.5 file1 升级：根 = 每调用实时解析的「任务目录」（DB 覆盖 or env），
    # 且全部操作写审计（actor="agent"）—— 见 core/common/workspace.py 与 core/tools/files.py。
    effective_settings = settings or _lazy_settings()
    for web_tool in make_web_tools(settings=effective_settings):
        registry.register(web_tool, idempotent=True)
    file_tools = make_file_tools(settings=effective_settings, conn=fs_conn)
    registry.register_many(
        [t for t in file_tools if t.name != "fs_write"],
        idempotent=True,
    )
    registry.register_many(
        [t for t in file_tools if t.name == "fs_write"],
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

    # 命令执行（v2.6，架构计划 C·§6.2）：内核能力（domain=None），idempotent=False ——
    # 执行会真实产生副作用，执行器绝不重试。审批档 manual 时"批准后后台执行一次"
    # （run_approval_execution），工具随后的调用只是读回历史结果 —— 不重复跑命令。
    # 未接连接（conn=None）也注册：白名单引用的工具必须真实存在，审批/审计/任务目录
    # 解析在测试宿主里回落"不传 conn 的分支"（fail-open 只影响留痕，不影响权限）。
    registry.register(
        make_run_tool(settings=effective_settings, conn=fs_conn),
        idempotent=False,
    )

    # 每个域的工具由**它自己的 SPEC** 声明：这里只负责按域分流注册（读工具可重试，
    # `spec.write_tool_names` 里的写工具不重试）。分流判据来自域自身，装配点不抄清单。
    for spec in specs if specs is not None else discover_specs():
        domain_tools = spec.tool_factory(
            DomainToolContext(
                ingestion=ingestion,
                query=query_services.get(spec.id),
                current_user=current_user,
                upload_dir=upload_dir,
            )
        )
        registry.register_many(
            [t for t in domain_tools if t.name not in spec.write_tool_names],
            domain=spec.id,
            idempotent=True,
        )
        registry.register_many(
            [t for t in domain_tools if t.name in spec.write_tool_names],
            domain=spec.id,
            idempotent=False,
        )

    # MCP 扩展通道（架构计划 C·§6.1）：外部 server 工具作为 domain="mcp" 域工具注册，
    # 复用白名单 / 超时 / 熔断 / 审计。无配置、依赖缺失、或所有 server 加载失败时静默跳过
    # （fail-open 仅影响扩展能力，绝不阻塞启动）。
    if settings is not None:
        from rolecard_agent.core.tools.mcp import load_mcp_tools

        mcp_tools = load_mcp_tools(getattr(settings, "mcp_servers", []) or [], conn=fs_conn)
        if mcp_tools:
            registry.register_many(mcp_tools, domain="mcp", idempotent=False)
    return registry


__all__ = [
    "DOMAINS",
    "DomainSpec",
    "build_query_services",
    "build_registry",
    "discover_specs",
    "domain_seed_roles",
]
