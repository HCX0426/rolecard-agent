"""WAL 的三件卫生事（09-28 轮 `R28-16` + 09-29 轮 `R28-48`）：有上限、开机收一次、退出再收一次。

症状是本机实测读出来的：`app.db-wal` 6,266,552 字节，而主库 mtime 停在两天前 ——

* SQLite 的自动检查点是**写触发**的：机器闲置下来不再写一笔，已经攒在 -wal 里的
  那两天就永远不落回主文件，也永远不被截断；
* 默认检查点之后 -wal 也不缩小，它留在长到的那个大小。

于是"-wal 坏掉 / 被删"的代价从"最后一笔"变成"这两天的一切"。
（备份不受这条影响：`backup_data_root.py` 与 `scratch_db` 走的是 sqlite `backup()`，
 它读得到 WAL 里的内容 —— 受影响的是盘与文件系统那一类故障。）

`R28-48` 补的是**"退出时收一次"这一半在装机形态上从不兑现**：壳退出与安装包关旧进程都是
`taskkill /T /F`（硬杀），跑不到 lifespan 的 `finally`，所以这里除了"连接带 `journal_size_limit`"
与"正常退出把 WAL checkpoint 并截断"，还要钉**开机那一次**、以及它**不许替第二个实例等满五秒**。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from rolecard_agent.core.bootstrap import build_runtime
from rolecard_agent.core.checkpointer import truncate_wal_at_boot
from rolecard_agent.domains.registry import DOMAINS, build_query_services, domain_seed_roles
from rolecard_agent.features.proactive import build_gateway
from rolecard_agent.storage.db import bootstrap, connect
from tests.unit.test_bootstrap import _settings, _wiring


def _runtime(tmp_path: Path):
    return build_runtime(
        domains=DOMAINS,
        query_services_factory=build_query_services,
        domain_seed_roles=domain_seed_roles(),
        proactive_factory=build_gateway,
        registry_factory=_wiring,  # type: ignore[arg-type]
        env_settings=_settings(tmp_path),
        model_factory=lambda *_a, **_k: None,
    )


def test_connect_sets_a_wal_size_ceiling(tmp_path: Path) -> None:
    """每条连接都带 `journal_size_limit` —— 它是 WAL 的**上界**，不是"要不要检查点"。"""
    conn = connect(tmp_path / "app.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        limit = int(conn.execute("PRAGMA journal_size_limit").fetchone()[0])
        assert limit > 0, "0 = 不设上限，于是 -wal 可以无限背着最近的写入"
    finally:
        conn.close()


def test_shutdown_checkpoints_the_wal_back_into_the_main_db(tmp_path: Path) -> None:
    """正常退出之后：数据在主库里，而 -wal 不再留着它。

    这里**故意再挂一条读连接**，因为那才是本 app 的真实形状：主连接之外还有
    `ThreadLocalConnection`（每个用到的线程一条）。SQLite 的自动检查点只在
    **最后一条连接关闭**时做一次 —— 还有别的连接活着就没有那一次。
    （反过来说：单连接情形下 `conn.close()` 自己就会把 WAL 收干净，所以这条用例
    如果只开一条连接，它测的是 SQLite 而不是我们的代码 —— 变异核验时就是这么发现的。）
    """
    runtime = _runtime(tmp_path)
    db = Path(runtime.env_settings.sqlite_path)
    wal = db.with_name(db.name + "-wal")

    lingering = connect(db)  # 第二条连接：全程不开事务，也不关
    try:
        runtime.conn.execute(
            "INSERT INTO session_thread (thread_id, user_id, current_role_id, title)"
            " VALUES ('s_walprobe', 'local-user', 'general_assistant', '落盘检查')"
        )
        runtime.conn.commit()
        assert wal.exists() and wal.stat().st_size > 0, "WAL 模式却没写出 -wal，用例量程是空的"
        main_before = db.stat().st_size

        runtime.shutdown()

        # 关掉之后 -wal 要么不存在、要么是 0 字节 —— 两种都算"落回去了"。
        size = wal.stat().st_size if wal.exists() else 0
        assert size == 0, f"-wal 还剩 {size} 字节没被 checkpoint 回主库（还有别的连接活着时更要紧）"
        assert db.stat().st_size >= main_before, "主库反而变小了？截断不该动主文件"

        row = lingering.execute(
            "SELECT title FROM session_thread WHERE thread_id = 's_walprobe'"
        ).fetchone()
        assert row is not None and str(row[0]) == "落盘检查", "截断把数据一起截没了"
    finally:
        lingering.close()



def _wal_size(db: Path) -> int:
    wal = db.with_name(db.name + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def test_boot_truncate_moves_the_wal_back_into_the_main_db(tmp_path: Path) -> None:
    """开机那一次真的把 -wal 落回主库并截断，而且**数据是搬回去的不是丢掉**。"""
    db = tmp_path / "boot.db"
    conn = connect(db)
    bootstrap(conn)
    conn.execute("INSERT INTO kernel_meta (key, value) VALUES (?, ?)", ("wal_probe", "y" * 4000))
    conn.commit()
    assert _wal_size(db) > 0, "夹具没造出 WAL，这条测不出东西"

    assert truncate_wal_at_boot(conn) == 0
    assert _wal_size(db) == 0, "截断之后 -wal 里还留着内容"
    row = conn.execute("SELECT value FROM kernel_meta WHERE key = 'wal_probe'").fetchone()
    assert row is not None and str(row[0]).startswith("y"), "搬回去的时候把数据弄丢了"
    conn.close()


def test_boot_truncate_does_not_wait_for_a_second_reader(tmp_path: Path) -> None:
    """有人正读着同一份库时：**放弃而不是替它等**。返回值>0 = 明说"这次没收成"。

    为什么这条值得单独钉：`connect()` 那条 5 秒 `busy_timeout` 是写给正常请求的，而
    `wal_checkpoint(TRUNCATE)` 撞上长读事务会**一直等到超时才返回 busy**（实测 5,038 ms）。
    那笔账落在开机路径上，症状就是"打开应用多等五秒"，而它换来的收益是零。
    """
    db = tmp_path / "contended.db"
    conn = connect(db)
    bootstrap(conn)
    conn.execute("INSERT INTO kernel_meta (key, value) VALUES (?, ?)", ("wal_probe", "q" * 4000))
    conn.commit()

    reader = connect(db)  # 第二条连接挂着一个读快照 = 本机实验用第二实例的形状
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM kernel_meta").fetchone()

    started = time.perf_counter()
    left = truncate_wal_at_boot(conn)
    waited = time.perf_counter() - started

    assert left > 0, "读事务挡着却报「收干净了」—— 那是假绿"
    assert waited < 2.0, f"它替第二个实例等了 {waited:.1f} 秒，开机不该被扣住"
    assert _wal_size(db) > 0, "没收成却把 WAL 弄没了"
    held = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    assert held == 5000, f"临时压小的忙等（{held}）留在连接上了"
    reader.close()
    conn.close()


def test_the_leftover_wal_from_a_killed_process_gets_given_back_at_boot(tmp_path: Path) -> None:
    """**生产链那一发**：上一趟进程被**硬杀**留下的 -wal，由下一次开应用收回去。

    为什么不手工造这个状态：只有进程死掉才会留下"已提交但没落回主库"的 -wal —— 最后一条
    连接正常关闭时 SQLite 自己就检查点了（本文件上面那条用例钉的正是这件事）。而
    `taskkill /T /F` 就是装机形态每天在走的退出路径。
    """
    db = tmp_path / "headless.db"
    seed = connect(db)
    bootstrap(seed)
    seed.close()

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"import sqlite3, time; c = sqlite3.connect(r'{db}');"
                " c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=5000');"
                " c.execute('CREATE TABLE IF NOT EXISTS w (payload TEXT)');"
                " c.execute('BEGIN');"
                " [c.execute('INSERT INTO w VALUES (?)', ('w' * 4000,)) for _ in range(600)];"
                " c.commit(); time.sleep(30)"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # 等到**提交真的看得见**再杀：只看 -wal 存不存在会杀在事务中间（第一笔写就建 WAL 头，
        # 那时候还没 commit），恢复时整笔回滚 —— 第一版就是这么量的，读出 0/600 而不是"数据没了"。
        # 600 行 ≈ 2.4 MB，**没到 1000 页那个自动检查点阈值**，所以提交之后帧仍然留在 -wal 里，
        # 正是她机器上那一刻的形状。
        deadline = time.time() + 30
        seen = 0
        while time.time() < deadline:
            try:
                probe = sqlite3.connect(str(db), timeout=1)
                seen = int(probe.execute("SELECT COUNT(*) FROM w").fetchone()[0])
                probe.close()
            except sqlite3.OperationalError:
                continue  # 它正在提交、库被锁住 —— 下一轮再看
            if seen == 600:
                break
            time.sleep(0.2)
        assert seen == 600, f"子进程没把 600 行提交出来（读到 {seen}），这条测不出东西"
        assert _wal_size(db) > 0, "提交之后没有留下 WAL —— 量程是空的"
    finally:
        child.kill()  # 硬杀：跑不到任何退出处理器，正是壳与安装包那条路
        child.wait(timeout=10)
    assert _wal_size(db) > 0, "硬杀之后 WAL 竟然自己没了 —— 量程是空的"

    runtime = _runtime(tmp_path)  # 用同一个 tmp_path：走真装配根，不是直接调那个函数
    try:
        assert Path(runtime.env_settings.sqlite_path) == db
        assert _wal_size(db) == 0, "被硬杀那一趟的 -wal 没在下一次启动时被收回去"
        check = sqlite3.connect(str(db))
        try:
            got = int(check.execute("SELECT COUNT(*) FROM w").fetchone()[0])
            assert got == 600, f"收回去的时候把数据弄丢了（{got}/600）"
        finally:
            check.close()
    finally:
        runtime.shutdown()
