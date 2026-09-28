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
    "不要抽：情节描写、模型自己说的话、你的推测或评价。\n"
    "**事实只从对方说过的话里取**：说过的地点、数值、时间照原样写；没出现过的地点、职业、"
    "程度、原因、结论一律不补（说过\"加班到十点\"推不出\"工作强度较高\"，更推不出住在哪）。"
    "反过来，**他明说的事就要记下来**，别因为看起来琐碎就漏掉（买了什么、几点睡、打算去做什么，"
    "对健康档案都是有用的事实）。\n"
    "**同一件事在对话里被提到几次，只写一条**，写信息最全的那句；不要把同一句原话抄两遍。\n"
    "直接写事实本身，不要加\"用户说\"\"用户表示\"这类前缀。\n"
    "每条事实写成一句话（不超过 40 字），使用与对话相同的语言。\n"
    "\n"
    "只输出下列格式的行，每行一条指令，不要任何解释、不要代码块：\n"
    "  ADD <事实>                     库里没有的新事实\n"
    "  UPDATE <编号> <事实>           库里那条已过时或说错了，用新的一条取代它\n"
    "  NOOP                           没有任何值得新增的事实（就只输出这一行）\n"
    "`ADD` 后可以紧跟一个档号 `[0]`/`[1]`/`[2]`（不写就是 `[1]`）：`[2]` 只给**说错会伤人**的那类"
    "—— 过敏与禁忌、正在治的东西、住址与家人这类身份事实；`[0]` 给「顺便提了一句」的偏好。"
    "拿不准就别写档号。\n"
    "编号见下面【已有条目】。**那份清单只用来看\"是不是已经有了\"和引用编号，"
    "不要把它的文字再抄成一条 ADD** —— 同义的事实不要重复 ADD。\n"
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

#: `ADD` 后面那个可选档号：`[2] 用户青霉素过敏`。写成可选是因为模型漏标是常态，
#: 而漏标的正确后果是"按常规档 1 记"，不是"这条被丢掉"。
_ADD = re.compile(r"^ADD\s*(?:\[(\d)\]\s*)?(.+)$", re.IGNORECASE)
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


#: 判"纯回声"时新条目至少要有多长：太短的句子（「猫」「十点」）作为子串到处都能撞上，
#: 拿它去跳过一条真事实的代价，比留一条重复大得多。
_MIN_ECHO_CHARS = 6


def is_echo(new_text: str, known: list[str]) -> bool:
    """这条 ADD 是不是**只是把已有条目里的字再抄一遍**。

    只认一个方向：新文本是某条已存条目的**子串** ⇒ 它没有带来任何新字，丢掉零风险。
    反方向（已有条目是新文本的子串）**不丢** —— 那是同一件事的更具体版本（"有结石" →
    "结石直径 6 mm"），把它当回声抹掉就是吞掉一条真事实，那种合并该由「整理记忆」判。

    为什么不在这里做 §12.9 那种相似度判断：那把尺子分不清"换个说法"与"换个值"
    （「住在上海」vs「住在苏州」的二元组 Jaccard 0.43，比两条真同义还像）。
    子串是**另一种东西**：它不问"像不像"，只问"这些字是不是已经在里面了"。
    """
    probe = " ".join(new_text.split())
    if len(probe) < _MIN_ECHO_CHARS:
        return False
    return any(probe in " ".join(one.split()) for one in known)


def extract(
    conn: SqlConnection,
    *,
    user_id: str,
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
    active = mem.ranked_active(conn, user_id=user_id, bucket=bucket)
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
    record_usage(conn, backend=backend, usage=usage, user_id=user_id, tracer=tracer)
    report = _report(tokens=usage.total if usage is not None else None)
    # 已存条目的文字，随本轮新增一起长：模型在同一次输出里把同一件事写两遍时也认得出回声。
    known = [str(i["text"]) for i in active]
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.upper() == "NOOP":
            report["noop"] += 1
            continue
        add = _ADD.match(text)
        if add:
            proposed = " ".join(str(add.group(2)).split())
            if is_echo(proposed, known):
                # 小模型会把【已有条目】那份清单当素材抄回 ADD（实测：本地 8B 连跑三轮，
                # added 11 → 9 → 8，桶里 26 条而 similar 已经 26）。丢掉纯回声是零信息损失的
                # 那一侧；要不要"合并成更具体的那条"是「整理记忆」的判断，不在这里越权。
                report["dup_echo"] += 1
                continue
            fresh = mem.add_item(
                conn,
                user_id=user_id,
                bucket=bucket,
                text=proposed,
                source=source,
                importance=mem.clamp_importance(add.group(1)),
            )
            if fresh is None:
                report["skipped"] += 1
            else:
                report["added"] += 1
                known.append(str(fresh["text"]))
            continue
        upd = _UPDATE.match(text)
        if upd:
            old = mem.get_item(conn, int(upd.group(1)), user_id=user_id)
            if old is None or old["invalidated_at"] is not None:
                report["skipped"] += 1
                continue
            fresh = mem.add_item(
                conn,
                user_id=user_id,
                bucket=bucket,
                text=upd.group(2),
                source=source,
            )
            if fresh is None:
                report["skipped"] += 1
                continue
            if int(str(fresh["id"])) == int(str(old["id"])):
                # 模型说"这条过时了"，给的却是同一件事的另一种说法 ⇒ 同义判重把它并回了原条。
                # 这里绝不能继续往下把原条标为失效 —— 那等于把一条真事实亲手弄丢，
                # 而界面上只会显示"更新 1 条"。
                report["noop"] += 1
                continue
            mem.invalidate_item(
                conn,
                user_id=user_id,
                item_id=old["id"],
                superseded_by=int(str(fresh["id"])),
            )
            report["updated"] += 1
            continue
        report["skipped"] += 1  # 看不懂的行：忽略并计数，不猜
    report["similar"] = count_similar(conn, user_id=user_id, bucket=bucket)
    return {"ok": True, "report": report}


def consolidate(
    conn: SqlConnection,
    *,
    user_id: str,
    model: Any,
    bucket: str,
    backend: str | None = None,
    tracer: Any = None,
) -> dict[str, Any]:
    """整理一个记忆桶：合并同义条目、让过时条目失效。**不物理删任何行。**"""
    active = mem.ranked_active(conn, user_id=user_id, bucket=bucket)[:_MAX_CONSOLIDATE_ITEMS]
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
    record_usage(conn, backend=backend, usage=usage, user_id=user_id, tracer=tracer)
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
            # 合并出来的那条**继承源条目里最高的显著性**：把"青霉素过敏"和另一句同义的
            # 过敏话合成一条，不该顺手把它从 [2] 降回常规档 —— 降级是「整理」里 INVALID
            # 那种有判断的动作，或者用户自己做的事，不是合并的副作用。
            # source 写 extract 而不是默认的 manual：这条是模型写的（schema 里那段注释
            # 早就这么说，之前这一行漏传了，于是面板上"谁写的"这一列对整理出来的条目在撒谎）。
            fresh = mem.add_item(
                conn,
                user_id=user_id,
                bucket=bucket,
                text=merge.group(2),
                source="extract",
                importance=max(
                    (mem.clamp_importance(t.get("importance")) for t in targets), default=1
                ),
            )
            if fresh is None:
                report["skipped"] += 1
                continue
            for item in targets:
                if int(str(item["id"])) == int(str(fresh["id"])):
                    # 合并结果被 `add_item` 并进了这几条里的一条 —— 那条就是幸存者，
                    # 再给它写上"被自己取代"会把合并出来的事实直接弄丢。
                    continue
                mem.invalidate_item(
                    conn,
                    user_id=user_id,
                    item_id=int(str(item["id"])),
                    superseded_by=int(str(fresh["id"])),
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
                fresh = mem.add_item(
                conn, user_id=user_id, bucket=bucket, text=invalid.group(2)
            )
            mem.invalidate_item(
                conn,
                user_id=user_id,
                item_id=int(str(old["id"])),
                superseded_by=int(str(fresh["id"])) if fresh else None,
            )
            report["invalidated"] += 1
            continue
        if text.upper() == "NOOP":
            report["noop"] += 1
            continue
        report["skipped"] += 1
    report["after"] = len(mem.ranked_active(conn, user_id=user_id, bucket=bucket))
    report["similar"] = count_similar(conn, user_id=user_id, bucket=bucket)
    return {"ok": True, "report": report}


def count_similar(conn: SqlConnection, *, user_id: str, bucket: str) -> int:
    """桶里"字面上看着像同一件事"的条目有几条。**只用于提示，不改动任何一行。**

    为什么是提示而不是闸门（任务 #9 的实测结论，数据与推导写在 `docs/架构审计.md` §12.7 末）：
    字面度量分不开"同一件事换个说法"（真同义，实测 0.40~0.73）与"同一句式换个值"
    （「住在上海」vs「住在苏州」0.43、「每周三上课」vs「每周四上课」0.60 —— 都是**不同事实**）。
    在写入路径上挡下来会**吞掉**一条真事实，而放过去只是多一条看得见重复的条目 ——
    所以这里只报数，真正判断"是不是同一件事"留给模型 + 用户发起的「整理记忆」。
    """
    texts = [str(i["text"]) for i in mem.ranked_active(conn, user_id=user_id, bucket=bucket)]
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
        "dup_echo": 0,  # 被当回声丢掉的 ADD：字面已在已有条目里，丢掉不损失任何信息
        "detail": "",
        "tokens": None,
        "before": None,
        "after": None,
    }
    out.update(fields)
    return out


# ---------------------------------------------------------------- 自动兜底的节奏


def due_for_extract(
    conn: SqlConnection, *, thread_id: str, every: int, messages: Sequence[Any]
) -> bool:
    """自上次提取以来是否攒够了 `every` 个**用户轮次**。

    只数 `HumanMessage`。docstring 一直写的是"用户轮次"，而实现数的是**消息条数除以 2** ——
    智能体模式下一次问答会有 AI 消息 + 工具消息 + 汇总消息（实测一次带工具调用的对话轮
    能占 4 条消息），于是"每 5 轮"实际变成"每 2.5 轮"：固定八轮对话跑了 4 次提取调用
    （3 自动 + 1 手动），每一次都是一次真模型调用，也每一次都把【已有条目】那份清单
    重新抄一遍回去（见 §12.12①）。**成本与重复是同一个根因。**

    `every <= 0` = 关掉自动提取（只留手动按钮）。游标存的是"上次提取时的消息条数"，
    所以这个判断与模型无关、也不依赖墙钟 —— 连续聊天不会把成本放大成每几条一调。
    """
    if every <= 0 or not messages:
        return False
    pending = pending_messages(conn, thread_id=thread_id, messages=list(messages))
    turns = sum(1 for m in pending if str(getattr(m, "type", "") or "").lower() == "human")
    return turns >= every


def mark_extracted(conn: SqlConnection, *, thread_id: str, message_count: int) -> None:
    conn.execute(
        "UPDATE session_thread SET distilled_at_seq = ? WHERE thread_id = ?",
        (message_count, thread_id),
    )
    conn.commit()


def nothing_new(
    conn: SqlConnection, *, user_id: str, bucket: str
) -> dict[str, Any]:
    """「这次没东西可抽」的报告 —— 形状归本模块管，宿主不该自己拼一份。

    `similar` 照样要算：按钮按下去却一条都没加时，界面上要说得出"库里有几对看着像同一件事"，
    那才是用户下一步（点「整理记忆」）的依据。
    """
    return _report(
        similar=count_similar(conn, user_id=user_id, bucket=bucket),
        detail="这段会话自上次提取以来没有新内容 —— 没有要重抽的东西。",
    )


def pending_messages(conn: SqlConnection, *, thread_id: str, messages: list[Any]) -> list[Any]:
    """**上次提取之后**新增的那几条消息 —— 决定"这一轮该喂什么"。

    这里修的是一个会直接放大成本的洞：游标 `distilled_at_seq` 过去只被 `due_for_extract`
    拿去判断"要不要跑"，喂给模型的却**始终是整段对话**。于是每自动提取一次，那几条老事实
    就换一种说法被重新抽一遍。实测（2026-09-24，固定八轮对话、副本库）：一轮对话自动跑了
    3 次，最后桶里 **35 条 / 实际只有 8 个不同事实** —— 而注入侧的窗口只有 8 条，
    重复条目是在**把不同的事实挤出 prompt**。

    编辑/删除消息会让列表变短、游标因此可能落在长度之外：那种情况下**退回整段**
    （宁可重抽一遍，也不要"看起来提取过、其实新消息一条都没看"）。
    """
    row = conn.execute(
        "SELECT distilled_at_seq FROM session_thread WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    cursor = int(str(row["distilled_at_seq"] or 0)) if row and row["distilled_at_seq"] else 0
    if not 0 <= cursor <= len(messages):
        return list(messages)
    return list(messages[cursor:])


__all__ = [
    "consolidate",
    "count_similar",
    "due_for_extract",
    "extract",
    "is_echo",
    "mark_extracted",
    "nothing_new",
    "pending_messages",
]
