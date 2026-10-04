"""SQLite connection helpers + schema bootstrap.

Bootstrap order is fixed and not optional:
  1. core/schema.sql        tenant / app_user / session_thread / plugin / audit_log
  2. roles/schema.sql       role_card
  3. domains/<x>/schema.sql for each ENABLED plugin, in DOMAINS order

Step 1 must run first: domains reference app_user(user_id).

Every connection MUST enable `PRAGMA foreign_keys = ON` - SQLite ignores foreign keys by
default, which would silently turn the ON DELETE CASCADE in domains/health/schema.sql into
a no-op and leave orphaned index rows behind.

列级迁移是**声明驱动**的：`bootstrap` 每次启动都拿 `schema.sql` 的声明形状比对这份库，
缺列自动 `ADD COLUMN`（`reconcile_columns`），所以加一列只改 `schema.sql` 一处就够。
只有"形状根本不同"的表才需要在 `_migrate` 里写一次整表重建。**绝不要靠删库来升级** ——
`data/sqlite/app.db` 与安装目录下那份是真实数据，不是演示脚手架（09-26 轮 R26-05 抓到
`CONTRIBUTING.md` 就是这么教的，那条已订正）。
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
import sqlite3
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# Domain ids become path segments, so they are validated rather than trusted.
_DOMAIN_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# 当前请求的**库代际**：中间件每请求发一个新 id；真正持连接的线程发现代际变了，
# 就先把上次可能残留的未提交事务回滚掉（审查报告 P1-10）。
# 为什么不能在中间件里直接 rollback：中间件跑在事件循环线程，而同步端点与图执行
# 各在别的线程持连接 —— 在那里 rollback 清的是另一条线程的连接，等于没清。
_REQUEST_EPOCH: contextvars.ContextVar[str] = contextvars.ContextVar("db_request_epoch", default="")


def set_request_epoch(epoch: str) -> None:
    """标记当前请求的库代际（接入层中间件每请求调用一次）。"""
    _REQUEST_EPOCH.set(epoch)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this schema depends on.

    `check_same_thread=False` because FastAPI serves requests from a thread pool; the
    caller is responsible for not sharing one connection across concurrent writers.
    """
    target = Path(path)
    if target.parent and str(target.parent) not in {"", "."}:
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # busy_timeout 必须是**第一个** PRAGMA：切换 journal_mode 本身要拿排他锁，而并发首次
    # 建连接时如果超时还没生效，就会直接抛 "database is locked"（实测踩到过 —— 把这条
    # 放在 WAL 之后等于没设）。
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    # 空页归还能力必须在**建库那一刻**就有（`R28-17`）：`auto_vacuum` 只在 `page_count == 0`
    # 时写得进文件头，库一旦有了第一页，这句 PRAGMA 就退回"要配一次全量 VACUUM 才生效"。
    # 所以它必须排在 `journal_mode = WAL` **之前** —— 切 WAL 就是那"第一笔写"（实测：先 WAL
    # 后本句，重开连接读回 0；先本句后 WAL，读回 2）。放在这里对已存在的库是无害的空操作，
    # 老库仍由 `checkpointer._reclaim_space` 在"删完一大堆、不怕慢"那一刻转换。
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL 的上限（`R28-16`）：默认行为是"自动检查点之后把 -wal 留在它长到的那个大小"，
    # 于是 -wal 可以背着近两天的写入一直长（本机实测 6,266,552 B 而主库两天没落一笔）。
    # 设了 `journal_size_limit` 之后，SQLite 在检查点时把 -wal **截断到这个字节数以内** ——
    # 一次 -wal 损坏丢的是"上限那一截"，不是两天。8 MB 是刻意留的余量：日常一次会话写入
    # 远小于它，太小的话每次检查点都要 ftruncate，反而在白盘上做无用功。
    # 另一半在 `checkpointer.truncate_wal_at_boot()`：**开机**时把 -wal 直接 checkpoint(TRUNCATE)
    # 干净（`R28-48`）。原先那半条挂在 `Runtime.shutdown()` 上，而装机形态没有任何一条退出路径
    # 跑得到那里 —— 壳退出与安装包关旧进程都是 `taskkill /T /F`（硬杀），只有 POSIX 的 SIGTERM
    # 与开发态 Ctrl+C 才走 lifespan 的 `finally`。`shutdown()` 那一半留着（那两条路仍然要收）。
    conn.execute("PRAGMA journal_size_limit = 8388608")
    # synchronous=NORMAL 是 WAL 的官方推荐档（2026-10-04 审查快照的写放大条目）：默认 FULL
    # 意味着**每次 commit 都 fsync**，而记忆注入一轮就 commit 8 次 —— 全部排进首 token 前的
    # 等待里。NORMAL 在断电时可能丢最近一个已提交事务，但已 checkpoint 的数据不受影响；
    # 本库唯一的逐轮高频 commit 恰恰是统计性的 hit_count，丢一格可接受。要绝对不丢用
    # backup，不要把 synchronous 钉回 FULL。
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class ThreadLocalConnection:
    """一条"逻辑连接"，内部为**每个线程**各持一条真实连接。

    为什么需要它：sqlite3 的连接对象本身不是线程安全的 —— `check_same_thread=False` 只是
    关掉 Python 侧的那道检查，并不会让并发 `execute` 变安全。而 FastAPI 的同步端点跑在
    线程池里，因此"全进程共用一条连接"意味着多个线程同时操作同一个连接对象，症状是偶发的
    `database is locked`、游标状态错乱，且极难复现。

    这个类把"只有一条连接"的假象维持给调用方（服务层、工具层、端点、langgraph 的
    checkpointer 都像以前一样持有它），实际每次调用都落到**当前线程自己的**连接上：

      * 服务层 / 工具层 / 端点代码一行不改 —— 重构成本为零；
      * 每个线程独占一条连接，跨线程共享彻底消失；
      * WAL 让多条连接可并发读，写冲突由 busy_timeout 排队（见 connect）。

    代价要说清：它**不是**连接池，也不做跨连接的事务协调 —— 两个线程各自开事务，仍然只能
    靠 SQLite 的写锁串行化。它解决的是"同一连接对象被并发使用"这个 unsafe 用法，不是
    "SQLite 写并发低"这个规模问题（后者是 v2.5 生产化替换的话题）。

    线程会被线程池复用，所以跨请求可能残留未提交事务。清理由**本类在取连接时**完成：
    接入层中间件每请求调 `set_request_epoch()` 发一个新代际号，`_current()` 发现本线程的
    代际变了（= 新请求第一次用库）就先回滚一次。早期版本把 `rollback_current()` 放在
    中间件里 —— 但中间件跑在事件循环线程，清的是另一条线程的连接，等于没清（P1-10）。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        opener: Callable[[str | Path], sqlite3.Connection] = connect,
    ) -> None:
        self._path = Path(path)
        self._opener = opener
        self._local = threading.local()
        self._created: dict[int, sqlite3.Connection] = {}
        self._lock = threading.Lock()

    def _current(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._opener(self._path)
            self._local.conn = conn
            with self._lock:
                # 账按**线程身份**记：`close()` 只认领自己这一格（`R102-02`）。
                self._created[threading.get_ident()] = conn
        # 请求代际检查必须发生在**真正持连接的线程**里：线程池的线程会被复用，
        # 上一个请求若在事务中途异常退出，残留的 BEGIN/未提交改动会被下一个请求
        # 继承并 commit（半写入落地）。中间件只负责发代际号，清理在这里做。
        epoch = _REQUEST_EPOCH.get()
        if getattr(self._local, "epoch", None) != epoch:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            self._local.epoch = epoch
        return conn

    def rollback_current(self) -> None:
        """丢弃本线程可能残留的未提交事务（线程池线程会被下一个请求复用）。"""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()

    # 高频方法显式转发（比 __getattr__ 快，也让读代码的人一眼看到这是转发）
    # 参数类型用 Any 而不是 object：这是**纯透传**，sqlite3 的 execute/cursor 都有重载，
    # 写 object 会让 mypy 挑不出匹配的重载（报 call-overload），而这里并不打算约束参数形状。
    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._current().execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._current().executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._current().executescript(*args, **kwargs)

    def commit(self) -> None:
        self._current().commit()

    def rollback(self) -> None:
        self._current().rollback()

    def cursor(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._current().cursor(*args, **kwargs)

    def close(self) -> None:
        """只关**本线程**那一格连接，并说清有几格留在了别人手里。

        `R102-02`：这一句从前是**反着写的** —— docstring 说"其它线程正在使用的连接不能在这里关"，
        代码却把 `_created` 全量 close，外面还罩着 `contextlib.suppress(sqlite3.Error)`。
        实测的后果不是"报个错"而是**静默丢写**：另一线程那笔未提交的写入，在主线程 close 之后
        再 commit 不抛、回读为 None（那笔改动就这样没了）。sqlite3 本来就禁止跨线程使用连接对象，
        跨线程 close 更是没有意义的动作。

        现在：登记的账按**线程身份**存，本线程那格先 rollback 再 close（挂着写事务的连接
        关之前必须先把事务结束掉，`R102-42` 同一条纪律）；别人那格一格都不碰，交给它们的线程
        收（进程退出时由解释器清理，与从前 docstring 的承诺一致）。留下了几格要**出声** ——
        一条从不报告的收尾等于一条没有的收尾。
        """
        ident = threading.get_ident()
        with self._lock:
            mine = self._created.pop(ident, None)
            left = len(self._created)
        if mine is not None:
            try:
                if mine.in_transaction:
                    mine.rollback()
                mine.close()
            except sqlite3.Error as exc:
                print(
                    f"[conn-close] 本线程的连接没关干净：{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        # 只清**本线程**的连接槽：`threading.local` 的属性本来就是按线程隔的，所以赋值 None
        # 只影响自己；而整体换掉 `self._local` 会把别人那格从新对象上抹掉 —— 那条线程下一次
        # `_current()` 会**另开一格**，它挂着的未提交写入就这么没了。这正是 `R102-02` 原本的
        # 后果换了个来源，被本轮新加的用例当场抓到。
        self._local.conn = None
        self._local.epoch = None
        if mine is None and left:
            # 只对"可疑"的那一出声（2026-10-04 审查快照的连接泄漏条目改）：本线程**没有
            # 自己的槽**却在 close，才是跨线程误用的签名。线程归还自己的槽（fire-and-forget
            # 短命线程用完即还，正是泄漏修复要的正常路径）与进程收尾都不是可疑形态 ——
            # 从前每次都打印，等于把"每轮对话刷一行"当成了日志。
            print(
                f"[conn-close] 留 {left} 格连接给它们各自的线程收（本线程没有自己的槽却调了 close"
                "—— 疑似跨线程误用，请检查调用方）",
                file=sys.stderr,
                flush=True,
            )

    def __getattr__(self, name: str) -> object:
        # 只在真正缺少该属性时兜底转发（row_factory / total_changes / in_transaction 等）。
        # 双下划线名字一律不转发，避免 pickle / copy 之类协议调用时意外新建连接。
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._current(), name)


def connect_threadlocal(path: str | Path) -> ThreadLocalConnection:
    """`connect` 的线程安全替身：给接入层（多线程）用，语义见 `ThreadLocalConnection`。"""
    return ThreadLocalConnection(path)


# 服务层真正依赖的连接契约：这两者共同暴露的方法子集
# （execute / executemany / executescript / cursor / commit / rollback）。
#
# 为什么是 Union 而不是 Protocol：**测试传真实 `sqlite3.Connection`、应用传
# `ThreadLocalConnection`，两者都必须被接受**。Protocol 要求 `sqlite3.Connection` 逐条
# 结构匹配（typeshed 里的重载签名很苛刻），Union 则直接列出两个合法实参，语义更准也更稳。
#
# 引入它的直接原因：`scripts/check_consistency.py` 里躺着 `[tool.mypy]` 配置却从不运行，
# 而"从不运行的类型检查"比没有更糟（看起来有兜底，其实没有）。跑起来之后第一类报错就是
# 这里 —— 28 处注解写的是 `sqlite3.Connection`，实际传进来的却不是（代码审查报告（第二轮）F2）。
SqlConnection = sqlite3.Connection | ThreadLocalConnection


def core_schema_path() -> Path:
    return PACKAGE_ROOT / "core" / "schema.sql"


def roles_schema_path() -> Path:
    return PACKAGE_ROOT / "roles" / "schema.sql"


def domain_schema_path(domain_id: str) -> Path:
    if not _DOMAIN_ID.match(domain_id):
        raise ValueError(f"invalid domain id: {domain_id!r}")
    return PACKAGE_ROOT / "domains" / domain_id / "schema.sql"


def schema_files(enabled_domains: Iterable[str] = ()) -> list[Path]:
    """Return the schema files to apply, in the fixed order documented above."""
    files = [core_schema_path(), roles_schema_path()]
    files.extend(domain_schema_path(d) for d in enabled_domains)
    return files


#: schema 代际（`R102-54`）。每次"形状变了、旧代码读新库会炸"的迁移合并进来时 +1；
#: bootstrap 写进 `PRAGMA user_version`，发现**库比代码新**就拒启 —— 装包回滚是本仓的
#: 真实操作，没有这一格，旧代码读到新库的报错是散落启动链各处的 `no such column`。
SCHEMA_VERSION = 1


def _migrate_event(message: str) -> None:
    """迁移事件落 stderr（`R102-64`）：backend.log 由壳转发 stderr，迁移是三类关键路径里
    **唯一没有可追溯事件**的一类 —— R102-27 那种"半路死"发生时只有异常栈可查。排查判据
    要进事件流，不进注释（`R102-42` 同族的另一半）。"""
    from datetime import datetime

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"[schema-migrate] {stamp} {message}", file=sys.stderr, flush=True)


def _check_schema_generation(conn: SqlConnection) -> None:
    """库的代际比对（`R102-54`）：新库给旧代码 = 一句 loud 拒启，而不是散落的炸点。

    只在这里**拒新**；把 user_version 推到当前值的动作移到了 bootstrap 的最后
    （2026-10-04 审查快照的迁移事务条目）：从前戳在迁移执行**之前**，中途崩掉就留下
    一个"名义代际是新的、形状还是旧的"的库 —— `_repair_stranded_rebuild` 只自愈
    三张已知暂存表，盖不住其它半途形状。
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库名义代际（user_version={version}）比当前代码（{SCHEMA_VERSION}）新 —— "
            "这通常是装包回滚后用旧程序读新库。请装回新版，或从备份恢复数据根。"
        )


def _repair_stranded_rebuild(conn: SqlConnection, *, original: str, temp: str) -> None:
    """整表重建"死在 DROP 原表之后、RENAME 之前"的现场自愈（`R102-53`）。

    判据是库的形状：原表不在、暂存表在 ⇒ 有数据就改名回原表（旧形状数据原样回来，
    后面的"判老形态→重建"整个重跑一遍，数据一分不丢），是空的就直接 DROP（那只是
    死在 INSERT 之前的老窗口，DROP IF EXISTS 已自愈过它）。两种现场都不许静默留着
    —— 滞留的暂存表是一本**永远没人读**的账本，而 bootstrap 重跑不但不报错还照常绿。
    """
    tables = {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    if original in tables or temp not in tables:
        return
    temp_rows = int(conn.execute(f"SELECT COUNT(*) FROM {temp}").fetchone()[0])
    if temp_rows:
        conn.execute(f"ALTER TABLE {temp} RENAME TO {original}")
        _migrate_event(
            f"检测到滞留暂存表（原表 {original} 不在、{temp} 有 {temp_rows} 行）—— "
            "已改名回原表，本次迁移整个重跑（R102-53 的第三种死法现场）"
        )
    else:
        conn.execute(f"DROP TABLE {temp}")
        _migrate_event(f"清掉空的滞留暂存表 {temp}（R28-15 的老窗口现场）")
    conn.commit()


#: 删之前那一行的落盘目录（相对数据根）与保留份数 —— 备份目录自己不许变成新的只增表。
RETENTION_BACKUP_DIRNAME = "retention-backups"
RETENTION_BACKUP_KEEP = 5


def _dump_before_delete(
    conn: SqlConnection,
    *,
    table: str,
    where: str,
    params: tuple[object, ...],
    backup_dir: Path,
    stamp: str,
) -> int:
    """把**将要被删的行**先写成 JSONL，返回行数。0 行就不落文件。

    顺序是判据：先落盘、再删、最后才 commit —— 中间崩掉的结果是"行还在库里 + 多一个
    备份文件"，而不是"行没了 + 没有任何地方能找回"。这是 `R102-29` 拍板里
    "先备份再删"那半句的实现（10-03 复核发现那半句从没落地，见台账 H12 批 24）。
    """
    rows = conn.execute(f"SELECT * FROM {table} {where}", params).fetchall()  # noqa: S608
    if not rows:
        return 0
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"{table}-{stamp}.jsonl"
    with out.open("a", encoding="utf-8") as fh:
        for row in rows:
            record = dict(zip(row.keys(), tuple(row), strict=True))
            fh.write(json.dumps(record, ensure_ascii=False, default=str))
            fh.write("\n")
    print(
        f"[retention] 先落备份 {out.name}（{len(rows)} 行 {table}）再删",
        file=sys.stderr,
        flush=True,
    )
    return len(rows)


def _trim_backups(backup_dir: Path) -> None:
    """每张表各留最近 `RETENTION_BACKUP_KEEP` 份 —— 防"为了不留只增表而造出另一张只增表"。

    分表裁而不是整目录一起裁：文件名是 `<表名>-<时刻>.jsonl`，字典序里 `command_approval-*`
    永远压在 `audit_log-*` 前面 —— 整目录裁会让一批审批备份把审计备份**饿死**（删掉的恰是
    唯一那份能找回审计行的文件）。时刻是秒级 `YYYYmmdd-HHMMSS`，同表内按名排序=按时间排序。
    """
    by_table: dict[str, list[Path]] = {}
    for f in backup_dir.glob("*.jsonl"):
        by_table.setdefault(f.name.split("-", 1)[0], []).append(f)
    for files in by_table.values():
        for stale in sorted(files, key=lambda p: p.name, reverse=True)[RETENTION_BACKUP_KEEP:]:
            stale.unlink(missing_ok=True)


def prune_retention_tables(
    conn: SqlConnection,
    *,
    audit_log_days: int,
    audit_log_max_rows: int,
    approval_done_days: int,
    backup_dir: Path,
) -> dict[str, int]:
    """三张只增表的 retention 清理（`R102-29`；2026-10-02 拍板：分表定档）。

    `audit_log` 留 `audit_log_days` 天、且至多 `audit_log_max_rows` 行（两条判据任一命中
    即清）；`command_approval` 的**终态**行留 `approval_done_days` 天 —— pending/approved
    是活队列，retention 永不碰（卡死的行由 `R102-47` 的兜底与开机清扫负责，那是状态机的
    职责不是保留策略的）；`agent_reachout` 走 per-role `reachout_keep` 既有机制（出厂默认
    200 只对新角色生效）。0 或负数 = 该档永不清理（旧行为）。
    调用点在 `core/bootstrap.py`（拿得到 Settings 的地方），清理量回报 schema-migrate 事件流。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    pruned: dict[str, int] = {}
    where: str
    params: tuple[object, ...]
    if audit_log_days > 0:
        where, params = "WHERE ts < datetime('now', ?)", (f"-{audit_log_days} days",)
        _dump_before_delete(
            conn, table="audit_log", where=where, params=params, backup_dir=backup_dir, stamp=stamp
        )
        cur = conn.execute(f"DELETE FROM audit_log {where}", params)
        pruned["audit_log_by_days"] = max(cur.rowcount, 0)
    if audit_log_max_rows > 0:
        where = "WHERE id NOT IN (SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)"
        params = (audit_log_max_rows,)
        _dump_before_delete(
            conn, table="audit_log", where=where, params=params, backup_dir=backup_dir, stamp=stamp
        )
        cur = conn.execute(f"DELETE FROM audit_log {where}", params)
        pruned["audit_log_by_rows"] = max(cur.rowcount, 0)
    if approval_done_days > 0:
        where = (
            "WHERE status IN ('done', 'rejected') AND updated_at < datetime('now', ?)"
        )
        params = (f"-{approval_done_days} days",)
        _dump_before_delete(
            conn,
            table="command_approval",
            where=where,
            params=params,
            backup_dir=backup_dir,
            stamp=stamp,
        )
        cur = conn.execute(f"DELETE FROM command_approval {where}", params)
        pruned["approvals_done"] = max(cur.rowcount, 0)
    conn.commit()
    if any(pruned.values()):
        _trim_backups(backup_dir)
    return pruned


def bootstrap(
    conn: SqlConnection,
    enabled_domains: Iterable[str] = (),
    *,
    provider_layers: Callable[[SqlConnection], int] | None = None,
) -> list[str]:
    """Apply every schema file. Idempotent - all DDL uses IF NOT EXISTS.

    Returns the applied file names, which is what tests assert on: a silently skipped
    schema is far worse than a loud failure.
    """
    # 代际戳（`R102-54`）：库的版本住在 `PRAGMA user_version` 里。库比代码新 = 装包
    # 回滚后旧代码读新库 —— 两层的 `model_backend` 没有 `api_key/usage` 列，旧读法的
    # `no such column` 会散落在启动链各处；这里一句 loud 报错把它并成一处。
    _check_schema_generation(conn)
    files = schema_files(enabled_domains)
    # 先把"声明里有、这份老库里没有"的列补上，再跑 DDL：域表的 `CREATE INDEX ... (新列)`
    # 排在任何迁移之前执行，老库踩它必炸（09-26 轮 R26-04 的实测现场）。
    reconcile_columns(conn, files=files)
    # 建部分唯一索引**之前**先清重（`R102-52`）：老库里若已躺着同命令的多条 pending
    # （并发 submit 的历史遗留），schema.sql 里那条 UNIQUE INDEX 会当场建失败 ——
    # 那是"库再也打不开"（R28-15）的同一条死法，不能让一份遗留数据把开机堵死。
    _dedupe_pending_approvals(conn)
    # 同族前置（2026-10-04 审查快照的上传幂等条目）：老库的重复台账会让下面的
    # 唯一索引当场建失败。
    _dedupe_ingestion_tasks(conn)
    # 整表重建的滞留自愈（`R102-53`）**必须站在 schema DDL 之前**：executescript 的
    # `CREATE TABLE IF NOT EXISTS` 会把被 DROP 掉的原表先建成一张空的新形表，随后
    # `_migrate` 的"判老形态"永远为假 —— 滞留暂存表就永远没人管、还照常报绿。
    # 三张暂存表对应 db.py 的 B2/B1a 两处整表重建与 model_settings 的搬层。
    _repair_stranded_rebuild(
        conn, original="role_proactive_state", temp="role_proactive_state__b2"
    )
    _repair_stranded_rebuild(conn, original="token_usage_day", temp="token_usage_day_new")
    _repair_stranded_rebuild(conn, original="model_backend", temp="model_backend__layers")
    applied: list[str] = []
    for path in files:
        if not path.exists():
            raise FileNotFoundError(f"schema file missing: {path}")
        conn.executescript(path.read_text(encoding="utf-8"))
        applied.append(str(path.relative_to(PACKAGE_ROOT)))
    _migrate(conn, provider_layers=provider_layers)
    # 第二遍：`_migrate` 里那些 DROP/重建（service_endpoint 整表、model_backend 搬层）
    # 会把第一遍补好的列跟着旧表一起带走，所以搬层之后再对齐一次声明。
    # 这一遍**不再跳过**那三张手形迁移的表（`R28-21`）。跳过只对**第一遍**是必要的：那里
    # 补出 `provider_id` 会让"没有 provider_id 就是旧形态"的判定当场失效，搬层被静默跳过。
    # 而此刻迁移已经做完、形状已经是新的 —— 继续跳过等于给这三张表判了"今后声明的新列永远
    # 补不上"，症状要等到某个老库升上来才现形（台账原判：埋点不是事故，但它是**只会更贵**
    # 那种埋点）。真遇到"这列必须回填数据"的情况，补列器会抛那句
    # 「NOT NULL 又没默认值 ⇒ 必须走整表重建」并点名 `_SHAPE_MIGRATED_TABLES`，
    # 那正是想要的大声失败，而不是静默什么都不做。
    reconcile_columns(conn, files=files, skip=frozenset())
    conn.commit()
    # 代际戳在**迁移全部完成后**才推进（2026-10-04 审查快照的迁移事务条目）：中途崩掉
    # 留下的是"user_version 还是旧的"，下一次启动会原样重跑一遍幂等 bootstrap —— 而不是
    # 一个"名义代际已新、形状未完成"的假新库。
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        _migrate_event(f"user_version 戳到 {SCHEMA_VERSION}（bootstrap 全部完成后）")
    return applied


#: 这几张表的旧形态由 `_migrate` 整表重建 / 搬层负责，通用补列器**必须避开**它们：
#: 提前给旧形 `model_backend` 补上 `provider_id`，`_migrate` 里那句"没有 provider_id
#: 就是旧形态"的判定当场失效 —— 搬层被跳过，旧行的凭据静静留在没人再读的列里。
#: `token_usage_day` 不是列的问题而是**主键**（B1a）：补列改不了 `(day, backend)` → 
#: `(day, user_id, backend)`，只补列会让 `ON CONFLICT(day,user_id,backend)` 永远报
#: "non-unique" —— 所以它同理要整表重建。
#: `service_endpoint` 需要**补列 + 按行回填**两步（B1b：`user_id` 加列由 `_migrate`
#: 手写、老 chat 引用行回填本机主人）。补列器只管加列、不负责回填数据 —— 让它自动补了
#: 列，旧 chat 行就是 NULL，按人过滤后对话默认当场消失；把两步放在 `_migrate` 同一个
#: 判断里才是一次完整的迁移。
_SHAPE_MIGRATED_TABLES = frozenset({"model_backend", "service_endpoint", "token_usage_day"})


def quote_ident(ident: object) -> str:
    """双引号包住标识符（SQLite 的标准引用法），内嵌的双引号按 SQL 规则翻倍。"""
    return '"' + str(ident).replace('"', '""') + '"'


def _declared_columns(files: Sequence[Path]) -> dict[str, dict[str, sqlite3.Row]]:
    """当前这些 schema 文件**声明**出来的列形状：`{表: {列: PRAGMA 那一行}}`。

    不去解析 SQL 文本 —— sqlite 自己就是那台解析器，再造一个解析器只会多一处会不同步的
    事实面（这正是本模块一直在抓的那个根因）。
    """
    probe = sqlite3.connect(":memory:")
    probe.row_factory = sqlite3.Row
    try:
        for path in files:
            probe.executescript(path.read_text(encoding="utf-8"))
        tables = [
            str(r[0])
            for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            t: {str(r["name"]): r for r in probe.execute(f"PRAGMA table_info({quote_ident(t)})")}
            for t in tables
        }
    finally:
        probe.close()


def _add_column_ddl(table: str, decl: sqlite3.Row) -> str:
    name = str(decl["name"])
    if decl["notnull"] and decl["dflt_value"] is None:
        raise RuntimeError(
            f"{table}.{name} 声明成 NOT NULL 又没有默认值，SQLite 不允许 ADD COLUMN 补它。"
            "这种列必须走整表重建：把它加进 `_SHAPE_MIGRATED_TABLES` 并在 `_migrate` 里写一次。"
        )
    spec = f"{quote_ident(name)} {decl['type'] or 'TEXT'}"
    if decl["notnull"]:
        spec += " NOT NULL"
    if decl["dflt_value"] is not None:
        spec += f" DEFAULT {decl['dflt_value']}"
    return f"ALTER TABLE {quote_ident(table)} ADD COLUMN {spec}"


def reconcile_columns(
    conn: SqlConnection,
    *,
    files: Sequence[Path] | None = None,
    skip: frozenset[str] = _SHAPE_MIGRATED_TABLES,
) -> list[str]:
    """把"schema 里声明了、这份库里却没有"的列补齐，返回补过的 `表.列` 清单。

    为什么要有这一层（09-26 轮 R26-04）：`_migrate` 原先那 12 处 ALTER 一条条手写，
    于是"加一列"的人必须记得来这儿再写一遍 —— **忘了不会红**，只会在第一次读那一列时炸。
    两份历史形状当时合计缺 19 列，全都能自动补（无一例 NOT NULL 无默认；09-27 的 M2a 给
    `role_card` 加了 `user_id` 之后这组数变成 23 —— 它本来就该随 schema 长，钉住它、逼改 schema
    的人回来看一眼这行注释的用例是 `test_audited_shortfall_is_still_the_shortfall`）。这一层把
    "记不记得"换成"声明即事实"：以后加列只改 `schema.sql` 一处。

    只对**已存在的表**补列：表整个不存在 = 那份 DDL 自己会建，不该在这儿猜形状。
    主键列不参与（形状根本不同，那是 `_migrate` 整表重建的事）。
    """
    targets = list(files) if files is not None else schema_files()
    declared = _declared_columns(targets)
    existing = {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    added: list[str] = []
    for table, cols in declared.items():
        if table in skip or table not in existing:
            continue
        have = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({quote_ident(table)})")}
        for name, decl in cols.items():
            if name in have or decl["pk"]:
                continue
            conn.execute(_add_column_ddl(table, decl))
            added.append(f"{table}.{name}")
    return added


def _columns(conn: SqlConnection, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


#: 重命名一条线程时要跟着改的表：**现数**，不写死清单（`R28-23`）。
#: 写死的那个版本列了 `session_thread` / `checkpoints` / `writes` 三张，漏了
#: `command_approval.thread_id` —— 挂旧 id 的审批行会指向一条不存在的会话（点进去是空的，
#: 而它自己还挂着 `decide_token`）。这类漏法不会因为"这次补上这一张"而消失：下一张带
#: `thread_id` 的表照样被忘。所以判据交给库本身：凡是**有 `thread_id` 列又不是
#: `session_thread` 自己**的表，都跟着改。langgraph 那两张（checkpoints / writes）本来就
#: 在这个集合里，原来那句"表不存在就不动"的 `has_cp` 特判因此也不需要了 —— 不存在的表
#: 根本进不了清单。
def thread_id_carriers(conn: SqlConnection) -> list[str]:
    tables = [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return [
        t for t in sorted(tables) if t != "session_thread" and "thread_id" in _columns(conn, t)
    ]


def _dedupe_pending_approvals(conn: SqlConnection) -> int:
    """建 `idx_command_approval_one_pending` 之前的清重（`R102-52`）。

    老库里若已躺着同命令的多条 pending（并发 submit 的历史遗留），那条部分唯一索引
    会当场建失败 = "库再也打不开"（R28-15 的同一条死法）。保留**最新**一条 pending
    （操作员的视线在那上头），其余收成 rejected 并注明理由 —— 幂等：无重复时零改动。
    """
    # 新库此时还没有这张表（schema 在后面才跑）—— 只有"表已在的 legacy 库"才需要清重。
    exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'command_approval'"
    ).fetchone()[0]
    if not exists:
        return 0
    cur = conn.execute(
        "UPDATE command_approval SET status = 'rejected', result_json = ?, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE status = 'pending' AND id NOT IN ("
        "  SELECT MAX(id) FROM command_approval WHERE status = 'pending' GROUP BY command)",
        (
            json.dumps(
                {"error": "并发提交产生的重复待批：同命令只保留最新一条，其余并成拒绝"},
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()
    return max(cur.rowcount, 0)


def _dedupe_ingestion_tasks(conn: SqlConnection) -> int:
    """建 `idx_ingestion_user_file` 之前的清重（2026-10-04 审查快照的上传幂等条目）。

    老库的 `ingestion_task` 若在拿到唯一索引前躺了同 (user_id, file_hash) 的重复行，
    建索引会当场失败 = 库打不开（R28-15 同族）。台账语义 = "同一份字节一个任务"，
    所以保留 `updated_at` 最新的一条、**删掉**其余（与审批清重的"收成 rejected"不同：
    这里的索引不带 WHERE，任何状态的重复行都违例；而被删的都是同字节的旧影子，
    最新那条承载全部语义）。幂等：无重复时零改动。
    """
    exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'ingestion_task'"
    ).fetchone()[0]
    if not exists:
        return 0
    has_index = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index'"
        " AND name = 'idx_ingestion_user_file'"
    ).fetchone()[0]
    if has_index:
        return 0  # 索引已在：库里不可能再有重复行，扫描是白费
    cur = conn.execute(
        "DELETE FROM ingestion_task WHERE rowid NOT IN ("
        "  SELECT rowid FROM ingestion_task i"
        "  WHERE i.rowid = (SELECT x.rowid FROM ingestion_task x"
        "                   WHERE x.user_id = i.user_id AND x.file_hash = i.file_hash"
        "                   ORDER BY x.updated_at DESC, x.rowid DESC LIMIT 1))"
    )
    conn.commit()
    removed = max(cur.rowcount, 0)
    if removed:
        _migrate_event(f"清掉重复的 ingestion_task {removed} 行（保留每组最新一条）")
    return removed


def _needs_provider_layers(conn: SqlConnection) -> bool:
    """这库里有一张旧形态的 model_backend 等着搬吗？

    判据与 `migrate_to_provider_layers` 自己的第一句完全同一把尺（列清单里没有
    `provider_id` 就是旧形态），所以两边永远不会对『要不要搬』给出不同答案 ——
    两处判断不同步就是「该搬的没搬、不该搬的搬了」的那种错。
    """
    cols = _columns(conn, "model_backend")
    return bool(cols) and "provider_id" not in cols


def _migrate(
    conn: SqlConnection, *, provider_layers: Callable[[SqlConnection], int] | None = None
) -> None:
    """**形状**迁移：只负责"通用补列器补不了"的那些事（幂等、可重跑）。

    列级缺列不再手写 ALTER —— `reconcile_columns` 每次都按 `schema.sql` 的声明比对
    （09-26 轮 R26-04：原先 12 处手写 ALTER 全在核心表上，加列忘了写**不会红**，
    只会在第一次读那列时炸）。这里只剩下三类真正需要人写的事：

    1. service_policy 已退役（策略并入 service_endpoint 行内 enabled/sort_order）→ DROP。
    2. service_endpoint 旧形态（行内嵌 key/base_url 的"实例"模型）→ 整表重建为
       「引用 model_backend」的新形态；旧行配置属演示数据且引用化后由模型页承接，
       直接弃用。**必须连 seed flag 一起清**，否则 seed_once 会以为播过种而跳过，
       留下一张空表（实测踩过：引用行全部缺失）。
    3. model_backend 旧形态（一张表混装供应商凭据与模型，每行自带 provider/base_url/
       api_key/usage）→ 先补齐历史列，再由 `model_settings.migrate_to_provider_layers`
       搬进两层表（凭据上收到 model_provider、用途变成 service_endpoint 的 chat 引用）。
       补列必须发生在搬层**之前**（搬层要读这些列），且必须限定"这是旧形态"才补 ——
       新库里 model_backend 已经没有 usage 列，无条件补一次就是把它加回来。
       这两张表都在 `_SHAPE_MIGRATED_TABLES` 里，通用补列器对它们不动手，正是为了
       不让"提前补上 provider_id"把第 3 条的形态判定当场弄失效。
    """
    cols = _columns(conn, "model_backend")
    if cols and "provider_id" not in cols:
        # 旧形态库：先把历史缺列补齐（这些列曾经分三次 ALTER 加过），再交给搬层迁移。
        for name, ddl in {
            "usage": "TEXT NOT NULL DEFAULT 'chat'",
            "num_ctx": "INTEGER",
            # 能力位**必须留 NULL**：NULL 是"没测过"，而 `schema.sql` 与界面（`?`）都按这个
            # 口径走。旧写法回填成 `NOT NULL DEFAULT 0/1`，等于替每个升级上来的用户回答了两
            # 个没人问过的问题 —— 界面上从此显示"✗ 不支持视觉 / ✓ 支持工具"，看着像测过。
            # 运行时不受影响（`_vision_of(NULL)=False`、`_tools_of(NULL)=True` 与旧默认同值），
            # 所以这是一次纯"别撒谎"的修正。（09-26 把本轮猜测区那条量实之后改的。）
            "supports_vision": "INTEGER",
            "supports_tools": "INTEGER",
        }.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE model_backend ADD COLUMN {name} {ddl}")
    # 原先这里挂着 8 组手写 ALTER（session_thread 的 agent_mode/distilled_at_seq、
    # role_card 的五个开关与 reachout_keep、role_memory_item.importance、
    # command_approval.decide_token）—— 全部由 `reconcile_columns` 按声明补齐，
    # `bootstrap` 在跑 DDL 前后各调一次，语义与那些 `if 缺则 ADD` 逐字相同
    # （列的 type/NOT NULL/DEFAULT 直接取自 `schema.sql`，见 R26-04）。
    # 7. 关系驱动主动开口（架构总览 §5）：per-role 状态与 per-role 记忆（幂等建表）。
    #    这张表的列仍由 reconcile_columns 补（不在 _SHAPE_MIGRATED_TABLES 里），只有
    #    **主键换 (user_id, role_id)** 是形状迁移，走下面 B2 的重建。
    if "affinity" not in _columns(conn, "role_proactive_state"):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS role_proactive_state ("
            " role_id TEXT PRIMARY KEY, affinity REAL NOT NULL DEFAULT 0.0,"
            " last_interaction_utc TIMESTAMP, calibration_json TEXT,"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    # 14. role_proactive_state 的主键升级（多租户 B2）：(role_id) → (user_id, role_id)。
    #     `role_card` 的主键将来要改成 (user_id, role_id)（第二个身份也可能建一张同名卡），
    #     状态表不跟着换就撞行。整表重建 + 老行归属实例主人（默认部署 = 'local-user'，
    #     与 base/identity.DEFAULT_USER_ID 一字不差）。判"老形态"用有没有 user_id 列。
    #     顺带把**主动会话线程 id 改成带身份**（s_proactive_<role> → s_proactive_<uid>_<role>）：
    #     同一个 role_id 将来可以属于两个人，线程 id 不带身份就会让两人的主动会话互相覆盖。
    #     老线程的归属从 session_thread.user_id 现读（数据即真相，不必知道 IDENTITY_USER_ID）；
    #     checkpoints/writes 的 thread_id 一起改（那是她主动说过的历史，不改就断了上下文）。
    #     幂等：新 id == 旧 id（已带身份）的不动；新库没有 legacy 行一轮跑过。这两件是本步
    #「换主键 + 换线程 id」两笔账，放在同一个 if 里是为了一次迁移只判断一次"是不是 B2 之前的库"。
    if "user_id" not in _columns(conn, "role_proactive_state"):
        # 滞留暂存表的现场自愈在 bootstrap 早期已经做过（那里才赶得在 schema DDL 前面）。
        # 前置 DROP：与上面 B1 那一处同一个理由（`R28-15`，红档）。这一步死在半路的话，
        # 残留的空 `__b2` 会让下次启动的 CREATE 报 `already exists`，bootstrap 永久打不开。
        # 整段重建（含下面的线程 id 换名）包进**一个显式事务**：DDL 在 legacy autocommit
        # 下逐句落盘，显式 BEGIN 让"半路死"整体回滚 —— DROP→RENAME 之间的窗口随事务消失。
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        _migrate_event("role_proactive_state 主键重建开始（B2，显式事务）")
        conn.execute("DROP TABLE IF EXISTS role_proactive_state__b2")
        conn.execute(
            "CREATE TABLE role_proactive_state__b2 ("
            " user_id TEXT NOT NULL DEFAULT 'local-user', role_id TEXT NOT NULL,"
            " affinity REAL NOT NULL DEFAULT 0.0, last_interaction_utc TIMESTAMP,"
            " calibration_json TEXT, open_threads TEXT, open_threads_at TIMESTAMP,"
            " recall_at TIMESTAMP,"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " PRIMARY KEY (user_id, role_id))"
        )
        conn.execute(
            "INSERT INTO role_proactive_state__b2 (user_id, role_id, affinity,"
            " last_interaction_utc, calibration_json, open_threads, open_threads_at, recall_at,"
            " updated_at)"
            " SELECT 'local-user', role_id, affinity, last_interaction_utc, calibration_json,"
            " open_threads, open_threads_at, recall_at, updated_at FROM role_proactive_state"
        )
        conn.execute("DROP TABLE role_proactive_state")
        conn.execute("ALTER TABLE role_proactive_state__b2 RENAME TO role_proactive_state")
        refs = thread_id_carriers(conn)
        for row in conn.execute(
            "SELECT thread_id, user_id FROM session_thread "
            "WHERE thread_id LIKE 's_proactive_%'"
        ).fetchall():
            tid = str(row["thread_id"])
            uid = str(row["user_id"])
            if tid.startswith(f"s_proactive_{uid}_"):
                continue  # 已带身份（本步跑过 / 新库建的）
            new_tid = f"s_proactive_{uid}_{tid[len('s_proactive_'):]}"
            conn.execute(
                "UPDATE session_thread SET thread_id = ? WHERE thread_id = ?",
                (new_tid, tid),
            )
            # 带 thread_id 的表全跟着走（`R28-23`）：审批、检查点、writes，以及将来任何新表
            # —— 判据是库的形状，不是这段代码记得列了几张。
            for table in refs:
                conn.execute(
                    f"UPDATE {quote_ident(table)} SET thread_id = ? WHERE thread_id = ?",
                    (new_tid, tid),
                )
        conn.commit()  # 重建原子落盘（`R102-53`）
        _migrate_event("role_proactive_state 主键重建完成（含线程 id 换名）")
    if "value" not in _columns(conn, "role_memory"):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS role_memory ("
            " role_id TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '',"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    # 8. role_memory_item（记忆条目表）：记忆从"一坨文本"升级为可逐条退役的条目。
    #    老库里的 role_memory / memory:facts 两块 blob **不迁移**（用户 2026-09-20："旧的记忆
    #    数据也可以不要了"）—— 旧表原样留着不删列（迁移纪律），只是不再是事实面。
    if "text" not in _columns(conn, "role_memory_item"):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS role_memory_item ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, role_id TEXT NOT NULL,"
            " text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',"
            " pinned INTEGER NOT NULL DEFAULT 0, hit_count INTEGER NOT NULL DEFAULT 0,"
            " last_hit_at TIMESTAMP, invalidated_at TIMESTAMP, superseded_by INTEGER,"
            " created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_role_memory_item_bucket ON role_memory_item"
            "(role_id, invalidated_at, pinned, id DESC)"
        )
    conn.execute("DROP TABLE IF EXISTS service_policy")
    # 4. token_usage_day 的主键升级（多租户 B1a）：(day, backend) → (day, user_id, backend)。
    #    补列改不了主键，也没有"PRAGMA 改 PK"这回事 —— 只能整表重建。旧行全部归属本机那份
    #    （语义 = 上线前测的本机用量，不是谁漏账）；新行由 `usage.record_usage(user_id=…)` 按
    #    花谁的 key 落格。判断"旧形态"用有没有 user_id 列（与列级迁移同口径）。
    if "user_id" not in _columns(conn, "token_usage_day"):
        # 前置 DROP（R28-15 同族）+ 显式事务包整段重建（`R102-53`）：
        # "半路死"要么整体回滚、要么在下一次开机被早期的自愈接住。
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        _migrate_event("token_usage_day 主键重建开始（B1a，显式事务）")
        conn.execute("DROP TABLE IF EXISTS token_usage_day_new")
        conn.execute(
            "CREATE TABLE token_usage_day_new ("
            " day TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'local-user',"
            " backend TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,"
            " prompt_tokens INTEGER NOT NULL DEFAULT 0,"
            " completion_tokens INTEGER NOT NULL DEFAULT 0,"
            " reasoning_tokens INTEGER NOT NULL DEFAULT 0,"
            " unreported INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (day, user_id, backend))"
        )
        conn.execute(
            "INSERT INTO token_usage_day_new (day, user_id, backend, calls, prompt_tokens,"
            " completion_tokens, reasoning_tokens, unreported)"
            " SELECT day, 'local-user', backend, calls, prompt_tokens, completion_tokens,"
            " reasoning_tokens, unreported FROM token_usage_day"
        )
        conn.execute("DROP TABLE token_usage_day")
        conn.execute("ALTER TABLE token_usage_day_new RENAME TO token_usage_day")
        conn.commit()  # 重建原子落盘（`R102-53`）
        _migrate_event("token_usage_day 主键重建完成")
    if "api_key" in _columns(conn, "service_endpoint"):
        # 重建段包进**单个事务**（2026-10-04 审查快照的迁移事务条目；B1a :790 同款）：
        # 从前 DROP 与 executescript 分属两个隐式事务，死在中间 = 下次启动靠
        # IF NOT EXISTS 重建出**空表**、seed 标志已删照常重播 —— 端点配置静默清空，
        # 无人被提示。executescript 会先隐式 COMMIT，所以把 DROP/清标志与整份 core schema
        # 拼成一段脚本、首尾 BEGIN IMMEDIATE/COMMIT 包死（schema.sql 无 TRIGGER/PRAGMA，
        # 逐条语句在 SQLite 里本就事务化）。崩掉要么整体回滚（配置还在）、要么全成。
        core = core_schema_path()
        conn.executescript(
            "BEGIN IMMEDIATE;"
            "DROP TABLE service_endpoint;"
            "DELETE FROM kernel_meta WHERE key = 'service_endpoints_seeded';"
            + core.read_text(encoding="utf-8")
            + ";COMMIT;"
        )
        _migrate_event("service_endpoint 整表重建完成（单事务，配置零丢失）")
    # 13. service_endpoint 的归属（多租户 B1b，方案 A）：加可空 user_id。
    #     补列器不碰这张表（它在 `_SHAPE_MIGRATED_TABLES` 里，整表重建/搬层族），所以这里手写
    #     一次。只对新形态库生效 —— 老形态（带 api_key）走上面那句 DROP 重建，新表已带列。
    #     语义（schema.sql 有全文）：**仅 chat 引用行按人**（默认/回退链 = 谁花 key 由谁定），
    #     能力端点（ocr/embedding/rerank）永远设备级、user_id 留 NULL。老 chat 行回填本机主人
    #     （默认部署 = 'local-user'，与 `base/identity.DEFAULT_USER_ID` 一字不差 —— 漂了就是
    #     "升级完对话默认丢失"那种最像默认值出问题的症状）。新 chat 行由 model_settings 的写
    #     入方显式带主人，所以这里只回填历史行。
    if "user_id" not in _columns(conn, "service_endpoint"):
        conn.execute("ALTER TABLE service_endpoint ADD COLUMN user_id TEXT")
        conn.execute(
            "UPDATE service_endpoint SET user_id = 'local-user' WHERE category = 'chat'"
        )
    # 9. model_backend 两层化（凭据上收 model_provider、usage 变成 chat 引用行）。
    #    必须排在 service_endpoint 重建**之后**：搬层要往新形态的引用表里写 chat 行。
    # 搬层这一步**由调用方交进来**（`R102-08`：从前这里是 `storage/db.py` 惰性
    # import `core.model_settings`，而 `model_settings` 顶层 import `storage.db` ——
    # 全仓唯一一条模块级真环，底层反向依赖上层）。它必须留在 `_migrate` 这个位置上：
    # 排在 service_endpoint 重建**之后**、`idx_model_backend_provider` 创建**之前**
    # —— 搬层会 DROP/RENAME 重建 model_backend，先建的索引跟着表一起没了。顺序是承重的，
    # 所以不是"调用方在 bootstrap 之后自己搬一次"，而是把那个动作传到这里来。
    #
    # 旧库却没人传 = 静默不搬 = 配置变小，所以这里判一下现场：真需要搬而没交动作，
    # 就当众失败，而不是留下一个"读出来比升级前少"的库。
    if _needs_provider_layers(conn):
        if provider_layers is None:
            raise RuntimeError(
                "model_backend 还是旧形态（没有 provider_id），需要搬层却没交 "
                "provider_layers：走 core/bootstrap.py 那条链，或显式传 "
                "provider_layers=rolecard_agent.core.model_settings"
                ".migrate_to_provider_layers。"
            )
        provider_layers(conn)
    # 索引在搬层之后建：搬层会 DROP/RENAME 重建 model_backend，先建的索引跟着表一起没了。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_backend_provider ON model_backend"
        "(provider_id, sort_order, name)"
    )
    # 10. model_backend 增列采样惩罚三档（设计稿 §8.2 的补课：只露过 num_ctx/temperature）。
    #     排在搬层**之后**：搬层会重建这张表，先补的列跟着旧表一起没了（与上面那条索引同理）。
    #     三档都可空，NULL = 不传该参数 = 引擎默认（Ollama 出厂 repeat_penalty=1.1，
    #     写成 0 是"把它关了"，与"没设"是两种行为 —— 所以这里不用 DEFAULT 0）。
    for name in ("repeat_penalty", "frequency_penalty", "presence_penalty"):
        if name not in _columns(conn, "model_backend"):
            conn.execute(f"ALTER TABLE model_backend ADD COLUMN {name} REAL")
    # 11.「未收尾话题」那两列（open_threads / open_threads_at）原先也手写在这里，现在由
    #     `bootstrap` 补搬层**之后**的那一遍 `reconcile_columns` 按声明补齐。
    # 12. 记忆条目的跨机器身份 `uid`（09-27 轮 M2b）。补列器能把**列**长出来，但填什么值是
    #     数据不是形状 —— 老行的 uid 必须在这里补上 uuid4，否则"哪些条目能跟云端对账"这件事
    #     没有答案，上行只剩"整表覆盖"那一条会丢数据的路。幂等：只碰 NULL/空串。
    if "uid" in _columns(conn, "role_memory_item"):
        rows = conn.execute(
            "SELECT id FROM role_memory_item WHERE uid IS NULL OR uid = ''"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE role_memory_item SET uid = ? WHERE id = ?",
                (uuid.uuid4().hex, int(r[0])),
            )
        conn.commit()
    # 14. OCR 引擎换家（10-02：Paddle → RapidOCR）：内置行的 **id 就是事实面**。
    #     `select_ocr_backend` 按 id 匹配候选、`check_availability` 按 id 分派探活、服务页把 id
    #     当标签来源 —— 旧库里那行 `paddle` 不改名，症状不是报错而是**静默降级**：服务页显示
    #     "本地 OCR 就绪"，选择器却永远匹配不上任何候选，`order` 里剩下的都是不可用的行，
    #     图片一路停在 pending，看起来像"这张图没识别出来"（与 rag/ocr.py 里"空 order 不抛"
    #     那条同一种阴）。
    #     幂等：只在真存在 `paddle` 行时动手。两种终态都对 —— 已有 `rapidocr` 行（新库、或
    #     播种已重播过）就删旧行，绝不撞 (category, id) 主键；没有就把旧行**改名带过去**，
    #     enabled / sort_order / user_id 一字不动：操作员在这行上做过的启停与排序，不该因为
    #     换了个引擎就被重置。
    has_old = conn.execute(
        "SELECT 1 FROM service_endpoint WHERE category = 'ocr' AND id = 'paddle'"
    ).fetchone()
    if has_old:
        if conn.execute(
            "SELECT 1 FROM service_endpoint WHERE category = 'ocr' AND id = 'rapidocr'"
        ).fetchone():
            conn.execute("DELETE FROM service_endpoint WHERE category = 'ocr' AND id = 'paddle'")
        else:
            conn.execute(
                "UPDATE service_endpoint SET id = 'rapidocr'"
                " WHERE category = 'ocr' AND id = 'paddle'"
            )
        conn.commit()
