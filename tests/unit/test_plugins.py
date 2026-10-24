"""域清单与工具代数（`core/plugins`）的单元测试 —— 2026-10-24 权限模型收口后重写。

从前的"开关"那一族（set_enabled / noop / 审计）随写路径一起退役：**域常开**，
`plugin` 表退化为域清单，`tool_epoch` 的唯一合法写入来源是热重建
（`assembler.rebuild` 在换装临界区内 `bump_tool_epoch`）。

现在钉住的三件事：
* `enabled_domains()` 返回**全部已注册域**（无 WHERE）—— 白名单即唯一边界；
* `bump_tool_epoch()` 在同一连接上推进代数（rebuild 的 C14 漂移检测靠它）；
* `register` 保持幂等（重跑 bootstrap 不重复建行）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rolecard_agent.core.plugins import PluginService
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def conn() -> sqlite3.Connection:
    # Core + roles schema only: seeds the `tool_epoch = 1` row the service reads.
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    return c


@pytest.fixture
def plugins(conn: sqlite3.Connection) -> PluginService:
    return PluginService(conn, known_plugins=["health", "finance"])


def test_tool_epoch_starts_at_one(conn: sqlite3.Connection) -> None:
    assert PluginService(conn).tool_epoch() == 1


def test_register_is_idempotent(plugins: PluginService) -> None:
    plugins.register("health", display_name="Health")
    plugins.register("health", display_name="Health")  # second call is a no-op
    assert len(plugins.list_plugins()) == 1


def test_enabled_domains_returns_every_registered_domain(plugins: PluginService) -> None:
    """域常开：清单里每一行都在（没有 enabled 过滤——白名单即唯一边界）。"""
    plugins.register("health", display_name="Health")
    plugins.register("finance", display_name="Finance")
    assert plugins.enabled_domains() == ["finance", "health"]


def test_enabled_domains_reads_new_rows_live(plugins: PluginService) -> None:
    """每次调用现读：register 一个新域，下一次调用即可见（不快照）。"""
    assert plugins.enabled_domains() == []
    plugins.register("health", display_name="Health")
    assert plugins.enabled_domains() == ["health"]


def test_bump_tool_epoch_advances_on_same_connection(plugins: PluginService) -> None:
    """热重建的写入点：bump 推进代数。提交归调用方（rebuild 现场提交）。"""
    before = plugins.tool_epoch()
    assert plugins.bump_tool_epoch() == before + 1
    assert plugins.tool_epoch() == before + 1


def test_bump_persists_after_reconnect(tmp_path: Path) -> None:
    """代数落在 kernel_meta 里：换一个连接读，值还在（重启后漂移检测仍可判）。"""
    p = tmp_path / "app.db"
    c = connect(str(p))
    bootstrap(c, enabled_domains=())
    svc = PluginService(c)
    svc.bump_tool_epoch()
    svc._conn.commit()  # noqa: SLF001 - bump 的提交归调用方，这里模拟 rebuild 的提交
    c.close()
    assert PluginService(connect(str(p))).tool_epoch() == 2


def test_register_rejects_unknown_domain(plugins: PluginService) -> None:
    """known_plugins 之外的域名不许进清单（清单与代码侧注册表必须一致）。"""
    from rolecard_agent.core.plugins import UnknownPlugin

    with pytest.raises(UnknownPlugin):
        plugins.register("bogus", display_name="Bogus")
