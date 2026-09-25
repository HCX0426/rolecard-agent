"""Shared fixtures.

Everything here is offline: no Ollama, no network, no model downloads. If a test needs a
model it gets `ScriptedChat`, which replays a fixed list of replies. That constraint is what
lets `pytest` run in CI on any machine.

`with TestClient(app)` 会跑 lifespan，而 lifespan 的启动预热是**真** HTTP 调用（把默认本地
模型按 num_ctx 钉进显存）—— 它不在任何断言里，却把 8B 装进 GPU 与测试抢资源。会话级
autouse fixture 把它关掉，是这条离线铁律的最后一块（架构审计报告 §6 隐蔽外呼）。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool, tool

from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import bootstrap, connect

# --------------------------------------------------------------------------- offline guard


@pytest.fixture(scope="session", autouse=True)
def _no_startup_model_pin() -> Iterator[None]:
    """整个测试会话都不做启动预热（`MODEL_PIN_ON_STARTUP=0`，见 config.py 该字段的理由）。

    同时关掉**自动提取记忆**（`MEMORY_EXTRACT_AUTO=0`）：那是一次后台真模型调用，它会把
    ScriptedChat 的脚本回复吃掉一条 —— 断言"这一轮回复是 build-N"的用例就会偶发错位。
    提取自身的用例（test_memory_distill）单独把它打开再测，不在这里偷开。
    """
    previous = {key: os.environ.get(key) for key in ("MODEL_PIN_ON_STARTUP", "MEMORY_EXTRACT_AUTO")}
    os.environ["MODEL_PIN_ON_STARTUP"] = "0"
    os.environ["MEMORY_EXTRACT_AUTO"] = "0"
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --------------------------------------------------------------------------- model fake


class ScriptedChat:
    """Replays queued replies and records what it was asked.

    Records the tools visible at each call, which is how the whitelist tests assert that
    filtering happened *before* binding rather than at execution time.
    """

    def __init__(self, replies: Sequence[BaseMessage] | None = None) -> None:
        self.replies: list[BaseMessage] = list(replies or [])
        self.calls: list[dict[str, Any]] = []
        self._bound: list[str] | None = None

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChat:
        self._bound = sorted(getattr(t, "name", str(t)) for t in tools)
        return self

    def _next_reply(self, input: Any) -> BaseMessage:  # noqa: A002
        self.calls.append({"tools": self._bound, "messages": list(input)})
        if not self.replies:
            return AIMessage(content="(script exhausted)")
        return self.replies.pop(0)

    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:  # noqa: A002
        return self._next_reply(input)

    def stream(self, input: Any, **kwargs: Any) -> Any:  # noqa: A002
        """内核现在走流（`call_model` 要能在分块边界收手，见 #18）。

        这里一次交出**整块**：节点的累加是 `acc + chunk`，单块就是恒等情形 —— 所以既有那些
        "断言提交了什么文本 / 哪些 tool_calls"的用例一条都不用改，而真实的多块与中途收手
        由 `tests/unit/test_turn_stop.py` 专门钉。
        """
        yield self._next_reply(input)

    @property
    def last_visible_tools(self) -> list[str] | None:
        return self.calls[-1]["tools"] if self.calls else None


# --------------------------------------------------------------------------- fake tools


@tool
def query_health_record(start_date: str = "", end_date: str = "") -> str:
    """Query stored health record indicators within an optional date range."""
    return "结石直径 6.0 mm（参考范围 0-5）【未经人工校验】"


@tool
def compare_health_index(index_name: str) -> str:
    """Compare one indicator across years to show how it changed."""
    return "结石直径：2025 年 5.0 mm → 2026 年 6.0 mm【未经人工校验】"


@tool
def list_roles() -> str:
    """List the roles available in this deployment."""
    return "medical_archivist"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def conn(tmp_path: Any) -> Iterator[sqlite3.Connection]:
    """A bootstrapped in-temp-dir database, including one tenant, user and thread."""
    connection = connect(tmp_path / "app.db")
    bootstrap(connection, enabled_domains=("health",))
    connection.executescript(
        """
        INSERT INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');
        INSERT INTO app_user (user_id, tenant_id, display_name) VALUES ('u1', 't1', 'demo user');
        INSERT INTO session_thread (thread_id, user_id, current_role_id)
            VALUES ('thread-1', 'u1', 'medical_archivist');
        """
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def roles(conn: sqlite3.Connection) -> RoleCardService:
    service = RoleCardService(conn)
    service.seed_builtins()
    service.seed_domain_roles()  # medical_archivist 是域种子角色（自定义类型）
    return service


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(list_roles)  # kernel tool: domain=None
    reg.register_many([query_health_record, compare_health_index], domain="health")
    return reg


@pytest.fixture
def all_tools() -> list[BaseTool]:
    return [list_roles, query_health_record, compare_health_index]


def model_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    """把 `GET /api/settings/models` 的分组视图摊平成"行"，给按行断言的用例用。

    放在 conftest 是因为契约只有一个视图（`providers`）：以前响应里还并排放着一份平铺
    `backends`，界面切完就删了 —— 测试不该为了少敲几行键而把那个双形状契约续命。
    摊平时把组上的凭据（provider/base_url/has_key/key_masked）挂到行上，正是"key 属于组"
    这件事在数据结构上的样子。
    """
    rows: list[dict[str, object]] = []
    for group in payload.get("providers", []):  # type: ignore[union-attr]
        for model in group["models"]:
            rows.append(
                {
                    **model,
                    "provider": group["provider"],
                    "provider_id": group["id"],
                    "base_url": group["base_url"],
                    "has_key": group["has_key"],
                    "key_masked": group["key_masked"],
                }
            )
    return rows
