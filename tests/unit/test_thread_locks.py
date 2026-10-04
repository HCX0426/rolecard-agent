"""会话写入锁（`core/thread_locks.py`，审计 #12）。

用户报的症状："我在'主动找我'那条会话里回话时正好触发了她的主动开口，我发的一条消息就没了。"
这里钉的是让那句话不再消失的四件事：
  1. 同一个 `thread_id` 的两个写者**串行**（不同线程 id 互不阻塞 —— 否则一句问候会卡住别的会话）；
  2. `try_thread_write(timeout=0)` 在被人占住时给 False（调度侧要的是"这次不插话"，不是等待）；
  3. `run_turn` 整轮持锁（主动投递因此永远插不进一轮对话的中间）；
  4. 锁表**不回收**（2026-10-04 审查快照）：按 locked() 现场判断回收有互斥竞态 —— 取出
     锁对象→acquire 之间没有引用计数，锁被 pop 后同 id 会拿到新锁，两边各写各的。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from rolecard_agent.core.thread_locks import (
    ThreadBusy,
    end_extraction,
    release_thread,
    thread_is_busy,
    thread_write,
    try_extraction,
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


def test_extraction_marker_is_not_the_write_lock() -> None:
    """提取的"在飞"标记与会话写入锁**互不相干**，这是刻意的。

    同一个会话的第二次提取要挡住（否则每次都在抄已有清单），但用户那一轮绝不能因此排队：
    一次提取 10–120 秒，而 `run_turn` 等锁上限 5 秒 —— 共用一把锁的话，"提取在跑"就等于
    "下一句最慢等 5 秒然后被拒"，那正是 §12.7 实测排除掉的耦合（第一版就踩了，被一条 158 秒的测试抓到）。
    """
    assert try_extraction("t7")
    # "在飞"这件事只用 try 的返回值读就够（它拿不到锁 = 有同类在跑）——
    # 从前这里另有一个 `extraction_is_running` 查询，生产零引用，只有测试在用（R26-13）。
    assert not try_extraction("t7"), "同会话的第二次提取该被挡住"
    # 但对话那一轮的写入锁照拿 —— 提取不该把它占住
    assert try_thread_write("t7", timeout=0.0)
    release_thread("t7")
    end_extraction("t7")
    assert try_extraction("t7"), "end 之后再 try 必须能拿到 = 标记真的清了"
    end_extraction("t7")


def test_extraction_marker_is_per_thread_and_tolerates_extra_ends() -> None:
    assert try_extraction("t8")
    assert try_extraction("other")  # 别的会话不受影响
    end_extraction("other")
    end_extraction("没有占过标记的会话")  # 静默，不抛


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


def test_thread_write_raises_instead_of_handing_back_a_bool() -> None:
    """`with thread_write(tid):` 里那句写**只有在拿到锁时才会执行**（`R28-02` 的根治处）。

    旧契约 yield 一个布尔，而 `with` 语句没法不带上分支就拿到它 —— 于是六个写检查点的口子里
    有五个把布尔丢了，拿不到锁时 `update_state` 照样无互斥跑。现在拿不到就抛，
    这条用例断的是"**函数体一次都没执行**"，不是"返回了 False"。
    """
    tid = "t-raise"
    assert try_thread_write(tid, timeout=0.0)
    ran = False
    started = time.monotonic()
    try:
        with thread_write(tid, timeout=0.2):
            ran = True
    except ThreadBusy as exc:
        assert exc.thread_id == tid
        assert exc.waited == pytest.approx(0.2, abs=0.01)
    else:
        raise AssertionError("别人正持有这把锁，thread_write 不该放行")
    assert ran is False, "抛了但函数体还是跑了 —— 那等于没锁"
    assert time.monotonic() - started < 2.0, "等不到锁应该快速失败，不是挂着"

    # 抛出去之后锁没被泄漏：同一会话下一位照样拿得到。
    release_thread(tid)
    with thread_write(tid, timeout=0.2):
        pass


def test_empty_thread_id_still_writes_without_a_lock() -> None:
    """没有 thread id（单测直接调节点、内核装配阶段）⇒ 没有可串行的对象，照常放行。"""
    with thread_write(""):
        pass
    assert try_thread_write("", timeout=0.0) is True


def test_run_turn_rejects_instead_of_forking_when_busy() -> None:
    """忙时的轮次必须明确拒绝，绝不无锁分叉（2026-10-04 审查快照、用户拍板）。

    旧语义等锁 150s 后"照样往下跑并留痕"——本地 8B 长轮 + 双窗口下就是消息被
    静默覆盖。现在：yield 一条 error 帧（409 的 SSE 形态）、图一次都不跑、锁不占。
    """
    from rolecard_agent.core.turn import Error as TurnError

    assert try_thread_write("t-busy", timeout=0.0), "夹具没能占住锁"
    try:
        class _NeverCalled:
            def __init__(self) -> None:
                self.calls = 0

            def stream(self, *_a: Any, **_kw: Any) -> Any:
                self.calls += 1
                return iter(())

        graph = _NeverCalled()
        events = list(
            run_turn(
                graph,
                graph_input={},
                config={"configurable": {"thread_id": "t-busy"}},
                role_summary={"role_id": "r", "role_name": "忙拒绝测试"},
            )
        )
        assert len(events) == 1 and isinstance(events[0], TurnError), events
        assert "还没轮到" in events[0].detail, events[0].detail
        assert graph.calls == 0, "忙时图一次都不该跑 —— 跑了就是无锁分叉"
    finally:
        release_thread("t-busy")
    # 拒绝路径绝不占锁：释放之后锁立即可得（它根本没拿过）
    assert try_thread_write("t-busy", timeout=0.0)
    release_thread("t-busy")
