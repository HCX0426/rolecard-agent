"""迁移步骤注册表（core 层）：一次 schema 变更只在这一处登记。

为什么存在（2026-10-04 审查快照 P1-6「迁移与服务逻辑 split-brain」）：从前形状迁移的
**引擎**住在 `storage/db.py`（`_migrate` 两百多行业务 SQL）、**搬层**那一步住在
`core/model_settings.py`、暂存表名与审批清重又散在 storage 的 bootstrap 序列里 —— 一次
schema 变更要在两个包里找代码，而 storage 里长满了业务表名（`core/ no domain token` 管
core 不管 storage，那半边一直没有尺子）。

收口后的分工：

  * **storage**：连接、声明引擎（`reconcile_columns`）、DDL 执行入口（`bootstrap(plan=…)`）。
    它只认识 `MigrationPlanLike` 这个**形状**（见 `storage/db.py`），不认识任何步骤、
    任何业务表名；
  * **本模块**：步骤的**清单与顺序**（`Step(id, check, run)`）。业务表名全在这儿 ——
    加一步 = 在下面的元组里加一个 `Step`，storage 与各个调用方零改动；
  * **顺序是承重的**，两个阶段各自成序：
      `pre_ddl`（清重 / 滞留暂存表自愈）必须站在 schema DDL **之前** —— DDL 里的
        `CREATE UNIQUE INDEX` 踩到重复行就是"库再也打不开"（R28-15）；
      `shape`（整表重建 / 搬层）必须站在 DDL **之后**、第二遍补列**之前** —— 重建会
        DROP/RENAME，先补的列与先建的索引跟着旧表一起没。

`check` 返回 False = 这一步对当前库无需执行（判"是不是旧形态"）；`None` = 总是跑，
由 `run` 自己幂等地早退。两者都必须**幂等**：bootstrap 每次开机都跑一遍。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from rolecard_agent.core.model_settings import migrate_to_provider_layers
from rolecard_agent.storage.db import (
    SqlConnection,
    core_schema_path,
    migrate_event,
    repair_stranded_rebuild,
    table_columns,
)
from rolecard_agent.storage.threads import rename_thread_id

#: `(conn) -> 需要跑吗`：True = 这份库还是旧形态。None = 无条件跑（run 自己幂等早退）。
StepCheck = Callable[[SqlConnection], bool]
#: `(conn) -> None`：执行这一步。必须幂等（每次开机都跑）。
StepRun = Callable[[SqlConnection], None]


@dataclass(frozen=True, slots=True)
class Step:
    """一步迁移：名字 + 判据 + 动作。`id` 进事件流，出事时能指着它说"死在哪一步"。"""

    id: str
    run: StepRun
    check: StepCheck | None = None

    def applies(self, conn: SqlConnection) -> bool:
        return self.check is None or self.check(conn)


def _cols(conn: SqlConnection, table: str) -> set[str]:
    """这张表当前的列集合（不存在 = 空集，"判老形态"的第一问）。"""
    return set(table_columns(conn, table))


# --------------------------------------------------------------------------- pre_ddl 阶段
#
# 这三类都必须赶在 schema DDL 之前（顺序见模块文档）。函数体大半自判早退，
# 所以 check 一律 None —— 判据写在 run 里，与它要保护的那条 SQL 贴在一起。


def dedupe_pending_approvals(conn: SqlConnection) -> int:
    """建 `idx_command_approval_one_pending` 之前的清重（`R102-52`），返回收成 rejected 的行数。

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


def dedupe_ingestion_tasks(conn: SqlConnection) -> int:
    """建 `idx_ingestion_user_file` 之前的清重（2026-10-04 审查快照的上传幂等条目），返回删除行数。

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
    return max(cur.rowcount, 0)


def _run_dedupe_pending_approvals(conn: SqlConnection) -> None:
    removed = dedupe_pending_approvals(conn)
    if removed:
        migrate_event(f"清掉重复的 pending 审批 {removed} 行（同命令留最新）")


def _run_dedupe_ingestion_tasks(conn: SqlConnection) -> None:
    removed = dedupe_ingestion_tasks(conn)
    if removed:
        migrate_event(f"清掉重复的 ingestion_task {removed} 行（保留每组最新一条）")


#: 三张会做"整表重建"的表各自留下的暂存名：原表不在、暂存表在 ⇒ 上一次开机死在
#: DROP 之后 RENAME 之前（`R102-53`）。自愈的**判据**（形状）在 storage 的
#: `repair_stranded_rebuild`，**点名**这三对的是本模块 —— 谁的名字谁记得住。
STRANDED_REBUILDS: tuple[tuple[str, str], ...] = (
    ("role_proactive_state", "role_proactive_state__b2"),
    ("token_usage_day", "token_usage_day_new"),
    ("model_backend", "model_backend__layers"),
)


def _repair_stranded_rebuilds(conn: SqlConnection) -> None:
    """整表重建滞留现场的自愈（`R102-53`）。**必须站在 schema DDL 之前**：

    executescript 的 `CREATE TABLE IF NOT EXISTS` 会把被 DROP 掉的原表先建成一张空的
    新形表，随后"判老形态"永远为假 —— 滞留暂存表就永远没人管、还照常报绿。
    """
    for original, temp in STRANDED_REBUILDS:
        repair_stranded_rebuild(conn, original=original, temp=temp)


#: DDL 之前的阶段（顺序即执行序）。
PRE_DDL_STEPS: tuple[Step, ...] = (
    Step(id="pre.dedupe_pending_approvals", run=_run_dedupe_pending_approvals),
    Step(id="pre.dedupe_ingestion_tasks", run=_run_dedupe_ingestion_tasks),
    Step(id="pre.repair_stranded_rebuilds", run=_repair_stranded_rebuilds),
)


# --------------------------------------------------------------------------- shape 阶段
#
# 只负责"通用补列器补不了"的那些事（幂等、可重跑）。列级缺列不再手写 ALTER ——
# `reconcile_columns` 每次都按 `schema.sql` 的声明比对（R26-04：12 处手写 ALTER 全在核心
# 表上，加列忘了写**不会红**，只会在第一次读那列时炸）。


def _model_backend_is_legacy(conn: SqlConnection) -> bool:
    """旧形态 = 列清单里没有 `provider_id`（与 `migrate_to_provider_layers` 同一把尺）。"""
    cols = _cols(conn, "model_backend")
    return bool(cols) and "provider_id" not in cols


def _backfill_model_backend_history_columns(conn: SqlConnection) -> None:
    """旧形态库先把历史缺列补齐（这些列曾经分三次 ALTER 加过），再交给搬层迁移。

    必须限定"这是旧形态"才补 —— 新库里 `model_backend` 已经没有 usage 列，无条件补一次
    就是把它加回来。能力位**必须留 NULL**：NULL 是"没测过"，而 `schema.sql` 与界面（`?`）
    都按这个口径走。旧写法回填成 `NOT NULL DEFAULT 0/1`，等于替每个升级上来的用户回答了
    两个没人问过的问题 —— 界面上从此显示"✗ 不支持视觉 / ✓ 支持工具"，看着像测过。
    运行时不受影响（`_vision_of(NULL)=False`、`_tools_of(NULL)=True` 与旧默认同值），
    所以这是一次纯"别撒谎"的修正。
    """
    cols = _cols(conn, "model_backend")
    for name, ddl in {
        "usage": "TEXT NOT NULL DEFAULT 'chat'",
        "num_ctx": "INTEGER",
        "supports_vision": "INTEGER",
        "supports_tools": "INTEGER",
    }.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE model_backend ADD COLUMN {name} {ddl}")


def _ensure_role_proactive_state(conn: SqlConnection) -> None:
    """关系驱动主动开口（架构总览 §5）：per-role 状态表（幂等建表）。

    这张表的列仍由 `reconcile_columns` 补（不在 `SHAPE_TABLES` 里），只有
    **主键换 (user_id, role_id)** 是形状迁移，走下面那一步的重建。
    """
    if "affinity" not in _cols(conn, "role_proactive_state"):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS role_proactive_state ("
            " role_id TEXT PRIMARY KEY, affinity REAL NOT NULL DEFAULT 0.0,"
            " last_interaction_utc TIMESTAMP, calibration_json TEXT,"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )


def _upgrade_role_proactive_state_pk(conn: SqlConnection) -> None:
    """role_proactive_state 的主键升级（多租户 B2）：(role_id) → (user_id, role_id)。

    `role_card` 的主键将来要改成 (user_id, role_id)（第二个身份也可能建一张同名卡），
    状态表不跟着换就撞行。整表重建 + 老行归属实例主人（默认部署 = 'local-user'，
    与 base/identity.DEFAULT_USER_ID 一字不差）。判"老形态"用有没有 user_id 列。

    顺带把**主动会话线程 id 改成带身份**（s_proactive_<role> → s_proactive_<uid>_<role>）：
    同一个 role_id 将来可以属于两个人，线程 id 不带身份就会让两人的主动会话互相覆盖。
    老线程的归属从 session_thread.user_id 现读（数据即真相，不必知道 IDENTITY_USER_ID）；
    checkpoints/writes 的 thread_id 一起改（那是她主动说过的历史，不改就断了上下文）。
    幂等：新 id == 旧 id（已带身份）的不动；新库没有 legacy 行一轮跑过。
    """
    if "user_id" in _cols(conn, "role_proactive_state"):
        return
    # 滞留暂存表的现场自愈在 pre_ddl 阶段已经做过（那里才赶得在 schema DDL 前面）。
    # 前置 DROP：与 B1b 那一处同一个理由（`R28-15`，红档）。这一步死在半路的话，
    # 残留的空 `__b2` 会让下次启动的 CREATE 报 `already exists`，bootstrap 永久打不开。
    # 整段重建（含下面的线程 id 换名）包进**一个显式事务**：DDL 在 legacy autocommit
    # 下逐句落盘，显式 BEGIN 让"半路死"整体回滚 —— DROP→RENAME 之间的窗口随事务消失。
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    migrate_event("role_proactive_state 主键重建开始（B2，显式事务）")
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
    for row in conn.execute(
        "SELECT thread_id, user_id FROM session_thread "
        "WHERE thread_id LIKE 's_proactive_%'"
    ).fetchall():
        tid = str(row["thread_id"])
        uid = str(row["user_id"])
        if tid.startswith(f"s_proactive_{uid}_"):
            continue  # 已带身份（本步跑过 / 新库建的）
        # 落笔在 owner（`session_thread write seam` 只认 storage），判断在这里：
        # 带 thread_id 的表全跟着走（`R28-23`）—— 审批、检查点、writes，以及将来任何新表，
        # 判据是库的形状，不是这段代码记得列了几张。
        rename_thread_id(conn, old=tid, new=f"s_proactive_{uid}_{tid[len('s_proactive_'):]}")
    conn.commit()  # 重建原子落盘（`R102-53`）
    migrate_event("role_proactive_state 主键重建完成（含线程 id 换名）")


def _ensure_role_memory(conn: SqlConnection) -> None:
    """role_memory（"一坨文本"时代的记忆表）与 role_memory_item（可逐条退役的条目）。

    老库里的 role_memory / memory:facts 两块 blob **不迁移**（用户 2026-09-20："旧的记忆
    数据也可以不要了"）—— 旧表原样留着不删列（迁移纪律），只是不再是事实面。
    """
    if "value" not in _cols(conn, "role_memory"):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS role_memory ("
            " role_id TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '',"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    if "text" not in _cols(conn, "role_memory_item"):
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


def _drop_service_policy(conn: SqlConnection) -> None:
    """service_policy 已退役（策略并入 service_endpoint 行内 enabled/sort_order）→ DROP。"""
    conn.execute("DROP TABLE IF EXISTS service_policy")


def _upgrade_token_usage_pk(conn: SqlConnection) -> None:
    """token_usage_day 的主键升级（多租户 B1a）：(day, backend) → (day, user_id, backend)。

    补列改不了主键，也没有"PRAGMA 改 PK"这回事 —— 只能整表重建。旧行全部归属本机那份
    （语义 = 上线前测的本机用量，不是谁漏账）；新行由 `usage.record_usage(user_id=…)` 按
    花谁的 key 落格。判断"旧形态"用有没有 user_id 列（与列级迁移同口径）。
    """
    if "user_id" in _cols(conn, "token_usage_day"):
        return
    # 前置 DROP（R28-15 同族）+ 显式事务包整段重建（`R102-53`）：
    # "半路死"要么整体回滚、要么在下一次开机被早期的自愈接住。
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    migrate_event("token_usage_day 主键重建开始（B1a，显式事务）")
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
    migrate_event("token_usage_day 主键重建完成")


def _rebuild_legacy_service_endpoint(conn: SqlConnection) -> None:
    """service_endpoint 旧形态（行内嵌 key/base_url 的"实例"模型）→ 整表重建为
    「引用 model_backend」的新形态；旧行配置属演示数据且引用化后由模型页承接，直接弃用。
    **必须连 seed flag 一起清**，否则 seed_once 会以为播过种而跳过，留下一张空表
    （实测踩过：引用行全部缺失）。

    重建段包进**单个事务**（2026-10-04 审查快照的迁移事务条目）：从前 DROP 与
    executescript 分属两个隐式事务，死在中间 = 下次启动靠 IF NOT EXISTS 重建出**空表**、
    seed 标志已删照常重播 —— 端点配置静默清空，无人被提示。executescript 会先隐式
    COMMIT，所以把 DROP/清标志与整份 core schema 拼成一段脚本、首尾 BEGIN IMMEDIATE/COMMIT
    包死（schema.sql 无 TRIGGER/PRAGMA，逐条语句在 SQLite 里本就事务化）。
    """
    if "api_key" not in _cols(conn, "service_endpoint"):
        return
    core = core_schema_path()
    conn.executescript(
        "BEGIN IMMEDIATE;"
        "DROP TABLE service_endpoint;"
        "DELETE FROM kernel_meta WHERE key = 'service_endpoints_seeded';"
        + core.read_text(encoding="utf-8")
        + ";COMMIT;"
    )
    migrate_event("service_endpoint 整表重建完成（单事务，配置零丢失）")


def _backfill_service_endpoint_owner(conn: SqlConnection) -> None:
    """service_endpoint 的归属（多租户 B1b，方案 A）：加可空 user_id 并回填历史 chat 行。

    补列器不碰这张表（它在 `SHAPE_TABLES` 里，整表重建/搬层族），所以这里手写一次。
    只对新形态库生效 —— 老形态（带 api_key）走上面那句 DROP 重建，新表已带列。
    语义（schema.sql 有全文）：**仅 chat 引用行按人**（默认/回退链 = 谁花 key 由谁定），
    能力端点（ocr/embedding/rerank）永远设备级、user_id 留 NULL。老 chat 行回填本机主人
    （默认部署 = 'local-user'，与 `base/identity.DEFAULT_USER_ID` 一字不差 —— 漂了就是
    "升级完对话默认丢失"那种最像默认值出问题的症状）。新 chat 行由 model_settings 的写
    入方显式带主人，所以这里只回填历史行。
    """
    if "user_id" in _cols(conn, "service_endpoint"):
        return
    conn.execute("ALTER TABLE service_endpoint ADD COLUMN user_id TEXT")
    conn.execute("UPDATE service_endpoint SET user_id = 'local-user' WHERE category = 'chat'")
    # 自己收口：这一步必须**独立成账** —— 依赖"后面某一步会 commit"就是把这列的回填
    # 绑在别的步骤的生命周期上（下一步抛掉时，这列就静默丢了）。
    conn.commit()


def _run_provider_layers(conn: SqlConnection) -> None:
    """model_backend 两层化（凭据上收 model_provider、usage 变成 chat 引用行）。

    必须排在 service_endpoint 重建**之后**（搬层要往新形态的引用表里写 chat 行）、
    `idx_model_backend_provider` 创建**之前**（搬层会 DROP/RENAME 重建 model_backend，
    先建的索引跟着表一起没了）。顺序是承重的，所以它必须是注册表里的一步，而不是
    "调用方在 bootstrap 之后自己搬一次"。

    由 `migrate_to_provider_layers`（`core/model_settings.py`）执行；`build_plan` 允许
    调用方替掉这一步的动作（测试注入 / 将来换实现），形状与顺序不动。
    """
    migrate_to_provider_layers(conn)


def _create_provider_index(conn: SqlConnection) -> None:
    """索引在搬层之后建：搬层会 DROP/RENAME 重建 model_backend，先建的索引会没。"""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_backend_provider ON model_backend"
        "(provider_id, sort_order, name)"
    )


def _backfill_penalty_columns(conn: SqlConnection) -> None:
    """model_backend 增列采样惩罚三档（设计稿 §8.2 的补课：只露过 num_ctx/temperature）。

    排在搬层**之后**：搬层会重建这张表，先补的列跟着旧表一起没了（与上面那条索引同理）。
    三档都可空，NULL = 不传该参数 = 引擎默认（Ollama 出厂 repeat_penalty=1.1，写成 0 是
    "把它关了"，与"没设"是两种行为 —— 所以这里不用 DEFAULT 0）。
    """
    cols = _cols(conn, "model_backend")
    for name in ("repeat_penalty", "frequency_penalty", "presence_penalty"):
        if name not in cols:
            conn.execute(f"ALTER TABLE model_backend ADD COLUMN {name} REAL")


def _backfill_memory_item_uid(conn: SqlConnection) -> None:
    """记忆条目的跨机器身份 `uid`（09-27 轮 M2b）。

    补列器能把**列**长出来，但填什么值是数据不是形状 —— 老行的 uid 必须在这里补上
    uuid4，否则"哪些条目能跟云端对账"这件事没有答案，上行只剩"整表覆盖"那一条会丢数据
    的路。幂等：只碰 NULL/空串。
    """
    if "uid" not in _cols(conn, "role_memory_item"):
        return
    rows = conn.execute(
        "SELECT id FROM role_memory_item WHERE uid IS NULL OR uid = ''"
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE role_memory_item SET uid = ? WHERE id = ?",
            (uuid.uuid4().hex, int(r[0])),
        )
    conn.commit()


def _rename_paddle_ocr_endpoint(conn: SqlConnection) -> None:
    """OCR 引擎换家（10-02：Paddle → RapidOCR）：内置行的 **id 就是事实面**。

    `select_ocr_backend` 按 id 匹配候选、`check_availability` 按 id 分派探活、服务页把 id
    当标签来源 —— 旧库里那行 `paddle` 不改名，症状不是报错而是**静默降级**：服务页显示
    "本地 OCR 就绪"，选择器却永远匹配不上任何候选，`order` 里剩下的都是不可用的行，
    图片一路停在 pending，看起来像"这张图没识别出来"（与 rag/ocr.py 里"空 order 不抛"
    那条同一种阴）。

    幂等：只在真存在 `paddle` 行时动手。两种终态都对 —— 已有 `rapidocr` 行（新库、或
    播种已重播过）就删旧行，绝不撞 (category, id) 主键；没有就把旧行**改名带过去**，
    enabled / sort_order / user_id 一字不动：操作员在这行上做过的启停与排序，不该因为
    换了个引擎就被重置。
    """
    has_old = conn.execute(
        "SELECT 1 FROM service_endpoint WHERE category = 'ocr' AND id = 'paddle'"
    ).fetchone()
    if not has_old:
        return
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


#: 通用补列器**必须避开**的表（每张都对应下面 shape 阶段里的一步整表重建/搬层）：
#: 提前给旧形 `model_backend` 补上 `provider_id`，"没有 provider_id 就是旧形态"的判定
#: 当场失效 —— 搬层被跳过，旧行的凭据静静留在没人再读的列里。`token_usage_day` 不是列
#: 的问题是**主键**（B1a）：补列改不了 `(day, backend)` → `(day, user_id, backend)`，
#: 只补列会让 `ON CONFLICT(day,user_id,backend)` 永远报 "non-unique"。`service_endpoint`
#: 需要**补列 + 按行回填**两步（B1b）—— 补列器只管加列、不负责回填数据，提前补了
#: 旧 chat 行就是 NULL，按人过滤后对话默认当场消失。
#:
#: `role_proactive_state` **刻意不在**这张表里（与搬家前的 `_SHAPE_MIGRATED_TABLES` 逐字
#: 一致）：它缺的 `user_id` 是主键列，补列器本来就跳过 pk 列（`decl["pk"]`），判据不受污染；
#: 而非 pk 的那几列（open_threads / recall_at）先补上也无害 —— B2 重建的 INSERT 会照抄。
SHAPE_TABLES: frozenset[str] = frozenset(
    {"model_backend", "service_endpoint", "token_usage_day"}
)


#: DDL 之后的阶段（顺序即执行序；哪一步排在前的理由写在各自 docstring 里）。
SHAPE_STEPS: tuple[Step, ...] = (
    Step(id="shape.model_backend_history_columns", run=_backfill_model_backend_history_columns,
         check=_model_backend_is_legacy),
    Step(id="shape.ensure_role_proactive_state", run=_ensure_role_proactive_state),
    Step(id="shape.role_proactive_state_pk", run=_upgrade_role_proactive_state_pk),
    Step(id="shape.ensure_role_memory", run=_ensure_role_memory),
    Step(id="shape.drop_service_policy", run=_drop_service_policy),
    Step(id="shape.token_usage_day_pk", run=_upgrade_token_usage_pk),
    Step(id="shape.rebuild_service_endpoint", run=_rebuild_legacy_service_endpoint),
    Step(id="shape.service_endpoint_owner", run=_backfill_service_endpoint_owner),
    Step(id="shape.model_backend_provider_layers", run=_run_provider_layers,
         check=_model_backend_is_legacy),
    Step(id="shape.model_backend_provider_index", run=_create_provider_index),
    Step(id="shape.model_backend_penalties", run=_backfill_penalty_columns),
    Step(id="shape.role_memory_item_uid", run=_backfill_memory_item_uid),
    Step(id="shape.ocr_paddle_to_rapidocr", run=_rename_paddle_ocr_endpoint),
)


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """交给 `storage.db.bootstrap(plan=…)` 的那份计划（结构性满足 `MigrationPlanLike`）。

    拆成两个阶段 + 一份"补列器要避开的表"，是 storage 唯一需要知道的三件事 —— 它不认识
    `Step`、不认识任何业务表名。
    """

    #: DDL 之前（清重 / 滞留自愈）。
    pre_ddl: tuple[Step, ...] = ()
    #: DDL 之后（整表重建 / 搬层族），顺序承重。
    shape: tuple[Step, ...] = ()
    #: 通用补列器第一遍必须避开的表（见 `SHAPE_TABLES`）。
    shape_tables: frozenset[str] = frozenset()

    def run_pre_ddl(self, conn: SqlConnection) -> None:
        _run_steps(conn, self.pre_ddl)

    def run_shape(self, conn: SqlConnection) -> None:
        _run_steps(conn, self.shape)


def _run_steps(conn: SqlConnection, steps: tuple[Step, ...]) -> None:
    for step in steps:
        if step.applies(conn):
            step.run(conn)


def build_plan(
    *,
    pre_ddl: tuple[Step, ...] = PRE_DDL_STEPS,
    shape: tuple[Step, ...] = SHAPE_STEPS,
    shape_tables: frozenset[str] = SHAPE_TABLES,
    provider_layers: Callable[[SqlConnection], int] | None = None,
) -> MigrationPlan:
    """组装一份计划。`provider_layers` 换掉"搬层"那一步的动作（测试注入 / 将来换实现）——
    只换动作不换位置：它在注册表里的次序（service_endpoint 重建之后、索引之前）是承重的。
    """
    if provider_layers is None:
        return MigrationPlan(pre_ddl=pre_ddl, shape=shape, shape_tables=shape_tables)
    replaced = tuple(
        Step(id=step.id, run=_wrap_provider_layers(provider_layers), check=step.check)
        if step.id == "shape.model_backend_provider_layers"
        else step
        for step in shape
    )
    return MigrationPlan(pre_ddl=pre_ddl, shape=replaced, shape_tables=shape_tables)


def _wrap_provider_layers(
    action: Callable[[SqlConnection], int],
) -> StepRun:
    def _run(conn: SqlConnection) -> None:
        action(conn)

    return _run


#: 生产入口用的默认计划（core/bootstrap、scripts/init_db 都传它）。
MIGRATION_PLAN: MigrationPlan = build_plan()

__all__ = [
    "MIGRATION_PLAN",
    "PRE_DDL_STEPS",
    "SHAPE_STEPS",
    "SHAPE_TABLES",
    "MigrationPlan",
    "Step",
    "build_plan",
]
