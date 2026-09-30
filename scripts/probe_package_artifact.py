"""打一发包之后、**不装**也能自证"这一包是这一版"：三路判据打在产物上。

为什么需要这一发：`install_package.ps1` 的五道验货全都发生在**装完之后**，而用户选了
「先不打，攒着一起」（09-30）⇒ 有一段时间产物存在、机器上装的还是上一包。那段路上没有
任何一条现成判据能回答"刚打出来的这份对不对"，而"构建退出码 0"从来不等于产物是当前的
（旧轮台账 §12.4 从头在说的那件事）。

三路各打**一层**，缺一层就会重演"前端是新的、后端是旧的还照样打印 OK"那次：
① 前端层 —— 包内 `_internal/frontend/dist` 与仓库 `frontend/dist` **逐文件 sha256 全等**，
   并核对 `GET /` 真正吐出来的入口资产（判据从仓库 dist 派生，不写死哈希）；
② 后端层 —— 包内那份 `rolecard-backend.exe` 的 sha256 == `build/sidecar` 里刚构建的那份
   （那份不存在就跳过并说明，不假红）；
③ frozen 层 —— 起包内那个 exe 答一次 `/api/health`，再读一个**只可能来自新代码**的读数
   （`/api/uploads/orphans` 的键集合：`dangling` 是 `R28-19` 才加的）。收进 PYZ ≠ frozen 可用，
   这一层与 CI 上那条 `发布链` job 量的是同一件事。

安全边界（与探针同一套规矩）：数据根落在系统临时目录、端口现取、**绝不打 :8000**、
只 terminate 自己 spawn 的那个 pid。

跑法：
    .venv\\Scripts\\python.exe scripts/probe_package_artifact.py
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
UNPACKED = ROOT / "shell" / "release" / "win-unpacked"
PKG_BACKEND = UNPACKED / "resources" / "rolecard-backend"
PKG_DIST = PKG_BACKEND / "_internal" / "frontend" / "dist"
REPO_DIST = ROOT / "frontend" / "dist"
FRESH_SIDECAR = ROOT / "build" / "sidecar" / "rolecard-backend" / "rolecard-backend.exe"
LOG = ROOT / "build" / "probe_package_artifact.log"
ENTRY_RE = re.compile(r"assets/(index-[A-Za-z0-9_\-]+\.js)")

# 结论行带「✅」与「①」：Windows 控制台默认 codepage 是 GBK，不重配编码第一个 print 就抛
# UnicodeEncodeError（console_encoding 那条一致性检查抓的就是这个）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: pathlib.Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    entries: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            entries[p.relative_to(root).as_posix()] = _sha256(p)
    return entries


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return int(resp.status), resp.read()


def _repo_entry() -> str:
    """仓库 `frontend/dist/index.html` 真正引用的那个入口文件名（判据**派生**，不写死哈希）。"""
    html = REPO_DIST / "index.html"
    if not html.exists():
        return ""
    found = ENTRY_RE.search(html.read_text(encoding="utf-8", errors="ignore"))
    return found.group(1) if found else ""


def check_frontend() -> bool:
    """① 包内 dist 与仓库 dist 逐文件全等，且期望入口哈希**从仓库 dist 派生**。"""
    repo = _files(REPO_DIST)
    packed = _files(PKG_DIST)
    if not repo:
        print("  ✗ 仓库 frontend/dist 是空的 —— 先跑 npm run build")
        return False
    if not packed:
        print(f"  ✗ 包里没有 dist：{PKG_DIST} 不存在（③ 也没法跑）")
        return False
    expected = _repo_entry()
    print(f"  仓库 dist {len(repo)} 个文件 / 包内 {len(packed)} 个文件")
    print(f"  入口资产（从仓库 dist/index.html 派生）= {expected or '(没数出来)'}")
    if repo != packed:
        only_repo = sorted(set(repo) - set(packed))
        only_pkg = sorted(set(packed) - set(repo))
        differ = sorted(n for n in set(repo) & set(packed) if repo[n] != packed[n])
        print(f"  ✗ 不一致：仓库独有 {only_repo[:3]} 包内独有 {only_pkg[:3]} 哈希不同 {differ[:3]}")
        return False
    print("  ✓ 逐文件 sha256 全等")
    return True


def check_backend_binary() -> bool:
    """② 包内那份 exe == 刚构建的那份。刚构建的那份不在就**跳过**（不假红，但要说明）。"""
    inside = PKG_BACKEND / "rolecard-backend.exe"
    if not inside.exists():
        print(f"  ✗ 包里没有后端：{inside}")
        return False
    if not FRESH_SIDECAR.exists():
        print(f"  ⚠ 跳过：{FRESH_SIDECAR.relative_to(ROOT)} 不存在 —— 这一发比不了「刚构建的那份」")
        print(f"    包内那份 sha256 = {_sha256(inside)[:12]}…"
              "（想比这一发就先跑 scripts/build_sidecar.py）")
        return True
    a, b = _sha256(FRESH_SIDECAR), _sha256(inside)
    print(f"  刚构建 = {a[:12]}…  包内 = {b[:12]}…")
    if a != b:
        print("  ✗ 包里那份不是刚构建的 —— 只重打了 NSIS 就是这个形状")
        return False
    print("  ✓ 后端 bundle 是刚构建的那份")
    return True


def check_frozen_boot() -> bool:
    """③ 起包内那个 exe：health 200 + `GET /` 的入口 == 仓库入口 + orphans 的键集合。"""
    exe = PKG_BACKEND / "rolecard-backend.exe"
    if not exe.exists():
        print(f"  ✗ 没有 {exe}")
        return False
    want = _repo_entry()
    if not want:
        print("  ✗ 仓库 frontend/dist/index.html 没数出入口文件名")
        return False
    port = _free_port()
    data_root = pathlib.Path(tempfile.mkdtemp(prefix="rc_pkg_artifact_"))
    env = {
        **os.environ,
        "DATA_ROOT": str(data_root),
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",
        "MODEL_PIN_ON_STARTUP": "0",
    }
    base = f"http://127.0.0.1:{port}"
    print(f"  起 {exe.name} :: port={port} DATA_ROOT={data_root}")
    with LOG.open("wb") as log:
        args = [str(exe)]
        srv = subprocess.Popen(
            args, env=env, cwd=str(exe.parent), stdout=log, stderr=subprocess.STDOUT
        )
    try:
        health = ""
        for i in range(45):
            time.sleep(2)
            if srv.poll() is not None:
                print(f"  ✗ 随包后端起了又退 exit={srv.returncode}（第 {i + 1} 次探测）")
                print(LOG.read_text("utf-8", "replace")[-1500:])
                return False
            try:
                status, body = _get(f"{base}/api/health")
            except (urllib.error.URLError, OSError):
                continue
            if status == 200:
                health = body.decode("utf-8", "replace")
                break
        if not health:
            print("  ✗ 90 秒内 /api/health 没答 200")
            return False
        print(f"  ✓ health 200 :: {health[:120]}")

        served = ""
        try:
            _, page = _get(f"{base}/")
            found = ENTRY_RE.search(page.decode("utf-8", "replace"))
            served = found.group(1) if found else ""
        except (urllib.error.URLError, OSError) as exc:
            print(f"  ✗ GET / 没答：{exc}")
            return False
        if served != want:
            print(f"  ✗ 它吐的入口是 {served}，仓库 dist 是 {want}")
            return False
        print(f"  ✓ 它真的在答这一版界面（{served}）")

        try:
            _, blob = _get(f"{base}/api/uploads/orphans")
            keys = sorted(json.loads(blob.decode("utf-8")))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"  ✗ /api/uploads/orphans 没答：{exc}")
            return False
        print(f"  或孤儿盘点的键集合 = {keys}")
        if "dangling" in keys:
            print("  ✓ 有 dangling（`R28-19` 那一笔在后端 bundle 里也活着）")
        else:
            print("  ✗ 没有 dangling —— 包里那份后端是旧的")
        return "dangling" in keys
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=20)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(srv.pid), "/T", "/F"], check=False)
        print(f"  已终止自己起的 pid={srv.pid}（未碰 :8000，未动任何装机目录）")


def main() -> int:
    layers = [
        ("① 前端层：包内 dist == 仓库 dist", check_frontend),
        ("② 后端层：包内 exe == 刚构建那份", check_backend_binary),
        ("③ frozen 层：起它、答话、读一个只可能来自新代码的数", check_frozen_boot),
    ]
    failed: list[str] = []
    for title, fn in layers:
        print(title)
        if not fn():
            failed.append(title)
    print("=" * 56)
    if failed:
        print(f"❌ {len(failed)}/{len(layers)} 层没过：" + "；".join(failed))
        return 1
    print(f"✅ {len(layers)} 层全过 —— 这一包就是这一版（**没有安装**，机器上那份没动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
