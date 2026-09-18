"""MCP 扩展通道（架构计划 C·§6.1）单元测试。

真实依赖 langchain_mcp_adapters 是可选的；本测试通过注入假模块覆盖 `_make_client` 的导入，
验证治理逻辑（前缀 / 描述裁剪 / 失败隔离 / 审计 / 域注册）与配置解析，无需真实安装。
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sqlite3
import sys
import types

import pytest
from langchain_core.tools import BaseTool

from rolecard_agent.config import McpServerConfig, Settings
from rolecard_agent.core.tools.mcp import load_mcp_tools
from rolecard_agent.core.tools.registry import ToolRegistry


class FakeTool(BaseTool):
    """A stand-in for a tool returned by an MCP server."""

    description: str = "fake"  # langchain_core 的 BaseTool.description 为必填字段

    def _run(self, **kwargs: object) -> str:
        return f"ran:{sorted(kwargs)}"

    async def _arun(self, **kwargs: object) -> str:
        return f"ran:{sorted(kwargs)}"


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Inject a fake `langchain_mcp_adapters` package whose client yields scripted tools."""
    fake_pkg = types.ModuleType("langchain_mcp_adapters")
    fake_client = types.ModuleType("langchain_mcp_adapters.client")

    state: dict[str, object] = {"tools": {}, "raise": set()}

    class FakeClient:
        def __init__(self, servers: dict[str, object]) -> None:
            self.server_id = next(iter(servers))

        async def get_tools(self) -> list[BaseTool]:
            if self.server_id in state["raise"]:
                raise RuntimeError(f"{self.server_id} boom")
            return list(state["tools"].get(self.server_id, []))  # type: ignore[arg-type]

    fake_client.MultiServerMCPClient = FakeClient  # type: ignore[attr-defined]
    fake_pkg.client = fake_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", fake_pkg)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", fake_client)

    orig = importlib.util.find_spec

    def _spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "langchain_mcp_adapters":
            return importlib.machinery.ModuleSpec(name, None)
        return orig(name, *args, **kwargs)  # type: ignore[call-arg]

    monkeypatch.setattr(importlib.util, "find_spec", _spec)
    return state


def test_empty_returns_nothing() -> None:
    assert load_mcp_tools([]) == []


def test_missing_dependency_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = importlib.util.find_spec

    def _spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "langchain_mcp_adapters":
            return None
        return orig(name, *args, **kwargs)  # type: ignore[call-arg]

    monkeypatch.setattr(importlib.util, "find_spec", _spec)
    monkeypatch.delitem(sys.modules, "langchain_mcp_adapters", raising=False)
    assert load_mcp_tools([McpServerConfig(id="a")]) == []


def test_name_prefix_avoids_collision(fake_mcp: dict[str, object]) -> None:
    cast = fake_mcp
    cast["tools"]["a"] = [FakeTool(name="echo")]  # type: ignore[index]
    cast["tools"]["b"] = [FakeTool(name="echo")]  # type: ignore[index]
    tools = load_mcp_tools([McpServerConfig(id="a"), McpServerConfig(id="b")])
    assert sorted(t.name for t in tools) == ["a__echo", "b__echo"]


def test_description_truncated(fake_mcp: dict[str, object]) -> None:
    cast = fake_mcp
    long_tool = FakeTool(name="echo", description="z" * 2000)
    cast["tools"]["a"] = [long_tool]  # type: ignore[index]
    tools = load_mcp_tools([McpServerConfig(id="a")])
    assert tools[0].description == "z" * 1024


def test_failure_isolation(fake_mcp: dict[str, object]) -> None:
    cast = fake_mcp
    cast["tools"]["good"] = [FakeTool(name="echo")]  # type: ignore[index]
    cast["raise"].add("bad")  # type: ignore[attr-defined]
    tools = load_mcp_tools([McpServerConfig(id="good"), McpServerConfig(id="bad")])
    assert [t.name for t in tools] == ["good__echo"]


def test_audit_written_on_invoke(fake_mcp: dict[str, object]) -> None:
    cast = fake_mcp
    cast["tools"]["a"] = [FakeTool(name="echo")]  # type: ignore[index]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, "
        "action TEXT, target TEXT, detail_json TEXT)"
    )
    tools = load_mcp_tools([McpServerConfig(id="a")], conn=conn)
    tools[0].invoke({})
    rows = conn.execute("SELECT action, target FROM audit_log").fetchall()
    assert any(r["action"].startswith("mcp:") for r in rows)
    assert any(r["target"] == "mcp:a" for r in rows)


def test_registered_as_mcp_domain(fake_mcp: dict[str, object]) -> None:
    cast = fake_mcp
    cast["tools"]["a"] = [FakeTool(name="echo")]  # type: ignore[index]
    registry = ToolRegistry()
    tools = load_mcp_tools([McpServerConfig(id="a")])
    registry.register_many(tools, domain="mcp", idempotent=False)
    enabled = registry.select(enabled_domains=["mcp"], role_whitelist=None)
    assert {t.name for t in enabled} == {"a__echo"}
    # 域未启用时被过滤掉（两阶段过滤的 stage 1）
    assert registry.select(enabled_domains=[], role_whitelist=None) == []


def test_settings_parses_mcp_servers() -> None:
    settings = Settings.from_env(
        {
            "MCP_SERVERS": '[{"id":"s1","transport":"stdio","command":"python",'
            '"args":["-m","x"]}]'
        }
    )
    assert len(settings.mcp_servers) == 1
    assert settings.mcp_servers[0].id == "s1"
    assert settings.mcp_servers[0].args == ["-m", "x"]


def test_settings_rejects_bad_mcp_servers() -> None:
    with pytest.raises(ValueError):
        Settings.from_env({"MCP_SERVERS": "not-json"})
