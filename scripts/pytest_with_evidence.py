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
# 签名清单**只有一份出处**，而且**匹配逻辑也只有一份**：conftest 的失败时刻钩子与这里问的是
# 同一个东西，两处各抄一份就是给"改了判据漏了另一处"留门（`R102` 轮那条"同一句理由出现在
# 第二处就该有尺子"的同族）。2026-10-09 我又差一点踩进去：既有断言只钉住了**清单对象**相同
# （`CHROMA_FLAKE_SIGNATURES is cfe.CHROMA_FLAKE_SIGNATURES`），我这轮新加的判定顺手写了
# `any(sig in text …)` —— 清单共用而**判法各写一遍**，两侧照样能各判各的还都看着合理。
# 所以这里 import 的是 `hits_signature` 这个函数本身，不再自己算。
# （清单仍留一个引用：`test_chroma_flake_evidence.py` 那条恒等断言是打在本模块属性上的，
#   删掉它会连带把那道"只有一份出处"的尺子废了 —— 那是有意保留的再导出。）
from chroma_flake_evidence import existing_evidence, hits_signature  # noqa: E402

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
        # 只说"量"，不说"量什么、量到多少"：source / branch / fail_under 全在
        # `pyproject.toml` 的 `[tool.coverage.*]`（2026-10-04 审查快照「口径硬编码在脚本」
        # 那一格的迁移）。从前这两个数挂在命令上，于是"覆盖率的范围与阈值"住在跑它的那条
        # 命令里 —— CI、夜间臂、人肉重跑各抄一份，漏抄的那份会安静地量出另一个数。
        COMMON + ["--cov"],
        "gate-coverage-run1.log",
        "gate-coverage-run2-retry.log",
    ),
}

#: 短摘要里"这一条红"的两种行首形状：`FAILED 文件::用例 - 原因`（断言失败）与
#: `ERROR 文件::用例 - 原因`（**fixture/收集期**炸，chroma 这个偶发最常见的形态）。
#: 从前只认 `FAILED` —— 2026-10-09 Windows 臂就是这么漏的：签名在册、`chroma_flake` 也判了
#: 真，摘要是 `ERROR tests/test_api_edges.py::…` ⇒ 旧正则抓不到 ⇒ `failed` 为空 ⇒ 走了
#: 「不在在册签名里，不重跑」那条分支按原样红。**两个半边各对一半，合起来是错结论。**
#:
#: 形状是拿 `build/ci-log/` 下 15 条真摘要行对的（不是照"我以为摘要长什么样"写的）：
#: 两种都是 `kind` + 单个空格 + **以 `.py` 结尾的路径**；`[gwN]` 前缀只出现在 verbose 行里，
#: 短摘要行**一条都没有**（实测计数 0），所以这里不为主观臆想的形状开口子 —— 开了就会把
#: pip 的 `ERROR: Could not find a version…`、docker 的 `ERROR: failed to build…` 也吞进来
#: （这两族同样真实存在于日志里），而那是**别的工具的报错**，不是本层的取证对象。
#: `(?:::|$)` 与 `\.py` 双锚一起挡住它们：既要求"路径后紧跟 `::` 或到行尾"，也要求"确实是个 .py"。
_SUMMARY_LINE_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(\S+\.py)(?:::|\s|$)(.*)$", re.M
)


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


def _failures(log: str) -> list[tuple[str, str]]:
    """短摘要里每一条红：`(文件, 该条自带的原因文本)`。

    两件事在这一个出处里办完，不留"看起来收了其实没收"的空隙：

    1. `FAILED 文件::…` 与 `ERROR 文件::…` **两族都要**。从前只认 `FAILED`，于是
       fixture/收集期炸掉的那些（chroma 这个偶发最常见的形态）一个都摘不出来 ⇒ 明明签名
       在册却走「按原样红，不重跑」（2026-10-09 Windows 臂实测）。
    2. 认不出文件的摘要行**大声打出来**，不静默丢：重跑名单少一个文件 = 那一半失败没被
       取证，而"取证跑过了"看起来照样成立 —— 与本轮修的"扫不到也算扫过"同一族。

    原因文本必须**逐条**取，不能"整份日志里出现过签名"就算 —— 见 `main()` 里那段
    「文档比实现严、实现却更松」的订正。
    """
    out: list[tuple[str, str]] = []
    unparsed: list[str] = []
    for m in _SUMMARY_LINE_RE.finditer(log):
        name = m.group(1).replace("\\", "/")
        out.append((name, m.group(2)))
    # `FAILED|ERROR` 打头却不是 `.py` 路径的行（收集期 `ERROR tests/x.py` 带 `::`、
    # 也可能整个文件炸）：这一族真日志里没有，但一旦 pytest 改了摘要形状，宁可打出来也别
    # 静默少取证。识别口径只有一处（上面的正则），这里只补"看到红行却没抓到文件"的警报。
    for ln in log.splitlines():
        s = ln.strip()
        if s.startswith(("FAILED ", "ERROR ")) and not _SUMMARY_LINE_RE.match(s):
            unparsed.append(s[:120])
    if unparsed:
        print(
            f"⚠️ 摘要里有 {len(unparsed)} 行认不出文件（不重跑它们，也不假装收全）：",
            *[f"   {u}" for u in unparsed[:5]],
            sep="\n",
            flush=True,
        )
    return out


def _is_chroma_flake(reason: str) -> bool:
    """这一条红是不是在册的 chroma 偶发。**空原因 = 不在册**（fail-closed）。

    摘要行不是每条都带原因文本（截断、或某些 pytest 版本不打）。那种情况下我们**不知道**
    它为什么红，而"不知道"不许换来一次重跑放行 —— 与依赖审计、密钥扫描同一条铁律：
    判据取不到证据时按不通过算。

    「是不是那一发偶发」这件事**不在这里答**：`hits_signature()` 的 docstring 明写着"判据只在
    这里答一次"，而我第一版顺手写了 `any(sig in text …)` —— 那是第二份实现（清单确实共用了，
    既有断言 `CHROMA_FLAKE_SIGNATURES is cfe.CHROMA_FLAKE_SIGNATURES` 钉住了对象，**可匹配逻辑
    没钉**）。于是同一句"在册"在 conftest 钩子与这里可以各判各的，而两边看起来都合理 ——
    正是本仓那一族。空原因这一侧仍由本函数负责（它问的是"有没有证据可判"，不是"像不像偶发"）。
    """
    text = reason.strip()
    if not text:
        return False
    return hits_signature(text)


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
    # `build/` 是 gitignore 的 —— **全新检出里没有这个目录**，而取证日志必须落在它里面。
    # 不 mkdir 的代价由 CI 付过（2026-10-09 Windows 臂）：首跑撞上在册 chroma 偶发本来是
    # 走"重跑取证"通道的，结果 `run1.write_text` 先炸在 `FileNotFoundError` 上 —— 取证层
    # 自己崩在写第一份日志那一行，重跑根本没发生，红报成了崩溃。门禁的 `--ci` 照不出它
    # 纯属顺序运气（静态组先跑、读数那一步把 build/ 建出来了），这条臂只跑本脚本，就撞上了。
    BUILD.mkdir(parents=True, exist_ok=True)
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

    failures = _failures(log)
    if not failures or not all(_is_chroma_flake(reason) for _f, reason in failures):
        # 判据是**逐条**问的，问的就是本文件开头「判据」那一节写死的第 1 条：
        # 「只有当**全部**失败都带着在册的 chroma 偶发签名时，才重跑那些文件一次」。
        # 从前这里是 `any(sig in log …)` —— 扫整份日志：只要**任何一处**出现过 chroma 签名，
        # 一起红的真 bug 也就跟着进重跑，而二跑只跑失败的那几个文件 ⇒ 一个"只有全套语境下
        # 才成立"的真红（跨用例污染正是这种）可以二跑绿、被记成 FLAKY-RECORDED 放行。
        # 那正是同节第 4 条自己警告的「万能遮羞布」，而它当时已经写死在判据里了 ——
        # **文档比实现严、实现却更松**，这一族最阴的地方是两边各自看起来都对。
        # （不写行号：这文件一直在长，行号会变成第二个会漂的事实面。）
        unknown = [f for f, r in failures if not _is_chroma_flake(r)]
        print(
            f"❌ {lane} 档这趟红了，而失败不在册的 chroma 偶发签名里 —— 按原样红，不重跑。"
            + (f"\n   不在册的那几条：{unknown[:6]}" if unknown else ""),
            flush=True,
        )
        return rc
    failed = sorted({f for f, _r in failures})

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
