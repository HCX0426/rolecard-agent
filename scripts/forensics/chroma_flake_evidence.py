"""chroma 偶发（`R102-41`）的**现场取证**：命中在册签名那一刻，把"元数据说在、盘上没了"钉下来。

为什么需要这一层（10-03 收盘的状态）：这发偶发三发假设都已被否证，而每次红完只剩一句
`Error creating hnsw segment reader: Nothing found on disk` —— 目录树早被 tmp 回收了，
下一次红又是同一句。取证层从前只做一件事（把首跑/二跑两份日志留下），它**不记录盘上形状**，
所以"谁在什么时候把那个目录抽走"这句话至今没有过一份现场。

这里补的就是那一格：失败时读两份事实并对它们 ——

  1. **进程内的 chroma 注册表**（`SharedSystemClient._identifier_to_system`）—— 本仓已经量到
     它的行为：`del` + `gc.collect()` 摘不掉，只有 `Client.close()` 会（`R102-74`）；
  2. **每个持久化目录里的 `chroma.sqlite3` 的 `segments` 表** —— 逐段问它"你说的 path 在盘上吗、
     里面有什么"。

两条读都按本仓纪律来：sqlite 一律 `mode=ro`，绝不写、绝不改；`segments` 读不出就**如实写
"读不出"**而不是留空（空与干净长得一模一样，是 `R102` 轮那条分母教训）。

签名清单只此一份出处（`pytest_with_evidence.py` 与本模块共用它）—— 两处各抄一份就是给
"改了判据漏了另一处"留门。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

#: 在册的偶发签名。**新增一条就等于承认"这一族我还没定位"，所以要慎。**
CHROMA_FLAKE_SIGNATURES: tuple[str, ...] = (
    "Nothing found on disk",
    "Error creating hnsw segment reader",
    "chromadb.errors.InternalError",
)

#: 一次现场最多列多少段 / 多少个目录项 —— 取证文件要能打开看完，不是越全越好。
MAX_SEGMENTS = 120
MAX_ENTRIES = 8


def hits_signature(text: str) -> bool:
    """这段异常文本是不是**在册**那一发。判据只在这里答一次。"""
    return any(sig in text for sig in CHROMA_FLAKE_SIGNATURES)


def _registry_systems() -> tuple[list[tuple[str, Path | None]], list[str]]:
    """进程内 chroma 注册表 → (`(标识, 持久化目录)` 的列表, 一句话说明的列表)。

    两类东西**分开返回**：把"注册表为空"这种说明塞进路径那一列，调用方就会拿 `Path()` 去包它，
    现场里于是出现「那个目录不在盘上：本进程此刻没有任何 system」这种胡话（第一版真这么印过）。
    """
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
    except Exception as exc:  # noqa: BLE001 - 取证层不能因为依赖不在就崩
        return [], [f"chroma 未导入，注册表读不出：{exc!r}"]
    registry = getattr(SharedSystemClient, "_identifier_to_system", None)
    if not isinstance(registry, dict):
        return [], ["SharedSystemClient._identifier_to_system 不是 dict，读不出（如实记）"]
    if not registry:
        return [], ["注册表为空：本进程此刻没有 system —— 这本身是一条信息，不等于干净"]
    rows: list[tuple[str, Path | None]] = []
    notes: list[str] = []
    for ident, system in registry.items():
        settings = getattr(system, "settings", None)
        persist = getattr(settings, "persist_directory", None)
        if persist:
            rows.append((str(ident), Path(str(persist))))
        else:
            rows.append((str(ident), None))
            notes.append(f"  {ident} 没有 persist_directory（system 形状与预期不符）")
    return rows, notes


def _list_dir(d: Path) -> str:
    """列一个段目录的内容，**列不出就写成人话而不是空**。

    三条口径都在这一个小函数里：
      * 走 `os.scandir`（目录句柄 + 相对名）—— Windows 上超 260 字符的路径里
        `Path.is_file()` 会**返回 False 而不报错**，那会把一份正常的现场读成"目录是空的"；
      * 单个条目 stat 失败只标 `?`，不牵连整段（现场要能指出"哪一件读不出"）；
      * 真的一个都列不出 ⇒ 明写"列不出"并带上原因。空串与"这个段目录本来就是空的"长得一模一样，
        而后者恰恰是我们要靠这份现场区分出来的那一发。
    """
    try:
        with os.scandir(d) as it:
            rows: list[str] = []
            for entry in it:
                try:
                    size = entry.stat().st_size
                    rows.append(f"{entry.name}:{size}")
                except OSError:
                    rows.append(f"{entry.name}:?")
    except OSError as exc:
        return f"（列不出：{exc!r}）"
    if not rows:
        return "（目录存在但一个条目都没有 —— 这一条本身就是要找的形状）"
    return "{" + ", ".join(sorted(rows)[:MAX_ENTRIES]) + "}"


def _segment_state(persist_dir: Path) -> list[str]:
    """把 `segments` 表逐段与盘上对读。**只读**（`mode=ro`），一行都不写。

    对读的形状是 10-03 现学来的一条事实（别再按旧 schema 猜）：chroma 1.5.9 的
    `segments` 表列为 `(id, type, scope, collection)`，**没有 `path` 列**；而盘上的段目录名
    就是 `segment_id` —— `<persist>/<segment_id>/`，里面是 hnsw 那四件
    （`data_level0.bin` / `header.bin` / `length.bin` / `link_lists.bin`）。
    所以"在册的 vector 段 ↔ 它那个目录"这一对，正好就是
    `Nothing found on disk` 在抱怨的东西：元数据说有，盘上没有。
    """
    lines: list[str] = []
    db = persist_dir / "chroma.sqlite3"
    if not db.is_file():
        return [f"  ! 没有 {db.name}（在 {persist_dir}）"]
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return [f"  ! 打不开库（如实记，不当成干净）：{exc!r}"]
    try:
        rows = con.execute("SELECT id, type, scope, collection FROM segments").fetchall()
    except sqlite3.Error as exc:
        return [f"  ! segments 表读不出（如实记）：{exc!r}"]
    finally:
        con.close()
    missing: list[str] = []
    for seg_id, seg_type, scope, coll in rows[:MAX_SEGMENTS]:
        kind = str(seg_type or "")
        if "vector" not in kind.lower():
            # 元数据段住在 chroma.sqlite3 里，本来就没有自己的目录 —— 不是"丢了"
            lines.append(f"  seg={seg_id} scope={scope} {kind}（在 chroma.sqlite3 内，无独立目录）")
            continue
        d = persist_dir / str(seg_id)
        if d.is_dir():
            # 用 os.scandir 而不是 listdir + `(d/e).is_file()`：pytest 的 tmp_path 里那段中文名
            # 加上 uuid 段目录会越过 Windows 的 260 字符线，而**超长路径上 `is_file()` 返回
            # False 而不是报错** —— 第一版就这么把四个明明在着的 hnsw 文件列成了 `内容={}`，
            # 一份会说谎的现场比没有现场更坏。scandir 经目录句柄按相对名 stat，耐长路径。
            listed = _list_dir(d)
            lines.append(f"  seg={seg_id} coll={coll} 目录在盘 内容={listed}")
            continue
        missing.append(f"{seg_id}（coll={coll}）")
        lines.append(f"  seg={seg_id} coll={coll} **目录不在盘上**：{d}")
    lines.append(
        f"  合计 {len(rows)} 段（最多列 {MAX_SEGMENTS}），"
        f"其中 **{len(missing)} 个 vector 段的目录不在盘上**"
        + (f"：{'; '.join(missing[:8])}" if missing else "")
    )
    return lines


def _extra_persist_dirs(roots: list[Path], limit: int = 6) -> list[Path]:
    """注册表之外再找一遍持久化目录：扫给定根里的 `chroma.sqlite3`。

    为什么必须有这条腿（10-03 当场演示出来的）：`Client.close()` 之后注册表**是空的**
    （本仓已量到 chroma 的 system 住在进程级注册表里，只有 close 会摘掉，`R102-74`），
    而偶发的现场可能恰好在"客户端已经关了、tmp 还没回收"那一刻才被钩到 ——
    只问注册表就会写一句"注册表为空"然后**什么盘上形状都不留**。
    """
    found: list[Path] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for db in sorted(root.rglob("chroma.sqlite3")):
                parent = db.parent
                if parent not in found:
                    found.append(parent)
                if len(found) >= limit:
                    return found
        except OSError:
            continue
    return found


def collect_forensics(nodeid: str, exc_text: str, extra_roots: list[Path] | None = None) -> str:
    """一次现场的完整文本。所有"读不出"都写成一行字，不留空。"""
    parts: list[str] = [
        f"nodeid={nodeid}",
        f"时刻={time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        "异常原文（截 2000）:",
        exc_text[:2000],
        "",
        "进程内 chroma 注册表（标识 → 持久化目录）:",
    ]
    rows, notes = _registry_systems()
    for note in notes:
        parts.append("  " + note.strip())
    walked: list[Path] = []
    for ident, persist in rows:
        parts.append(f"  {ident} -> {persist if persist else '（无 persist_directory）'}")
        if persist is None:
            continue
        walked.append(persist)
        if persist.is_dir():
            parts.extend(_segment_state(persist))
        else:
            parts.append(f"  ! 持久化目录本身已经不在盘上：{persist}")

    # 第二条腿：注册表以外，从给定根里现找（同一个目录不重复列）
    extras = [p for p in _extra_persist_dirs(list(extra_roots or [])) if p not in walked]
    parts.append("")
    parts.append(
        "从给定根里另找到的持久化目录"
        f"（扫 {len(extra_roots or [])} 个根，另 {len(extras)} 个）:"
        if extra_roots
        else "（没有给扫描根 —— 调用方可以传 tmp_path，注册表为空时这条腿才留得下盘上形状）"
    )
    for p in extras:
        parts.append(f"  {p}")
        parts.extend(_segment_state(p))
    return "\n".join(parts) + "\n"


def dump_evidence(
    build_dir: Path, nodeid: str, exc_text: str, extra_roots: list[Path] | None = None
) -> Path:
    """落一份现场文件，返回它的路径。名字带序号，**永不覆盖上一次的现场**。"""
    build_dir.mkdir(parents=True, exist_ok=True)
    stem = nodeid.replace("::", "_").replace("/", "_").replace(".py", "")
    name = f"r102-41-evidence-{stem}-{time.strftime('%H%M%S')}-{os.getpid()}.txt"
    path = build_dir / name
    path.write_text(
        collect_forensics(nodeid, exc_text, extra_roots), encoding="utf-8", newline="\n"
    )
    return path


def existing_evidence(build_dir: Path) -> list[Path]:
    """build/ 里已有的现场文件（按名序）。取证包装层靠它把路径喊到屏幕上。"""
    return sorted(build_dir.glob("r102-41-evidence-*.txt"))
