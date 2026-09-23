"""记忆的两条模型路径：提取精华（对话 → 条目）与整理记忆（条目 → 更少、更准的条目）。

两条路都必须**显式**，理由不同但同源：

  * 一次提取 = 一次真模型调用。按消息数放大它在本地 8GB 卡上就是不可用的成本（市面做法也
    一致回避 per-message 提取），所以自动那条只按 `MEMORY_EXTRACT_TURNS` 轮兜底，并且跑在
    **响应已经流完之后的后台线程**里：它可以失败，但不能让对话变慢或变得不确定。
  * 整理会改写用户记忆里的事实。谁发起、什么时候发起必须看得见，所以界面上是一个按钮，
    不是一次"顺手在超限的时候做了"（超限时的保底淘汰在 `memory.enforce_cap` 里，那条不调模型）。

**线协议而不是 JSON**：让模型输出严格的行（`ADD 事实` / `UPDATE 12 新事实` / `MERGE 3,7 文本`）
比让它吐结构再容错更好教、更好校验，也避免"多写一个逗号整批丢光"。解析不了的行**忽略并计数**：
一行坏输出不该毁掉整次提取，但也绝不猜它想说什么（猜出来的事实会永久留在用户记忆里）。

**只写标记，不物理删**：整理产生的每一条变更都是 `invalidate_item`（带 `superseded_by`）或
新增一条，所以"整理坏了"最坏是回滚一个标记。物理删除只属于用户亲手点删除那一条路。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from rolecard_agent.core import memory as mem
from rolecard_agent.core.anti_repeat import grams, jaccard
from rolecard_agent.core.text import text_of
from rolecard_agent.core.usage import TokenUsage, parse_usage, record_usage
from rolecard_agent.storage.db import SqlConnection

# 一次提取最多带多少条最近消息、每条截多长：提取要的是"关于这个人的稳定事实"，
# 不是全文摘要 —— 给全文既贵又会让模型去总结情节而不是抽事实。
_MAX_MESSAGES = 24
_MAX_LINE_CHARS = 300
# 整理一次最多看这么多条（超出的按注入序留下次）：一次调用要能装进小上下文模型的窗口。
_MAX_CONSOLIDATE_ITEMS = 60

_OPS = "ADD / UPDATE / MERGE / INVALID / NOOP"

#: `count_similar` 的两个数：提示门槛与"短到不比"的下限。门槛定在实测同义对的最低值上
#: （0.40），因为它**判错不伤人**（用户多看一眼），而真正的合并仍由「整理记忆」发起。
SIMILAR_HINT = 0.40
_MIN_HINT_GRAMS = 6  # ≈7 个字：再短，两条不相干的事实靠共用几个字就能挤过门槛

_EXTRACT_PROMPT = (
    "你在维护一个长期记忆库。读下面的对话，只抽**关于用户的、值得跨会话记住的**事实："
    "身份与称呼、居住地、固定习惯与偏好、长期项目、明确说过的重要事件。\n"
    "不要抽：情节描写、模型自己说的话、一次性的闲聊、你的推测或评价。\n"
    "每条事实写成一句话（不超过 40 字），使用与对话相同的语言。\n"
    "\n"
    "只输出下列格式的行，每行一条指令，不要任何解释、不要代码块：\n"
    "  ADD <事实>                     库里没有的新事实\n"
    "  UPDATE <编号> <事实>           库里那条已过时或说错了，用新的一条取代它\n"
    "  NOOP                           没有任何值得新增的事实（就只输出这一行）\n"
    "编号见下面【已有条目】。与已有条目同义的事实不要重复 ADD。\n"
)

_CONSOLIDATE_PROMPT = (
    "你在整理一个长期记忆库，目标：更少、更准，不丢信息。\n"
    "对下面的条目做三类判断：同义的多条合成一条；被后面事实取代的标为失效并指向取代它的那条；"
    "其余不要动（不要为它输出任何行）。\n"
    "带 * 的条目由用户钉住，**不要对它输出任何指令**。\n"
    "\n"
    "只输出下列格式的行，每行一条指令，不要任何解释、不要代码块：\n"
    "  MERGE <编号,编号> <合并成的一条事实>\n"
    "  INVALID <编号> <取代它的那条事实>\n"
    "没有可整理的就只输出 NOOP。\n"
)

_ADD = re.compile(r"^ADD\s+(.+)$", re.IGNORECASE)
_UPDATE = re.compile(r"^UPDATE\s+(\d+)\s+(.+)$", re.IGNORECASE)
_MERGE = re.compile(r"^MERGE\s+([\d,\s]+)\s+(.+)$", re.IGNORECASE)
_INVALID = re.compile(r"^INVALID\s+(\d+)\s+(.*)$", re.IGNORECASE)


def _turn_lines(messages: Sequence[Any]) -> str:
    """把最近的消息压成"用户：… / 角色：…"的行（图片与空内容跳过）。"""
    lines: list[str] = []
    for message in list(messages)[-_MAX_MESSAGES:]:
        kind = str(getattr(message, "type", "") or "").lower()
        who = "用户" if kind in ("human", "user") else None if kind == "tool" else "角色"
        if who is None:
            continue
        text = " ".join(text_of(message).split())
        if not text:
            continue
        lines.append(f"{who}：{text[:_MAX_LINE_CHARS]}")
    return "\n".join(lines)


def _existing_block(items: list[dict[str, Any]], *, mark_pinned: bool) -> str:
    if not items:
        return "（空）"
    return "\n".join(
        f"{i['id']}{'*' if mark_pinned and i['pinned'] else ''}. {i['text']}" for i in items
    )


def _invoke(model: Any, prompt: str) -> tuple[str, TokenUsage | None]:
    """跑一次提取/整理调用，带回正文与**这次花掉的 token**（后端没报就是 None）。"""
    reply = model.invoke(prompt)
    return text_of(reply).strip(), parse_usage(reply)


def extract(
    conn: SqlConnection,
    *,
    model: Any,
    bucket: str,
    messages: Sequence[Any],
    source: str = "extract",
    backend: str | None = None,
    tracer: Any = None,
) -> dict[str, Any]:
    """从一段对话里提事实，按 ADD/UPDATE 落进某个记忆桶。

    UPDATE 不是就地改文本：它**新增一条**并把旧条目标为失效（`superseded_by` 指向新那条）
    —— 记忆的历史是审计"它什么时候开始以为我住在北京"的唯一证据（决策点 C）。

    `backend`/`tracer` 只为一件事：提取也花真钱（本地实测一次约 122 秒 / 248 token），
    所以它进同一本 token 账（审计 §12.8）。没给 backend 就记在"未指名"下，不假装免费。
    """
    dialogue = _turn_lines(messages)
    if not dialogue:
        return {"report": _report(noop=1, detail="这段对话没有可读取的文本。"), "ok": True}
    active = mem.ranked_active(conn, bucket=bucket)
    prompt = (
        _EXTRACT_PROMPT
        + "\n【已有条目】\n"
        + _existing_block(active, mark_pinned=False)
        + "\n\n【最近对话】\n"
        + dialogue
        + "\n"
    )
    try:
        raw, usage = _invoke(model, prompt)
    except Exception as exc:  # noqa: BLE001 - 提取失败不该影响任何东西，但要能查
        return {"ok": False, "report": _report(detail=f"模型调用失败：{type(exc).__name__}")}
    record_usage(conn, backend=backend, usage=usage, tracer=tracer)
    report = _report(tokens=usage.total if usage is not None else None)
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.upper() == "NOOP":
            report["noop"] += 1
            continue
        add = _ADD.match(text)
        if add:
            if mem.add_item(conn, bucket=bucket, text=add.group(1), source=source) is None:
                report["skipped"] += 1
            else:
                report["added"] += 1
            continue
        upd = _UPDATE.match(text)
        if upd:
            old = mem.get_item(conn, int(upd.group(1)))
            if old is None or old["invalidated_at"] is not None:
                report["skipped"] += 1
                continue
            fresh = mem.add_item(conn, bucket=bucket, text=upd.group(2), source=source)
            if fresh is None:
                report["skipped"] += 1
                continue
            if int(str(fresh["id"])) == int(str(old["id"])):
                # 模型说"这条过时了"，给的却是同一件事的另一种说法 ⇒ 同义判重把它并回了原条。
                # 这里绝不能继续往下把原条标为失效 —— 那等于把一条真事实亲手弄丢，
                # 而界面上只会显示"更新 1 条"。
                report["noop"] += 1
                continue
            mem.invalidate_item(conn, item_id=old["id"], superseded_by=int(str(fresh["id"])))
            report["updated"] += 1
            continue
        report["skipped"] += 1  # 看不懂的行：忽略并计数，不猜
    report["similar"] = count_similar(conn, bucket=bucket)
    return {"ok": True, "report": report}


def consolidate(
    conn: SqlConnection,
    *,
    model: Any,
    bucket: str,
    backend: str | None = None,
    tracer: Any = None,
) -> dict[str, Any]:
    """整理一个记忆桶：合并同义条目、让过时条目失效。**不物理删任何行。**"""
    active = mem.ranked_active(conn, bucket=bucket)[:_MAX_CONSOLIDATE_ITEMS]
    if len(active) < 2:
        return {
            "ok": True,
            "report": _report(noop=1, detail="活跃条目少于两条，没有可整理的。"),
        }
    prompt = _CONSOLIDATE_PROMPT + "\n【条目】\n" + _existing_block(active, mark_pinned=True) + "\n"
    try:
        raw, usage = _invoke(model, prompt)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "report": _report(detail=f"模型调用失败：{type(exc).__name__}")}
    record_usage(conn, backend=backend, usage=usage, tracer=tracer)
    by_id = {int(str(i["id"])): i for i in active}
    report = _report(tokens=usage.total if usage is not None else None, before=len(active))
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        merge = _MERGE.match(text)
        if merge:
            ids = [int(n) for n in re.findall(r"\d+", merge.group(1))]
            picked = [by_id.get(i) for i in ids]
            # 有任何一个 id 不存在 / 是钉住的，就整条指令放弃：宁可不合并，也不把用户钉住
            # 的事实卷进一次模型改写。
            if len(ids) < 2 or any(t is None for t in picked):
                report["skipped"] += 1
                continue
            targets = [t for t in picked if t is not None]
            if any(t["pinned"] for t in targets):
                report["skipped"] += 1
                continue
            fresh = mem.add_item(conn, bucket=bucket, text=merge.group(2))
            if fresh is None:
                report["skipped"] += 1
                continue
            for item in targets:
                if int(str(item["id"])) == int(str(fresh["id"])):
                    # 合并结果被 `add_item` 并进了这几条里的一条 —— 那条就是幸存者，
                    # 再给它写上"被自己取代"会把合并出来的事实直接弄丢。
                    continue
                mem.invalidate_item(
                    conn, item_id=int(str(item["id"])), superseded_by=int(str(fresh["id"]))
                )
            report["merged"] += 1
            continue
        invalid = _INVALID.match(text)
        if invalid:
            old = by_id.get(int(invalid.group(1)))
            if old is None or old["pinned"]:
                report["skipped"] += 1
                continue
            fresh = None
            if invalid.group(2).strip():
                fresh = mem.add_item(conn, bucket=bucket, text=invalid.group(2))
            mem.invalidate_item(
                conn,
                item_id=int(str(old["id"])),
                superseded_by=int(str(fresh["id"])) if fresh else None,
            )
            report["invalidated"] += 1
            continue
        if text.upper() == "NOOP":
            report["noop"] += 1
            continue
        report["skipped"] += 1
    report["after"] = len(mem.ranked_active(conn, bucket=bucket))
    report["similar"] = count_similar(conn, bucket=bucket)
    return {"ok": True, "report": report}


def count_similar(conn: SqlConnection, *, bucket: str) -> int:
    """桶里"字面上看着像同一件事"的条目有几条。**只用于提示，不改动任何一行。**

    为什么是提示而不是闸门（任务 #9 的实测结论，数据与推导写在 `docs/架构审计.md` §12.7 末）：
    字面度量分不开"同一件事换个说法"（真同义，实测 0.40~0.73）与"同一句式换个值"
    （「住在上海」vs「住在苏州」0.43、「每周三上课」vs「每周四上课」0.60 —— 都是**不同事实**）。
    在写入路径上挡下来会**吞掉**一条真事实，而放过去只是多一条看得见重复的条目 ——
    所以这里只报数，真正判断"是不是同一件事"留给模型 + 用户发起的「整理记忆」。
    """
    texts = [str(i["text"]) for i in mem.ranked_active(conn, bucket=bucket)]
    gram_sets = [_bigrams(t) for t in texts]
    flagged = 0
    for a in range(len(texts)):
        for b in range(a + 1, len(texts)):
            ga, gb = gram_sets[a], gram_sets[b]
            if len(ga) < _MIN_HINT_GRAMS or len(gb) < _MIN_HINT_GRAMS:
                continue  # 短到没什么可比材料的两条不比：共用几个字就能挤过门槛
            if jaccard(ga, gb) >= SIMILAR_HINT:
                flagged += 2  # 这一对里的两条都算"看着像重复"
                break
    return min(flagged, len(texts))


def _bigrams(text: str) -> list[str]:
    """字符二元组。用 2 而不是闸门那把 4-gram：换语序的同义句在 4-gram 上只剩 0.20。"""
    return grams(text, n=2)


def _report(**fields: Any) -> dict[str, Any]:
    """一次调用的结果账本。`tokens` 拿不到就是 None（不编一个数当成本）。"""
    out: dict[str, Any] = {
        "added": 0,
        "updated": 0,
        "similar": 0,  # 有几条字面上看着像同一件事：只是提示，合并要由「整理记忆」发起
        "merged": 0,
        "invalidated": 0,
        "noop": 0,
        "skipped": 0,
        "detail": "",
        "tokens": None,
        "before": None,
        "after": None,
    }
    out.update(fields)
    return out


# ---------------------------------------------------------------- 自动兜底的节奏


def due_for_extract(conn: SqlConnection, *, thread_id: str, every: int, message_count: int) -> bool:
    """自上次提取以来是否攒够了 `every` 个**用户轮次**（一轮 = 一问一答两条消息）。

    `every <= 0` = 关掉自动提取（只留手动按钮）。游标存的是"上次提取时的消息条数"，
    所以这个判断与模型无关、也不依赖墙钟 —— 连续聊天不会把成本放大成每几条一调。
    """
    if every <= 0 or message_count <= 0:
        return False
    row = conn.execute(
        "SELECT distilled_at_seq FROM session_thread WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    cursor = int(str(row["distilled_at_seq"] or 0)) if row and row["distilled_at_seq"] else 0
    return message_count - cursor >= every * 2


def mark_extracted(conn: SqlConnection, *, thread_id: str, message_count: int) -> None:
    conn.execute(
        "UPDATE session_thread SET distilled_at_seq = ? WHERE thread_id = ?",
        (message_count, thread_id),
    )
    conn.commit()


__all__ = [
    "consolidate",
    "count_similar",
    "due_for_extract",
    "extract",
    "mark_extracted",
]
