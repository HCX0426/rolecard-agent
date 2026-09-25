"""把真库复制成一份可写副本 —— 实验脚本专用（审计 §12.9）。

为什么所有"我自己演用户"的脚本都必须走副本：那几句"用户说的话"是编的。写进真库就是
给她植入假事实，而 persona 那条不变式（只解读真实观测、不捏造）最先烂掉的地方就是这种
"我只是测一下"的写库。

`sqlite3.backup()` 而不是 `shutil.copy`：真库通常是开着的（WAL），直接拷文件会拷出一份
主库与 WAL 不同步的半成品 —— 表现为"副本里少了最近几条"，而那种数据缺陷会让人以为是
被测代码的问题。

## 为什么"选哪份当源"要在这里判定，而不是留给调用方

本机有**两个数据根**（`core/paths.user_data_root()`：开发态 = 仓库 `data/`，打包态 =
`%LOCALAPPDATA%\\rolecard-agent`）。2026-09-24 把日常数据搬进了安装目录，而仓库那份**被复制
而非移走** —— 于是它成了一份"看着完全合理、但停在 09-23"的快照：会话数与真库一样是 24 条，
主动消息却少一条，最后更新差两天。

那一版留的默认值是它。后果不是"某次实验用了旧数据"，是**所有走默认值的实验都静默地用了
两天前的世界**（采样扫描、未收尾话题那两组读数都在里面），而没有任何一条输出会提到这件事。
判据换成"谁新用谁 + 把选了谁打在 stderr 上"：**沉默是这类 bug 的唯一栖息地**，所以宁可吵。

显式覆盖仍然优先：`LIVE_DB_PATH` 或 `copy_of_live_db(source=...)` 指哪儿是哪儿。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
#: 两个候选源（可能都不存在，也可能都存在但内容不同 —— 那正是这里的存在理由）。
CANDIDATE_SOURCES: tuple[Path, ...] = (
    _REPO_ROOT / "data" / "sqlite" / "app.db",
    Path.home() / "AppData" / "Local" / "rolecard-agent" / "sqlite" / "app.db",
)


#: 判"这份库有没有内容、什么时候有内容"要看的所有表。
#: **不能只看 session_thread**：今天有一轮实验里快照库的 reachout 比 session 晚 26 小时，
#: 只看会话表会把活的那一侧读成旧的。
_FRESHNESS_QUERIES: tuple[str, ...] = (
    "SELECT MAX(updated_at) FROM session_thread",
    "SELECT MAX(created_at) FROM agent_reachout",
)

#: 取证副本的家（09-26 轮 `R26-18` 的尾巴）。
#:
#: 以前 `persona_ab` / `persona_chat_sim` 的 `--copy` 默认落在 `data/sqlite/` 里
#: （`_persona_ab.db`、`_chat_sim.db`），而那个目录同时住着"开发态真库"与"09-24 隔离出来的
#: 陈旧快照" —— 三个长得都像库的文件放一起，认错一次就是一整轮错档读数（09-25 真发生过）。
#: 副本一律改落 `build/scratch/`：整目录 gitignore，而且**不在"能连上的库"的视野里**。
SCRATCH_DIR = _REPO_ROOT / "build" / "scratch"


def _freshness(path: Path) -> datetime | None:
    """这份库"最后一次有内容"的时刻。**空库一律读成 None**（= 不参与比较，必输）。

    为什么不能用文件 mtime 兜底（这是我今天自己写出来的一个回归）：第一版在表为空时
    回落 `path.stat().st_mtime`，于是"刚被某个开发态启动顺手建出来的空库"凭着一个新 mtime
    **压过了 52 MB 的真库**被选为源 —— 空库永远不该赢，赢了的后果是整轮实验采了一个空世界。
    """
    best: datetime | None = None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        for sql in _FRESHNESS_QUERIES:
            try:
                row = conn.execute(sql).fetchone()
            except sqlite3.Error:
                continue  # 表还没有（很老的库/新库）—— 跳过这一路，别当成 0
            if not row or not row[0]:
                continue
            try:
                stamp = datetime.fromisoformat(str(row[0]))
            except ValueError:
                continue  # 各家后端时间格式不完全一致，读不懂就忽略这一路
            if best is None or stamp > best:
                best = stamp
    finally:
        conn.close()
    return best


def resolve_live_db() -> Path:
    """这一轮实验该从哪份库取数。显式 `LIVE_DB_PATH` > 两根里较新那份。

    选完**一定**往 stderr 打一行：这条输出的全部价值就是让"用了哪份世界"不可忽略。
    只有一份存在时不必吵（没有歧义），但两存且较新的那份不是默认旧根时要说清 —— 那正是
    09-24 之后每一个实验的处境。
    """
    env_src = os.environ.get("LIVE_DB_PATH", "").strip()
    if env_src:
        chosen = Path(env_src)
        if not chosen.exists():
            raise SystemExit(f"LIVE_DB_PATH 指向的库不存在：{chosen}")
        print(f"[scratch_db] 源库 = {chosen}（LIVE_DB_PATH 显式指定）", file=sys.stderr)
        return chosen

    existing = [p for p in CANDIDATE_SOURCES if p.exists()]
    if not existing:
        raise SystemExit(
            "找不到任何一份源库。两个候选：" + "、".join(str(p) for p in CANDIDATE_SOURCES)
        )
    if len(existing) == 1:
        return existing[0]

    ranked = sorted(existing, key=lambda p: _freshness(p) or datetime.min, reverse=True)
    chosen, runner_up = ranked[0], ranked[1]
    print(
        f"[scratch_db] 源库 = {chosen}\n"
        f"[scratch_db]          最后更新 {_freshness(chosen) or '未知'}；"
        f"另一份 {runner_up}（{_freshness(runner_up) or '未知'}）**更旧**，已弃用。"
        f"要用它请设 LIVE_DB_PATH。",
        file=sys.stderr,
    )
    return chosen


def copy_of_live_db(dest: Path, source: Path | None = None) -> Path:
    """把真实库在线复制一份到 `dest`（覆盖），返回 dest。

    `source=None` 时走 `resolve_live_db()`（显式 env > 较新的那个数据根）—— 见模块 docstring
    里那段"为什么这里要判定"。
    """
    src_path = source or resolve_live_db()
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
