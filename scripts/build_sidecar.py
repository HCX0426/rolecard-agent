"""打随包后端（sidecar）：跑 PyInstaller onedir，产物落在 `build/sidecar/`。

用法：

    .venv\\Scripts\\python.exe scripts\\build_sidecar.py

前置只有一件：`cd frontend && npm run build` —— 控制台界面是后端**静态托管**的 `dist`，
没有它，装出来的包能起 API 但打不开页面。

产物不复制到 `shell/`：electron-builder 的 `extraResources` 直接指过来这一份（少一次复制
就少一处"改了 spec 忘了重拷"的错位）。

**打包前先把身份烤进去**（10-01，台账 `R28-56`）：`build/build_info.json` 记 HEAD 与工作树脏旗，
spec 把它收进 `_internal/`，`/api/health` 于是能报"这一包是从哪个 commit 打的"。没有它，
`probe_package_artifact.py` 只能比字节 —— 一次纯后端改动会让前端哈希一字不差，"全绿"就骗人。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 产物路径与名字的**唯一出处**在 `core/artifacts.py`（台账 `R28-59`）：这条链上从前有六处
# 各拼一遍 `build/sidecar/rolecard-backend`，改一次布局要在六处对齐。
sys.path.insert(0, str(ROOT / "src"))
from rolecard_agent.core.artifacts import (  # noqa: E402 - 要先改 sys.path 才import得到 src 里那份出处
    SIDECAR_DIR,
    SIDECAR_WORK_DIR,
    sidecar_bundle,
    sidecar_exe,
)

OUT = ROOT / SIDECAR_DIR
WORK = ROOT / SIDECAR_WORK_DIR
SPEC = ROOT / "packaging" / "rolecard-backend.spec"
#: 烤进产物的那份身份文件（名字的家在 `core/build_info.py`，两处不许各写一遍）。
BUILD_INFO = ROOT / "build" / "build_info.json"

# 与 `gate.py` 同一族、同一个漏网（`R26-24` 当时只修了 gate.py）：Windows 控制台默认 GBK，
# 收尾那句「✅」在 **PyInstaller 已经全部成功之后** 抛 UnicodeEncodeError ⇒ 退出码 1，
# 看起来像"打包失败"而产物其实是对的（实测：225 MB 的 bundle 已经躺在 build/sidecar 里）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024


def _git(*args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return done.stdout.strip()


#: 会进包的路径 —— `dirty` 那一格只问这些。
#: 理由（10-01 第十七包实测）：整棵工作树的脏一律算在包头上，产出的就是一个**说错话**的旗 ——
#: 那次脏的只有 `scripts/install_package.ps1`（装机那一步被 PowerShell 读，根本不在 PYZ 里），
#: 而 `/api/health` 照样回答 `dirty: true`，`probe_package_artifact` 第 ③ 层据此把这个包判红。
#: "装进去的那份后端是不是这个 commit"才是这一问的本意，所以 pathspec 就按进包的输入给。
BUNDLED_INPUT_PATHS = (
    "src",
    "packaging",
    "frontend/dist",
    "pyproject.toml",
    "requirements/requirements.txt",
    "requirements/requirements-api.txt",
    "requirements/requirements-rag.txt",
    "requirements/requirements-cloud.txt",
    "requirements/requirements-mcp.txt",
    "requirements/requirements.lock",
)


def _write_build_info() -> None:
    """把"这一包是从哪份源码打的"写进 `build/build_info.json`，由 spec 收进 `_internal/` 根。

    写不出 sha（不是 git 仓库 / git 不在）就老实写 `unknown` —— 让 `/api/health` 与判据自己去红，
    而不是造一个"两边都空所以相等"的绿。`dirty` 一起记：从脏工作树打出来的包，"装的就是 HEAD"
    这句话本来就不成立，让它在产物里带着这个旗，比让文档去猜谁改过什么诚实 —— 但**这一问的范围
    是进包的那几条路径**（见 `BUNDLED_INPUT_PATHS`），不是"这台机器上此刻还有别处在改吗"。
    """
    sha = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--", *BUNDLED_INPUT_PATHS))
    payload = {
        "git_sha": sha or "unknown",
        "built_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "dirty": dirty,
    }
    BUILD_INFO.parent.mkdir(parents=True, exist_ok=True)
    BUILD_INFO.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"构建指纹 git_sha={sha[:12] or 'unknown'} dirty={dirty} → {BUILD_INFO}", flush=True)


def main() -> int:
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        print("缺 frontend/dist：先 `cd frontend && npm run build`", file=sys.stderr)
        return 2
    _write_build_info()
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(OUT),
        "--workpath",
        str(WORK),
        str(SPEC),
    ]
    print(" ".join(cmd), flush=True)
    result = subprocess.run(cmd, check=False)
    bundle = sidecar_bundle(ROOT)
    if result.returncode != 0 or not sidecar_exe(ROOT).exists():
        print(f"PyInstaller 失败（exit={result.returncode}），见上面的日志", file=sys.stderr)
        return result.returncode or 1
    print(f"\n✅ 随包后端：{bundle}")
    print(f"   体积 {_size_mb(bundle):.0f} MB，exe = rolecard-backend.exe")
    print("   下一步：cd shell && npm run package（electron-builder 把它塞进 resources/）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
