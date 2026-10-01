"""打一发包之后、**不装**也能自证"这一包是这一版"：三路判据打在产物上。

为什么需要这一发：`install_package.ps1` 的验货全都发生在**装完之后**，而用户选了
「先不打，攒着一起」（09-30）⇒ 有一段时间产物存在、机器上装的还是上一包。那段路上没有
任何一条现成判据能回答"刚打出来的这份对不对"，而"构建退出码 0"从来不等于产物是当前的
（旧轮台账 §12.4 从头在说的那件事）。

三路各打**一层**，缺一层就会重演"前端是新的、后端是旧的还照样打印 OK"那次：
① 前端层 —— 包内 `_internal/frontend/dist` 与仓库 `frontend/dist` **逐文件 sha256 全等**，
   并核对 `GET /` 真正吐出来的入口资产（判据从仓库 dist 派生，不写死哈希）；
② 后端层 —— 包内那份 `rolecard-backend.exe` 的 sha256 == `build/sidecar` 里刚构建的那份；
   **刚构建的那份不在就红**，不再"跳过算过"（10-01 改的：旧写法在这里 `return True`，
   于是"三层全绿"可以在中间一层根本没跑的情况下打印出来，而这句话的全部意义就是三层都跑了）；
③ frozen 层 —— 起包内那个 exe，问 `/api/health` 里的 **`build.sha`（这一包自报的 commit）**
   并直接与仓库 HEAD 比 —— 这是决定性判据，与"比对对象还在不在"无关；打包那一刻工作树脏也判红
   （"装的就是 HEAD"那句此刻不成立）。`/api/uploads/orphans` 的键集合从判据**降级为读数**：
   `dangling` 是 `R28-19` 加的，第十五包里就有，它只证明"frozen 那份码真被执行了"（收进 PYZ ≠
   frozen 可用），证明不了"最新"。

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
sys.path.insert(0, str(ROOT / "src"))
# 这几条路径从前各拼一遍（盘点 P1-5），现在只从 `core/artifacts.py` 派生。
from rolecard_agent.core.artifacts import sidecar_exe, unpacked_backend, unpacked_dist  # noqa: E402

SHELL_RELEASE = ROOT / "shell" / "release"
UNPACKED = SHELL_RELEASE / "win-unpacked"
PKG_BACKEND = unpacked_backend(SHELL_RELEASE)
PKG_DIST = unpacked_dist(SHELL_RELEASE)
REPO_DIST = ROOT / "frontend" / "dist"
FRESH_SIDECAR = sidecar_exe(ROOT)
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


def _repo_head_sha() -> str:
    """仓库现在的 HEAD。读不到回空串 —— 调用方必须把它当"没有基准"，不许当"相等"。"""
    done = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return done.stdout.strip()


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


def _terminate_own(srv: subprocess.Popen[bytes], port: int) -> bool:
    """只回收**自己 spawn 的那个 pid**，并回读一次"真没了"。

    为什么回读而不是看退出码（与 `install_package.ps1` 第 [3/5] 步同一条判据，10-01 补齐）：
    `taskkill` 对"找不到那个 PID"也返回失败，只看退出码既会假红也会假绿；
    唯一可靠的问法是那个进程还在不在。不在才算这一发结束 —— 留着它等于给下一发
    探针或下一次装机留一个占着端口与临时数据根的陌生人。
    """
    srv.terminate()
    try:
        srv.wait(timeout=20)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(srv.pid), "/T", "/F"], check=False)  # noqa: S603
        time.sleep(1)
        listed = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {srv.pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
        if str(srv.pid) in listed:
            print(f"  ⚠ pid={srv.pid} 还活着（taskkill 没收掉）—— 它占着 :{port}，请手动确认")
            return False
    print(f"  已终止自己起的 pid={srv.pid}（未碰 :8000，未动任何装机目录）")
    return True


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
    """② 包内那份 exe == 刚构建的那份。**刚构建的那份不在就红**，不"跳过算过"。

    10-01 改的（盘点 P0-1）：旧写法在这里 `return True`，于是"三层全绿"这句结论可以在
    中间一层根本没跑的情况下打印出来 —— 而这句话的全部意义就是"三层都跑过了"。
    比不了就说比不了：这一层证明的是**字节**一致，第③层证明的是**身份**一致，两件事不能互替。
    """
    inside = PKG_BACKEND / "rolecard-backend.exe"
    if not inside.exists():
        print(f"  ✗ 包里没有后端：{inside}")
        return False
    if not FRESH_SIDECAR.exists():
        print(f"  ✗ 比不了：{FRESH_SIDECAR.relative_to(ROOT)} 不存在 —— 没有「刚构建的那份」当尺子")
        print(f"    包内那份 sha256 = {_sha256(inside)[:12]}…"
              "（先跑 scripts/build_sidecar.py 再打这一发）")
        return False
    a, b = _sha256(FRESH_SIDECAR), _sha256(inside)
    print(f"  刚构建 = {a[:12]}…  包内 = {b[:12]}…")
    if a != b:
        print("  ✗ 包里那份不是刚构建的 —— 只重打了 NSIS 就是这个形状")
        return False
    print("  ✓ 后端 bundle 是刚构建的那份")
    return True


def check_frozen_boot() -> bool:
    """③ 起包内那个 exe：**先问它是从哪个 commit 打的**，再看界面入口与或孤儿盘点。

    身份这一格是 10-01 加的决定性判据（盘点 P0-1）。旧版这一层问的是"`/api/uploads/orphans`
    里有没有 `dangling` 这个键"，那只能证明"至少是 `R28-19` 之后"——第十五、十六包里它都成立，
    所以它**看不见**最近那一笔纯后端改动。前端哈希（①）也一样看不见：那一笔没动界面。
    现在它自己报 sha，判据退化成一次字符串比较，与"比对对象还在不在"无关。
    """
    exe = PKG_BACKEND / "rolecard-backend.exe"
    if not exe.exists():
        print(f"  ✗ 没有 {exe}")
        return False
    want = _repo_entry()
    if not want:
        print("  ✗ 仓库 frontend/dist/index.html 没数出入口文件名")
        return False
    head = _repo_head_sha()
    if not head:
        print("  ✗ 读不到仓库 HEAD —— 这一发没有比对的基准"
                  "（不是包的问题，是这里没在 git 仓库里跑）")
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

        # —— 决定性的一格：这一包自报的 commit 是不是仓库现在的 HEAD。
        try:
            reported = (json.loads(health).get("build") or {}).get("sha") or ""
            built_dirty = (json.loads(health).get("build") or {}).get("dirty")
        except ValueError:
            reported, built_dirty = "", None
        if not reported or reported == "unknown":
            print("  ✗ 它没报构建指纹（`build.sha` 缺失或 unknown）—— 这一包不知道自己是谁")
            print("     两种可能：① 这一包是在指纹机制存在之前打的（那它本来就该被重打一次）；"
                  "② 有人绕过 scripts/build_sidecar.py 直接 pyinstaller —— spec 里那道硬闸该拦住的")
            return False
        print(f"  这一包自报 git_sha={reported}（打包时工作树 dirty={built_dirty}）")
        if reported != head[: len(reported)]:
            print(f"  ✗ 包里的后端来自 {reported}，而仓库 HEAD 是 {head[: len(reported)]}"
                  " —— 攒着没重打的那一笔就在这里")
            return False
        if built_dirty:
            print("  ✗ 指纹对上了，但打包那一刻工作树是**脏**的："
                  "「装的就是 HEAD」这句话不成立，先把改动提交再重打")
            return False
        print(f"  ✓ 身份一致：这一包就是 {head[:12]}")

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
        # 这一格从"判据"降级成"读数"（10-01）：`dangling` 是 `R28-19` 加的，第十五包里就有，
        # 它证明不了"最新"，只能证明"frozen 那条码真的被执行了"（收进 PYZ ≠ frozen 可用）。
        has_dangling = "有" if "dangling" in keys else "没有"
        print(f"  或孤儿盘点的键集合 = {keys}（含 dangling：{has_dangling}）")
        verdict = "dangling" in keys
    finally:
        gone = _terminate_own(srv, port)
    # 自己起的进程没收干净 ⇒ 这一发不算完成：留下的那个 exe 占着端口与临时数据根，
    # 下一发探针或下一次装机读到的就是它（10-01 与 install_package.ps1 第 [3/5] 步对齐）。
    return verdict and gone


def main() -> int:
    layers = [
        ("① 前端层：包内 dist == 仓库 dist", check_frontend),
        ("② 后端层：包内 exe == 刚构建那份（缺尺子即红，不算跳过）", check_backend_binary),
        ("③ frozen 层：起它、问它是哪个 commit、再看界面与读数", check_frozen_boot),
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
