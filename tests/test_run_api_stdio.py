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


def _spawn(code: str) -> bytes:
    """跑一个子进程并把它的 stdout 收成**原始字节**（这正是壳拿到手的东西）。"""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout.strip()


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
    """反面对照：**不设编码的那个孩子**在这台机器上吐的就不是 UTF-8。

    没有这一半，上一条可以在"机器本来就是 UTF-8"上假绿 —— 那时两个用例都过，而生产上
    的 cp936 从没被测到。所以这条按环境给结论：本地默认已经是 UTF-8 就明说"这里测不出"。
    """
    probe = _spawn("import sys; print(sys.stdout.encoding)")
    default_encoding = probe.decode("utf-8", "replace").strip().lower()
    if default_encoding.replace("-", "") in ("utf8",):
        # Windows 的 Python 在非终端上用的是 ANSI 代码页，不是 UTF-8；真出现 UTF-8 说明
        # 这台机器的行为与实测那台不同 —— 明说，不假装测到了。
        print(f"\n（本机默认已是 {default_encoding}，这条测不出反面；生产那台是 cp936）")
        return
    raw = _spawn(f"print({SENTENCE!r})")
    assert raw.decode("utf-8", "replace") != SENTENCE, (
        f"不设编码居然解得开（bytes={raw[:16]!r}），那这条对照就没在测东西，改断言前先查机器"
    )
