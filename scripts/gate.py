"""分层门禁：平时迭代用 --fast，提交/发布前用全量。

为什么存在（用户 2026-09-18：「每次都要跑全量吗？有计时吗？」）：
  * 全量门禁（含真机冒烟）一次 **6~8 分钟**，每次小改都跑是纯浪费 —— 大部分失败
    ruff/mypy/单测 30 秒内就能暴露；
  * 没有**每步计时**，慢了也不知道慢在哪、该优化谁。本脚本每步打印耗时并汇总。

用法：
  python scripts/gate.py --fast   # ruff + mypy + 单测(-x, 无覆盖率) + 一致性  ≈ 1.5 分钟
  python scripts/gate.py          # 全量：上面(单测换成一趟带覆盖率) + 前端 test/build
                                  #   + dist 入库同步 + 真机冒烟
                                  #   覆盖率那趟仅在改动 src/ 时跑（没碰 src/ 自动跳过，≈ 省 97s）

任何一步失败即停（后续步骤不再跑），但已跑完步骤的耗时仍会打印。

真模型/真机用例带 `live` 标记，**两层门禁都不跑**（`pyproject.toml` 的 addopts 已含
`-m 'not live'`）：一次 8B 抽取比其余 640 个用例加起来还贵，且结果随模型版本漂移。
要跑它们：`pytest -m live`；跑完整冒烟但跳过浏览器那段：`SMOKE_SKIP_UI=1`。

实测记录（2026-09-19，架构审计报告 §6 的提速落地之后；Ollama 在跑的 Windows 本机）：
  * fast 层 83.8s：ruff 1.0 / mypy 6.2 / pytest 73.7 / consistency 3.0 —— pytest 仍占 88%；
  * 提速前同一套 pytest 是 199s：最慢的 `test_records_api` 一条用例（真 8B 抽取）独占 117s。
    现在它离线跑（模型后端指向死端口 + 断言收紧到确定的 502），全套 199s → 73.7s；
    lifespan 的启动预热（真 POST /api/generate，把 8B 钉进显存）由 conftest 统一关闭；
    离线冒烟 13 项 98s → 10s。
  * **pytest-xdist 并行反而更慢**（-n auto 56.6s vs 46.9s）：大量用例各自起临时
    chroma/sqlite，worker 复制导入与建库的开销吃掉了并行收益 —— 别再试；
  * 剩下的 73.7s 里最大的两块：两条指向死端口的抽取用例各 ~7s（langchain 客户端对
    连接失败重试两遍，约 4s/次 —— 减它需要给模型客户端加"重试次数"配置，为测试速度
    改生产默认不值得），其余是 ~640 个用例各自的建库/装配开销。
  * 更快的迭代方式是**只跑相关测试文件**（单文件 ≈ 2s），gate.py 是提交前的最低门槛。
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
# Windows 下 npm 是 .cmd，subprocess 直调 "npm" 解析不到会抛 WinError 2（FileNotFoundError）。
NPM = "npm.cmd" if platform.system() == "Windows" else "npm"

# (名称, 命令, 模式) —— "both"=快/全量都跑; "fast"=仅快门禁; "full"=仅全量
# 覆盖率那趟只在 full 跑，且 _src_changed() 为 False 时跳过（见 main）。
STEPS: list[tuple[str, list[str], str]] = [
    ("ruff", [PY, "-m", "ruff", "check", "."], "both"),
    ("mypy", [PY, "-m", "mypy"], "both"),
    (
        "pytest(-x, 无覆盖率)",
        [PY, "-m", "pytest", "-p", "no:cacheprovider", "-W", "ignore", "-q", "-x"],
        "fast",
    ),
    ("consistency", [PY, "scripts/check_consistency.py"], "both"),
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
        "full",
    ),
    ("前端 vitest", [NPM, "test"], "full"),
    ("前端 tsc+build", [NPM, "run", "build"], "full"),
    ("真机冒烟(14 项)", [PY, "scripts/smoke_check.py"], "full"),
]


def _src_changed() -> bool:
    """覆盖率只量 ``src/``。没碰 ``src/`` 时那趟是纯重跑，可跳过。

    fail-safe：任何不确定（无 git / 找不到基线 / 调用异常）都返回 True（要跑），
    绝不因探测失误而悄悄削弱 85% 安全网。
    """
    try:
        base = (
            subprocess.run(
                ["git", "merge-base", "HEAD", "origin/main"],
                cwd=str(ROOT), capture_output=True, text=True,
            ).stdout.strip()
            or subprocess.run(
                ["git", "merge-base", "HEAD", "main"],
                cwd=str(ROOT), capture_output=True, text=True,
            ).stdout.strip()
        )
        if not base:
            return True  # 找不到基线 → 保守跑
        changed = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
        ).stdout.splitlines()
        untracked = [
            line[3:] for line in subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(ROOT), capture_output=True, text=True,
            ).stdout.splitlines()
            if line.startswith("??")
        ]
        return any(p.startswith("src/") for p in changed + untracked)
    except Exception:
        return True


def _run(name: str, cmd: list[str], cwd: Path | None = None) -> tuple[bool, float]:
    print(f"\n▶ {name}", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else str(ROOT))
    dt = time.perf_counter() - t0
    print(f"  ⏱ {name}: {dt:.1f}s {'✅' if proc.returncode == 0 else '❌'}", flush=True)
    return proc.returncode == 0, dt


# dist 是**有意入库**的第二份事实（clone 后无 node 也能演示、随包后端直接托管它）。
# 入库意味着"必须与源码同时更新"，而这条此前没人把守：六批 UI 整改全部落地后，
# 仓库里的 dist 还停在批次之前 —— 症状不是报错，是 clone 与安装包静静带着旧界面。
DIST_PATH = "frontend/dist"


def _dist_drifted() -> list[str]:
    """刚构建完，`frontend/dist` 却与仓库不一致 ⇒ 入库的那份是旧的。

    只在**全量门禁的构建之后**判：fast 层不跑 build，那时"没差异"只说明没人重建过，
    判断不了陈旧。拿不到 git / 调用异常时返回空（不拦）—— 这条的意义是"确认漂移"，
    一次误报就会让人开始忽略门禁的红。
    """
    try:
        probe = subprocess.run(
            ["git", "status", "--porcelain", "--", DIST_PATH],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return []
    except Exception:
        return []
    return [line for line in probe.stdout.splitlines() if line.strip()]


def _check_dist_sync() -> tuple[bool, float]:
    print("\n▶ dist 入库同步")
    t0 = time.perf_counter()
    drift = _dist_drifted()
    dt = time.perf_counter() - t0
    print(f"  ⏱ dist 入库同步: {dt:.1f}s {'❌' if drift else '✅'}", flush=True)
    if drift:
        print(
            f"  {DIST_PATH} 在重建后与仓库不一致 ⇒ **入库的构建产物是旧的**："
            "clone 出来的界面、以及随包后端托管的那份 dist 都不是当前代码。"
            "把刚构建出来的产物一起提交（或明确决定不入库，再改这条）。",
            flush=True,
        )
        for line in drift[:8]:
            print(f"    {line}", flush=True)
    return not drift, dt


def main() -> int:
    parser = argparse.ArgumentParser(description="分层门禁（带每步计时）")
    parser.add_argument("--fast", action="store_true", help="只跑快速层（约 1 分钟）")
    args = parser.parse_args()

    timings: list[tuple[str, float]] = []
    failures: list[str] = []
    started = time.perf_counter()

    for name, cmd, mode in STEPS:
        if args.fast and mode not in ("fast", "both"):
            continue
        if (not args.fast) and mode not in ("full", "both"):
            continue
        # 覆盖率那趟：没碰 src/ 就跳过（安全网只在数字真会变时跑）
        if (not args.fast) and name.startswith("pytest(覆盖率") and not _src_changed():
            print("\n▶ pytest(覆盖率≥85%)：跳过（src/ 无改动）", flush=True)
            timings.append((name, 0.0))
            continue
        cwd = ROOT / "frontend" if name.startswith("前端") else None
        ok, dt = _run(name, cmd, cwd)
        timings.append((name, dt))
        if not ok:
            failures.append(name)
            break  # 失败即停：后面的步骤在同一个问题上只会重复失败
        # 构建之后才谈得上"入库的 dist 旧没旧"，所以这条挂在这里而不是一致性检查里。
        if name == "前端 tsc+build":
            ok, dt = _check_dist_sync()
            timings.append(("dist 入库同步", dt))
            if not ok:
                failures.append("dist 入库同步")
                break

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
