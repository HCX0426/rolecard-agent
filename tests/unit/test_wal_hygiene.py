"""WAL 的两件卫生事（09-28 轮 `R28-16`）：有上限，且正常退出时落回主库。

症状是本机实测读出来的：`app.db-wal` 6,266,552 字节，而主库 mtime 停在两天前 ——
 
* SQLite 的自动检查点是**写触发**的：机器闲置下来不再写一笔，已经攒在 -wal 里的
  那两天就永远不落回主文件，也永远不被截断；
* 默认检查点之后 -wal 也不缩小，它留在长到的那个大小。

于是"-wal 坏掉 / 被删"的代价从"最后一笔"变成"这两天的一切"。
（备份不受这条影响：`backup_data_root.py` 与 `scratch_db` 走的是 sqlite `backup()`，
 它读得到 WAL 里的内容 —— 受影响的是盘与文件系统那一类故障。）

这里钉三件事：连接上带了 `journal_size_limit`；正常退出把 WAL checkpoint 并截断；
以及**截断之后数据仍在主库里**（少了第三条，前两条可以靠"把数据弄丢了"来达成）。
"""

from __future__ import annotations

from pathlib import Path

from rolecard_agent.core.bootstrap import build_runtime
from rolecard_agent.domains.registry import DOMAINS
from rolecard_agent.storage.db import connect
from tests.unit.test_bootstrap import HealthQueryService, _settings, _wiring


def _runtime(tmp_path: Path):
    return build_runtime(
        domains=DOMAINS,
        query_factory=HealthQueryService,
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
