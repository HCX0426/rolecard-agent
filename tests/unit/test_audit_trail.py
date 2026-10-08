"""审计咽喉与它照出来的两件事（2026-10-02 轮 `R102-07` / `R102-14` / `R102-09`）。

七条判据，各自挡一种坏法：

  1. **审计不再是角色卡的一部分** —— `RoleCardService` 上不许再有 `audit`，端点侧写审计
     走 `ctx.audit`（从前 55 处全借角色卡服务，而其中没有一件与角色卡有关）；
  2. **留痕真的落库**，且 `detail` 里塞进不可序列化的值时**不抛**（五份 INSERT 里只有
     `mcp` 那份额外用 `default=str`，归一之后这条对全部写入点成立）；
  3. **动作词表与调用点双向对上**（`R102-14`）—— 台账问的是"能否被一条命令否证"，
     这条就是那条命令：`pytest tests/unit/test_audit_trail.py -k vocabulary`；
  4. **状态视图不再每次调用现建服务**（`R102-09`）—— 同一条读链上并行存在两份
     `ServiceEndpointService` 时，新加的状态只会出现在其中一份上。
"""

from __future__ import annotations

import ast
import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rolecard_agent.api.deps import AppContext
from rolecard_agent.base.audit import (
    AUDIT_ACTIONS,
    DYNAMIC_ACTION_PREFIXES,
    AuditTrail,
    tool_audit,
)
from rolecard_agent.base.identity import DEFAULT_TENANT_ID, DEFAULT_USER_ID
from rolecard_agent.roles.models import RoleCardCreate
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import bootstrap, connect

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "rolecard_agent"


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT actor, action, target, detail_json FROM audit_log"))


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "app.db")
    bootstrap(c, enabled_domains=("health",))
    try:
        yield c
    finally:
        c.close()


# ------------------------------------------------------- R102-07：咽喉归一


def test_role_service_no_longer_owns_the_audit_throat(conn) -> None:
    """`ctx.roles.audit(...)` 这个写法从此不存在（端点侧改名是这条的意义）。

    判据盯的是**名字**：把 `audit()` 方法放回 `RoleCardService` 上，这里必须红 ——
    否则过一阵又会有第 56 处 `ctx.roles.audit(`，而它问的从来不是角色卡。
    """
    assert not hasattr(RoleCardService, "audit")
    assert hasattr(AppContext, "audit")


def test_switch_role_still_leaves_exactly_one_row(conn) -> None:
    """搬咽喉不许少留痕：换一次角色 = 一行 `switch_role`，actor 照旧是调用方给的。"""
    store = RoleCardService(conn)
    store.scoped(DEFAULT_USER_ID).create(
        RoleCardCreate(role_id="girl", role_name="girl", system_prompt="x")
    )
    # 会话行有 FK 指向 app_user，而 app_user 由身份层播种（这里直接落两行，不绕装配）。
    conn.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) VALUES (?, ?, ?)",
        (DEFAULT_USER_ID, DEFAULT_TENANT_ID, "本地用户"),
    )
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id) VALUES (?, ?, ?)",
        ("s_1", DEFAULT_USER_ID, "girl"),
    )
    conn.commit()

    store.set_thread_role(
        thread_id="s_1", role_id="girl", actor="operator", user_id=DEFAULT_USER_ID
    )

    rows = _rows(conn)
    assert [r["action"] for r in rows] == ["switch_role"]
    assert rows[0]["actor"] == "operator"
    assert '"girl"' in (rows[0]["detail_json"] or "")


def test_trail_encodes_detail_it_could_not_serialize(conn) -> None:
    """归一之后 `default=str` 对全部写入点生效：留痕绝不该因为载荷里有个 datetime 就抛。"""
    AuditTrail(conn).log(
        actor="operator",
        action="create_session",
        target="s_1",
        detail={"at": datetime.datetime(2026, 10, 3, 1, 0, 0)},
    )
    assert "2026-10-03 01:00:00" in _rows(conn)[0]["detail_json"]


def test_tool_audit_without_conn_skips_instead_of_raising() -> None:
    """没接审计的宿主（测试、无库环境）：跳过留痕，不影响动作本身 —— 从前三份各自写着这条。"""
    tool_audit(None, "fs_read", "/tmp/x")


# --------------------------------------------------------- R102-14：词表是清单


def _actions_used_in_source() -> tuple[set[str], set[str]]:
    """从审计调用点收动作名：字面量归 `literal`，f-string 的前缀归 `dynamic`。

    与门禁 `audit action vocabulary` 同一条规则（含 `_audit(phase, detail)` 那一支的跳过
    条件），所以这里红了门禁也红、门禁红了这里也红 —— 判据不能只住在其中一个地方。
    """
    literal: set[str] = set()
    dynamic: set[str] = set()

    def take(node: ast.expr) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal.add(node.value)
        elif isinstance(node, ast.IfExp):
            take(node.body)
            take(node.orelse)
        elif isinstance(node, ast.JoinedStr) and node.values:
            head = node.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                dynamic.add(head.value)

    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = (
                callee.attr
                if isinstance(callee, ast.Attribute)
                else (callee.id if isinstance(callee, ast.Name) else "")
            )
            if name not in {"audit", "_audit", "log", "tool_audit"}:
                continue
            action_kw = next((k for k in node.keywords if k.arg == "action"), None)
            if action_kw is not None:
                take(action_kw.value)
            elif name in {"_audit", "tool_audit"} and len(node.args) >= 2:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    continue  # `_audit(phase, detail)`：第一个参数不是动作名
                take(node.args[1])
    return literal, dynamic


def test_action_registry_matches_call_sites() -> None:
    """双向差分：用了没登记的词即红，登记了却没人用同样红（`R102-37`：空转臂不算判据）。"""
    literal, dynamic = _actions_used_in_source()
    assert literal - set(AUDIT_ACTIONS) == set()
    assert set(AUDIT_ACTIONS) - literal == set()
    assert dynamic - set(DYNAMIC_ACTION_PREFIXES) == set()
    assert literal, "一个动作名都没收到 = 这条判据在空转"


def test_registry_names_are_unique_words() -> None:
    """词表是一组动作名，不是一句话：不许有空串或带空格的条目。"""
    assert all(name and " " not in name for name in AUDIT_ACTIONS)


# ---------------------------------------------------- R102-09：视图用注入的服务


def test_status_view_builds_no_second_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`service_status_view` 读的是注入那一份，不再自己 `ServiceEndpointService(...)`。

    变异 = 把函数体改回"用 conn 现建一个"，这里当场红（计数不是 0）。
    """
    from rolecard_agent.config import Settings
    from rolecard_agent.core.model_settings import ModelSettingsService
    from rolecard_agent.core.models.services import ServiceEndpointService, service_status_view

    c = connect(tmp_path / "view.db")
    bootstrap(c, enabled_domains=("health",))
    svc = ServiceEndpointService(c, owner=DEFAULT_USER_ID)
    ms = ModelSettingsService(c)

    built: list[Any] = []
    real_init = ServiceEndpointService.__init__

    def counting(self: ServiceEndpointService, *args: Any, **kwargs: Any) -> None:
        built.append(args)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(ServiceEndpointService, "__init__", counting)
    # 探活会真等网络超时（不可达的 Ollama 每次 3s）：本条判据看的是"谁建了服务"，
    # 不是"端点在不在跑"，所以替身掉。
    monkeypatch.setattr(
        "rolecard_agent.core.models.services.endpoint_available", lambda _e, _s: (True, "")
    )

    view = service_status_view(svc, ms, Settings(), user_id=None)

    assert built == [], f"状态视图每次调用现建了 {len(built)} 个服务端点实例"
    assert view["services"], "视图本身还得读得出那些类别（不能为了不留痕把读法改坏）"
    c.close()
