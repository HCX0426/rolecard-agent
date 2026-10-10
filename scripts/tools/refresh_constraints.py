"""按 **Linux 侧实测 resolve** 刷新 `config/constraints-linux.txt`（ENGI-18 第三路的刷新器）。

跑法（**必须在 Linux 上跑**，CI 的 ubuntu job 用；在 Windows 上跑是错平台的读数，退 2）：

    python scripts/tools/refresh_constraints.py            # 判 + 写（CI 用）
    python scripts/tools/refresh_constraints.py --check    # 只问变没变（不写盘）

退出码与 `recompile_locks.py` / `audit_deps` 同族 fail-closed：

    0 = 约束没变（什么都不写，PR 不该开）
    1 = 约束变了（已写回工作树，新旧版本打在 stdout）
    2 = 跑不了（错平台 / 锁里读不出 uvicorn pin / pip --report 失败 / 报告里没有 uvloop）
        —— **绝不退成 0**：「刷不了」报成「没变化」，那一周就安静地没更新，而没人知道。

判据（两条来自实测，不是推测）：

  1. **必须 Linux**：uvloop 的声明带 `sys_platform != "win32"` 门，在 Windows 上跑
     `pip --dry-run` 永远不会解析出 uvloop —— 会把「没有 uvloop」当成"上游改了声明"
     写回文件，静默删掉整个约束。错平台直接退 2，不产出读数。
  2. **resolve 的输入是锁里的 uvicorn pin**（不是 `uvicorn[standard]` 最新版）：约束必须钉住
     「这把锁装出来的那个 uvicorn」要的 uvloop；uvicorn 升级由周更锁刷新推动，这里跟着锁走，
     不自己追新 —— 否则两处各有一份"uvicorn 是哪个版本"的事实面。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = ROOT / "config/constraints-linux.txt"
LOCK = ROOT / "requirements/requirements.lock"

#: 锁里 `uvicorn[standard]==X` 那一行（pin 行永不缩进；extras 在名字后面）。
_UVICORN = re.compile(r"^uvicorn\[standard\]==(\S+)", re.M)
#: 约束文件里的 uvloop pin 行。
_UVLOOP = re.compile(r"^uvloop==(\S+)$", re.M)

_HEADER = """\
# Linux 装配面的约束（ENGI-18 第三路，2026-10-09 拍板：install 侧，不碰锁生成 / P1-12 口径）。
#
# 为什么存在：两把锁都在 Windows 上由 pip-compile 解析（P1-12 拍板的「Windows-first」），
# 而 `uvicorn[standard]` 对 uvloop 的声明带 `sys_platform != "win32"` 门 —— Windows 解析时
# 整条把它剥掉。Linux 侧 `pip install -r 锁` 因此拿到的是**没锁版本**的 uvloop；这一行钉住那一半。
#
# 由 `scripts/tools/refresh_constraints.py` 在 **Linux** 上按锁里的 uvicorn pin 实测 resolve
# （`pip install --dry-run --report`），周更 `lock-refresh.yml` 刷新、PR 先验证后开。
# 本文件不是 pip-compile 的产物：手改它而不动 uvicorn pin，周更会把它刷回来。
"""


def resolve_uvloop(uvicorn_ver: str, timeout: int = 300) -> str:
    """在本机（Linux）实测 `uvicorn[standard]==<ver>` 会带出哪个 uvloop。拿不到就抛。"""
    with tempfile.TemporaryDirectory(prefix="resolve-uvloop-") as td:
        report = Path(td) / "report.json"
        proc = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--dry-run", "--ignore-installed", "--quiet",
                "--report", str(report),
                f"uvicorn[standard]=={uvicorn_ver}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            # 与 recompile_locks 同一条纪律：本机 pip 配置（镜像源）不许进读数。
            env={**os.environ, "PIP_CONFIG_FILE": os.devnull},
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pip --dry-run 退 {proc.returncode}：{proc.stderr.strip()[:200]}"
            )
        data = json.loads(report.read_text(encoding="utf-8"))
    for item in data.get("data", []):
        name = item.get("metadata", {}).get("name", "")
        if name.replace("_", "-").lower() == "uvloop":
            return str(item["metadata"]["version"])
    # 报告里没有 uvloop：上游改了声明、或解析形状变了 —— 这是"要人看"的形状，不是"没变化"。
    raise RuntimeError("解析报告里没有 uvloop —— 上游声明或解析形状变了，先看再刷")


def render(uvicorn_ver: str, uvloop_ver: str) -> str:
    return _HEADER + f"uvloop=={uvloop_ver}\n"


def main(argv: list[str] | None = None, *, platform: str | None = None) -> int:
    """`platform` 可注入（默认 `os.name`）—— 让"错平台退 2"这条判据能在任何机器上测。"""
    ap = argparse.ArgumentParser(description="刷新 config/constraints-linux.txt")
    ap.add_argument("--check", action="store_true", help="只问变没变，不写盘")
    args = ap.parse_args(argv)
    plat = platform if platform is not None else os.name

    if plat == "nt":
        print(
            "refresh_constraints: 必须在 Linux 上跑（Windows 解析不出带 sys_platform 门的 "
            "uvloop，会把『没有』写成『上游改了』）—— 退 2",
            file=sys.stderr,
        )
        return 2
    if not LOCK.exists():
        print("refresh_constraints: requirements/requirements.lock 不见了 —— 退 2", file=sys.stderr)
        return 2
    m = _UVICORN.search(LOCK.read_text(encoding="utf-8", errors="ignore"))
    if not m:
        print("refresh_constraints: 锁里读不出 uvicorn[standard] pin —— 退 2", file=sys.stderr)
        return 2
    uvicorn_ver = m.group(1)
    try:
        uvloop_ver = resolve_uvloop(uvicorn_ver)
    except Exception as exc:  # noqa: BLE001 — 任何解析失败都是"刷不了"，同一族退 2
        print(f"refresh_constraints: {exc} —— 退 2", file=sys.stderr)
        return 2

    # 判"变没变"只看 **uvloop 那条结构行**，不按字节比整文件 —— 与 recompile_locks 同一条
    # 纪律（注释横幅不是判断内容）：手改注释不触发刷写，版本没动也不产生空 PR。
    old_text = CONSTRAINTS.read_text(encoding="utf-8") if CONSTRAINTS.exists() else ""
    old_v = _UVLOOP.search(old_text)
    if old_v and old_v.group(1) == uvloop_ver:
        print(f"config/constraints-linux.txt 没变（uvloop=={uvloop_ver}，uvicorn=={uvicorn_ver}）")
        return 0
    if args.check:
        print(f"config/constraints-linux.txt 有变化：uvloop → {uvloop_ver}（--check 不写盘）")
        return 1
    CONSTRAINTS.write_text(render(uvicorn_ver, uvloop_ver), encoding="utf-8", newline="\n")
    print(
        f"config/constraints-linux.txt 已更新：uvloop "
        f"{old_v.group(1) if old_v else '（无此行）'} → {uvloop_ver}"
        f"（按锁里 uvicorn=={uvicorn_ver} 在 Linux 上实测 resolve）"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
