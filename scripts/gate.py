"""分层门禁：平时迭代用 --fast，提交/发布前用全量。

为什么存在（用户 2026-09-18：「每次都要跑全量吗？有计时吗？」）：
  * 全量门禁（含真机冒烟）一次 **6~8 分钟**，每次小改都跑是纯浪费 —— 大部分失败
    ruff/mypy/单测 30 秒内就能暴露；
  * 没有**每步计时**，慢了也不知道慢在哪、该优化谁。本脚本每步打印耗时并汇总。

用法：
  python scripts/gate.py --fast   # ruff + mypy + 单测(不带覆盖率, -x) + 一致性  ≈ 1 分钟
  python scripts/gate.py          # 全量：上面 + 覆盖率门槛 + 前端 test/build + 真机冒烟

任何一步失败即停（后续步骤不再跑），但已跑完步骤的耗时仍会打印。

实测记录（2026-09-18，避免重复试错）：
  * fast 层 51.7s：ruff 0.6 / mypy 2.1 / pytest 46.9 / consistency 2.1 —— pytest 占 91%；
  * **pytest-xdist 并行反而更慢**（-n auto 56.6s vs 46.9s）：大量用例各自起临时
    chroma/sqlite，worker 复制导入与建库的开销吃掉了并行收益 —— 别再试；
  * 更快的迭代方式是**只跑相关测试文件**（单文件 ≈ 2s），gate.py 是提交前的最低门槛。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

# (名称, 命令, 是否只在全量跑)
STEPS: list[tuple[str, list[str], bool]] = [
    ("ruff", [PY, "-m", "ruff", "check", "."], False),
    ("mypy", [PY, "-m", "mypy"], False),
    (
        "pytest(-x, 无覆盖率)",
        [PY, "-m", "pytest", "-p", "no:cacheprovider", "-W", "ignore", "-q", "-x"],
        False,
    ),
    ("consistency", [PY, "scripts/check_consistency.py"], False),
    (
        "pytest(覆盖率≥85%)",
        [
            PY,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-W",
            "ignore",
            "--cov=rolecard_agent",
            "--cov-fail-under=85",
            "-q",
        ],
        True,
    ),
    ("前端 vitest", ["npm", "test"], True),
    ("前端 tsc+build", ["npm", "run", "build"], True),
    ("真机冒烟(14 项)", [PY, "scripts/smoke_check.py"], True),
]


def _run(name: str, cmd: list[str], cwd: Path | None = None) -> tuple[bool, float]:
    print(f"\n▶ {name}", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else str(ROOT))
    dt = time.perf_counter() - t0
    print(f"  ⏱ {name}: {dt:.1f}s {'✅' if proc.returncode == 0 else '❌'}", flush=True)
    return proc.returncode == 0, dt


def main() -> int:
    parser = argparse.ArgumentParser(description="分层门禁（带每步计时）")
    parser.add_argument("--fast", action="store_true", help="只跑快速层（约 1 分钟）")
    args = parser.parse_args()

    timings: list[tuple[str, float]] = []
    failures: list[str] = []
    started = time.perf_counter()

    for name, cmd, full_only in STEPS:
        if args.fast and full_only:
            continue
        cwd = ROOT / "frontend" if name.startswith("前端") else None
        ok, dt = _run(name, cmd, cwd)
        timings.append((name, dt))
        if not ok:
            failures.append(name)
            break  # 失败即停：后面的步骤在同一个问题上只会重复失败

    total = time.perf_counter() - started
    print("\n" + "=" * 52)
    print("门禁计时汇总")
    print("=" * 52)
    for name, dt in timings:
        print(f"  {name:24} {dt:6.1f}s")
    print(f"  {'总计':24} {total:6.1f}s（模式：{'fast' if args.fast else 'full'}）")
    if failures:
        print(f"  ❌ 失败步骤：{'、'.join(failures)}")
        return 1
    print("  ✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
