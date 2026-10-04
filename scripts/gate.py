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
import json
import os
import platform
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
    # **不在命令行再补 `-q`**：`pyproject.toml` 的 `addopts` 已经带了一个 `-q`，而 pytest 的
    # quiet 是累加的 —— 两个 `-q` 就是 verbosity -2，会把最后那行 `1320 passed in 177.31s`
    # 连同失败时的 `FAILED tests/...` 一起吞掉（实测：本仓整份输出到 `[100%]` 就直接收尾）。
    # 第一版门禁就是这么写的，于是"从输出里读后端用例数"那条读数**永远读不到**，而失败步骤
    # 也只报"这一步红了"报不出是哪条用例 —— 两件事是同一个根。
    (
        # 快档也套上"红跑取证"（`R102-41`）：10-03 这一档第一次报出同一签名，而它没有二跑日志
        # —— 同一发偶发原先只在覆盖率档装了监控。判据与签名清单两档共用一份。
        "pytest(-x, 无覆盖率)",
        [PY, "scripts/pytest_with_evidence.py", "--lane", "fast"],
        "fast",
    ),
    ("consistency", [PY, "scripts/check_consistency.py"], "both"),
    ("baseline --check", [PY, "scripts/baseline.py", "--check"], "full"),
    (
        # 同一趟 pytest，包了一层"红跑取证"（`R102-41`）：只有失败命中在册的 chroma 偶发
        # 签名时才重跑那批文件一次，且重跑**为取证不为转绿**（首跑日志原样留着、屏幕上
        # 大声标 FLAKY-RECORDED）。判据与签名清单在 scripts/pytest_with_evidence.py。
        "pytest(覆盖率≥85%)",
        [PY, "scripts/pytest_with_evidence.py", "--lane", "coverage"],
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
    # 10-01 挪进快档：它新加了**不需要产物**的那半段（spec 的收包清单 ↔ src 的懒加载），
    # 那一半正是 M2 那发变异（把一族从清单里摘掉）唯一的探测器，只在 full 档跑等于"打完才醒"。
    ("随包后端 parity", [PY, "scripts/check_bundle_parity.py"], "both"),
    ("真机冒烟(14 项)", [PY, "scripts/smoke_check.py"], "full"),
    # **末尾再问一次 README 那组数**（`R28-73`）：`consistency` 排在覆盖率与 vitest 之前，
    # 那两个数在本趟里是"比对之后才写进去的"，于是那一格永远晚一趟才发现漂。
    # 判据不重写第二份 —— 这个脚本直接加载 `check_consistency` 只调那一个函数。
    ("README 数字收尾", [PY, "scripts/check_readme_numbers.py"], "both"),
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


def _cwd_for(name: str) -> Path | None:
    """步骤跑在哪个目录按名字前缀定（比在元组里再加一个字段少一处噪声）。第一版
    我把 shell 那步写成"全局 npm run typecheck"，于是它在仓库根跑、根本没有这个
    script —— 报错的样子像"壳的类型检查挂了"，其实是步目录错了。"""
    if name.startswith("前端"):
        return ROOT / "frontend"
    if name.startswith("shell"):
        return ROOT / "shell"
    return None


def _run_captured(name: str, cmd: list[str], cwd: Path | None = None) -> tuple[bool, float, str]:
    """并发组专用的运行器：**捕获**输出、跑完一次打出（流式打印并发会互相穿插）。

    与 `_run` 同一个返回契约（ok, 秒数, 完整输出），只是 io 模式不同 —— 静态四步都
    很快（本机 <30s / runner 带缓存更短），不存在"长步骤静默被当挂死"的顾虑。
    """
    print(f"\n▶ {name}（并发）", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    dt = time.perf_counter() - t0
    output = proc.stdout + (proc.stderr or "")
    code = proc.returncode
    print(output, end="", flush=True)
    print(f"  ⏱ {name}: {dt:.1f}s {'✅' if code == 0 else '❌'}", flush=True)
    return code == 0, dt, output


def _run(name: str, cmd: list[str], cwd: Path | None = None) -> tuple[bool, float, str]:
    """跑一步，返回 (是否通过, 秒数, 该步的完整输出)。

    输出**边跑边打在控制台上，同时留一份在内存里**（第三个返回值）—— 留这一份只为了
    一件事：把 README 首屏那几个数变成"这一步刚才量出来的"，而不是"某人上次手抄的"。
    流式打印不能丢（长步骤静默几分钟会被当成挂死），所以自己按行读而不是 capture_output。
    """
    print(f"\n▶ {name}", flush=True)
    t0 = time.perf_counter()
    collected: list[str] = []
    with subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    ) as proc:
        for line in proc.stdout or ():
            print(line, end="", flush=True)
            collected.append(line)
        code = proc.wait()
    dt = time.perf_counter() - t0
    print(f"  ⏱ {name}: {dt:.1f}s {'✅' if code == 0 else '❌'}", flush=True)
    return code == 0, dt, "".join(collected)


def _readings_path() -> Path:
    """读数的落点：**本机 = 入库那份，runner = 一次性产物**。

    入库的理由（09-30 第六条规矩）：尺子的判据里不许有 gitignore 的东西 —— 判据读到暂存区，
    问的就不再是"这格数是不是真的"，而是"这台机器上有没有这个文件"；放 `docs/` 而不是 `build/`
    只为了干净克隆里它必须在，CI 才有可比的东西。

    但**runner 上不许写它**（10-01 那发 CI 红换来的，`R28-74`）：覆盖率是**按平台**的数 ——
    本机 win32 量到 91.92%，GitHub 的 Linux runner 量到 91.77%（win32/posix 那两条分支各自
    执行不了对方的行）。入库那一份的语义是"README 抄的是谁量的那一次"，让第二个机器去覆盖它，
    等于两份都对的数互相把对方判成漂移。所以 `GATE_READINGS_SCRATCH=1`（ci.yml 里设）时
    落点换成 gitignore 的 `build/`：runner 照样量、照样在自己那趟里自我比对，只是不碰共同记录。
    """
    if os.environ.get("GATE_READINGS_SCRATCH"):
        return ROOT / "build" / "gate-readings.json"
    return ROOT / "docs" / "gate-readings.json"


#: README 首屏那组数的落点。它不是"文档的一部分"，是"上一趟门禁量到了什么"（细节见上面那个函数）。
READINGS = _readings_path()
_READING_PATTERNS = {
    # (读哪个步骤, 正则, 存成什么名)
    "pytest(-x, 无覆盖率)": (r"(\d+) passed", "backend_tests"),
    "pytest(覆盖率≥85%)": (r"Total coverage:\s*([\d.]+)%", "coverage_percent"),
    "前端 vitest": (r"Tests\s+(\d+) passed", "frontend_tests"),
    # **一致性那把尺子有几条，不在这里读，也不在 README 抄**（10-01 撞到的自指）：那 45 条里
    # 含"比对 README 这一条"自己 —— README 一漂就有一条红，读到的"过了几条"立刻少 1，于是那句
    # 数变成两处错；把它改对，下一趟又回到原值。就算改读 `passed + failed` 也还在打转：
    # 那个键只在**全绿的那一趟**才写得动（红了就不写，见 `_write_readings`），而它红的原因恰恰是
    # 它自己。所以这一格退回它本来该在的地方 —— 门禁输出，散文不抄（`docs/开发流程.md` 那条规矩）。
}


#: 读 ANSI/VT 转义序列。读正则**前先剥色**：vitest 即便输出被 pipe 也照样上色，那行
#: `Tests  336 passed` 在字节上是 `\x1b[2m Tests \x1b[22m \x1b[1m\x1b[32m336 passed\x1b[39m…`，
#: 于是"数字前有两个空格"这种写法永远读不到（10-01 实测：`--only 前端` 跑完落的是
#: `frontend_tests_unreadable`）。颜色是呈现层的事，读数不该依赖它。
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _write_readings(outputs: dict[str, str], ok: bool) -> None:
    """把这一步量到的数并进 `docs/gate-readings.json`（入库，理由见 READINGS 上方注释）。

    **红了就没有读数**：一条失败的 pytest 那一步照样打 `1 failed, 957 passed in 158.34s`，
    同一个正则会乐呵呵把那半截跑完的 957 读走 —— 实测 10-01：一次 chroma 偶发失败把
    `backend_tests` 从 1326 洗成 957，而 README 那个数才是对的（于是这条守卫差一点反过来
    把人对的那一格判成漂移）。所以红跑**不写值**、也不打"没量到"的记号（那一步确实跑了、
    也确实有汇总行，问题不在输出格式上）——**但要留下"这一步红过"这个事实**（`R102-36`
    半条，10-03 收）：从前的红跑静默退场，旧读数被钉在原地而**没有任何一格说明最近一趟
    是红的**。记号是 `<key>_red_at`；只有等这个键再量到新值（绿跑）才清掉它 —— 红被绿
    取代才算翻篇，`check_readme_headline_numbers` 见到记号就上屏提醒"这一格还是上一次
    绿跑量到的"。

    调用点是**一步一份**（每步跑完立刻并一次，不是整趟结束再一起写）：排在建步之后的
    `consistency` 因此能看见同一趟刚量到的数，加完用例不用跑两趟门禁才发现 README 对不上。

    **合并而不是整体覆盖** —— 第一版是覆盖，当场就撞出后果：门禁会并发跑（我这边一次
    `--fast`，同时另一次 `--ci` 还在跑），后写那趟没量覆盖率那个键，于是把先写那趟的读数
    **整份抹掉**。"只写我量到的"这件事，只有落成合并才成立。

    每个键带自己的测量时刻（`<key>_at`）：覆盖率来自 full/ci 那趟、后端测试数本趟就有，
    两件事不该共用一个时间戳 —— 比对的那条断言靠它说清"比的是哪一趟"。
    """
    try:
        loaded = json.loads(READINGS.read_text(encoding="utf-8")) if READINGS.exists() else {}
        data: dict[str, object] = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        data = {}  # 坏了的产物当没有：下一次整份重写，不跟它争
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    added: list[str] = []
    if not ok:
        # 红跑：不写值、不碰 head，只给这一步的键落"红过"记号（合并写盘，见 docstring）。
        for step, (_pattern, key) in _READING_PATTERNS.items():
            if step in outputs:
                data[f"{key}_red_at"] = stamp
                added.append(f"{key}（红）")
    else:
        data["head"] = _git("rev-parse", "HEAD").strip()[:12]
        for step, (pattern, key) in _READING_PATTERNS.items():
            if step not in outputs:
                continue  # 这一趟没跑那一步（档位不含它，或前一步红了就停）：不判、也不抹旧读数
            hits = re.findall(pattern, _ANSI.sub("", outputs[step]))
            if not hits:
                # **跑过却没量到**是另一件事，而且是有信息量的那一件：这一步的輸出格式变了
                # （或它的命令行参数把汇总行吞了）。落下记号让比对那条断言去红，而不是安静地
                # 少一个键 —— 第一版就是少一个 `backend_tests` 键而全绿，README 那个数从此没人看。
                data[f"{key}_unreadable"] = stamp
                continue
            data[key] = hits[-1]
            data[f"{key}_at"] = stamp
            data.pop(f"{key}_unreadable", None)
            data.pop(f"{key}_red_at", None)  # 绿跑量到新值：红被取代，记号清掉
            added.append(key)
            if key == "coverage_percent":
                # 覆盖率是**按平台**的数（runner 的 Linux 与本机 win32 各执行不了对方那半条分支），
                # 所以这个键必须带着"是哪台机器量的"，否则下一个 91.77 会被读成"覆盖率掉了"。
                data["coverage_platform"] = sys.platform
    try:
        READINGS.parent.mkdir(parents=True, exist_ok=True)
        READINGS.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if added:
            what = "读数已并入" if ok else "红跑留痕已记下"
            print(f"  {what} {os.path.relpath(READINGS, ROOT)}：{', '.join(added)}", flush=True)
    except OSError as exc:  # 写不了读数不该让门禁失败
        print(f"  （读数没落盘：{exc}）", flush=True)


# CI 档跳过的步骤：要么要 node/浏览器/真机环境（前端与壳各有专属 job、冒烟要本机 Chrome），
# "dist 入库同步"在 CI 上由 frontend job 跑**同一个脚本**（那个 job 才装 node、才真的重建）；
# 覆盖率档自 2026-10-04 起也跳过（审查快照的 CI 门禁条目 + 用户拍板）：此前 --ci 把 fast 与
# coverage **两趟** pytest 全跑（同一套 1490 条用例每次 push 重复一遍），正是 CI 门禁臂两次
# 顶穿 20 分钟的直接构成。现在 CI 只留单趟（无覆盖率），覆盖率档移到本机全量档与夜间臂。
# 名单而不是标志位：加一步新检查时默认进 CI，除非在这里点名跳过 —— 漏跑的代价比多跑大。
CI_SKIP = frozenset(
    {
        "shell typecheck",
        "前端 vitest",
        "前端 tsc+build",
        "dist 入库同步",
        "真机冒烟(14 项)",
        "pytest(覆盖率≥85%)",
    }
)


def main() -> int:
    parser = argparse.ArgumentParser(description="分层门禁（带每步计时）")
    parser.add_argument("--fast", action="store_true", help="只跑快速层（约 1 分钟）")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI 档：全量层但跳过需要 node/浏览器/真机/覆盖率档的步骤（单趟 pytest，无覆盖率）",
    )
    parser.add_argument(
        "--only",
        metavar="步骤名子串",
        help="只跑名字含这个子串的步骤：补读一个数时不必等整趟（读数是按键合并的）",
    )
    args = parser.parse_args()

    # 读数的键按**步骤名**挂钩，而步骤名会改（改名/合并/删步）：写错的键从此永不命中，
    # 却长得像"这一趟没跑那一步"（见 _write_readings）—— 于是一条读数静静消失、门禁照绿。
    # 这一步先挡住，比事后从 README 的数不对倒查回来便宜得多。
    unknown_patterns = sorted(set(_READING_PATTERNS) - {name for name, _, _ in STEPS})
    if unknown_patterns:
        print(f"❌ _READING_PATTERNS 引用了不存在的步骤：{unknown_patterns}", flush=True)
        return 2

    # 同一个形状的第二格（10-02 轮 `R102-38`）：`--only` 的子串打错时循环一步都不进，
    # 而末尾那句"✅ 全部通过"只看 `failures` —— 于是**零步也报绿**。补读一个数的人
    # 拿到的是"绿"，实际什么都没跑，比红贵得多。
    #
    # 同族第三格（10-03 终局序列当场撞出）：子串**命中**了名字，但那一步被档位过滤
    # （`pytest(-x, 无覆盖率)` 是 fast 档，默认 full 档不带它）—— 循环照样一步不进、
    # 照样报绿。实测：`gate.py --only "pytest(-x"` 0.0s 打出"✅ 全部通过"、退出 0。
    # 修法不是再补一个条件，而是让守卫与循环共用同一条"这趟会不会跑"的判据：
    # 零步（无论哪种零法）一律可乐回 2，并说清怎么把它真的跑起来。
    def _will_run(name: str, mode: str) -> bool:
        if args.only and args.only not in name:
            return False
        if args.ci and name in CI_SKIP:
            return False  # CI 档不跑 node/真机那几步（它们各有专属 job 或要本机环境）
        if args.fast:
            return mode in ("fast", "both")
        if args.ci:
            return True  # CI 档到这儿只剩"档位"一层：不按快/全量筛
        return mode in ("full", "both")

    if args.only and not any(_will_run(name, mode) for name, _, mode in STEPS):
        matched = [name for name, _, _ in STEPS if args.only in name]
        print(f"❌ --only「{args.only}」这趟一步都不会跑（零步不许当通过）", flush=True)
        if matched:
            print(f"   命中了：{'、'.join(matched)} —— 但当前档位不会跑它", flush=True)
            print("   fast 档步骤加 --fast；全量档步骤不加旗（默认）", flush=True)
            print("   CI 跳过的步骤别加 --ci", flush=True)
        else:
            print("   现有步骤：" + "、".join(name for name, _, _ in STEPS), flush=True)
        return 2

    timings: list[tuple[str, float]] = []
    failures: list[str] = []
    started = time.perf_counter()

    # 先算出这一趟真正会跑的步骤（档位过滤只有这一处判据，循环与 --only 守卫共用它）。
    runnable = [(name, cmd, mode) for name, cmd, mode in STEPS if _will_run(name, mode)]

    # 头部的静态四步（ruff + 三档 mypy）互不依赖、输出互不读，**并发跑**
    # （2026-10-04 审查快照的 CI 门禁条目②）：三遍 mypy 在 runner 上是 60-120s 的串行
    # 冷启动，并发 + 缓存后归到一路。失败语义不变：整组跑完后**按序**处理结果，
    # 任何一步红 → 不再启动后面的步骤（后面的步骤在同一个问题上只会重复失败）。
    # 静态组各自捕获输出、跑完再打（并发流式打印会互相穿插，读不了）。
    _STATIC_HEAD = ("ruff", "mypy", "mypy scripts/", "mypy(linux 档)")
    head = [e for e in runnable if e[0] in _STATIC_HEAD]
    parallel_ran = False
    rest = runnable
    if len(head) >= 2:
        parallel_ran = True
        rest = [e for e in runnable if e[0] not in _STATIC_HEAD]
        with ThreadPoolExecutor(max_workers=len(head)) as pool:
            outcomes = list(pool.map(lambda e: _run_captured(e[0], e[1], _cwd_for(e[0])), head))
        for (name, _cmd, _mode), (ok, dt, output) in zip(head, outcomes, strict=True):
            timings.append((name, dt))
            _write_readings({name: output}, ok)
            if not ok:
                failures.append(name)
                break  # 静态组内失败：不进后续步骤（组内其余步骤已跑完，照常报读数）
        if not failures:
            print(f"  ⏱ 静态组（{'、'.join(n for n, _, _ in head)}）并发完成", flush=True)

    for name, cmd, _mode in rest:
        if parallel_ran and name in _STATIC_HEAD:
            continue  # 静态组已在上面并发跑过（--only 只点名静态步时不会走到这）
        # 覆盖率那趟：本地全量档"没碰 src/ 就跳过"。CI 档已在 CI_SKIP 里整步跳过
        # （2026-10-04 起覆盖率移到本机全量档与夜间臂；从前这里写着"CI 必跑"—— 用户 09-29
        # 的拍板 R28-24，被审查快照的 CI 门禁条目修订：双趟 pytest 是 CI 预算顶穿的主因）。
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
        ok, dt, output = _run(name, cmd, cwd)
        timings.append((name, dt))
        # **一步一份，跑完立刻落**（不是整趟结束后一次性写）：否则同一趟里排在后面的
        # `consistency` 比的是**上一趟**的读数 —— 加了六条用例要跑两趟门禁才看得见。
        _write_readings({name: output}, ok)
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
    if args.only:
        tier += f"，只跑「{args.only}」"
    print(f"  {'总计':24} {total:6.1f}s（模式：{tier}）")
    if failures:
        print(f"  ❌ 失败步骤：{'、'.join(failures)}")
        return 1
    print("  ✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
