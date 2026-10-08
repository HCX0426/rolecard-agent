"""把一条会话的 `(说话人, 原文)` 序列切成 prompt 素材 —— **纯函数，不读库**。

为什么住在 core（2026-10-04 审查快照"core 装了产品功能"那一族，与 `proactive_thread.py`
同一刀）：这些"怎么切、怎么措辞"的规矩从前在主动开口功能包里，而**内核也要用** ——
`core/proactive.py` 的三个上下文读法（调度器开口素材、未收尾话题扫描）读的是 checkpoint
里的消息，跟"谁在生成这句话"无关。放在功能包里就得让内核反向 import 一个功能。

三个格式化器共用 `_bullets` 那一处"**截断与每行截 120 字**"：那条规矩散在几份里迟早漂。
措辞与截断只该有一处，读 checkpoint 的那一侧只负责把消息取出来。

两刀互补（`unanswered_lines` / `unreplied_lines`）切在同一个位置，只看"最后说话的是谁"，
两边合起来才覆盖"此刻悬着的是哪一截"—— 这是 09-26 那两次用户报障换来的形状，
依据逐条写在各自 docstring 里。
"""

from __future__ import annotations

from collections.abc import Sequence

#: 开口素材取最后几条（她还没接住的那一截）。
RECENT_THREAD_LIMIT = 6
#: 回声取最后几条：她主动说过的话在**别的**线程里也要能被她看见，但每条都在 prompt 里
#: 占一行 —— 3 条是"接得上话"与"多花 token"的折中（再多就是为"她记得"付整轮等待）。
CHAT_ECHO_LIMIT = 3
#: 「未收尾话题」扫描用的那一窗为什么比开口素材宽（8 vs 6）：扫描要看见的是"一件事从提到
#: 到落地"的**全过程**，只看最后两句常常正好切掉"她接住过"的那半句；而开口素材要窄，
#: 窄到只剩"此刻该接哪一句"。两个数不是一个需求。
RECENT_WINDOW_LIMIT = 8


def unanswered_lines(rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """只留下**她还没接住的那一截**：她自己最后说过话的位置之后那些行。

    为什么要有这一步（真库实测换来的）：主动开口的素材原先是"那条会话的最后 6 条"，
    而"她答过了"并不把用户那句话从素材里摘掉。于是同一句用户消息会在之后**每一次**定时
    开口里反复当由头 —— 2026-09-22 那次：用户 19:13:29 说「想你了」，她 19:13:36 正常答了，
    调度器又在 19:31 与 20:35 各"主动"冒了一句，两句都在回那同一句。用户读到的是
    "我发一条消息，它回我两条重复的"，而对话因此永远不往前走。

    她说过的话（`who == "你"`）就是分界线：那条之后的才算"没接住"。**返回空表不是"没话可说"，
    而是"轮到另一截"** —— 那时悬着的是她说过而他没回的那几句，见 `unreplied_lines`。
    """
    cut = 0
    for i, (who, _) in enumerate(rows):
        if who == "你":
            cut = i + 1
    return list(rows[cut:])


def unreplied_lines(rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """`unanswered_lines` 的另一半：**她说了、对方还没回**的那一截（最后一条用户消息之后）。

    两刀是互补的，切在同一个位置：最后说话的人是他 ⇒ 前一截非空；是她 ⇒ 后一截非空。
    为什么要有后一截（09-26 用户报的"也不管之前的内容"）：素材原先只供给"他说了而她没接"，
    于是"她上一条还悬着、他一个字没回"这件事在她那里是**隐形**的 —— 她只能另找由头，
    读起来就是"转头说起一件不相干的事"。把这一截给她，她才可能接住那份安静。

    一条用户消息都没有时她说的**全都算**悬着 —— 那是"他从没在这条线里开过口"，不是"没得算"。
    """
    cut = 0
    for i, (who, _) in enumerate(rows):
        if who == "用户":
            cut = i + 1
    return list(rows[cut:])


def _bullets(rows: Sequence[tuple[str, str]], limit: int) -> str | None:
    """取最后 `limit` 条非空原文拼成清单行；一条都没有给 None（让调用方决定空串）。

    三个格式化器共用这一处"**截断与每行截 120 字**"：那条规矩散在三份里迟早漂。
    """
    picked = [(str(who), str(text).strip()) for who, text in rows if str(text).strip()]
    if not picked:
        return None
    return "\n".join(f"- {who}：{text[:120]}" for who, text in picked[-limit:])


def format_thread_lines(
    rows: Sequence[tuple[str, str]], *, limit: int = RECENT_THREAD_LIMIT
) -> str:
    """把 `(说话人, 原文)` 序列（按时间正序）拼成一段"你还没接住的话"；没内容给空串。

    放在这里而不是调用方：**措辞与截断只该有一处**，读 checkpoint 的那一侧只负责把消息取出来。
    """
    lines = _bullets(rows, limit)
    if lines is None:
        return ""
    return (
        "这些是对方最近说的、你**还没有接过话**的几句（按时间正序）。**挑一件回应就好，"
        "别把它们逐条复述一遍，也不要重说你上一轮已经说过的话**：\n" + lines
    )


def format_unreplied_lines(
    rows: Sequence[tuple[str, str]], *, limit: int = RECENT_THREAD_LIMIT
) -> str:
    """拼成"你说过而对方还没回"的那一段；没内容给空串。

    措辞与 `format_thread_lines` 相反是有意的：那一段让她"挑一件回应"，这一段让她**别另起炉灶**。
    同一条"别重复说过的话"的规矩仍然生效，所以这里要写清"接住那份安静"不等于"把那句再说一遍"。
    """
    lines = _bullets(rows, limit)
    if lines is None:
        return ""
    return (
        "这几句是**你**说的、对方到现在还没有回的话（按时间正序）。别当它们没发生过、"
        "也别转头说起一件不相干的事：要么自然地问一句他现在在忙什么、是不是没空看手机，"
        "要么顺着你上一句再往前说一步 —— **但不要把已经说过的这句原样再说一遍**。"
        "如果他一直不回，这一条宜短不宜长。\n" + lines
    )


def format_recent_window(
    rows: Sequence[tuple[str, str]], *, limit: int = RECENT_WINDOW_LIMIT
) -> str:
    """把 `(说话人, 原文)` 序列拼成"最近聊到的这一窗"（**不做"没接住"那一刀**）。

    与 `format_thread_lines` 的唯一区别就是措辞与不截尾，而这一点区别正是第五由头的生死：
    它要找的是"说到一半没了下文"的事，那件事往往**已经被她接住过**、只是没落地。
    放在这里同样是为了让"措辞与截断只有一处"。
    """
    lines = _bullets(rows, limit)
    if lines is None:
        return ""
    return "最近这一窗对话（按时间正序，含她已经接过话的那些句）：\n" + lines


__all__ = [
    "CHAT_ECHO_LIMIT",
    "RECENT_THREAD_LIMIT",
    "RECENT_WINDOW_LIMIT",
    "format_recent_window",
    "format_thread_lines",
    "format_unreplied_lines",
    "unanswered_lines",
    "unreplied_lines",
]
