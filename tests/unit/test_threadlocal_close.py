"""`ThreadLocalConnection.close()` 只许收自己那一格（`R102-02`，10-03 复核时抓出来的漏账）。

从前这段是**宣称与实现分叉**的标本：docstring 写"其它线程正在使用的连接不能在这里关"，
代码却把 `_created` 全量 close，外面罩着 `contextlib.suppress(sqlite3.Error)` —— 于是
另一线程那笔未提交的写入在主线程 close 之后"commit 不抛、回读为 None"，**数据静默消失**。

四条判据：

  1. 别的线程挂着未提交的写，主线程 `close()` 之后那笔写**还能落库**（这条就是从前会输的那一发）；
  2. `close()` 在**可疑形态**下必须出声（2026-10-04 审查快照起：本线程没有自己的槽却调
     close = 跨线程误用签名）；线程归还**自己的**槽是正常路径，安静 —— fire-and-forget
     线程用完即还不该每轮刷一行日志；
  3. 本线程那格若挂着事务，关之前先 rollback（`R102-42` 同一条纪律：不能把写锁留在已关闭的连接上）；
  4. 账是按线程身份记的，不是按"创建顺序"记的。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from rolecard_agent.storage.db import bootstrap, connect, connect_threadlocal


def _seed(tmp_path: Path) -> Path:
    """建库：用一个普通连接把 schema 落下去，暂存库的连接由被测对象自己开。"""
    path = tmp_path / "app.db"
    plain = connect(path)
    bootstrap(plain, enabled_domains=("health",))
    plain.close()
    return path


def test_other_threads_pending_write_survives_my_close(tmp_path: Path, capsys) -> None:
    path = _seed(tmp_path)
    tl = connect_threadlocal(path)
    tl.execute("SELECT 1")  # 主线程也持一格连接（从前这一步之后 close 会把别人那格一起带走）

    wrote = threading.Event()
    committed = threading.Event()
    errors: list[str] = []

    def worker() -> None:
        try:
            tl.execute(
                "INSERT INTO kernel_meta (key, value) VALUES ('b_other_thread', 'kept')"
            )
            wrote.set()
            # 等主线程把"关连接"这件事做完，再提交自己那一笔。
            committed.wait(5)
            tl.commit()
            tl.close()  # 由它自己的线程收（这正是修好之后的分工）
        except sqlite3.Error as exc:  # noqa: BLE001 - 失败形状本身是判据的一部分
            errors.append(f"{type(exc).__name__}: {exc}")
            wrote.set()

    t = threading.Thread(target=worker)
    t.start()
    assert wrote.wait(5), "工作线程没能走到写入这一步"
    tl.close()  # ← 从前的病发点：这里会把工作线程那一格一起 close 掉
    committed.set()
    t.join(5)

    probe = sqlite3.connect(path)
    probe.row_factory = sqlite3.Row  # 按名取列（app 连接自带这个 row_factory）
    probe.execute("PRAGMA busy_timeout = 3000")
    try:
        row = probe.execute(
            "SELECT value FROM kernel_meta WHERE key = 'b_other_thread'"
        ).fetchone()
    finally:
        probe.close()
    assert not errors, f"工作线程报错（从前是静默丢写，现在是明着失败）：{errors}"
    assert row is not None and str(row["value"]) == "kept", (
        "另一线程未提交的写入被主线程 close() 带走了（R102-02 的原始后果）"
    )
    err = capsys.readouterr().err
    assert "[conn-close]" not in err, (
        f"线程归还自己的槽是正常路径，不该刷告警：{err!r}"
    )


def test_close_ends_my_own_open_transaction_before_closing(tmp_path: Path) -> None:
    """本线程挂着 0 行的 UPDATE（照样开了写事务）→ close 之后不能把写锁留在原地。"""
    path = _seed(tmp_path)
    tl = connect_threadlocal(path)
    cur = tl.execute("UPDATE session_thread SET title = 'x' WHERE thread_id = '不存在'")
    assert cur.rowcount == 0
    inner = tl._created[threading.get_ident()]
    assert inner.in_transaction, "前置条件：0 行的 UPDATE 确实开了写事务"

    tl.close()

    # 换一个进程外连接去写：从前会被那把没结束的写事务堵到 busy_timeout
    other = connect(path)
    other.execute("PRAGMA busy_timeout = 1500")
    other.execute("INSERT INTO kernel_meta (key, value) VALUES ('after_close', 'ok')")
    other.commit()
    assert other.execute(
        "SELECT value FROM kernel_meta WHERE key = 'after_close'"
    ).fetchone()[0] == "ok"
    other.close()


def test_registry_is_keyed_by_thread_not_by_creation_order(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    tl = connect_threadlocal(path)
    seen: set[int] = set()
    done = threading.Event()

    def worker() -> None:
        tl.execute("SELECT 1")
        seen.add(id(tl._created.get(threading.get_ident())))
        done.set()

    tl.execute("SELECT 1")
    main_ident = threading.get_ident()
    t = threading.Thread(target=worker)
    t.start()
    assert done.wait(5)
    t.join()
    assert len(tl._created) == 2
    assert main_ident in tl._created

    tl.close()  # 主线程只认自己那一格
    assert list(tl._created) != [main_ident]
    assert len(tl._created) == 1, "别人那格不该被主线程关掉"
    # 剩那一格由它自己的线程收，测试里手动收掉避免 ResourceWarning
    leftover = next(iter(tl._created.values()))
    leftover.close()


def test_close_is_safe_when_this_thread_never_opened_one(tmp_path: Path, capsys) -> None:
    """没开过连接的线程调 close()：不炸、也不该顺手清掉别人那一格 —— 但**必须出声**。"""
    path = _seed(tmp_path)
    tl = connect_threadlocal(path)
    tl.execute("SELECT 1")
    holder = tl._created[threading.get_ident()]

    def stranger() -> None:
        tl.close()

    t = threading.Thread(target=stranger)
    t.start()
    t.join(5)
    assert tl._created.get(threading.get_ident()) is holder
    err = capsys.readouterr().err
    assert "疑似跨线程误用" in err, f"跨线程 close 是可疑形态，必须告警：{err!r}"
    holder.close()
    with pytest.raises(sqlite3.ProgrammingError):
        holder.execute("SELECT 1")


def test_short_lived_thread_returns_its_connection_quietly(tmp_path: Path, capsys) -> None:
    """fire-and-forget 线程"开一格 → 用 → close 归还"：零残留、零噪音。

    这是 distill 后台线程的标准生命周期（2026-10-04 审查快照的连接泄漏条目）：从前没人
    归还，Windows 线程 ID 近似单调递增，`_created` 的槽永远等不到同 ident 覆写 —— 每轮
    对话泄漏一个 fd。修复后线程 finally 里 `close()`，这条用例钉住"还了且不刷日志"。
    """
    path = _seed(tmp_path)
    tl = connect_threadlocal(path)
    tl.execute("SELECT 1")  # 主线程持一格，好证明工作线程的槽确实**各自独立**地被归还了
    worker_ident = threading.get_ident()  # 占位，worker 里覆盖

    def worker() -> None:
        nonlocal worker_ident
        worker_ident = threading.get_ident()
        tl.execute("SELECT 1")
        tl.close()  # ← distill finally 里那一句的等价物

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    assert worker_ident not in tl._created, "工作线程归还后，它的槽还在账上（泄漏没修）"
    assert threading.get_ident() in tl._created, "工作线程的归还误伤了主线程的槽"
    err = capsys.readouterr().err
    assert "[conn-close]" not in err, f"正常归还路径不该有告警：{err!r}"
    tl.close()  # 主线程自己收尾
