"""`frontend/dist` 入库同步检查：**本机全量门禁与 CI 共用这一份实现**（`R28-31`）。

为什么要有这个文件：判据此前**有两份**，而且 CI 那份更弱 —— `ci.yml` 用
`git diff --quiet -- frontend/dist`，而 `gate.py` 用 `git status --porcelain`（含未跟踪文件）。
Vite 每次构建都给 chunk 改名，于是"只净增一个新命名的产物、旧的没被 diff 看见"这种形状
真的会出现：CI 空过、本机红，两边说两套话。一条检查在两个地方各写一遍，迟早漂开——
和这个仓库里所有"两份实现"的下场一样。所以现在只有一份，在这儿。

判据：`npm run build` 刚跑完，工作树里的 `frontend/dist` 却与仓库不一致 ⇒ 入库的那份是旧的。
dist 是**有意入库**的第二份事实（clone 后不装 node 也能起控制台、随包后端直接托管它），
"入库"就意味着"必须与源码同一次提交"。这条被跳过时的症状不是报错，
而是 clone 与安装包静静带着旧界面（09-26 那批六轮 UI 整改之后就是这么漏的）。

只在**构建之后**判：没重建过时"没差异"只说明没人动过文件，判断不了陈旧 —— 因此
`gate.py --ci` 档跳过它（那个 job 不装 node），它跑在两处：本机 full 档、CI 的 frontend job。

拿不到 git、或 git 报错时**返回"没漂移"**（不拦）：这条的价值是"确认漂移过"，
一次误报就会让人开始忽略它的红（与 `check_consistency` 那族"未知不拦"同一条判据）。

用法：

    .venv\\Scripts\\python.exe scripts/check_dist_sync.py      # 退出码非 0 = 入库的 dist 是旧的
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_PATH = "frontend/dist"


def drifted_entries() -> list[str]:
    """`frontend/dist` 相对仓库的改动行（含未跟踪）；拿不到 git 时返回空。"""
    try:
        probe = subprocess.run(
            ["git", "status", "--porcelain", "--", DIST_PATH],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if probe.returncode != 0:
            return []
    except Exception:  # noqa: BLE001 - 没有 git / 环境异常时不拦，见模块 docstring
        return []
    return [line for line in probe.stdout.splitlines() if line.strip()]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    drift = drifted_entries()
    if not drift:
        print("✅ dist 入库同步：刚构建的 frontend/dist 与仓库一致")
        return 0
    print(
        f"❌ 重建之后 `frontend/dist` 仍有 {len(drift)} 处与仓库不一致"
        " ⇒ **入库的构建产物是旧的**：clone 出来的界面、随包后端托管的那份 dist 都不是当前代码。",
    )
    print("   修法：把刚构建出来的产物一起提交（`git add frontend/dist`）；")
    print("   或者明确决定 dist 不入库 —— 那是改这条检查与 README/镜像口径的一次产品决定。")
    for line in drift[:8]:
        print(f"     {line}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
