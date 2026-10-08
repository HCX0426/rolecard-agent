"""照 README「快速开始」真的起一次服务 —— 文档可跑性是门禁断言（R28-36 / 决策5）。

为什么单独立这条：README 是 US-6 硬门槛（干净环境 ≤3 条命令跑起来）的唯一载体，而此前
没有任何尺子看着它。落档时实测：第 7 步那条裸 `uvicorn --factory …` 在 src 布局 +
不做 editable 安装的仓库里**必挂** `ModuleNotFoundError: No module named 'rolecard_agent'`，
而且 `.env` 只有 `scripts/run_api.py` 会读 —— 照文档做完 1-6 步再用那条命令，
配置根本不生效。文档写错了比代码写错了更难发现，因为它没人跑。

判据（红=确证的负面，不猜）：
  1. 快速开始代码块里**不许出现裸 `uvicorn` 启动命令**（src 布局下它需要 PYTHONPATH，
     文档读者不会知道）；也不许出现裸 `python scripts/…`（Windows 上裸 `python` 可能是
     Microsoft Store 占位，静默 exit 49 —— 本仓所有脚本入口一律写全解释器路径）。
  2. 把文档里那条启动命令**原样执行一次**（干净临时工作目录 + 独立数据根），
     轮询 `/api/health` 到 200 才算过；起不来就把它的 stderr 尾部打出来。

跑法（仓库根）：

    PYTHONIOENCODING=utf-8 .venv\\Scripts\\python.exe scripts/probe_readme_quickstart.py
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # 与其余脚本同一写法：不假设装过
from rolecard_agent.core.tools.run import terminate_process_tree  # noqa: E402

README = ROOT / "README.md"
BLOCK_HEADING = "## 快速开始"


def _venv_python() -> str:
    """本平台 venv 里的解释器（形状随平台：Windows 是 Scripts/python.exe，类 Unix 是 bin/python）。

    找不到就用调用方自己那一份 —— 与 gate.py 同一判据，别再各写一套硬编码 Windows 路径
    （那正是 CI 第一发红的原因）。
    """
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


# 裸解释器 / 裸 uvicorn：这两类在 README 里出现就是红（理由见模块 docstring）。
_BARE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*uvicorn\b"), "裸 uvicorn（src 布局下需 PYTHONPATH，照抄必挂）"),
    (re.compile(r"^\s*python\s+scripts/"), "裸 python（Windows 上可能是 Store 占位，静默退场）"),
    (re.compile(r"^\s*npm\s+install\b"), "npm install（有 lockfile，一律用 npm ci）"),
)


def _quick_start_lines() -> list[str]:
    """取「快速开始」那一节的 fenced code block 里的非注释行。"""
    text = README.read_text(encoding="utf-8")
    if BLOCK_HEADING not in text:
        raise AssertionError(f"README 里没有 {BLOCK_HEADING} 这一节")
    body = text.split(BLOCK_HEADING, 1)[1]
    fence = re.search(r"```(?:bash|sh|shell)?\n(.*?)```", body, re.S)
    if not fence:
        raise AssertionError("「快速开始」下面找不到代码块")
    lines = [ln for ln in fence.group(1).splitlines() if ln.strip()]
    return [ln for ln in lines if not ln.strip().startswith("#")]


def _joined_commands() -> list[str]:
    """把 README 里用 `\\` 续行的命令拼回单条（按行匹配会漏掉续行后的参数）。"""
    out: list[str] = []
    buf = ""
    for line in _quick_start_lines():
        buf += line.rstrip() + " " if line.rstrip().endswith("\\") else line
        if line.rstrip().endswith("\\"):
            buf = buf.rstrip("\\ ").rstrip()
            continue
        out.append(buf.strip())
        buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [c for c in out if c]


def _start_command(cmds: list[str]) -> str:
    """文档里那条"起服务"的命令：必须存在且只有一条含糊不得。"""
    hits = [c for c in cmds if "run_api.py" in c]
    if not hits:
        raise AssertionError("README 快速开始里没有用 scripts/run_api.py 起服务的命令")
    if len(hits) > 1:
        raise AssertionError(f"README 里有多条启动命令，读者不知道该照哪条：{hits}")
    return hits[0]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_the_documented_command(cmd: str, workdir: Path, port: int) -> subprocess.Popen[str]:
    r"""原样执行文档那一条（只在 shell 层做两处替换：端口与数据根，命令本身不换）。

    **cwd 是仓库根**：README 里的命令是相对仓库根写的（`.\.venv\Scripts\python.exe`），
    读者的位置就在仓库根 —— 换到临时目录去跑会把"路径不存在"报成"文档错了"。
    隔离靠环境变量：数据根指到临时目录，探针绝不碰安装根的真库（见
    rolecard-live-scratch-probe 的四条红线）。
    端口两处都给：`RUN_API_PORT`（run_api.py 只认这个环境变量，没有 --port），以及把文档里
    写死的 8000 换成抢来的空闲口 —— 两条都覆盖，才不会因为 README 换写法而误红。

    **只有"形状"层被规范化，命令内容与参数一个字不改**：README 面向 Windows 读者，写的
    是 `.\\.venv\\Scripts\\python.exe` 与 `scripts\\run_api.py`；CI 的 runner 是 Linux，
    所以解释器换成本平台 venv 里那一份、路径分隔符换成本平台分隔符。除此之外原样执行 ——
    这条探针要验的恰恰是"文档说的那条命令本身对不对"，不是"能不能凑活跑起来"。
    """
    real = cmd.replace("8000", str(port))
    # 替换用函数而不是字符串：Windows 的路径里有反斜杠，作为 replacement 字符串会被
    # `re.sub` 当成转义序列（实测 `bad escape \U`）。
    real = re.sub(
        r"""^\s*(?:"[^"]*\.venv[^"]*"|\S*\.venv\S*)""",
        lambda _m: _venv_python(),
        real,
        count=1,
    )
    if os.sep != "\\":
        real = real.replace("\\", os.sep)
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "RUN_API_PORT": str(port),
        "SQLITE_PATH": str(workdir / "app.db"),
        "CHROMA_PATH": str(workdir / "chroma"),
        "UPLOAD_DIR": str(workdir / "uploads"),
        "MEMORY_EXTRACT_AUTO": "0",
        "MODEL_PIN_ON_STARTUP": "0",
    }
    return subprocess.Popen(  # noqa: S603
        real,
        shell=True,
        cwd=str(ROOT),  # 读者的位置：仓库根（命令里的相对路径是按这里写的）
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        # 独立进程组是**收尾那句 killpg 的前提**：`terminate_process_tree` 在 POSIX 上按
        # 组杀，孩子若不搬家，`getpgid` 给回来的就是**调用方自己的组** —— 于是"收一个后端"
        # 变成 SIGKILL 整条 CI 步骤（2026-10-09 run 37842997481 实测：起服务后 2 秒整步没，
        # 被杀名单里连 `timeout` 的外壳都在）。Windows 上这条不生效也不需要（走 taskkill
        # 按树杀），所以本机跑半年照不出它。
        start_new_session=os.name != "nt",
    )


def _who_listens(port: int) -> str:
    """把"还在听这个端口的是谁"打出来 —— 只说"仍有监听"不够定位。

    2026-10-09 的 CI 现场就是这句孤立无援：Linux 上 `killpg` 之后端口仍应答，而本机（Windows）
    永远复现不出来，日志里除了"仍监听"一个字都没有 ⇒ 又一次只能倒推。判据负责说"红"，
    现场负责说"为什么"，两件事都得有。
    """
    if os.name == "nt":
        cmds: list[list[str]] = [
            ["netstat", "-ano", "-p", "TCP"],
        ]
    else:
        cmds = [["ss", "-ltnp"], ["netstat", "-ltnp"]]
    for cmd in cmds:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue  # 这个工具不在这台机器上：换下一个，别把取证本身弄炸
        if proc.returncode != 0:
            continue
        needle = f":{port}"
        hits = [ln.strip() for ln in (proc.stdout or "").splitlines() if needle in ln]
        if hits:
            return f"{Path(cmd[0]).name}：" + " | ".join(hits[:4])
        return f"{Path(cmd[0]).name} 里没有 :{port} 的 LISTEN 行（应答可能是 TIME_WAIT 残连）"
    return "（这台机器上没有 netstat/ss，拿不到持有者）"


def _port_still_listening(port: int) -> bool:
    """还能连上 = 还有人**在答** —— 比"杀过了"这件事本身可信。

    用一次带短超时的 TCP 连接来判，不去解析 `netstat` 的列（那是另一类"读不到就当没有"
    的坑：分隔符与本地化输出都会变，而这一句要的是"到底还有没有人应答"）。
    """
    with socket.socket() as probe:
        probe.settimeout(0.6)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    workdir = Path(os.environ.get("TEMP", "/tmp")) / "readme_quickstart_probe"
    workdir.mkdir(exist_ok=True)
    cmds = _joined_commands()

    problems: list[str] = []
    for cmd in cmds:
        for pattern, why in _BARE_PATTERNS:
            if pattern.search(cmd):
                problems.append(f"{why}：{cmd}")

    server: subprocess.Popen[str] | None = None
    try:
        if problems:
            for line in problems:
                print(f"❌ {line}")
            return 1
        start = _start_command(cmds)
        port = _free_port()
        print(f"照 README 起服务：{start}（端口 {port}，数据根 {workdir}）")
        server = _run_the_documented_command(start, workdir, port)
        deadline = time.time() + 90
        health: object = None
        while time.time() < deadline:
            if server.poll() is not None:
                err = (server.stderr.read() if server.stderr else "") or ""
                print(f"❌ 文档里的命令直接退出（code={server.returncode}）：\n{err[-1500:]}")
                return 1
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:  # noqa: S310
                    if r.status == 200:
                        health = r.status
                        break
            except Exception:  # noqa: BLE001 - 还没就绪
                time.sleep(1.0)
        if health != 200:
            print("❌ 90 秒内 /api/health 没到 200")
            return 1
        print(f"✅ README 快速开始的命令可跑：/api/health = {health}")
        return 0
    except AssertionError as exc:
        print(f"❌ {exc}")
        return 1
    finally:
        if server is not None:
            # **按树杀，不是只杀外壳**：上面那条命令是 `shell=True` 起的，Windows 上
            # 外壳是 cmd.exe，`terminate()` 只撤掉 cmd，真正的后端（launcher + uvicorn 子进程）
            # 原地活着。实测这一格留了 **两个** 孤儿 python 在 127.0.0.1:62911 上答了
            # 20 多分钟 —— 而这一步在 CI 档里每次都跑（10-02 轮 `R102-43`）。
            # 实现仍只有 `core/tools/run.py` 那一份（那里还多一条 POSIX 的 killpg 支路）。
            terminate_process_tree(server)
            # 等不等得到无所谓：真正的判据是下面那一次端口回读，不是这里的返回值。
            with contextlib.suppress(Exception):
                server.wait(timeout=20)
            if _port_still_listening(port):
                # **可乐，不只是打印一句**：这一步宣称"不给读者的机器留东西"，
                # 而留下一个还在答的后端是**确证的负面**（本轮就是这么留了两个孤儿）。
                # 打印了却回 0 的格子，与"存在但从不输出的 warns 列表"是同一件事。
                #
                # 先分两种可能，别把"内核回收滞后"报成"留了孤儿"（2026-10-09 CI 上这一格
                # 在 Linux 臂红、Windows 臂绿，而两边日志里除了一句"仍监听"什么都不知道）：
                # 给 3 次、每次 1 秒的宽限 —— 还在答就点名是谁在答。真凶与滞后从此分得开。
                grace = 0
                while grace < 3 and _port_still_listening(port):
                    time.sleep(1.0)
                    grace += 1
                if _port_still_listening(port):
                    who = _who_listens(port)
                    if server.poll() is None:
                        shell_state = "还活着（收树没收到它）"
                    else:
                        shell_state = f"已退（code={server.returncode}）"
                    print(
                        f"❌ 端口 {port} 上仍有监听（宽限 {grace}s 后照旧）—— "
                        f"这一趟的后端没收干净。\n"
                        f"   外壳进程状态：{shell_state}\n"
                        f"   持有者：{who}\n"
                        f"   （本机/该平台的收树形状与此不同 —— 别按另一侧的绿推断这一侧。）"
                    )
                    raise SystemExit(1)
                print(f"✅ 端口 {port} 在宽限 {grace}s 后腾空（内核回收滞后，不是孤儿）")
            else:
                print(f"✅ 端口 {port} 已腾空（整棵树收干净）")


if __name__ == "__main__":
    sys.exit(main())
