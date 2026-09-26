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

UNKNOWN_SOURCE = "（这一列上线前落的，不知道）"


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


def summarize(conn: Any) -> dict[str, Any]:
    """算出那几个口径。**为什么要抽出来**：① 与 ④ 说的是同一批行，"接了话的由头"必须
    跟着"接了话"的口径走。第一版把这两处各写一遍，于是 ① 报 1 条而 ④ 数出 9 条 ——
    一个 print-only 的脚本没法被测到这种自相矛盾，抽成函数之后一条用例就够。
    """
    rows = conn.execute(
        "SELECT id, role_id, state, created_at, read_at, dismissed_at, fired_by "
        "FROM agent_reachout ORDER BY id"
    ).fetchall()
    streak = worst = 0
    picked = ignored = dismissed = unseen = 0
    by_source: dict[str, int] = {}
    picked_sources: dict[str, int] = {}
    for r in rows:
        stamp = _parse(r["created_at"]) or datetime.min
        replied = _user_replies(conn, str(r["role_id"]), stamp) > 0
        # NULL 单列一档：**不知道**不等于"是某个源"。把老行摊进任何一档，就是替它们编一个由头。
        key = str(r["fired_by"]) if r["fired_by"] else UNKNOWN_SOURCE
        by_source[key] = by_source.get(key, 0) + 1
        # 结局四选一，判完就定；④ 那句"接了话的由头"从这里长出来，不再另算一遍。
        if r["state"] == "dismissed":
            dismissed += 1
        elif r["read_at"] is None:
            unseen += 1
        elif replied:
            picked += 1
            picked_sources[key] = picked_sources.get(key, 0) + 1
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
    return {
        "total": len(rows),
        "picked": picked,
        "ignored": ignored,
        "unseen": unseen,
        "dismissed": dismissed,
        "worst_streak": worst,
        "tail_streak": streak,
        "by_source": by_source,
        "picked_sources": picked_sources,
    }


def _spread(counts: dict[str, int]) -> str:
    return " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) or "—"


def main() -> None:
    src = scratch_db.resolve_live_db()
    work = scratch_db.copy_of_live_db(ROOT / "build" / "scratch-outcomes.db", src)
    conn = connect(work)
    # 真库那份可能还没跑过新代码：`read_at` / `dismissed_at` / `fired_by` 都靠启动时的
    # `reconcile_columns` 补出来 —— 数据一行不改，只是把声明的列补齐。真库从头到尾只读。
    bootstrap(conn, enabled_domains=("health", "finance"))
    print(f"源库（只读）：{src}")
    print(f"副本（补过列之后读它）：{work}\n")
    s = summarize(conn)
    total = int(s["total"])
    print(f"主动消息 {total} 条（含已划掉的）\n")
    print("① 接话率（她冒话后用户在**同一条主动会话**里回了）")
    print(f"   {s['picked']} / {total} = {s['picked'] / total:.0%}  ← 会低估：人更可能在主会话回话")
    print("② 自说自话连击数（连续多少条冒了话没有任何后续）")
    print(f"   最长 {s['worst_streak']} 连击；当前链尾 {s['tail_streak']}")
    print("③ 结局分布")
    print(
        f"   接了 {s['picked']} · 看了没接 {s['ignored']} · "
        f"没看 {s['unseen']} · 划掉 {s['dismissed']}"
    )
    print("④ 由头分布（`fired_by` 那一列上线之后才有的读法）")
    print(f"   {_spread(s['by_source'])}")
    print(f"   接了话的那几条由头 = {_spread(s['picked_sources'])}")
    print(
        "   ↑ 这一行就是 `R26-09` 的回访判据：「未收尾话题」到底有没有**真的驱动过**"
        "\n     一次被接住的开口。在 `fired_by` 落库之前，这个数只活在 tracer 里，"
        "\n     而桌宠日志每次启动被覆盖 —— 所以上线前那批行永远归不到由头上，"
        "\n     只能从这一列之后重新开始攒。"
    )
    print(
        "\n读法：四个数都还只是**条数口径**，样本 <20 时不要拿去做任何档位取舍 ——"
        "\n     它们的作用是把『完全量不出来』变成『有数但噪声大』。"
    )
    conn.close()


if __name__ == "__main__":
    main()
