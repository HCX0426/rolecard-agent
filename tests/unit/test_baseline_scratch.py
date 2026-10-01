"""`scripts/baseline.py` 的暂存件卫生 —— 它自己就是"每跑一次留一对"的那个泄漏源。

为什么给一条基线脚本写单测（与 `test_scratch_db.py` 同一个理由）：`build/_baseline/` 在
gitignore 里，所以它长到什么尺寸**没有任何东西会报警**。第一版 `_scratch()` 只删"自己这一对"
（文件名带 pid，别的 pid 永远撞不上），10-01 清点时那里堆了 100 个文件 / 15 MB。
这两条用例钉的是"扫旧的"与"不杀活的"，缺一不可：只写前一条，实现会退化成按 pid 全删，
把并发的另一份 baseline（本机 + CI）踩坏。
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "baseline.py"


def _load(tmp_path: Path) -> object:
    """重新 import 一份，把 ROOT 指到临时目录 —— 不许碰真的 build/_baseline/。"""
    spec = importlib.util.spec_from_file_location(f"baseline_t_{tmp_path.name}", str(SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = tmp_path
    return mod


def _drop(root: Path, name: str, *, age_seconds: float) -> Path:
    directory = root / "build" / "_baseline"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"")
    touched = time.time() - age_seconds
    os.utime(path, (touched, touched))
    return path


def test_stale_scratch_is_swept(tmp_path: Path) -> None:
    mod = _load(tmp_path)
    old = _drop(tmp_path, "routes-1111.db", age_seconds=mod._STALE_SCRATCH_SECONDS + 60)
    old_wal = _drop(tmp_path, "declared-1111.db-wal", age_seconds=mod._STALE_SCRATCH_SECONDS + 60)
    mine = mod._scratch("routes.db")
    assert mine.name == f"routes-{os.getpid()}.db"
    assert not old.exists(), "超过龄期的暂存件没被扫掉 —— 这就是那 100 个文件的来路"
    assert not old_wal.exists(), "-wal / -shm 这些附属件同样要扫"


def test_fresh_scratch_is_not_swept(tmp_path: Path) -> None:
    """并发的另一份 baseline 还在用它的暂存件 —— 龄期之内一律不动。"""
    mod = _load(tmp_path)
    live = _drop(tmp_path, f"routes-{os.getpid() + 1}.db", age_seconds=5)
    mod._scratch("declared.db")
    assert live.exists(), "按 pid 全删会踩坏同时在跑的那一份（本机 + CI 是两条真的并发）"
