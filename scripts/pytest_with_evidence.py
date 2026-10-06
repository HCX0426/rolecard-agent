"""红跑取证的包装层（2026-10-02 轮 `R102-41` 的在册修法），**快档与覆盖率档共用**。

现场（开轮批、复审批各撞过一次，10-03 快档又撞一次）：同一份代码、同一趟门禁里，
带覆盖率那一趟报 `chromadb.errors.InternalError: Error executing plan: Internal error:
Error creating hnsw segment reader: Nothing found on disk`，而同一趟前一步
`pytest(-x, 无覆盖率)` 全绿；10-03 反过来在**快档**报同一签名（`test_scope_isolation`，
单条复跑 1/1 绿）。机制至今未定位 —— 要一次带 chroma 侧日志的复跑才能说清
"谁在什么时候把那个目录抽走"。

为什么现在两档都装：原先只有覆盖率档套了这层，快档裸跑 ⇒ 同一发偶发在快档红了就是红了，
留下一句"这一趟门禁红"而没有任何现场。判据一字未改，只是不再只护一台机器。

判据（写死在这里，不留给读日志的人猜）：

  * 只有当**全部**失败都带着在册的 chroma 偶发签名时，才重跑那些文件一次；
  * 重跑**为取证不为转绿**：首跑日志原样留在 `build/<档>-run1.log`，屏幕上大声打出
    "首跑红 / 二跑绿"，退出码按第二跑判；
  * 同一批文件二次红 ⇒ 真红，退出非 0；
  * 失败里混进任何一条不是 chroma 签名的 ⇒ **立刻按原样红，不重跑**（这条不能变成
    万能遮羞布：`R102-38` 那一族的教训是"看起来绿"比红贵得多）。

第二跑把 chroma 那侧的日志打开（`--log-cli-level=DEBUG` 收进 run2 日志）—— 这是这台机器上
唯一能让"segment reader 读不到东西"这件事留下现场的办法。

用法（门禁里）：`python scripts/pytest_with_evidence.py --lane fast|coverage`，不带 `--lane`
时按覆盖率档（与改名前的行为一致）。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Windows 控制台默认 GBK，而这份输出里有中文与 ⚠️/❌ —— 不重配编码的话，判据读到的是
# 一串问号（门禁的 `scripts stdout encoding` 那条就是看着这件事的）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
BUILD = ROOT / "build"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "forensics"))
# 签名清单**只有一份出处**：conftest 的失败时刻钩子与这里问的是同一个东西，两处各抄一份
# 就是给"改了判据漏了另一处"留门（`R102` 轮那条"同一句理由出现在第二处就该有尺子"的同族）。
from chroma_flake_evidence import CHROMA_FLAKE_SIGNATURES, existing_evidence  # noqa: E402

# 警告政策住在 `pyproject.toml` 的 `filterwarnings`（全仓唯一出处：自有代码弃用即红、
# 已知 ResourceWarning 按消息精确放行）。这里从前挂着一句 `-W ignore` 把整条政策连同
# langchain/langgraph 升级唯一的预警信号一起吞掉（2026-10-04 审查快照的吞警告条目）—— 删了，
# 命令行不再有任何吞警告的口子。
COMMON = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
#: 两档的命令行：快档带 `-x`（红就停、不量覆盖率）+ 4 worker 并行；覆盖率档反之（保守
#: 串行，夜间臂再评估）。xdist 的旧结论是"反而更慢"（gate.py 09-19 记录：47s 套件上
#: worker 建库开销吃掉收益）—— 2026-10-04 重测：套件 1514 条、串行 188s，`-n 4` 实测
#: 68~72s（提速 62%，连续两趟全绿），远超采纳门槛 35%，旧结论正式翻案。偶发签名
#: （chroma 首跑 InternalError 一发）与下面的在册取证判据同族，红跑取证照常兜底。
_XDIST = ["-n", "4"]
LANES: dict[str, tuple[list[str], str, str]] = {
    "fast": (COMMON + _XDIST + ["-x"], "gate-fast-run1.log", "gate-fast-run2-retry.log"),
    "coverage": (
        COMMON + ["--cov=rolecard_agent", "--cov-fail-under=85"],
        "gate-coverage-run1.log",
        "gate-coverage-run2-retry.log",
    ),
}

_FAILED_RE = re.compile(r"^FAILED (\S+?)::", re.M)


def _rel(path: pathlib.Path) -> str:
    """日志路径的显示形式。`BUILD` 未必在仓库里（非归属机器落 `build/` 暂存、CI 换工作目录），
    直接 `relative_to(ROOT)` 会让这层取证**自己崩在打印那一行** —— 用例实测抓到过一次。
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _run(cmd: list[str], extra: list[str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd + (extra or []), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    sys.stdout.write(out)
    sys.stdout.flush()
    return proc.returncode, out


def _failing_files(log: str) -> list[str]:
    return sorted({m.split("::")[0] for m in _FAILED_RE.findall(log)})


def _lane_from_argv(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a.startswith("--lane="):
            return a.split("=", 1)[1]
        if a == "--lane" and i + 1 < len(argv):
            return argv[i + 1]
    return None


#: 这一步自己的开关：带它就表示"这是一趟子集跑"（`gate.py --fast` 的受影响选择走这条）。
#: 它**不是** pytest 的参数，必须在本文件里摘掉；它同时决定要不要打那行给读数机看的记号。
_SUBSET_FLAG = "--affected-subset"


def _extra_from_argv(argv: list[str]) -> list[str]:
    """`--lane X` 与 `--affected-subset` 之外的位置参数，原样转给 pytest。

    受影响子集（`gate.py --fast` 那条路）靠它把文件清单交进来。为什么不另起一个入口：
    "红跑取证"的判据与签名清单两档共用一份，多一个入口就多一处会漂的事实面。

    `--affected-subset` 是**这一步自己的**开关（只为了在输出里留个记号），必须在这里摘掉：
    第一版忘了摘，它被当成文件清单的第一项转给 pytest，pytest 当场
    `unrecognized arguments: --affected-subset`（真跑一趟才看见 —— 纯函数用例只测到
    "挑哪些文件"，测不到"命令行最后长什么样"）。
    """
    extra: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--lane":
            skip_next = True
            continue
        if arg.startswith("--lane=") or arg == _SUBSET_FLAG:
            continue
        extra.append(arg)
    return extra


def main() -> int:
    lane = _lane_from_argv(sys.argv[1:]) or "coverage"
    if lane not in LANES:
        print(f"未知的 --lane：{lane!r}（可选：{sorted(LANES)}）", file=sys.stderr)
        return 2
    base, run1_name, run2_name = LANES[lane]
    run1, run2 = BUILD / run1_name, BUILD / run2_name

    extra = _extra_from_argv(sys.argv[1:])
    if extra or _SUBSET_FLAG in sys.argv[1:]:
        # 这个记号是给 `gate.py._write_readings` 读的：子集跑的 "N passed" 不是全套的数，
        # 读成 backend_tests 就是一次静默漂（同族现场 10-04 出过一次，把 1595 洗成 32）。
        print(
            f"[AFFECTED-SUBSET] 这一趟只跑 {len(extra)} 个受影响文件（{lane} 档）—— "
            "全量读数这趟不刷新（子集不是全量）",
            flush=True,
        )

    rc, log = _run(base, extra)
    run1.write_text(log, encoding="utf-8", newline="\n")
    if rc == 0:
        return 0

    failed = _failing_files(log)
    chroma_flake = any(sig in log for sig in CHROMA_FLAKE_SIGNATURES)
    if not failed or not chroma_flake:
        print(
            f"❌ {lane} 档这趟红了，而失败不在在册的 chroma 偶发签名里 —— 按原样红，不重跑。",
            flush=True,
        )
        return rc

    ev = existing_evidence(BUILD)
    ev_line = (
        f"失败时刻的盘上现场 {len(ev)} 份（最新：{_rel(ev[-1])}）"
        if ev
        else "⚠️ 没有任何 r102-41 现场文件 —— conftest 那个钩子没跑到，这比偶发本身更值得查"
    )
    print(
        f"\n⚠️  {lane} 档首跑红，且失败形状命中在册的 chroma 偶发（`R102-41`）。"
        f"重跑那 {len(failed)} 个文件一次取证：{failed}\n"
        f"   首跑完整日志：{_rel(run1)}（不删、不改，二跑不掩盖它）\n"
        f"   {ev_line}",
        flush=True,
    )
    rc2, log2 = _run(
        COMMON + failed,
        ["-o", "log_cli=true", "--log-cli-level=DEBUG"],
    )
    run2.write_text(log2, encoding="utf-8", newline="\n")
    if rc2 == 0:
        print(
            "\n⚠️  FLAKY-RECORDED：同一批文件二跑绿。这一趟按「未知」放行"
            f"（见 `R102-41`：重跑为取证非转绿），两份日志都在："
            f"{_rel(run1)} / {_rel(run2)}。"
            "机制仍未定位 —— 别把这句读成「已修」。",
            flush=True,
        )
        return 0
    print(
        f"\n❌ 二跑仍红（{failed}）—— 这就是真红，不是那发偶发。日志：{_rel(run2)}",
        flush=True,
    )
    return rc2


if __name__ == "__main__":
    raise SystemExit(main())
