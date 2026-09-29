"""分层门禁：平时迭代用 --fast，提交/发布前用全量。

为什么存在（用户 2026-09-18：「每次都要跑全量吗？有计时吗？」）：
  * 全量门禁（含真机冒烟）一次 **6~8 分钟**，每次小改都跑是纯浪费 —— 大部分失败
    ruff/mypy/单测 30 秒内就能暴露；
  * 没有**每步计时**，慢了也不知道慢在哪、该优化谁。本脚本每步打印耗时并汇总。

用法：
  python scripts/gate.py --fast   # ruff + mypy + 单测(-x, 无覆盖率) + 一致性  ≈ 1.5 分钟
  python scripts/gate.py          # 全量：上面(单测换成一趟带覆盖率) + 前端 test/build
                                  #   + dist 入库同步 + README 可跑性 + 随包后端 parity + 真机冒烟
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
import contextlib
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COV_MARKER = ROOT / "build" / ".cov-last-sha"
def _venv_python() -> str:
    """按平台找虚拟环境里那个解释器，找不到就退回**调用方自己用的那个**。

    为什么不能写死 Windows 那条路径：CI 的 Linux runner 上没有 `.venv/Scripts/python.exe`，
    每一步都会以"文件不存在"失败（`bin/python` 才是那边的形状），而那副模样看起来像
    "门禁跑了、全红"。同理，runner 上通常压根没有 `.venv` 目录 —— 依赖是直接装进系统
    python 的，这时用 `sys.executable` 才是对的，而不是硬凑一个不存在的虚拟环境。
    """
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",  # Windows
        ROOT / ".venv" / "bin" / "python",  # Linux / macOS
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


PY = _venv_python()

# 步骤名带中文与「▶」，而 Windows 控制台默认 codepage 是 GBK：不重配编码，脚本在**第一个 print**
# 就抛 UnicodeEncodeError 退场，一步都没跑（实测在 git-bash 里中招；输出 pipe 给 tail 时
# 退出码被 tail 吃掉，于是看起来像成功）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Windows 下 npm 是 .cmd，subprocess 直调 "npm" 解析不到会抛 WinError 2（FileNotFoundError）。
NPM = "npm.cmd" if platform.system() == "Windows" else "npm"

# (名称, 命令, 模式) —— "both"=快/全量都跑; "fast"=仅快门禁; "full"=仅全量
# 覆盖率那趟只在 full 跑，且 _src_changed() 为 False 时跳过（见 main）。
STEPS: list[tuple[str, list[str], str]] = [
    ("ruff", [PY, "-m", "ruff", "check", "."], "both"),
    ("mypy", [PY, "-m", "mypy"], "both"),
    # scripts/ 是 2957 行**取证尺子**，从前只过 ruff 不过 mypy（09-26 轮 R26-21）。
    # 补上第一天就抓到两个运行时已经坏了的脚本（seed_demo_data / run_eval 调
    # `make_embedder` 少两个必填关键字参数 ⇒ 一跑就 TypeError），见那一轮台账 S-6 行。
    ("mypy scripts/", [PY, "-m", "mypy", "scripts/"], "both"),
    # Linux 档的类型检查。容器跑的就是 Linux（Dockerfile `python:3.13-slim`），而本机
    # 门禁只查 win32 档 —— CI 第一发就红在这上面（`ctypes.WinDLL` 在 Linux 档没有、
    # POSIX 分支的 `type: ignore` 在 Linux 档成了 unused）。“本机绿”而“容器里红”属于
    # 同一个假绿家族，两边一起查才关得掉。
    ("mypy(linux 档)", [PY, "-m", "mypy", "--platform", "linux"], "both"),
    # shell/（Electron 壳）此前全程无人检查：它有 `npm run typecheck` 但门禁只 cd frontend。
    ("shell typecheck", [NPM, "run", "typecheck"], "full"),
    (
        "pytest(-x, 无覆盖率)",
        [PY, "-m", "pytest", "-p", "no:cacheprovider", "-W", "ignore", "-q", "-x"],
        "fast",
    ),
    ("consistency", [PY, "scripts/check_consistency.py"], "both"),
    ("baseline --check", [PY, "scripts/baseline.py", "--check"], "full"),
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
    # README 的"快速开始"是 US-6 硬门槛（干净环境 ≤3 条命令）的唯一载体，此前没有任何
    # 尺子看着它 —— 落档时实测第 7 步那条裸 uvicorn 在 src 布局下必挂（R28-36）。
    # 这条探针把文档里的启动命令**原样执行一次**并等 /api/health（数据根走临时目录）。
    # 判据只有一份实现（`R28-31`：此前 CI 与本机各写一条 git 命令，CI 那条还看不见未跟踪文件）：
    ("dist 入库同步", [PY, "scripts/check_dist_sync.py"], "full"),
    ("README 可跑性", [PY, "scripts/probe_readme_quickstart.py"], "full"),
    # 随包后端的 import↔bundle parity（R28-34）。只在 full：它量的是**产物**，快档没有产物。
    # `build/sidecar/` 不存在时脚本自己返回 0 并打出"没打过包不是负面"（与"未知不拦"同一条判据），
    # 所以这一条不会因为"这轮没打包"而假红 —— 但它一旦有包就必查。
    ("随包后端 parity", [PY, "scripts/check_bundle_parity.py"], "full"),
    ("真机冒烟(14 项)", [PY, "scripts/smoke_check.py"], "full"),
]


def _git(*args: str) -> str:
    """git 查询：失败就抛（调用方按"不确定 = 保守跑"处理，绝不静默当成"没改动"）。

    `encoding="utf-8"` 不是可选的：本仓有中文文件名，git 吐出来的是 UTF-8，而 Windows 上
    `text=True` 默认按 GBK 解 —— 解码在**读子进程输出的那个线程**里抛 UnicodeDecodeError，
    主线程只会拿到一份被截断的 stdout（实测 09-25 就在 `diff --name-only` 上中招）。
    截断不是"少几行"，`_src_changed()` 会拿着半份清单判断要不要跑覆盖率，那是一次静默失效。
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {proc.returncode}")
    return proc.stdout


def _src_changed() -> bool:
    """覆盖率只量 ``src/``。自**上次覆盖率实跑**以来没碰 ``src/`` 时那趟是纯重跑，可跳过。

    判据 2026-09-28 修过一个盲区：原版兜底看「最近一个提交」，于是「提交 src 改动 → 再提交
    一个纯 docs 的」就把 src 的改动遮住了 —— 装前全量门禁静默跳过覆盖率，而那恰是十次打包
    规矩里唯一必须实跑它的时刻。现改为记录**上次覆盖率实跑时的 HEAD**（build/ 不入库）：
    marker 缺失（首次 / CI / 清过 build/）一律保守跑；git 挂了等任何不确定也一律 True
    —— fail-safe 不变，绝不因探测失误而悄悄削弱 85% 安全网。

    同一族的第二个盲区（`R28-27`，09-29 现测复现）：HEAD 相等那条短路**排在查工作树之前**，
    于是"覆盖率跑过 → 改 src 不提交 → 再跑一次门禁"照样返回"跳过"（marker == HEAD，直接就
    return False 了，那几行查 `git diff HEAD` 的代码根本不执行）。修法不是补条件而是**换顺序**：
    工作树这一趟先算，因为它要防的正是"HEAD 没动"那一刻。
    """
    try:
        head = _git("rev-parse", "HEAD").strip()
        # 未提交（含已暂存）与未跟踪：两条都要，Vite 那类"净增新命名产物"的形状同理。
        work = _git("diff", "--name-only", "HEAD").splitlines()
        work += _git("ls-files", "--others", "--exclude-standard").splitlines()
        dirty_src = any(p.startswith("src/") for p in work)
        if COV_MARKER.exists():
            last = COV_MARKER.read_text(encoding="utf-8").strip()
            if last == head:
                return dirty_src  # HEAD 没动：工作树里有没有 src 就是全部判据
            if last:
                changed = _git("diff", "--name-only", f"{last}..HEAD").splitlines()
                return dirty_src or any(p.startswith("src/") for p in changed)
        # 没有 marker（首次 / CI / 清过 build/）：保守跑一趟并从此留下基准。
        return True
    except Exception:
        return True


def _run(name: str, cmd: list[str], cwd: Path | None = None) -> tuple[bool, float]:
    print(f"\n▶ {name}", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else str(ROOT))
    dt = time.perf_counter() - t0
    print(f"  ⏱ {name}: {dt:.1f}s {'✅' if proc.returncode == 0 else '❌'}", flush=True)
    return proc.returncode == 0, dt


# CI 档跳过的步骤：要么要 node/浏览器/真机环境（前端与壳各有专属 job、冒烟要本机 Chrome），
# "dist 入库同步"在 CI 上由 frontend job 跑**同一个脚本**（那个 job 才装 node、才真的重建）。
# 名单而不是标志位：加一步新检查时默认进 CI，除非在这里点名跳过 —— 漏跑的代价比多跑大。
CI_SKIP = frozenset(
    {
        "shell typecheck",
        "前端 vitest",
        "前端 tsc+build",
        "dist 入库同步",
        "真机冒烟(14 项)",
    }
)


def main() -> int:
    parser = argparse.ArgumentParser(description="分层门禁（带每步计时）")
    parser.add_argument("--fast", action="store_true", help="只跑快速层（约 1 分钟）")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI 档：全量层但跳过需要 node/浏览器/真机的步骤，且覆盖率**必跑**",
    )
    args = parser.parse_args()

    timings: list[tuple[str, float]] = []
    failures: list[str] = []
    started = time.perf_counter()

    for name, cmd, mode in STEPS:
        if args.ci and name in CI_SKIP:
            continue  # CI 档不跑 node/真机那几步（它们各有专属 job 或要本机环境）
        if args.fast and mode not in ("fast", "both"):
            continue
        if (not args.fast) and (not args.ci) and mode not in ("full", "both"):
            continue
        # 覆盖率那趟：本地全量档"没碰 src/ 就跳过"；**CI 档必跑** —— 用户 09-29 拍了
        # "不手动"，85% 这条线从此在每次 push/PR 上设防（R28-24 的修法）。
        if (
            (not args.fast)
            and (not args.ci)
            and name.startswith("pytest(覆盖率")
            and not _src_changed()
        ):
            print("\n▶ pytest(覆盖率≥85%)：跳过（src/ 无改动）", flush=True)
            timings.append((name, 0.0))
            continue
        # 步骤跑在哪个目录按名字前缀定（比在元组里再加一个字段少一处噪声）。第一版
        # 我把 shell 那步写成"全局 npm run typecheck"，于是它在仓库根跑、根本没有这个
        # script —— 报错的样子像"壳的类型检查挂了"，其实是步目录错了。
        if name.startswith("前端"):
            cwd: Path | None = ROOT / "frontend"
        elif name.startswith("shell"):
            cwd = ROOT / "shell"
        else:
            cwd = None
        ok, dt = _run(name, cmd, cwd)
        timings.append((name, dt))
        if not ok:
            failures.append(name)
            break  # 失败即停：后面的步骤在同一个问题上只会重复失败
        if name.startswith("pytest(覆盖率"):
            # 记下"覆盖率这次是在哪个 HEAD 上实跑的"：_src_changed 的判据靠它，
            # 纯 docs 的后续提交才不会再把 src 的改动遮住。写不了 marker 只会让
            # 下次多跑一趟覆盖率，不是错误。
            with contextlib.suppress(Exception):
                COV_MARKER.parent.mkdir(parents=True, exist_ok=True)
                COV_MARKER.write_text(_git("rev-parse", "HEAD").strip(), encoding="utf-8")
    total = time.perf_counter() - started
    print("\n" + "=" * 52)
    print("门禁计时汇总")
    print("=" * 52)
    for name, dt in timings:
        print(f"  {name:24} {dt:6.1f}s")
    tier = "fast" if args.fast else "ci" if args.ci else "full"
    print(f"  {'总计':24} {total:6.1f}s（模式：{tier}）")
    if failures:
        print(f"  ❌ 失败步骤：{'、'.join(failures)}")
        return 1
    print("  ✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
