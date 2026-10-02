"""任务目录变化侦测（架构总览 §5 文件事件触发的基础层）—— 轮询快照 + 基线 diff。

## 为什么是轮询而不是文件系统事件

B/S 形态下后端进程不保证驻留在目录旁边（网络盘 / WSL / 容器挂载），watch API 跨平台
行为差异大；`size + int(mtime)` 的元数据快照 30s 一轮的 stat 成本可接受（上限兜底），
且**绝不读文件内容**——内容读取是角色自己的 fs 工具的事（带审计与路径边界）。

## 状态口径（铁律：唯一全局的是用户级护栏）

任务目录本身是全局单值（`kernel_meta` 的 workspace 绑定），变化事件与哈希基线因此
**全局一份**（`kernel_meta` 单行 JSON）；"哪个角色可被触发"是角色属性，归 role_card 的
per-role 闸门，由调用方判定。基线时间戳只在**推进基线**时刷新，扫描本身不动它 ——
否则事件会被无限续期，永不过期。

## 生命周期

首次 / 根目录热切 → 只建基线不触发（新绑定的目录里全是"旧文件"，没有可播报的变化）；
此后每轮 diff 出增/改/删即挂起为待通知事件；事件保持到高优先角色成功开口（或被任意
角色消费）后由调用方推进基线；超过 `EVENT_EXPIRY` 无人开口则过期丢弃（重扫建基线）。

领域中性纪律：本模块不感知"角色/开口"概念，check_changes 只回答"目录相对基线变了
没有"；触发口吻与素材注入在调用方（core/reachout/triggers.py）。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from rolecard_agent.storage.db import SqlConnection

# 单行 JSON 状态的键（kernel_meta，与 workspace:dir 同一张表、同一 upsert 先例）。
FILE_WATCH_KEY = "file_watch:state"

# 事件过期：挂起超过该时长仍没有任何角色消费 → 丢弃重建基线（防陈旧事件无限压制他源）。
EVENT_EXPIRY = timedelta(hours=24)

# 扫描上限：再多就截断（标注 truncated）—— 挡住 node_modules 级目录把一轮 tick 拖死。
MAX_FILES = 2000
MAX_DEPTH = 4

# 忽略的目录/文件名：版本库、依赖、构建产物、缓存与临时文件（`.` 前缀另判）。
IGNORE_NAMES = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".cache",
        ".idea",
        ".vscode",
    }
)


# 待通知事件的持久化形态。
class FileEvent(TypedDict):
    op: str  # add | mod | del
    path: str


class WatchState(TypedDict, total=False):
    root: str
    entries: dict[str, list[int]]  # 相对路径 -> [size, int(mtime)]
    baseline_utc: str
    changed: list[FileEvent]
    truncated: bool


def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def scan_snapshot(root: Path) -> tuple[dict[str, list[int]], bool]:
    """任务目录元数据快照：{相对 posix 路径: [size, mtime整秒]}。

    只 stat 不读内容；跳过隐藏项 / IGNORE_NAMES / 超深层级；条目超 MAX_FILES 截断
    （返回 truncated=True，diff 时随事件挂起，供素材说明"不止这些"）。
    目录不可读 → 返回空快照（调用方下一轮自然重试，绝不打死调度线程）。
    """
    entries: dict[str, list[int]] = {}
    truncated = False
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(dirpath)
            depth = len(base.relative_to(root).parts)
            dirnames[:] = [d for d in dirnames if d not in IGNORE_NAMES and not d.startswith(".")]
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            for name in filenames:
                if name.startswith(".") or name in IGNORE_NAMES:
                    continue
                if len(entries) >= MAX_FILES:
                    truncated = True
                    break
                try:
                    st = (base / name).stat()
                except OSError:
                    continue
                rel = base.relative_to(root) / name
                entries[rel.as_posix()] = [st.st_size, int(st.st_mtime)]
            if truncated:
                break
    except OSError:
        return {}, False
    return entries, truncated


def load_state(conn: SqlConnection) -> WatchState | None:
    """读持久化状态；缺行 / JSON 损坏 / 形态不认识 → None（等同首次，重建基线）。"""
    row = conn.execute("SELECT value FROM kernel_meta WHERE key = ?", (FILE_WATCH_KEY,)).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(str(row["value"]))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or "entries" not in raw or "root" not in raw:
        return None
    state: WatchState = {
        "root": str(raw["root"]),
        "entries": {k: [int(v[0]), int(v[1])] for k, v in raw["entries"].items()},
        "baseline_utc": str(raw.get("baseline_utc", "")),
        "changed": [
            FileEvent(op=str(e.get("op", "")), path=str(e.get("path", "")))
            for e in raw.get("changed", [])
            if isinstance(e, dict)
        ],
        "truncated": bool(raw.get("truncated", False)),
    }
    return state


def save_state(conn: SqlConnection, state: WatchState) -> None:
    """单行 upsert（照 workspace.save_task_dir 的 kernel_meta 先例）。"""
    conn.execute(
        "INSERT INTO kernel_meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (FILE_WATCH_KEY, json.dumps(state, ensure_ascii=False)),
    )
    conn.commit()


def clear_state(conn: SqlConnection) -> None:
    conn.execute("DELETE FROM kernel_meta WHERE key = ?", (FILE_WATCH_KEY,))
    conn.commit()


def diff_entries(old: dict[str, list[int]], new: dict[str, list[int]]) -> list[FileEvent]:
    """基线 -> 现照的增/改/删清单（size 或 mtime 任一变化即 mod）。"""
    events: list[FileEvent] = []
    for path in sorted(set(new) - set(old)):
        events.append(FileEvent(op="add", path=path))
    for path in sorted(set(old) & set(new)):
        if old[path] != new[path]:
            events.append(FileEvent(op="mod", path=path))
    for path in sorted(set(old) - set(new)):
        events.append(FileEvent(op="del", path=path))
    return events


def check_changes(
    conn: SqlConnection, root: Path, *, now_utc: datetime | None = None
) -> tuple[list[FileEvent], bool, bool] | None:
    """核心入口：本轮有没有"值得播报的目录变化"。

    返回 None = 无事件（含"本轮只建/重建基线"）；
    返回 (变更清单, truncated, expired) = 有挂起事件。`expired` = 事件已挂起超过
    `EVENT_EXPIRY`（基线时间戳至今）——调用方应放弃该事件并推进基线。
    **本函数只挂起事件、不推进基线**——基线推进必须等调用方确认事件被消费（开口成功）
    或过期，否则变化会被白白吞掉。
    """
    now = now_utc or datetime.now(UTC)
    entries, truncated = scan_snapshot(root)
    state = load_state(conn)
    root_str = str(root)

    if state is None or state["root"] != root_str:
        # 首次 / 任务目录热切：全是"旧文件"，没有可播报的变化 —— 只建基线。
        save_state(
            conn,
            WatchState(
                root=root_str,
                entries=entries,
                baseline_utc=_fmt(now),
                changed=[],
                truncated=False,
            ),
        )
        return None

    if state["changed"]:
        # 已有挂起事件：不重复 diff、不覆盖清单；只报告它是否已过期。
        stamp = _parse_ts(state["baseline_utc"])
        expired = stamp is not None and now - stamp > EVENT_EXPIRY
        return state["changed"], state["truncated"], expired

    events = diff_entries(state["entries"], entries)
    if not events:
        return None
    state["changed"] = events
    state["truncated"] = truncated
    state["baseline_utc"] = _fmt(now)  # 事件新鲜度从此刻计（挂起期间扫描不再续期）
    save_state(conn, state)
    return events, truncated, False


def advance_baseline(conn: SqlConnection, root: Path, *, now_utc: datetime | None = None) -> None:
    """消费/过期后推进：重扫现照为新基线，清空挂起事件。"""
    now = now_utc or datetime.now(UTC)
    entries, _ = scan_snapshot(root)
    save_state(
        conn,
        WatchState(
            root=str(root),
            entries=entries,
            baseline_utc=_fmt(now),
            changed=[],
            truncated=False,
        ),
    )


def pending_count(conn: SqlConnection) -> int:
    """当前挂起的变更条数（收件箱提示用）；无状态/无事件 = 0。"""
    state = load_state(conn)
    return len(state["changed"]) if state else 0


__all__ = [
    "EVENT_EXPIRY",
    "FILE_WATCH_KEY",
    "FileEvent",
    "advance_baseline",
    "check_changes",
    "clear_state",
    "diff_entries",
    "load_state",
    "pending_count",
    "save_state",
    "scan_snapshot",
]
