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
6. **人眼对照抽样表**（`S-3` 候选 ②，09-28 落地）—— 按「由头 × 结局」分层抽 20 条导出成
   markdown（默认落 `build/`，不入库）：每行是她那句原文 + 你是否回过 + 你回话的前 60 字。
   ①②③④ 都是**条数口径**，回答不了"这句你想不想回"；这一份是唯一直接问那个问题的判据，
   代价是它不自动 —— 一周看一次、一次 10 分钟。

读的是**真库副本**：真库那份还没跑过新代码（`read_at`/`dismissed_at` 两列要靠启动时的
`reconcile_columns` 补出来），所以这里先 `copy_of_live_db` 再 `bootstrap` 一次 —— 数据
一行不改，只是把声明的列补齐。真库从头到尾只读。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/reachout_outcomes.py
    （--n 20 抽样条数 / --out 抽样表落盘路径）
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

from rolecard_agent.base.identity import resolve_instance_identity  # noqa: E402
from rolecard_agent.config import Settings  # noqa: E402
from rolecard_agent.features.reachout import (  # noqa: E402
    PROACTIVE_THREAD_PREFIX,
    proactive_thread_id,
)
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

    from rolecard_agent.base.text import text_of  # noqa: PLC0415
    from rolecard_agent.core.checkpointer import make_checkpointer  # noqa: PLC0415

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


def _first_reply_after(pairs: list[tuple[str, str]], text: str) -> str | None:
    """真判据的原始形式：**她那句之后**那条线上出现的第一条用户消息（原文）。

    两个 None 语义合一：她那句不在这条线里（投递失败的老消息 / 文本改过）与"没人回" ——
    对"接了没有"这个问题两者都是"没有"，但原文给"回话前 60 字"那列用（抽样表）。
    """
    at = next(
        (i for i in range(len(pairs) - 1, -1, -1) if pairs[i] == ("你", text.strip())),
        None,
    )
    if at is None:
        return None
    return next((t for who, t in pairs[at + 1:] if who == "用户"), None)


def classify(
    row: Any, lane_msgs: dict[str, list[tuple[str, str]]]
) -> dict[str, Any]:
    """把一行主动消息归到「由头 × 结局」那一格 —— **逐行判据只此一份**。

    `summarize` 的计数与抽样表都读它：两个视图可以说同一批行的不同话，但不允许对同一行
    给出不同的桶（`S-2` 第 1 条那个教训 —— ① 报 1 条而 ④ 数出 9 条）。

    结局的优先级：`dismissed`（划掉是用户主动的更强表态）> `picked`（他回话本身就等于看过，
    所以这一档不要求 `state`）> `unseen`（`state='read'` 才算看过，判据与 ③ 一致）> `ignored`。
    """
    text = str(row["text"])
    pairs = lane_msgs.get(proactive_thread_id(str(row["role_id"]), user_id=OWNER), [])
    reply = _first_reply_after(pairs, text)
    state = str(row["state"])
    if state == "dismissed":
        outcome = "dismissed"
    elif reply is not None:
        outcome = "picked"
    elif state != "read":
        outcome = "unseen"
    else:
        outcome = "ignored"
    return {
        # NULL 单列一档：**不知道**不等于"是某个源"。把老行摊进任何一档，就是替它们编一个由头。
        "source": str(row["fired_by"]) if row["fired_by"] else UNKNOWN_SOURCE,
        "outcome": outcome,
        "reply": reply,
        "real": reply is not None,
    }


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
    # 逐行归类的判据**只有一份**（`classify`）：这里数格子、抽样表也读它 —— 两个视图
    # 允许说同一批行的不同话，但不允许对同一行给出不同的桶（S-2 第 1 条的教训）。
    for r in rows:
        stamp = _parse(r["created_at"]) or datetime.min
        # 两个口径并排算：真判据用来分桶，近似那个留着**对照**（差值本身就是"这条近似有多不可信"）。
        info = classify(r, lane_msgs)
        picked_proxy += _proxy_replies(conn, str(r["role_id"]), stamp) > 0
        key = str(info["source"])
        outcome = str(info["outcome"])
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
        if outcome == "dismissed":
            dismissed += 1
            cell["dismissed"] += 1
        elif outcome == "picked":
            picked += 1
            picked_sources[key] = picked_sources.get(key, 0) + 1
            cell["picked"] += 1
        elif outcome == "unseen":
            unseen += 1
            cell["unseen"] += 1
        elif outcome == "ignored":
            ignored += 1
            cell["ignored"] += 1
        else:  # 防御：classify 只出这四个，兜住的就是"加了个新结局忘了这里"
            raise RuntimeError(f"unhandled outcome {outcome!r} from classify")
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
        if info["real"]:
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


OUTCOME_LABEL = {
    "picked": "接了",
    "ignored": "看了没接",
    "unseen": "没看",
    "dismissed": "划掉",
}


def _clip(text: object, limit: int) -> str:
    """压成一行 + 截断 + 转义管道符（markdown 表格里一竖会把格子切碎）。"""
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def sample_table(
    conn: Any, lane_msgs: dict[str, list[tuple[str, str]]], n: int = 20
) -> list[dict[str, Any]]:
    """人眼对照的 n 条（`S-3` 候选 ②）：按「由头 × 结局」**分层**抽，每格取最新的。

    为什么分层：一周只有那 10 分钟，最大的那格（比如 `affection` 一口吃掉半边天）不许
    把名额全占掉 —— 看的目的是"哪种由头、哪种结局的句子你最想回"，不是复刻分布。
    为什么不足 n 就不硬凑：库里只有 18 条时样本就是全部 18 条，多出来的名额无处可借。
    判据读 `classify`（与 ①④′ 同一份），所以这里的"结局"与前面那几个数永远同源。
    """
    if n <= 0:
        return []
    rows = conn.execute(
        "SELECT id, role_id, state, text, created_at, read_at, seen_at, dismissed_at, fired_by,"
        " repeat_score FROM agent_reachout ORDER BY id"
    ).fetchall()
    if not rows:
        return []
    classified: list[dict[str, Any]] = []
    for r in rows:
        item = dict(classify(r, lane_msgs))
        item.update(
            id=int(r["id"]),
            role_id=str(r["role_id"]),
            text=str(r["text"]),
            created_at=str(r["created_at"]),
            repeat_score=(
                float(str(r["repeat_score"])) if r["repeat_score"] is not None else None
            ),
        )
        classified.append(item)

    def newest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items, key=lambda x: (str(x["created_at"]), int(x["id"])), reverse=True
        )

    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in classified:
        cells.setdefault((str(item["source"]), str(item["outcome"])), []).append(item)
    total = len(classified)
    chosen: dict[int, dict[str, Any]] = {}
    for key in sorted(cells, key=lambda k: (-len(cells[k]), k)):
        quota = max(1, int(n * len(cells[key]) / total))  # 向下取整，宁可少分不多分
        for item in newest_first(cells[key])[:quota]:
            chosen[int(item["id"])] = item
    if len(chosen) < n:  # 配额取整后没分满：按时间补足（小格子先到先得由排序公平决定）
        for item in newest_first([x for x in classified if int(x["id"]) not in chosen]):
            chosen[int(item["id"])] = item
            if len(chosen) == n:
                break
    if len(chosen) > n:  # 各格至少 1 条之后可能超员：只留最新的 n 条
        for item in newest_first(list(chosen.values()))[n:]:
            chosen.pop(int(item["id"]), None)
    return newest_first(list(chosen.values()))


def render_sample(
    sample: list[dict[str, Any]], *, rows_total: int, generated_at: str
) -> str:
    """抽样表渲染成 markdown。最后一列留空给**人**填 —— 这张表的产出就是那 20 个手写值。"""
    lines = [
        "# 主动开口对照样本（人眼判一次）",
        "",
        f"生成于 {generated_at}｜从 {rows_total} 条主动消息里**分层抽样** {len(sample)} 条"
        "（由头 × 结局 每格取最新）。",
        "",
        "用法：一周一次、一次 10 分钟，只看**两句 + 最后一列** —— 她那句你想不想回"
        "（填「想回 / 不想回 / 说不清」）。「结局」那一列是真判据（她那句之后这条线里"
        "出现过你的消息），但**接不接 ≠ 想不想回**，这一眼就是来补那个差。",
        "",
        "| # | 时间 | 角色 | 由头 | 结局 | 她说的 | 你回话前 60 字 | 复读分 | 你判 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, item in enumerate(sample, 1):
        score = (
            f"{float(item['repeat_score']):.2f}"
            if item["repeat_score"] is not None
            else "—"
        )
        reply = _clip(item["reply"], 60) if item["reply"] else "—"
        # 时间直接切片到分钟：`_clip` 会在末尾加省略号，把 "04:38" 截成 "04:3…"。
        stamp_minute = str(item["created_at"])[:16]
        lines.append(
            f"| {i} | {stamp_minute} | {_clip(item['role_id'], 24)} "
            f"| {_clip(item['source'], 24)} | {OUTCOME_LABEL.get(str(item['outcome']), '?')} "
            f"| {_clip(item['text'], 120)} | {reply} | {score} |  |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    import argparse  # noqa: PLC0415 —— 只有真跑脚本时才需要它，装进测试导入也无害

    ap = argparse.ArgumentParser(description="主动开口的结局度量（含人眼对照抽样表）")
    ap.add_argument("--n", type=int, default=20, help="抽样表条数（默认 20，一周看一次的量）")
    ap.add_argument(
        "--out", default=None, help="抽样表落盘路径（默认 build/reachout-sample-<时间戳>.md）"
    )
    args = ap.parse_args()
    src = scratch_db.resolve_live_db()
    work = scratch_db.copy_of_live_db(ROOT / "build" / "scratch-outcomes.db", src)
    conn = connect(work)
    # 真库那份可能还没跑过新代码：`read_at` / `dismissed_at` / `fired_by` 都靠启动时的
    # `reconcile_columns` 补出来 —— 数据一行不改，只是把声明的列补齐。真库从头到尾只读。
    bootstrap(conn, enabled_domains=("health", "finance"))
    print(f"源库（只读）：{src}")
    print(f"副本（补过列之后读它）：{work}\n")
    # 检查点只读一次：口径计数与 ⑦ 的抽样表都从这一份里取"她那句之后有没有人回"。
    lane_msgs = read_lane_messages(conn)
    s = summarize(conn, lane_msgs)
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
    # ⑦ 人眼对照抽样表（S-3 候选 ②）：①②③④ 都答不了"这句你想不想回"，这一份直接问。
    # 落盘而不是只打印：那张表要**写**（最后一列手填），终端里写不了。
    sample = sample_table(conn, lane_msgs, args.n)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out) if args.out else ROOT / "build" / f"reachout-sample-{stamp}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_sample(sample, rows_total=total, generated_at=stamp), encoding="utf-8"
    )
    print("⑦ 人眼对照抽样表（唯一直接回答「这句你想不想回」的判据，代价是要人看）")
    print(f"   落盘 {out_path}")
    print(
        f"   {len(sample)} 条 / 全部 {total} 条（由头 × 结局分层，每格取最新；最后一列留给你手填）"
    )
    if total and len(sample) < total:
        print("   样本不是全部 —— 别拿它当统计结论，它的用途是把『这周她说得怎么样』摊在眼前。")
    conn.close()


if __name__ == "__main__":
    main()
