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

import contextlib
import sqlite3
import threading
import time
from typing import Any, cast

from langgraph.checkpoint.sqlite import SqliteSaver

from rolecard_agent.base.observability import logline
from rolecard_agent.storage.db import SqlConnection

#: 每条线程最多留几条检查点 —— **含**最新那条（`R28-20`：原先这行注释写着"不含最新"，
#: 而 `rank >= keep` 那句删的是第 7 条起，实际留下的是"最新 + 5 条祖先"= 共 6 条）。
#: 判据是实测的那条用例：10 条无过期 → 留下 6 条。
CHECKPOINT_KEEP_PER_THREAD = 6
#: 祖先快照的年龄上限（天）。超过就删，哪怕它还在那 6 条里。
CHECKPOINT_KEEP_DAYS = 14
#: 一次性收口的记账键（`kernel_meta`）。没有它，每次启动都会把"只留最新一条"重演一遍，
#: 那等于把用户新攒的最近几轮也吃掉。
_BACKLOG_KEY = "checkpoint_backlog_compacted_at"
#: 锁等待的告警阈值（毫秒）。单次拿锁等过它就落一行人读日志 —— 正常的微秒级拿锁
#: 不该刷屏，但"排在别人的大快照后面"必须出声（观测的全部意义就在那一行）。
LOCK_WAIT_WARN_MS = 100.0


class ObservedLock:
    """包住 `threading.Lock` 的观测层：拿锁等了多久，从此可断言、可告警。

    为什么要它（2026-10-04 审查快照的 saver 全局锁条目，"先包锁等待观测再拆"）：
    langgraph 的 `SqliteSaver` 用**一把**全局锁串行化全进程所有会话的 checkpoint
    读写 —— 会话 A 写 26 轮大快照期间，B 的历史回放/红点轮询全部排队。但"排队排了
    多久"此前没有任何读数，拆连接族（每线程一条）值不值、拆完有没有真变快，全都
    只能靠感觉。这一层把等待变成事实：

      * `stats()` 给累计读数（次数 / 最长 / 平均，测试与探针直接断言）；
      * 单次等待 ≥ `LOCK_WAIT_WARN_MS` 时 `logline(warning)` 出一行 —— 真机日志
        里自己会报告"谁在等、等了多久"，不需要谁记得去开探针。

    契约与 `threading.Lock` 同形（acquire / release / 上下文管理器），语义逐字不变：
    互斥照旧、非重入照旧。计数用一把私有锁保护 —— 它不能复用被观测的那把（在
    acquire 里再 acquire 同一把锁 = 自己等死自己）。
    """

    def __init__(self, warn_ms: float = LOCK_WAIT_WARN_MS) -> None:
        self._lock = threading.Lock()
        self._warn_ms = warn_ms
        self._count_lock = threading.Lock()
        self._acquires = 0
        self._total_wait_ms = 0.0
        self._max_wait_ms = 0.0

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        t0 = time.perf_counter()
        got = self._lock.acquire(*args, **kwargs)
        waited_ms = (time.perf_counter() - t0) * 1000.0
        with self._count_lock:
            self._acquires += 1
            self._total_wait_ms += waited_ms
            self._max_wait_ms = max(self._max_wait_ms, waited_ms)
            acquires, total, worst = (
                self._acquires,
                self._total_wait_ms,
                self._max_wait_ms,
            )
        if waited_ms >= self._warn_ms:
            # 只在超阈值时出声：常态的亚毫秒拿锁每次一行会把日志刷成噪音，而这一行
            # 恰恰是"拆连接族"要拿去对数的那个证据（谁在等、等了多久、这是第几次）。
            logline(
                "warning",
                "checkpoints",
                f"checkpoint 锁等待 {waited_ms:.0f}ms（累计 {acquires} 次拿锁、"
                f"最长 {worst:.0f}ms、平均 {total / acquires:.1f}ms）——"
                "另一会话正持锁写检查点，本次读写在排队",
            )
        return got

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> ObservedLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def stats(self) -> dict[str, float | int]:
        """累计读数：拿锁次数 / 总等待毫秒 / 最长等待毫秒（测试与探针的断言面）。"""
        with self._count_lock:
            return {
                "acquires": self._acquires,
                "total_wait_ms": round(self._total_wait_ms, 3),
                "max_wait_ms": round(self._max_wait_ms, 3),
            }


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


#: 空页攒到多少才值得在启动路径上回收一次。刻意是个"绝对值"而不是比例：
#: 一个 3 MB 的库攒 86% 空洞也就几百页（本机实测 790/914），回收它几毫秒；
#: 而真到了值得动的量级，页数本身就是那个信号。低于这个数就什么都不做 —— 启动路径上
#: 不该为了好看去碰文件。
_RECLAIM_MIN_FREE_PAGES = 256
#: 空洞占到文件的比例超过它，就改用**全量** VACUUM。
#:
#: 这个数是被实测逼出来的（副本库，本机 2026-09-29）：真库 914 页里 790 页是 freelist，
#: `PRAGMA incremental_vacuum` 只归还 **1 页**（914 → 913，文件一个字节没小），同一条库
#: `VACUUM` 用 **6 ms** 把它压到 123 页 / 0.5 MB。原因在 SQLite 的语义：incremental 只能砍掉
#: **文件尾部连续的那一段**空页，而"删掉散在中间的 127 条检查点"留下的洞本来就是散开的。
#: 所以只挂 incremental 的回收等于挂了一个几乎从不兑现的回收 —— 那条路只在一批删除连着文件尾
#: 时才有效，日常形态恰恰不是。
_RECLAIM_VACUUM_RATIO = 0.5


def _vacuum(conn: SqlConnection) -> int:
    """一次全量 VACUUM，返回归还的页数；库被别人占着就**放弃并说清**，绝不让启动失败。

    为什么要 catch：`make_checkpointer` 跑在装配期，而这台机器上可能同时开着第二个实例
    （开发态与安装态两份库、或本机实验用的第二实例，都是日常操作）。VACUUM 要排他锁，
    拿不到就是 `database is locked` —— 为一次磁盘整理把应用挡在门外，代价完全不成比例。
    """
    before = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        logline("warning", "checkpoints", f"库正被别的连接占着，这次不重排文件（{exc}）")
        return 0
    after = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
    return max(before - after, 0)


def reclaim_if_fragmented(
    conn: SqlConnection,
    *,
    min_free_pages: int = _RECLAIM_MIN_FREE_PAGES,
    min_ratio: float = _RECLAIM_VACUUM_RATIO,
) -> int:
    """空闲页攒够了才把空页还给磁盘；没攒够就**什么都不做**。返回释放掉的页数。

    为什么需要它（09-28 轮 `R28-17`）：`_reclaim_space` 原先只挂在修剪那两条路上，于是
    **"日常永不回收"** —— 真库实测 914 页里 790 页是空洞（86%）。修剪只在删会话时跑，
    而一个用得久的库大半的空洞来自检查点收口、消息删除、迁移重建这些**不叫"修剪"的路径**。
    启动时按阈值问一句是最省事的收口点：那一刻没有别的写者。

    两档，按"洞占多少"分：
      * 洞过半 → 一次全量 VACUUM（实测本机 3.7 MB / 6 ms，且顺手把 `auto_vacuum` 转成
        INCREMENTAL —— 那一刻本来就在重写整个文件，转换不要钱）；
      * 洞不过半 → 只做便宜的 `incremental_vacuum`，它只砍文件尾部的连续空页，够不着散洞。
    """
    free = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
    pages = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
    if free < min_free_pages:
        return 0
    ratio = free / pages if pages else 0.0
    if ratio < min_ratio:
        before = pages
        conn.execute("PRAGMA incremental_vacuum")
        after = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
        freed = max(before - after, 0)
        if freed:
            logline(
                "info",
                "checkpoints",
                f"归还尾部 {freed} 个空页（{before} 页 → {after} 页）",
            )
        return freed

    mode = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
    if mode == 0:
        # 转换只在**已经要付全量 VACUUM 的这一刻**做。这句 PRAGMA 落下去靠的是紧接着的
        # VACUUM：实测非空库上它只是"待写入的意图"（`PRAGMA auto_vacuum` 读回来还是 0、
        # 不开 VACUUM 就重开还是 0），VACUUM 重建文件时才把它写进头。既然这一刻本来就在
        # 重写整个文件，转换不要钱。
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    freed = _vacuum(conn)
    if freed:
        logline(
            "info",
            "checkpoints",
            f"空洞占 {ratio:.0%}（{free}/{pages} 页），全量重排归还 {freed} 页",
        )
    return freed


#: 启动那次 WAL 截断**临时**压到的忙等上限（毫秒）。
#: 压住它是刻意的：`connect()` 那条 5 秒 `busy_timeout` 是写给正常请求的，而有人拿着长读事务时
#: `wal_checkpoint(TRUNCATE)` 会**一直等到超时才放弃**（实测：6.6 MB 的 -wal + 一条挂着的读事务
#: → 返回 busy 且占住 5,038 ms）。开机不该被另一个实例扣住五秒，所以这里只等 250 ms，
#: 拿不到就下一趟再来 —— 少收一次不影响正确性，多等五秒影响的是"打开就能用"。
_WAL_TRUNCATE_BUSY_MS = 250


def truncate_wal_at_boot(conn: SqlConnection) -> int:
    """开机把 -wal 落回主库并截断；返回 -wal 里剩下的页数（0 = 收干净了；-1 = 这次没做成）。

    为什么这一半要放在**启动**而不是只放在退出（`R28-48`）：这台机器的发版形态没有任何一条
    退出路径跑到 `Runtime.shutdown()` —— 壳自己退出走 `shell/main/backend.ts` 的
    `taskkill /PID … /T /F`，安装包关旧进程同理，两个都是硬杀；只有 POSIX 的 SIGTERM 与
    开发态 Ctrl+C 才走得到 lifespan 那个 `finally`。于是 `R28-16` 的"正常退出时把 WAL 收干净"
    在**装机形态上从不兑现**（本机实测读数：装包那一刻 -wal 仍是 6,266,552 B，与两天前那条
    红项一字不差）。启动这一刻是同一份文件上唯一"确定还没有别的写者"的时刻，实测代价
    58 ms（6.6 MB WAL、无人竞争：主库 503 KB → 7.09 MB、-wal 归零，数据一行不少）。
    """
    try:
        prev = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    except sqlite3.Error:
        prev = 5000
    try:
        conn.execute(f"PRAGMA busy_timeout = {_WAL_TRUNCATE_BUSY_MS}")
        busy, log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError as exc:
        logline("warning", "checkpoints", f"这次没能收 WAL（{exc}）—— 不影响数据，下次启动再试")
        return -1
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"PRAGMA busy_timeout = {prev}")
    if int(busy or 0):
        logline(
            "warning",
            "checkpoints",
            f"WAL 没截断：有连接正读着同一份库，{int(log)} 页还压在 -wal 里"
            f"（只等了 {_WAL_TRUNCATE_BUSY_MS} ms，开机不等第二个实例）",
        )
        return int(log)
    return 0


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
    # 锁等待观测（2026-10-04 审查快照的 saver 全局锁条目，第一步"先观测"）：
    # langgraph 的 saver 用**一把**全局锁串行化全进程所有会话的 checkpoint 读写，
    # "会话 A 写大快照期间 B 的历史回放/红点轮询全部排队"此前只有源码证据、没有现场
    # 读数。换上观测锁，语义逐字不变（同一把互斥、同样的 acquire/release 契约），
    # 但"等锁等了多久"从此可断言、超阈值自动出声 —— 拆不拆连接族等它说话。
    # 经 Any 赋值而不是 setattr/B010 那对互斥的规矩：`lock` 在 langgraph 侧被推断成
    # `_thread.LockType`，直接赋协议同形的观测锁会被 mypy 拒，cast 到 LockType 又是
    # 撒谎。这一处的形状契约由 ObservedLock 自己保证（acquire/release/with 同形），
    # 类型系统看不见它 —— cast(Any) 是"我知道、我负责"的如实写法。
    cast(Any, saver).lock = ObservedLock()
    saver.setup()
    ensure_checkpoint_clock(conn)
    compacted = compact_backlog_once(conn)
    pruned = prune_checkpoints(conn)
    # 启动路径上按阈值问一句空页（`R28-17`）：修剪只在"删过会话"那天回收，而日常攒下的
    # 空洞来自收口/删除/迁移重建 —— 本机实测 86% 的页是 freelist 就是这么来的。
    # 放在修剪之后：修剪自己会释放页，紧接着这一步就能把它们真还给磁盘。
    reclaim_if_fragmented(conn)
    # WAL 也收在这一刻（`R28-48`）：发版形态没有任何退出路径跑到 `Runtime.shutdown()`，
    # 所以"落回主库"只能在开机这一头做。排在 VACUUM 之后：先让文件缩小，再把 -wal 归零。
    truncate_wal_at_boot(conn)
    if compacted or pruned:
        logline(
            "info",
            "checkpoints",
            f"祖先快照收口 {compacted} 行、修剪 {pruned} 行"
            f"（每条线程最多留 {CHECKPOINT_KEEP_PER_THREAD} 条（含最新那条），且只留 "
            f"{CHECKPOINT_KEEP_DAYS} 天以内）",
        )
    return saver

