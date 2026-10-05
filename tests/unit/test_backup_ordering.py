"""数据根备份里「先库快照、后文件照抄」这个顺序是有判据的（`R102-75` = 台账里那条没编号的 DAT-06）。

`scripts/backup_data_root.py` 从前先 `copy2` 拷 `chroma/uploads`、后 `backup()` 快照 sqlite，
得到的 zip 里**库比文件新**：库里写着某个分块已索引，而那个分块的文件没进包 —— 还原之后检索
指到不存在的分块上，症状是"知识库里有条目却读不出内容"，而且只在真要回滚的时候才被发现。
顺序反过来最坏只多拷几个库还不认识的孤儿文件：**多比少好**。

两臂都判（`_order_violation` 这条谓词也得能被反序喂红），否则"没违反"可能只是谓词恒真。
"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_backup_module():
    for p in (str(ROOT), str(ROOT / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "backup_data_root_under_test", str(ROOT / "scripts" / "tools" / "backup_data_root.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "liveroot"
    for rel in ("sqlite", "knowledge", "chroma/c1", "uploads"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for db in (root / "sqlite" / "app.db", root / "knowledge" / "chunks.db"):
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('一行')")
        conn.commit()
        conn.close()
    (root / "sqlite" / "app.db-wal").write_bytes(b"stale WAL")
    (root / "chroma" / "c1" / "data.parquet").write_bytes(b"chunk file")
    (root / "uploads" / "a.txt").write_text("上传件", encoding="utf-8")
    return root


def _order_violation(events: list[tuple[str, str]]) -> int | None:
    """返回"第一个抢在最后一批库快照之前发生的文件拷贝"的下标；没有违规则 None。"""
    snapshots = [i for i, (kind, _) in enumerate(events) if kind == "snapshot"]
    copies = [i for i, (kind, _) in enumerate(events) if kind == "copy"]
    if not snapshots or not copies:
        return None if not copies else 0
    last_snapshot = max(snapshots)
    early = [i for i in copies if i < last_snapshot]
    return early[0] if early else None


def test_backup_snapshots_every_db_before_copying_any_file(tmp_path: Path, monkeypatch) -> None:
    mod = _load_backup_module()
    root = _make_root(tmp_path)
    events: list[tuple[str, str]] = []

    def fake_snapshot(src: Path, dst: Path) -> None:
        events.append(("snapshot", src.relative_to(root).as_posix()))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"snapshot")

    def fake_copy2(src: Path, dst: Path, *a, **k):  # noqa: ANN001, ANN002, ANN003
        events.append(("copy", src.relative_to(root).as_posix()))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    monkeypatch.setattr(mod, "_snapshot_db", fake_snapshot)
    monkeypatch.setattr(mod.shutil, "copy2", fake_copy2)

    out = mod.backup(tmp_path / "zips", root=root)

    snapshotted = {name for kind, name in events if kind == "snapshot"}
    copied = {name for kind, name in events if kind == "copy"}
    assert snapshotted == {"sqlite/app.db", "knowledge/chunks.db"}, f"库快照漏了：{snapshotted}"
    assert "uploads/a.txt" in copied and "chroma/c1/data.parquet" in copied
    assert "sqlite/app.db-wal" not in copied, "活的 WAL 不许被照抄（内容已在快照里）"
    assert _order_violation(events) is None, f"有文件抢在库快照之前被拷走：{events}"

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    assert {"sqlite/app.db", "knowledge/chunks.db"} <= names, f"zip 里缺库：{names}"


def test_the_order_predicate_itself_can_be_red(tmp_path: Path) -> None:
    """反序那一臂：从前那种"先拷文件后快照库"必须被谓词抓到，否则上面的绿是恒真。"""
    old_shape = [
        ("copy", "uploads/a.txt"),
        ("copy", "chroma/c1/data.parquet"),
        ("snapshot", "sqlite/app.db"),
        ("snapshot", "knowledge/chunks.db"),
    ]
    assert _order_violation(old_shape) == 0
    new_shape = [("snapshot", "sqlite/app.db"), ("snapshot", "knowledge/chunks.db")] + old_shape
    assert _order_violation(new_shape) == 2, "违规则要指出是哪一次拷贝抢跑的"
