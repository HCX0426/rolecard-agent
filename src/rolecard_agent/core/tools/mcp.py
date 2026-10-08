"""MCP 扩展通道（架构计划 C·§6.1）。

把外部 MCP server 暴露的工具作为「域工具」(domain="mcp") 注册进 ToolRegistry，
复用既有角色白名单 / 超时 / 熔断 / 审计。

权限边界（铁律）：MCP server 只提供裸能力；白名单、路径边界（_resolve_within）、
权限档位仍在本家，guard 与路径守卫绝不委托给外部 server。

治理规则：
- 工具名冲突 → 加 `{server_id}__` 前缀；
- 描述过长 → 裁剪到 MAX_MCP_DESC，避免 bind_tools 载荷过大；
- 单个 server 连接失败 → 隔离，不拖垮其余 server、不阻塞启动；
- 每个 MCP 工具调用前后写 audit_log（actor="agent"）。

依赖：langchain_mcp_adapters（见 requirements-mcp.txt）。未安装时即便配置了
MCP_SERVERS 也只打 warning 跳过，不阻塞启动（fail-open 仅影响扩展能力）。
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any, cast

from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from rolecard_agent.base.audit import AGENT_ACTOR, AuditTrail
from rolecard_agent.base.observability import logline
from rolecard_agent.config import McpServerConfig

# bind_tools 把每个工具的 name + description + JSON schema 一起发给模型。描述过长会白白
# 撑大每轮 prompt 载荷；截断到一个够用的上限即可（架构计划 §6.1 治理项）。
MAX_MCP_DESC = 1024

# MCP 是**可选扩展层**（与 OCR/RAG 同层，不进 requirements.txt 内核）。没装 adapters 时
# 一律给这条可操作提示，而不是裸 ModuleNotFoundError（用户点"测试连接"时看得懂下一步）。
MISSING_ADAPTER_HINT = "MCP 依赖未安装：运行 pip install -r requirements-mcp.txt"


def _adapters_installed() -> bool:
    return importlib.util.find_spec("langchain_mcp_adapters") is not None


def _connection(cfg: McpServerConfig) -> dict[str, Any]:
    """Build the connection dict for one server, transport-aware."""
    if cfg.transport == "http":
        conn: dict[str, Any] = {"url": cfg.url, "transport": "http"}
        if cfg.headers:
            conn["headers"] = dict(cfg.headers)
        return conn
    # stdio（默认）
    conn = {"command": cfg.command, "args": list(cfg.args), "transport": "stdio"}
    if cfg.env:
        conn["env"] = dict(cfg.env)
    return conn


def _make_client(cfg: McpServerConfig) -> Any:
    """Construct a MultiServerMCPClient for exactly one server.

    Isolated per-server so a misbehaving server cannot poison the others. Lazy import so the
    dependency is only required when MCP is actually configured.
    """
    if not _adapters_installed():
        raise ImportError(MISSING_ADAPTER_HINT)
    from langchain_mcp_adapters.client import MultiServerMCPClient

    # _connection 返回的是 transport 相关的动态 dict；cast 让 mypy 在"装了 adapters"时也不
    # 纠结它的具体 TypedDict 分支（未装时它本就是 Any）。
    return MultiServerMCPClient(cast(Any, {cfg.id: _connection(cfg)}))


async def _fetch_one(cfg: McpServerConfig) -> list[BaseTool]:
    client = _make_client(cfg)
    return await client.get_tools()


class AuditedMcpTool(BaseTool):
    """Delegates to a raw MCP tool and writes an audit row around each call.

    The raw tool carries the real schema; this wrapper only adds provenance (the server id in
    the audit `target`) and the audit trail. Everything else - args, return shape - passes
    through untouched, so the model sees the original tool, not a re-described one.
    """

    name: str
    description: str
    # 透传 raw 工具的入参 schema：新版 langchain-mcp-adapters 给的是 JSON dict，旧的/BaseModel
    # 工具给的是模型类 → 类型放宽到 Any 原样带过，交给 BaseTool 解析（此前按 type[BaseModel]
    # 收窄会让真连 MCP 时 ValidationError 直接崩，mock 测不到）。
    args_schema: Any = None

    _raw: BaseTool = PrivateAttr()
    _conn: Any = PrivateAttr(default=None)
    _server_id: str = PrivateAttr()

    def __init__(self, raw: BaseTool, *, conn: Any, server_id: str) -> None:
        super().__init__(
            name=f"{server_id}__{raw.name}",
            description=(raw.description or "")[:MAX_MCP_DESC],
            args_schema=getattr(raw, "args_schema", None),
        )
        self._raw = raw
        self._conn = conn
        self._server_id = server_id

    def _audit(self, phase: str, detail: dict[str, Any]) -> None:
        # 这一族动作名带 server/tool 变量，所以它不在 `AUDIT_ACTIONS` 里，而在
        # `DYNAMIC_ACTION_PREFIXES`（`mcp:`）那一档（`R102-14`）。
        try:
            AuditTrail(self._conn).log(
                actor=AGENT_ACTOR,
                action=f"mcp:{self.name}",
                target=f"mcp:{self._server_id}",
                detail={"phase": phase, **detail},
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败绝不应影响工具结果
            logline("warning", "mcp", f"审计写入失败：{self.name}: {exc}")

    def _run(self, **kwargs: Any) -> Any:
        self._audit("invoke", {"args": kwargs})
        try:
            # MCP 工具是 async-only（StructuredTool.invoke 会 NotImplementedError）。
            # 而本项目的执行器**同步**调 tool.invoke(...)（core/agent/nodes.py），故同步路径必须
            # 桥接到 raw 的 ainvoke —— 在调用方线程里跑一个事件循环（_run_async 已处理"已在
            # loop 中"的情况）。真连开源 MCP server 才发现，mock 的假工具带 sync _run 掩盖了它。
            return _run_async(self._raw.ainvoke(kwargs))
        except Exception as exc:
            self._audit("error", {"error": f"{type(exc).__name__}: {exc}"})
            raise

    async def _arun(self, **kwargs: Any) -> Any:
        self._audit("invoke", {"args": kwargs})
        try:
            return await self._raw.ainvoke(kwargs)
        except Exception as exc:
            self._audit("error", {"error": f"{type(exc).__name__}: {exc}"})
            raise


def _run_async(coro: Any) -> Any:
    """Run a coroutine, tolerating an already-running event loop.

    `build_registry` runs inside `create_app` (a plain sync function), so the normal case is a
    fresh `asyncio.run`. But under pytest-asyncio or other already-async callers, `asyncio.run`
    raises "cannot run loop while another loop is running" - in that case run it on a dedicated
    thread so an external tool load never blocks startup.

    **先问"有没有 loop 在场"，而不是靠 catch RuntimeError 分流**（覆盖率补错误路径时抓出来的）：
    从前写成 `try: asyncio.run(coro) except RuntimeError: 换线程重试`，而**工具自己抛的
    `RuntimeError`**（上游 5xx、连接断、库内部几乎都用这个类）会被同一个 except 接住，
    于是拿一条已经 await 过的协程去重跑 —— 调用方最后看到的是
    `cannot reuse already awaited coroutine`，真正的错**连人带审计记录一起被抹掉**。
    这条支路只在真连 MCP 时暴露，而那时最不该丢的就是原始异常。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # 没有 loop 在场：正常跑，工具的异常原样往上抛
    import concurrent.futures as _cf

    with _cf.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def load_mcp_tools(servers: list[McpServerConfig], *, conn: Any = None) -> list[BaseTool]:
    """Load and wrap MCP tools for every configured server.

    Returns the list of `domain="mcp"`-ready BaseTools (caller registers them). Empty list when
    nothing is configured, or when the adapter dependency is missing/servers all fail (logged).
    """
    if not servers:
        return []
    if importlib.util.find_spec("langchain_mcp_adapters") is None:
        logline(
            "warning",
            "mcp",
            "配了 MCP_SERVERS 但没装 langchain-mcp-adapters，MCP 工具跳过"
            "（pip install -r requirements-mcp.txt）",
        )
        return []

    async def collect() -> list[BaseTool]:
        out: list[BaseTool] = []
        for cfg in servers:
            try:
                raw_tools = await _fetch_one(cfg)
            except Exception as exc:  # noqa: BLE001 - 隔离单个 server 故障
                logline("warning", "mcp", f"MCP server {cfg.id} 加载失败：{exc}")
                continue
            for raw in raw_tools:
                out.append(AuditedMcpTool(raw, conn=conn, server_id=cfg.id))
        return out

    return _run_async(collect())
