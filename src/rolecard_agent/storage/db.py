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
只有"形状根本不同"的表才需要一步整表重建 —— 那些是**业务语义**，住
`core/migrations.py` 的注册表里，经 `bootstrap(plan=…)` 交进来：本模块不认识任何业务表名，
也不再有一段 `_migrate`（2026-10-04 审查快照 P1-6 的 split-brain 收口）。**绝不要靠删库来
升级** —— `data/sqlite/app.db` 与安装目录下那份是真实数据，不是演示脚手架（09-26 轮
R26-05 抓到 `CONTRIBUTING.md` 就是这么教的，那条已订正）。
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

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
    try:
        _apply_pragmas(conn)
    except sqlite3.DatabaseError as exc:
        # **坏库的启动诊断**（2026-10-04 快照"零故障注入"那一格的后半）：库文件不是 sqlite
        # 库（被覆盖/截断）、打不开（权限/路径）、被别的进程独占 —— 这三种在**开连接**这一步就
        # 会炸，而从前抛出去的是 `sqlite3.DatabaseError: file is not a database`：一句话，
        # 没说哪个文件、没说什么能做。启动期是唯一还来得及说人话的时刻，这一格就是那句话。
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        raise StorageUnreadable(_connect_hint(target, exc)) from exc
    return conn


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """建连接时必须钉上的那几档 PRAGMA（顺序承重，逐条理由见下）。"""
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


class StorageUnreadable(RuntimeError):
    """库文件在**开连接**那一步就读不出来 —— 启动期唯一还来得及说人话的时刻。

    它不是"某个请求失败了"，是"这台机器上的库现在打不开"：损坏（不是 sqlite 文件）、
    权限/路径不对、被别的进程独占。所以它带一句可操作的话（哪个文件、能做什么），
    而不是把 `sqlite3.DatabaseError` 原样扔给一个正在看控制台的人。
    """


def _connect_hint(target: Path, exc: sqlite3.DatabaseError) -> str:
    """把三类"开不了库"翻成一句能照着做的话（认不出来就照原样带上原文，不猜）。"""
    raw = str(exc) or type(exc).__name__
    lowered = raw.lower()
    if "not a database" in lowered or "malformed" in lowered or "encrypted" in lowered:
        what = (
            "这个文件不是 SQLite 库（被覆盖、截断，或根本不是库文件）——"
            "**先别删它**，原地留证：换一份备份的库接着跑，再慢慢查它是怎么坏的"
        )
    elif "unable to open" in lowered or "readonly" in lowered or "permission" in lowered:
        what = "打不开这个文件 —— 查一下路径是否存在、进程有没有读写权限"
    elif "locked" in lowered or "busy" in lowered:
        what = "被别的进程占着 —— 先确认没有第二个实例在跑同一个数据根"
    else:
        what = "开库失败（原因见下），先按数据安全处理：留证、换备份、再排查"
    return f"库文件打不开：{target}\n  原因：{raw}\n  怎么办：{what}"


#: 测试态故障注入（2026-10-04 快照「零故障注入」那一格）：`{操作名: 剩余次数}`。
#: **生产路径上它永远是空的 dict** —— 这一层不读配置、不看环境变量，只有测试会写它。
#:
#: 为什么要有它，而不是在用例里 monkeypatch `sqlite3.Connection.commit`：那样改的是**别人的
#: 库**（补丁装在标准库类上，撤不干净就会漏到别的用例里），而这里注入的是本仓自己的那一格，
#: 复位是同一条命令。为什么用"剩余次数"而不是开关：一个会一直生效的故障注入，第二个用例
#: 撞上它时红得莫名其妙 —— "测试互相下毒"是这一族最容易长出来的形状。
_INJECTED_FAULTS: dict[str, int] = {}


def inject_fault(op: str, *, times: int = 1) -> None:
    """让接下来 `times` 次 `op`（`execute` / `executemany` / `executescript` / `commit`）
    抛一条**真形状**的 `sqlite3.OperationalError`（磁盘满那一句，与 ENOSPC 实测同文本）。"""
    _INJECTED_FAULTS[op] = _INJECTED_FAULTS.get(op, 0) + times


def clear_faults() -> None:
    _INJECTED_FAULTS.clear()


def _maybe_fault(op: str) -> None:
    remaining = _INJECTED_FAULTS.get(op, 0)
    if remaining <= 0:
        return
    if remaining == 1:
        _INJECTED_FAULTS.pop(op, None)
    else:
        _INJECTED_FAULTS[op] = remaining - 1
    raise sqlite3.OperationalError("database or disk is full")


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
        _maybe_fault("execute")
        return self._current().execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        _maybe_fault("executemany")
        return self._current().executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        _maybe_fault("executescript")
        return self._current().executescript(*args, **kwargs)

    def commit(self) -> None:
        _maybe_fault("commit")
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


def migrate_event(message: str) -> None:
    """迁移事件落 stderr（`R102-64`）：backend.log 由壳转发 stderr，迁移是三类关键路径里
    **唯一没有可追溯事件**的一类 —— R102-27 那种"半路死"发生时只有异常栈可查。排查判据
    要进事件流，不进注释（`R102-42` 同族的另一半）。

    **公开给 core**：步骤的清单与顺序住在 `core/migrations.py`，那边执行一步就念一声 ——
    同一条事件流，不另起炉灶（storage 里再长一个私有出口就是第二个真相）。"""

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"[schema-migrate] {stamp} {message}", file=sys.stderr, flush=True)


def _check_schema_generation(conn: SqlConnection) -> None:
    """库的代际比对（`R102-54`）：新库给旧代码 = 一句 loud 拒启，而不是散落的炸点。

    只在这里**拒新**；把 user_version 推到当前值的动作移到了 bootstrap 的最后
    （2026-10-04 审查快照的迁移事务条目）：从前戳在迁移执行**之前**，中途崩掉就留下
    一个"名义代际是新的、形状还是旧的"的库 —— `repair_stranded_rebuild` 只自愈
    三张已知暂存表，盖不住其它半途形状。
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库名义代际（user_version={version}）比当前代码（{SCHEMA_VERSION}）新 —— "
            "这通常是装包回滚后用旧程序读新库。请装回新版，或从备份恢复数据根。"
        )


def repair_stranded_rebuild(conn: SqlConnection, *, original: str, temp: str) -> None:
    """整表重建"死在 DROP 原表之后、RENAME 之前"的现场自愈（`R102-53`）。

    判据是库的形状：原表不在、暂存表在 ⇒ 有数据就改名回原表（旧形状数据原样回来，
    后面的"判老形态→重建"整个重跑一遍，数据一分不丢），是空的就直接 DROP（那只是
    死在 INSERT 之前的老窗口，DROP IF EXISTS 已自愈过它）。两种现场都不许静默留着
    —— 滞留的暂存表是一本**永远没人读**的账本，而 bootstrap 重跑不但不报错还照常绿。

    **点名哪三对**（原表 / 暂存表）的是 `core/migrations.py` 的 `STRANDED_REBUILDS`：
    storage 只回答"怎么修"，不记得"修谁"（P1-6：业务表名不住在 storage）。
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
        migrate_event(
            f"检测到滞留暂存表（原表 {original} 不在、{temp} 有 {temp_rows} 行）—— "
            "已改名回原表，本次迁移整个重跑（R102-53 的第三种死法现场）"
        )
    else:
        conn.execute(f"DROP TABLE {temp}")
        migrate_event(f"清掉空的滞留暂存表 {temp}（R28-15 的老窗口现场）")
    conn.commit()


#: 删之前那一行的落盘目录（相对数据根）与保留份数 —— 备份目录自己不许变成新的只增表。
RETENTION_BACKUP_DIRNAME = "retention-backups"
RETENTION_BACKUP_KEEP = 5


def _json_cell(value: object) -> object:
    """一格的 JSON 可表示形式：**bytes 走 base64 包**，其余交给 `default=str`。

    为什么必须这样：blob 列（langgraph 检查点的 msgpack、图片指纹那族）按 `str()` 落盘是
    **不可逆**的 —— 备份的整个意义是"删错了还能拿回原样"，而 `str(b'\\x00...')` 拿回来的是
    一段 `"b'\\x00...'"` 文本，重放时没人能还原那 40 个字节。从前这一族有两份实现
    （retention 用 `default=str`、sync 的删前备份用 base64），而**需要无损的那一侧恰好是
    检查点**，所以 sync 那份才是对的。合并成一份按对的来，retention 那三张表没有 bytes 列，
    行为逐字不变（用例 `test_doomed_rows_are_dumped_column_by_column_before_they_vanish`
    断的就是逐列原样，两版都过 —— 过了才敢说非回归）。
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__base64__": base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)


def dump_rows_to_jsonl(
    path: Path,
    rows: Sequence[sqlite3.Row],
    *,
    notice: str | None = None,
) -> int:
    """把给定的行逐列写成 JSONL（追加语义），返回写出的行数。0 行不碰文件系统。

    `notice` 给就顺带往 stderr 念一声（retention 用它报"先落备份再删"）：备份这件事
    **必须让人看见**，静默写一份没人知道存在的文件，等价于没有备份。
    """
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            record = dict(zip(row.keys(), tuple(row), strict=True))
            fh.write(json.dumps(record, ensure_ascii=False, default=_json_cell))
            fh.write("\n")
    if notice:
        print(notice, file=sys.stderr, flush=True)
    return len(rows)


def trim_backups(backup_dir: Path, *, keep: int = RETENTION_BACKUP_KEEP) -> None:
    """每张表各留最近 `keep` 份 —— 防"为了不留只增表而造出另一张只增表"。

    分表裁而不是整目录一起裁：文件名是 `<表名>-<时刻>.jsonl`，字典序里 `command_approval-*`
    永远压在 `audit_log-*` 前面 —— 整目录裁会让一批审批备份把审计备份**饿死**（删掉的恰是
    唯一那份能找回审计行的文件）。时刻是秒级 `YYYYmmdd-HHMMSS`，同表内按名排序=按时间排序。
    """
    by_table: dict[str, list[Path]] = {}
    for f in backup_dir.glob("*.jsonl"):
        by_table.setdefault(f.name.split("-", 1)[0], []).append(f)
    for files in by_table.values():
        for stale in sorted(files, key=lambda p: p.name, reverse=True)[keep:]:
            stale.unlink(missing_ok=True)


def dump_before_delete(
    conn: SqlConnection,
    *,
    table: str,
    where: str,
    params: tuple[object, ...],
    backup_dir: Path,
    stamp: str,
    source: str = "retention",
) -> int:
    """把**将要被删的行**先写成 JSONL，返回行数。0 行就不落文件。

    顺序是判据：先落盘、再删、最后才 commit —— 中间崩掉的结果是"行还在库里 + 多一个
    备份文件"，而不是"行没了 + 没有任何地方能找回"。这是 `R102-29` 拍板里
    "先备份再删"那半句的实现（10-03 复核发现那半句从没落地，见台账 H12 批 24）。

    `source` 进日志前缀（默认 `retention`）：那一声"[retention]"从前是唯一调用点留下的，
    而现在删会话也走这台机械 —— 备份是谁触发的必须说对，排障时"retention 删了这些行"
    与"用户删了会话"是两件完全不同的事（同一个 prefix 会让人去查保留策略而看不到是谁按了键）。

    **公开给 core**：`table` / `where` 是**策略**（哪张表、留多久），住 `core/retention.py`；
    本模块只提供"按给定条件先备份再交出去"这台机械 —— 与 `prune_retention_tables` 搬家
    同一批（2026-10-04 审查快照 P1-6：storage 不留业务表名）。
    """
    rows = conn.execute(f"SELECT * FROM {table} {where}", params).fetchall()  # noqa: S608
    n = dump_rows_to_jsonl(
        backup_dir / f"{table}-{stamp}.jsonl",
        rows,
        notice=f"[{source}] 先落备份 {table}-{stamp}.jsonl（{len(rows)} 行 {table}）再删",
    )
    return n


class MigrationPlanLike(Protocol):
    """storage 认识的**迁移计划形状**（`core.migrations.MigrationPlan` 结构性满足它）。

    storage 只问三件事：DDL 之前跑什么、DDL 之后跑什么、通用补列器第一遍避开哪些表。
    它不认识 `Step`、不认识任何业务表名 —— 步骤的清单与顺序住在 `core/migrations.py`
    （2026-10-04 审查快照 P1-6：形状迁移的语义从 storage 收回 core，storage 只留
    连接 + 声明引擎 + 执行入口）。
    """

    @property
    def shape_tables(self) -> frozenset[str]:
        """通用补列器第一遍必须避开的表（每张对应 core 那边一步整表重建/搬层）。"""
        ...

    def run_pre_ddl(self, conn: SqlConnection) -> None:
        """schema DDL **之前**的步骤（清重 / 滞留暂存表自愈）。"""
        ...

    def run_shape(self, conn: SqlConnection) -> None:
        """schema DDL **之后**、第二遍补列**之前**的步骤（整表重建 / 搬层族）。"""
        ...


def _require_plan_for_existing_db(conn: SqlConnection, *, plan: MigrationPlanLike | None) -> None:
    """**已经存在的库**必须显式交迁移计划，否则当众失败 —— 不做任何启发式判断。

    为什么是"非空库一律要计划"而不是"落后于声明才要"（2026-10-04 审查快照 P1-6 的落地
    取舍）：判"这份库需要哪些业务步骤"的知识住在 `core/migrations.py`（清重、滞留自愈、
    整表重建、搬层），storage 拿不到 —— 任何"我看它像不需要"的猜测都会把**该搬的没搬**
    变成静默事故（症状：配置变小 / 主键没换 / 暂存表里的数据再没人读），而那正是搬家前
    `provider_layers is None` 那句 RuntimeError 要拦的东西。判据换成"库是不是空的"有三个
    好处：不点名任何表、不解析声明（省一次内存探针）、**新建库零负担**（空库没有历史，
    本来也没有东西可搬）。

    生产入口（`core/bootstrap.py`、`scripts/init_db.py`）永远交 `MIGRATION_PLAN`；会撞上
    这句的只有"拿已有库却忘了交计划"的接线 —— 那正该红。
    """
    if plan is not None:
        return
    existing = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    if existing:
        raise RuntimeError(
            "数据库已存在，业务迁移计划必须显式交进来（清重 / 滞留自愈 / 整表重建 / 搬层"
            "这些步骤住 core/migrations.py，storage 不认识它们）：显式传 "
            "plan=rolecard_agent.core.migrations.MIGRATION_PLAN；新建空库可以不传。"
        )


def bootstrap(
    conn: SqlConnection,
    enabled_domains: Iterable[str] = (),
    *,
    plan: MigrationPlanLike | None = None,
) -> list[str]:
    """Apply every schema file. Idempotent - all DDL uses IF NOT EXISTS.

    `plan` 是**业务迁移的全部**：DDL 之前的清重/自愈与 DDL 之后的整表重建/搬层都由它交
    （见 `MigrationPlanLike`）。None 只对**新建空库**合法 —— 库里已有表时会被
    `_require_plan_for_existing_db` 当众拦下（业务步骤住 core，storage 猜不到这份库该搬
    什么，静默跳过正是"配置变小"那种最难查的事故）。

    Returns the applied file names, which is what tests assert on: a silently skipped
    schema is far worse than a loud failure.
    """
    # 代际戳（`R102-54`）：库的版本住在 `PRAGMA user_version` 里。库比代码新 = 装包
    # 回滚后旧代码读新库 —— 两层的 `model_backend` 没有 `api_key/usage` 列，旧读法的
    # `no such column` 会散落在启动链各处；这里一句 loud 报错把它并成一处。
    _check_schema_generation(conn)
    _require_plan_for_existing_db(conn, plan=plan)
    files = schema_files(enabled_domains)
    # 声明形状只解析一次（内存探针把整套 DDL 跑一遍），两遍补列共用它。
    declared = _declared_columns(files)
    # 先把"声明里有、这份老库里没有"的列补上，再跑 DDL：域表的 `CREATE INDEX ... (新列)`
    # 排在任何迁移之前执行，老库踩它必炸（09-26 轮 R26-04 的实测现场）。
    reconcile_columns(
        conn,
        files=files,
        declared=declared,
        skip=frozenset() if plan is None else plan.shape_tables,
    )
    # 业务阶段（core 交进来的步骤，顺序与名字都在 core/migrations.py）：
    # pre_ddl 必须站在 DDL 之前（唯一索引踩重复行 = 库打不开，R28-15）；
    # shape 必须站在 DDL 之后（重建会 DROP/RENAME，先补的列与先建的索引跟着旧表没）。
    if plan is not None:
        plan.run_pre_ddl(conn)
    applied: list[str] = []
    for path in files:
        if not path.exists():
            raise FileNotFoundError(f"schema file missing: {path}")
        conn.executescript(path.read_text(encoding="utf-8"))
        applied.append(str(path.relative_to(PACKAGE_ROOT)))
    if plan is not None:
        plan.run_shape(conn)
    # 第二遍：形状迁移里那些 DROP/重建（service_endpoint 整表、model_backend 搬层）
    # 会把第一遍补好的列跟着旧表一起带走，所以搬层之后再对齐一次声明。
    # 这一遍**不再跳过**那三张手形迁移的表（`R28-21`）。跳过只对**第一遍**是必要的：那里
    # 补出 `provider_id` 会让"没有 provider_id 就是旧形态"的判定当场失效，搬层被静默跳过。
    # 而此刻迁移已经做完、形状已经是新的 —— 继续跳过等于给这三张表判了"今后声明的新列永远
    # 补不上"，症状要等到某个老库升上来才现形（台账原判：埋点不是事故，但它是**只会更贵**
    # 那种埋点）。真遇到"这列必须回填数据"的情况，补列器会抛那句
    # 「NOT NULL 又没默认值 ⇒ 必须走整表重建」并点名 core 的 `SHAPE_TABLES`，
    # 那正是想要的大声失败，而不是静默什么都不做。
    reconcile_columns(conn, files=files, declared=declared, skip=frozenset())
    conn.commit()
    # 代际戳在**迁移全部完成后**才推进（2026-10-04 审查快照的迁移事务条目）：中途崩掉
    # 留下的是"user_version 还是旧的"，下一次启动会原样重跑一遍幂等 bootstrap —— 而不是
    # 一个"名义代际已新、形状未完成"的假新库。
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        migrate_event(f"user_version 戳到 {SCHEMA_VERSION}（bootstrap 全部完成后）")
    return applied


def quote_ident(ident: object) -> str:
    """双引号包住标识符（SQLite 的标准引用法），内嵌的双引号按 SQL 规则翻倍。"""
    return '"' + str(ident).replace('"', '""') + '"'


def table_columns(conn: SqlConnection, table: str) -> list[str]:
    """这张表**当前真实**的列名（`PRAGMA table_info`，现算不缓存）。

    为什么是现算而不是一张手写的列清单：清单就是第二份事实面 —— 表加了列而清单没跟上，
    那边的 INSERT 永远少那一列，症状是"写路径静默少列"（同步导入那条链踩过，见
    `core/memory.restore_row` / `core/reachout.inbox.restore_row`）。
    表名走 `quote_ident`：这张表名来自代码常量，但"现算"的机制不该给调用方留一个
    直接把变量拼进 PRAGMA 的口子。
    """
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return [str(r["name"]) for r in rows]


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
            "这种列必须走整表重建：把它加进 core/migrations.py 的 `SHAPE_TABLES`，"
            "并在那边的 shape 阶段写一次重建步骤。"
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
    skip: frozenset[str] = frozenset(),
    declared: dict[str, dict[str, sqlite3.Row]] | None = None,
) -> list[str]:
    """把"schema 里声明了、这份库里却没有"的列补齐，返回补过的 `表.列` 清单。

    为什么要有这一层（09-26 轮 R26-04）：形状迁移原先那 12 处 ALTER 一条条手写，
    于是"加一列"的人必须记得来这儿再写一遍 —— **忘了不会红**，只会在第一次读那一列时炸。
    两份历史形状当时合计缺 19 列，全都能自动补（无一例 NOT NULL 无默认；09-27 的 M2a 给
    `role_card` 加了 `user_id` 之后这组数变成 23 —— 它本来就该随 schema 长，钉住它、逼改 schema
    的人回来看一眼这行注释的用例是 `test_audited_shortfall_is_still_the_shortfall`）。这一层把
    "记不记得"换成"声明即事实"：以后加列只改 `schema.sql` 一处。

    `skip` 由调用方按计划给（`plan.shape_tables` = 那些要走整表重建/搬层的表，第一遍必须
    避开）；`declared` 供 `bootstrap` 复用同一份解析结果（内存探针跑整套 DDL 不便宜）。

    只对**已存在的表**补列：表整个不存在 = 那份 DDL 自己会建，不该在这儿猜形状。
    主键列不参与（形状根本不同，那是 core 那边整表重建的事）。
    """
    targets = list(files) if files is not None else schema_files()
    if declared is None:
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


#: 「带 `thread_id` 的表有哪些」的**现数**判据住在 `storage/threads.py`（线程的 repository）——
#: 写死清单那一版漏过 `command_approval.thread_id`（R28-23），判据交给库本身才不会复发；
#: 而 db.py 只留连接与声明引擎，不点名任何表（2026-10-04 审查快照 P1-6）。


