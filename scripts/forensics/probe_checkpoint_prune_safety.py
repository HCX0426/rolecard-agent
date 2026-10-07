#!/usr/bin/env python
"""检查点收口的安全对减：同一份真库副本，**修剪开 / 修剪关**各读一遍历史。

用法（只读真库，写全部落在临时副本上）：
    python scripts/probe_checkpoint_prune_safety.py

为什么需要它：`R26-07` 那次改动删的是 `checkpoints` 表里的行，而**会话历史就存在这张表里**。
集成用例（`tests/integration/test_checkpoint_prune.py`）用的是三问三答的合成会话；真库里
有 50 行的长线程、有主动会话、有 agent 模式留下的子图 ns。"删完还能读出同样的历史"这句话，
只有在真库副本上、走产品自己的读路径（`GET /api/session/{tid}/messages`）、按消息 id 序列
逐条对减才算被说过。

判据是**逐线程的 id 序列相等**，不是行数、不是字节数（行数一样也可能是把对的那几条删了、
把错的那几条留了）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 与 `pyproject.toml` 的 `[tool.pytest.ini_options] pythonpath = ["src"]` 同一个理由：
# 这个包不是装进 venv 用的，是仓库根直接跑的。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import scratch_db  # noqa: E402

os.environ.setdefault("MODEL_PIN_ON_STARTUP", "0")  # 探针不跑推理，也别把 8B 钉进显存
os.environ.setdefault("MEMORY_EXTRACT_AUTO", "0")  # 更不许顺手改她的记忆

from fastapi.testclient import TestClient  # noqa: E402

from rolecard_agent.api.main import create_app  # noqa: E402
from rolecard_agent.core.storage import checkpointer as ck  # noqa: E402


def _read_history(db: Path) -> dict[str, list[tuple[Any, ...]]]:
    """用产品自己的读路径把每条线程的历史读成一份可对比的形状。"""
    app = create_app(sqlite_path=db)
    with sqlite3.connect(db) as conn, TestClient(app) as client:
        threads = [
            str(r[0]) for r in conn.execute("SELECT thread_id FROM session_thread ORDER BY 1")
        ]
        out: dict[str, list[tuple[Any, ...]]] = {}
        for tid in threads:
            res = client.get(f"/api/session/{tid}/messages?limit=500")
            if res.status_code != 200:
                out[tid] = [("HTTP", res.status_code, "")]
                continue
            out[tid] = [
                (m.get("id"), m.get("role"), str(m.get("content", ""))[:400])
                for m in res.json().get("messages", [])
            ]
    return out


def _stats(db: Path) -> dict[str, Any]:
    with sqlite3.connect(db) as conn:
        rows, threads = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT thread_id) FROM checkpoints"
        ).fetchone()
        payload = conn.execute(
            "SELECT COALESCE(SUM(length(checkpoint) + length(metadata)), 0) FROM checkpoints"
        ).fetchone()[0]
        writes = conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
    return {
        "rows": int(rows),
        "threads": int(threads),
        "payload_mb": round(int(payload) / 1e6, 2),
        "writes": int(writes),
        "file_mb": round(Path(db).stat().st_size / 1e6, 2),
    }


def main() -> int:
    live = scratch_db.resolve_live_db()
    print(f"源库（只读）：{live}")

    before_db = Path(tempfile.mkdtemp(prefix="ck_before_")) / "app.db"
    after_db = Path(tempfile.mkdtemp(prefix="ck_after_")) / "app.db"
    scratch_db.copy_of_live_db(before_db)
    scratch_db.copy_of_live_db(after_db)

    # 「修剪开」这一侧走**未打补丁**的真实装配路径：补时钟 → 一次性收口 → 常态修剪。
    after_before_stats = _stats(after_db)
    history_after = _read_history(after_db)
    after_stats = _stats(after_db)

    # 「修剪关」= 把这次新增的两个动作替成空操作，其余装配一字不动。
    real_compact, real_prune = ck.compact_backlog_once, ck.prune_checkpoints
    ck.compact_backlog_once = lambda conn: 0
    ck.prune_checkpoints = lambda conn, **kw: 0
    try:
        history_before = _read_history(before_db)
    finally:
        ck.compact_backlog_once = real_compact
        ck.prune_checkpoints = real_prune

    print(f"修剪前：{after_before_stats}")
    print(f"修剪后：{after_stats}")

    bad = [
        tid
        for tid in set(history_before) | set(history_after)
        if history_before.get(tid) != history_after.get(tid)
    ]
    print(f"线程数：{len(history_before)}；读出来不一样的：{len(bad)}")
    for tid in bad[:5]:
        a, b = history_before.get(tid, []), history_after.get(tid, [])
        print(f"  {tid}: 修剪前 {len(a)} 条 / 修剪后 {len(b)} 条")
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            if x != y:
                print(f"    第 {i} 条起分叉：{x!r} != {y!r}")
                break
    if bad:
        print("RESULT: FAIL —— 收口动到了能读出来的历史")
        return 1
    print("RESULT: PASS —— 每条线程的历史逐条相同，删掉的只有没人再读的祖先快照")
    return 0


if __name__ == "__main__":
    sys.exit(main())
