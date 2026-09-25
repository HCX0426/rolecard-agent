"""Session persistence. SqliteSaver keyed by thread_id.

Note: InMemorySaver (formerly MemorySaver) is dev-only - state is lost on restart.

`SqliteSaver.setup()` MUST be called before the first write; it is what creates the
checkpoint tables. Forgetting it produces a confusing "no such table" error on the first
conversation rather than at startup, which is why the factory here always calls it.

这一层还负责**检查点的磁盘卫生**（09-26 轮 R26-07，用户 2026-09-25 拍："先把历史数据删了，
后面加时间列，按时间裁"）。三件事都在这个模块里，因为只有它知道"检查点表已经存在了"：

1. `checkpoints` 表由 langgraph 拥有，**它没有时间列**，而 `checkpoint` 那列是 msgpack
   BLOB（`json_extract` 直接 malformed JSON）⇒ 光看这张表根本不知道哪条是旧的。补一列
   `created_at` 并由**触发器**在每次 INSERT 时盖上时钟：langgraph 的 INSERT 列清单是它
   自己的，我们不改它一行，只在库上挂一条 DDL。
2. 一次性收口：把每张线程除最新那条之外的祖先快照全删掉（真库实测 150 行 / 23 线程 /
   46.14 MB → 23 行 / 3.28 MB）。删祖先安全、不删对话：**每条线程的最新那个检查点就是
   会话的全部真相**（langgraph 每轮写的是整份 state 快照，不是增量），而"编辑重跑"走的
   是 `update_state` 改当前态，不回到旧检查点。
3. 之后的常态修剪按 `CHECKPOINT_KEEP_PER_THREAD` + `CHECKPOINT_KEEP_DAYS` 两个闸走。

为什么两个闸都要、而不是只按时间：单条长会话的**每一轮都存整份历史**，字节数随轮数平方
增长（实测最大那条线程 26 轮 40.48 MB，而它最新那一条只占 1.56 MB）—— 只看"最近两周"
会让一个天天用的会话攒下两周的全部快照，只看"最近几条"又会让一条闲置半年的线程白白占着
十几 MB。所以判据是"最近的几条**且**最近的两周内"，两个条件缺一个就删。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from langgraph.checkpoint.sqlite import SqliteSaver

from rolecard_agent.storage.db import SqlConnection

#: 每条线程最多留几条祖先快照（**不含**最新那条 —— 最新那条永远不删）。
CHECKPOINT_KEEP_PER_THREAD = 6
#: 祖先快照的年龄上限（天）。超过就删，哪怕它还在那 6 条里。
CHECKPOINT_KEEP_DAYS = 14
#: 一次性收口的记账键（`kernel_meta`）。没有它，每次启动都会把"只留最新一条"重演一遍，
#: 那等于把用户新攒的最近几轮也吃掉。
_BACKLOG_KEY = "checkpoint_backlog_compacted_at"


def _has_column(conn: SqlConnection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def ensure_checkpoint_clock(conn: SqlConnection) -> None:
    """给 `checkpoints` 补上可读的时间列，并让每次写入自动盖上时钟。返回是否建了触发器。

    触发器而不是改 langgraph：它的 `INSERT` 列清单我们不该碰（升级即失效），而 SQLite 的
    `AFTER INSERT` 触发器对"表由别人建、写入由别人发"这种局面是干净的解法 —— 库里的 DDL
    是我们自己的事。`ALTER TABLE ADD COLUMN` 也不能带 `DEFAULT (strftime(...))` 这种非常量
    默认值，所以"新行有时间"这件事只能靠触发器。
    """
    if not _has_column(conn, "checkpoints", "created_at"):
        conn.execute("ALTER TABLE checkpoints ADD COLUMN created_at TEXT")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS checkpoints_stamp_created_at
        AFTER INSERT ON checkpoints
        BEGIN
            UPDATE checkpoints
               SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE rowid = new.rowid AND created_at IS NULL;
        END;
        """
    )
    conn.commit()


def _flagged(conn: SqlConnection, key: str) -> bool:
    row = conn.execute("SELECT value FROM kernel_meta WHERE key = ?", (key,)).fetchone()
    return bool(row and row[0])


def _set_flag(conn: SqlConnection, key: str) -> None:
    conn.execute(
        "INSERT INTO kernel_meta (key, value) VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key,),
    )


def _drop_orphan_writes(conn: SqlConnection, gone: list[tuple[str, str]]) -> int:
    """删掉**这次被删的那些检查点**名下的待写入行。

    只按刚删的 (线程, 检查点) 精确删，不用"没有对应检查点的 writes 都是孤儿"那种全局判据：
    `writes` 的语义就是"某个检查点还没落盘时先攒着"，飞行中的那一轮正是全局判据会误杀的形态。
    """
    if not gone:
        return 0
    total = 0
    for thread_id, checkpoint_id in gone:
        cur = conn.execute(
            "DELETE FROM writes WHERE thread_id = ? AND checkpoint_id = ?",
            (thread_id, checkpoint_id),
        )
        total += cur.rowcount if cur.rowcount > 0 else 0
    return total


def _stale_ancestors(
    conn: SqlConnection, keep_per_thread: int, keep_days: int
) -> list[tuple[str, str]]:
    """该删的祖先检查点 `(thread_id, checkpoint_id)` 清单（纯判定，不动库）。

    排序按 `rowid` **降序**：SQLite 的 rowid 就是插入顺序，而 langgraph 每轮追加一个新
    检查点，所以"同 scope 内 rowid 最大"= 最新那条 —— 不需要解析 msgpack 里的 ts。
    分组键是 `(thread_id, checkpoint_ns)` 而**不只是 thread_id**：子图（agent 模式那侧）
    的检查点写在同一个 thread 的另一个 ns 下，只按 thread 留最新一条会把根图那条删掉、
    把子图那条留下 —— 那不叫修剪，那叫删会话。
    """
    rows = conn.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id, created_at FROM checkpoints"
        " ORDER BY thread_id, checkpoint_ns, rowid DESC"
    ).fetchall()
    # 一个截止时刻、一次比较，而不是每行问一次 SQL：`created_at` 是触发器写的
    # `%Y-%m-%dT%H:%M:%fZ`（UTC、定宽、带 Z），**字典序就是时间序**，所以字符串比较够用。
    cutoff = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)", (f"-{int(keep_days)} days",)
    ).fetchone()[0]
    out: list[tuple[str, str]] = []
    previous: tuple[str, str] | None = None
    rank = 0
    for thread_id, checkpoint_ns, checkpoint_id, created_at in rows:
        scope = (str(thread_id), str(checkpoint_ns))
        if scope != previous:
            previous, rank = scope, 0
        else:
            rank += 1
        if rank == 0:
            continue  # 最新那条 = 这条会话的全部真相，永不删
        if rank >= keep_per_thread:
            out.append((str(thread_id), str(checkpoint_id)))
            continue
        if created_at is None:
            continue  # 没时钟可读 = 不知道多旧，留着（只在触发器上线前那批里出现）
        if str(created_at) < str(cutoff):
            out.append((str(thread_id), str(checkpoint_id)))
    return out


def compact_backlog_once(conn: SqlConnection) -> int:
    """一次性收口：每个 (线程, checkpoint_ns) 只留最新那个检查点（用户拍的那句"先把历史数据删了"）。

    跑完在 `kernel_meta` 记账，第二次启动就是空操作。之后由常态修剪接管。
    真库实测：150 行 → 23 行，检查点负载 46.14 MB → 3.28 MB。
    """
    if _flagged(conn, _BACKLOG_KEY):
        return 0
    rows = conn.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id, rowid FROM checkpoints"
    ).fetchall()
    # 与 `_stale_ancestors` 同一个分组口径：每张线程的**每个 ns** 各留最新一条。
    newest: dict[tuple[str, str], int] = {}
    for thread_id, checkpoint_ns, _cid, rowid in rows:
        scope = (str(thread_id), str(checkpoint_ns))
        newest[scope] = max(newest.get(scope, -1), int(rowid))
    gone = [
        (str(thread_id), str(checkpoint_id))
        for thread_id, checkpoint_ns, checkpoint_id, rowid in rows
        if int(rowid) != newest[(str(thread_id), str(checkpoint_ns))]
    ]
    for thread_id, checkpoint_id in gone:
        conn.execute(
            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ?",
            (thread_id, checkpoint_id),
        )
    _drop_orphan_writes(conn, gone)
    _set_flag(conn, _BACKLOG_KEY)
    conn.commit()
    _reclaim_space(conn)
    return len(gone)


def _reclaim_space(conn: SqlConnection) -> None:
    """把删出来的空页还给磁盘。

    只 DELETE 不缩文件（真库实测 freelist 4.2%，"删了会话"和"文件变小"是两件事）。
    第一次收口时顺手把 `auto_vacuum` 切成 INCREMENTAL —— 这个改动**只有配一次全量 VACUUM
    才生效**，而那一刻正好是全仓唯一一次"删完一大堆、又不怕慢"的时机；之后的修剪就只走
    便宜的 `incremental_vacuum`，不再全量重排。
    """
    mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    if int(mode) == 0:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")
        return
    conn.execute("PRAGMA incremental_vacuum")


def prune_checkpoints(
    conn: SqlConnection,
    *,
    keep_per_thread: int = CHECKPOINT_KEEP_PER_THREAD,
    keep_days: int = CHECKPOINT_KEEP_DAYS,
) -> int:
    """常态修剪：删掉"不在最近几条之内"或"比保留期更旧"的祖先检查点。返回删掉的行数。"""
    gone = _stale_ancestors(conn, keep_per_thread, keep_days)
    for thread_id, checkpoint_id in gone:
        conn.execute(
            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ?",
            (thread_id, checkpoint_id),
        )
    _drop_orphan_writes(conn, gone)
    if gone:
        conn.commit()
        _reclaim_space(conn)
    return len(gone)


def make_checkpointer(conn: SqlConnection) -> SqliteSaver:
    """Wrap an existing connection.

    Takes a connection rather than a path so the caller controls when it is opened and
    closed - the same connection also backs the kernel tables, and SQLite cannot have two
    writers fighting over one file.

    装配时就做磁盘卫生（补时钟 → 一次性收口 → 常态修剪）：这张表是 langgraph 建的，
    在它 `setup()` 之前任何地方碰 `checkpoints` 都是"no such table"。
    """
    # `SqliteSaver` 的签名要求真实 `sqlite3.Connection`，而应用传进来的是
    # `SqlConnection`（可能是 `ThreadLocalConnection`，它转发同一个方法子集）。
    # 运行期成立、类型系统表达不了 —— 显式 cast 并留下理由，而不是把签名放宽成 Any
    # 让整个模块失去检查。
    saver = SqliteSaver(cast("sqlite3.Connection", conn))
    saver.setup()
    ensure_checkpoint_clock(conn)
    compacted = compact_backlog_once(conn)
    pruned = prune_checkpoints(conn)
    if compacted or pruned:
        print(
            f"[checkpoints] 祖先快照收口 {compacted} 行、修剪 {pruned} 行"
            f"（保留每条线程最新 1 条 + 最近 {CHECKPOINT_KEEP_PER_THREAD} 条且 "
            f"{CHECKPOINT_KEEP_DAYS} 天以内）",
            flush=True,
        )
    return saver
