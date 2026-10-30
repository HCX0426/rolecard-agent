"""把**装着的那一份**应用的数据根整体备份成一个 zip（装包之前先做这一步）。

为什么单独一个脚本而不是"手工记着备份"：2026-09-26 第六次打包时就漏这一步 —— 装完才补了一份，
那时"装坏了拿什么回滚"已经没有了。备份是安装流程的一环，不是一个提醒。

**快照与文件拷贝的先后**见下方 `backup()` 里的注释（`R102-75`）：库先、文件后。
为什么 sqlite 三件套走 `sqlite3.backup()` 而不是直接复制：真库通常开着 WAL，
直拷会得到一份主库与 `-wal` 不同步的半成品（表现成"备份里少了最近几条"，
而那种备份只有在要回滚的时候才被发现是坏的）。

数据根**不是** `base.paths.user_data_root()`：那个函数在开发态故意返回仓库的 `data/`
（"跑一次测试不该污染安装包目录"），而这里要的正是安装包那一份。第一版就栽在这个上面 ——
它"备份成功"地产出了一个 77 MB、250 文件的 zip，里面一条真数据都没有，全是仓库那份陈旧快照。
所以根的判定复用 `scratch_db.CANDIDATE_SOURCES`（"两个数据根"这件事在本仓只允许有一处判定，
见 `R26-17`），并且写完 zip 之后**回头查一遍**：源目录里每个 `*.db` 都必须在 zip 里，
少一个就抛。少了主库的备份不是备份。

**这个 zip 里有敏感物（R1031-23，2026-10-31 注）**：sqlite 里的 `model_settings` 存着
模型 API key（明文是设计拍板，见台账），备份会把它们一并带走。zip 别进网盘/聊天工具；
要分享先确认不需要 key 或自行清库后再备份。
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
# scratch_db 住 forensics/（脚本分层后各归其层）——同一份"两个数据根"的判定。
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "forensics"))


def installed_data_root() -> Path:
    """装着的那一份的数据根。找不到就抛，**不回落**到仓库那份。"""
    for cand in _candidates():
        if not _in_repo(cand) and cand.exists():
            return cand.parents[1]  # <root>/sqlite/app.db → <root>
    names = ", ".join(str(c) for c in _candidates())
    raise SystemExit(f"没找到装着的那一份的库（候选：{names}）—— 拒绝备份一个不知道是谁的目录")


def _candidates():
    from scratch_db import CANDIDATE_SOURCES

    return CANDIDATE_SOURCES


def _in_repo(p: Path) -> bool:
    try:
        p.resolve().relative_to(_REPO_ROOT.resolve())
    except ValueError:
        return False
    return True


def _snapshot_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    writer = sqlite3.connect(dst)
    with writer:
        reader.backup(writer)
    reader.close()
    writer.close()


def backup(dest_dir: Path, *, root: Path | None = None) -> Path:
    src_root = root or installed_data_root()
    if not src_root.exists():
        raise SystemExit(f"数据根不存在，没什么可备份：{src_root}")
    sources = [p for p in sorted(src_root.rglob("*")) if p.is_file()]
    # 活的 sqlite（含 -wal / -shm 与那些 `*.db.备份名` 历史件）单独走 backup()，其余照抄
    dbs = [p for p in sources if p.suffix == ".db"]
    skip = {p for p in sources if p.suffix in (".db", ".db-wal", ".db-shm") or ".db-" in p.name}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = dest_dir / f"backup-liveroot-{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="rc-backup-"))
    missing: list[str] = []
    try:
        # 顺序是判据，不是风格（`R102-75`，就是台账里那条没编号的 DAT-06）：
        # **先把库快照下来，再照抄其余文件。** 反过来做（从前就是这样）会得到一个
        # "库比文件新"的 zip —— 库里写着某条分块已索引，而它的文件没进包，
        # 还原之后检索会指到不存在的分块上（症状是"知识库里有条目却读不出内容"）。
        # 这个顺序下最坏只会多拷几份库还不认识的孤儿文件：**多比少好**。
        for db in dbs:
            _snapshot_db(db, tmp / db.relative_to(src_root))
        for src in sources:
            if src in skip:
                continue
            dst = tmp / src.relative_to(src_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(tmp.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(tmp).as_posix())
        with zipfile.ZipFile(out) as z:
            wrote = set(z.namelist())
        missing = [
            rel for rel in (d.relative_to(src_root).as_posix() for d in dbs) if rel not in wrote
        ]
        total = len(wrote)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if missing:
        raise SystemExit(f"备份里没有这些库（写出来了但不完整）：{missing}")
    print(
        f"BACKUP ok: {out} ({out.stat().st_size:,} bytes, {total} files, {len(dbs)} 个 sqlite 快照)"
    )
    return out


def restore(zip_path: Path, into: Path) -> Path:
    """把备份 zip 还原进一个**空目标目录**（R1031-15：备份此前只会生产不会消费）。

    两道闸：目标目录非空就拒绝（还原是"换机/回滚"动作，不是覆盖合并）；zip 里有
    绝对路径或 `..` 穿越段就拒绝（zip-slip，OWASP 文件上载防线的下载侧对称物）。
    """
    if not zip_path.exists():
        raise SystemExit(f"备份 zip 不存在：{zip_path}")
    into = into.resolve()
    if into.exists() and any(into.iterdir()):
        raise SystemExit(f"目标目录不是空的，拒绝覆盖（先清空或换目录）：{into}")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        for name in names:
            drives = len(name) > 1 and name[1] == ":"  # Windows 盘符
            if name.startswith("/") or ".." in Path(name).parts or drives:
                raise SystemExit(f"zip 里有不安全的路径，拒绝还原：{name}")
        into.mkdir(parents=True, exist_ok=True)
        z.extractall(into)
    dbs = list(sorted(into.rglob("*.db")))
    print(f"RESTORE ok: {zip_path} -> {into} ({len(names)} files, {len(dbs)} 个 sqlite)")
    return into


def main() -> int:
    ap = argparse.ArgumentParser(description="备份/还原装着的那一份应用的数据根")
    ap.add_argument("--dest", default="build", help="（备份档）zip 落在哪（默认仓库的 build/）")
    ap.add_argument(
        "--root",
        default=None,
        help="覆盖数据根（只给测试用；默认是装着的那一份，不回落仓库 data/）",
    )
    ap.add_argument(
        "--restore",
        default=None,
        metavar="ZIP",
        help="还原档：把这个备份 zip 还原进 --into 指定的**空**目录",
    )
    ap.add_argument(
        "--into", default=None, metavar="DIR", help="（还原档）还原到哪；必须是不存在或为空的目录"
    )
    args = ap.parse_args()
    if args.restore:
        if not args.into:
            raise SystemExit("--restore 需要成对的 --into（还原到哪个空目录）")
        restore(Path(args.restore), Path(args.into))
        return 0
    if args.into:
        raise SystemExit("--into 只跟 --restore 成对使用")
    backup(Path(args.dest), root=Path(args.root) if args.root else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
