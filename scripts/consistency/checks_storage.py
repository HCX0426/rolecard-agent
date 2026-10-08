"""storage 主题这一族判据（P3-9 按主题细分，从 checks.py 平移，正文一字未改）。

按主题拆出；执行顺序由 registry.CHECKS 唯一决定，本模块只回答"这一族住哪"。
行为等价由门禁实跑全部 CHECKS 证明。
"""

from __future__ import annotations

import ast
import pathlib
import re

# 跨族共享助手：唯一定义在别的族模块，按「谁在用谁 import」接线（不复制定义）。
from .checks_domain import _sql_table_names  # noqa: F401
from .core import ROOT, fails, iter_files, out


def _declared_table_names() -> set[str]:
    """schema 里声明的**全部**表名（core / roles / 各域）—— 业务表名的唯一来源。

    与 `_domain_private_tokens` 同一条规矩：名字从声明里推，不手写清单 —— 手写清单就是
    第二份事实面，而"清单漏了"的表现是这条尺子照常打绿。
    """
    src = ROOT / "src" / "rolecard_agent"
    names = _sql_table_names(src / "core" / "schema.sql") | _sql_table_names(
        src / "roles" / "schema.sql"
    )
    for schema in sorted((src / "domains").glob("*/schema.sql")):
        names |= _sql_table_names(schema)
    return names


def _code_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """文件里的字符串常量（**扣掉 docstring**，注释本来就不是常量）。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not (isinstance(body, list) and body):
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.lineno not in docstring_lines
    ]


def check_storage_db_has_no_business_tables() -> None:
    """`storage/db.py` 里不许出现业务表名（2026-10-04 审查快照 P1-6 的验收句）。

    为什么单立这一格：迁移引擎从 `_migrate` 收成 `core/migrations.py` 的步骤注册表、
    retention 策略搬进 `core/retention.py`、`thread_id_carriers` 归 `storage/threads.py`
    之后，**连接 + 声明引擎 + 执行入口**这层在语义上再也不需要点名任何一张表 ——
    剩下的每一处点名都意味着"策略/业务语义偷偷留在了 storage"。

    判据只管**字符串常量**（注释不算：注释里的表名单是解释，不是耦合），并扣掉
    docstring（模块/函数顶那段散文里讲"哪些表"是必要的说明）。反向一臂：声明出来的表名
    必须够多，否则这条尺子会因为"declared 为空"而永远绿。
    """
    rel = "src/rolecard_agent/storage/db.py"
    tables = sorted(_declared_table_names())
    if len(tables) < 10:  # 反向臂：判据被掏空就该红，而不是"零命中=通过"
        out("storage db has no business tables", False, f"只推出 {len(tables)} 个表名（<10）")
        fails.append(
            "storage db: declared table names look empty — the check would pass vacuously"
        )
        return
    pattern = re.compile(rf"\b({'|'.join(sorted(tables, key=len, reverse=True))})\b")
    hits = [
        f"{rel}:{lineno} {m.group(1)}"
        for lineno, text in _code_strings(ROOT / rel)
        if (m := pattern.search(text))
    ]
    ok = not hits
    detail = (
        f"db.py 里 0 处点名业务表（声明里 {len(tables)} 张表）"
        if ok
        else "; ".join(hits[:4])
    )
    out("storage db has no business tables", ok, detail)
    if hits:
        fails.append(
            "storage/db.py names business tables: "
            f"{hits} —— 策略/业务语义应住 core（migrations / retention）或 storage 的 repository"
        )


#: `api/` 里出现下面任一形状即红：直连执行（`.execute(`）或**以 SQL 开头的字符串常量**。
#: 两臂各挡一类回流：只查 `.execute(` 会被"把语句写成常量再传出去"绕过；只查常量会被
#: `conn.execute(变量)` 漏掉（变量那一形靠 execute 臂抓）。
#: **锚在串首**不是随手写的：本仓同族那把 `audit action vocabulary` 最初按子串数，
#: 把 `audit.py` 自己 docstring 里那句"从前有五份"数成了第二份语句（`R102-37` 记过），
#: 改法就是换成"以该语句开头的常量"。这里同病同治：注释与 docstring 里提一句
#: "从前这里是 SELECT …"不该把自己数成越层者。
_SQL_SHAPE = re.compile(
    r"^\s*(SELECT\b|INSERT\s+INTO\s+\w|UPDATE\s+\w+\s+SET|DELETE\s+FROM\s+\w|PRAGMA\s+\w)",
    re.IGNORECASE,
)


def check_api_holds_no_sql() -> None:
    """HTTP 层不许自己碰 SQL（2026-10-04 service 收口的尺子，`R102-05` 那条纪律的另一半）。

    为什么单独立一条：从前 `api/routers/` 是**事实上的 service 层** —— 12 处裸 SQL、
    整份替换的事务与回滚决定、完整一段记忆提取状态机都长在路由里。越层的代价不是读不出来，
    是**非 HTTP 宿主复用不了**：桌宠壳与 `scripts/` 的取证脚本想要同一条链，只能再抄一遍，
    而抄的那一份不跟着事务边界一起改（本仓那一族事故的正面描述）。

    两条判据写成一条 if/elif 链 —— 不嵌套（嵌套撞 ruff 的 SIM102），也不把 `isinstance`
    先取到局部 bool 再判（那会让 mypy 丢掉类型收窄，实测 `"AST" has no attribute "value"`）。
    两种都试过，这条链是唯一同时过两关的形状。
      * `.execute(` 调用点：不论参数是字面量还是变量，HTTP 层握着连接发语句即红；
      * 以 SQL 关键字开头的文本常量：抓"语句写在路由里、执行在别处"那一形。
    两臂各挡一条绕路：只查 execute 会被"常量放模块顶、别处执行"绕过，只查常量会被
    `conn.execute(变量)` 漏掉。**拼接（f-string）那一形没算进第二臂**：要在 `api/` 里真的
    发出语句必须握着连接，而握连接就是第一臂 —— 第二臂只是提前一步的哨兵，够用。

    串首锚定 + 排除 docstring 两样一起，为的是让"解释这句话"不污染判据：docstring 在 AST
    里也是 `ast.Constant`，一句"从前这里是 SELECT …"会把尺子变成自指导弹（`R102-37`
    那把 `audit action vocabulary` 最初就栽在同一件事上，改法是锚语句首）。

    这一条按定义就该是 0：`grep -rn "\\.execute(" api/` 为零是它的验收口径，所以**不设
    豁免名单** —— 要往回加 SQL，得先说服这条判据改掉。
    反向还有一臂（防空转）：`api/` 必须真的扫到文件，否则"0 处"是扫了个空。
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted((ROOT / "src" / "rolecard_agent" / "api").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        scanned += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        # 裸字符串语句 = docstring 的位置。它不是代码（与 `write txn ownership inventory`
        # 把协议声明排除掉是同一条道理）。
        prose = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in prose
                and _SQL_SHAPE.search(node.value)
            ):
                offenders.append(f"{rel}:{node.lineno} 裸 SQL 常量")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
            ):
                offenders.append(f"{rel}:{node.lineno} 直连 execute(")
    hollow = scanned == 0
    ok = not offenders and not hollow
    detail = (
        f"api/ {scanned} 个模块，SQL 与 execute 0 处（都走 service / storage）"
        if ok
        else (f"越层：{offenders}" if offenders else "api/ 一个文件都没扫到（判据在空转）")
    )
    out("api holds no sql", ok, detail)
    if offenders:
        fails.append(
            "the HTTP layer is talking to SQL directly: "
            f"{offenders[:6]}（业务与语句归 service / storage，路由只留校验、异常映射、投送）"
        )
    if hollow:
        fails.append("api-holds-no-sql is hollow: nothing under src/rolecard_agent/api was scanned")


#: 裸 `print` 的**豁免表**（2026-10-04 审查快照"三条日志通道并存"那格的验收：
#: "grep src 无 print(（豁免外）"）。键 = 文件，值 = 允许的**精确数量** ——
#: 多一处（有人新开口）与少一处（已迁移却没更新名单，名单在说谎）都红。
RAW_PRINT_ALLOWLIST: dict[str, int] = {
    # 观测出口自己：`base/observability.logline` 的唯一实现处，全仓的人读日志从这一句出去。
    "src/rolecard_agent/base/observability.py": 1,
    # storage 在 base 之下，观测出口在结构上不可达（`test_import_floor` 的
    # ALLOWED_DOWNWARD：storage 只许 import config/storage）—— 这 4 处是启动期通道：
    # conn-close 两处诊断 + `migrate_event`（schema-migrate 事件流的 storage 侧写手，
    # db.py 自己的 docstring 写着"不另起炉灶"）+ 备份删除的 notice。要迁它们得先动分层
    # 契约，不该在日志这一格里顺手翻尺。
    "src/rolecard_agent/storage/db.py": 4,
}
#: 反向防空转：`logline` 在 src 里的真实调用点少于这个数 = 迁移压根没发生（或被拆回去了）。
LOGLINE_MIN_CALLS = 10


def check_log_channels_unified() -> None:
    """人读日志只有一条通道：裸 `print` 只许在豁免表里，stdlib `logging` 在 src 归零。

    为什么单立这一格：从前三条人读通道并存 —— Tracer 的结构化事件（机器读）、17 处裸
    `print`（各写各的前缀、stdout/stderr 混着来）、`core/tools/mcp.py` 一份英文
    stdlib logging。代价不是难看，是**排障要先猜消息在哪条通道**。收口后分工两句话：
    结构化归 `Tracer.emit`，人读的一句话归 `base/observability.logline`。

    四臂判据（少一臂都会空转）：
      * 豁免表外的文件出现 print 即红（抓"再开一个口"）；
      * 豁免表里的数量**精确相等**：多 = 新开口；少 = 已迁移却没更新名单（名单说谎）；
      * src 里任何 `import logging` / `from logging import` 即红（mcp 那三行已迁）；
      * `logline` 调用点 ≥ `LOGLINE_MIN_CALLS`（防空转：一条都没有说明"迁移"是假的）。
    AST 找调用而非 grep 文本：docstring 里那句"从前 17 处裸 print"是解释，不是出口
    （同 `api holds no sql` 的"字符串常量 + 扣 docstring"那条纪律）。
    """
    src = ROOT / "src" / "rolecard_agent"
    counts: dict[str, int] = {}
    logging_hits: list[str] = []
    logline_calls = 0
    scanned = 0
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        scanned += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        prints = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "print":
                    prints += 1
                elif node.func.id == "logline":
                    logline_calls += 1
            elif isinstance(node, ast.Import):
                if any(a.name == "logging" or a.name.startswith("logging.") for a in node.names):
                    logging_hits.append(f"{rel}:{node.lineno} import logging")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module == "logging" or node.module.startswith("logging."))
            ):
                logging_hits.append(f"{rel}:{node.lineno} from logging import …")
        if prints:
            counts[rel] = prints

    unregistered = sorted(
        f"{rel}×{n}" for rel, n in counts.items() if rel not in RAW_PRINT_ALLOWLIST
    )
    stale = sorted(
        f"{rel}（登记 {RAW_PRINT_ALLOWLIST[rel]}，现 {counts.get(rel, 0)}）"
        for rel in RAW_PRINT_ALLOWLIST
        if counts.get(rel, 0) != RAW_PRINT_ALLOWLIST[rel]
    )
    hollow = logline_calls < LOGLINE_MIN_CALLS
    ok = not unregistered and not stale and not logging_hits and not hollow
    if ok:
        detail = (
            f"{scanned} 个模块：裸 print 全在豁免内（{sum(RAW_PRINT_ALLOWLIST.values())} 处）、"
            f"logging 0 处、logline {logline_calls} 个调用点"
        )
    else:
        detail = "; ".join(
            part
            for part in (
                f"豁免外裸 print：{unregistered}" if unregistered else "",
                f"豁免数量对不上：{stale}" if stale else "",
                f"src 里还有 logging：{logging_hits}" if logging_hits else "",
                f"logline 只有 {logline_calls} 个调用点（<{LOGLINE_MIN_CALLS}，判据在空转）"
                if hollow
                else "",
            )
            if part
        )
    out("log channels unified", ok, detail)
    if unregistered:
        fails.append(
            "raw print outside the logline allowlist: "
            f"{unregistered}（人读日志归 base/observability.logline；"
            "确有结构性理由的加进 RAW_PRINT_ALLOWLIST 并写清理由）"
        )
    if stale:
        fails.append(
            f"raw-print allowlist out of date: {stale}（迁移了就更新数量，别留说谎的名单）"
        )
    if logging_hits:
        fails.append(f"stdlib logging survives in src: {logging_hits}（迁到 logline）")
    if hollow:
        fails.append(
            "log-channels check is hollow: logline call sites "
            f"{logline_calls} < {LOGLINE_MIN_CALLS}"
        )


def check_sync_write_ownership() -> None:
    """`features/sync.py` 里不许再有 INSERT 字面量 —— 写入口归各自的 owner service。

    同步那条链的四类写入各有主人：card → `RoleCards`、thread → `storage/threads`、
    memory → `core/memory.restore_row`、reachout → `features/reachout/inbox.restore_row`；
    `features/sync.py` 只剩"顺序与结果语义"（created/updated/foreign/skipped 的分派）。
    这条判据防的正是搬走的那半回来：**列集从前是手抄的第二份事实面** —— 表加了列而
    手抄清单没跟上，这条链静默少那一列（owner 里现在按 PRAGMA 现算，与 schema 同源）。

    两臂都判（`R102-37` 的"空转臂"纪律）：sync 里出现 `INSERT INTO` 常量即红；
    **两个 owner 的 INSERT 也必须真的在** —— 否则"sync 干净"是因为没人写了，
    那是另一场事故，不许绿。
    """
    src = ROOT / "src" / "rolecard_agent"

    def insert_literals(path: pathlib.Path) -> list[str]:
        found: list[str] = []
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            return found
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.lstrip().upper().startswith("INSERT INTO")
            ):
                found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
        return found

    sync_writes = insert_literals(src / "features" / "sync.py")
    # memory.py 的正文随 P3-7 第三刀成为 `core/memory/__init__.py`（路径按件拼装，
    # prose 级 `core/…` 扫描看不见这一处 —— 崩在 FileNotFoundError 才被点名）。
    memory_writes = insert_literals(src / "core" / "memory" / "__init__.py")
    inbox_writes = insert_literals(src / "features" / "reachout" / "inbox.py")
    hollow = not memory_writes or not inbox_writes
    ok = not sync_writes and not hollow
    detail = (
        "features/sync.py 0 处 INSERT，memory/inbox 两个 owner 各自的写入都在"
        if ok
        else (
            f"sync 里回来了：{sync_writes}" if sync_writes else f"owner 被掏空：{hollow}"
        )
    )
    out("sync write ownership", ok, detail)
    if sync_writes:
        fails.append(
            "features/sync.py is hand-writing SQL again: "
            f"{sync_writes}（写入口归 owner：memory/inbox 的 restore_row 按现算列集落库）"
        )
    if hollow:
        fails.append(
            "sync write ownership is hollow: owner INSERT literals missing "
            f"(memory={len(memory_writes)}, inbox={len(inbox_writes)}) —— "
            "'sync 干净'若是因为没人写了，那是另一场事故"
        )


#: 写语句所在、但**本函数自己不结束事务**的那九个位置（`R102-03` 沿袭项的盘点结果）。
#:
#: 逐处读过才登记：每一条都是"辅助函数由调用方收口"的形状 —— 写在这里的意义不是
#: "这些地方可以悬挂"，而是**新增一处这样的写法必须先过一道判断**（要么自己 commit，
#: 要么进这份名单并说清调用方在哪一步收口）。`R102-42` 那族（0 行的写也开了事务、
#: 抛之前不结束）的教训就是"同一句理由散在几处、注释传到第二处就停"；这份名单把
#: "谁收口"变成判据而不是记忆。
#:
#: 判据只数**代码里的字符串常量**（注释/docstring 里提到表名不算），并跳过协议声明
#: （函数体只有 `...` 或 `pass` —— 那是契约，不是写点：`DomainQueryService.create_report`
#: 就是被这样误收过一次）。
WRITE_TXN_HELPERS = frozenset(
    {
        "src/rolecard_agent/core/storage/checkpointer.py::_set_flag",
        "src/rolecard_agent/core/storage/checkpointer.py::_drop_orphan_writes",
        "src/rolecard_agent/core/memory/memory_distill.py::extract",
        # model_settings 拆包后（2026-10-04 审查快照 P1-6）这个方法住在 read mixin 里。
        "src/rolecard_agent/core/model_settings/read.py::_write_chat_refs",
        "src/rolecard_agent/core/plugins/__init__.py::_bump_tool_epoch",
        # 2026-10-04 sync 写入口归 owner：这两段从 `core/sync.py` 的 `_write_memory` /
        # `_write_reachout`（该模块后迁 `features/`）搬进各自的 owner，**不收口**的性质不变
        # —— 与随后的导入共用
        # 一个事务，收口点仍是 `features/sync_service.run_import`。memory 的插入半边是
        # 模块级 `_restore_insert`（写语句在它体内；闭包的名字进不了这份名单）。
        "src/rolecard_agent/core/memory/__init__.py::_restore_insert",
        "src/rolecard_agent/core/memory/__init__.py::restore_row",
        # 2026-10-04 P1-6（迁移注册表）：形状迁移从 `storage/db.py::_migrate` 搬进
        # `core/migrations.py` 的步骤，**收口纪律随代码一起搬** —— 每步自己收口（上面
        # `_backfill_service_endpoint_owner` 那一族）或点名收口点（下面两条）。
        # 这一步的 COMMIT 活在 executescript 里（`… ;COMMIT;` 首尾 BEGIN IMMEDIATE 包死），
        # 正是"端点配置不许半路清空"那条迁移的核心设计 —— AST 只看得见函数调用，所以登记。
        "src/rolecard_agent/core/storage/migrations.py::_rebuild_legacy_service_endpoint",
        "src/rolecard_agent/features/reachout/inbox.py::restore_row",
        # 2026-10-04 service 收口：replace 档的行类清空**刻意不收口** —— 与随后的导入共用
        # 一个事务，成败一体。收口点在 `features/sync_service.py::run_import`（导入有失败即
        # rollback + 抛 ReplaceAborted 交路由翻 400，成功则统一 commit）。从前这段 SQL 住在
        # `api/routers/sync.py` 里 —— 那正是 router 长成事实 service 的那一格。
        # 注：登记的是**含写语句字面量的那一个函数**（判据按 AST 里的 SQL 常量数），
        # `sync_service.clear_rows_for_replace` 只是转调，它自己不带语句所以不进名单。
        "src/rolecard_agent/storage/sync_rows.py::delete_rows_for_user",
        "src/rolecard_agent/storage/threads.py::delete_threads_for_user",
        "src/rolecard_agent/storage/threads.py::set_current_role",
        # 2026-10-05 冗余计数（镜像探针那条）：两个写函数**刻意不收口** —— 增量与
        # 真相同批：bump 由检查点写入口的调用方收口（chat 轮走 `_after_turn_chat` /
        # 编辑重生成钩子 `_bump_count_committed`，编辑与删除 rides `delete_messages`
        # 的 touch+commit，上传说明与主动投递各自紧跟 commit）；record 由全量
        # `/messages` 真读后的对账点（`get_session_messages`）commit。分开收口是
        # 刻意的：调用方各自的检查点写与计数写要能落进同一个本地事务。
        "src/rolecard_agent/storage/threads.py::bump_message_count",
        "src/rolecard_agent/storage/threads.py::record_message_count",
        # 换线程 id 的落笔（2026-10-04 P1-6 从 `storage/db.py::_migrate` 搬来）：
        # **刻意不收口** —— 它跑在整表重建的显式事务里（core/migrations.py 的 B2 步骤
        # 首尾 BEGIN/COMMIT），自己 commit 会把 DROP→RENAME 之间的窗口重新打开。
        # 与 `set_current_role` 同族：不收口是因为调用方要用 rowcount/事务判成败。
        "src/rolecard_agent/storage/threads.py::rename_thread_id",
    }
)


def check_write_txn_ownership_inventory() -> None:
    """"哪个产品写点会留未提交事务"从此有一份被看着的名单（`R102-03` 沿袭的那一角）。

    第 47 条断言 `dangling write txn` 管的是"判了 rowcount 那一支有没有先结束事务"；
    这一条管的是**另一支**：函数体里有写语句、而本函数既不 commit 也不 rollback ——
    这类写法本身不坏（helper 交给调用方收口是常见形状），坏的是**没人知道有几处**。
    10-03 的盘点给出 10 个候选，逐个读上下文后 9 处登记、1 处是协议声明（不是写点）。

    两个方向都要能红：新增了没登记的红，登记着却已经不在了也红（`R102-37` 的空转臂教训）。
    """
    src = ROOT / "src" / "rolecard_agent"
    found: set[str] = set()
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            writes = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and re.search(r"\b(INSERT INTO|UPDATE\s+\w+|DELETE FROM)\b", n.value)
            ]
            if not writes:
                continue
            ends = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"commit", "rollback"}
                for n in ast.walk(fn)
            )
            if ends:
                continue
            real_body = [
                n
                for n in fn.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            ]
            if not real_body or (len(real_body) == 1 and isinstance(real_body[0], ast.Pass)):
                continue  # 协议/抽象声明：契约不是写点
            found.add(f"{rel}::{fn.name}")

    unregistered = sorted(found - WRITE_TXN_HELPERS)
    stale = sorted(WRITE_TXN_HELPERS - found)
    ok = not unregistered and not stale
    detail = (
        f"{len(found)} 个「写而不收口」的位置全在名单内"
        if ok
        else f"未登记：{unregistered}；名单里已经不在了：{stale}"
    )
    out("write txn ownership inventory", ok, detail)
    if unregistered:
        fails.append(
            "new function writes without ending the transaction and is not registered: "
            f"{unregistered}（要么自己 commit/rollback，要么进 "
            "check_consistency.WRITE_TXN_HELPERS 并说清调用方在哪一步收口）"
        )
    if stale:
        fails.append(
            f"WRITE_TXN_HELPERS has entries that no longer match a write site: {stale}"
        )


def check_single_text_extractor() -> None:
    """消息取文本只允许一处实现：`base/text.py::text_of`（架构审计报告 台账 `R28-59`）。

    这条断言防的是两类复发：
      1. **就地复刻** —— `domains/health/extract.py` 曾抄了第二份，且用空串拼接而不是空格，
         于是同一条消息在"流式输出"与"抽取解析"两条路上渲染成不同文本；
      2. **裸取消息体** —— 把多模态/流式形态下的分块列表直接字符串化，得到的是 Python
         repr（方括号花括号那一串）。那份 repr 曾进过 guard、用户收件箱，以及"增强提示词"
         贴回输入框的文本。
    所以除了"不许有第二份定义"，两种裸取写法也一起挡掉：先 getattr 取 content 再整体
    字符串化、以及拿 content 属性兜一个空串当文本用。它们正是上面那个 bug 的形状。
    """
    offenders: list[str] = []
    for path in iter_files(".py"):
        if "tests" in path.parts:
            continue  # 测试里为验证降级行为而手工构造怪形状，是刻意的
        rel = path.relative_to(ROOT).as_posix()
        if rel == "src/rolecard_agent/base/text.py":
            continue  # 唯一实现本尊
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            redefined = re.search(r"^\s*def _(?:text_of|serialize_text|content_text)\b", line)
            stringified = re.search(r"\bstr\(\s*getattr\([^()]*,\s*[\"']content[\"']", line)
            empty_defaulted = re.search(r"\.content\s+or\s+[\"'][\"']", line)
            if redefined or stringified or empty_defaulted:
                offenders.append(f"{rel}:{lineno}")
    detail = "single implementation" if not offenders else str(offenders[:6])
    out("single text extractor", not offenders, detail)
    if offenders:
        fails.append(f"message text must be read via base/text.py::text_of only: {offenders}")


def find_dangling_write_txns(root: pathlib.Path | None = None) -> list[str]:
    """找出"判了 `rowcount` 却在那条路上不结束事务"的位置（`R102-42`）。

    为什么这条需要尺子而不是注释：SQLite 在写语句前隐式 BEGIN，**改到 0 行的 `UPDATE`
    同样开了一个写事务**；而 `if cur.rowcount == 0: raise ...` 这一路走不到 `commit()`。
    于是那把 RESERVED 锁留在调用它的那条线程上 —— 线程池里的线程不死，锁就没有来路可解，
    表现是同一台机器上后续所有写请求等 5 秒一起 `database is locked`。
    本仓早就知道这件事（`domains/health/service.py:365,412` 的注释原话是"未命中也要结束事务，
    否则悬挂的写事务会堵住别的线程"），**却只在两处做了**：同族另外五处漏着 ——
    这正是"一个形状靠注释传下去"的下场，所以它归尺子管。

    判据（只看 `if` 的测试里比较了 `X.rowcount` 的那种）：`body` 与 `orelse` 两个分支里，
    凡在第一个 `commit()` / `rollback()` **之前**就出现 `raise` 或 `return` 的，算一条。
    """
    base = root if root is not None else ROOT / "src" / "rolecard_agent"
    offenders: list[str] = []
    for path in sorted(base.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            tested = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
            if "rowcount" not in tested:
                continue
            for branch in (node.body, node.orelse):
                ended = False
                for stmt in branch:
                    names = {
                        n.attr
                        for n in ast.walk(stmt)
                        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                    }
                    if names & {"commit", "rollback"}:
                        ended = True
                    if any(isinstance(s, (ast.Raise, ast.Return)) for s in _statements_of(stmt)):
                        if not ended:
                            offenders.append(f"{rel}:{stmt.lineno}")
                        break
    return offenders


def _statements_of(node: ast.stmt) -> list[ast.stmt]:
    """这一个语句"自己或最前面的一层"里包含的语句 —— 用来判 `if ...: raise` 这种一行体。"""
    if isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
        inner: list[ast.stmt] = [node]
        inner.extend(getattr(node, "body", []))
        inner.extend(getattr(node, "orelse", []))
        for handler in getattr(node, "handlers", []):
            inner.extend(handler.body)
        return inner
    return [node]


def check_dangling_write_txns() -> None:
    """门禁那一格：把 `find_dangling_write_txns` 的结果报出来。"""
    offenders = find_dangling_write_txns()
    out(
        "dangling write txn",
        not offenders,
        "；".join(offenders[:6])
        + (f"（共 {len(offenders)} 处）" if len(offenders) > 6 else "")
        if offenders
        else "所有判 rowcount 的分支都在抛/回之前结束了事务（写语句即便改到 0 行也开了事务）",
    )
    if offenders:
        fails.append(f"rowcount branches that leave a write transaction open: {offenders}")


def check_shape_migration_ddl() -> None:
    """搬层用的暂存表 DDL 必须与声明面**逐列同形**（架构审计 2026-10-02 轮 `R102-11`）。

    `core/model_settings/migration.py` 的 `CREATE TABLE model_backend__layers` 把
    `core/schema.sql` 里 `model_backend` 的十列又抄了一遍，而 `SHAPE_TABLES` 让补列器对这张表
    **不动手** —— 两份 DDL 一旦分叉，"旧库那份抄的"就赢：新库有列、搬完层的旧库没列，
    读侧 `no such column`（`repeat_penalty` 那一发的教训，文件注释自己记着）。
    这把尺子就是那句"逐列相等"的兑现：分叉当场红，不再等老库升上来才炸。
    """
    schema_text = (ROOT / "src" / "rolecard_agent" / "core" / "schema.sql").read_text(
        encoding="utf-8"
    )
    ms_text = (
        ROOT / "src" / "rolecard_agent" / "core" / "model_settings" / "migration.py"
    ).read_text(encoding="utf-8")

    def declared_columns(create_block: str) -> set[str]:
        """从 CREATE TABLE 的列定义区抓列名（跳过约束行、SQL `--` 注释、python 串的引号）。"""
        names: set[str] = set()
        for raw in create_block.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue  # model_settings.py 的抄写现场里，python 注释行夹在串与串之间
            line = re.sub(r"--.*$", "", line).strip().strip('"').rstrip(",").strip()
            if not line or re.match(
                r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", line, flags=re.I
            ):
                continue
            names.add(line.split()[0])
        return names

    schema_m = re.search(r"CREATE TABLE IF NOT EXISTS model_backend \((.*?)\);", schema_text, re.S)
    layer_m = re.search(r'CREATE TABLE model_backend__layers \((.*?)\)"', ms_text, re.S)
    if not schema_m or not layer_m:
        fails.append("shape migration DDL: 判据的两个锚点（schema.sql / model_settings.py）没抓到")
        out("shape migration DDL", False, "anchor missing")
        return
    declared = declared_columns(schema_m.group(1))
    copied = declared_columns(layer_m.group(1))
    drift = sorted(declared ^ copied)
    out(
        "shape migration DDL",
        not drift,
        "；".join(drift[:8])
        if drift
        else f"model_backend__layers 与 model_backend 逐列相等（{len(declared)} 列）",
    )
    if drift:
        fails.append(
            "model_backend__layers DDL drifted from schema.sql: "
            f"{drift} —— 搬完层的旧库会 no such column；改列要两边一起改"
        )
