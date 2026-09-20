"""项目内本地路径发现（core 层，向下无依赖）。

此前 `default_ocr_python` 定义在 `rag/parser.py`，导致 `core/services.py` 反向 import `rag`
（core→rag 跨层耦合，见审查 M10）。OCR 解释器只是"项目根的独立 venv"路径问题，与 rag 无关，
故下沉到 core 层；`rag` 仍可 import core（向下依赖合规）。

里程碑 D②-4（后端打进桌面安装包）把另外两样也放进来：`bundle_root()`（**随包资源**在哪）与
`user_data_root()`（**用户数据**在哪）。分开的理由是硬的：安装目录可能根本不可写
（Program Files），而且升级是整目录替换 —— 数据库与知识库放进去等于"更新一次丢一次"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_NAME = "rolecard-agent"
IS_WINDOWS = sys.platform.startswith("win")


def is_frozen() -> bool:
    """是否跑在打包出来的可执行文件里（PyInstaller onedir 会同时设这两个）。"""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def repo_root() -> Path:
    """仓库根（开发态）：本文件在 `<root>/src/rolecard_agent/core/paths.py`。"""
    return Path(__file__).resolve().parents[3]


def bundle_root() -> Path:
    """随包只读资源的落点：冻结态是 `sys._MEIPASS`（onedir 下即 `_internal/`），开发态是仓库根。

    前端构建产物（`frontend/dist`）这类"打进包里但不该被改"的东西都从这里找。
    """
    if is_frozen():
        return Path(str(sys._MEIPASS))  # type: ignore[attr-defined]  # PyInstaller 注入，typeshed 不认识
    return repo_root()


def user_data_root() -> Path:
    """用户数据根，**永远可写**：sqlite / chroma / uploads 的默认父目录。

    开发态保持仓库 `data/`（现有约定与测试都指这儿，改它会让"跑一次测试"污染安装包目录）；
    冻结态换到系统数据目录（Windows 用 `%LOCALAPPDATA%`，其他平台 `~/.local/share`）。
    """
    if not is_frozen():
        return repo_root() / "data"
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / _APP_NAME
    return Path.home() / ".local" / "share" / _APP_NAME


def path_from_config(value: str | Path) -> Path:
    """配置（`.env` / 界面里的运行环境）里的路径 → 绝对路径。

    相对路径的基准**故意不是数据根**：`.env.example` 写的是 `./data/sqlite/app.db`，那是
    按仓库根成立的历史约定，换成按数据根解析会静默变成 `<repo>/data/data/...` —— 用户看
    着"路径没改过"却发现库空了。打包态没有仓库根，才退到数据根。
    """
    path = Path(value)
    if path.is_absolute():
        return path
    return (user_data_root() if is_frozen() else repo_root()) / path


def dotenv_path() -> Path:
    """`.env` 的落点：开发态在仓库根；冻结态在用户数据根旁边（安装目录不是配置位）。

    冻结态为什么不直接读 `user_data_root()/.env`：那个目录是 `…/rolecard-agent`，本身就是
    数据根，配置混进数据里会让"备份/清空数据"这类操作多一个坑。
    """
    if not is_frozen():
        return repo_root() / ".env"
    return user_data_root().parent / f"{_APP_NAME}.env"


def default_ocr_python() -> str | None:
    """默认 OCR 解释器：项目根下的独立 venv（requirements-ocr.txt 的安装约定）。

    打包态这里必然返回 None —— Paddle 按设计**不进包**（独立 venv、体积与 numpy 冲突），
    表现是"本地 OCR 不可用"，云端 OCR（配置了 key 时）不受影响。
    """
    cand = repo_root() / ".venv-ocr" / "Scripts" / "python.exe"
    return str(cand) if cand.exists() else None
