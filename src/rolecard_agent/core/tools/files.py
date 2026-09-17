"""工作区文件工具：fs_read / fs_write / fs_list —— 给角色的"电脑文件读写"能力。

## 为什么手写而不直接用开源轮子

评估过两个现成轮子，都不适用：
  * langchain-community 的 FileManagementToolkit —— 官方已宣布 sunset；
  * MCP filesystem server —— 需要先引入完整 MCP 客户端（路线 B）。
工具 I/O 本身是标准库，真正有分量的部分是**路径边界守卫**，而守卫必须达到本项目
上传路径同级的 rigor（H1：resolve 收敛相对段与符号链接 + normcase 比较 + 越界测试），
所以约百行的实现里，安全部分是项目自己的，不是轮子能替的。

## 边界（这是"读写用户磁盘"的动作，规则比功能更保守）

  * 所有路径**必须落在 `WORKSPACE_DIR` 内**（默认 `./data/workspace`），`..` 与符号链接
    都被 resolve 收敛后由 is_relative_to 拦下 —— 与上传路径守卫同一套 rigor；
  * `fs_read` 有单文件大小上限（读进 prompt 的东西都要有上界）；
  * `fs_write` **不声明幂等**（执行器不会重试它），创建父目录，写入即真实落盘；
  * 全部工具只作用于工作区 —— 角色卡 + 白名单决定谁拥有它们。
"""

from __future__ import annotations

import os
from pathlib import Path

from langchain_core.tools import tool

from rolecard_agent.core.tools.errors import ToolExecutionError

READ_MAX_CHARS = 200_000  # 单文件读入 prompt 的字符上限（约 20 万字符）


class FsToolError(ToolExecutionError):
    """文件工具的可读失败。"""


def _resolve_within(root: Path, rel_path: str) -> Path:
    """把（可能带 .. / 子目录 / 大小写差异的）相对路径收敛到工作区内的绝对路径。

    与上传路径守卫（domains/<域>/tools.resolve_upload_target）同一套 rigor：
    resolve() 收敛 `..` 与符号链接，is_relative_to 拦越界，normcase 抹平 Windows 大小写。
    """
    root_res = Path(root).resolve()
    target = (root_res / rel_path).resolve()
    if os.path.normcase(str(target)) != os.path.normcase(
        str(root_res)
    ) and not target.is_relative_to(root_res):
        raise FsToolError(f"路径越界：只允许访问工作区目录内的文件（{root_res}）。")
    return target


def make_file_tools(*, settings) -> list:
    """构建工作区文件工具。目录不存在时惰性创建（fs_write / fs_list 需要）。"""
    root = Path(settings.workspace_dir)

    @tool("fs_read")
    def fs_read(path: str) -> str:
        """读取工作区内的一个文本文件，返回其内容。path 是相对工作区的路径，如 notes/todo.md。"""
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
        return f"（文件 {path}，{target.stat().st_size} 字节）\n{text}"

    @tool("fs_write")
    def fs_write(path: str, content: str) -> str:
        """把文本内容写入工作区内的一个文件（会创建缺失的父目录，覆盖同名文件）。
        path 是相对工作区的路径。"""
        try:
            target = _resolve_within(root, path)
        except FsToolError as exc:
            return str(exc)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            return f"写入失败：{exc}"
        return f"已写入 {path}（{len(content.encode('utf-8'))} 字节）。"

    @tool("fs_list")
    def fs_list(path: str = "") -> str:
        """列出工作区内某个目录（默认根目录）的文件与子目录，含大小。
        path 为空或 "." 表示工作区根目录。"""
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
        return f"（工作区 {path or '.'}，共 {len(entries)} 项）\n" + "\n".join(lines)

    return [fs_read, fs_write, fs_list]


__all__ = ["FsToolError", "make_file_tools"]
