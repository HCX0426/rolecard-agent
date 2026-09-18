"""任务目录变化侦测（core/file_watch.py）的单元测试。

钉住四件事：快照口径（只收元数据、跳过噪声）、diff 三分类（增/改/删）、
事件生命周期（首建不触发 / 挂起不续期 / 过期由调用方推进 / 根热切重建基线）、
kernel_meta 状态的损坏容错。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rolecard_agent.core import file_watch as fw


def _write(root: Path, rel: str, content: str = "x") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    d = tmp_path / "watched"
    d.mkdir()
    return d


# ------------------------------------------------------------------ 快照


def test_scan_collects_metadata_and_skips_noise(task_dir: Path) -> None:
    _write(task_dir, "a.txt")
    _write(task_dir, "sub/b.md")
    _write(task_dir, ".git/config")
    _write(task_dir, "node_modules/pkg/index.js")
    _write(task_dir, ".hidden.txt")
    entries, truncated = fw.scan_snapshot(task_dir)
    assert truncated is False
    assert set(entries) == {"a.txt", "sub/b.md"}
    size, mtime = entries["a.txt"]
    assert size == 1 and mtime > 0


def test_scan_respects_depth_limit(task_dir: Path) -> None:
    deep = "/".join(f"d{i}" for i in range(fw.MAX_DEPTH + 2))
    _write(task_dir, f"{deep}/x.txt")
    _write(task_dir, "top.txt")
    entries, _ = fw.scan_snapshot(task_dir)
    assert set(entries) == {"top.txt"}


def test_scan_truncates_at_max_files(task_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fw, "MAX_FILES", 3)
    for i in range(6):
        _write(task_dir, f"f{i}.txt")
    entries, truncated = fw.scan_snapshot(task_dir)
    assert truncated is True
    assert len(entries) == 3


def test_scan_unreadable_dir_returns_empty(tmp_path: Path) -> None:
    entries, truncated = fw.scan_snapshot(tmp_path / "not-exist")
    assert entries == {} and truncated is False


# ------------------------------------------------------------------ diff


def test_diff_classifies_add_mod_del() -> None:
    old = {"keep.txt": [1, 100], "gone.txt": [2, 100], "touched.txt": [5, 100]}
    new = {"keep.txt": [1, 100], "touched.txt": [9, 200], "new.txt": [3, 200]}
    events = fw.diff_entries(old, new)
    assert {"op": "add", "path": "new.txt"} in events
    assert {"op": "mod", "path": "touched.txt"} in events
    assert {"op": "del", "path": "gone.txt"} in events
    assert all(e["path"] != "keep.txt" for e in events)


# ------------------------------------------------------------------ 状态持久化


def test_state_roundtrip_and_corruption_tolerance(conn, task_dir: Path) -> None:
    state = fw.WatchState(
        root=str(task_dir),
        entries={"a.txt": [1, 2]},
        baseline_utc="2026-09-18 00:00:00",
        changed=[],
        truncated=False,
    )
    fw.save_state(conn, state)
    assert fw.load_state(conn) == state
    conn.execute("UPDATE kernel_meta SET value = '{broken' WHERE key = 'file_watch:state'")
    conn.commit()
    assert fw.load_state(conn) is None
    fw.clear_state(conn)
    assert fw.load_state(conn) is None


# ------------------------------------------------------------------ 事件生命周期


def test_first_check_only_builds_baseline(conn, task_dir: Path) -> None:
    _write(task_dir, "old.txt")
    now = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    assert fw.check_changes(conn, task_dir, now_utc=now) is None
    state = fw.load_state(conn)
    assert state is not None and state["root"] == str(task_dir)
    assert state["changed"] == []
    # 无变化 = 无事件
    assert fw.check_changes(conn, task_dir, now_utc=now + timedelta(minutes=1)) is None


def test_change_detected_and_pending_preserved(conn, task_dir: Path) -> None:
    now = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    fw.check_changes(conn, task_dir, now_utc=now)  # 建基线
    _write(task_dir, "report.md", "新报告")
    got = fw.check_changes(conn, task_dir, now_utc=now + timedelta(seconds=30))
    assert got is not None
    events, truncated, expired = got
    assert events == [{"op": "add", "path": "report.md"}]
    assert truncated is False and expired is False
    # 下一轮（没再改）：事件仍挂起、清单不被覆盖、不重复 diff
    _write(task_dir, "other.txt")  # 新变化也并入不了已挂起的清单（等消费后下轮再说）
    again = fw.check_changes(conn, task_dir, now_utc=now + timedelta(minutes=1))
    assert again is not None and again[0] == events


def test_pending_expiry_flag_and_advance(conn, task_dir: Path) -> None:
    now = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    fw.check_changes(conn, task_dir, now_utc=now)
    _write(task_dir, "a.txt")
    fresh = fw.check_changes(conn, task_dir, now_utc=now + timedelta(minutes=2))
    assert fresh is not None and fresh[2] is False
    old = fw.check_changes(conn, task_dir, now_utc=now + fw.EVENT_EXPIRY + timedelta(hours=1))
    assert old is not None and old[2] is True  # 过期标志（清单还在，由调用方处置）
    # 调用方推进基线 → 事件清空、该文件不再算变化
    fw.advance_baseline(conn, task_dir, now_utc=now + timedelta(hours=25))
    assert fw.check_changes(conn, task_dir, now_utc=now + timedelta(hours=25)) is None
    assert fw.pending_count(conn) == 0


def test_root_hotswitch_rebuilds_baseline(conn, task_dir: Path, tmp_path: Path) -> None:
    now = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    fw.check_changes(conn, task_dir, now_utc=now)
    other = tmp_path / "另一个目录"
    other.mkdir()
    _write(other, "z.txt")
    # 换根 = 重建基线、不触发（旧目录的事件与新目录无关）
    assert fw.check_changes(conn, other, now_utc=now + timedelta(minutes=1)) is None
    state = fw.load_state(conn)
    assert state is not None and state["root"] == str(other)


def test_mtime_only_change_counts_as_mod(conn, task_dir: Path) -> None:
    now = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    _write(task_dir, "a.txt", "same")
    fw.check_changes(conn, task_dir, now_utc=now)
    os.utime(task_dir / "a.txt", (now.timestamp() + 500, now.timestamp() + 500))
    got = fw.check_changes(conn, task_dir, now_utc=now + timedelta(minutes=1))
    assert got is not None
    assert got[0] == [{"op": "mod", "path": "a.txt"}]


def test_pending_count_zero_without_state(conn) -> None:
    fw.clear_state(conn)
    assert fw.pending_count(conn) == 0
