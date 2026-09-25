"""主动开口的"结局"度量（09-26 轮 R26-14 / S-2 的正题）。

这一源的成败在"她冒出来的那句你想不想回"，而从前**一种结局都量不出来**：删除是真删行、
`read-all` 可以一刷一大片所以 read≠看过、消息时间也没人读。现在三列时刻都在库里
（`created_at` / `read_at` / `dismissed_at`），本脚本只读它们，产出三个口径：

1. **接话率** —— 她冒话之后，用户有没有回**同一条主动会话**。（会**低估**：人更可能在
   主会话里回话，所以必须和 3 一起看。）
2. **自说自话连击数** —— 连续多少条她冒了话而没有任何后续。调"该不该现在开口"的直读指标。
3. **看了不接** —— 有 `read_at` 却没有用户消息：打开了、扫一眼、没回。

读的是**真库副本**：真库那份还没跑过新代码（`read_at`/`dismissed_at` 两列要靠启动时的
`reconcile_columns` 补出来），所以这里先 `copy_of_live_db` 再 `bootstrap` 一次 —— 数据
一行不改，只是把声明的列补齐。真库从头到尾只读。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/reachout_outcomes.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import scratch_db  # noqa: E402

from rolecard_agent.core.reachout import proactive_thread_id  # noqa: E402
from rolecard_agent.storage.db import bootstrap, connect  # noqa: E402


def _parse(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _user_replies(conn: Any, role_id: str, after: datetime) -> int:
    """该角色主动会话里，`after` 之后用户说过几句（读检查点里的消息表）。

    没有一条消息表可查（checkpoint 是序列化的），所以这里退而用 `session_thread` 的
    更新时间与 `agent_reachout` 的时刻对：**能定序、不能定内容**。这是刻意的下限口径 ——
    宁可报"看不出有人回"，也不把"她自己在同一线程里补的话"算成用户回话。
    """
    row = conn.execute(
        "SELECT updated_at FROM session_thread WHERE thread_id = ?",
        (proactive_thread_id(role_id),),
    ).fetchone()
    touched = _parse(row["updated_at"]) if row is not None else None
    return 1 if touched is not None and touched > after else 0


def main() -> None:
    src = scratch_db.resolve_live_db()
    work = scratch_db.copy_of_live_db(ROOT / "build" / "scratch-outcomes.db", src)
    conn = connect(work)
    bootstrap(conn, enabled_domains=("health", "finance"))
    print(f"源库（只读）：{src}")
    print(f"副本（补过列之后读它）：{work}\n")
    rows = conn.execute(
        "SELECT id, role_id, state, created_at, read_at, dismissed_at "
        "FROM agent_reachout ORDER BY id"
    ).fetchall()
    print(f"主动消息 {len(rows)} 条（含已划掉的）\n")

    streak = worst = 0
    picked = ignored = dismissed = unseen = 0
    for r in rows:
        stamp = _parse(r["created_at"]) or datetime.min
        replied = _user_replies(conn, str(r["role_id"]), stamp) > 0
        if r["state"] == "dismissed":
            dismissed += 1
        elif r["read_at"] is None:
            unseen += 1
        elif replied:
            picked += 1
        else:
            ignored += 1
        # 连击的判据是"**有没有后续**"，不是"看没看"：她连冒三条而用户一条都没回，
        # 那就是三连击 —— 哪怕三条都还没被读到。第一版把"没看"当成断链，于是
        # ③ 报"5 条没看"而 ② 报"0 连击"，两个数互相打脸。
        if replied:
            streak = 0
        else:
            streak += 1
            worst = max(worst, streak)

    total = len(rows)
    print("① 接话率（她冒话后用户在**同一条主动会话**里回了）")
    print(f"   {picked} / {total} = {picked / total:.0%}  ← 会低估：人更可能在主会话回话")
    print("② 自说自话连击数（连续多少条冒了话没有任何后续）")
    print(f"   最长 {worst} 连击；当前链尾 {streak}")
    print("③ 结局分布")
    print(f"   接了 {picked} · 看了没接 {ignored} · 没看 {unseen} · 划掉 {dismissed}")
    print(
        "\n读法：三个数都还只是**条数口径**，样本 <20 时不要拿去做任何档位取舍 ——"
        "\n     它们的作用是把『完全量不出来』变成『有数但噪声大』。"
    )
    conn.close()


if __name__ == "__main__":
    main()
