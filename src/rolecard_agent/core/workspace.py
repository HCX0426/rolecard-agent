"""任务目录（工作区）—— 角色"碰电脑"的授权边界与目录浏览。

## 两层来源（与本项目其它配置同一哲学）

  * **env / Settings.workspace_dir** 是部署期引导（出厂默认 `./data/workspace`）；
  * **DB 覆盖（kernel_meta `workspace:dir`）** 是操作员时刻：设置页选好的任务目录写入
    这里，`resolve_task_dir` 把它叠到 env 之上 —— **后写者胜**，删除覆盖即回落 env
    （与 runtime_settings 的 override 语义一致，只是这里存的是目录路径而非 Settings 字段）。

## 为什么工具根要用"每调用实时解析"而不是 build 时快照

fs 工具的运行边界必须跟着"用户此刻在设置页选的任务目录"走。build 时快照会让
"刚换了目录、下一轮对话仍用旧目录"，与「插件启停即时生效」的直觉冲突。所以
`make_dir_resolver` 返回的解析器**每次调用都读一次 kernel_meta**（PRIMARY KEY 点查，
微秒级），保存即生效、无需 rebuild —— 这是文件权限功能本身的正确设计，不是锦上添花。

## 目录浏览（tree）的边界

浏览是**人**在设置页选目录用的，不是给模型的能力：所以它只读、只列单层、限制条目数、
不递归，且**不必限制在任务目录内**（选目录本身就是"授权哪个目录"）。危险操作（跟随
符号链接写、深度递归、删除）都不在这个接口内。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rolecard_agent.config import Settings
from rolecard_agent.storage.db import SqlConnection

# kernel_meta 键：与 `memory:facts` / `runtime:<field>` 同一命名约定。
WORKSPACE_KEY = "workspace:dir"

# 单层目录浏览的条目上限：设置页选择器足够用，也挡住了"几万个文件的目录"把响应打爆。
TREE_MAX_ENTRIES = 500


def load_task_dir(conn: SqlConnection) -> str | None:
    """读 DB 覆盖的任务目录；没设置过 = None。"""
    row = conn.execute("SELECT value FROM kernel_meta WHERE key = ?", (WORKSPACE_KEY,)).fetchone()
    return str(row["value"]) if row else None


def _normalise(raw: str) -> Path:
    """把用户输入的路径解析成规范绝对路径，目录不存在则创建。

    规则：strip 空 → ValueError；resolve 收敛相对路径/`..`/大小写；落地前创建
    （用户可能给一个"打算建"的新目录）；创建失败（无权限路径等）→ ValueError 可读原因。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("任务目录不能为空。")
    target = Path(text).expanduser().resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建任务目录 {target}：{exc}") from exc
    if not target.is_dir():
        raise ValueError(f"任务目录不是目录：{target}")
    return target


def save_task_dir(conn: SqlConnection, raw: str) -> str:
    """保存任务目录（规范绝对路径）并返回落库值。非法输入整体拒绝（不落任何值）。"""
    target = _normalise(raw)
    conn.execute(
        "INSERT INTO kernel_meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (WORKSPACE_KEY, str(target)),
    )
    conn.commit()
    return str(target)


def clear_task_dir(conn: SqlConnection) -> None:
    """清除 DB 覆盖，回落 env（Settings.workspace_dir）。"""
    conn.execute("DELETE FROM kernel_meta WHERE key = ?", (WORKSPACE_KEY,))
    conn.commit()


def resolve_task_dir(settings: Settings, conn: SqlConnection) -> Path:
    """当前生效的任务目录：DB 覆盖优先，其次 env/出厂默认。

    两条分支都返回**规范绝对路径**（与 save 落库值同一口径）：调用方拿到的就是
    立即可用的根，不用再猜它是相对还是绝对。
    """
    override = load_task_dir(conn)
    return Path(override) if override else Path(settings.workspace_dir).resolve()


def make_dir_resolver(settings: Settings, conn: SqlConnection) -> Callable[[], Path]:
    """fs 工具用的根解析器：每次调用实时读 DB 覆盖（保存即生效，无需 rebuild）。

    与 `enabled_domains` / `memory_provider` 同一模式 —— 内核工具持有的是"每轮现取"
    的读取器，而不是构建时的快照。
    """
    return lambda: resolve_task_dir(settings, conn)


def browse_tree(raw: str) -> dict[str, object]:
    """列目录的单层内容（人用的选择器后端，不是模型的工具）。

    `raw` 为空 → 从用户主目录起步（设置页第一次打开时有个可用的起点）。
    返回 `{path, entries:[{name, is_dir, size}], truncated}`；目录不可读/不存在抛
    ValueError（路由层转 400）。
    """
    target = Path(raw).expanduser().resolve() if (raw or "").strip() else Path.home()
    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise ValueError(f"无法列出目录 {target}：{exc}") from exc
    shown: list[dict[str, object]] = []
    for entry in entries:
        try:
            is_dir = entry.is_dir()
            size = 0 if is_dir else entry.stat().st_size
        except OSError:
            continue  # 权限不足/已消失的条目直接跳过，不让一个坏条目炸掉整棵树
        shown.append({"name": entry.name, "is_dir": is_dir, "size": size})
    truncated = len(shown) > TREE_MAX_ENTRIES
    if truncated:
        shown = shown[:TREE_MAX_ENTRIES]
    return {
        "path": str(target),
        "parent": str(target.parent),  # 前端「上一级」直接调这个值，不在前端拼路径
        "entries": shown,
        "truncated": truncated,
    }


__all__ = [
    "WORKSPACE_KEY",
    "browse_tree",
    "clear_task_dir",
    "load_task_dir",
    "make_dir_resolver",
    "resolve_task_dir",
    "save_task_dir",
]
