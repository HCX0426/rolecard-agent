"""把**装着的那一份**应用的数据根整体备份成一个 zip（装包之前先做这一步）。

**机制本体在 `src/rolecard_agent/features/data_backup.py`**（R1031-15，2026-10-31）：
备份判据只许有一份事实面 —— 应用内的「全量备份」端点与这支 CLI 共用同一个实现，
这里只剩 argparse 的壳与退出码。为什么（快照先后 `R102-75`、不回落仓库 `data/`、
zip 含 API key 的敏感物警示 R1031-23）都写在 core 的模块 docstring 里，那一处。

用法（PowerShell）：

    # 备份
    .venv\\Scripts\\python.exe scripts\\tools\\backup_data_root.py --dest build
    # 还原（R1031-15 的另一半：备份是可消费的）—— 目标必须是空目录/不存在
    .venv\\Scripts\\python.exe scripts\\tools\\backup_data_root.py \\
        --restore build/backup-liveroot-xxx.zip --into D:\\rc-new

幂等：备份每次落一个新时间戳的 zip（不覆盖旧备份）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rolecard_agent.features.data_backup import (  # noqa: E402
    DataBackupError,
    backup,
    restore,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="备份/还原装着的那一份应用的数据根")
    ap.add_argument("--dest", default="build", help="（备份档）zip 落在哪（默认仓库的 build/）")
    ap.add_argument(
        "--root",
        default=None,
        metavar="DIR",
        help="（备份档）显式指定数据根；省略=自动找装着的那一份（拒绝猜）",
    )
    ap.add_argument(
        "--restore",
        default=None,
        metavar="ZIP",
        help="（还原档）把这个备份 zip 还原进 --into 指定的**空**目录",
    )
    ap.add_argument(
        "--into", default=None, metavar="DIR", help="（还原档）还原到哪；必须是不存在或为空的目录"
    )
    args = ap.parse_args()
    try:
        if args.restore:
            if not args.into:
                raise DataBackupError("--restore 需要成对的 --into（还原到哪个空目录）")
            if args.dest != "build" or args.root:
                raise DataBackupError("--restore 只认 --zip 与 --into，别混用备份档的旗")
            restore(Path(args.restore), Path(args.into))
        else:
            if args.into:
                raise DataBackupError("--into 只跟 --restore 成对使用")
            root = Path(args.root) if args.root else None
            backup(Path(args.dest), root=root)
    except DataBackupError as exc:
        # core 抛这个而不是 SystemExit（那是 CLI 的退出方式）；这里的形状保持不变
        # （install_package.ps1 第②道判 exit 非零 + stderr 可读）。
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
