"""MCP 工具层的失败分支（覆盖率基线点名的 `core/tools/mcp.py` 75%）。

`test_mcp_api.py` 钉的是**端点面**（SSRF 边界、headers 只写不回读、审计）—— 它把
`load_mcp_tools` 整个 patch 掉了，所以这一层的内部支路一条都没走到。这一族恰好全是
"真连了才知道"的形状：

* 两种 transport 的连接字典（http 带 url / stdio 带 command，**可选键只在有值时出现** ——
  多传一个空 dict 进去，某些版本的 adapters 会把它当成"要覆盖环境"而不是"没设"）；
* 没装 adapters 时报错要带**下一步**（裸 ModuleNotFoundError 用户不知道要装哪一份
  requirements），而加载入口自己也要挡这一格；
* **审计失败绝不影响工具结果**（`_audit` 吞异常并出声）：审计是留痕，不是执行的前提，
  让它把一次成功的工具调用变成异常，等于用观测面推翻事实面；
* 工具本身抛异常时，error 那一笔要落、异常要原样再抛（吞掉就变成"工具返回了个怪结果"）；
* 单个 server 挂掉不许带走别的 server（隔离）。

同步 `_run` 那条桥（本项目执行器同步调 `tool.invoke`，而 MCP 工具是 async-only）也在这里，
它以前只有真连过 MCP 才会暴露 —— mock 的假工具带 sync `_run` 会掩盖它（文件里那句原话）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool

import rolecard_agent.core.tools.mcp as mcp_mod
from rolecard_agent.config import McpServerConfig
from rolecard_agent.storage import db as db_mod

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


class _Raw(BaseTool):
    """一个 async-only 的假 MCP 工具：`_run` 抛 `NotImplementedError` —— 照抄真货的形状。

    真连时 `_raw` 是 `StructuredTool`，它**只有** `_arun`；`invoke()` 会当场抛
    NotImplementedError（文件注释原话）。所以假工具若给一个能用的 `_run`，就会把
    "同步桥必须存在"这件事掩盖掉 —— 这一格曾经真就是这么漏的。
    """

    name: str = "do"
    description: str = "做一件小事"

    def _run(self, **kwargs: Any) -> Any:  # noqa: ARG002
        raise NotImplementedError("MCP 工具是 async-only")

    async def _arun(self, **kwargs: Any) -> str:
        return f"ok:{sorted(kwargs)}"


class _BoomRaw(BaseTool):
    name: str = "boom"
    description: str = "总是失败"

    def _run(self, **kwargs: Any) -> Any:  # noqa: ARG002
        raise NotImplementedError("MCP 工具是 async-only")

    async def _arun(self, **kwargs: Any) -> str:  # noqa: ARG002
        raise RuntimeError("上游炸了")


@pytest.fixture
def conn(tmp_path: Path):
    c = db_mod.connect(tmp_path / "audit.db")
    db_mod.bootstrap(c, enabled_domains=("health",))
    yield c
    c.close()


# ---------------------------------------------------------------- 连接字典（transport 两支）


def test_http_连接只带它该有的键且空值不出现() -> None:
    plain = mcp_mod._connection(McpServerConfig(id="s", transport="http", url="http://x/mcp"))
    assert plain == {"url": "http://x/mcp", "transport": "http"}, plain
    with_hdr = mcp_mod._connection(
        McpServerConfig(id="s", transport="http", url="http://x", headers={"A": "b"})
    )
    assert with_hdr["headers"] == {"A": "b"}


def test_stdio_连接带_command_args_而_env_空时不出现() -> None:
    cfg = McpServerConfig(id="s", command="python", args=["-m", "srv"])
    assert mcp_mod._connection(cfg) == {
        "command": "python",
        "args": ["-m", "srv"],
        "transport": "stdio",
    }
    with_env = mcp_mod._connection(
        McpServerConfig(id="s", command="python", env={"TOKEN": "t"})
    )
    assert with_env["env"] == {"TOKEN": "t"}


# --------------------------------------------------------------- 没装依赖时的两种出声


def test_没装_adapters_时构造客户端报的是下一步而非裸异常(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod.importlib.util, "find_spec", lambda _n: None, raising=True)
    with pytest.raises(ImportError) as got:
        mcp_mod._make_client(McpServerConfig(id="s", command="python"))
    assert "requirements-mcp.txt" in str(got.value), "错误信息必须给下一步"


def test_没装_adapters_时加载入口跳过而不是崩(monkeypatch, capsys) -> None:
    """配了 MCP_SERVERS 但没装依赖 ⇒ 空清单 + 一句 warning（不能让进程起不来）。"""
    monkeypatch.setattr(mcp_mod.importlib.util, "find_spec", lambda _n: None, raising=True)
    assert mcp_mod.load_mcp_tools([McpServerConfig(id="s", command="python")]) == []
    err = capsys.readouterr().err
    assert "没装 langchain-mcp-adapters" in err and "requirements-mcp.txt" in err


def test_没配任何_server时直接空且不碰依赖(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_mod.importlib.util, "find_spec", lambda _n: object(), raising=True
    )  # 就算装了也不该走那条路
    assert mcp_mod.load_mcp_tools([]) == []


# --------------------------------------------------------------------- 审计与异常的形状


def test_工具调用前后都留痕且结果原样透传(conn) -> None:
    tool = mcp_mod.AuditedMcpTool(_Raw(), conn=conn, server_id="srv1")
    assert tool.name == "srv1__do", "名字带 server 前缀是防撞名，也是审计 target 的来源"
    assert tool.invoke({"q": "1"}) == "ok:['q']"
    rows = conn.execute("SELECT action, target FROM audit_log ORDER BY id").fetchall()
    actions = [str(r["action"]) for r in rows]
    assert "mcp:srv1__do" in actions, actions
    assert rows[0]["target"] == "mcp:srv1", "target 说的是哪个 server，不是工具名"


def test_审计写失败绝不能推翻一次成功的工具调用(conn, capsys) -> None:
    """审计是留痕不是执行前提：让观测面把事实面掀了，是本仓最不能接受的一类"更正确"。"""
    tool = mcp_mod.AuditedMcpTool(_Raw(), conn=conn, server_id="srv1")
    conn.close()  # 之后任何写入都会抛 —— 模拟审计面坏掉
    assert tool.invoke({"q": "1"}) == "ok:['q']", "审计坏了工具就该照样成功"
    assert "审计写入失败" in capsys.readouterr().err, "但不能静默：这一声是唯一的线索"


def test_工具自己抛异常时_error_落痕且异常原样再抛(conn) -> None:
    """工具的真异常不许被同步桥的 catch 洗成 `cannot reuse already awaited coroutine`。

    这是补错误路径时抓到的真缺陷：`_run_async` 从前用 `try: asyncio.run() except RuntimeError:`
    分流，而**工具自己抛的 RuntimeError**（上游几乎都用这个类）会被同一个 except 接住，
    拿跑过的协程重跑 → 调用方与审计记录看到的都是那句内部噪声，真错误彻底消失。
    """
    tool = mcp_mod.AuditedMcpTool(_BoomRaw(), conn=conn, server_id="srv1")
    with pytest.raises(RuntimeError) as got:
        tool.invoke({})
    assert "上游炸了" in str(got.value), f"原始错误被掩盖了：{got.value!r}"
    assert "reuse" not in str(got.value), "内部噪声不许顶替真错误"
    import json

    rows = conn.execute("SELECT detail_json FROM audit_log ORDER BY id").fetchall()
    details = [json.loads(str(r["detail_json"])) for r in rows]
    errs = [d for d in details if d.get("phase") == "error"]
    assert errs, f"error 那一笔没落：{details}"
    assert "上游炸了" in errs[0]["error"], errs[0]


def test_异步路径同样留痕且异常原样抛(conn) -> None:
    """`_arun` 是注册表真跑异步时走的那条支路（同步 `_run` 只是桥）。

    两条路都得各自留痕、各自原样抛 —— 只测同步那条，异步那条的审计就可能在某次改动后
    静默消失（两份代码各写各的下场，本仓在审计咽喉那格见过五次）。
    """
    import asyncio

    ok = mcp_mod.AuditedMcpTool(_Raw(), conn=conn, server_id="srv1")
    assert asyncio.run(ok.ainvoke({"q": "2"})) == "ok:['q']"
    boom = mcp_mod.AuditedMcpTool(_BoomRaw(), conn=conn, server_id="srv1")
    with pytest.raises(RuntimeError, match="上游炸了"):
        asyncio.run(boom.ainvoke({}))
    import json

    rows = conn.execute("SELECT detail_json FROM audit_log ORDER BY id").fetchall()
    phases = [json.loads(str(r["detail_json"])).get("phase") for r in rows]
    assert phases.count("invoke") == 2 and "error" in phases, phases


def test_一个_server挂掉不带走别的(monkeypatch, conn) -> None:
    async def fake(cfg: McpServerConfig) -> list[BaseTool]:
        if cfg.id == "bad":
            raise ConnectionError("连不上")
        return [_Raw()]

    monkeypatch.setattr(mcp_mod, "_fetch_one", fake, raising=True)
    monkeypatch.setattr(
        mcp_mod.importlib.util, "find_spec", lambda _n: object(), raising=True
    )
    tools = mcp_mod.load_mcp_tools(
        [McpServerConfig(id="good", command="p"), McpServerConfig(id="bad", command="p")],
        conn=conn,
    )
    assert [t.name for t in tools] == ["good__do"], tools
    # 挂掉那个 server 只留一句 warning，不许把整次加载带走
    assert "good" in [t.name.split("__")[0] for t in tools]


async def _coro_ok() -> int:
    return 42


def test_同步桥在已有事件循环里也能跑() -> None:
    """本项目执行器同步调 invoke，但调用方可能已经在 loop 里（pytest-asyncio、桌宠壳）。

    `asyncio.run` 在运行中的 loop 里会抛 RuntimeError —— 那一条支路的"另起一个线程跑完"
    才是这里要走的；不测它，就等着真在异步宿主里第一次调用时崩。
    """
    import asyncio

    # 先看普通情形：没有 loop 在场，直接 asyncio.run
    assert mcp_mod._run_async(_coro_ok()) == 42

    async def inside() -> Any:
        # 已经在 loop 里：同步 `_run_async` 必须走那条线程支路，而不是把 RuntimeError 抛出来
        return mcp_mod._run_async(_coro_ok())

    assert asyncio.run(inside()) == 42
