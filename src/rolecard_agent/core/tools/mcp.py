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
import json
import logging
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, PrivateAttr

from rolecard_agent.config import McpServerConfig

logger = logging.getLogger(__name__)

# bind_tools 把每个工具的 name + description + JSON schema 一起发给模型。描述过长会白白
# 撑大每轮 prompt 载荷；截断到一个够用的上限即可（架构计划 §6.1 治理项）。
MAX_MCP_DESC = 1024


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
    from langchain_mcp_adapters.client import MultiServerMCPClient

    return MultiServerMCPClient({cfg.id: _connection(cfg)})


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
    args_schema: type[BaseModel] | None = None

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
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO audit_log (actor, action, target, detail_json) VALUES (?, ?, ?, ?)",
                (
                    "agent",
                    f"mcp:{self.name}",
                    f"mcp:{self._server_id}",
                    json.dumps({"phase": phase, **detail}, default=str),
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - 审计失败绝不应影响工具结果
            logger.warning("mcp audit write failed for %s: %s", self.name, exc)

    def _run(self, **kwargs: Any) -> Any:
        self._audit("invoke", {"args": kwargs})
        try:
            return self._raw.invoke(kwargs)
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
    """
    try:
        return asyncio.run(coro)
    except RuntimeError:
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
        logger.warning(
            "MCP_SERVERS is set but langchain_mcp_adapters is not installed; "
            "skipping MCP tools (pip install -r requirements-mcp.txt)"
        )
        return []

    async def collect() -> list[BaseTool]:
        out: list[BaseTool] = []
        for cfg in servers:
            try:
                raw_tools = await _fetch_one(cfg)
            except Exception as exc:  # noqa: BLE001 - 隔离单个 server 故障
                logger.warning("MCP server %s failed to load: %s", cfg.id, exc)
                continue
            for raw in raw_tools:
                out.append(AuditedMcpTool(raw, conn=conn, server_id=cfg.id))
        return out

    return _run_async(collect())
