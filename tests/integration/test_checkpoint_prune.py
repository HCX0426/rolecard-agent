"""检查点的磁盘卫生（09-26 轮 R26-07）：补时钟 → 一次性收口 → 常态修剪。

这里最要紧的一条断言**不是"删了多少行"**，而是"删掉祖先之后，会话历史一个字都没少" ——
所以判据走真读路径（`graph.get_state` 拿到的消息 id 列表做逐一对减），不是数行数：
行数少了不代表历史还在，行数没变也不代表它对。

其余几条钉两个闸各自的行为：最新那条永不删、条数闸、年龄闸、"没时钟可读就一律不删"
（fail-open，与门禁那条纪律同源），以及 `writes` 只跟着"这次真被删掉的那些检查点"走。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from rolecard_agent.base.observability import NullTracer
from rolecard_agent.config import Settings
from rolecard_agent.core.graph import build_kernel
from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.state import new_state
from rolecard_agent.core.storage import checkpointer as ck
from rolecard_agent.core.storage.checkpointer import make_checkpointer
from rolecard_agent.core.storage.migrations import MIGRATION_PLAN
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.domains.registry import domain_seed_roles
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import bootstrap, connect
from tests.conftest import ScriptedChat, list_roles

#: 收口记账键 —— 用例要重演"一份从没跑过收口的老库"时清的就是这一行。
_FLAG = "checkpoint_backlog_compacted_at"
_OLD = "2020-01-01T00:00:00.000Z"


def _conn(db_path: Path) -> Any:
    conn = connect(db_path)
    bootstrap(conn, enabled_domains=("health",), plan=MIGRATION_PLAN)
    return conn


def _forget_compaction(conn: Any) -> None:
    conn.execute("DELETE FROM kernel_meta WHERE key = ?", (_FLAG,))
    conn.commit()


def _add_ckpt(conn: Any, thread: str, cid: str) -> None:
    """塞一条检查点。触发器会当场盖上今天的时钟 —— 想要"旧"就事后再 UPDATE（触发器只管 INSERT）。"""
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint,"
        " metadata) VALUES (?, '', ?, 't', x'00', x'00')",
        (thread, cid),
    )
    conn.commit()


def _age(conn: Any, cids: list[str], *, stamp: str | None) -> None:
    conn.executemany(
        "UPDATE checkpoints SET created_at = ? WHERE checkpoint_id = ?",
        [(stamp, cid) for cid in cids],
    )
    conn.commit()


def _kept(conn: Any, thread: str) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? ORDER BY rowid", (thread,)
        )
    ]


def _kernel(db_path: Path, replies: list[Any]) -> Any:
    """真图 + 真检查点：与 `test_graph_routing._kernel` 同一套装配，只留对话必需的两件。"""
    conn = _conn(db_path)
    conn.executescript(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name)"
        "  VALUES ('u1', 't1', 'demo user');"
        "INSERT OR IGNORE INTO session_thread (thread_id, user_id, current_role_id)"
        "  VALUES ('thread-1', 'u1', 'medical_archivist');"
    )
    conn.commit()
    roles = RoleCardService(conn)
    roles.seed_builtins(user_id="u1")
    # `medical_archivist` 是**域角色**：只 seed_builtins 的话每轮都回"角色已不存在"。
    roles.seed_domain_roles(domain_seed_roles(), user_id="u1")
    plugins = PluginService(conn, known_plugins=["health"])
    reg = ToolRegistry()
    reg.register(list_roles)
    return build_kernel(
        model=ScriptedChat(replies),
        registry=reg,
        roles=roles,
        tracer=NullTracer(),
        settings=Settings(),
        checkpointer=make_checkpointer(conn),
        plugins=plugins,
    )


def _say(graph: Any, text: str) -> None:
    graph.invoke(
        {
            **new_state(
                thread_id="thread-1", user_id="u1", current_role_id="medical_archivist"
            ),
            "messages": [HumanMessage(content=text)],
        },
        config={"configurable": {"thread_id": "thread-1"}},
    )


def _visible(graph: Any) -> list[str]:
    state = graph.get_state({"configurable": {"thread_id": "thread-1"}})
    return [f"{m.type}:{m.content}" for m in state.values.get("messages", [])]


# ------------------------------------------------------------------ 时钟与触发器


def test_every_write_gets_a_clock_without_touching_langgraphs_inserts(tmp_path: Path) -> None:
    """`checkpoints` 表由 langgraph 拥有、它的 INSERT 列清单我们一行没改 —— 时钟靠触发器盖。"""
    conn = _conn(tmp_path / "app.db")
    make_checkpointer(conn)
    _add_ckpt(conn, "t1", "c-1")
    (created_at,) = conn.execute(
        "SELECT created_at FROM checkpoints WHERE checkpoint_id = 'c-1'"
    ).fetchone()
    assert created_at is not None and created_at.endswith("Z"), created_at
    # 第二遍装配（= 重启）既不重复建列也不报错，触发器仍然只有一条。
    make_checkpointer(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master"
        " WHERE type = 'trigger' AND name = 'checkpoints_stamp_created_at'"
    ).fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------------------ 一次性收口


def test_the_backlog_is_compacted_once_and_only_the_newest_survives(tmp_path: Path) -> None:
    """用户拍的那句"先把历史数据删了"：每张线程只留最新一条，而且**只演一次**。"""
    conn = _conn(tmp_path / "app.db")
    make_checkpointer(conn)
    for i in range(5):
        _add_ckpt(conn, "busy", f"b-{i}")
    for i in range(2):
        _add_ckpt(conn, "quiet", f"q-{i}")
    _forget_compaction(conn)  # 装作一份从没跑过收口的老库

    assert ck.compact_backlog_once(conn) == 5  # busy 4 条 + quiet 1 条
    assert _kept(conn, "busy") == ["b-4"]
    assert _kept(conn, "quiet") == ["q-1"]
    # 第二次是空操作：否则每次启动都会把"最近几条"重新吃成"只剩一条"。
    _add_ckpt(conn, "busy", "b-5")
    assert ck.compact_backlog_once(conn) == 0
    assert _kept(conn, "busy") == ["b-4", "b-5"]
    conn.close()


def test_a_subgraph_namespace_is_not_pruned_against_its_parent(tmp_path: Path) -> None:
    """子图的检查点写在同一个 thread 的**另一个 `checkpoint_ns`** 下。

    分组只按 thread 的话：最新那条 = 根图的 root-2，于是 child-1 被当祖先删掉；反过来若
    子图最后写入，被删的就是根图那条 —— 那不是修剪，那是删会话。
    """
    conn = _conn(tmp_path / "app.db")
    make_checkpointer(conn)
    _add_ckpt(conn, "t1", "root-1")
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint,"
        " metadata) VALUES ('t1', 'sub:1', 'child-1', 't', x'00', x'00')"
    )
    conn.commit()
    _add_ckpt(conn, "t1", "root-2")
    _forget_compaction(conn)

    assert ck.compact_backlog_once(conn) == 1  # 只有 root-1 该走
    got = [
        (str(ns), str(cid))
        for ns, cid in conn.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM checkpoints ORDER BY checkpoint_ns"
        )
    ]
    assert got == [("", "root-2"), ("sub:1", "child-1")]
    conn.close()


# ------------------------------------------------------------------ 常态修剪的两个闸
def test_pruning_applies_both_gates_and_never_touches_the_newest(tmp_path: Path) -> None:
    """不在最近 6 条之内 **或** 比 14 天更旧，才删。c-9 是最新那条（rowid 最大）。"""
    conn = _conn(tmp_path / "app.db")
    make_checkpointer(conn)
    for i in range(10):
        _add_ckpt(conn, "t1", f"c-{i}")
    _age(conn, ["c-5"], stamp=_OLD)  # rank 4：在 6 条之内，但已经过期

    assert ck.prune_checkpoints(conn, keep_per_thread=6, keep_days=14) == 5
    # rank 6..9 = c-3/c-2/c-1/c-0 出条数闸；c-5 出年龄闸。留下 c-9,c-8,c-7,c-6,c-4。
    assert _kept(conn, "t1") == ["c-4", "c-6", "c-7", "c-8", "c-9"]
    conn.close()


def test_the_count_knob_counts_checkpoints_including_the_newest(tmp_path: Path) -> None:
    """`CHECKPOINT_KEEP_PER_THREAD=6` 数的是**含最新那条**的总数（`R28-20`）。

    这条不是新行为，是把已有行为钉住：常量头顶那行注释原先写"不含最新那条"，而
    `rank >= keep` 删的是第 7 条起 —— 留下的是"最新 + 5 条祖先"= 共 6 条，**启动打印也照旧
    写着"最新 1 条 + 最近 6 条"**（那是同一个差一）。注释与打印都按实测改掉了，这里留一把尺：
    以后谁改了那句 `rank >=` 的边界，或把注释又写回去，这条会红。

    为什么值得单独一条而不并入上面那条两闸用例：那条故意让一条过期（留下 5 条），
    "6"那个数被年龄闸遮住了，差一在它身上看不出来。
    """
    conn = _conn(tmp_path / "app.db")
    make_checkpointer(conn)
    for i in range(10):
        _add_ckpt(conn, "t1", f"c-{i}")  # 都不过期：只走条数闸

    assert ck.prune_checkpoints(conn, keep_per_thread=6, keep_days=14) == 4
    assert _kept(conn, "t1") == ["c-4", "c-5", "c-6", "c-7", "c-8", "c-9"], (
        "留下的条数与常量声明不一致：那个数含不含最新那条，现在有了唯一出处"
    )
    assert len(_kept(conn, "t1")) == 6
    conn.close()


def test_a_row_without_a_clock_survives_while_its_peers_do_not(tmp_path: Path) -> None:
    """不知道多旧 = 不删。这条要能单独成立，才说明救它的是那个 NULL 而不是"它还新"。"""
    conn = _conn(tmp_path / "app.db")
    make_checkpointer(conn)
    for i in range(9):
        _add_ckpt(conn, "t1", f"c-{i}")
    # c-8 是最新那条；c-7..c-0 全部标成 2020 年，只有 c-5 的时钟**读不到**。
    _age(conn, [f"c-{i}" for i in range(8)], stamp=_OLD)
    _age(conn, ["c-5"], stamp=None)

    ck.prune_checkpoints(conn, keep_per_thread=6, keep_days=14)
    assert _kept(conn, "t1") == ["c-5", "c-8"]  # 同 rank 段的 c-6/c-4 都被年龄闸删了
    conn.close()


def test_pending_writes_go_only_with_the_checkpoints_we_deleted(tmp_path: Path) -> None:
    """`writes` 的语义是"检查点还没落盘时先攒着" ⇒ **没有**对应检查点的行恰恰可能是飞行中的。

    所以只删"这次真被删掉的那些检查点"名下的行。全局孤儿判据会把正在跑的那一轮的
    中间结果一起清掉 —— 那是比不修剪坏得多的错法。
    """
    conn = _conn(tmp_path / "app.db")
    make_checkpointer(conn)
    _add_ckpt(conn, "t1", "c-0")
    _add_ckpt(conn, "t1", "c-1")

    def add_write(cid: str, task: str) -> None:
        conn.execute(
            "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx,"
            " channel, type, value) VALUES ('t1', '', ?, ?, 0, 'messages', 't', x'00')",
            (cid, task),
        )
        conn.commit()

    add_write("c-0", "gone")  # 属于将被收口删掉的那条
    add_write("c-1", "kept")  # 属于留下的那条
    add_write("c-flying", "in-flight")  # 检查点还没写出来：飞行中的那一轮
    _forget_compaction(conn)

    ck.compact_backlog_once(conn)
    assert [str(r[0]) for r in conn.execute("SELECT task_id FROM writes ORDER BY task_id")] == [
        "in-flight",
        "kept",
    ]
    conn.close()


# ------------------------------------------------------------------ 安全边界


def test_deleting_ancestors_leaves_the_visible_history_intact(tmp_path: Path) -> None:
    """**整条改动的安全边界**：收口之后屏幕上能读到的历史一个字都不能少，而且还能接着聊。

    会话的唯一真相就是最新那个检查点 —— langgraph 每轮写的是**整份** state 快照（正是它
    让磁盘平方级增长），所以删祖先删的是冗余，不是对话。
    """
    graph = _kernel(
        tmp_path / "app.db",
        [
            AIMessage(content="第一次回答"),
            AIMessage(content="第二次回答"),
            AIMessage(content="第三次回答"),
            AIMessage(content="收口之后还能接上"),
        ],
    )
    for turn in ("第一问", "第二问", "第三问"):
        _say(graph, turn)
    before = _visible(graph)
    assert len(before) >= 6, before  # 三问三答都进了历史

    conn = _conn(tmp_path / "app.db")
    rows_before = len(_kept(conn, "thread-1"))
    assert rows_before > 3, "一轮会写好几个检查点，这正是平方级增长的来源"
    _forget_compaction(conn)
    assert ck.compact_backlog_once(conn) == rows_before - 1
    assert len(_kept(conn, "thread-1")) == 1  # 只剩最新那一条

    assert _visible(graph) == before, "删祖先之后读出来的历史变了"

    _say(graph, "第四问")
    after = _visible(graph)
    assert after[: len(before)] == before
    assert after[-1].endswith("收口之后还能接上"), after[-1]
    conn.close()


def _holed_db(tmp_path: Path, name: str, *, rows: int, drop: str) -> tuple[Path, Any]:
    """先把库**灌大**，再按 `drop`（一句 WHERE）删掉一批 —— 删完不再插，洞才留得住。

    为什么非要"先灌后删"（本机实测逼出来的）：删掉一页之后紧接着的写入会把那页拿去用，
    所以"边插边删"造不出 freelist；第一版就是这样，三条用例全在空转。真库那 86% 的空洞来自
    收口时删掉散在文件**中间**的 127 条检查点，而 `PRAGMA incremental_vacuum` 只归还**文件尾部
    连着的那一段**（那次实测：790 页洞只归还 1 页）。所以洞的位置也是量程的一部分：
    `drop` 给奇偶交替的谓词 = 散洞，给 `rowid <= N` = 前洞、尾巴还是活的。
    """
    db = tmp_path / name
    conn = connect(db)
    # 全新库的 `auto_vacuum` 由 `connect()` 在建库那一刻就带上（排序理由见 storage/db.py）。
    # 先问一句再往下写：不问，测到的可能是"这个库根本没能力归还空页"那条分支 —— 第一版把
    # PRAGMA 写在 bootstrap() 之后，正是这样把用例变成空转的。
    assert int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]) != 0, "新库没开 auto_vacuum"
    bootstrap(conn, plan=MIGRATION_PLAN)
    blob = "x" * 4000
    conn.execute("CREATE TABLE filler (payload TEXT)")
    conn.execute("BEGIN")
    for _ in range(rows):
        conn.execute("INSERT INTO filler (payload) VALUES (?)", (blob,))
    conn.commit()
    conn.execute(f"DELETE FROM filler WHERE {drop}")
    conn.commit()
    conn.close()
    return db, connect(db)


def _pragma_i(conn: Any, name: str) -> int:
    return int(conn.execute(f"PRAGMA {name}").fetchone()[0])


def test_startup_reclaim_shrinks_the_file_when_holes_dominate(tmp_path: Path) -> None:
    """洞过半就必须**真把文件缩小**（`R28-17`）：跑了回收但一个字节没还，就是这条要防的。

    它防两件事。一是"日常永不回收"：回收原先只挂在修剪路径上，而用得久的库大半空洞来自
    收口 / 删消息 / 迁移重建那些不叫"修剪"的路径（本机实测 86% 的页是 freelist、文件从没
    变小过）。二是"回收只做了 incremental"：同一次实测里 790 页散洞只归还 1 页，报出来像个成功
    —— 所以这条断言按**归还的比例**卡，不是卡 `> 0`。
    """
    db, conn = _holed_db(tmp_path, "app.db", rows=1000, drop="rowid % 4 != 0")
    grown, free = _pragma_i(conn, "page_count"), _pragma_i(conn, "freelist_count")
    assert free >= ck._RECLAIM_MIN_FREE_PAGES, f"夹具没攒够空页（freelist={free}），这条测不出东西"
    assert free / grown >= ck._RECLAIM_VACUUM_RATIO, f"夹具的洞不过半（{free}/{grown}）"

    freed = ck.reclaim_if_fragmented(conn)
    assert freed >= grown * 0.4, f"洞过半却只归还了 {freed}/{grown} 页 —— 只砍尾部那一段不够兑现"
    now = _pragma_i(conn, "page_count")
    assert now < grown // 2, f"{grown} 页 → {now} 页，没收干净"
    assert _pragma_i(conn, "freelist_count") == 0

    # 归还的是**空页**，不是内容：内核表与剩下那些行都还在、读得回来。
    assert conn.execute("SELECT COUNT(*) FROM kernel_meta").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM filler").fetchone()[0] == 250
    conn.close()
    # 落盘尺寸要看**关掉之后**：同一个连接里 VACUUM 刚做完时 stat 还读得到旧尺寸（本机实测）。
    assert db.stat().st_size < grown * 4096 // 2, "逻辑页数少了，文件却没小"


def test_startup_reclaim_stays_cheap_when_holes_are_minor(tmp_path: Path) -> None:
    """洞不过半时不做全量重排：允许归还尾部那几页，但不许把整个文件重写一遍。

    判据是**留着的洞**：一次 VACUUM 会把 freelist 清零，而这里要的就是"别碰它"。全量重排是
    启动路径上唯一慢的那一步（实测 3.7 MB / 6 ms，随文件大小线性增长），洞不过半就不该付它。
    """
    db, conn = _holed_db(tmp_path, "light.db", rows=1000, drop="rowid <= 400")
    pages, free = _pragma_i(conn, "page_count"), _pragma_i(conn, "freelist_count")
    assert free >= ck._RECLAIM_MIN_FREE_PAGES, f"空页没攒够（{free}），这条测不出东西"
    assert free / pages < ck._RECLAIM_VACUUM_RATIO, f"洞过半了（{free}/{pages}），量程不对"

    ck.reclaim_if_fragmented(conn)
    assert _pragma_i(conn, "freelist_count") > 0, "洞不过半却做了一次全量 VACUUM"
    # 允许砍掉尾部连着的那几页（真库副本实测 914→913、这条夹具 1061→1060），但不许重排整个文件。
    now = _pragma_i(conn, "page_count")
    assert now > pages * 0.95, f"{pages} 页 → {now} 页，砍得太多了"
    conn.close()


def test_startup_reclaim_does_nothing_before_the_holes_are_worth_it(tmp_path: Path) -> None:
    """攒得不够就什么都不做 —— 启动路径不为"好看"碰文件。"""
    db, conn = _holed_db(tmp_path, "tiny.db", rows=120, drop="rowid <= 40")
    pages, free = _pragma_i(conn, "page_count"), _pragma_i(conn, "freelist_count")
    assert free < ck._RECLAIM_MIN_FREE_PAGES, f"夹具本来就攒够了（{free}），量程不对"

    assert ck.reclaim_if_fragmented(conn) == 0
    assert _pragma_i(conn, "page_count") == pages
    conn.close()
    assert db.stat().st_size > 0


def test_startup_reclaim_converts_a_legacy_db_while_it_is_paying_anyway(tmp_path: Path) -> None:
    """老库（`auto_vacuum=0`）洞过半时：这一步自己把 INCREMENTAL 落进文件头。

    转换要么花一次全量 VACUUM，要么花在建库那一刻（就是 `storage/db.py:connect` 那句 PRAGMA
    排在 WAL 之前的理由）。既然这一刻已经在重写整个文件，转换不要钱；不落这一句，下一次散洞
    还得再等一次"洞过半"。

    这里**故意用裸连接**跑回收，不用 `connect()`：后者每条连接都带着"待写入的 INCREMENTAL 意图"
    （实测：`connect()` 之后不 VACUUM 的话文件头仍是 0，一 VACUUM 就变成 2），用它就分不清
    转换到底是这一步做的还是开库时捎带的。
    """
    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db))  # 不经 connect()：文件头里就是 auto_vacuum=0
    raw.execute("CREATE TABLE filler (payload TEXT)")
    blob = "x" * 4000
    raw.execute("BEGIN")
    for _ in range(1000):
        raw.execute("INSERT INTO filler (payload) VALUES (?)", (blob,))
    raw.commit()
    raw.execute("DELETE FROM filler WHERE rowid % 4 != 0")
    raw.commit()
    assert int(raw.execute("PRAGMA auto_vacuum").fetchone()[0]) == 0, "夹具没造出老库形状"

    assert ck.reclaim_if_fragmented(raw) > 0, "老库洞过半却没归还"
    assert int(raw.execute("PRAGMA auto_vacuum").fetchone()[0]) != 0, "转换没落进文件头"
    raw.close()
    reopened = sqlite3.connect(str(db))
    assert int(reopened.execute("PRAGMA auto_vacuum").fetchone()[0]) != 0, "重开就丢了"
    reopened.close()
