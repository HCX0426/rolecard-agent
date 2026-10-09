"""Shared fixtures.

Everything here is offline: no Ollama, no network, no model downloads. If a test needs a
model it gets `ScriptedChat`, which replays a fixed list of replies. That constraint is what
lets `pytest` run in CI on any machine.

`with TestClient(app)` 会跑 lifespan，而 lifespan 的启动预热是**真** HTTP 调用（把默认本地
模型按 num_ctx 钉进显存）—— 它不在任何断言里，却把 8B 装进 GPU 与测试抢资源。会话级
autouse fixture 把它关掉，是这条离线铁律的最后一块（架构审计报告 §6 隐蔽外呼）。
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool, tool

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.domains.registry import domain_seed_roles
from rolecard_agent.roles.service import RoleCards, RoleCardService
from rolecard_agent.storage.db import bootstrap, connect

# --------------------------------------------------------------------------- offline guard


def cards(store: RoleCardService) -> RoleCards:
    """测试里"本机主人眼里的那些卡"的简写（M2a 之后每次读写都得说清为谁）。"""
    return store.scoped(...)

@pytest.fixture(scope="session", autouse=True)
def _no_startup_model_pin() -> Iterator[None]:
    """整个测试会话都不做启动预热（`MODEL_PIN_ON_STARTUP=0`，见 config.py 该字段的理由）。

    同时关掉**自动提取记忆**（`MEMORY_EXTRACT_AUTO=0`）：那是一次后台真模型调用，它会把
    ScriptedChat 的脚本回复吃掉一条 —— 断言"这一轮回复是 build-N"的用例就会偶发错位。
    提取自身的用例（test_memory_distill）单独把它打开再测，不在这里偷开。

    同理关掉**来源标识护栏**（`LOCAL_ORIGIN_ENFORCE=0`，`R102-45`）：TestClient 的 Host 是
    `testserver`，不是回环名 —— 全部既有 API 用例会在护栏上集体 403。护栏自己的行为由
    `tests/unit/test_local_origin_guard.py` 单独开起来钉（含"开着时本机 Host 照常放行"）。
    """
    previous = {
        key: os.environ.get(key)
        for key in ("MODEL_PIN_ON_STARTUP", "MEMORY_EXTRACT_AUTO", "LOCAL_ORIGIN_ENFORCE")
    }
    os.environ["MODEL_PIN_ON_STARTUP"] = "0"
    os.environ["MEMORY_EXTRACT_AUTO"] = "0"
    os.environ["LOCAL_ORIGIN_ENFORCE"] = "0"
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def wait_upload():
    """等一次上传**走完后台**（P3-3 后台化：201 只是受理，终局要轮询进度端点）。

    为什么必须显式等：解析/建索引现在在池线程里跑，POST 返回 `status=processing`。
    不等就断言终局 = 撞运气（曾有三支 records 用例"碰巧"过了 —— 池线程比下一发
    请求快），那是按机器时序通过的测试，不是按判据通过的。

    `running=False` 是唯一的完成信号：`status=pending` 可能是"还没跑"也可能是
    OCR 未配置那条**终局** pending，只有与 running 并读才不含糊（进度端点的 docstring
    讲了这条口径）。超时给足 OCR 上限（120s）+ 余量 —— 真超时就是断言失败，不静默。
    """
    import time as _time

    def _wait(client, task_id: str, *, timeout: float = 150.0) -> dict[str, Any]:
        deadline = _time.monotonic() + timeout
        while True:
            body = client.get(f"/api/uploads/tasks/{task_id}").json()
            if not body["running"]:
                return body
            if _time.monotonic() > deadline:
                raise AssertionError(f"upload task never finished: {task_id} -> {body}")
            _time.sleep(0.05)

    return _wait


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
    # 出厂卡也有主人：测试里就是本机那份。`roles` 夹具仍返回**服务**（图与内核的桩吃它），
    # 读写用例自己 `roles.scoped(...)` 拿视图）。
    service.seed_builtins(user_id=DEFAULT_USER_ID)
    # medical_archivist 是域种子角色（自定义类型）—— 清单由各域 SPEC 聚合给出
    service.seed_domain_roles(domain_seed_roles(), user_id=DEFAULT_USER_ID)
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


def _chroma_registry() -> dict | None:
    """chroma 的进程级 system 注册表（拿不到 = 没装 chromadb，返回 None 让判据退化成不判）。"""
    try:
        from chromadb.api.client import SharedSystemClient
    except Exception:  # noqa: BLE001 - 本机没装 chromadb 的测试环境
        return None
    return getattr(SharedSystemClient, "_identifier_to_system", None)


@pytest.fixture(autouse=True)
def _no_chroma_system_leak():
    """用例里新开的 chroma system，结束时统一摘掉（`R102-74`）。

    chroma 本地客户端把 system 记在进程级注册表 `SharedSystemClient._identifier_to_system`
    里（键是 persist_directory），而实测 **`del` 客户端不会摘掉它**（注册表仍是 1），
    只有 `Client.close()` 会。所以每开一个 `KnowledgeBase` 而没人收尾，就往这张表里堆一格，
    sqlite 句柄与后台建索引的线程池都常驻。10-03 的探针（`build/probe_chroma_flake.py`）
    证明"同一进程里并存多格 system"正是 `R102-41` 那记 `Nothing found on disk` 的复现形状。

    放在 conftest 而不是每个用例里手写 `close()`：漏一个就是一格，而漏的那个**不会自己报告** ——
    这正是本仓那一族"注释传知识传两处就停"的形状。
    """
    registry = _chroma_registry()
    before = set(registry) if registry is not None else set()
    yield
    if registry is None:
        return
    for ident in [k for k in registry if k not in before]:
        system = registry.pop(ident, None)
        if system is not None:
            with contextlib.suppress(Exception):
                system.stop()
# --------------------------------------------------------------- R102-41 失败时刻的现场
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
BUILD_DIR = pathlib.Path(__file__).resolve().parents[1] / "build"
# 取证层住 forensics/（脚本分层后各归其层）；签名清单与门禁共用同一份。
_FORENSICS_DIR = _SCRIPTS_DIR / "forensics"
if str(_FORENSICS_DIR) not in sys.path:
    sys.path.insert(0, str(_FORENSICS_DIR))

import chroma_flake_evidence as _flake  # noqa: E402  （取证层与门禁共用同一份签名清单）


@pytest.hookimpl(trylast=True)
def pytest_runtest_makereport(item: pytest.Item, call: Any) -> None:
    """命中在册的 chroma 偶发时，把"元数据说在、盘上没了"那一份现场钉进 `build/`。

    为什么挂在 makereport 而不是用例里：这发偶发**不可请求**（约 1.3-2%/轮），而它一红，
    `tmp_path` 在会话收尾时就被回收了 —— 于是每次红完只剩同一句 `Nothing found on disk`，
    十五批取证批批从头。台账要的正是"一次带 chroma 侧状态的现场"（`R102-41`），这一步给它。

    **2026-10-09 改掉的两处（原来是两处叠着的缺陷，实测都在这发偶发身上）**：

    ① 从前有一句 `if call.when != "call": return`，理由是"setup 里的错通常是夹具自己的问题"。
       这个闸门是**多余的、而且是错的**：多余因为签名判据本来就会把"不是那一发"的挡掉；
       错因为 run 37860489135 的 Windows 臂那次 chroma `(code: 5) database is locked`
       **正是发生在 setup**（`ERROR at setup of test_…`，fixture 里建 client 就炸了）——
       守卫在它最该起作用的那一形状上失明，"红跑不许没有现场"当场落空。现在**只由签名决定**。

    ② 从前喂判据的是 `str(exc)`，而 `CHROMA_FLAKE_SIGNATURES` 里有一条是**类名**
       （`chromadb.errors.InternalError`）—— `str()` 永远不含自己的类名 ⇒ 那一条签名
       在这个调用点上**物理不可能命中**（实测：同一发偶发，门禁 wrapper 读的日志含类名行 ⇒
       认；钩子读 `str(exc)` ⇒ 不认）。这就是账本 ENGI-24① 记的"两台机器各读半张"的**真机制**，
       而它的解不是"两处各写一份判据"（判据早已共用），是让两处**读到语义相同的文本**：
       这里补上限定类名，与 pytest 打报告时的那一行同形。
    """
    if call.excinfo is None:
        return
    exc = call.excinfo.value
    try:
        kind = f"{type(exc).__module__}.{type(exc).__qualname__}"
        text = f"{kind}: {exc}"
    except Exception:  # noqa: BLE001 - 取证层不能因为异常没法 str 就崩掉整趟
        return
    if not _flake.hits_signature(text):
        return
    roots: list[pathlib.Path] = []
    tmp = getattr(item, "funcargs", {}).get("tmp_path")
    if isinstance(tmp, pathlib.Path):
        roots.append(tmp)
    path = _flake.dump_evidence(BUILD_DIR, item.nodeid, text, extra_roots=roots)
    phase = "" if call.when == "call" else f"（{call.when} 阶段）"
    print(f"[R102-41 现场] 命中在册 chroma 偶发{phase} ⇒ 盘上形状落到 {path}")
