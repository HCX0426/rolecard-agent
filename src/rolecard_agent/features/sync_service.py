"""同步写入侧的服务（2026-10-04 service 收口第一步：sync 那段先从路由里搬出来）。

`api/routers/sync.py` 从前自己写 SQL、自己决定回滚 —— 于是"整份替换"这条最危险的链
（备份 → 清空 → 导入 → 失败整批回滚）是**路由的私有函数**：非 HTTP 宿主（桌宠壳、
`scripts/` 的取证与真机探针）想复用只能再抄一遍，而抄的那一份不跟着事务边界一起改。
归属按本仓一贯的两层：**语句在 `storage/`，顺序与事务在这里，HTTP 语义在路由**。

三条不能动的判据（都在下面的函数里，搬走时别顺手"简化"）：

  1. **先落盘、再删**（`dump_before_clear`）：中途崩掉的可接受结果是"行还在库里 + 多一份
     备份"，绝不该是"行没了 + 无处可找"。
  2. **清空与导入共一个事务**（`clear_rows_for_replace` 刻意不 commit，由 `run_import`
     收口）：这是"清了不导"唯一的原子化办法。card 的写入器（`RoleCards`）内部自 commit，
     它那部分收不回来 —— 所以下面的 `ReplaceAborted.detail` 必须如实点名这一格，
     而不是让用户以为"全部还原"。
  3. **thread 的清空走图侧、罩不进事务**：所以它先清、由删前备份兜底，并且**锁在级联删外面**
     （在飞轮次不该被从脚下抽走检查点）。

`ReplaceAborted` 是新加的异常族成员（错误收口那一族）：服务只说"导入有几条失败、
清掉了多少行"，**由路由翻成 400**。状态码不是业务事实，把它写进服务层就等于让 service
认识 HTTP。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rolecard_agent.core.thread_locks import thread_write
from rolecard_agent.features import sync as sync_lib
from rolecard_agent.storage import sync_rows
from rolecard_agent.storage.db import (
    RETENTION_BACKUP_DIRNAME,
    SqlConnection,
    dump_rows_to_jsonl,
    trim_backups,
)
from rolecard_agent.storage.threads import (
    delete_thread_everywhere,
    delete_threads_for_user,
    thread_id_carriers,
)

#: 整份替换的删前备份每张表各留几份。与 retention 同一套哲学，但落在 `sync/` 子目录：
#: `trim_backups` 按目录 glob，两边互不清对方的账（谁裁谁的备份必须只有一处答案）。
SYNC_BACKUP_KEEP = 5

#: 协议里每一类**住在哪张表**。这份映射管两类：按身份的清空按它删（`DELETABLE_KINDS` 是它的
#: 子集）、按身份的备份按它读（card/memory/reachout 三类 + 会话载体行 `session_thread`）。
#: **唯独"按 thread_id 归的那一族"不在这里数** —— 备份与级联删共用 `thread_id_carriers`
#: （现数），因为那张名单会跟着库的形状自己长，而手抄的清单不会（抄的那份迟早"备份少一族"）。
#: 从前这里躺着三份手写字典（备份三张表、清空三张表、会话那半又四张表）—— 加一类时
#: 漏改哪一份都不报错，症状是"备份少一族"或"清空漏一族"，只有真跑一次整份替换才看得见。
#: `session_thread` 在备份里是必须的：检查点那些表只有 blob 与 thread_id，少这一行元数据
#: 就还原不出归属 —— 备份齐不齐的判据是"能不能重放"，不是"表数对不对"。
KIND_TABLE: dict[str, str] = {
    sync_lib.KIND_CARD: "role_card",
    sync_lib.KIND_MEMORY: "role_memory_item",
    sync_lib.KIND_REACHOUT: "agent_reachout",
    sync_lib.KIND_THREAD: "session_thread",
}

#: 能"按身份整类清空"的那几类。差别只有 thread：它的行要连着检查点与审批一起走级联删
#: （`clear_threads_for_replace`），整类 DELETE 会留下孤儿。所以删侧是这份映射的**子集**，
#: 不是第二份映射。
DELETABLE_KINDS: frozenset[str] = frozenset(
    kind for kind in KIND_TABLE if kind != sync_lib.KIND_THREAD
)


class ReplaceAborted(Exception):
    """replace 档的导入有失败，已**整批回滚**（清空与已导入的条目一起还原）。

    带的三个数就是路由那一记 400 要说出口的东西：失败几条、本机清掉了多少行、第一条错是什么。
    `card_partial` 单独一位：card 类写入自提交、回滚收不回来，这句话不能说成"全部还原"。
    """

    def __init__(
        self, *, failed: int, cleared_rows: int, first_error: str, card_partial: bool
    ) -> None:
        super().__init__(f"replace 档导入失败 {failed} 条，已整批回滚")
        self.failed = failed
        self.cleared_rows = cleared_rows
        self.first_error = first_error
        self.card_partial = card_partial


@dataclass(slots=True)
class ImportOutcome:
    """一次 import 落库的结果（路由只负责把它序列化成响应，不参与判断）。"""

    written: dict[str, int]
    skipped: dict[str, int]
    errors: list[dict[str, str]]
    cleared: dict[str, int]
    backed_up: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "skipped": self.skipped,
            "errors": self.errors,
            "cleared": self.cleared,
        }


def _backup_dir() -> Path:
    from rolecard_agent.base.paths import user_data_root

    return user_data_root() / RETENTION_BACKUP_DIRNAME / "sync"


def dump_before_clear(
    conn: SqlConnection, *, user_id: str, kinds: list[str]
) -> dict[str, int]:
    """整份替换清空**之前**，把将要被删的行先落成 JSONL，返回每张表的行数。

    会话的正文在 langgraph 的检查点表里（blob），载体行 `session_thread` 只是元数据 ——
    两样都要备份，缺一样就还原不出那条对话。检查点的 blob 是 msgpack，由
    `storage.db._json_cell` 走 base64 无损落盘，配着同目录的 `session_thread` 行足以人工重放。
    """
    backup_dir = _backup_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dumped: dict[str, int] = {}
    for kind in kinds:
        table = KIND_TABLE.get(kind)
        if table is None:
            continue
        rows = sync_rows.rows_for_user(conn, table, user_id)
        dumped[table] = dump_rows_to_jsonl(backup_dir / f"{table}-{stamp}.jsonl", rows)
    if sync_lib.KIND_THREAD in kinds:
        # 会话族的行按 thread_id 归（那几张表不认 user_id，所以先从 session_thread 换一道）。
        # **名单与级联删共用同一份现数的**（`thread_id_carriers`）：从前这里抄着两张检查点表，
        # 而删侧数的是库的形状 —— 于是 `command_approval` 这种"带 thread_id、但没人会想起
        # 要抄进备份清单"的表被删掉而不备份。抄的那份迟早漏，数的这份不会（2026-10-04
        # 快照对盘会话备份那一格时量出并立案）。
        tids = sync_rows.thread_ids_for_user(conn, user_id)
        for table in thread_id_carriers(conn):
            rows = sync_rows.rows_for_threads(conn, table, tids)
            dumped[table] = dump_rows_to_jsonl(backup_dir / f"{table}-{stamp}.jsonl", rows)
    trim_backups(backup_dir, keep=SYNC_BACKUP_KEEP)
    return dumped


def clear_rows_for_replace(
    conn: SqlConnection, *, user_id: str, kinds: list[str]
) -> dict[str, int]:
    """card/memory/reachout 三类的清空，**不 commit** —— 与后面的导入同一个事务。

    这三类是纯 DB 行，所以清空与导入能共事务：导入有任何一条失败就整批回滚、什么都没动。
    thread 类涉及检查点/图侧删除，事务罩不住，留在 `clear_threads_for_replace` 里先清、
    由删前备份兜底。card 的写入器（`RoleCards`）内部自 commit —— 它的导入一旦成功就落盘、
    回滚收不回来，这一格由 `ReplaceAborted.card_partial` 如实报出去。

    收口点：本模块 `run_import`（成功统一 commit，导入有失败则 rollback 并抛 `ReplaceAborted`）。
    """
    cleared: dict[str, int] = {}
    for kind in kinds:
        if kind not in DELETABLE_KINDS:
            continue  # thread 走 clear_threads_for_replace 的级联真删，整类 DELETE 会留孤儿
        cleared[kind] = sync_rows.delete_rows_for_user(conn, KIND_TABLE[kind], user_id)
    return cleared


def clear_threads_for_replace(conn: SqlConnection, *, user_id: str, graph: Any) -> dict[str, int]:
    """thread 类的清空：级联**真删**（2026-10-02 拍板）并提交 —— 图侧罩不进事务。

    从前这里用 `update_state(REMOVE_ALL)` 留空壳 + 写死删两张表：`command_approval`
    恰好漏掉（孤儿审批挂在已删会话上），空壳检查点还被修剪器**永留**最新一条。
    锁在级联删外面：在飞轮次不该被从脚下抽走检查点。

    返回的条数是"逐条级联删"与"收尾那条"**两个数相加** —— 只取收尾那一个会恒为 0，
    于是报成"清了 0 条"（`R102-05` 复核当场照出的读数错）。
    """
    removed = 0
    for tid in sync_rows.thread_ids_for_user(conn, user_id):
        if graph is not None:
            with thread_write(tid):
                removed += delete_thread_everywhere(conn, tid)["session_thread"]
    removed += delete_threads_for_user(conn, user_id)
    conn.commit()
    return {sync_lib.KIND_THREAD: removed}


def run_import(
    conn: SqlConnection,
    *,
    user_id: str,
    graph: Any,
    settings: Any,
    items: list[dict[str, Any]],
    clear_kinds: list[str],
) -> ImportOutcome:
    """整份替换 + 写入的**完整一次事务**：备份 → 清 thread → 清行类 → 导入 → 收口。

    协议层的校验（kind 在不在枚举里、有没有 `confirm_replace`、空载荷配清空）在**路由**做，
    这里不重复判 —— 那些是"这个请求成形吗"，不是"这批数据怎么写下去"。

    清空与导入共事务：导入有任何一条失败就 `rollback` 并抛 `ReplaceAborted`，
    本机清掉的行全部还原、对面什么都没少（"清了不导"从此没有半途形态）。
    """
    backed_up = dump_before_clear(conn, user_id=user_id, kinds=clear_kinds) if clear_kinds else {}
    cleared_threads: dict[str, int] = {}
    cleared_rows: dict[str, int] = {}
    if clear_kinds:
        # 顺序是判据：thread 先清（图侧、自带 commit），行类后清但不 commit，留给下面的导入共事务。
        if sync_lib.KIND_THREAD in clear_kinds:
            cleared_threads = clear_threads_for_replace(conn, user_id=user_id, graph=graph)
        cleared_rows = clear_rows_for_replace(conn, user_id=user_id, kinds=clear_kinds)

    result = sync_lib.apply_import(
        conn,
        user_id=user_id,
        graph=graph,
        settings=settings,
        items=items,
        commit=False,
    )
    if cleared_rows and result["errors"]:
        conn.rollback()
        raise ReplaceAborted(
            failed=len(result["errors"]),
            cleared_rows=sum(cleared_rows.values()),
            first_error=str(result["errors"][0].get("error", ""))[:120],
            card_partial=sync_lib.KIND_CARD in cleared_rows,
        )
    conn.commit()
    return ImportOutcome(
        written=result["written"],
        skipped=result["skipped"],
        errors=result["errors"],
        cleared={**cleared_rows, **cleared_threads},
        backed_up=backed_up,
    )
