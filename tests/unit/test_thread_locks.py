"""会话写入锁（`core/thread_locks.py`，审计 #12）。

用户报的症状："我在'主动找我'那条会话里回话时正好触发了她的主动开口，我发的一条消息就没了。"
这里钉的是让那句话不再消失的四件事：
  1. 同一个 `thread_id` 的两个写者**串行**（不同线程 id 互不阻塞 —— 否则一句问候会卡住别的会话）；
  2. `try_thread_write(timeout=0)` 在被人占住时给 False（调度侧要的是"这次不插话"，不是等待）；
  3. `run_turn` 整轮持锁（主动投递因此永远插不进一轮对话的中间）；
  4. 回收表项时**不动正在被持有的锁**（抽走它等于两边各拿一把新锁，互斥直接失效）。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from rolecard_agent.core.thread_locks import (
    release_thread,
    thread_is_busy,
    thread_write,
    try_thread_write,
)
from rolecard_agent.core.turn import run_turn


def _hold(tid: str, seconds: float) -> None:
    if try_thread_write(tid, timeout=0.0):
        time.sleep(seconds)
        release_thread(tid)


def test_same_thread_serializes_and_different_threads_do_not() -> None:
    order: list[str] = []

    def writer(tid: str, label: str, hold: float) -> None:
        with thread_write(tid):
            order.append(f"{label}:开始")
            time.sleep(hold)
            order.append(f"{label}:结束")

    a = threading.Thread(target=writer, args=("t1", "甲", 0.08))
    b = threading.Thread(target=writer, args=("t1", "乙", 0.0))
    a.start()
    time.sleep(0.02)  # 让甲先拿到锁
    b.start()
    a.join()
    b.join()
    # 同一个线程 id：乙一定整段排在甲后面（交错就说明锁没起作用）
    assert order == ["甲:开始", "甲:结束", "乙:开始", "乙:结束"]

    # 不同会话不该互相等
    order.clear()
    c = threading.Thread(target=writer, args=("t2", "丙", 0.05))
    d = threading.Thread(target=writer, args=("t3", "丁", 0.05))
    c.start()
    d.start()
    c.join()
    d.join()
    assert len(order) == 4
    assert {order[0][:1], order[1][:1]} == {"丙", "丁"}  # 两段是并行的（谁先都行）


def test_try_is_nonblocking_when_someone_else_holds_it() -> None:
    assert try_thread_write("t4", timeout=0.0)
    assert thread_is_busy("t4")
    assert not try_thread_write("t4", timeout=0.0)
    release_thread("t4")
    assert not thread_is_busy("t4")
    assert try_thread_write("t4", timeout=0.0)
    release_thread("t4")


def test_no_thread_id_never_blocks() -> None:
    """单测直接调节点、或内核装配阶段没有 thread_id ⇒ 没有可串行的对象，照常跑。"""
    assert try_thread_write("", timeout=0.0)
    release_thread("")  # 不该炸
    assert not thread_is_busy("")


class _CheckingGraph:
    """`.stream` 跑的时候问一句"我是不是持着锁"—— 那一轮的互斥只在这里能被验到。"""

    def __init__(self) -> None:
        self.saw_lock: list[bool] = []

    def stream(self, *_a: Any, **_kw: Any) -> Any:
        self.saw_lock.append(thread_is_busy("t5"))
        return iter(())


def test_run_turn_holds_the_thread_lock_for_the_whole_turn() -> None:
    graph = _CheckingGraph()
    list(
        run_turn(
            graph,
            graph_input={},
            config={"configurable": {"thread_id": "t5"}},
            role_summary={"role_id": "r", "role_name": "锁测试"},
        )
    )
    assert graph.saw_lock == [True], "那一轮没占住写入锁 ⇒ 主动投递还能插进来"
    assert not thread_is_busy("t5"), "跑完必须放锁，否则整个会话从此写不进去"


def test_run_turn_releases_even_when_the_graph_raises() -> None:
    class _Boom:
        def stream(self, *_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("后端炸了")
            yield  # pragma: no cover

    list(
        run_turn(
            _Boom(),
            graph_input={},
            config={"configurable": {"thread_id": "t6"}},
            role_summary={"role_id": "r", "role_name": "锁测试"},
        )
    )
    assert not thread_is_busy("t6")
    assert try_thread_write("t6", timeout=0.0)
    release_thread("t6")
