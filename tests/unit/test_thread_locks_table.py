"""锁表生命周期（2026-10-04 审查快照 · 锁表竞态条目）。

历史事故形状：锁表超限时按 `locked()` 回收"没被持有"的锁，而"取出锁对象 → acquire"
之间没有引用计数 —— 持有者还没 acquire，锁就被当 stale pop 掉，第三个线程为同一
thread_id 拿到**新锁**，同会话互斥瓦解（后写盖先写）。修复后锁表**只告警不回收**；
这几条用例钉住这个语义，谁把回收加回来谁红。
"""

from __future__ import annotations

from rolecard_agent.core import thread_locks as tl


def test_lock_object_survives_table_growth_while_held(monkeypatch) -> None:
    """持锁期间锁表疯狂增长，这把锁绝不能被换掉或丢掉。"""
    monkeypatch.setattr(tl, "_MAX_TRACKED", 64)
    monkeypatch.setattr(tl, "_locks", {})
    assert tl.try_thread_write("tid-a")
    try:
        held = tl._locks["tid-a"]
        for i in range(200):  # 远超阈值：旧实现这里会回收，held 就成了孤儿锁
            tl._lock_for(f"tid-fill-{i}")
        assert tl._locks.get("tid-a") is held, "持锁中的锁对象被锁表增长换掉了"
        assert tl.thread_is_busy("tid-a"), "锁表增长后同会话互斥状态丢了"
    finally:
        tl.release_thread("tid-a")


def test_same_thread_id_always_yields_the_same_lock(monkeypatch) -> None:
    """同一 thread_id 无论隔多少次增长，拿到的必须是同一把锁对象。"""
    monkeypatch.setattr(tl, "_MAX_TRACKED", 64)
    monkeypatch.setattr(tl, "_locks", {})
    first = tl._lock_for("tid-b")
    for i in range(200):
        tl._lock_for(f"tid-fill-{i}")
    assert tl._lock_for("tid-b") is first


def test_hitting_the_alarm_threshold_does_not_evict(monkeypatch, capsys) -> None:
    """到达阈值只告警一次、不清表：超限后旧条目与新条目都还在。"""
    monkeypatch.setattr(tl, "_MAX_TRACKED", 16)
    monkeypatch.setattr(tl, "_locks", {})
    monkeypatch.setattr(tl, "_warned_full", False)
    for i in range(40):
        tl._lock_for(f"tid-{i}")
    assert len(tl._locks) == 40, "阈值触发了回收 —— 锁表竞态面回来了"
    out = capsys.readouterr().err
    assert "thread-locks" in out and "不再回收" in out
    tl._lock_for("tid-another")
    assert "thread-locks" not in capsys.readouterr().err, "告警应当只响一次"
