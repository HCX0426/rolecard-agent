"""故障注入（快照“零故障注入”那一格的前半）：磁盘满那种"环境故障"要**可读**，且不许留下半笔。

判据锚在快照那句验收上："ENOSPC 注入断言 502/可读错误且无数据半写；坏库启动有明确诊断"。
三件事分开量、都不看时序：
  * 注入层本身：抛一次真形状的 `OperationalError`，用完自动复位（不许继续毒后面的用例）；
  * **不留半笔**：commit 被注入打断之后，换一条新连接回读，那笔必须不在；
  * **可读**：环境型故障回 503 + 一句人话，而"编程错"（`no such table`）**照旧 500** ——
    把真 bug 也报成"临时故障、重试就好"，是本仓最恨的那种格子。
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from rolecard_agent.api.errors import storage_trouble_status
from rolecard_agent.storage import db


def _fresh_db(tmp_path: pathlib.Path) -> db.ThreadLocalConnection:
    conn = db.connect_threadlocal(tmp_path / "app.db")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    return conn


def test_注入一次只抛一次(tmp_path: pathlib.Path) -> None:
    """用完即复位：会一直生效的注入，第二个用例撞上它时红得莫名其妙。"""
    db.clear_faults()
    conn = _fresh_db(tmp_path)
    db.inject_fault("commit")
    with pytest.raises(sqlite3.OperationalError):
        conn.commit()
    conn.commit()  # 第二次不该再抛
    assert db._INJECTED_FAULTS == {}  # noqa: SLF001 - 判据本身就是"账清了"
    conn.close()


def test_注入的错是真形状(tmp_path: pathlib.Path) -> None:
    """消息文本与 ENOSPC 实测同形 —— 注入的错要能被**同一套判据**认出来。"""
    db.clear_faults()
    conn = _fresh_db(tmp_path)
    db.inject_fault("execute")
    with pytest.raises(sqlite3.OperationalError) as got:
        conn.execute("SELECT 1")
    assert storage_trouble_status(got.value) == 503
    conn.close()


def test_commit_被打断时那笔不会留下来(tmp_path: pathlib.Path) -> None:
    """**"无数据半写"就是这一条**：注入打断 commit，换一条新连接回读，行必须不在。

    为什么换新连接：同一线程那条连接上事务还挂着（回滚是调用方的事），
    用它的读数证明不了"落盘没有"—— 那正是"半写"最难看的形态：本线程看得见、别人看不见。
    """
    db.clear_faults()
    conn = _fresh_db(tmp_path)
    db.inject_fault("commit")
    conn.execute("INSERT INTO t (v) VALUES ('x')")
    with pytest.raises(sqlite3.OperationalError):
        conn.commit()
    conn.rollback()  # 调用方该做的收尾（写事务不能悬着）
    other = db.connect(tmp_path / "app.db")
    try:
        assert other.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        other.close()
    conn.close()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("database or disk is full", 503),
        ("database is locked", 503),
        ("disk I/O error", 503),
        ("unable to open database file", 503),
        ("no such table: t", None),  # 编程错：不许伪装成"重试就好"
        ("near \"SELEC\": syntax error", None),
    ],
)
def test_只有环境型故障才算故障(message: str, expected: int | None) -> None:
    assert storage_trouble_status(sqlite3.OperationalError(message)) == expected


def test_真_app_上挂着的那个_handler_把环境故障翻成可读的_503(tmp_path: pathlib.Path) -> None:
    """真挂在 app 上量一遍：handler 注册错地方 / 被别的 handler 抢先，这里都看得出来。

    不新加路由（`create_app` 最后把静态站 `mount("/")` 挂在末尾，之后再加的路由会被它挡住
    —— 第一版就是这么吃到 404 的），而是直接取 app 上注册的那个 handler 调一次：
    要判的正是"这条异常在真 app 上由谁处理、回什么"。
    """
    import asyncio

    from rolecard_agent.api.main import create_app

    app = create_app(sqlite_path=tmp_path / "app.db")
    handler = app.exception_handlers[sqlite3.OperationalError]  # 没挂上这里就 KeyError

    storage = asyncio.run(handler(None, sqlite3.OperationalError("database or disk is full")))
    assert storage.status_code == 503
    detail = storage.body.decode("utf-8")
    assert "磁盘" in detail and "disk is full" in detail, detail

    bug = asyncio.run(handler(None, sqlite3.OperationalError("no such table: nope")))
    assert bug.status_code == 500, "编程错不许伪装成'临时故障、重试就好'"
    assert "不该出现" in bug.body.decode("utf-8")


def test_坏库在开连接那一步就给出人话(tmp_path: pathlib.Path) -> None:
    """快照后半句"坏库启动有明确诊断"：从前抛的是 `sqlite3.DatabaseError: file is not a
    database` —— 没说哪个文件、没说能做什么，而**启动期是唯一还来得及说人话的时刻**。"""
    db.clear_faults()
    broken = tmp_path / "app.db"
    broken.write_bytes("这不是一个 sqlite 库".encode() * 200)
    with pytest.raises(db.StorageUnreadable) as got:
        db.connect(broken)
    message = str(got.value)
    assert str(broken) in message, "要点名是哪个文件"
    assert "不是 SQLite 库" in message
    assert "备份" in message or "留证" in message, "要给一句能照着做的"
    # 原始原因也要留着（认不出来的形态靠它排查），不许被我们自己那句话盖掉
    assert "not a database" in message.lower()


def test_坏库的三种形态各说各的话(tmp_path: pathlib.Path) -> None:
    """三条分支不许合并成一句：损坏 / 打不开 / 被占着，用户要做的事完全不同。"""
    target = tmp_path / "app.db"
    corrupt = db._connect_hint(target, sqlite3.DatabaseError("file is not a database"))  # noqa: SLF001
    denied = db._connect_hint(  # noqa: SLF001
        target, sqlite3.OperationalError("unable to open database file")
    )
    locked = db._connect_hint(target, sqlite3.OperationalError("database is locked"))  # noqa: SLF001
    assert "不是 SQLite 库" in corrupt
    assert "权限" in denied
    assert "第二个实例" in locked
    assert len({corrupt, denied, locked}) == 3


def test_坏库让应用起不来且报的是那句话(tmp_path: pathlib.Path) -> None:
    """端到端：坏库时 `create_app` 抛的是**我们的**诊断，不是裸的 sqlite 异常。

    这条才是"启动期"三个字的意思 —— 库坏了不该表现为"跑起来之后某个请求 500"。
    """
    from rolecard_agent.api.main import create_app

    broken = tmp_path / "app.db"
    broken.write_bytes(b"garbage" * 500)
    with pytest.raises(db.StorageUnreadable) as got:
        create_app(sqlite_path=broken)
    assert "不是 SQLite 库" in str(got.value)


def test_运行期的坏库也算环境故障() -> None:
    """启动期有专门的翻法，运行期走错误族那一条 —— 两处都别漏。"""
    assert storage_trouble_status(sqlite3.DatabaseError("file is not a database")) == 503
