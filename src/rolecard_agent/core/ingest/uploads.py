"""上传目录的孤儿文件：盘点（只读）与回收（需显式确认）。

## 为什么需要它

上传目录是**只增不减**的：上传端点写 `<uuid8>_<name>`、解析文本另存一份 `.parsed.txt`
副本，而删会话不删文件、也没有任何 GC。M4 修掉"重复上传再写一份副本"之后，新增的垃圾
止住了，但**历史遗留的副本与半成品仍在**（仓库里实际就能看到 `01b7f378_x.bin` 这类残留）。

## 这是"删用户数据"的动作，所以规则比功能更保守

  1. **只扫上传目录内的普通文件**，`resolve()` 之后必须仍在目录内 —— 符号链接指向外部时
     不越界（上传目录本身由本服务创建，但目录内容可能被人工干预过）；
  2. **只认"没有任何 ingestion_task 引用"的文件**：只要有一条台账指向它，就不动；
  3. **`.parsed.txt` 跟随主文件**：主文件被引用则副本保留，主文件孤立则副本一起回收；
  4. **先盘点、后执行**：`scan_orphans` 是纯只读，接入层把它给操作员看过再调删除；
  5. **执行必须留痕**：删了多少个文件、多少字节，写进审计。

第 4 条是刻意的：一步到位的 `DELETE /cleanup` 很省事，但"点一下就永久删掉一批用户文件"
不该是一个没有预览面的操作。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# 解析文本副本的后缀。落点由下面的 `parsed_text_path` 定 —— 后缀与落点从此同一处，
# 从前副本函数住在 `api/deps.py`，本文件只剩这行后缀加一句"与那边一致"：隔着一层
# 互相望着的两处事实面，而 core 想知道副本长什么样反而够不到 api。
PARSED_SUFFIX = ".parsed.txt"


def parsed_text_path(target: Path) -> Path:
    """解析文本副本的落点：主文件同目录 `<原名>.parsed.txt`（随 uploads/ 一起被 gitignore）。

    为什么落盘：结构化抽取需要原文，而图片的解析要走 OCR 子进程（很贵）。上传时顺手存一份，
    抽取就不必再跑一次 OCR。
    """
    return target.with_name(target.name + PARSED_SUFFIX)


@dataclass(frozen=True, slots=True)
class OrphanFile:
    """一个待回收的文件：名字 + 大小 + 是否属于某个已孤立主文件的副本。"""

    name: str
    size: int
    companion: bool


@dataclass(frozen=True, slots=True)
class OrphanReport:
    """盘点结果。`referenced` / `scanned` 让操作员能自己核对"为什么只删了这些"。

    `dangling` 是**反方向**的那一半（`R28-19`）：台账写着原件、这台机器上却没有那个文件。
    没有它，"数据根被搬走了一半"这种状态在界面上表现为"一切正常，0 个孤儿"—— 沉默就是缺陷，
    而它当时是真的：安装根那份 `ingestion_task` 里三行 `source_file` 指仓库旧根的绝对路径，
    而安装根的 uploads 目录是空的，扫描对这批台账一个字都不说。
    """

    files: tuple[OrphanFile, ...]
    total_bytes: int
    scanned: int
    referenced: int
    dangling: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "orphans": [
                {"name": f.name, "size": f.size, "companion": f.companion} for f in self.files
            ],
            "total_bytes": self.total_bytes,
            "scanned": self.scanned,
            "referenced": self.referenced,
            "dangling": list(self.dangling),
        }


def normalize(path: str | Path) -> str:
    """路径比较用规范形：`resolve()` 归一相对段与符号链接，`normcase` 抹平大小写。

    两侧必须用**同一个**函数：上传端点存库的是它写文件时的路径，扫描时拿到的是目录项，
    不归一就会出现"明明有台账引用却被判为孤儿"——那会直接删掉在用的文件。
    """
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = Path(path)
    return os.path.normcase(str(resolved))


def referenced_paths(source_files: Iterable[str | None]) -> set[str]:
    """台账里的 `source_file` 全部归一成一个集合。

    入参是 `Iterable` 而不是 `list`：函数只做遍历，而 `list` 在类型系统里是不变的
    （`list[str]` 不能传给 `list[str | None]`）—— 用 `list` 会把调用方逼成多余的类型转换。
    """
    return {normalize(s) for s in source_files if s}


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False


def scan_orphans(upload_dir: Path, referenced: set[str]) -> OrphanReport:
    """只读盘点：列出上传目录里没有被任何台账引用的文件。**不删任何东西。**

    认"这份原件还在不在"用的是**文件名**而不是整条路径（`R28-19`）：台账写的是它当时被
    上传到哪个根（换过数据根之后那是另一个绝对路径），而文件就在这台机器的目录里躺着。
    按名字认的误差方向是"多算成有引用" ⇒ 少删，不会删掉在用的文件，这比反过来安全。
    """
    root = upload_dir
    names = {Path(item).name for item in referenced if item}
    if not root.is_dir():
        # 目录都不在：没有可删的东西，但**台账里那些行必须说出来**，不然这一格永远空白。
        return OrphanReport(
            files=(),
            total_bytes=0,
            scanned=0,
            referenced=len(referenced),
            dangling=tuple(sorted(names)),
        )

    entries: list[tuple[str, int, Path]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or not _inside(entry, root):
            continue  # 目录、越界符号链接一律跳过
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        entries.append((entry.name, size, entry))

    # 主文件集合：名字不以 .parsed.txt 结尾的那些。
    primaries = {name for name, _, _ in entries if not name.endswith(PARSED_SUFFIX)}

    orphans: list[OrphanFile] = []
    referenced_count = 0
    for name, size, _entry in entries:
        if name in names:
            referenced_count += 1
            continue
        if name.endswith(PARSED_SUFFIX):
            # 副本：主文件被引用（或被删但台账仍在）→ 保留；主文件也不在 → 一起回收。
            base = name[: -len(PARSED_SUFFIX)]
            base_entry = root / base
            base_referenced = base in primaries and (
                base in names or normalize(base_entry) in referenced
            )
            if base_referenced:
                referenced_count += 1
                continue
            orphans.append(OrphanFile(name=name, size=size, companion=True))
            continue
        orphans.append(OrphanFile(name=name, size=size, companion=False))

    # 反方向：台账说有、目录里没有的那个原件（跨数据根搬过来的行就是这个形状）。
    dangling = tuple(sorted(names - {name for name, _, _ in entries}))

    return OrphanReport(
        files=tuple(orphans),
        total_bytes=sum(f.size for f in orphans),
        scanned=len(entries),
        referenced=referenced_count,
        dangling=dangling,
    )


def remove_orphans(upload_dir: Path, report: OrphanReport) -> tuple[int, int]:
    """按盘点结果回收。返回 `(删除数, 释放字节数)`。

    逐个删除并**吞掉单个文件的失败**：一个文件被外部进程占用不该让整批回收中止，
    而"实际删掉了几个"由返回值如实给出（调用方写审计）。同时再校验一次路径仍在目录内 ——
    盘点到删除之间目录内容可能已经变了，删除前重新确认比信任旧清单更稳。
    """
    root = upload_dir
    deleted = 0
    freed = 0
    for item in report.files:
        target = root / item.name
        if not _inside(target, root) or not target.is_file():
            continue
        try:
            size = target.stat().st_size
            target.unlink()
        except OSError:
            continue
        deleted += 1
        freed += size
    return deleted, freed
