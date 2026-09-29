"""覆盖率该不该重跑（`R28-27`）：marker 等于 HEAD 时**看工作树**，不看就静默跳过。

这条防的是「85% 那条线假绿」：`gate.py` 的判据是「自上次覆盖率实跑以来 src 有没有变」，
而它原先把 `last == head` 那条短路排在查工作树**之前** —— 于是最常见的那个形状
（跑过覆盖率 → 改 src 但不提交 → 再跑一次门禁，正是「装前全量」那一步）返回「跳过」，
而下面那几行 `git diff HEAD` 根本没执行过。09-29 现测复现：marker 设成 HEAD、
`src/` 里放一个未跟踪的 .py，旧代码答 False。

这里刻意用**真 git 仓库**而不是替身：判据的全部价值就在于它读的是 git 的真实形状，
mock 掉 git 等于测我自己编的那个判据（未提交 / 未跟踪 / 已暂存三态的差别正是被测对象）。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gate.py"


def _load():
    # 按文件路径加载：pytest 的 pythonpath 只有 src，脚本目录不在上面
    # （与 test_scratch_db 同一个方式）。
    spec = importlib.util.spec_from_file_location("gate_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {args} -> {proc.returncode}: {proc.stderr}"
    return proc.stdout.strip()


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", msg)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个最小真仓库：一次带 src 的提交，然后把 gate 的两个模块级路径指过来。"""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "add", "-A")
    _commit(root, "seed")
    monkeypatch.setattr(gate, "ROOT", root)
    monkeypatch.setattr(gate, "COV_MARKER", root / "build" / ".cov-last-sha")
    return root


def _mark_head(root: Path) -> str:
    head = _git(root, "rev-parse", "HEAD")
    marker = root / "build" / ".cov-last-sha"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(head, encoding="utf-8")
    return head


def _commit_change(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _commit(root, rel)


def test_dirty_src_forces_a_rerun_even_when_head_has_not_moved(repo: Path) -> None:
    """**这条就是缺陷本尊**：marker == HEAD 且 src 有未提交改动 ⇒ 必须重跑。"""
    _mark_head(repo)
    (repo / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert gate._src_changed() is True, "HEAD 没动就被判成「没变化」，那一趟 85% 是假绿"


def test_untracked_new_file_in_src_counts(repo: Path) -> None:
    """未跟踪的新文件也算：`git diff` 那一族看不见它（与 dist 同步那条同一个盲区）。"""
    _mark_head(repo)
    (repo / "src" / "brand_new.py").write_text("NEW = 1\n", encoding="utf-8")
    assert gate._src_changed() is True, "净增一个 src 新文件而覆盖率不重跑"


def test_clean_worktree_at_the_same_head_still_skips(repo: Path) -> None:
    """省时间那一半还得保住：HEAD 没动且工作树干净 ⇒ 跳过（否则每次全量都白跑一趟）。"""
    _mark_head(repo)
    assert gate._src_changed() is False


def test_uncommitted_docs_only_still_skips(repo: Path) -> None:
    """只改 docs（未提交）同样跳过：判据是 src，不是「文件动没动」。"""
    _mark_head(repo)
    (repo / "README.md").write_text("docs only\n", encoding="utf-8")
    assert gate._src_changed() is False


def test_committed_src_since_the_marker_reruns(repo: Path) -> None:
    """marker 停在旧 HEAD、之后提交了 src 改动 ⇒ 重跑（原版修过的那一半）。"""
    _mark_head(repo)
    _commit_change(repo, "src/app.py")
    assert gate._src_changed() is True


def test_committed_docs_since_the_marker_skips(repo: Path) -> None:
    """提交过但只碰 docs ⇒ 仍跳过（「docs 提交遮住 src」那个误判的反面）。"""
    _mark_head(repo)
    _commit_change(repo, "docs/note.md")
    assert gate._src_changed() is False


def test_missing_marker_is_conservative(repo: Path) -> None:
    """没有 marker（首次 / CI / 清过 build/）一律跑。"""
    assert not (repo / "build" / ".cov-last-sha").exists()
    assert gate._src_changed() is True


def test_broken_git_is_conservative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git 探测失败 = 不确定 = 跑，绝不静默当成「没改动」（fail-safe 那条纪律）。"""
    nowhere = tmp_path / "not-a-repo"
    nowhere.mkdir()
    monkeypatch.setattr(gate, "ROOT", nowhere)
    monkeypatch.setattr(gate, "COV_MARKER", nowhere / "build" / ".cov-last-sha")
    assert gate._src_changed() is True
