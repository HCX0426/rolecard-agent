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
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
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
    files = schema_files(enabled_domains)
    # 先把"声明里有、这份老库里没有"的列补上，再跑 DDL：域表的 `CREATE INDEX ... (新列)`
    # 排在任何迁移之前执行，老库踩它必炸（09-26 轮 R26-04 的实测现场）。
    reconcile_columns(conn, files=files)
    applied: list[str] = []
    for path in files:
        if not path.exists():
            raise FileNotFoundError(f"schema file missing: {path}")
        conn.executescript(path.read_text(encoding="utf-8"))
        applied.append(str(path.relative_to(PACKAGE_ROOT)))
    _migrate(conn)
    # 第二遍：`_migrate` 里那些 DROP/重建（service_endpoint 整表、model_backend 搬层）
    # 会把第一遍补好的列跟着旧表一起带走，所以搬层之后再对齐一次声明。
    reconcile_columns(conn, files=files)
    conn.commit()
    return applied


#: 这几张表的旧形态由 `_migrate` 整表重建 / 搬层负责，通用补列器**必须避开**它们：
#: 提前给旧形 `model_backend` 补上 `provider_id`，`_migrate` 里那句"没有 provider_id
#: 就是旧形态"的判定当场失效 —— 搬层被跳过，旧行的凭据静静留在没人再读的列里。
#: `token_usage_day` 不是列的问题而是**主键**（B1a）：补列改不了 `(day, backend)` → 
#: `(day, user_id, backend)`，只补列会让 `ON CONFLICT(day,user_id,backend)` 永远报
#: "non-unique" —— 所以它同理要整表重建。
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


def _migrate(conn: SqlConnection) -> None:
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
    if "api_key" in _columns(conn, "service_endpoint"):
        conn.execute("DROP TABLE service_endpoint")
        conn.execute("DELETE FROM kernel_meta WHERE key = 'service_endpoints_seeded'")
        core = core_schema_path()
        conn.executescript(core.read_text(encoding="utf-8"))
    # 9. model_backend 两层化（凭据上收 model_provider、usage 变成 chat 引用行）。
    #    必须排在 service_endpoint 重建**之后**：搬层要往新形态的引用表里写 chat 行。
    from rolecard_agent.core.model_settings import migrate_to_provider_layers  # noqa: PLC0415

    migrate_to_provider_layers(conn)
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
