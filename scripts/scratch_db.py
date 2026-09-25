"""把真库复制成一份可写副本 —— 实验脚本专用（审计 §12.9）。

为什么所有"我自己演用户"的脚本都必须走副本：那几句"用户说的话"是编的。写进真库就是
给她植入假事实，而 persona 那条不变式（只解读真实观测、不捏造）最先烂掉的地方就是这种
"我只是测一下"的写库。

`sqlite3.backup()` 而不是 `shutil.copy`：真库通常是开着的（WAL），直接拷文件会拷出一份
主库与 WAL 不同步的半成品 —— 表现为"副本里少了最近几条"，而那种数据缺陷会让人以为是
被测代码的问题。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def copy_of_live_db(dest: Path, source: Path | None = None) -> Path:
    """把真实库在线复制一份到 `dest`（覆盖），返回 dest。

    源库默认是**开发态那一份**（`repo/data/sqlite/app.db`）。本机日常数据在 2026-09-24
    搬进了安装包目录（`%LOCALAPPDATA%\\rolecard-agent\\sqlite\\app.db`），所以要量"她真实的
    记忆/角色卡"时得指过去 —— 用 `LIVE_DB_PATH` 指，不改默认：默认那份对"跑通链路"仍然够用，
    而把默认改成安装目录会让所有旧脚本突然测到用户的真数据。
    """
    env_src = os.environ.get("LIVE_DB_PATH", "").strip()
    src_path = source or (
        Path(env_src)
        if env_src
        else Path(__file__).resolve().parents[1] / "data" / "sqlite" / "app.db"
    )
    if not src_path.exists():
        raise SystemExit(f"源库不存在：{src_path}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dest
