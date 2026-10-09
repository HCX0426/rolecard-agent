"""分层门禁：平时迭代用 --fast，提交/发布前用全量。

为什么存在（用户 2026-09-18：「每次都要跑全量吗？有计时吗？」）：
  * 全量门禁（含真机冒烟）一次 **6~8 分钟**，每次小改都跑是纯浪费 —— 大部分失败
    ruff/mypy/单测 30 秒内就能暴露；
  * 没有**每步计时**，慢了也不知道慢在哪、该优化谁。本脚本每步打印耗时并汇总。

用法：
  python scripts/gate.py --fast   # ruff + mypy + 单测(-x, 无覆盖率) + 一致性 + 前端 test ≈ 2 分钟
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
import threading
import time
import traceback
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
    # 三档 mypy 各用各的缓存目录（`--cache-dir`）。并发共写一份 `.mypy_cache` 是 2026-10-08
    # CI 红的那一发的形状：静态组三发**同时冷启动**（CI 缓存 miss）共写一个目录，
    # `mypy scripts/` 在 1.0s 抛 INTERNAL ERROR（编译器崩溃，不是类型错），另两档同一棵树却
    # 39.6s 干净通过。本机 Windows 上 4 轮并发压不出来（本地 venv 是 mypy 2.3.1、锁里 pin 的
    # 是 2.4.0 —— 本地绿根本没过 CI 用的那台编译器），但"多个写者共写一份缓存"这件事本身
    # 就不成立，不必先定罪再拆。ci.yml 的缓存 `path: .mypy_cache` 整目录收，子目录照缓存。
    ("mypy", [PY, "-m", "mypy", "--cache-dir", ".mypy_cache/src"], "both"),
    # scripts/ 是 2957 行**取证尺子**，从前只过 ruff 不过 mypy（09-26 轮 R26-21）。
    # 补上第一天就抓到两个运行时已经坏了的脚本（seed_demo_data / run_eval 调
    # `make_embedder` 少两个必填关键字参数 ⇒ 一跑就 TypeError），见那一轮台账 S-6 行。
    ("mypy scripts/", [PY, "-m", "mypy", "scripts/", "--cache-dir", ".mypy_cache/scripts"], "both"),
    # Linux 档的类型检查。容器跑的就是 Linux（Dockerfile `python:3.13-slim`），而本机
    # 门禁只查 win32 档 —— CI 第一发就红在这上面（`ctypes.WinDLL` 在 Linux 档没有、
    # POSIX 分支的 `type: ignore` 在 Linux 档成了 unused）。“本机绿”而“容器里红”属于
    # 同一个假绿家族，两边一起查才关得掉。
    (
        "mypy(linux 档)",
        [PY, "-m", "mypy", "--platform", "linux", "--cache-dir", ".mypy_cache/linux"],
        "both",
    ),
    # 依赖方向契约（import-linter，判据与豁免纪律在 pyproject 的 [tool.importlinter]）。
    # 同进程跑而不是直接调 CLI：契约名是中文的，子进程按 GBK 写管道、这一步按 UTF-8 解
    # 就会成一串问号，而 src 布局还要先把 src 放进 sys.path（两件事都在包装脚本里做掉）。
    ("依赖方向", [PY, "scripts/check_import_layers.py"], "both"),
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
        # 步骤名**不含阈值数字**（2026-10-07 抬阈值时摘的）：它是身份（读数键、CI_SKIP
        # 成员），政策归 pyproject 的 fail_under —— 数字住在名字里，每抬一次都要同步改
        # 三处身份，漏一处就断（读数键查主、CI 跳过表照旧按名匹配）。
        "pytest(覆盖率)",
        [PY, "scripts/pytest_with_evidence.py", "--lane", "coverage"],
        "full",
    ),
    (
        # 分模块地板紧跟覆盖率那一步（数据是它刚写的 `.coverage`）。名字与覆盖率步同前缀，
        # 与它同进退：CI 不跑覆盖率（CI_SKIP 含覆盖率步），地板也整步跳过 —— 没有数据的
        # 判据在 CI 上只会是噪音。配置缺失/文件跌破都由 scripts/coverage_floor.py 自己判红。
        "覆盖率分模块地板",
        [PY, "scripts/coverage_floor.py"],
        "full",
    ),
    (
        # 改动行覆盖率（diff-cover）排在地板之后：两步共用覆盖率那步刚写的 `.coverage`。
        # 基线 origin/main，与覆盖率步同进 CI_SKIP —— CI 上没有覆盖率数据，这步只是噪音。
        "改动行覆盖率",
        [PY, "scripts/diff_coverage.py"],
        "full",
    ),
    # 快档也跑前端计数（2026-10-04 审查快照"读数漂移窗"那条的落地）：readings 的
    # `frontend_tests` 从前只在全量档刷新 —— 加了前端用例而几天不跑全量，读数（scratch 槽）
    # 就静静停在旧数（实测现场：读数 355 停在 10-03，真实 359，差了两天，是撞上别的事才查
    # 出来的）。翻成 both 之后**每趟快档都把它对到现值**：前端用例与读数都随每趟走。
    # CI 档不受影响（`CI_SKIP` 已点名它 + 前端 tsc+build，CI 的前端各归专属 job）；
    # 本机代价约 +10s（vitest 热跑实测 9.7s）—— 这一格买的就是漂移当场红。
    ("前端 vitest", [NPM, "test"], "both"),
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
    # **依赖漏洞扫描**（ENGI-15 ①，2026-10-07 锁文件落地后接线）：`audit_deps.py` 出网问
    # OSV，所以它**不进快档**（本仓"测试全离线"的铁律），mode=full 落在本地全量档与
    # `--ci`（--ci 是 full 减 CI_SKIP，而它**刻意不在 CI_SKIP**）—— 每次 push 的 CI 红
    # 线从这条起成立。判据在 dependency-audit-allowlist.json：新出现的告警 = 红；
    # 在册豁免带"上游已给修复版本"前提，前提消失会**反过来红**（豁免不是永久通行证）。
    # 退出码 fail-closed：扫不成（断网/接口变更）退 2，门禁同样红 —— 扫不成不等于干净。
    ("依赖审计", [PY, "scripts/tools/audit_deps.py"], "full"),
    # **密钥扫描**（审查快照（2026-10-04）#ENGI-15 ②，可选档但本仓已接）：`scan_secrets.py`
    # 包装 gitleaks，扫的是**由 git 名单圈出的影子树**（tracked + 未忽略的未跟踪 = 本次要发的
    # 那份源码；2026-10-09 订正 —— 从前这里写的是"只扫当前工作树（`--no-git`，离线可跑）"，
    # 而 `--no-git` 其实是**盘上全扫、根本不看 .gitignore**，注释与代码互相矛盾了半年）。
    # 不碰网络历史。**不进快档**（与 ① 同一条「测试全离线」
    # 铁律），mode=full 落在本地全量档与 `--ci`（--ci 是 full 减 CI_SKIP，而它**刻意不在
    # CI_SKIP**）—— 每次 push 的 CI 红线从这条起成立。退出码 fail-closed：gitleaks 装不到 /
    # 定不出名单 / 扫不成（断网）都退 2，门禁同样红 —— 扫不成不等于干净。放行判据集中在
    # `.gitleaks.toml`（本仓经 git grep 全量核查，tracked 文件里没有任何真密钥，allowlist 只为
    # 挡默认规则对良性内容：.env.example 空值、文档示例 endpoint、CI 假 token、data URL、
    # 测试 fixture 的误报）。
    ("密钥扫描", [PY, "scripts/tools/scan_secrets.py"], "full"),
    ("真机冒烟(14 项)", [PY, "scripts/smoke_check.py"], "full"),
    # 「README 数字收尾」这一步已随拍板"数字移出散文"整个删除：README 不再抄数 ⇒ 没有
    # 数要"收尾"，反向断言（首屏不许有数 + 引用必须在）本就排在 `consistency` 里、不依赖
    # 覆盖率/vitest 之后才写进读数 —— R28-73 那条"晚一趟"的病灶连根没了。
]


def _git(*args: str) -> str:
    """git 查询：失败就抛（调用方按"不确定 = 保守跑"处理，绝不静默当成"没改动"）。

    `encoding="utf-8"` 不是可选的：本仓有中文文件名，git 吐出来的是 UTF-8，而 Windows 上
    `text=True` 默认按 GBK 解 —— 解码在**读子进程输出的那个线程**里抛 UnicodeDecodeError，
    主线程只会拿到一份被截断的 stdout（实测 09-25 就在 `diff --name-only` 上中招）。
    截断不是"少几行"，`_src_changed()` 会拿着半份清单判断要不要跑覆盖率，那是一次静默失效。

    `-c core.quotepath=false` 是**同一条病的另一半**（2026-10-09 由 CI 现场照出，
    run 37852226595）：这个配置在 Linux 上默认 **true**（Git for Windows 默认 false），true 时
    git 把非 ASCII 路径输出成**带双引号的八进制转义** —— `"data/lore/01-\345\237\272…"`。
    于是 `p.startswith("src/")` 对中文路径**永远判假**：改动清单里那些文件既不算"改了 src"、
    也不算"改了别的"，而是直接从判据眼里消失。`_src_changed()` 中招就是覆盖率被静默跳过
    （= 变弱，正是本函数上面那段注释不许发生的事）。本机复现不出来，只在 CI 上发作 ——
    关掉它对 ASCII 路径毫无影响，所以这是一处零成本的收口。
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        # 60s 上限（2026-10-09）：`_git` 是全部门禁里**唯一没有超时**的 subprocess，而它跑在
        # 每一步之前（改动清单、`_src_changed`、dist 同步、parity…）。git 在坏 lockfile/超大
        # 未跟踪树/凭据助手弹窗上就是能吊住不动的 —— 那正好是"每步预算都界得住，整趟却照样
        # 被杀"的形状（run 37823819338）。超时抛 TimeoutExpired，调用方各有兜底：`_changed_paths`
        # 的兜底是 None→保守全量，`_src_changed` 的兜底是 True→照跑覆盖率 —— 全是**变慢不变弱**，
        # 而"吊死整条 job"是变没证据。
        timeout=60,
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
    —— fail-safe 不变，绝不因探测失误而悄悄削弱覆盖率安全网（具体那条线是多少见
    pyproject 的 ``fail_under``，本函数不读它）。

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


#: 受影响用例选择（2026-10-04 快照的 CI 压缩方案第 ⑤ 步）。
#:
#: 为什么只在改了 src/ 时挑，且**挑不出来就退回全量**：这一步买的是一分钟量级的本地反馈，
#: 代价是"少跑的那些文件里可能有一条本来会红"。所以判据全部朝保守一侧倒 ——
#: 任何一处不确定（git 查不动、改了横切面、模块找不到同名测试、挑出来的比例太高）
#: 都退回全量。这条纪律与 `_src_changed()` 是同一条：**探测失误只许让门禁更慢，不许让它更弱**。
_SRC_PREFIX = "src/rolecard_agent/"
#: 地基包：上下各层都 import 它们，"改了它只跑同名的测试"是错的（`base/paths` 一次改动
#: 就牵动装配、打包、OCR 三条线）。
_FOUNDATION_PREFIXES = ("src/rolecard_agent/base/", "src/rolecard_agent/storage/")
#: 固定常跑集：与改动无关、但守的正是"全仓性质"的那几支，且便宜（<1s）。
_ALWAYS_RUN_TESTS = ("tests/unit/test_import_floor.py",)
#: 挑出来超过这个比例就不挑了：省不下多少，还白白换来一次"我到底跑全了没有"的疑问。
_MAX_AFFECTED_SHARE = 0.6
#: **机器自己写的东西不算"你改了什么"**。`docs/gate-readings.json` 是门禁每跑完一步就写的
#: 读数文件（入库槽）：它必然出现在改动清单里，而它按规矩属于"src/ 与 tests/ 之外"⇒ 一律退回全量。
#: 实测第一趟演示就是这么退回全量的（工作树里只有它 + 一处 src 改动），也就是说
#: 没有这一格，这条特性**永远不会生效** —— 单元用例量不到，只有真跑一趟才看得见。
#: scratch 槽（`build/gate-readings-scratch.json`）在 gitignore 的 build/ 下，根本进不了
#: `git status`/`ls-files` 的清单，不用列。
_MACHINE_ARTIFACTS = frozenset({"docs/gate-readings.json"})


def _changed_paths() -> list[str]:
    """工作树里的改动（未提交含已暂存 + 未跟踪），与 `_src_changed` 同一口径。

    `_git` 失败会抛 —— 调用方按"不确定 = 跑全量"处理，不在这里吞掉。
    """
    changed = _git("diff", "--name-only", "HEAD").splitlines()
    changed += _git("ls-files", "--others", "--exclude-standard").splitlines()
    return [p.strip().replace("\\", "/") for p in changed if p.strip()]


def _test_files() -> list[str]:
    return sorted(
        str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.glob("tests/**/test_*.py")
    )


def select_affected(changed: list[str], tests: list[str]) -> tuple[list[str], str]:
    """把改动的文件映射成"该跑哪些测试文件"；**空列表 = 退回全量**（理由写在第二项里）。

    判据按保守程度排（任一命中即全量）：
      1. 改动里有 `src/` 与 `tests/` 之外的东西（pyproject、conftest、CI 工作流…）——
         那些文件能影响整套用例的收集与运行方式；
      2. 改的是 `src/rolecard_agent/` 下的**包顶层模块**（`config.py` 这类）—— 谁都可能 import；
      3. 改的是地基包 `base/` 或 `storage/`—— 同上，只是理由更具体；
      4. 某个改动模块**找不到同名测试**（零命中）—— 这正是"宁可慢不可漏"那一句；
      5. 挑出来的文件超过全集六成 —— 省不下多少。

    这条路的产出**只有本地 `--fast` 用**：CI 与全量档永远跑全套，读数也从不采信子集
    （见 `_write_readings` 里 `AFFECTED-SUBSET` 那一支）。
    """
    # 先摘掉机器自己写的文件（读数是门禁每步在写的，不是"你在改的代码"）。
    changed = [p for p in changed if p not in _MACHINE_ARTIFACTS]
    src = sorted(p for p in changed if p.startswith(_SRC_PREFIX) and p.endswith(".py"))
    if not src:
        return [], "工作树里没有 src/ 的改动（受影响选择只在改了 src/ 时生效）"
    outside = sorted(
        p for p in changed if not p.startswith(_SRC_PREFIX) and not p.startswith("tests/")
    )
    if outside:
        return [], f"改动里有 src/ 与 tests/ 之外的文件（{outside[0]}）—— 横切面，全量"
    # `src/rolecard_agent/<mod>.py` 只有两段斜杠；再深一层才是子包里的模块。
    shallow = [p for p in src if p.count("/") == 2]
    if shallow:
        return [], f"改的是 {shallow[0]}（包顶层模块，谁都可能 import）—— 全量"
    foundation = [p for p in src if p.startswith(_FOUNDATION_PREFIXES)]
    if foundation:
        return [], f"改的是 {foundation[0]}（地基包，上下各层都 import）—— 全量"
    picked: set[str] = set(_ALWAYS_RUN_TESTS)
    for path in src:
        stem = Path(path).stem
        hits = [t for t in tests if stem in Path(t).name]
        if not hits:
            return [], f"{path} 找不到同名测试（映射零命中）—— 宁可慢不可漏，全量"
        picked.update(hits)
    # 改到的测试文件本身一定要跑（按词干匹配可能匹配不到它自己，例如改了
    # `test_ocr_bundled_worker.py` 而没改任何 `ocr*` 模块）。
    picked.update(p for p in changed if p.startswith("tests/") and p.endswith(".py"))
    chosen = sorted(picked)
    if len(chosen) > len(tests) * _MAX_AFFECTED_SHARE:
        return [], f"映射出 {len(chosen)}/{len(tests)} 个文件，省不下多少 —— 全量"
    return chosen, f"改了 {len(src)} 个模块 → 跑 {len(chosen)}/{len(tests)} 个测试文件"


def _affected_pytest_command(
    base: list[str], changed: list[str] | None
) -> tuple[list[str], str]:
    """快档那趟 pytest 的命令：能挑就挑（并在输出里留下 `AFFECTED-SUBSET` 记号）。

    `changed` 由 `main()` 在**开跑之前**取一次（`None` = 没取到）。不在这里现取，是因为
    门禁自己每跑完一步就往读数文件里写 —— 边跑边取会把机器刚写的文件读成"你改的东西"。
    """
    if changed is None:
        return base, "全量（git 查询失败：不确定就保守跑）"
    picked, why = select_affected(changed, _test_files())
    if not picked:
        return base, f"全量（{why}）"
    return [*base, "--affected-subset", *picked], f"受影响子集：{why}"


def _cwd_for(name: str) -> Path | None:
    """步骤跑在哪个目录按名字前缀定（比在元组里再加一个字段少一处噪声）。第一版
    我把 shell 那步写成"全局 npm run typecheck"，于是它在仓库根跑、根本没有这个
    script —— 报错的样子像"壳的类型检查挂了"，其实是步目录错了。"""
    if name.startswith("前端"):
        return ROOT / "frontend"
    if name.startswith("shell"):
        return ROOT / "shell"
    return None


#: 每一步的墙钟上限（秒）。2026-10-09 装的，根因是那两趟 **"什么都没报错"的 cancel**：
#: 一个步骤吊住不动 → runner 把整个 job 杀在预算上 → GitHub 对 cancelled job **不上传日志**
#: （run 37804463056 与 37815831250 连续两趟，现场两次都拿不回来，只能从相邻臂倒推）。
#: 挂死必须变成**这一步的红**（带 ⏱ 与点名信息、整趟照常打出汇总、日志照常上传），而不是
#: 整条 job 陪葬。数值按本机实测放宽：本机全绿时最长的一步是 pytest ~86s，这里最少的档也
#: 给了 4 倍余量 —— 超时红是"吊死了"的强信号，不是"机器慢"的常见信号；CI 冷启动的抖动
#: 由余量吃，真到上限的那一步就是坏了。`--deadline-minutes`（CI 传 22）再兜一层：
#: 就算每步都在自己的上限里慢慢走，总量也不能越过 runner 预算（见 _budgeted）。
DEFAULT_STEP_TIMEOUT = 900.0
STEP_TIMEOUTS: dict[str, float] = {
    "ruff": 180.0,
    "mypy": 420.0,
    "mypy scripts/": 420.0,
    "mypy(linux 档)": 420.0,
    "依赖方向": 180.0,
    "shell typecheck": 420.0,
    "pytest(-x, 无覆盖率)": 900.0,
    "consistency": 300.0,
    "baseline --check": 300.0,
    "pytest(覆盖率)": 1500.0,
    "覆盖率分模块地板": 120.0,
    "改动行覆盖率": 120.0,
    "前端 vitest": 420.0,
    "前端 tsc+build": 600.0,
    "dist 入库同步": 120.0,
    "README 可跑性": 420.0,  # 脚本内自己只等 90s 健康 + 收尾，这里给它起停的余量
    "随包后端 parity": 300.0,
    "依赖审计": 700.0,  # 脚本内 pip-audit 硬超时 600s，这里留善后余量
    "密钥扫描": 420.0,  # 脚本内 gitleaks 硬超时 300s + 首跑下载
    "真机冒烟(14 项)": 900.0,
}


def _step_timeout(name: str) -> float:
    # 快档的 pytest 那一步到 `_run` 手里时带着"｜受影响子集…"的显示后缀（`_resolve` 加的），
    # 直接查表会掉进 DEFAULT —— 查不到不报错，只是每步上限**静默失效**。先摘掉后缀再查。
    return STEP_TIMEOUTS.get(name.split("｜", 1)[0], DEFAULT_STEP_TIMEOUT)


def _timeout_note(budget: float) -> str:
    """挂死当红时补的那句 —— 它存在的意义就是**别再丢现场**：红一步带日志上传，
    而不是把整条 job 吊到预算外被杀（cancelled job 不传日志，连续两趟现场是这么丢的）。"""
    return (
        f"\n（这一步在 {budget:.0f}s 上限里没跑完 —— 按挂死当红。截止此刻的输出原样留在上方；"
        "宁可红一步，不要让整条 job 被杀后什么都不剩。）\n"
    )


def _budgeted(step_timeout: float, remaining: float | None) -> float:
    """单步真正可用的墙钟：步自身上限与**全局剩余**取小（CI 传 `--deadline-minutes`）。
    `remaining=None`（本机默认）→ 只受步上限管。下限 1s：剩余哪怕只够一步开个头，
    也要跑出一次**带日志的红**，而不是无声跳过。
    """
    if remaining is None:
        return step_timeout
    return max(min(step_timeout, remaining), 1.0)


def _remaining_seconds(started: float, deadline: float | None) -> float | None:
    """全局墙钟还剩多少秒；`deadline=None` → None（不约束）。

    住在函数里而不是散在循环里：静态并发组**进池前算一次**、串行步**开跑前各算一次**，
    两处共用这同一条"从趟开始起算"的口径，别让第二处自己再拿 `time.monotonic()` 起算。
    """
    if deadline is None:
        return None
    return max(deadline - (time.perf_counter() - started), 0.0)


#: 挂死时看门狗要能点名"此刻在跑哪一步"。runner 在 job 预算上杀进程时 GitHub 不传日志，
#: 而**步级的 timeout 管不到步骤之外的无界调用**（`_git()` 没有超时、读数落盘、导入替身
#: …）。run 37823819338 就是这么证明的：deadline 21 分钟、每步预算全界得住，job 照样被
#: 杀在 25 分钟上、现场照样丢 —— 挂死根本不在步的等待里。所以最后这道防线必须是一把
#: **独立的刀**：看门狗线程到点自己打出汇总并 `os._exit`，主线程哪怕焊死也拦不住它留现场。
_ACTIVE: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()
#: 主循环开步前写这一步的名字；看门狗读它来点名。字符串赋值在 CPython 下原子，够用。
_CURRENT: list[str] = ["（还没开步）"]


#: 看门狗结束进程的那把刀。单列成变量只为测试能换掉它 —— 默认 `os._exit` 是真必须的
#: （触发场景 = 主线程卡在无界调用上，`SystemExit` 走不到解释器），而它连正在跑本脚本的
#: pytest 一起带走，用例不换掉就没法断言"它打了什么"。
_WATCHDOG_EXIT = os._exit

#: 打现场的时间上限（秒）。到点**照样退**，见 `_arm_watchdog` 里那一段为什么这是整件事的要点。
_SCENE_BUDGET_SECONDS = 10.0


def _emit(line: str = "") -> None:
    """看门狗唯一的出口写法（单独成函数只为测试能把它换成一个会堵的替身）。"""
    print(line, flush=True)


def _arm_watchdog(started: float, deadline: float, timings: list[tuple[str, float]]) -> None:
    """到点把现场打在 stdout 上然后**自己**结束进程（退 3）。

    为什么非 `os._exit` 不可：触发场景就是主线程卡在某个无界调用上，`SystemExit` 要等主
    线程回到解释器 —— 那等于没有。

    为什么现场要放进**另一个线程里限时等**（10-09 第四趟换来的，这条是本机制的要点）：
    run 37830147312 带着 21 分钟的 deadline 照样被杀在 25 分钟，说明看门狗到点**没能退出**。
    它当时做的每件事都可能把自己卡在那儿：① `print(flush=True)` 往 runner 的日志管道写 ——
    管道没人消化就是**阻塞在 write 里**，而"打现场"这件事自己堵住，恰恰是三趟里唯一还没
    排除的形状；② 收孩子前那句惰性 import 也可能排在主线程握着的 import 锁后面。
    所以现在的形状是：**打现场尽力而为、最多等十秒，退出这条路上一个可阻塞的调用都不留**。
    宁可丢掉栈那一行，也要换来 job 自己结束 —— `failure` 会上传日志（前面已经打出的每一步
    输出、以及"此刻在跑哪一步"全都在），而等下去是 `cancelled` + 什么都没剩。
    那句惰性 import **留着**：它在一条"可以被放弃"的线程里，卡住也只是丢掉收孩子这一步，
    不再拖住退出；而上膛就取实测要 454ms、还会把 15 个 app 模块拖进门禁进程，不值。
    """
    if deadline <= 0:
        return

    def _scene() -> None:
        _emit(
            f"\n⛔ 被全局墙钟掐停（{deadline / 60:.1f} 分钟，起算于开跑第一行）。"
            f"此刻在跑：{_CURRENT[0]}"
        )
        main_id = threading.main_thread().ident
        frame = sys._current_frames().get(main_id) if main_id is not None else None
        if frame is not None:
            _emit("   主线程卡在这一行（这就是挂点，别再从相邻臂倒推）：")
            for line in traceback.format_stack(frame):
                _emit("   " + line.rstrip().replace("\n", "\n   "))
        _emit("=" * 52)
        _emit("门禁计时汇总（被掐停的这趟）")
        _emit("=" * 52)
        for name, dt in timings:
            _emit(f"  {name:24} {dt:6.1f}s")
        _emit("  ❌ 失败步骤：全局墙钟（被掐停）")

    def _kill_children() -> None:
        with _ACTIVE_LOCK:
            victims = list(_ACTIVE)
        for popen in victims:
            with contextlib.suppress(Exception):
                # 走 `_terminate_step`：它里面那句惰性 import 现在**允许**存在 —— 这条线程
                # 整个可以被放弃（join 超时就走），卡住只丢"收孩子"这一步，不再拖住退出。
                _terminate_step(popen)

    def _report_then_kill() -> None:
        """现场 + 收孩子都在**这一条**线程里 —— 它卡住也无所谓，见 `_watch`。

        顺序刻意：先打再收，收孩子会往管道回话，把证据搅浑。
        """
        _scene()
        _kill_children()

    def _watch() -> None:
        while True:
            left = deadline - (time.perf_counter() - started)
            if left <= 0:
                break
            time.sleep(min(left, 1.0))
        reporter = threading.Thread(target=_report_then_kill, name="gate-scene", daemon=True)
        reporter.start()
        reporter.join(_SCENE_BUDGET_SECONDS)
        # 下面这三行**不许有任何可能阻塞的东西**（不 print、不 import、不等锁）：这一行的
        # 唯一职责是"无论如何把进程结束掉"。前面那发 join 有超时，所以它顶多晚到十秒。
        # 现场全丢也认 —— `failure` 至少把**已经打出来的每一步输出**留下来，而等下去是
        # `cancelled` + 什么都没剩（run 37830147312 实测：带看门狗仍被杀在 25 分钟）。
        _WATCHDOG_EXIT(3)

    threading.Thread(target=_watch, name="gate-watchdog", daemon=True).start()


def _terminate_step(proc: subprocess.Popen[str]) -> None:
    """把挂死的一步**整棵树**收掉。只 `terminate()` 外壳不够 —— 本仓实测过两次"杀了外壳、
    真后端原地活着答了二十分钟"（`probe_readme_quickstart` 与 `run_api` 各一发）。实现只有
    一份（`core/tools/run.py`，POSIX 走 killpg、Windows 走 taskkill /T），这里与那两处同
    一个 src 布局写法：不假设装过。

    看门狗也用这一个函数（10-09 定过一次"上膛时就取实现"，实测代价 454ms + 把 15 个 app
    模块拖进门禁进程，撤了）—— 撤得掉的理由是形状变了：现在收孩子跑在一条**可以被放弃**
    的线程里（`_arm_watchdog` 的 join 超时），这句 import 排在主线程的 import 锁后面也只
    丢"收孩子"这一步，不再拖住退出。留孤儿的后果（下一次 push 自己会撞上）比看门狗不退出
    的后果（现场又丢一次）轻，排序就是这么定的。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.core.tools.run import terminate_process_tree  # noqa: PLC0415

    terminate_process_tree(proc)


def _run_captured(
    name: str,
    cmd: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
    remaining: float | None = None,
) -> tuple[bool, float, str]:
    """并发组专用的运行器：**捕获**输出、跑完一次打出（流式打印并发会互相穿插）。

    与 `_run` 同一个返回契约（ok, 秒数, 完整输出），只是 io 模式不同。超时语义同 `_run`：
    到点红给这一步、带上截止此刻的输出，不 traceback、不吊整条 job。静态组的 `remaining`
    在**进池前算一次**（五并发共用同一个剩余预算 —— 它们本来就同时跑，不该各拿一份全额）。
    """
    print(f"\n▶ {name}（并发）", flush=True)
    t0 = time.perf_counter()
    budget = _budgeted(timeout if timeout is not None else _step_timeout(name), remaining)
    try:
        # `Popen + communicate(timeout)` 而不是 `subprocess.run(timeout=...)`：要的那个
        # 孩子句柄只有前者给得到 —— 看门狗到点要能把它收掉（`communicate` 自己会并发读
        # 两路管道，不存在主线程干等的死锁问题，这正是 `subprocess.run` 内部的做法）。
        popen = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            # **必须独立成组**：到点 `_terminate_step` 在 POSIX 上走 killpg，孩子若还住在
            # 继承来的组里，那一刀连 bash + `timeout` + gate.py 自己一起杀（2026-10-09
            # run 37842997481 的现场：起服务后 2 秒整步 SIGKILL，被杀名单里连外层 `timeout`
            # 都在）。Windows 上这条是 no-op（走 taskkill 按树），所以本机永远照不出来。
            start_new_session=os.name != "nt",
        )
        with _ACTIVE_LOCK:
            _ACTIVE.add(popen)
        stdout: str | None = None
        stderr: str | None = None
        try:
            stdout, stderr = popen.communicate(timeout=budget)
        except subprocess.TimeoutExpired:
            # communicate 超时后孩子还活着；先收树再二次 communicate 把管道里的余货清掉。
            _terminate_step(popen)
            with contextlib.suppress(Exception):
                stdout, stderr = popen.communicate(timeout=15)
            dt = time.perf_counter() - t0
            partial = (stdout or "") if isinstance(stdout, str) else ""
            output = partial + _timeout_note(budget)
            print(output, end="", flush=True)
            print(f"  ⏱ {name}: {dt:.1f}s ❌（步超时上限 {budget:.0f}s）", flush=True)
            return False, dt, output
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(popen)
    except OSError as exc:  # 命令本身起不来（找不到解释器之类）—— 也算这一步的红
        dt = time.perf_counter() - t0
        print(f"  ⏱ {name}: {dt:.1f}s ❌（起不来：{exc}）", flush=True)
        return False, dt, f"起不来：{exc}\n"
    dt = time.perf_counter() - t0
    output = (stdout or "") + (stderr or "")
    code = popen.returncode
    print(output, end="", flush=True)
    print(f"  ⏱ {name}: {dt:.1f}s {'✅' if code == 0 else '❌'}", flush=True)
    return code == 0, dt, output


def _run(
    name: str,
    cmd: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
    remaining: float | None = None,
) -> tuple[bool, float, str]:
    """跑一步，返回 (是否通过, 秒数, 该步的完整输出)。

    输出**边跑边打在控制台上，同时留一份在内存里**（第三个返回值）—— 留这一份只为了
    一件事：把 README 首屏那几个数变成"这一步刚才量出来的"，而不是"某人上次手抄的"。
    流式打印不能丢（长步骤静默几分钟会被当成挂死），所以自己按行读而不是 capture_output。

    `remaining` 是**全局剩余**（`--deadline-minutes` 设的墙钟；None = 不约束）。步的预算取
    `_budgeted(步上限, remaining)`：就算每一步都在自己的上限里慢慢走，总量也不许越过 runner
    预算（那两次"什么都没报错的 cancel"就是这么烧掉的 —— 预算死在 job 上时 GitHub 不传日志，
    现场没了；预算死在步上时这是一次带日志的红）。到点**整棵树**收掉（只 terminate 外壳是
    本仓实测过两次的孤儿后端形状），已打出的输出原样保留。
    """
    print(f"\n▶ {name}", flush=True)
    t0 = time.perf_counter()
    budget = _budgeted(timeout if timeout is not None else _step_timeout(name), remaining)
    collected: list[str] = []
    timed_out = False
    _CURRENT[0] = name.split("｜", 1)[0]  # 看门狗点名用（快档那步带显示后缀，摘掉）
    with subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        # 同上：这条也是会走 `_terminate_step`（killpg）的孩子，必须自己成组。
        start_new_session=os.name != "nt",
    ) as proc:
        with _ACTIVE_LOCK:
            _ACTIVE.add(proc)  # 挂死时看门狗要能收掉**活着的孩子**，不是只退自己
        # 读流必须与等待**并发**。主线程 `wait(timeout)` 而没人读管道 = 经典死锁：子进程
        # 写满几 KB 的管道缓冲后阻塞在 write 上，wait 会**把超时当挂死等到点** —— pytest 那
        # 一步的输出远超管道容量，这把尺子会把正常慢读成挂死。读泵放线程里，主线程只持 deadline。
        def _pump() -> None:
            for line in proc.stdout or ():
                print(line, end="", flush=True)
                collected.append(line)

        reader = threading.Thread(target=_pump, daemon=True)
        reader.start()
        code: int | None
        try:
            code = proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_step(proc)  # 整棵树：外壳收了、真后端原地活着是本仓实测过两次的孤儿形状
            code = -1
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(proc)
        # 到点后管道的 EOF 由杀进程树保证；join 给足 10s，读不完的部分随 daemon 线程收。
        reader.join(timeout=10.0)
    dt = time.perf_counter() - t0
    if timed_out:
        note = _timeout_note(budget)
        print(note, end="", flush=True)
        collected.append(note)
        print(f"  ⏱ {name}: {dt:.1f}s ❌（步超时上限 {budget:.0f}s）", flush=True)
        return False, dt, "".join(collected)
    print(f"  ⏱ {name}: {dt:.1f}s {'✅' if code == 0 else '❌'}", flush=True)
    return code == 0, dt, "".join(collected)


def _for_display(path: Path) -> str:
    """读数文件在屏幕上怎么称呼：能相对仓库根就用相对路径，**跨盘符就用绝对路径**。

    为什么不是直接 `os.path.relpath`：Windows 上两个盘之间没有相对路径可言，`relpath` 抛
    `ValueError`（不是 `OSError`）—— 而这一句住在"写不了读数不该让门禁失败"那个 `try` 里，
    兜的偏偏只有 `OSError`。CI 的 Windows 臂就是这么红的：runner 的检出在 `D:`、临时目录在
    `C:`，于是一句**纯打印**把整趟用例带崩（2026-10-08 实测，本机照不出：仓库与 temp 同盘）。
    这条路径本来就是"给人看一眼写在哪"的，展示不了相对形状就退回绝对形状 —— 它没有任何
    一句是在判据上，不值得为它把门禁弄红。
    """
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return str(path)


def _readings_paths() -> tuple[Path, Path]:
    """读数**两槽**的落点：入库槽（慢数）在前，scratch 槽（快数）在后。

    分槽的判据只有一条 —— **这格数多久变一次**（哪些键归哪槽见 `_write_readings` 的
    `slot_of`）。这是拍板"数字移出散文"的另一半：README 改成引用不抄数之后，若读数本身
    还整份入库，快档每跑一趟（head、用例数、各自的 `_at` 必刷新）工作树照样每轮弄脏、
    纯数字提交照旧 —— 实测一个会话 13+ 笔"读数收尾"，其中不少是把一个还对的数改成另一个
    还对的数。所以快家当搬去 gitignore 的 scratch，覆盖率家族（只有全量档量它、src 没动时
    整步跳过）留下入库 —— 它才是"入库不吵"的。

    入库槽**入库**的理由（09-30 第六条规矩）：尺子的判据里不许有 gitignore 的东西 —— 判据
    读到暂存区，问的就不再是"这格数是不是真的"，而是"这台机器上有没有这个文件"；放 `docs/`
    而不是 `build/` 只为了干净克隆里它必须在（README 链接它，`coverage-threshold` 尺子读它）。

    但 **runner 上不许写入库那份**（10-01 那发 CI 红换来的，`R28-74`）：覆盖率是**按平台**
    的数 —— 本机 win32 与 GitHub 的 Linux runner 各量各的（win32/posix 那两条分支各自执行
    不了对方的行），让第二个机器覆盖入库那份，等于两份都对的数互相把对方判成漂移。所以
    `GATE_READINGS_SCRATCH=1`（ci.yml 里设）时入库槽落点换成 gitignore 的 `build/`：runner
    照样量、照样在自己那趟里自我比对，只是不碰共同记录。scratch 槽永远在 `build/` ——
    它存在的意义就是不入库。
    """
    if os.environ.get("GATE_READINGS_SCRATCH"):
        committed = ROOT / "build" / "gate-readings.json"
    else:
        committed = ROOT / "docs" / "gate-readings.json"
    return committed, ROOT / "build" / "gate-readings-scratch.json"


#: 入库槽（覆盖率家族；README 链接它，`coverage-threshold` 那条尺子读它）与
#: scratch 槽（后端/前端用例数、head 与各自的记号 —— 本机的"上一趟量到了什么"）。
READINGS, READINGS_SCRATCH = _readings_paths()
_READING_PATTERNS = {
    # (读哪个步骤, 正则, 存成什么名)
    "pytest(-x, 无覆盖率)": (r"(\d+) passed", "backend_tests"),
    "pytest(覆盖率)": (r"Total coverage:\s*([\d.]+)%", "coverage_percent"),
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


def _load_slot(path: Path) -> dict[str, object]:
    """读一个槽的现值；坏了当没有 —— 下一次整份重写，不跟它争。"""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_readings(outputs: dict[str, str], ok: bool) -> None:
    """把这一步量到的数**分槽**并进两份读数文件（落点见 `_readings_paths`）。

    分槽是拍板"数字移出散文"的另一半：README 那头改成引用不抄数之后，这一头的键按
    "多久变一次"分两家 —— 覆盖率家族住**入库槽**（README 链接它、`coverage-threshold`
    尺子读它），后端/前端用例数与 head 住 **scratch 槽**。住在入库文件里的快数，每刷新
    一次就是一笔纯数字提交；搬走之后，入库槽只在全量档真的重量了覆盖率时才动，工作树
    平时是干净的。scratch 槽没有机器读者（拿读数比 README 的旧尺子已随对读时期退役），
    它就是本机的"上一趟量到了什么"。

    **红了就没有读数**：一条失败的 pytest 那一步照样打 `1 failed, 957 passed in 158.34s`，
    同一个正则会乐呵呵把那半截跑完的 957 读走 —— 实测 10-01：一次 chroma 偶发失败把
    `backend_tests` 从 1326 洗成 957。所以红跑**不写值**、也不打"没量到"的记号（那一步
    确实跑了、也确实有汇总行，问题不在输出格式上）——**但要留下"这一步红过"这个事实**
    （`R102-36` 半条，10-03 收）：从前的红跑静默退场，旧读数被钉在原地而**没有任何一格
    说明最近一趟是红的**。记号是 `<key>_red_at`；只有等这个键再量到新值（绿跑）才清掉
    它 —— 红被绿取代才算翻篇。

    调用点是**一步一份**（每步跑完立刻并一次，不是整趟结束再一起写）：一趟中途断掉，
    前几步量到的数已经落盘，不用整趟白跑陪葬。"consistency 与读数的先后"那层关系随
    对读尺子退役一并消失 —— 它不再读这里写的任何键；`coverage-threshold` 读的覆盖率
    排在它**后面**才量，那格的当趟拦阻由 pytest 自己的 `fail_under` 负责（读数 ≥ 阈值
    那条尺子退为下一趟的复核，晚一趟的缝隙有人兜底）。

    第三类是**绿了但量不到**（10-04 撞出来的）：chroma 偶发命中在册签名时二跑取证、按未知
    放行（`pytest_with_evidence.py` 的 FLAKY-RECORDED），但首跑带 `-x` 已截断、二跑只跑
    子集 —— 全量那条汇总行这趟根本没打出来。读数机如果照常取匹配，会把子集的数（实测 32）
    写进去。这一档的处理是"不写这个键"：旧值连旧 `_at` 一起留着（诚实性在时间戳上），
    下一趟干净绿跑刷新；head 照常更新 —— 这一步确实绿了。受影响子集（AFFECTED-SUBSET）
    完全同一处理 —— 同族现场已经出过一次，加"受影响选择"不许顺手制造第二个。

    **合并而不是整体覆盖** —— 第一版是覆盖，当场就撞出后果：门禁会并发跑（我这边一次
    `--fast`，同时另一次 `--ci` 还在跑），后写那趟没量覆盖率那个键，于是把先写那趟的读数
    **整份抹掉**。"只写我量到的"这件事，只有落成合并才成立。

    每个键带自己的测量时刻（`<key>_at`）：覆盖率来自 full/ci 那趟、后端测试数本趟就有，
    两件事不该共用一个时间戳。
    """
    committed = _load_slot(READINGS)
    scratch = _load_slot(READINGS_SCRATCH)
    slots: dict[Path, dict[str, object]] = {READINGS: committed, READINGS_SCRATCH: scratch}
    added: dict[Path, list[str]] = {READINGS: [], READINGS_SCRATCH: []}

    def slot_of(key: str) -> Path:
        # 一格数住哪个槽，判据只有"多久变一次"（见 `_readings_paths`）：快档每跑一趟都要
        # 刷新的（用例数与 head，连带各自的 `_at`/记号）落 scratch，其余（覆盖率家族）入库。
        if key == "head" or key.startswith(("backend_tests", "frontend_tests")):
            return READINGS_SCRATCH
        return READINGS

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    if not ok:
        # 红跑：不写值、不碰 head，只给这一步的键落"红过"记号（合并写盘，见 docstring）。
        for step, (_pattern, key) in _READING_PATTERNS.items():
            if step in outputs:
                path = slot_of(f"{key}_red_at")
                slots[path][f"{key}_red_at"] = stamp
                added[path].append(f"{key}（红）")
    else:
        slots[READINGS_SCRATCH]["head"] = _git("rev-parse", "HEAD").strip()[:12]
        for step, (pattern, key) in _READING_PATTERNS.items():
            if step not in outputs:
                continue  # 这一趟没跑那一步（档位不含它，或前一步红了就停）：不判、也不抹旧读数
            path = slot_of(key)
            data = slots[path]
            stripped = _ANSI.sub("", outputs[step])
            if "FLAKY-RECORDED" in stripped:
                # **取证放行的那趟量不到这个键**：首跑带 `-x`、偶发即截断（`1 failed,
                # 1139 passed` 不是全量），二跑只重跑命中在册签名的那几个文件（`32 passed`
                # 是子集）—— 两行都读不得（10-04 实测把 backend_tests 洗成 32）。全量没
                # 跑完 = 这一趟没量到：旧值与旧 `_at` 原样留着，也不打"没量到"的记号 ——
                # 这一步绿了，只是这个键这趟没数。`head` 照常更新（见上）。
                added[path].append(f"{key}（取证放行未量，沿用上一趟）")
                continue
            if "AFFECTED-SUBSET" in stripped:
                # 受影响子集跑：这一趟只跑了一部分文件，`N passed` 当然不是全套的数。
                # 与取证放行**同一处理**（不写值、不碰 `_at`，下一趟干净的绿跑自然刷新）。
                added[path].append(f"{key}（受影响子集未量，沿用上一趟）")
                continue
            hits = re.findall(pattern, stripped)
            if not hits:
                # **跑过却没量到**是另一件事，而且是有信息量的那一件：这一步的輸出格式变了
                # （或它的命令行参数把汇总行吞了）。落下记号 —— 少一个键必须比多一个键响，
                # 第一版就是少一个 `backend_tests` 键而全绿，那个数从此没人看。
                data[f"{key}_unreadable"] = stamp
                continue
            data[key] = hits[-1]
            data[f"{key}_at"] = stamp
            data.pop(f"{key}_unreadable", None)
            data.pop(f"{key}_red_at", None)  # 绿跑量到新值：红被取代，记号清掉
            added[path].append(key)
            if key == "coverage_percent":
                # 覆盖率是**按平台**的数（runner 的 Linux 与本机 win32 各执行不了对方那半条分支），
                # 所以这个键必须带着"是哪台机器量的"，否则另一台机器的数会被读成"覆盖率掉了"。
                data["coverage_platform"] = sys.platform
                # README 的覆盖率徽章（shields endpoint 格式）在同一现场生成 —— 与 gate-readings
                # 共命运：只有真量到才刷新，红的/子集的/取证放行的趟都不碰它。它**只在本机全量档**
                # 落入库槽（scratch 槽是 runner 的，公开 badge 的数来自入库那台）。
                if path is READINGS:
                    badge = {
                        "schemaVersion": 1,
                        "label": "coverage",
                        "message": f"{data[key]}%",
                        "color": "brightgreen" if float(data[key]) >= 90.0 else "yellow",
                    }
                    (ROOT / "docs" / "coverage-badge.json").write_text(
                        json.dumps(badge, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
    for path, data, notes in (
        (READINGS, committed, added[READINGS]),
        (READINGS_SCRATCH, scratch, added[READINGS_SCRATCH]),
    ):
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            unchanged = path.exists() and path.read_text(encoding="utf-8") == payload
            if not unchanged and (data or path.exists()):
                # 内容没变就不重写；**空槽且盘上没有**也不写 —— 这一趟一步快数都没量到时
                # （比如 --only ruff 的绿跑，head 都没轮到），不该凭空落一份 "{}" 出来。
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8", newline="\n")
            if notes:
                what = "读数已并入" if ok else "红跑留痕已记下"
                print(f"  {what} {_for_display(path)}：{', '.join(notes)}", flush=True)
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
        "pytest(覆盖率)",
        "覆盖率分模块地板",
        "改动行覆盖率",
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
    parser.add_argument(
        "--full-tests",
        action="store_true",
        help="快档也跑全套用例（关掉受影响选择；提交前那趟建议带上）",
    )
    parser.add_argument(
        "--deadline-minutes",
        type=float,
        default=None,
        help="整趟的墙钟预算：到点前门禁自己收摊并报红（CI 传 runner timeout 减几分钟）。"
        "存在的理由：runner 杀 job 时 GitHub **不上传日志** —— 连续两趟挂死的现场就是这么丢的；"
        "死在自己的 deadline 上则是一次**带完整汇总的红**。不设 = 只受每步上限管（本机默认）。",
    )
    args = parser.parse_args()

    # 墙钟从 `main()` 的第一行起算，**排在第一个 git 调用之前**：从前 `started` 在这里往下
    # 四十行（守卫、读数键检查之后），于是 `_changed_paths()` 那第一批 `_git()` —— 全门禁里
    # 唯一没有超时的 subprocess —— 整个跑在预算外。run 37823819338 的"每步预算都界得住、
    # 趟却照样被杀在 25 分钟"就是这么留出门缝的。现在看门狗先上膛，后面任何无界卡死都会
    # 换来一次**带主线程栈的红**。
    timings: list[tuple[str, float]] = []
    failures: list[str] = []
    started = time.perf_counter()
    # 全局墙钟的**总秒数**（`--deadline-minutes` 没设 = None = 不约束，本机默认只受每步上限
    # 管）。它存在只为一件事：runner 杀 job 时 GitHub **不上传日志**（连续两趟挂死的现场就
    # 是这么丢的），而这趟自己到点收摊会打出**完整汇总 + 失败步骤点名** —— 现场留得下来。
    # 之后所有步共用 `_remaining_seconds(started, deadline)` 这一条口径，别让第二处自己起表。
    deadline = args.deadline_minutes * 60.0 if args.deadline_minutes else None
    if deadline is not None:
        _arm_watchdog(started, deadline, timings)

    # 受影响选择的改动清单：**开跑前**取一次。门禁自己每跑完一步就往读数槽写读数（入库
    # 那份 `docs/gate-readings.json`；scratch 槽在 gitignore 的 build/ 下、进不了清单），
    # 边跑边取会把机器刚写的文件当成"你改的东西"（实测第一趟演示就是这么退回全量的）。
    # 取不到（不是 git 工作树、git 挂了）记 None —— 下游按"不确定 = 全量"处理。
    try:
        _changed_at_start: list[str] | None = _changed_paths()
    except Exception:
        _changed_at_start = None

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

    # （`timings`/`failures`/`started`/`deadline` 只在 `main()` 顶上那一份里定义与起表，
    #  这里不再重开一份：重开会把 `started` 推到守卫之后，看门狗与各步剩余各读各的表。）
    # 先算出这一趟真正会跑的步骤（档位过滤只有这一处判据，循环与 --only 守卫共用它）。
    runnable = [(name, cmd, mode) for name, cmd, mode in STEPS if _will_run(name, mode)]

    # 头部的静态五步（ruff + 三档 mypy + 依赖方向）互不依赖、输出互不读，**并发跑**
    # （2026-10-04 审查快照的 CI 门禁条目②）：三遍 mypy 在 runner 上是 60-120s 的串行
    # 冷启动，并发 + 缓存后归到一路。静态组各自捕获输出、跑完再打（并发流式打印会互相穿插）。
    #
    # **试过、被数据否决的一步**（2026-10-07 实测，当天改完当天退回）：把 pytest（独立线程、
    # 流式）与前端计数也拉进这趟并发 —— 想法是"让它们藏在 mypy 那 29s 底下"。实测**反而更慢**：
    # 整趟 142s → 254s，pytest 88s → 244s、vitest 9s → 61s、随包 parity 1.3s → 16.6s
    # （本机 16GB 且常态 70% 占用：三档 mypy + pytest -n 4 + node 同时跑是明显超额认购，
    # 抢 CPU 的代价远大于重叠省下的那点时间）。所以这一格保持"静态组并发、其余串行"，
    # 不为纸面上的重叠去抢 CPU。要再动它，先在**满载**的机器上量一遍再说话。
    _STATIC_HEAD = ("ruff", "mypy", "mypy scripts/", "mypy(linux 档)", "依赖方向")

    def _cov_lane_skipped(name: str) -> bool:
        """覆盖率那条：本地全量档"没碰 src/ 就跳过"（CI 档已在 `CI_SKIP` 里整步跳过 ——
        2026-10-04 起覆盖率移到本机全量档与夜间臂：从前这里写着"CI 必跑"，是被审查快照的
        CI 门禁条目修订掉的（双趟 pytest 是 CI 预算顶穿的主因，`R28-24`）。"""
        return (
            (not args.fast)
            and (not args.ci)
            and name.startswith("pytest(覆盖率")
            and not _src_changed()
        )

    def _resolve(name: str, cmd: list[str]) -> tuple[str, list[str]]:
        """一步最终的（显示名，命令行）—— 受影响用例选择只在这一处发生。"""
        if args.fast and not args.full_tests and name.startswith("pytest(-x"):
            run_cmd, note = _affected_pytest_command(cmd, _changed_at_start)
            return f"{name}｜{note}", run_cmd
        return name, cmd

    head = [e for e in runnable if e[0] in _STATIC_HEAD]
    # 并发组只有**凑得齐两个**才成立；凑不齐时静态步不许从名单里消失。
    # 这一格是 2026-10-09 拿 `--only "mypy(linux"` 实测撞出来的第四种"零步当绿"：
    # 从前写的是 `rest = [非静态]`，于是 `head` 只剩一步时并发不启动、那一步又不在 `rest` 里
    # —— **一步被选中、零步实际跑过、末尾照样"✅ 全部通过"**。上面那道 `--only` 守卫拦不住
    # 它：守卫问的是"这趟会不会跑"，而这里的账是"分组的名单漏了人"，两回事。
    # 所以名单只有一份（`runnable`），并发跑过的那几步在串行循环里按 `parallel_ran` 跳过。
    parallel_ran = len(head) >= 2
    if parallel_ran:
        # 失败语义不变：整组跑完后**按序**处理结果，任何一步红 → 不再启动后面的步骤
        # （后面的步骤在同一个问题上只会重复失败）。
        # 并发五步共用**同一份**剩余预算（它们同时跑，各拿一份全额等于允许总量翻倍）。
        head_remaining = _remaining_seconds(started, deadline)
        with ThreadPoolExecutor(max_workers=len(head)) as pool:
            outcomes = list(
                pool.map(
                    lambda e: _run_captured(e[0], e[1], _cwd_for(e[0]), remaining=head_remaining),
                    head,
                )
            )
        for (name, _cmd, _mode), (ok, dt, output) in zip(head, outcomes, strict=True):
            timings.append((name, dt))
            _write_readings({name: output}, ok)
            if not ok:
                failures.append(name)
        if not failures:
            print(f"  ⏱ 静态组（{'、'.join(n for n, _, _ in head)}）并发完成", flush=True)

    rest = [e for e in runnable if not (parallel_ran and e[0] in _STATIC_HEAD)]
    # **失败即停**：静态组红了就不再往下走（后面几步在同一个问题上只会重复失败）。
    # 这一格从前漏着 —— 静态组红了 `rest` 照样跑（ruff 报个错还要把整套用例烧完），是"加并发组"
    # 那次留下的缝：`break` 只跳出了**结算循环**，没挡住后面的步骤。文档写着"任何一步失败即停"，
    # 而那句话没有判据看着 —— 这半句就是被判据抓住之前的样子。
    if failures:
        print(
            f"  ⏭️  失败即停：后续 {len(rest)} 步不再跑"
            f"（{'、'.join(n for n, _, _ in rest)}）",
            flush=True,
        )
        rest = []

    for name, cmd, _mode in rest:
        # 覆盖率那条：本地全量档"没碰 src/ 就跳过"（判据在 `_cov_lane_skipped`）。
        if _cov_lane_skipped(name):
            print(f"\n▶ {name}：跳过（src/ 无改动）", flush=True)
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
        display, run_cmd = _resolve(name, cmd)
        ok, dt, output = _run(
            display, run_cmd, cwd, remaining=_remaining_seconds(started, deadline)
        )
        timings.append((name, dt))
        # **一步一份，跑完立刻落**（不是整趟结束后一次性写）：否则同一趟里排在后面的
        # `consistency` 比的是**上一趟**的读数 —— 加了六条用例要跑两趟门禁才看得见。
        _write_readings({name: output}, ok)
        if not ok:
            failures.append(name)
            break  # 失败即停：后面的步骤在同一个问题上只会重复失败
        if name.startswith("pytest(覆盖率"):
            # 记下"覆盖率这次是在哪个 HEAD 上实跑的"：`_src_changed` 的判据靠它，
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
