r"""文档命令块可执行化（P14，docs-as-tests 扩面）—— 沿 README 探针的形状扩同族。

为什么需要：README 的"快速开始"已有探针真起一次服务（`probe_readme_quickstart.py`），
其余文档的命令块仍靠人肉保鲜。文档三族烂法（事实漂 / 命令失效 / 结构烂掉）里，
"命令会失效"是机器能接住的那一半：脚本改名、清单搬家、工具换名 —— 照着文档敲必挂，
而没有任何尺子看着。本探针抽**登记过的** fenced 命令块（首批：CONTRIBUTING.md §2
工作流 + §8.2/8.3/8.4 环境命令），逐行走两档判据：

  形状臂（全部行，纯离线秒级）：行里引用的脚本路径 / requirements 清单必须真实存在；
  §2 那种散文步骤则钉"引用的工具与脚本名没漂"（ruff 可解析、check_consistency.py 在）。
  执行臂（只跑登记为 run 的行）：两处 venv 创建（`.venv` / `.venv-ocr`）的**落点换到
  临时目录、行首裸解释器换成本平台 venv 那份**（实测 cmd.exe 里裸 python 可能是 Store
  占位 exit 9009 —— 探针机器不该为此红，读者装好 python 的机器不受影响）；
  `scripts\init_db.py` 原样执行、数据根全走环境变量隔离（探针绝不碰仓库根的真实库）。

  **不在执行臂的每一行都登记理由**（出网 / 会改真实环境 / 已有专属门禁步）——
  静默跳过 = 生成物悄悄少一类，那正是本仓最忌讳的形状。取舍明细见 `_decide`。

跑法（仓库根）：PYTHONIOENCODING=utf-8 .venv\\Scripts\\python.exe scripts/probe_docs_commands.py
门禁步：`文档命令可跑性`（full 档；锚点用例 tests/unit/test_probe_docs_commands.py）。
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from venv_python import venv_python as _venv_python

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]


@dataclasses.dataclass(frozen=True)
class Block:
    """一个登记的命令块：按标题前缀定位（行号会漂，标题不会——标题就是锚）。"""

    doc: str
    heading: str  # 前缀匹配
    fence_lang: str  # "" = 无语言标签的素 fence（§2 的散文步骤）
    label: str


REGISTRY: tuple[Block, ...] = (
    Block("CONTRIBUTING.md", "## 2. 工作流：测试先行", "", "§2 工作流（散文步骤）"),
    Block("CONTRIBUTING.md", "### 8.2 ", "powershell", "§8.2 主环境"),
    Block("CONTRIBUTING.md", "### 8.3 ", "powershell", "§8.3 OCR 独立环境"),
    Block("CONTRIBUTING.md", "### 8.4 ", "powershell", "§8.4 国内网络"),
)

#: 文档行里引用的仓库内路径（脚本 / requirements 清单）。形状臂逐个验证存在性。
_PATH_RE = re.compile(r"(?:scripts|requirements)[/\\][\w./\\-]+\.(?:py|txt|lock)")


def _extract_block(doc: Path, entry: Block) -> list[str]:
    """取登记标题之后的**第一个** fenced 块（含注释行 —— 取舍判据要看注释里的理由）。"""
    text = doc.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(entry.heading):
            start = i
            break
    if start is None:
        raise AssertionError(f"{entry.doc} 里找不到 {entry.heading!r}（文档结构漂了）")
    fence_open = None
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("```") and fence_open is None:
            fence_open = j
            lang = stripped[3:].strip()
            if entry.fence_lang and lang != entry.fence_lang:
                raise AssertionError(
                    f"{entry.heading} 的块语言是 {lang!r}，登记写的是 {entry.fence_lang!r}"
                )
            continue
        if fence_open is not None and stripped.startswith("```"):
            return [ln for ln in lines[fence_open + 1 : j] if ln.strip()]
    raise AssertionError(f"{entry.heading} 下面找不到闭合的代码块")


@dataclasses.dataclass(frozen=True)
class Decision:
    mode: str  # "run" / "shape" / "comment"
    reason: str = ""


def _decide(entry: Block, line: str) -> Decision:
    """单行判据档位。每一条 shape 都要有**写出来的理由**（锚点用例钉着这一格）。"""
    stripped = line.strip()
    if stripped.startswith("#"):
        return Decision("comment")
    if entry.heading.startswith("## 2."):
        return Decision(
            "shape",
            "工作流清单是散文步骤不是脚本；引用的两个命令（ruff / check_consistency.py）"
            "各有专属门禁步每趟都跑 —— 这里只钉「名字没漂」",
        )
    if "-m venv" in line:
        return Decision("run")  # 离线、落点换临时目录、幂等 —— 执行臂的成本就这一格
    if "-m pytest" in line:
        return Decision(
            "shape",
            "全套用例已有专属门禁步（pytest 两档）在跑同一条命令 —— 这里真跑等于"
            "全量门禁最贵的一步跑两遍",
        )
    if "pip install" in line or "pip config" in line:
        return Decision(
            "shape",
            "出网（PyPI）且会改真实环境（-U pip 动仓库根 .venv、config set 动全局配置）"
            "—— 探针不许；引用的清单文件存在性在形状臂里钉",
        )
    if "check_consistency" in line:
        return Decision(
            "shape",
            "同名命令已有专属门禁步（consistency）每趟都跑 —— 这里只钉路径没漂",
        )
    # 剩下的（init_db / OCR 块的其余行）：
    if "init_db" in line:
        return Decision("run")  # 离线、数据根走环境变量隔离
    return Decision(
        "shape",
        "OCR 环境按设计不在运行树里，真装是 150MB 级出网下载 —— 只钉引用路径存在",
    )


@dataclasses.dataclass(frozen=True)
class RunPlan:
    cmd: str
    env: dict[str, str]


def _plan_run(line: str, workdir: Path) -> RunPlan:
    """执行臂的形状规范化：**命令内容与参数一个字不改**，只换落点与解释器（README 探针
    同一纪律）。venv 行的目标（.venv / .venv-ocr）换到临时目录、裸 `python` 换成本平台
    venv 那份 —— 实测 cmd.exe 里裸 python 可能是 Store 占位（exit 9009），文档读者装好
    python 的机器没问题，探针机器不该因此红；init_db 的解释器同理、数据根走环境变量
    —— 两条都绝不碰仓库根的真实状态。"""
    env = {"PYTHONIOENCODING": "utf-8", **os.environ}
    if "-m venv" in line:
        # 先换目标（此刻行里只有文档写的那个目标带 .venv），再换行首的裸解释器。
        cmd = re.sub(r"(\S*\.venv[\w-]*)", lambda m: str(workdir / Path(m.group(1)).name), line)
        cmd = re.sub(r"^\s*python\b", lambda _m: _venv_python(), cmd, count=1)
        return RunPlan(cmd, env)
    # init_db：解释器换成本平台 venv 那份（文档写的是 Windows 形状，Linux 换分隔符），
    # 数据根全部指进临时目录（与 README 探针同一组环境变量）。
    cmd = re.sub(
        r"""^\s*(?:"[^"]*\.venv[^"]*"|\S*\.venv\S*)""",
        lambda _m: _venv_python(),
        line,
        count=1,
    )
    if os.sep != "\\":
        cmd = cmd.replace("\\", os.sep)
    env.update(
        {
            "SQLITE_PATH": str(workdir / "app.db"),
            "CHROMA_PATH": str(workdir / "chroma"),
            "UPLOAD_DIR": str(workdir / "uploads"),
            "MEMORY_EXTRACT_AUTO": "0",
            "MODEL_PIN_ON_STARTUP": "0",
        }
    )
    return RunPlan(cmd, env)


def _shape_problems(entry: Block, lines: list[str]) -> list[str]:
    """形状臂：引用的仓库内路径必须存在；§2 钉"工具与脚本名没漂"。"""
    problems: list[str] = []
    for line in lines:
        if _decide(entry, line).mode == "comment":
            continue
        for match in _PATH_RE.finditer(line):
            ref = match.group(0).replace("\\", "/").rstrip(".,；;")
            if not (ROOT / ref).exists():
                problems.append(
                    f"{entry.label}：引用的 {ref} 不存在（命令已失效）行={line.strip()!r}"
                )
    if entry.heading.startswith("## 2."):
        body = "\n".join(lines)
        if "ruff" not in body:
            problems.append("§2：工作流里 ruff 那一步不见了（工具名漂了）")
        if "check_consistency.py" not in body:
            problems.append("§2：check_consistency.py 那一步不见了（脚本路径漂了）")
        import importlib.util

        if importlib.util.find_spec("ruff") is None:
            problems.append("§2：ruff 在本环境不可解析（文档教的第一道命令跑不了）")
    return problems


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="docs_cmds_probe_"))
    failures: list[str] = []
    try:
        for entry in REGISTRY:
            doc = ROOT / entry.doc
            lines = _extract_block(doc, entry)
            failures.extend(_shape_problems(entry, lines))
            for line in lines:
                if _decide(entry, line).mode != "run":
                    continue
                plan = _plan_run(line, workdir)
                proc = subprocess.run(  # noqa: S603
                    plan.cmd,
                    shell=True,
                    cwd=str(ROOT),
                    env=plan.env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                if proc.returncode != 0:
                    failures.append(
                        f"{entry.label}：执行失败（exit={proc.returncode}）行={line.strip()!r}\n"
                        f"  stderr 尾部：{(proc.stderr or '')[-400:]}"
                    )
            if not failures:
                print(f"✅ {entry.label}（{entry.doc} {entry.heading.strip()}）")
        if failures:
            print("❌ 文档命令块失效：")
            for f in failures:
                print(f"  {f}")
            print("   修法：改文档去对齐现实；命令真的该变，就一并更新登记的理由。")
            return 1
        print(f"✅ 登记的 {len(REGISTRY)} 个命令块全部可解析、引用路径都在、执行臂三行真跑通过")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
