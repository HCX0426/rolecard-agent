"""一次性域包清理步（`shape.rip_domain_packages`）的回归钉。

为什么单独立一个文件：这一步的判据是 `kernel_meta` 上的一面旗，而**两面都要钉**——

  1. 幽灵必须清掉：0.6.2 装过的机器升上来时，`plugin` 里躺着 health/finance 两行
     （上一版开机播种留下的），而 0.6.3 已经没有那两个域包 —— 界面上挂着页签、
     背后一行代码都没有，正是本仓最忌讳的两套真相。实测本机就是这个状态。
  2. 用户建的不许回收：「＋ 新建域」是 2026-11-07 才有的功能，清理跑过之后用户
     完全可以再建一个叫 health 的域。若这一步每次开机都跑，他的域会在下次开机
     被代码悄悄删掉 —— 那是反方向的两套真相，比幽灵更糟（丢的是用户数据）。

判据用形状（"私有表还在不在"）不够：本机实测出现过"表已被上一版删掉、plugin 行
还留着"的半清理状态，形状判据对它永远为假，幽灵就再也清不掉。所以这里钉的是旗。
"""

from __future__ import annotations

from pathlib import Path

from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.storage.migrations import DOMAIN_RIP_FLAG, MIGRATION_PLAN
from rolecard_agent.domains.registry import DOMAINS
from rolecard_agent.storage.db import bootstrap, connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_schemas"


def _build_with_ghosts(path: Path) -> object:
    """按 14cb9db 那份老形状建库，再手工造出"0.6.2 装过"的现场。

    老 fixture 只有 schema 没有行，所以幽灵行必须自己插 —— 真实机器上它们是上一版
    `seed_plugin_rows` 播出来的。domain_data 也照真实情况插两行（health 一条、
    用户自己的 notes 一条），用来验"只删退役域的记录、不碰别的域"。
    """
    conn = connect(path)
    for part in ("core", "roles", "domains_health"):
        conn.executescript((FIXTURES / f"14cb9db_{part}.sql").read_text(encoding="utf-8"))
    conn.executescript(
        "INSERT INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT INTO app_user (user_id, tenant_id, display_name) VALUES ('u1', 't1', 'u1');"
    )
    # 上一版播种留下的两个域（当时还没有 retired 列，形状按当时写）
    conn.executescript(
        "INSERT INTO plugin (plugin_id, display_name, enabled, sort_order) "
        "  VALUES ('health', 'health', 1, 0);"
        "INSERT INTO plugin (plugin_id, display_name, enabled, sort_order) "
        "  VALUES ('finance', 'finance', 1, 0);"
    )
    # domain_data 那张表在 14cb9db 还没有，bootstrap 的 DDL 会补上 —— 所以这里的插入
    # 放到升级之后做（见各用例），此处只留 plugin 与私有表这两样"上一版真的有的东西"。
    conn.commit()
    return conn


def _plugin_ids(conn: object) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT plugin_id FROM plugin")}  # type: ignore[attr-defined]


def _flagged(conn: object) -> bool:
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT 1 FROM kernel_meta WHERE key = ?", (DOMAIN_RIP_FLAG,)
    ).fetchone()
    return row is not None


def test_幽灵域被一次性清掉(tmp_path: Path) -> None:
    """0.6.2 升上来的现场：health/finance 两行必须消失，旗要立起来。"""
    conn = _build_with_ghosts(tmp_path / "app.db")
    assert _plugin_ids(conn) == {"health", "finance"}, "前提：老库带着那两行"

    bootstrap(conn, enabled_domains=DOMAINS, plan=MIGRATION_PLAN)  # type: ignore[arg-type]

    assert _plugin_ids(conn) == set(), f"幽灵页签还在：{_plugin_ids(conn)}"
    assert _flagged(conn), "清理跑过就必须立旗，否则下次开机还会跑"
    # 私有表也一起没了（上一版那一步只删了表、没删行，就是现在的 bug 形状）
    tables = {
        str(r[0])
        for r in conn.execute(  # type: ignore[attr-defined]
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "medical_report" not in tables and "medical_index" not in tables
    conn.close()  # type: ignore[attr-defined]


def test_清理跑过之后用户自建的同名域不再被回收(tmp_path: Path) -> None:
    """反方向那半边：旗立起来之后，用户自己建的 `health` 域必须活过下一次开机。

    这条是"一次性"存在的理由。把 `check` 摘掉（或让判据恒真）——本用例红，
    而上一那条照旧绿：两面各钉一个方向，缺哪面都会漏。
    """
    conn = _build_with_ghosts(tmp_path / "app.db")
    bootstrap(conn, enabled_domains=DOMAINS, plan=MIGRATION_PLAN)  # type: ignore[arg-type]
    assert _plugin_ids(conn) == set()

    # 用户现在自己在资料页建一个叫 health 的域（`POST /api/settings/domains` 走的就是这个）
    PluginService(conn).create("health", display_name="体检")
    assert "health" in _plugin_ids(conn), "前提：域建出来了"

    # 下一次开机
    bootstrap(conn, enabled_domains=DOMAINS, plan=MIGRATION_PLAN)  # type: ignore[arg-type]
    assert "health" in _plugin_ids(conn), "用户自己建的域被历史清理步回收了 —— 清理必须是一次性的"
    conn.close()  # type: ignore[attr-defined]


def test_清理不碰别的域的记录(tmp_path: Path) -> None:
    """③ 那半边：只删退役域名下的 domain_data，别的域一条都不许动。"""
    conn = _build_with_ghosts(tmp_path / "app.db")
    bootstrap(conn, enabled_domains=DOMAINS, plan=MIGRATION_PLAN)  # type: ignore[arg-type]
    # 升级后再建记录（domain_data 在 14cb9db 那份形状里还不存在）
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO domain_data (id, domain, user_id, label) "
        "VALUES ('d1','health','u1','旧体检记录')"
    )
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO domain_data (id, domain, user_id, label) VALUES ('d2','notes','u1','我的笔记')"
    )
    conn.commit()  # type: ignore[attr-defined]

    # 手动再跑一次清理函数本体（绕过旗），验它的删除范围
    from rolecard_agent.core.storage.migrations import _rip_domain_packages

    _rip_domain_packages(conn)  # type: ignore[arg-type]
    left = {str(r[0]) for r in conn.execute("SELECT domain FROM domain_data")}  # type: ignore[attr-defined]
    assert left == {"notes"}, f"清理越界了，别的域的记录被牵连：{left}"
    conn.close()  # type: ignore[attr-defined]
