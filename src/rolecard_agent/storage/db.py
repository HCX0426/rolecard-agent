"""SQLite connection helpers + schema bootstrap.

Bootstrap order is fixed and not optional:
  1. core/schema.sql        tenant / app_user / session_thread / plugin / audit_log
  2. roles/schema.sql       role_card
  3. domains/<x>/schema.sql for each ENABLED plugin, in DOMAINS order

Step 1 must run first: domains reference app_user(user_id).

Every connection MUST enable `PRAGMA foreign_keys = ON` - SQLite ignores foreign keys by
default, which would silently turn the ON DELETE CASCADE in domains/health/schema.sql into
a no-op and leave orphaned index rows behind.

No migrations in v1. Every statement is `CREATE TABLE IF NOT EXISTS`, so changing a table
does NOT update an existing database - it has to be rebuilt (`rm data/sqlite/app.db` then
re-run `scripts/init_db.py`). That is a deliberate v1 trade-off, not an oversight; production
would bring in Alembic. Stated here because this is where someone would look for it.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
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
    conn.execute("PRAGMA journal_mode = WAL")
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
        self._created: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _current(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._opener(self._path)
            self._local.conn = conn
            with self._lock:
                self._created.append(conn)
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
        """关闭本线程能安全关闭的连接。

        其它线程正在使用的连接**不能**在这里关（sqlite3 禁止跨线程使用连接对象），它们会
        随线程结束被回收 —— 进程退出时由解释器统一清理。
        """
        with self._lock:
            pending = list(self._created)
            self._created.clear()
        for conn in pending:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._local = threading.local()

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


def bootstrap(conn: SqlConnection, enabled_domains: Iterable[str] = ()) -> list[str]:
    """Apply every schema file. Idempotent - all DDL uses IF NOT EXISTS.

    Returns the applied file names, which is what tests assert on: a silently skipped
    schema is far worse than a loud failure.
    """
    applied: list[str] = []
    for path in schema_files(enabled_domains):
        if not path.exists():
            raise FileNotFoundError(f"schema file missing: {path}")
        conn.executescript(path.read_text(encoding="utf-8"))
        applied.append(str(path.relative_to(PACKAGE_ROOT)))
    _migrate(conn)
    conn.commit()
    return applied


def _columns(conn: SqlConnection, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: SqlConnection) -> None:
    """演示库的幂等列级迁移（无迁移框架，ALTER/DROP 全部可重跑）。

    1. model_backend 增列 usage（供应商配置唯一事实面的用途标记）——旧库补列，默认 chat。
    2. service_policy 已退役（策略并入 service_endpoint 行内 enabled/sort_order）→ DROP。
    3. service_endpoint 旧形态（行内嵌 key/base_url 的"实例"模型）→ 整表重建为
       「引用 model_backend」的新形态；旧行配置属演示数据且引用化后由模型页承接，
       直接弃用。**必须连 seed flag 一起清**，否则 seed_once 会以为播过种而跳过，
       留下一张空表（实测踩过：引用行全部缺失）。
    """
    if "usage" not in _columns(conn, "model_backend"):
        conn.execute("ALTER TABLE model_backend ADD COLUMN usage TEXT NOT NULL DEFAULT 'chat'")
    # 4. model_backend 增列 num_ctx（本地 Ollama 的实际上下文窗口，tokens）。
    #    为什么必须有：Ollama 默认只开 2048 tokens 的窗口——不显式传 num_ctx，
    #    模型自带的 32k 窗口形同虚设，超出的历史会被引擎静默截断。NULL = 用模型默认。
    if "num_ctx" not in _columns(conn, "model_backend"):
        conn.execute("ALTER TABLE model_backend ADD COLUMN num_ctx INTEGER")
    # 5. session_thread 增列 agent_mode（v2.5 会话级「对话/智能体」切换）。
    #    与 model_name 同一模式：NULL = 跟随全局默认（settings.agent_default_mode），
    #    chat 端点每轮实时读库解析有效值注入 state，会话切模式下一轮即生效。
    #    旧库无此列 → 补；新库建表已含 → 跳过（幂等）。
    if "agent_mode" not in _columns(conn, "session_thread"):
        conn.execute("ALTER TABLE session_thread ADD COLUMN agent_mode TEXT")
    # 6. role_card 增列 reachout_enabled（v2.5 角色主动开口，架构计划 B）。
    #    NULL/DEFAULT 0 = 出厂静默；角色卡上勾选后该角色才有资格主动（还需全局开关）。
    if "reachout_enabled" not in _columns(conn, "role_card"):
        conn.execute("ALTER TABLE role_card ADD COLUMN reachout_enabled INTEGER NOT NULL DEFAULT 0")
    # 6b. role_card 增列 recall_enabled / time_pattern_enabled（关系驱动主动开口，架构计划 §5.2）。
    #     per-role 两类关系驱动触发源的开关；默认 1 = 开启 reachout_enabled 后关系驱动即生效。
    if "recall_enabled" not in _columns(conn, "role_card"):
        conn.execute("ALTER TABLE role_card ADD COLUMN recall_enabled INTEGER NOT NULL DEFAULT 1")
    if "time_pattern_enabled" not in _columns(conn, "role_card"):
        conn.execute(
            "ALTER TABLE role_card ADD COLUMN time_pattern_enabled INTEGER NOT NULL DEFAULT 1"
        )
    # 6c. role_card 增列 file_watch_enabled（文件事件触发，架构计划 C·§5.2）。
    #     per-role 闸门；默认 1 = 有主动开口资格的角色自动可被目录变化触发（还需全局闸）。
    if "file_watch_enabled" not in _columns(conn, "role_card"):
        conn.execute(
            "ALTER TABLE role_card ADD COLUMN file_watch_enabled INTEGER NOT NULL DEFAULT 1"
        )
    # 7. 关系驱动主动开口（架构计划 §5.2）：per-role 状态与 per-role 记忆（幂等建表）。
    if "affinity" not in _columns(conn, "role_proactive_state"):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS role_proactive_state ("
            " role_id TEXT PRIMARY KEY, affinity REAL NOT NULL DEFAULT 0.0,"
            " last_interaction_utc TIMESTAMP, calibration_json TEXT,"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    if "value" not in _columns(conn, "role_memory"):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS role_memory ("
            " role_id TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '',"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    conn.execute("DROP TABLE IF EXISTS service_policy")
    if "api_key" in _columns(conn, "service_endpoint"):
        conn.execute("DROP TABLE service_endpoint")
        conn.execute("DELETE FROM kernel_meta WHERE key = 'service_endpoints_seeded'")
        core = core_schema_path()
        conn.executescript(core.read_text(encoding="utf-8"))
