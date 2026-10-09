"""`refresh_constraints.py` 的判据（ENGI-18 第三路的刷新器，2026-10-09 拍板）。

这脚本会**改写一个被三个 Linux 装配面 `-c` 引用的文件**，所以失效形状都要钉死：
  * 错平台跑（Windows 解析不出带 sys_platform 门的 uvloop）⇒ 必须退 2，**不产出读数**
    —— 否则会把"上游改了声明"写成文件、静默删掉整条约束；
  * 锁里读不出 uvicorn pin / resolve 失败 ⇒ 退 2（"刷不了"绝不许报成"没变化"）；
  * 判"变没变"只看 uvloop 那条结构行（与 recompile_locks 同纪律：注释不是判断内容，
    手改注释不触发空 PR）；
  * `--check` 只问不写。

网络与平台都在**接缝**上：`resolve_uvloop` 可被替换，`platform` 可注入 —— 用例全离线。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "refresh_constraints_under_test",
        str(ROOT / "scripts" / "tools" / "refresh_constraints.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def rc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _load()
    lock = tmp_path / "requirements.lock"
    lock.write_text("uvicorn[standard]==0.54.0\n    # via -r requirements-api.txt\n",
                    encoding="utf-8")
    cons = tmp_path / "constraints-linux.txt"
    monkeypatch.setattr(mod, "LOCK", lock)
    monkeypatch.setattr(mod, "CONSTRAINTS", cons)
    resolved: list[str] = []

    def fake_resolve(ver: str) -> str:
        resolved.append(ver)
        return "9.9.9"

    monkeypatch.setattr(mod, "resolve_uvloop", fake_resolve)
    return mod, lock, cons, resolved


def test_错平台退2且不产出读数(rc) -> None:
    mod, _lock, cons, resolved = rc
    assert mod.main([], platform="nt") == 2, "Windows 上跑必须退 2（读数是错的）"
    assert not cons.exists(), "错平台不许写文件"
    assert resolved == [], "错平台连 resolve 都不许调（不许产生会被误用的读数）"


def test_锁缺uvicorn_pin退2(rc) -> None:
    mod, lock, _cons, _resolved = rc
    lock.write_text("mcp==1.30.0\n", encoding="utf-8")
    assert mod.main([], platform="posix") == 2


def test_resolve失败退2(rc, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, _lock, cons, _resolved = rc

    def boom(_ver: str) -> str:
        raise RuntimeError("pip --dry-run 挂了")

    monkeypatch.setattr(mod, "resolve_uvloop", boom)
    assert mod.main([], platform="posix") == 2
    assert not cons.exists(), "跑不动时文件不许被写"


def test_首写1_同版本再跑0_换版本再写1(rc, capsys) -> None:
    mod, _lock, cons, resolved = rc
    assert mod.main([], platform="posix") == 1, "首写必须报'变了'"
    assert "uvloop==9.9.9" in cons.read_text(encoding="utf-8")
    assert resolved == ["0.54.0"], "resolve 的输入必须是锁里的 uvicorn pin，不是最新版"
    assert mod.main([], platform="posix") == 0, "同版本再跑必须'没变'"
    # 换一个 resolve 结果 → 又是 1，文件跟着换
    mod.resolve_uvloop = lambda _v: "9.9.10"
    assert mod.main([], platform="posix") == 1
    assert "uvloop==9.9.10" in cons.read_text(encoding="utf-8")


def test_check只问不写(rc) -> None:
    mod, _lock, cons, _resolved = rc
    assert mod.main(["--check"], platform="posix") == 1, "有变化该报 1"
    assert not cons.exists(), "--check 不许写盘"
    assert mod.main([], platform="posix") == 1  # 先写上
    assert mod.main(["--check"], platform="posix") == 0, "写过之后 --check 该报 0"
    assert "uvloop==9.9.9" in cons.read_text(encoding="utf-8")


def test_手改注释不触发刷新_判的是结构行(rc) -> None:
    """与 recompile_locks 同一条纪律：注释不是判断内容，按整文件字节比会每周开空 PR。"""
    mod, _lock, cons, _resolved = rc
    mod.CONSTRAINTS = cons
    cons.write_text("# 手写的一行注释\nuvloop==9.9.9\n", encoding="utf-8")
    assert mod.main([], platform="posix") == 0, "版本没动 ⇒ 不许写、不许报'变了'"
    assert cons.read_text(encoding="utf-8").startswith("# 手写的一行注释"), "注释被刷掉了"
