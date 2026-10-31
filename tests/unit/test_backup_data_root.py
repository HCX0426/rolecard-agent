"""数据根备份机制本体（`features/data_backup.py`）的判据 —— 装包之前那份"能不能回滚"。

从前这里加载的是 `scripts/tools/backup_data_root.py`（R1031-15 之后脚本退化成
argparse 壳，判据全在 core）—— 测试跟着搬：机制在哪，判据就问哪。CLI 壳另留一条
退出码烟测（install_package.ps1 第②道吃的是 `exit != 0`）。

第一版就带着一个真缺陷跑了一次真装机：它用 `base.paths.user_data_root()` 找数据根，
而那个函数**在开发态故意**返回仓库的 `data/`（"跑一次测试不该污染安装包目录"，那条理由
是对的）。于是它打印"备份成功 / 77 MB / 250 文件"，而 zip 里一条真数据都没有 ——
备份这种东西只在要回滚的那天才被打开，坏在那天等于没有。

所以这里钉四件事：
1. 备份的是**装着那一份**，找不到它宁可抛，绝不回落到仓库 `data/`；
2. 活的 sqlite 走 `backup()`，zip 里那个库读得回来、数据在；
3. 写完回头看一眼：源目录里每个 `*.db` 都必须在 zip 里，少一个就抛（不完整不等于成功）；
4. core 抛的是 `DataBackupError`（不是 SystemExit —— 那是 CLI 的形状），CLI 把它转成
   非零退出，应用端点把它转成 HTTP 错误。
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from rolecard_agent.features import data_backup as mod
from rolecard_agent.features.data_backup import DataBackupError, backup, restore


def _make_root(root: Path) -> None:
    """一份像样的"装着的应用"的数据根：一个 WAL 库 + 一个非库文件 + wal/shm 伴生件。"""
    (root / "sqlite").mkdir(parents=True)
    db = root / "sqlite" / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE session_thread (thread_id TEXT PRIMARY KEY, updated_at TEXT)")
    conn.execute("INSERT INTO session_thread VALUES ('s_proactive_elysia', '2026-09-26 13:28:00')")
    conn.commit()
    (root / "pet-prefs.json").write_text('{"alwaysOnTop": true}', "utf-8")
    # 伴生件必须在**连接关掉之后**手写：给一个活着的库写 `-shm` 不是"造个文件"，
    # 那是一块被映射着的区，`write_bytes` 当场 OSError 22。
    conn.close()
    (root / "sqlite" / "app.db-wal").write_bytes(b"stale wal bytes")
    (root / "sqlite" / "app.db-shm").write_bytes(b"shm")


def test_it_backs_up_the_installed_root_and_reads_back(tmp_path: Path) -> None:
    src = tmp_path / "installed"
    _make_root(src)

    out = backup(tmp_path / "dest", root=src)

    names = zipfile.ZipFile(out).namelist()
    assert "sqlite/app.db" in names, "主库没进 zip —— 这种备份回滚不了任何东西"
    assert "pet-prefs.json" in names
    assert "sqlite/app.db-wal" not in names and "sqlite/app.db-shm" not in names, (
        "wal/shm 与主库一起打包会得到一份不同步的半成品，它们不该单独进来"
    )
    with zipfile.ZipFile(out) as z:
        z.extract("sqlite/app.db", tmp_path / "restored")
    back = sqlite3.connect(tmp_path / "restored" / "sqlite" / "app.db")
    rows = back.execute("SELECT thread_id FROM session_thread").fetchall()
    assert [r[0] for r in rows] == ["s_proactive_elysia"], "快照里读不回原数据"
    back.close()


def test_it_refuses_to_guess_and_never_falls_back_to_the_repo(tmp_path: Path, monkeypatch) -> None:
    """候选里没有"仓库外那份"时宁可抛。

    回落 = 把仓库的 `data/`（可能是一份停在昨天的陈旧快照）当作用户的真数据备份出去，
    而它会长得完全像一次成功。
    """
    repo_like = tmp_path / "repo" / "data" / "sqlite" / "app.db"
    repo_like.parent.mkdir(parents=True)
    repo_like.touch()
    # `_in_repo` 的仓库根经 `repo_root` 这个名字进模块命名空间（core import 的那一行）：
    # 把假仓库根 patch 在那个名字上，repo_like 就成了"仓库里那份"⇒ 候选只剩它 ⇒ 拒绝。
    monkeypatch.setattr(mod, "_candidates", lambda: (repo_like,))
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path / "repo")
    with pytest.raises(DataBackupError) as got:
        mod.installed_data_root()
    assert "拒绝备份" in str(got.value)


def test_an_incomplete_zip_is_a_failure_not_a_warning(tmp_path: Path, monkeypatch) -> None:
    """少一个库就是少一个库 —— 抛，别打印 ok；坏包不许留在原地被人当好的。"""
    src = tmp_path / "installed"
    _make_root(src)
    (src / "chroma").mkdir(parents=True)
    other = src / "chroma" / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()

    real = mod._snapshot_db  # noqa: SLF001 - 造"有一个库没快照上"的故障形状

    def _skip_some(reader: Path, writer: Path) -> None:
        if "other.db" in str(reader):
            return  # 模拟"有一个库没快照上"
        real(reader, writer)

    monkeypatch.setattr(mod, "_snapshot_db", _skip_some)
    dest = tmp_path / "dest"
    with pytest.raises(DataBackupError, match="不完整"):
        backup(dest, root=src)
    assert not list(dest.glob("backup-liveroot-*.zip")), "不完整的包必须被撤掉"


def test_restore_roundtrip_into_empty_dir(tmp_path: Path) -> None:
    """R1031-15：备份 → 还原进空目录 → 文件与库逐一对得上（备份从此可消费）。"""
    root = tmp_path / "root"
    (root / "sqlite").mkdir(parents=True)
    (root / "uploads").mkdir()
    conn = sqlite3.connect(root / "sqlite" / "app.db")
    conn.execute("CREATE TABLE t(x)").close()
    (root / "uploads" / "a.txt").write_text("hello", encoding="utf-8")

    out = backup(tmp_path / "dest", root=root)
    into = tmp_path / "restored"
    restore(out, into)
    assert (into / "uploads" / "a.txt").read_text(encoding="utf-8") == "hello"
    reread = sqlite3.connect(into / "sqlite" / "app.db")
    assert reread.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    reread.close()


def test_restore_refuses_nonempty_target_and_zip_slip(tmp_path: Path) -> None:
    """两道闸：非空目标拒绝；zip 里的 `..` 穿越段拒绝（zip-slip）。"""
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "x.txt").write_text("1", encoding="utf-8")
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escape.txt", "nope")
    with pytest.raises(DataBackupError, match="不是空的"):
        restore(evil, occupied)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with pytest.raises(DataBackupError, match="不安全的路径"):
        restore(evil, fresh)


def test_cli_exits_nonzero_when_mechanism_refuses(tmp_path: Path, monkeypatch) -> None:
    """壳的契约（install_package.ps1 第②道吃的就是它）：core 抛 ⇒ 退出码 1 + stderr 可读。"""

    from scripts.tools import backup_data_root as cli

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003 - 造一个"机制拒绝"的形状
        raise DataBackupError("炸了")

    monkeypatch.setattr(cli, "backup", _boom)
    monkeypatch.setattr(cli.sys, "argv", ["backup_data_root.py", "--dest", str(tmp_path)])
    assert cli.main() == 1
