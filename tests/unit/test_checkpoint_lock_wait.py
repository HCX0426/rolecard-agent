"""checkpoint 锁等待观测层（`ObservedLock`）的回归钉子。

这批用例守的是 2026-10-04 审查快照" saver 全局锁"那条的第一步（**先观测再拆**）：

  1. 观测锁的互斥语义**逐字不变** —— 它包在 langgraph `SqliteSaver` 那把全局锁的
     位置上，语义漂移会让"两会话并发写检查点"变成静默的数据损坏，比没有观测更糟；
  2. 等待读数可断言（stats）—— 拆连接族值不值、拆完有没有真变快，全靠这组数对账；
  3. 超阈值自动出声（logline warning）—— 真机日志里自己会报告"谁在排队"，
     不需要谁记得去开探针。

计时断言全部留足 CI 抖动余量（等 100ms、断言 ≥50ms），红了先怀疑机器抖、别怀疑判据。
"""

from __future__ import annotations

import threading
import time

from rolecard_agent.core.checkpointer import ObservedLock, make_checkpointer


def test_observed_lock_keeps_mutual_exclusion() -> None:
    """互斥照旧：一个线程持锁时，另一个的 acquire 必须真的阻塞。"""
    lock = ObservedLock()
    holder_in = threading.Event()
    release = threading.Event()
    other_acquired = threading.Event()

    def holder() -> None:
        with lock:
            holder_in.set()
            release.wait(5)

    def other() -> None:
        assert holder_in.wait(5), "持锁线程没起来"
        lock.acquire()
        other_acquired.set()
        lock.release()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=other)
    t1.start()
    t2.start()
    # 持锁期间另一个线程不该拿到：给它 100ms 排队时间，然后放行。
    time.sleep(0.1)
    assert not other_acquired.is_set(), "观测锁没挡住并发进入 —— 互斥语义被破坏"
    release.set()
    t1.join(5)
    t2.join(5)
    assert other_acquired.is_set()


def test_observed_lock_stats_record_the_wait() -> None:
    """等锁的时长进 stats：这是"拆连接族值不值"要拿去对账的那组数。"""
    lock = ObservedLock()
    holder_in = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with lock:
            holder_in.set()
            release.wait(5)

    t1 = threading.Thread(target=holder)
    t1.start()
    assert holder_in.wait(5)
    time.sleep(0.1)  # 让排队线程实打实等 100ms 再进
    t_waiter = threading.Thread(target=lock.acquire)
    t_waiter.start()
    time.sleep(0.05)  # 此刻它还在排队（互斥由上一支用例钉过）
    release.set()
    t1.join(5)
    t_waiter.join(5)
    lock.release()  # 归还 waiter 拿到的那一次

    stats = lock.stats()
    assert stats["acquires"] == 2, stats
    # 等待 ≥50ms：实打实等了 100ms+，下界留一半给 CI 抖动。
    assert stats["max_wait_ms"] >= 50.0, stats
    assert stats["total_wait_ms"] >= stats["max_wait_ms"], stats


def test_slow_wait_emits_one_warning_line(capsys) -> None:
    """超阈值出声：一行 warning，带等待时长与累计读数（真机排障的那行证据）。"""
    lock = ObservedLock(warn_ms=20.0)
    holder_in = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with lock:
            holder_in.set()
            release.wait(5)

    t1 = threading.Thread(target=holder)
    t1.start()
    assert holder_in.wait(5)

    def waiter() -> None:
        lock.acquire()
        lock.release()

    t2 = threading.Thread(target=waiter)
    t2.start()
    time.sleep(0.05)  # 等 waiter 实打实排上 50ms > 阈值 20ms，再放行
    release.set()
    t1.join(5)
    t2.join(5)

    err = capsys.readouterr().err
    assert "[warning] [checkpoints]" in err, err
    assert "checkpoint 锁等待" in err, err
    # 累计读数也在行里：光有"等了多久"没有"这是常态还是偶发"，读数没有行动价值。
    assert "累计" in err and "最长" in err, err


def test_fast_wait_stays_silent(capsys) -> None:
    """没超阈值不出声：常态的微秒级拿锁每次一行会把日志刷成噪音。"""
    lock = ObservedLock(warn_ms=10_000.0)
    for _ in range(3):
        with lock:
            pass
    captured = capsys.readouterr()
    assert "checkpoint 锁等待" not in captured.err
    assert "checkpoint 锁等待" not in captured.out


def test_make_checkpointer_installs_the_observed_lock(conn) -> None:
    """装配出来的 saver 挂的就是观测锁，且热路径照走（读一次空库也算一次拿锁）。"""
    saver = make_checkpointer(conn)
    assert isinstance(saver.lock, ObservedLock), type(saver.lock)
    # 语义不变的最小证据：正常读一次（走 cursor → 观测锁），无异常、计数进账。
    assert saver.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}) is None
    stats = saver.lock.stats()
    assert stats["acquires"] >= 1, stats
