"""主动开口的"结局"度量（09-26 轮 R26-14 / S-2 的正题）。

这一源的成败在"她冒出来的那句你想不想回"，而从前**一种结局都量不出来**：删除是真删行、
`read-all` 可以一刷一大片所以 read≠看过、消息时间也没人读。现在三列时刻都在库里
（`created_at` / `read_at` / `seen_at` / `dismissed_at` / `fired_by`），本脚本只读它们，
产出五个口径：

1. **接话率（近似）** —— 她冒话之后，那条主动会话有没有**又动过一次**。这个分子两个方向都偏：
   只认单条点开的时刻时它**低估**（09-26 报过 1/11），而 `updated_at` 会被任何一轮甚至
   重命名那类 PATCH 推动时它**高估**（改完口径后是 9/11）。两个数都不是"他回了她那一句"。
2. **自说自话连击数** —— 连续多少条她冒了话而没有任何后续。调"该不该现在开口"的直读指标。
3. **看了不接** —— `state='read'`（"进了对话界面就算看过"那条口径）却没有后续。
4. **由头分布** —— 每一条是**被什么驱动的**（`fired_by`），以及接了话的那几条各由什么驱动。
   这一项是给 `R26-09` 的回访用的；该列 09-26 傍晚才加，之前的行只能算"不知道"。
5. **攒样本进度** —— 带由头的行有几条、观测窗口多长、离"够 20 条再判"还差多少。
   回访要等的就是条数，那就让它自己报进度；只有一条样本时**明说算不出速率**而不是外推。

读的是**真库副本**：真库那份还没跑过新代码（`read_at`/`dismissed_at` 两列要靠启动时的
`reconcile_columns` 补出来），所以这里先 `copy_of_live_db` 再 `bootstrap` 一次 —— 数据
一行不改，只是把声明的列补齐。真库从头到尾只读。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/reachout_outcomes.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import scratch_db  # noqa: E402

from rolecard_agent.config import Settings  # noqa: E402
from rolecard_agent.core.identity import resolve_instance_identity  # noqa: E402
from rolecard_agent.core.reachout import PROACTIVE_THREAD_PREFIX, proactive_thread_id  # noqa: E402
from rolecard_agent.storage.db import bootstrap, connect  # noqa: E402

# 读的是**这份库实际的主人在用的主动线程**（B2 之后线程 id 带身份）—— 与运行中的实例
# 一致即可：IDENTITY_USER_ID 设了就跟着它，没设就是默认那份。
OWNER = resolve_instance_identity(Settings.from_env())

UNKNOWN_SOURCE = "（这一列上线前落的，不知道）"


def _parse(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _proxy_replies(conn: Any, role_id: str, after: datetime) -> int:
    """**近似**口径（留着只为和新口径对照）：那条会话的 `updated_at` 晚于她这句吗？

    两个方向都偏：`updated_at` 被任何一轮推动，也被重命名/换模型那类 PATCH 推动 ——
    "动过这条线"不等于"回了她那一句"。真判据在 `read_lane_messages` / `_real_replies`。
    """
    row = conn.execute(
        "SELECT updated_at FROM session_thread WHERE thread_id = ?",
        (proactive_thread_id(role_id, user_id=OWNER),),
    ).fetchone()
    touched = _parse(row["updated_at"]) if row is not None else None
    return 1 if touched is not None and touched > after else 0


def read_lane_messages(conn: Any) -> dict[str, list[tuple[str, str]]]:
    """每条主动会话的 `(说话人, 原文)` 序列（按时间正序）—— **读检查点，不靠时刻猜**。

    走 `make_checkpointer` 而不是造一个 `Runtime`：只要 `get_tuple` 拿那一份
    `channel_values["messages"]`，不需要模型也不需要跑图。读不到（那条线没有检查点）
    就是空表 ⇒ 该行的"有人回"落在"看不出"那一档，不猜。
    """
    from langchain_core.messages import HumanMessage, ToolMessage  # noqa: PLC0415

    from rolecard_agent.core.checkpointer import make_checkpointer  # noqa: PLC0415
    from rolecard_agent.core.text import text_of  # noqa: PLC0415

    saver = make_checkpointer(conn)
    out: dict[str, list[tuple[str, str]]] = {}
    lanes = conn.execute(
        "SELECT thread_id FROM session_thread WHERE thread_id LIKE ?",
        (f"{PROACTIVE_THREAD_PREFIX}%",),
    ).fetchall()
    for lane in lanes:
        tid = str(lane["thread_id"])
        tup = saver.get_tuple({"configurable": {"thread_id": tid}})
        # `Checkpoint` 是 TypedDict，对它 `.get("channel_values")` 会被 mypy 按重载拒掉；
        # 拷成普通 dict 再取，顺带把"那条线没有检查点"摊成空字典。
        ckpt = dict(tup.checkpoint) if tup else {}
        values = cast("dict[str, Any]", ckpt.get("channel_values") or {})
        pairs: list[tuple[str, str]] = []
        for m in values.get("messages") or []:
            if isinstance(m, ToolMessage):
                continue  # 工具结果不是她出口说的话
            text = text_of(m).strip()
            if text:
                pairs.append(("用户" if isinstance(m, HumanMessage) else "你", text))
        out[tid] = pairs
    return out


def _real_replies(lane_msgs: dict[str, list[tuple[str, str]]], role_id: str, text: str) -> int:
    """真判据：**她那句之后**，那条线上有没有出现过一条用户消息。"""
    pairs = lane_msgs.get(proactive_thread_id(role_id, user_id=OWNER), [])
    at = next(
        (i for i in range(len(pairs) - 1, -1, -1) if pairs[i] == ("你", text.strip())),
        None,
    )
    if at is None:
        return 0  # 那句不在这条线里（投递失败的老消息 / 文本被改过）—— 不替它编
    return int(any(who == "用户" for who, _ in pairs[at + 1:]))


def summarize(
    conn: Any, lane_msgs: dict[str, list[tuple[str, str]]] | None = None
) -> dict[str, Any]:
    """算出那几个口径。**为什么要抽出来**：① 与 ④ 说的是同一批行，"接了话的由头"必须
    跟着"接了话"的口径走。第一版把这两处各写一遍，于是 ① 报 1 条而 ④ 数出 9 条 ——
    一个 print-only 的脚本没法被测到这种自相矛盾，抽成函数之后一条用例就够。

    `lane_msgs` 是给用例留的注入点：不传 = 自己去读检查点（真实那条路）。
    """
    if lane_msgs is None:
        lane_msgs = read_lane_messages(conn)
    rows = conn.execute(
        "SELECT id, role_id, state, text, created_at, read_at, seen_at, dismissed_at, fired_by "
        "FROM agent_reachout ORDER BY id"
    ).fetchall()
    streak = worst = 0
    picked = ignored = dismissed = unseen = 0
    picked_proxy = 0
    opened = batch_seen = legacy_seen = 0
    by_source: dict[str, int] = {}
    picked_sources: dict[str, int] = {}
    # 由头 × 结局交叉（09-28 拍的口径）：每个由头一行 `total/picked/ignored/unseen/dismissed`。
    # 有了它，"recall 档的开口 vs timer 档的开口，谁更被回"这种问题不用再跑一次脚本。
    cross: dict[str, dict[str, int]] = {}
    for r in rows:
        stamp = _parse(r["created_at"]) or datetime.min
        text = str(r["text"])
        # 两个口径并排算：真判据用来分桶，近似那个留着**对照**（差值本身就是"这条近似有多不可信"）。
        real = _real_replies(lane_msgs, str(r["role_id"]), text) > 0
        proxy = _proxy_replies(conn, str(r["role_id"]), stamp) > 0
        picked_proxy += proxy
        # NULL 单列一档：**不知道**不等于"是某个源"。把老行摊进任何一档，就是替它们编一个由头。
        key = str(r["fired_by"]) if r["fired_by"] else UNKNOWN_SOURCE
        by_source[key] = by_source.get(key, 0) + 1
        cell = cross.setdefault(
            key,
            {"total": 0, "picked": 0, "ignored": 0, "unseen": 0, "dismissed": 0},
        )
        cell["total"] += 1
        # "看过"的判据是 `state`，不是那两个时刻：用户 09-23 定的口径就是"点进对话界面就算
        # 都看过"，而批量那条路走的就是这个口径 —— 只认 `read_at` 会把**正在那条会话里跟
        # 她说话的人**数成"没看"（09-26 那组"没看 10/11"就是这么来的）。
        # 两个时刻只用来分**怎么看的**：单条点开 / 批量刷过 / 两个列都没留下（老数据）。
        seen = str(r["state"]) == "read"
        if str(r["state"]) == "dismissed":
            dismissed += 1
            cell["dismissed"] += 1
        elif real:
            # 他回话本身就等于看过，所以这一档不再要求 `state`
            picked += 1
            picked_sources[key] = picked_sources.get(key, 0) + 1
            cell["picked"] += 1
        elif not seen:
            unseen += 1
            cell["unseen"] += 1
        else:
            ignored += 1
            cell["ignored"] += 1
        if seen:
            if r["read_at"] is not None:
                opened += 1
            elif r["seen_at"] is not None:
                batch_seen += 1
            else:
                legacy_seen += 1
        # 连击的判据是"**有没有后续**"，不是"看没看"：她连冒三条而用户一条都没回，
        # 那就是三连击 —— 哪怕三条都还没被读到。第一版把"没看"当成断链，于是
        # ③ 报"5 条没看"而 ② 报"0 连击"，两个数互相打脸。
        if real:
            streak = 0
        else:
            streak += 1
            worst = max(worst, streak)
    known = [r for r in rows if r["fired_by"]]
    return {
        "total": len(rows),
        "picked": picked,
        "picked_proxy": picked_proxy,
        "ignored": ignored,
        "unseen": unseen,
        "dismissed": dismissed,
        "worst_streak": worst,
        "tail_streak": streak,
        "opened": opened,
        "batch_seen": batch_seen,
        "legacy_seen": legacy_seen,
        "by_source": by_source,
        "picked_sources": picked_sources,
        "cross": cross,
        # ⑤ 攒样本的进度（`R26-09` 的回访条件是"样本够 20 条再拿由头做取舍"）。
        # 观测窗口从**第一条带由头的行**算起，而不是从"那一列上线"写死一个日期：
        # 装好的那份是哪一版、什么时候装的，都会体现在数据里，写死的日子一定会漂。
        "known_source": len(known),
        "first_known_at": str(known[0]["created_at"]) if known else None,
        "last_known_at": str(known[-1]["created_at"]) if known else None,
    }


def _spread(counts: dict[str, int]) -> str:
    return " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) or "—"


def repeat_distribution(conn: Any) -> dict[str, Any]:
    """复读分分布（N4 ①）：`agent_reachout.repeat_score` 这一列的读侧。

    口径（`core/anti_repeat.repeat_score`）：0 = 完全不撞，越大越像自己。NULL = 列上线前
    落的，**不算样本**（与 `fired_by` 同一纪律：不知道就不摊）。返回中位数、高分位
    （≥0.5 = 闸门重灾区）占比、以及"谁在复读自己"按角色的最差一档 —— 前两个是分布，
    第三个是给"该调哪个角色卡的 anti-repeat"指路。
    """
    rows = conn.execute(
        "SELECT role_id, repeat_score FROM agent_reachout WHERE repeat_score IS NOT NULL"
    ).fetchall()
    if not rows:
        return {"n": 0, "median": None, "ge_half": None, "by_role": []}
    scores = sorted(float(str(r["repeat_score"])) for r in rows)
    n = len(scores)
    mid = n // 2
    median = scores[mid] if n % 2 == 1 else (scores[mid - 1] + scores[mid]) / 2.0
    ge_half = sum(1 for s in scores if s >= 0.5)
    per_role: dict[str, list[float]] = {}
    for r in rows:
        per_role.setdefault(str(r["role_id"]), []).append(float(str(r["repeat_score"])))
    by_role = sorted(
        (
            {
                "role": role,
                "n": len(v),
                "median": sorted(v)[len(v) // 2],
                "max": max(v),
            }
            for role, v in per_role.items()
        ),
        key=lambda kv: -float(str(kv["max"])),
    )
    return {"n": n, "median": median, "ge_half": ge_half, "by_role": by_role}


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
    seen_total = int(s["opened"] + s["batch_seen"] + s["legacy_seen"])
    print(f"主动消息 {total} 条（含已划掉的）\n")
    print("① 接话率（真判据：读那条线程的检查点，她那句之后出现过你的消息）")
    print(f"   {s['picked']} / {total} = {s['picked'] / total:.0%}")
    print(
        f"   同一批行按**旧那条近似**（那条线的 `updated_at` 晚于她这句）会报 "
        f"{s['picked_proxy']} / {total} —— 差 {abs(s['picked'] - s['picked_proxy'])} 条。"
        "\n     近似会被重命名、换模型那类 PATCH 推着走（`R26-35` 才发现 `updated_at` 这脾气），"
        "\n     所以两个数分开印在这里：看它们的差有多大，别拿其中一个当结论。"
    )
    print("② 自说自话连击数（连续多少条冒了话没有任何后续）")
    print(f"   最长 {s['worst_streak']} 连击；当前链尾 {s['tail_streak']}")
    print("③ 结局分布")
    print(
        f"   接了 {s['picked']} · 看了没接 {s['ignored']} · "
        f"没看 {s['unseen']} · 划掉 {s['dismissed']}"
    )
    print(
        f"   看过（`state=read`，也就是「进了对话界面就算看过」那条口径）{seen_total} 条里："
        f"单条点开 {s['opened']} · 批量刷过 {s['batch_seen']} · "
        f"两个时刻列都没留下的老数据 {s['legacy_seen']}"
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
    print("④′ 由头 × 结局（09-28 拍的口径：每个由头一行，接不接都数）")
    for key, cell in sorted(
        s["cross"].items(), key=lambda kv: (sum(kv[1].values()), kv[0]), reverse=True
    ):
        print(
            f"   {key}: {cell['total']} 条 → 接 {cell['picked']} · 看了没接 {cell['ignored']}"
            f" · 没看 {cell['unseen']} · 划掉 {cell['dismissed']}"
        )
    # ⑤ 攒样本的进度：回访要等的是条数，那就让它自己报还差多少，而不是下次再来翻代码。
    kn = int(s["known_source"])
    need = 20
    print(f"⑤ 攒样本进度（回访条件：带由头的行 ≥ {need} 条才拿它做档位取舍）")
    if kn == 0:
        print("   一条都还没有 —— 装好的那份还没跑过会写 `fired_by` 的版本，回访还没开始计时。")
    else:
        span_h = 0.0
        first, last = _parse(s["first_known_at"]), _parse(s["last_known_at"])
        if first and last and last > first:
            span_h = (last - first).total_seconds() / 3600.0
        print(f"   带由头 {kn} / {need} 条；从 {s['first_known_at']} 攒到 {s['last_known_at']}"
              f"（观测 {span_h:.1f} 小时）")
        if kn >= need:
            print("   够了 —— 可以按 ④ 的分布判第五由头有没有真驱动过一次开口。")
        elif span_h > 0 and kn >= 2:
            per_day = kn / (span_h / 24.0)
            print(f"   按这个速率约 {per_day:.1f} 条/天，还差 {need - kn} 条 ≈"
                  f" {(need - kn) / per_day:.1f} 天 —— 这只是「什么时候值得回来看一眼」，"
                  f"\n     不是预测：开口本身被闸门压着（间隔 + 静默段 + 退避），速率不该外推。")
        else:
            print("   只有 1 条，**算不出速率**（一条样本推不出「每天几条」，硬推就是编）。"
                  "\n     下次再读这一项时它会自己变准。")
    rd = repeat_distribution(conn)
    n_scored = int(rd["n"])
    print("⑥ 复读分分布（`repeat_score` 那一列，N4 ① —— 0=不撞，越大越像自己）")
    if n_scored == 0:
        print("   一条都还没有 —— 装有会写这一列的版本之前，这四项都是 ——。")
        print("   （这项要跨启动攒：`reachout_sent` 审计只在当次日志里，这才是能聚的）")
    else:
        print(
            f"   有分的 {n_scored} 条 · 中位 {rd['median']:.2f} · "
            f"≥0.5（闸门重灾）{int(rd['ge_half'])} 条 = {int(rd['ge_half']) / n_scored:.0%}"
        )
        worst = " · ".join(
            f"{d['role']} {d['n']}条/中位{d['median']:.2f}/最差{d['max']:.2f}"
            for d in rd["by_role"][:5]
        )
        print(f"   谁在复读自己（按最差档）：{worst}")
    print(
        "\n读法：四个数都还只是**条数口径**，样本 <20 时不要拿去做任何档位取舍 ——"
        "\n     它们的作用是把『完全量不出来』变成『有数但噪声大』。"
        "\n     ① 那条**今天之前只能偏低**：『主动会话』这一条线在 09-26 之前只有她在写"
        "\n     （控制台默认线、临时话题降级、原话跟着角色注入都是 09-26 才落地的），"
        "\n     他回话都回在别的线程里 —— 所以『她那句之后线里没出现你的消息』是真的，"
        "\n     但那不等于他没回她。从这个语义落地那天起，① 才开始量它字面上说的东西。"
        "\n     ⑥ 同理从装有会写 `repeat_score` 的版本那天起才开始攒。"
    )
    conn.close()


if __name__ == "__main__":
    main()
