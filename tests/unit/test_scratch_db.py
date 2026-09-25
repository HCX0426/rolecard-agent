"""`scripts/scratch_db.py` 选源逻辑的测试 —— 它决定"每个实验看到的是哪一天的世界"。

为什么要给一个脚本助手写测试：本机有两个数据根（开发态仓库 `data/`、打包态
`%LOCALAPPDATA%`），而 2026-09-24 那次搬迁把仓库那份**复制**而非移走，于是留下一个
"24 条会话、看着完全合理、但停在 09-23"的快照。走默认值取源的实验会静默用上它。
所以这里钉的三件事：选新的、说了话、以及不许沉默。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scratch_db.py"


def _load(monkeypatch, tmp_path: Path):
    """每次重新 import 一份，免得测试之间共享 CANDIDATE_SOURCES。"""
    spec = importlib.util.spec_from_file_location(f"scratch_db_t_{tmp_path.name}", str(SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db(path: Path, *, newest: str, ids: tuple[str, ...]) -> Path:
    """造一份只够选源逻辑用的库：一张 session_thread 加几行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_thread (thread_id TEXT PRIMARY KEY, updated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO session_thread VALUES (?, ?)",
        [(i, newest) for i in ids],
    )
    conn.commit()
    conn.close()
    return path


def test_explicit_env_wins(tmp_path, monkeypatch):
    explicit = _db(tmp_path / "explicit.db", newest="2020-01-01", ids=("a",))
    monkeypatch.setenv("LIVE_DB_PATH", str(explicit))
    mod = _load(monkeypatch, tmp_path)
    assert mod.resolve_live_db().name == "explicit.db"


def test_explicit_env_pointing_nowhere_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_DB_PATH", str(tmp_path / "nope.db"))
    mod = _load(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="LIVE_DB_PATH"):
        mod.resolve_live_db()


def test_picks_the_newer_root_when_both_exist(tmp_path, monkeypatch, capsys):
    old = _db(tmp_path / "dev.db", newest="2026-09-23 12:21:11", ids=("s1", "s2"))
    new = _db(tmp_path / "installed.db", newest="2026-09-25 05:59:18", ids=("s1", "s3"))
    monkeypatch.delenv("LIVE_DB_PATH", raising=False)
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "CANDIDATE_SOURCES", (old, new))

    assert mod.resolve_live_db() == new
    err = capsys.readouterr().err
    # 沉默是这个 bug 的栖息地：选了谁、谁被弃用，必须打出来。
    assert "installed.db" in err and "dev.db" in err and "更旧" in err


def test_single_root_is_quiet(tmp_path, monkeypatch, capsys):
    only = _db(tmp_path / "only.db", newest="2026-09-20 00:00:00", ids=("s1",))
    monkeypatch.delenv("LIVE_DB_PATH", raising=False)
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "CANDIDATE_SOURCES", (only, tmp_path / "absent.db"))

    assert mod.resolve_live_db() == only
    assert capsys.readouterr().err == ""  # 没有歧义就别吵


def test_content_beats_file_mtime(tmp_path, monkeypatch):
    """WAL 模式下主库文件可以几十天不动而内容很新；只看 mtime 会把活库读成死的。"""
    db = _db(tmp_path / "live.db", newest="2026-09-25 05:59:18", ids=("s1",))
    stale = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    import os

    os.utime(db, (stale, stale))
    mod = _load(monkeypatch, tmp_path)
    assert mod._freshness(db).year == 2026  # 来自表，不是文件时间


def test_copy_carries_the_exact_session_set(tmp_path, monkeypatch):
    """验收口径是**集合**，不是条数（§12.18 那次误删就是按计数删的）。"""
    src = _db(tmp_path / "src.db", newest="2026-09-25 05:59:18", ids=("s_a", "s_b", "s_ecb"))
    monkeypatch.delenv("LIVE_DB_PATH", raising=False)
    mod = _load(monkeypatch, tmp_path)
    dest = tmp_path / "copy.db"

    mod.copy_of_live_db(dest, source=src)
    conn = sqlite3.connect(dest)
    got = {r[0] for r in conn.execute("SELECT thread_id FROM session_thread")}
    conn.close()
    assert got == {"s_a", "s_b", "s_ecb"}
