"""打随包后端（sidecar）：跑 PyInstaller onedir，产物落在 `build/sidecar/`。

用法：

    .venv\\Scripts\\python.exe scripts\\build_sidecar.py

前置只有一件：`cd frontend && npm run build` —— 控制台界面是后端**静态托管**的 `dist`，
没有它，装出来的包能起 API 但打不开页面。

产物不复制到 `shell/`：electron-builder 的 `extraResources` 直接指过来这一份（少一次复制
就少一处"改了 spec 忘了重拷"的错位）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "sidecar"
WORK = ROOT / "build" / "sidecar-work"
SPEC = ROOT / "packaging" / "rolecard-backend.spec"

# 与 `gate.py` 同一族、同一个漏网（`R26-24` 当时只修了 gate.py）：Windows 控制台默认 GBK，
# 收尾那句「✅」在 **PyInstaller 已经全部成功之后** 抛 UnicodeEncodeError ⇒ 退出码 1，
# 看起来像"打包失败"而产物其实是对的（实测：225 MB 的 bundle 已经躺在 build/sidecar 里）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024


def main() -> int:
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        print("缺 frontend/dist：先 `cd frontend && npm run build`", file=sys.stderr)
        return 2
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
    bundle = OUT / "rolecard-backend"
    if result.returncode != 0 or not (bundle / "rolecard-backend.exe").exists():
        print(f"PyInstaller 失败（exit={result.returncode}），见上面的日志", file=sys.stderr)
        return result.returncode or 1
    print(f"\n✅ 随包后端：{bundle}")
    print(f"   体积 {_size_mb(bundle):.0f} MB，exe = rolecard-backend.exe")
    print("   下一步：cd shell && npm run package（electron-builder 把它塞进 resources/）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
