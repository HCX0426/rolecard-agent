"""工作区文件工具：fs_read / fs_write / fs_list —— 给角色的"电脑文件读写"能力。

## 为什么手写而不直接用开源轮子

评估过两个现成轮子，都不适用：
  * langchain-community 的 FileManagementToolkit —— 官方已宣布 sunset；
  * MCP filesystem server —— 需要先引入完整 MCP 客户端（路线 B）。
工具 I/O 本身是标准库，真正有分量的部分是**路径边界守卫**，而守卫必须达到本项目
上传路径同级的 rigor（H1：resolve 收敛相对段与符号链接 + normcase 比较 + 越界测试），
所以约百行的实现里，安全部分是项目自己的，不是轮子能替的。

## 边界（这是"读写用户磁盘"的动作，规则比功能更保守）

  * 所有路径**必须落在根目录内**。根不再是 build 时固定的 `WORKSPACE_DIR` 快照，而是
    `dir_resolver()` 每次调用实时解析的任务目录（DB「设置→通用」可配，保存即生效，
    见 core/workspace.py）—— 这样"角色读写的范围"跟着用户的授权走；
  * `..` 与符号链接都被 resolve 收敛后由 is_relative_to 拦下 —— 与上传路径守卫同一套 rigor；
  * `fs_read` 有单文件大小上限（读进 prompt 的东西都要有上界）；
  * `fs_write` **不声明幂等**（执行器不会重试它），创建父目录，写入即真实落盘；
  * 全部操作**写审计**（actor="agent"，action=fs_*，路径与字节数，不记内容）——
    "角色碰电脑"必须可追溯（架构计划 A·§4.2；仅传入 conn 时启用，测试可不传）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from langchain_core.tools import tool

from rolecard_agent.config import Settings
from rolecard_agent.core.tools.errors import ToolExecutionError
from rolecard_agent.core.workspace import make_dir_resolver, resolve_within
from rolecard_agent.storage.db import SqlConnection

READ_MAX_CHARS = 200_000  # 单文件读入 prompt 的字符上限（约 20 万字符）


class FsToolError(ToolExecutionError):
    """文件工具的可读失败。"""


def _resolve_within(root: Path, rel_path: str) -> Path:
    """fs 工具的路径边界：委托给 `core/workspace.resolve_within`（唯一实现），
    只把它抛出的异常换成文件工具的可读失败类型。"""
    return resolve_within(
        root, rel_path, error_cls=FsToolError, what="访问任务目录内的文件"
    )


def _audit(
    conn: SqlConnection | None,
    action: str,
    target: str,
    detail: dict[str, object] | None = None,
) -> None:
    """写审计（actor 固定 "agent"：模型触发的工具动作，与操作员的 operator 动作区分）。

    与 roles.audit 同列结构；不传 conn = 跳过（测试/未接审计的宿主，fail-open 只影响
    留痕、不影响权限）。
    """
    if conn is None:
        return
    conn.execute(
        "INSERT INTO audit_log (actor, action, target, detail_json) VALUES (?, ?, ?, ?)",
        (
            "agent",
            action,
            target,
            None if detail is None else json.dumps(detail, ensure_ascii=False),
        ),
    )
    conn.commit()


def make_file_tools(
    *,
    settings: Settings,
    conn: SqlConnection | None = None,
    dir_resolver: Callable[[], Path] | None = None,
) -> list:
    """构建工作区文件工具。

    `dir_resolver` = 每调用实时解析的任务目录；不传时回落 env 的 settings.workspace_dir。
    `conn` = 审计写入用的连接（不传不审计）。
    """
    if dir_resolver is not None:
        root_of = dir_resolver
    elif conn is not None:
        root_of = make_dir_resolver(settings, conn)
    else:
        root_of = lambda: Path(settings.workspace_dir)  # noqa: E731 - 闭包，模块内约定

    @tool("fs_read")
    def fs_read(path: str) -> str:
        """读取任务目录内一个文本文件并返回内容；path 相对任务目录，如 notes/todo.md。"""
        root = root_of()
        try:
            target = _resolve_within(root, path)
        except FsToolError as exc:
            return str(exc)
        if not target.is_file():
            return f"文件不存在：{path}"
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"读取失败：{exc}"
        if len(text) > READ_MAX_CHARS:
            text = text[:READ_MAX_CHARS] + f"\n…（文件过长，已截断，共约 {len(text)} 字符）"
        _audit(conn, "fs_read", str(target), {"size": target.stat().st_size})
        return f"（文件 {path}，{target.stat().st_size} 字节）\n{text}"

    @tool("fs_write")
    def fs_write(path: str, content: str) -> str:
        """把文本内容写入任务目录内的一个文件（会创建缺失的父目录，覆盖同名文件）。
        path 是相对任务目录的路径。"""
        root = root_of()
        try:
            target = _resolve_within(root, path)
        except FsToolError as exc:
            return str(exc)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            return f"写入失败：{exc}"
        bytes_written = len(content.encode("utf-8"))
        _audit(conn, "fs_write", str(target), {"bytes": bytes_written})
        return f"已写入 {path}（{bytes_written} 字节）。"

    @tool("fs_list")
    def fs_list(path: str = "") -> str:
        """列出任务目录内某个目录（默认根目录）的文件与子目录，含大小。
        path 为空或 "." 表示任务目录根目录。"""
        root = root_of()
        try:
            target = _resolve_within(root, path or ".")
        except FsToolError as exc:
            return str(exc)
        if not target.is_dir():
            return f"目录不存在：{path or '.'}"
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        if not entries:
            return "（空目录）"
        lines = []
        for entry in entries[:100]:
            if entry.is_dir():
                lines.append(f"[目录] {entry.name}/")
            else:
                lines.append(f"        {entry.name}（{entry.stat().st_size} 字节）")
        _audit(conn, "fs_list", str(target), {"entries": len(entries)})
        return f"（任务目录 {path or '.'}，共 {len(entries)} 项）\n" + "\n".join(lines)

    return [fs_read, fs_write, fs_list]


__all__ = ["FsToolError", "make_file_tools"]
