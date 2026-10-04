"""run_command —— 角色的"在授权目录里执行命令"能力（架构计划 C·§6.2）。

## 为什么命令也要"审批"

fs 工具只是读写文件；命令是**执行任意逻辑**（装包、跑脚本、连网、删东西）。
所以模型提议的命令**默认要人批准才真跑**（`RUN_APPROVAL=manual`）：

  提交（无记录）→ pending → 用户在界面批准 → **后台执行一次** → done（结果回填）
                     → 拒绝 → rejected（终态）

批准是**一次性**的：某条命令一旦跑过（done），工具再被调用同一命令就返回历史
结果，**不重复执行** —— "批准一次跑 N 遍"是审批机制最怕的滑坡。要重跑需换命令
（或改参数）。`auto` 档（绿色通道，仅自研/可信任务用）= 无审批直接执行。

## 执行边界（与 fs 工具同一套 rigor）

  * `cwd` 必须落在任务目录内（`_resolve_within` 收敛，与 files.py 同源）；
  * 超时用 `settings.tool_timeout_seconds`（执行器同款上限）；
  * 输出按字节截断（`OUTPUT_MAX_BYTES`），回填给审批记录/模型的东西有上界；
  * 命令含独立 venv 注入：任务目录对应 `.venv-tasks/<目录名>`（与 .venv / .venv-ocr
    同级的独立解释器）存在时，把它的 Scripts/bin 前置到 PATH —— 命令里的 `python`
    就解析到独立 venv（主 .venv 只读引用、不装包，见架构计划 §6.2）；
  * 全流程写审计（actor="agent"）：命令、目录、退出码、耗时、输出字节，**不记输出内容**。

## 为什么审批执行在"后台线程"

批准这个 HTTP 请求必须快（前端 30s 超时），而命令可能要跑几十秒 —— 所以 approve
只改状态，`run_approval_execution` 在独立 `_APPROVAL_EXECUTOR` 里执行并回填。该池是
进程级资源，与 `_CHAT_POOL` 同生命周期（scripts/run_api.py 的退出路径关闭）。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from rolecard_agent.base.audit import tool_audit as _audit
from rolecard_agent.config import Settings
from rolecard_agent.core.approvals import ApprovalNotFound, ApprovalService
from rolecard_agent.core.tools.errors import ToolExecutionError
from rolecard_agent.core.workspace import make_dir_resolver, resolve_task_dir, resolve_within
from rolecard_agent.storage.db import SqlConnection

# 单条命令返回给模型 / 审批记录的输出上限（字符，近似字节）。命令的输出能被塞进
# prompt，就必须有上界（与 web_fetch / fs_read 同一哲学）。
OUTPUT_MAX_BYTES = 64 * 1024

# 审批后的后台执行池：并发审批的机器不会同时在本地跑很多个命令。
_APPROVAL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="approve-run")

# 任务目录独立 venv 的根（与本仓库的 .venv / .venv-ocr 平级）。
_TASK_VENV_ROOT = Path(__file__).resolve().parents[4] / ".venv-tasks"


class RunCommandError(ToolExecutionError):
    """命令执行的可读失败。"""


def shutdown_approval_executor() -> None:
    """进程退出路径：释放审批后台池（不等待在跑的命令，进程即将消亡）。"""
    _APPROVAL_EXECUTOR.shutdown(wait=False)


def _resolve_within(root: Path, rel_path: str) -> Path:
    """cwd 的路径边界：委托给 `core/workspace.resolve_within`（唯一实现），
    只把异常换成命令工具的可读失败类型。"""
    return resolve_within(root, rel_path, error_cls=RunCommandError, what="在任务目录内执行命令")


def _task_venv_bin(task_dir: Path) -> str | None:
    """任务目录对应的独立 venv 可执行目录；不存在 = None（命令回落系统 PATH）。

    `.venv-tasks/<目录名>`：把 Scripts/bin 前置进 PATH，命令里的 `python` 即解析到
    独立环境 —— 主 .venv 保持"只读引用、禁止安装"（架构计划 §6.2）。
    """
    venv = _TASK_VENV_ROOT / task_dir.name
    for sub in ("Scripts", "bin"):
        if (venv / sub).is_dir():
            return str(venv / sub)
    return None


def _build_env(task_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    venv_bin = _task_venv_bin(task_dir)
    if venv_bin:
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    return env


def _merge_output(stdout: str, stderr: str) -> str:
    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return "\n".join(parts)


def _truncate(text: str, limit: int = OUTPUT_MAX_BYTES) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出过长，已截断，共 {len(text)} 字符）"


@dataclass(slots=True)
class RunResult:
    """命令执行结果。`exit_code=None` = 没跑完（超时 / 启动失败）。"""

    exit_code: int | None
    output: str  # 已截断的可读输出
    output_bytes: int  # 截断前的原始字节数（审计用，不记内容）
    duration_ms: int


def terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """杀掉整棵进程树。

    **全仓唯一的"按树杀"**：`scripts/probe_readme_quickstart.py` 也用它（`R102-43`）。
    Windows 的 shell=True 是 cmd → 子进程的树：只杀父进程的话，
    实测 communicate() 会干等到孙进程退完（timeout 形同虚设）。taskkill /T /F 按树杀；
    POSIX 用 start_new_session 建的独立进程组 + killpg。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        # 守卫用 `sys.platform` 而不是 `os.name`：mypy 只认前一种收窄。换过来之后，
        # Windows 档看不到 killpg 这半（typeshed 里它不存在，原先那个 ignore 就是为此打的，
        # 现在不再需要），Linux 档看得到且不带 ignore —— 这半真正的运行平台就是 Linux
        # （容器里跑的就是它，CI 的 mypy 在那个档上查它）。
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()


def execute_command(command: str, cwd: Path, *, timeout_seconds: float) -> RunResult:
    """在任务目录内执行命令并捕获输出。超时/启动失败都返回可读结果，不抛异常。

    用 Popen + communicate(timeout) 而不是 subprocess.run：run() 在超时时只 kill 父
    进程，Windows 的 cmd / POSIX shell 派生的子进程会继续跑满整个 timeout —— 模型等待
    时长失控。这里超时 = 杀整棵进程树后再收尸。
    """
    start = time.monotonic()
    use_timeout = timeout_seconds > 0
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_build_env(cwd),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        return RunResult(
            exit_code=None,
            output=f"无法启动命令（{type(exc).__name__}）：{exc}",
            output_bytes=0,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    stdout = ""
    stderr = ""
    timed_out = False
    try:
        if use_timeout:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        else:
            stdout, stderr = proc.communicate()
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(proc)
        with contextlib.suppress(OSError):
            stdout, stderr = proc.communicate()
    exit_code: int | None = proc.returncode
    if timed_out:
        # 被杀进程的 returncode 是 1（cmd 被 taskkill 撤掉）—— 对调用方不算"命令失败"，
        # 语义是"超时了"。统一置 None，界面/模型读到"进行中未完成"而不是误导性退出码。
        exit_code = None
    raw = _merge_output(stdout or "", stderr or "")
    if timed_out:
        raw = f"命令超过 {timeout_seconds:g}s 未结束，已终止。\n" + raw
    return RunResult(
        exit_code=exit_code,
        output=_truncate(raw),
        output_bytes=len(raw.encode("utf-8", errors="replace")),
        duration_ms=int((time.monotonic() - start) * 1000),
    )


def run_approval_execution(
    approval_id: int,
    *,
    settings: Settings,
    conn: SqlConnection | None,
) -> None:
    """后台执行一条**已批准**的命令：执行 → 回填审批结果 → 写审计。

    只对刚转 approved 的记录干活：已被拒绝 / 已是 done 的不重复执行。
    由路由在 approve 后丢进 `_APPROVAL_EXECUTOR`。
    """
    if conn is None:
        return
    approvals = ApprovalService(conn)
    try:
        row = approvals.get(approval_id)
    except ApprovalNotFound:
        return
    if row["status"] != "approved":
        return
    # 终态兜底（`R102-47`）：这个函数跑在**无人监督**的后台线程里，`_audit`/`finish` 的
    # 写库撞上别的线程持久的写事务（busy_timeout 之后 OperationalError；`R102-44` 实测一轮
    # 可持会话写锁 150s —— 恰是这里最易撞的时刻）就会沉进被弃置的 Future：状态机停在
    # approved，"已获批准，正在执行…"成了永久谎言，且没有任何 API 能把它推进到 done。
    # 所以失败本身也要落成终态（done + result.error）—— "批准=执行一次"由状态机自足，
    # 任何路径都达终态，不靠运气。
    try:
        cwd = resolve_task_dir(settings, conn)
        if row["cwd"]:
            try:
                cwd = _resolve_within(cwd, row["cwd"])
            except RunCommandError:
                cwd = resolve_task_dir(settings, conn)
        result = execute_command(row["command"], cwd, timeout_seconds=settings.tool_timeout_seconds)
        _audit(
            conn,
            "run_command",
            str(cwd),
            {
                "command": row["command"],
                "exit_code": result.exit_code,
                "output_bytes": result.output_bytes,
                "duration_ms": result.duration_ms,
            },
        )
        approvals.finish(
            approval_id,
            {
                "exit_code": result.exit_code,
                "output": result.output,
                "output_bytes": result.output_bytes,
                "duration_ms": result.duration_ms,
            },
        )
    except Exception as exc:  # noqa: BLE001 -- 兜底必须盖住一切，否则就回到"卡死在 approved"
        _finish_terminal_error(approvals, approval_id, exc)


def _finish_terminal_error(approvals: ApprovalService, approval_id: int, exc: Exception) -> None:
    """把后台执行的失败回填成终态（`R102-47`）：行进 done，result 里带 error。

    连这条回填也失败（比如同一把写锁还没放）就只剩 stderr 一行日志 —— 此时行仍停在
    approved，由**下一次开机的清扫**（`approvals.sweep_interrupted`）收尾；那是已知的
    最坏路径，不是设计路径。
    """
    try:
        approvals.finish(
            approval_id,
            {"error": f"{type(exc).__name__}: {exc}"},
        )
    except Exception:  # noqa: BLE001 -- 见上：最后只剩可观察性
        print(
            f"[approvals] 审批 #{approval_id} 的终态兜底也失败了，行停在 approved，"
            f"等开机清扫收尾：{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def _pending_or_result(command: str, svc: ApprovalService) -> str | None:
    """审批门：返回需要"非执行"答复的文本；None = 放行交给执行器直接执行。"""
    status = svc.status_of(command)
    if status is None:
        return None
    row = svc.latest(command)
    if row is None:
        return None
    if status == "pending":
        return (
            f"命令已在等待审批（#{row['id']}）：{command}\n批准后我会执行，并把结果带回任务目录。"
        )
    if status == "rejected":
        return f"命令已被拒绝（#{row['id']}）：{command}\n请换一种做法，或由操作员重新提交审批。"
    if status == "approved":
        return f"命令已获批准，正在执行（#{row['id']}）：{command}\n稍后再问我结果。"
    # done：已执行过一次，返回历史结果（不重复执行）
    result = row["result"] or {}
    exit_code = result.get("exit_code")
    output = result.get("output", "")
    return (
        f"（该命令已执行过，审批 #{row['id']}，退出码 {exit_code}，"
        f"耗时 {result.get('duration_ms', 0)}ms）\n{output}"
    )


def make_run_tool(
    *,
    settings: Settings,
    conn: SqlConnection | None = None,
    approvals: ApprovalService | None = None,
    dir_resolver: Callable[[], Path] | None = None,
) -> BaseTool:
    """构建 run_command 工具。

    `approvals` 可注入（默认从 conn 现建）；`dir_resolver` = 每调用实时解析的任务目录
    （默认连 conn 的实时解析器）。None 连接 = 命令执行不落审计、审批回落系统 PATH。
    """
    svc = approvals if approvals is not None else (ApprovalService(conn) if conn else None)
    if dir_resolver is not None:
        root_of = dir_resolver
    elif conn is not None:
        root_of = make_dir_resolver(settings, conn)
    else:
        root_of = lambda: Path(settings.workspace_dir)  # noqa: E731 - 闭包，模块内约定

    @tool("run_command")
    def run_command(command: str, cwd: str = "") -> str:
        """在任务目录内执行一条 shell 命令并把输出返回给用户看。

        command 是要执行的命令；cwd 是相对任务目录的工作目录（空 = 任务目录根）。
        执行任何命令都需要先经过命令审批（见设置——命令执行）。"""
        if not settings.run_tools_enabled:
            return "命令执行已被管理员关闭（RUN_TOOLS_ENABLED=0）。"
        cmd = (command or "").strip()
        if not cmd:
            return "命令为空。"
        try:
            target = _resolve_within(root_of(), cwd or ".")
        except RunCommandError as exc:
            return str(exc)
        if not target.is_dir():
            return f"目录不存在：{cwd or '.'}"
        if settings.run_approval != "auto" and svc is None:
            # fail-closed（2026-10-04 审查快照的审批条目）：审批档开着的却没接审批服务 ——
            # 从前这一格直接落到底部"未接审批连接：直接执行"，等于任何忘传 conn/approvals 的
            # 新接线或测试路径都会**静默失去审批门**。宁可拒绝执行让配置问题当场现形。
            return (
                "命令审批服务未接（数据库连接缺失），而当前配置要求审批 —— 命令被拒绝执行。"
                "这是装配问题，请检查 run_command 工具的接线"
                "（RUN_APPROVAL != auto 时必须提供审批服务）。"
            )
        if settings.run_approval != "auto" and svc is not None:
            gate = _pending_or_result(cmd, svc)
            if gate is not None:
                return gate
            row = svc.submit(
                cmd,
                cwd=str(target),
                thread_id=None,
            )
            return (
                f"命令已提交审批（#{row['id']}）：{cmd}\n"
                "待你在界面上批准后，我会在任务目录执行并把结果带回来。"
            )
        # auto 档（或未接审批连接）：直接执行。
        result = execute_command(cmd, target, timeout_seconds=settings.tool_timeout_seconds)
        _audit(
            conn,
            "run_command",
            str(target),
            {
                "command": cmd,
                "exit_code": result.exit_code,
                "output_bytes": result.output_bytes,
                "duration_ms": result.duration_ms,
            },
        )
        tail = f"（退出码 {result.exit_code}，耗时 {result.duration_ms}ms）"
        if result.exit_code is None:
            return f"{result.output}\n{tail}"
        return f"{result.output}\n{tail}"

    return run_command


__all__ = [
    "RunCommandError",
    "execute_command",
    "make_run_tool",
    "run_approval_execution",
    "shutdown_approval_executor",
]
