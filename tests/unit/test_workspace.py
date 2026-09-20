"""任务目录（core/workspace.py + 升级后的 fs 工具）的单元测试。

覆盖五件事：读写往返与环境回落、实时解析（保存即生效）、目录树浏览的边界、
fs 工具用「任务目录」做根（越界仍拒）、以及 fs 操作写审计（actor="agent"）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.config import Settings
from rolecard_agent.core import workspace
from rolecard_agent.core.tools import files as files_mod
from rolecard_agent.core.tools import run as run_mod
from rolecard_agent.core.tools.files import make_file_tools

DEFAULT_DIR = "./data/workspace"


def test_save_load_clear_roundtrip(conn) -> None:
    assert workspace.load_task_dir(conn) is None  # 从未设置 = None（跟随 env）
    saved = workspace.save_task_dir(conn, str(Path("my-task")))  # 相对路径会被解析成绝对
    assert saved == str(Path("my-task").resolve())
    assert workspace.load_task_dir(conn) == saved
    workspace.clear_task_dir(conn)
    assert workspace.load_task_dir(conn) is None


def test_save_creates_missing_dir(conn, tmp_path: Path) -> None:
    target = tmp_path / "brand" / "new"
    assert not target.exists()
    saved = workspace.save_task_dir(conn, str(target))
    assert target.is_dir()  # 用户给的"打算建"的目录也会被建出来
    assert saved == str(target.resolve())


def test_save_rejects_invalid(conn) -> None:
    with pytest.raises(ValueError, match="不能为空"):
        workspace.save_task_dir(conn, "   ")
    # 不可创建（如路径里的非法字符 / 不可写位置）→ 可读错误，且不落任何值
    with pytest.raises(ValueError):
        workspace.save_task_dir(conn, "\x00-bad")
    assert workspace.load_task_dir(conn) is None  # 校验失败 = 整体拒绝，不落半套


def test_resolve_prefers_db_override_over_env(conn, tmp_path: Path) -> None:
    settings = Settings()  # env 默认 ./data/workspace
    assert workspace.resolve_task_dir(settings, conn) == Path(DEFAULT_DIR).resolve()
    workspace.save_task_dir(conn, str(tmp_path / "task"))
    assert workspace.resolve_task_dir(settings, conn) == (tmp_path / "task").resolve()
    workspace.clear_task_dir(conn)
    assert workspace.resolve_task_dir(settings, conn) == Path(DEFAULT_DIR).resolve()  # 回落


def test_dir_resolver_reads_live(conn, tmp_path: Path) -> None:
    """fs 工具根不是 build 时快照：保存任务目录后，下次调用即用新根。"""
    old = tmp_path / "old"
    new = tmp_path / "new"
    resolver = workspace.make_dir_resolver(Settings(), conn)
    assert resolver() == Path(DEFAULT_DIR).resolve()
    workspace.save_task_dir(conn, str(old))
    assert resolver() == old.resolve()  # 同一解析器，无需 rebuild 就换根
    workspace.save_task_dir(conn, str(new))
    assert resolver() == new.resolve()


def test_browse_tree_lists_and_bounds(conn, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    result = workspace.browse_tree(str(tmp_path))
    assert result["path"] == str(tmp_path.resolve())
    assert result["parent"] == str(tmp_path.parent.resolve())  # 「上一级」直接可用
    entries = {e["name"]: e for e in result["entries"]}
    assert entries["sub"]["is_dir"] is True
    assert entries["a.txt"]["is_dir"] is False
    assert entries["a.txt"]["size"] == 2
    assert result["truncated"] is False

    with pytest.raises(ValueError):
        workspace.browse_tree(str(tmp_path / "missing"))


def test_browse_tree_defaults_to_home() -> None:
    result = workspace.browse_tree("")
    assert Path(result["path"]) == Path.home()


def test_fs_tools_use_task_dir_and_still_reject_escape(conn, tmp_path: Path) -> None:
    task = tmp_path / "task"
    workspace.save_task_dir(conn, str(task))
    tools = {t.name: t for t in make_file_tools(settings=Settings(), conn=conn)}

    write = tools["fs_write"].invoke({"path": "notes/hello.md", "content": "你好"})
    assert "已写入" in write
    assert (task / "notes" / "hello.md").read_text(encoding="utf-8") == "你好"

    read = tools["fs_read"].invoke({"path": "notes/hello.md"})
    assert "你好" in read

    listed = tools["fs_list"].invoke({"path": ""})
    assert "notes" in listed

    # 越界仍然拒绝（路径守卫不因换了根而失效）
    escape = tools["fs_read"].invoke({"path": "../../etc/passwd"})
    assert "越界" in escape


def test_fs_actions_are_audited_as_agent(conn, tmp_path: Path) -> None:
    task = tmp_path / "task"
    workspace.save_task_dir(conn, str(task))
    tools = {t.name: t for t in make_file_tools(settings=Settings(), conn=conn)}
    tools["fs_write"].invoke({"path": "f.txt", "content": "审计这个"})
    tools["fs_read"].invoke({"path": "f.txt"})

    rows = conn.execute(
        "SELECT action, target FROM audit_log WHERE actor = 'agent' ORDER BY id"
    ).fetchall()
    actions = [str(r["action"]) for r in rows]
    assert "fs_write" in actions and "fs_read" in actions
    assert all("task" in str(r["target"]) for r in rows)  # 记的是规范化绝对路径


def test_fs_tools_without_conn_fall_back_to_env(tmp_path: Path) -> None:
    """不传 conn（测试场景）：根回落 env workspace_dir，且不审计也不炸。"""
    settings = Settings(workspace_dir=str(tmp_path / "envdir"))
    tools = {t.name: t for t in make_file_tools(settings=settings)}
    out = tools["fs_write"].invoke({"path": "x.txt", "content": "x"})
    assert "已写入" in out
    assert (tmp_path / "envdir" / "x.txt").exists()


def test_resolve_within_allows_inside_rejects_escape(tmp_path: Path) -> None:
    """路径边界唯一实现（收口 §5 冗余）：root 内（含子目录）放行、越界按给定异常拒。"""
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert workspace.resolve_within(
        tmp_path, "a/b", error_cls=ValueError, what="访问文件"
    ) == (tmp_path / "a" / "b").resolve()
    # root 自身允许（normcase 相等那条分支）
    assert workspace.resolve_within(
        tmp_path, ".", error_cls=ValueError, what="访问文件"
    ) == tmp_path.resolve()
    # 越界 → 抛指定异常，文案含动作短语
    with pytest.raises(ValueError, match="只允许访问文件"):
        workspace.resolve_within(
            tmp_path, "../../etc/passwd", error_cls=ValueError, what="访问文件"
        )


def test_files_and_run_boundaries_share_one_impl(tmp_path: Path) -> None:
    """fs 工具与 run_command 的边界委托同一实现，各自保留异常类型与文案。"""
    with pytest.raises(files_mod.FsToolError, match="只允许访问任务目录内的文件"):
        files_mod._resolve_within(tmp_path, "../../escape")
    with pytest.raises(run_mod.RunCommandError, match="只允许在任务目录内执行命令"):
        run_mod._resolve_within(tmp_path, "../../escape")
