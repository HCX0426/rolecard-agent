"""`_tracked_files` 的路径形状：`core.quotepath` 不许把名单变成假名字（CI 实测换来的）。

真事故（run 37852226595，2026-10-09，日志原文）：
    [scan_secrets] 名单里有 37 项此刻不在盘上（竞态/目录项），举例：['"data/lore/01-\\345\\237…md"']
    [scan_secrets] 范围 = git 名单 702 个文件（影子树）
CI（Linux）的 `core.quotepath` 默认 **true**：`git ls-files` 把非 ASCII 路径输出成
**带双引号的八进制转义**。影子树拿那种假名字去 `is_file()` 必然假 ⇒ 本次要发的源码里
**37 个文件一个都没进扫描**，而那一步照样退 0、绿。本机 Git-for-Windows 默认 `false`
⇒ 同一份代码在这台机器上永远是对的 —— 典型的"只有某个平台才暴露"。

这一档不用替身：在 `tmp_path` 里 `git init` 一个真仓库、把它的 `core.quotepath` **设成
true**（写进那个 repo 的 config，于是任何 `git -C` 调用都走转义路径），再放一个非 ASCII
文件名。这就把 CI 的形状搬到了本机 —— 去掉 `-z` 这一发必须红（变异实测过）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from scan_secrets import ScopeMismatch, _shadow_tree, _tracked_files  # noqa: E402

LORE = "data/lore/01-基本设定与人设.md"


def _make_repo(tmp_path: Path, *, quote: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        assert p.returncode == 0, f"git {' '.join(args)} 失败：{p.stderr[:200]}"

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "probe")
    git("config", "core.quotepath", quote)
    victim = repo / LORE
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("SECRET_HINT = 1\n", encoding="utf-8")
    git("add", "-A")
    return repo


def test_转义开启时名单仍给可用的原名(tmp_path: Path) -> None:
    """`-z` 的全部意义：`core.quotepath=true`（CI 的形状）下名单还能直接 `is_file()`。"""
    repo = _make_repo(tmp_path, quote="true")
    files = _tracked_files(repo)
    assert LORE in files, f"名单里没有原样路径（被转义了？）：{files}"
    assert not any(n.startswith('"') or "\\3" in n for n in files), (
        f"名单里出现引号/八进制转义形状：{files}"
    )
    # 真正要紧的那一句：这个名字在盘上找得到
    assert (repo / LORE).is_file(), "名单给的名字在盘上用不了 = 那个文件不会被扫"


def test_影子树在转义开启时照样建起来(tmp_path: Path) -> None:
    """端到端：`_tracked_files` → `_shadow_tree` 在 CI 形状下不抛、且文件真进了树。"""
    repo = _make_repo(tmp_path, quote="true")
    shadow = _shadow_tree(repo, _tracked_files(repo))
    try:
        assert (shadow / LORE).is_file(), "文件没进影子树 = 它免检了"
    finally:
        import shutil

        shutil.rmtree(shadow, ignore_errors=True)


def test_名单里有盘上找不到的项必须抛而不是打印一声(tmp_path: Path) -> None:
    """掩体拆除那一格：旧写法只 print「竞态/目录项」然后继续退 0（37 个文件就是这么漏的）。

    现在必须 `ScopeMismatch` —— 由 `main()` 收成语义明确的退 2（见
    `test_范围建不起来退2而不是traceback`）。
    """
    repo = _make_repo(tmp_path, quote="false")
    with pytest.raises(ScopeMismatch):
        _shadow_tree(repo, [LORE, "不存在的东西.py"])


def test_退码语义_范围没定下来是2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:
    """退出码就是判据（0 干净 / 1 发现 / 2 扫不成）：范围建不起来必须走 2，不许抛 traceback。

    没有这一格的话，`ScopeMismatch` 会一路冲出 `main()`：门禁照样红，但打出来的是 traceback，
    等于给这条线新造第四种码（本仓在 `OSError` 那格已经为同一件事付过一次）。
    """
    scan = _load_scan_secrets()
    repo = _make_repo(tmp_path, quote="true")
    (repo / ".gitleaks.toml").write_text("[allowlist]\n", encoding="utf-8")
    monkeypatch.setattr(scan, "_tool_path", lambda: repo / "gitleaks")
    # 名单里掺一个不存在的路径 ⇒ `_shadow_tree` 抛 ScopeMismatch
    monkeypatch.setattr(scan, "_tracked_files", lambda _r: [LORE, "ghost.py"])
    assert scan.main(["--repo", str(repo)]) == 2, "范围没定下来却没退 2（或抛了 traceback）"
    err = capsys.readouterr().err
    assert "影子树建不起来" in err, err[-300:]


def _load_scan_secrets():
    """`main()` 住在脚本里，用 importlib 取（与本档其他用例同一手法）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "scan_secrets_scope_under_test", str(ROOT / "scripts" / "tools" / "scan_secrets.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_默认配置下也拿得到原名(tmp_path: Path) -> None:
    """反向一臂（`quotepath=false`，也就是本机形状）：同一份代码两边都给原名。

    只测转义那一臂会漏掉一种修法：把 `-z` 换成"手工反解八进制"——那种写法在 false 下
    容易把正常的引号文件名弄坏。两边都要过。
    """
    repo = _make_repo(tmp_path, quote="false")
    assert LORE in _tracked_files(repo)
