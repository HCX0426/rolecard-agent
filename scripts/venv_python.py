"""本平台 venv 解释器的**唯一**解析实现（2026-10-30 壳与脚本审计 #7：此前 gate.py 与
两个探针各写一份逐字等价的候选列表，后两份 docstring 还互相宣称「与 gate.py 同一
判据」——判据三处维护，改一处漏两处正是本仓反复抓的漂移族）。

为什么不能写死 Windows 那条路径：CI 的 Linux runner 上没有 `.venv/Scripts/python.exe`，
每一步都会以"文件不存在"失败（`bin/python` 才是那边的形状），而那副模样看起来像
"门禁跑了、全红"。同理，runner 上通常压根没有 `.venv` 目录 —— 依赖是直接装进系统
python 的，这时用 `sys.executable` 才是对的，而不是硬凑一个不存在的虚拟环境。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def venv_python() -> str:
    """按平台找虚拟环境里那个解释器，找不到就退回**调用方自己用的那个**。"""
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",  # Windows
        ROOT / ".venv" / "bin" / "python",  # Linux / macOS
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable
