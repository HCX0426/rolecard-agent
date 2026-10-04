"""`features/sync_service.py` 的形状（2026-10-04 service 收口第一步的回归钉子）。

这批用例存在的理由不是"覆盖率"，而是**这条链的两条判据必须被钉住**，否则下一次有人
"顺手简化"就会把它们拆掉，而拆掉的后果只在真机上以"数据没了"的形式出现：

  1. 先落盘、再删（备份里必须找得回将被删的行，含 blob 的无损还原）；
  2. 清空与导入共一个事务（导入有失败 ⇒ 清掉的行全部还原，并且那句"全部还原"
     对 card 类必须如实降级）。

另外钉一件事：**这条链可以被非 HTTP 宿主复用** —— 建一个连接直接调服务，不起 app、
不造 TestClient。这正是它从前住在 `api/routers/sync.py` 时做不到的事，也是收口的目的
（桌宠壳与 `scripts/` 的取证脚本是同一条诉求）。
"""

from __future__ import annotations

import ast
import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rolecard_agent.base.identity import DEFAULT_TENANT_ID, DEFAULT_USER_ID
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.features import sync_service
from rolecard_agent.storage.db import bootstrap, connect

KINDS = ("card", "memory", "reachout", "thread")


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "app.db")
    bootstrap(c, enabled_domains=("health",))
    # 检查点两张表由 `SqliteSaver.setup()` 建（不在 schema.sql 里），而备份要读得到它们 ——
    # 用真路径建，别在测试里手搓一份表结构（那就是第二个事实面，列名一改测试先骗自己）。
    make_checkpointer(c)
    c.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    c.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) VALUES (?, ?, ?)",
        (DEFAULT_USER_ID, DEFAULT_TENANT_ID, "本地用户"),
    )
    c.commit()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据根指到 tmp：备份落在 `<数据根>/retention-backups/sync/`，不许写进仓库 data/。"""
    root = tmp_path / "data-root"
    monkeypatch.setenv("DATA_ROOT", str(root))
    return root


def _seed_memory(conn: sqlite3.Connection, uid: str, text: str) -> None:
    conn.execute(
        "INSERT INTO role_memory_item (role_id, user_id, uid, text) VALUES ('', ?, ?, ?)",
        (DEFAULT_USER_ID, uid, text),
    )
    conn.commit()


def _count(conn: sqlite3.Connection, table: str, **where: str) -> int:
    col, val = next(iter(where.items()))
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (val,)).fetchone()[0])  # noqa: E501


# ---------------------------------------------------------------- 备份那一半


def test_rows_are_dumped_before_they_are_cleared(
    conn: sqlite3.Connection, data_root: Path
) -> None:
    """删前备份里有那一行原文；顺序是判据：**备份写完才允许删**。"""
    _seed_memory(conn, "uid-doomed", "这句要被整份替换清掉")

    dumped = sync_service.dump_before_clear(conn, user_id=DEFAULT_USER_ID, kinds=["memory"])
    assert dumped["role_memory_item"] == 1

    files = list((data_root / "retention-backups" / "sync").glob("role_memory_item-*.jsonl"))
    assert files, "备份文件没落盘 —— '有备份'这件事必须有痕迹，静默写一份等于没有"
    assert "这句要被整份替换清掉" in files[0].read_text(encoding="utf-8")


def test_checkpoint_blob_survives_the_backup_roundtrip(
    conn: sqlite3.Connection, data_root: Path
) -> None:
    """检查点 blob 是 msgpack 字节：备份必须**无损**（base64），不许 `str()` 成一串 repr。

    这一条钉的是合并两份 JSONL 落盘实现时选的那一版语义：从前 retention 那份用
    `default=str`，对没有 bytes 列的三张表没问题，而**需要无损的恰好是检查点这一族**。
    写成 repr 的备份在重放时还原不出原来那 40 个字节，等于白备份。
    """
    payload = bytes(range(256))  # 含 NUL 与全部不可打印字节：`str()` 一定走样
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch) "
        "VALUES ('s_blob', ?, 'girl', 1)",
        (DEFAULT_USER_ID,),
    )
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint,"
        " metadata) VALUES ('s_blob', '', 'ck1', 'msgpack', ?, x'00')",
        (payload,),
    )
    conn.commit()

    dumped = sync_service.dump_before_clear(conn, user_id=DEFAULT_USER_ID, kinds=["thread"])
    assert dumped["checkpoints"] == 1

    files = list((data_root / "retention-backups" / "sync").glob("checkpoints-*.jsonl"))
    record = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    cell = record["checkpoint"]  # langgraph 那列就叫 checkpoint（msgpack 字节）
    assert isinstance(cell, dict) and "__base64__" in cell, f"blob 没走无损编码：{cell!r}"
    assert base64.b64decode(cell["__base64__"]) == payload, "还原出来的字节必须逐字等于原值"


def test_clearing_is_a_single_transaction_with_the_import(
    conn: sqlite3.Connection, data_root: Path
) -> None:
    """清空不自己 commit：调用方（`run_import`）能连导入一起回滚 —— "清了不导"的唯一解。"""
    _seed_memory(conn, "uid-1", "一条")
    cleared = sync_service.clear_rows_for_replace(
        conn, user_id=DEFAULT_USER_ID, kinds=["memory"]
    )
    assert cleared == {"memory": 1}
    assert conn.in_transaction, "行类清空必须还挂在事务里，否则回滚收不回来"
    conn.rollback()
    assert _count(conn, "role_memory_item", uid="uid-1") == 1, "回滚应当把清掉的行还原"


def test_thread_backup_covers_both_checkpoint_tables(
    conn: sqlite3.Connection, data_root: Path
) -> None:
    """会话正文住在 `checkpoints` / `writes` 两张表里：只备份载体行 = 还原不出那条评论。"""
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch) "
        "VALUES ('s_two', ?, 'girl', 1)",
        (DEFAULT_USER_ID,),
    )
    conn.execute(
        "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel,"
        " type, value) VALUES ('s_two', '', 'ck1', 't1', 0, 'messages', 'msgpack', X'0102')"
    )
    conn.commit()

    dumped = sync_service.dump_before_clear(conn, user_id=DEFAULT_USER_ID, kinds=["thread"])
    assert dumped["session_thread"] == 1
    assert dumped["writes"] == 1, "writes 表漏备份 ⇒ 那条会话即使重放也少一半内容"
    assert dumped["checkpoints"] == 0, "空结果不该落文件（0 行 = 连目录都不碰）"


def test_backup_files_are_trimmed_per_table(
    conn: sqlite3.Connection, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每表各留 N 份：裁的是**本表**的份数，不能让一个表的备份把另一个表的饿死。"""
    monkeypatch.setattr(sync_service, "SYNC_BACKUP_KEEP", 2)
    backup_dir = data_root / "retention-backups" / "sync"
    backup_dir.mkdir(parents=True)
    for n in range(3):
        (backup_dir / f"role_memory_item-2026010{n}-000000.jsonl").write_text(
            "{}\n", encoding="utf-8"
        )
    (backup_dir / "role_card-20260101-000000.jsonl").write_text("{}\n", encoding="utf-8")

    _seed_memory(conn, "uid-x", "删我")
    sync_service.dump_before_clear(conn, user_id=DEFAULT_USER_ID, kinds=["memory"])

    assert (backup_dir / "role_card-20260101-000000.jsonl").exists(), (
        "另一张表的旧备份被误删：裁必须按表分组"
    )
    survivors = sorted(f.name for f in backup_dir.glob("role_memory_item-*.jsonl"))
    # 三份旧的 + 本轮这一份 = 四份，裁到 keep=2。本轮那份**必须**活着：从前出过一次
    # "备份写完了、紧接着被同一趟裁掉，剩下两份早到没法用"的事故，所以这条断言的
    # 重点不是"等于 2"，是"最新的这一份在幸存者里"。
    assert len(survivors) == 2, survivors
    assert survivors[-1] > "role_memory_item-20260102-000000.jsonl", (
        f"裁完留下的不是最新的两份：{survivors}"
    )
    assert "role_memory_item-20260102-000000.jsonl" in survivors, survivors


def test_clear_threads_sums_both_delete_paths(
    conn: sqlite3.Connection, data_root: Path
) -> None:
    """真删逐条 + 收尾那条**相加**才是条数；只取收尾那个数会恒报 0（`R102-05` 的读数错）。"""
    for tid in ("s_x", "s_y", "s_z"):
        conn.execute(
            "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch)"
            " VALUES (?, ?, 'girl', 1)",
            (tid, DEFAULT_USER_ID),
        )
    conn.commit()

    class _FakeGraph:
        """级联删那条路要求拿得到检查点；这里只验**计数口径**，不跑真图。"""

    cleared = sync_service.clear_threads_for_replace(
        conn, user_id=DEFAULT_USER_ID, graph=_FakeGraph()
    )
    assert cleared == {"thread": 3}
    assert _count(conn, "session_thread", thread_id="s_x") == 0


def test_service_runs_without_http_for_non_http_hosts(
    conn: sqlite3.Connection, data_root: Path
) -> None:
    """收口的目的：不起 app、不造 TestClient 也能跑这条链（桌宠壳与脚本复用同一段代码）。"""
    assert "api" not in _imported_packages(sync_service), (
        "service 反向依赖 HTTP 层，非 HTTP 宿主就 import 不动它"
    )
    _seed_memory(conn, "uid-quiet", "脚本里也能清")
    cleared = sync_service.clear_rows_for_replace(
        conn, user_id=DEFAULT_USER_ID, kinds=["memory"]
    )
    assert cleared == {"memory": 1}


def _imported_packages(module: Any) -> set[str]:
    """这个模块 import 到的 `rolecard_agent.<pkg>` 里的那些 `<pkg>`。"""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
        for name in names:
            if name.startswith("rolecard_agent."):
                found.add(name[len("rolecard_agent.") :].split(".")[0])
    return found
