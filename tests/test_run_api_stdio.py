"""打包态的日志编码：后端往 stdout 打的中文，必须是**能按 UTF-8 解开的字节**。

为什么要有这个文件（09-26 实测撞出来的）：桌面壳接管后端的 stdout/stderr 时先
`stream.setEncoding("utf8")` 再落盘，而 Python 在 stdout 不是终端时按 ANSI 代码页编码
（本机 cp936）。装好的那份 `backend.log` 里，1622 行中带中文的 3 行全是 U+FFFD —— 包括
主动开口那句给人看的静默原因（`S-8` 的日志出口）和启动横幅。

这里**照抄那条链的形状**：子进程、stdout 是管道、父进程按 UTF-8 解码。所以它测的不是
"函数被调用没有"，而是"换个人接管还能不能读出中文"。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

SCRIPTS = str(pathlib.Path(__file__).resolve().parents[1] / "scripts")
SENTENCE = "距上次说话不足 266 分钟"


def _run_child(code: str) -> subprocess.CompletedProcess[bytes]:
    """跑一个子进程，stdout/stderr 都收成**原始字节**（这正是壳拿到手的东西）。

    **这里不判退出码**：反面那一条要看的正是"孩子自己崩了"那一档（见那条的用例）。
    要"必须成功"的调用方走 `_spawn`。
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60)


def _spawn(code: str) -> bytes:
    done = _run_child(code)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout.strip()


def _crash_is_the_encoding_one(done: subprocess.CompletedProcess[bytes]) -> bool:
    """孩子崩了：只有**崩在编码上**才算反面成立。

    别的崩溃（解释器坏了、路径不存在…）必须红 —— 否则这条对照会把任何一次非 0 退出
    读成"果然测不出 UTF-8"，那就是一条恒绿的判据（本仓治过的那一族）。
    """
    return "UnicodeEncodeError" in done.stderr.decode("utf-8", "replace")


def test_forced_stdio_emits_utf8_through_a_pipe() -> None:
    code = (
        f"import sys; sys.path.insert(0, {SCRIPTS!r});"
        "import run_api; run_api.force_utf8_stdio();"
        f"print({SENTENCE!r})"
    )
    raw = _spawn(code)
    # 乱码的形状就是"替换符 + 零星空序列"，所以断言解得开且解出来一字不差。
    # `_spawn` 已经把末尾换行 strip 掉了。
    assert raw.decode("utf-8") == SENTENCE, raw


def test_the_same_child_without_the_fix_is_unreadable() -> None:
    """反面对照：**不设编码的那个孩子**在这台机器上要么吐乱码、要么直接崩。

    没有这一半，上一条可以在"机器本来就是 UTF-8"上假绿 —— 那时两个用例都过，而生产上
    的 cp936 从没被测到。

    "读不出中文"有两副面孔，两副都算反面成立（2026-10-08 CI 的 Windows 臂照出第二副）：
      * **乱码**：代码页认这些字（本机 cp936），孩子退 0、吐出一串按 UTF-8 解不开原句的字节；
      * **当场崩**：代码页根本不认这些字（英文 Windows runner 的 cp1252），孩子在 `print`
        那一步 `UnicodeEncodeError` 退 1 —— 比乱码更糟，但同样是"不设编码就说不清中文"。
        原版只接第一副：`_spawn` 见非 0 就断言失败，于是这条在英文 runner 上红在**它想证明的
        那件事**上。
    其余任何非 0 退出（解释器坏了、路径不存在…）**必须红** —— 把任何一次崩溃都读成"果然
    测不出 UTF-8"，这条就成了恒绿的判据（与它要防的假绿是同一族）。
    """
    probe = _spawn("import sys; print(sys.stdout.encoding)")
    default_encoding = probe.decode("utf-8", "replace").strip().lower()
    if default_encoding.replace("-", "") in ("utf8",):
        # Windows 的 Python 在非终端上用的是 ANSI 代码页，不是 UTF-8；真出现 UTF-8 说明
        # 这台机器的行为与实测那台不同 —— 明说，不假装测到了。
        print(f"\n（本机默认已是 {default_encoding}，这条测不出反面；生产那台是 cp936）")
        return
    done = _run_child(f"print({SENTENCE!r})")
    if done.returncode != 0:
        assert _crash_is_the_encoding_one(done), (
            f"不设编码的孩子退 {done.returncode}，可崩的不是编码那一条："
            f"{done.stderr.decode('utf-8', 'replace')[-300:]}"
        )
        print(
            f"\n（本机代码页 {default_encoding} 打不出这句中文，当场 UnicodeEncodeError"
            " —— 反面成立）"
        )
        return
    raw = done.stdout.strip()
    assert raw.decode("utf-8", "replace") != SENTENCE, (
        f"不设编码居然解得开（bytes={raw[:16]!r}），那这条对照就没在测东西，改断言前先查机器"
    )
