"""检查点的磁盘卫生（09-26 轮 R26-07）：补时钟 → 一次性收口 → 常态修剪。

这里最要紧的一条断言**不是"删了多少行"**，而是"删掉祖先之后，会话历史一个字都没少" ——
所以判据走真读路径（`graph.get_state` 拿到的消息 id 列表做逐一对减），不是数行数：
行数少了不代表历史还在，行数没变也不代表它对。

其余几条钉两个闸各自的行为：最新那条永不删、条数闸、年龄闸、"没时钟可读就一律不删"
（fail-open，与门禁那条纪律同源），以及 `writes` 只跟着"这次真被删掉的那些检查点"走。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from rolecard_agent.config import Settings
from rolecard_agent.core import checkpointer as ck
from rolecard_agent.core.checkpointer import make_checkpointer
from rolecard_agent.core.graph import build_kernel
from rolecard_agent.core.observability import NullTracer
from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.state import new_state
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import bootstrap, connect
from tests.conftest import ScriptedChat, list_roles

#: 收口记账键 —— 用例要重演"一份从没跑过收口的老库"时清的就是这一行。
_FLAG = "checkpoint_backlog_compacted_at"
_OLD = "2020-01-01T00:00:00.000Z"


def _conn(db_path: Path) -> Any:
    conn = connect(db_path)
    bootstrap(conn, enabled_domains=("health",))
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
    roles.seed_builtins()
    # `medical_archivist` 是**域角色**：只 seed_builtins 的话每轮都回"角色已不存在"。
    roles.seed_domain_roles()
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
