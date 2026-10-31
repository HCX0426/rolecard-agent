"""数据根整体备份/还原的**机制本体**（R1031-15，2026-10-31）。

从前这里的一切住在 `scripts/tools/backup_data_root.py`，于是应用内没有入口：换机
"备份 zip"只有装了 python 的机器做得到（审计快照 §七的定性）。脚本与端点现在共用
这一个模块 —— 备份的判据只有一份事实面（脚本退化为薄 CLI）。

**快照与文件拷贝的先后**见 `backup()` 里的注释（`R102-75` = 台账 DAT-06）：库先、文件后。
为什么 sqlite 三件套走 `sqlite3.backup()` 而不是直接复制：真库通常开着 WAL，
直拷会得到一份主库与 `-wal` 不同步的半成品（表现成"备份里少了最近几条"，
而那种备份只有在要回滚的时候才被发现是坏的）。

数据根**不是** `base.paths.user_data_root()`：那个函数在开发态故意返回仓库的 `data/`
（"跑一次测试不该污染安装包目录"，那条理由是对的），而备份要的正是安装包那一份。
第一版栽在这个上面 —— 它"备份成功"地产出 77 MB 的 zip，里面一条真数据都没有。
所以根的判定（`installed_data_root()`）找**仓库外**那一份，找不到就抛：
回落 = 把仓库的陈旧快照当成用户真数据备份出去，而它长得完全像一次成功。

**这个 zip 里有敏感物（R1031-23）**：sqlite 里的 `model_settings` 存着模型 API key
（明文是设计拍板），备份把它们一并带走。zip 别进网盘/聊天工具；应用内入口的文案
也带着这句话 —— 知道有入口的人，得同时知道包是敏感的。
"""

from __future__ import annotations

import datetime
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from rolecard_agent.base.observability import logline
from rolecard_agent.base.paths import data_root_candidates, repo_root


class DataBackupError(RuntimeError):
    """备份/还原做不成时说的话（中文、可读、带形状）。

    从前机制用 `SystemExit`：那是 CLI 的退出方式，不该是库的。现在脚本的 CLI 捕获
    它转成退出码，应用端点把它转成 HTTP 4xx/5xx —— 一句话在两个出口都成立。
    """


def _candidates() -> tuple[Path, ...]:
    """两份候选数据根的库路径 —— **判定只有一处**（`base.paths.data_root_candidates`，
    `R26-17`）：取证侧 `scratch_db.CANDIDATE_SOURCES` 与备份侧这里读的是同一对。
    """
    return data_root_candidates()


def _in_repo(p: Path) -> bool:
    """这份库住在仓库里吗（开发态那份）。"""
    try:
        p.resolve().relative_to(repo_root().resolve())
    except ValueError:
        return False
    return True


def installed_data_root() -> Path:
    """装着的那一份的数据根。找不到就抛，**不回落**到仓库那份（见模块 docstring）。"""
    for cand in _candidates():
        if not _in_repo(cand) and cand.exists():
            return cand.parents[1]  # <root>/sqlite/app.db → <root>
    names = ", ".join(str(c) for c in _candidates())
    raise DataBackupError(f"没找到装着的那一份的库（候选：{names}）—— 拒绝备份一个不知道是谁的目录")


def _snapshot_db(src: Path, dst: Path) -> None:
    """活的 sqlite 走 `backup()` API：一致性快照，开着程序也不坏（模块 docstring 的理由）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    writer = sqlite3.connect(dst)
    with writer:
        reader.backup(writer)
    reader.close()
    writer.close()


def backup(dest_dir: Path, *, root: Path | None = None) -> Path:
    """整根备份成 zip（库先快照、文件后照抄），返回 zip 路径。"""
    src_root = root or installed_data_root()
    if not src_root.exists():
        raise DataBackupError(f"数据根不存在，没什么可备份：{src_root}")
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
        # **先把库快照下来，再照抄其余文件。** 反过来做会得到一个"库比文件新"的 zip
        # —— 库里写着某条分块已索引，而它的文件没进包，还原之后检索会指到不存在的
        # 分块上（症状是"知识库里有条目却读不出内容"）。这个顺序下最坏只会多拷几份
        # 库还不认识的孤儿文件：**多比少好**。
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
        out.unlink(missing_ok=True)  # "不完整不等于成功"：坏包不许留在原地被人当好的
        raise DataBackupError(f"备份里没有这些库（写出来了但不完整）：{missing}")
    logline(
        "info",
        "data-backup",
        f"BACKUP ok: {out} ({out.stat().st_size:,} bytes, "
        f"{total} files, {len(dbs)} 个 sqlite 快照)",
    )
    return out


def restore(zip_path: Path, into: Path) -> Path:
    """把备份 zip 还原进一个**空目标目录**（备份是可消费的，不只生产）。

    两道闸：目标目录非空就拒绝（还原是"换机/回滚"动作，不是覆盖合并）；zip 里有
    绝对路径或 `..` 穿越段就拒绝（zip-slip，OWASP 文件上载防线的下载侧对称物）。
    """
    if not zip_path.exists():
        raise DataBackupError(f"备份 zip 不存在：{zip_path}")
    into = into.resolve()
    if into.exists() and any(into.iterdir()):
        raise DataBackupError(f"目标目录不是空的，拒绝覆盖（先清空或换目录）：{into}")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        for name in names:
            drives = len(name) > 1 and name[1] == ":"  # Windows 盘符
            if name.startswith("/") or ".." in Path(name).parts or drives:
                raise DataBackupError(f"zip 里有不安全的路径，拒绝还原：{name}")
        into.mkdir(parents=True, exist_ok=True)
        z.extractall(into)
    dbs = list(sorted(into.rglob("*.db")))
    logline(
        "info",
        "data-backup",
        f"RESTORE ok: {zip_path} -> {into} ({len(names)} files, {len(dbs)} 个 sqlite)",
    )
    return into
