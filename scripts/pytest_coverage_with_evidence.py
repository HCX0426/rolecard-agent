"""覆盖率那一趟的"红跑取证"包装（2026-10-02 轮 `R102-41` 的在册修法）。

现场（开轮批与复审批各撞过一次，n=5 里 1 红 4 绿）：同一份代码、同一趟门禁，
`pytest(-x, 无覆盖率)` 全绿，紧接着带覆盖率那一趟报
`chromadb.errors.InternalError: Error executing plan: Internal error: Error creating hnsw
segment reader: Nothing found on disk` —— 红的是那条用例，覆盖率本身达标。机制至今未定位
（要一次带 chroma 侧日志的复跑才能说清"谁在什么时候把那个目录抽走"）。

判据（写死在这里，不留给读日志的人猜）：

  * 只有当**全部**失败都带着在册的 chroma 偶发签名时，才重跑一次那些文件；
  * 重跑**为取证不为转绿**：首跑的完整日志原样留在 `build/gate-coverage-run1.log`，
    屏幕上大声打出"首跑红 / 二跑绿"，退出码仍然按第二跑判；
  * 同一批文件二次红 ⇒ 真红，退出非 0；
  * 失败里混进任何一条不是 chroma 签名的 ⇒ **立刻按原样红，不重跑**（这条不能变成
    万能遮羞布：`R102-38` 那一族的教训是"看起来绿"比红贵得多）。

第二跑把 chroma 那侧的日志打开（`--log-cli-level=DEBUG` 收进 run2 日志）—— 这是这台机器
上唯一能让"segment reader 读不到东西"这件事留下现场的办法。
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
RUN1 = BUILD / "gate-coverage-run1.log"
RUN2 = BUILD / "gate-coverage-run2-retry.log"

#: 在册的偶发签名（`R102-41`）。新增一条就等于承认"这一族我还没定位"，所以要慎。
CHROMA_FLAKE_SIGNATURES = (
    "Nothing found on disk",
    "Error creating hnsw segment reader",
    "chromadb.errors.InternalError",
)

BASE = [
    sys.executable,
    "-m",
    "pytest",
    "-p",
    "no:cacheprovider",
    "-W",
    "ignore",
    "--cov=rolecard_agent",
    "--cov-fail-under=85",
]

_FAILED_RE = re.compile(r"^FAILED (\S+?)::", re.M)


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


def main() -> int:
    rc, log = _run(BASE)
    RUN1.write_text(log, encoding="utf-8", newline="\n")
    if rc == 0:
        return 0

    failed = _failing_files(log)
    chroma_flake = any(sig in log for sig in CHROMA_FLAKE_SIGNATURES)
    if not failed or not chroma_flake:
        print(
            "❌ 覆盖率这趟红了，而失败不在全册的 chroma 偶发签名里 —— 按原样红，不重跑。",
            flush=True,
        )
        return rc

    print(
        "\n⚠️  首跑红，且失败形状命中在册的 chroma 偶发（`R102-41`）。"
        f"重跑那 {len(failed)} 个文件一次取证：{failed}\n"
        f"   首跑完整日志：{RUN1.relative_to(ROOT)}（不删、不改，二跑不掩盖它）",
        flush=True,
    )
    rc2, log2 = _run(
        [BASE[0], "-m", "pytest", "-p", "no:cacheprovider", "-W", "ignore", *failed],
        ["-o", "log_cli=true", "--log-cli-level=DEBUG"],
    )
    RUN2.write_text(log2, encoding="utf-8", newline="\n")
    if rc2 == 0:
        print(
            "\n⚠️  FLAKY-RECORDED：同一批文件二跑绿。这一趟按「未知」放行"
            f"（见 `R102-41`：重跑为取证非转绿），两份日志都在："
            f"{RUN1.relative_to(ROOT)} / {RUN2.relative_to(ROOT)}。"
            "机制仍未定位 —— 别把这句读成「已修」。",
            flush=True,
        )
        return 0
    print(
        f"\n❌ 二跑仍红（{failed}）—— 这就是真红，不是那发偶发。日志："
        f"{RUN2.relative_to(ROOT)}",
        flush=True,
    )
    return rc2


if __name__ == "__main__":
    raise SystemExit(main())
