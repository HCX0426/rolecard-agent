"""轮次线程池必须看见**本请求的库代际**（10-02 轮 `R102-03`）。

`storage/db.py` 的清理设计是：中间件每请求发一枚代际号，**真正持连接的那条线程**在
`_current()` 里发现代际变了就先回滚一次（早期版本在中间件里 rollback，清的是另一条线程的
连接，等于没清 —— P1-10）。而代际住在 contextvar 里，`loop.run_in_executor` **不传播**它
（3.13 实测：裸调时工作线程只读得到默认值；`asyncio.to_thread` 与 `copy_context().run` 才带过去）。
于是在跑一轮的那 8 条线程里代际一辈子不变，这道清理**一次都没触发过**：一轮在提交前断掉
（模型报错 / 停止生成 / 客户端断开），它留下的未提交事务就把写锁占死，别的连接要等到
`busy_timeout`(5s) 才报 `database is locked`。

同一个形状在 `core/nodes.py` 的线程池边界上早就做对了（`copied.run(tool.invoke, args)`，
那里的症状是权限静默失效），漏的是 SSE 这道缝。

两条用例各挡一面：① 代际真的送进了工作线程；② 上一轮残留的未提交写入不会跟着下一轮落库。
"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from rolecard_agent.api import chat as chat_mod
from rolecard_agent.core.graph import MODEL_NODE
from rolecard_agent.storage.db import (
    _REQUEST_EPOCH,
    ThreadLocalConnection,
    connect,
    set_request_epoch,
)


def _chunk(text: str) -> tuple[str, Any]:
    return (
        "messages",
        (AIMessageChunk(content=text), {"langgraph_node": MODEL_NODE, "langgraph_step": 1}),
    )


def _update(text: str) -> tuple[str, Any]:
    return ("updates", {MODEL_NODE: {"messages": [AIMessage(content=text)]}})


class _RecordingGraph:
    """一条假图流：在**它自己被调用的那条线程里**读一次代际，再正常投一句话。"""

    def __init__(self, sink: list[str], body: str = "你好呀") -> None:
        self.sink = sink
        self.body = body

    def stream(self, *_a: Any, **_kw: Any) -> Any:
        self.sink.append(_REQUEST_EPOCH.get())
        yield _chunk(self.body)
        yield _update(self.body)


def _drive(epoch: str, tid: str, graph: Any) -> None:
    """走 `chat_events` 这道生产缝：先由"中间件"发号，再把整条流抽干。"""

    async def go() -> None:
        set_request_epoch(epoch)
        async for _frame in chat_mod.chat_events(
            graph,
            graph_input={},
            config={"configurable": {"thread_id": tid}},
            role_summary={"role_id": "r", "role_name": "代际"},
        ):
            pass

    asyncio.run(go())


def test_the_turn_thread_sees_this_request_epoch() -> None:
    """不带上下文时这里读到的是 `""` ⇒ 那条线程的代际永远不变、清理永不触发。"""
    seen: list[str] = []
    _drive("EPOCH-FROM-REQUEST", "s_epoch", _RecordingGraph(seen))
    assert seen and seen[0] == "EPOCH-FROM-REQUEST", (
        f"轮次线程里的库代际是 {seen!r} —— 中间件发的号没送进线程池，"
        "那条线程的清理（`_current()` 见代际变了先回滚）一次都不会触发"
    )


def test_residue_from_the_previous_turn_does_not_ride_into_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上一轮留下的未提交写入，必须在下一轮第一次用库时被回滚掉、**不跟着落库**。

    池收成**一条**线程：两轮走同一条连接才叫复现"线程池被复用"这件事，
    否则残留压根不在同一块地上，断言是空的（这条用例第一版就这么假绿过一回）。
    """
    one_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-stream-t")
    monkeypatch.setattr(chat_mod, "_CHAT_POOL", one_worker)
    try:
        db = tmp_path / "app.db"
        store = ThreadLocalConnection(db, opener=connect)
        seed = sqlite3.connect(db)
        seed.execute("CREATE TABLE note (who TEXT)")
        seed.commit()
        seed.close()

        class _SowThenDie:
            """投一个字，然后**留下一个没提交的事务** —— 就当作那一轮断在这儿。"""

            def stream(self, *_a: Any, **_kw: Any) -> Any:
                store.execute("INSERT INTO note (who) VALUES ('半句就断了的那轮')")
                yield _chunk("半句")
                yield _update("半句")

        _drive("EPOCH-1", "s_residue_1", _SowThenDie())
        # 前提：那一轮的写入还没被任何人看见（没提交）—— 这正是"半句就断了"的样子
        assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM note").fetchone()[0] == 0

        # 第二轮：**在同一 worker 里**插一条再提交。上一轮那笔残留要是没被回滚，
        # 这一次 commit 会把两轮一起写进去（第一版把 commit 写在主线程，等于没测）。
        class _SowAndCommit:
            def stream(self, *_a: Any, **_kw: Any) -> Any:
                store.execute("INSERT INTO note (who) VALUES ('第二轮')")
                store.commit()
                yield _chunk("第二轮")
                yield _update("第二轮")

        _drive("EPOCH-2", "s_residue_2", _SowAndCommit())
        rows = sorted(str(r[0]) for r in sqlite3.connect(db).execute("SELECT who FROM note"))
        assert rows == ["第二轮"], (
            f"上一轮那笔没提交的写入跟着这一轮落库了：{rows} —— "
            "说明这条线程的代际没变过，`_current()` 的「新请求先回滚残留」没触发"
        )
    finally:
        one_worker.shutdown(wait=True)
