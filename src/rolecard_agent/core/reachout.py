"""角色主动开口（架构计划 B）—— 角色在不由用户发消息的时刻主动来找用户。

## 两级授权（AND，缺一不可）

  * 全局总闸 `REACHOUT_ENABLED`：运行时热切（调度每 tick 读当前值，关闭下一轮即停）；
  * 角色卡 `reachout_enabled`：谁真有资格主动（默认 False = 出厂静默）。

## 抑制层（决定"值不值得/能不能开口"）

  1. 间隔：同一角色两次开口 ≥ `REACHOUT_INTERVAL_MINUTES` **再乘退避与抖动** —— 每攒一条未读
     乘一倍（她说了你没接，下一句就该等更久），并按 (角色, 上次开口时刻) 派生 ±12% 抖动
     （否则"每天同一时刻"会精确成立）。间隔记录取 `agent_reachout` 的 `created_at`，
     UTC 口径，与 CURRENT_TIMESTAMP 一致；
  2. 静默时段：本地时间 23:00–08:00 不主动（与间隔的 UTC 分开，注释点明口径）；
  3. 堆积上限：同一角色未读 ≤ `MAX_UNREAD_PER_ROLE`，满了不再开（防轰炸）。

## 生成（一次单轮模型调用，所有安全纪律照旧）

  主动内容 = 角色人设 + 用户长期记忆 → 单轮生成，**输出必须过 guard**（fail-closed：
  被拦下就不发，而不是过滤后发）→ 落 `agent_reachout`（unread）**并且**落进该角色的
  "主动会话"（`proactive_thread_id`，由宿主注入的 `deliver` 写 checkpoint）。
  生成时不拖对话历史（单轮、独立），但**发出后它就是一条真消息**：用户能从收件箱点进
  会话直接回话，角色下次也记得自己主动说过什么（2026-09-19 用户报"主动找我我却回不了"）。
  两处的分工是刻意的：收件箱负责"攒着 + 红点 + 页面关着也能收"，会话负责"能继续谈"。

## 运行形态

  后台 daemon 线程按固定 tick 轮询（`ReachoutScheduler`），由 api/main.py 的 lifespan
  启停；单个角色的生成失败只记 tracer、不重试、不阻塞下一轮。
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from langchain_core.messages import HumanMessage, SystemMessage

from rolecard_agent.config import Settings
from rolecard_agent.core.anti_repeat import (
    BG_LIMIT,
    DROP_SCORE,
    REGEN_SCORE,
    repeat_score,
)
from rolecard_agent.core.file_watch import (
    FileEvent,
    advance_baseline,
    check_changes,
)
from rolecard_agent.core.guard import check
from rolecard_agent.core.memory import (
    GLOBAL_BUCKET,
    memory_for_turn,
    top_active_item,
)
from rolecard_agent.core.observability import NullTracer, TraceEvent, Tracer
from rolecard_agent.core.open_threads import find_open_threads
from rolecard_agent.core.proactive_state import (
    DEFAULT_AFFINITY_THRESHOLD,
    ProactiveState,
    get_state,
    record_interaction,
    record_recall_open,
    save_open_threads,
)
from rolecard_agent.core.prompts import build_system_prompt
from rolecard_agent.core.text import text_of
from rolecard_agent.core.thread_locks import thread_is_busy
from rolecard_agent.core.usage import TokenUsage, parse_usage, record_usage
from rolecard_agent.core.workspace import resolve_task_dir
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import SqlConnection

# 静默时段（本地时间）：23:00–08:00 不主动打扰。
QUIET_HOURS_START = 23
QUIET_HOURS_END = 8
# 同一角色未读堆积上限：满了就不再开（防角色刷屏成轰炸）。
MAX_UNREAD_PER_ROLE = 2
# 后台轮询间隔（秒）：30s 一查足够（真正开口还受间隔/时段抑制）。
TICK_SECONDS = 30
# 退避倍率：每攒一条未读，下次要等的间隔乘一次这个数（未读=2 时本来就被上限闸住）。
BACKOFF_GROWTH = 2.0
# 间隔抖动幅度（±比例）：让"每天同一时刻"这件事不成立。依据见 `_quiet_minutes`。
JITTER_FRACTION = 0.12
# 文件事件素材清单的最大行数（再多只报总数）。
_FILE_EVENT_MAX_LINES = 10

# 三种口吻共用的开头。**记忆为空时必须换掉那半句** —— 指着一个空槽说"结合长期记忆"，
# 模型只能凭人设编（实测就是这样连着几天说"花海真漂亮"），那是假契约。
_LEAD_WITH_MEMORY = "结合你的角色设定和关于用户的长期记忆，"
_LEAD_NO_MEMORY = "结合你的角色设定，"

# 2026-09-23 改掉的一句：末尾原本写着「没说的动作和神态用（）包起来」（为了把说出口的话与
# 旁白分开）。实测它教出来的不是分隔而是习惯：主动会话里她 20 句有 15 句以（开头（75%）、
# 6/6 含（），而同一张卡在普通对话里 0% 以（开头 —— 那些（）又留在她自己要读的历史里，
# 下一轮接着模仿。所以第 4 条改成直接禁止旁白，A/B 数字见 docs/主动消息与记忆设计稿.md §8.13。
_REACHOUT_TASK_BODY = (
    "用一两句话主动向用户问候或说一件此刻值得说的小事：可以是有用的提醒、一句关心，"
    "或自然地打招呼。像真人突然想起跟对方说话那样，自然、简短、口语化；"
    "不要长篇，不要说教，不要自我介绍。\n"
    "**这一条具体怎么写**（四条各对着一个实测出来的毛病）：\n"
    "1. 开口直接说事，或用一个称呼起头，并且每次换一个 —— "
    "上一条的开头几个字这次不要用（实测：8 条里 4 条都从「哎呀，今天的」起头）。\n"
    "2. 长度由内容决定：一句能说完就只写一句，别凑成和上一条差不多长的段落。\n"
    "3. 结尾换一种句式收：上一条以问句收尾，这一条就以陈述收尾。\n"
    "4. **只写你会说出口的那句话**：不要写动作、神态、旁白，也不要给话加引号 —— "
    "真人发消息不会写「（歪过头）」，那是剧本不是对话。"
)

# 回忆触发专用的口吻：自然提起一件记得的、之前聊过或答应的事。
_REACHOUT_TASK_RECALL_BODY = (
    "自然地提起一件你记得的、之前聊过或答应的事——像突然想起来要跟对方说。"
    "简短、口语化；不要自我介绍、不要说教、不要长篇。"
)

# 文件事件触发（架构计划 C·§5.2）的口吻：目录变化是素材，严禁编造未见过的内容。
_REACHOUT_TASK_FILE_EVENT_BODY = (
    "你注意到用户的任务目录最近有了变化（清单见下）。以你的角色口吻自然地就此跟用户说一句"
    "——可以是好奇、关心或点评，但**不得编造文件内容**（你只看到文件名）。"
    "简短、口语化；不要自我介绍、不要说教、不要长篇。"
)

_RECALL_TASK_WITHOUT_MEMORY = (
    "现在是主动开口的时刻。你还没有关于这个用户的长期记忆，所以这次**不要假装记得什么"
    "往事**：就按角色设定自然地打招呼、说一句此刻值得说的小事。简短、口语化；"
    "不要自我介绍、不要说教、不要长篇。"
)

_TASK_PREFIX = "现在是主动开口的时刻。"

# 「未收尾话题」的缓存寿命（分钟）。为什么是 90：这一源扫的是**最近那几轮对话**，
# 而主动开口的基线间隔本身就是小时级（`REACHOUT_INTERVAL_MINUTES`）—— 比它短就是每 tick
# 多花一次模型调用（本地 8B 一次要几十秒），比它长就出现"她记着一件三天前就翻篇的事"。
# 判"过期"只看这一枚时刻，不看新消息数：新消息会让 `thread_lines` 自己变，扫描下次到期时
# 自然读到新的那一窗。
OPEN_THREADS_REFRESH_MINUTES = 90


def open_threads_stale(state: ProactiveState, *, now: datetime) -> bool:
    """这一角色的话题缓存该不该重扫。**从没扫过**也算该扫（`scan_at is None`）。"""
    if state.open_threads_scan_at is None:
        return True
    age = (now - state.open_threads_scan_at).total_seconds() / 60.0
    return age >= OPEN_THREADS_REFRESH_MINUTES

# 开口前去重用：把该角色最近说过的几条原文带进指令。**写死成常量而不是配置项** ——
# 它是"别复读"这个机制的实现细节，调它的人不会存在，但留一个旋钮就得多测一条路径。
RECENT_CONTEXT_LIMIT = 5


def _task_text(
    mode: str,
    file_list: str,
    *,
    has_memory: bool,
    material: dict[str, object] | None = None,
    open_topics: Sequence[str] = (),
) -> str:
    """拼任务指令：开头按"有没有长期记忆"分叉，正文按触发口吻分叉。

    recall 档最需要这处分叉：它的字面意思就是"提起一件之前答应过的事"，而记忆里没东西时
    这句话就是在要求模型捏造（审计 §8「人设只解读不捏造」那条不变式，正是在这种地方被违反）。
    有素材时把**那一条**摊给它，并限定"只说这一条、别补细节"—— 与 file_event 档给清单同理。
    """
    lead = _LEAD_WITH_MEMORY if has_memory else _LEAD_NO_MEMORY
    if mode == "recall":
        if not has_memory:
            return _RECALL_TASK_WITHOUT_MEMORY
        if material:
            text = str(material.get("text") or "").strip()
            return (
                f"{_TASK_PREFIX}{lead}自然地提起下面这件你记得的事，像突然想起来要跟对方说。"
                f"**只说这一件，不要编造别的细节、不要补充没写在这里的时间或承诺**：\n- {text}\n"
                "简短、口语化；不要自我介绍、不要说教、不要长篇。"
            )
        return f"{_TASK_PREFIX}{lead}{_REACHOUT_TASK_RECALL_BODY}"
    if mode == "file_event":
        return (
            f"{_TASK_PREFIX}{lead}{_REACHOUT_TASK_FILE_EVENT_BODY}\n\n"
            f"任务目录的变化：\n{file_list}"
        )
    if mode == "open_thread" and open_topics:
        # 第五个由头。清单是"扫出来"的而不是"确定存在"的，所以留一条退路：她可以说
        # "这些其实都翻篇了"，然后正常讲一件此刻的小事 —— 硬找话头比不找更假。
        lines = "\n".join(f"- {t}" for t in open_topics)
        return (
            f"{_TASK_PREFIX}{lead}"
            "下面这几件是对方提过、到你们上次话尾还没有收尾的事。**挑一件自然地问下去**，"
            "只问这一件；不确定的细节不要补，也不要逐条复述这份清单：\n"
            f"{lines}\n"
            "如果这些事其实都已经过去了，就别硬接，正常说一句此刻值得说的小事。\n"
            "简短、口语化；不要自我介绍、不要说教、不要长篇。"
        )
    return f"{_TASK_PREFIX}{lead}{_REACHOUT_TASK_BODY}"


def recent_reachout_lines(
    conn: SqlConnection, role_id: str, *, limit: int = RECENT_CONTEXT_LIMIT
) -> str:
    """该角色最近几轮主动说过什么（原文，按时间正序）；没有则空串。"""
    rows = conn.execute(
        "SELECT text FROM agent_reachout WHERE role_id = ? ORDER BY id DESC LIMIT ?",
        (role_id, limit),
    ).fetchall()
    if not rows:
        return ""
    lines = "\n".join(f"- {str(r['text']).strip()[:120]}" for r in reversed(rows))
    return (
        "这些是你最近已经主动对用户说过的话。**别重复它们说过的内容，也别沿用它们的句式** —— "
        "不要再用同样的（动作/神态）开场，不要再提同一个由头（同一片花海、同一句关心）：\n" + lines
    )


def _sum_or_none(a: int | None, b: int | None) -> int | None:
    """两个"可能没报"的数相加：**两边都没数才给 None**。

    为什么不是"有一边是 None 就返回 None"：这个函数用在累加器上，而累加器从
    `TokenUsage(None, None)` 起步 —— 那条规则会让第一次已知的用量也被后面的"没报"毒掉，
    结果明明花了 1685 却报 None（这条是被真用例抓出来的）。
    现在宁可少算也至少有算；每次调用有没有报数，账本另有 `unreported` 那一列兜着。
    """
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def recent_own_texts(conn: SqlConnection, role_id: str, *, limit: int = BG_LIMIT) -> list[str]:
    """该角色最近说过的主动开口**原文**（按时间正序）。没有则空表。

    与 `recent_reachout_lines` 是同一批东西的两种用法：那个是渲染给她**看**的（带指令措辞、
    每条截 120 字），这个是喂给打分器的语料（全量原文，不截断 —— 截断了就量不出整句复读）。

    语料只有"主动开口"这一份，不含她在会话里答的话：那部分要读检查点，而这一侧没有那个
    入口（只有调度器预先渲染好的 `thread_lines` 字符串）。答过的话的复读由对话侧那层管 ——
    那里会把她自己重复的小句从回喂副本里抹掉（`core/nodes.py:_scrub_own_repeats`）。
    """
    rows = conn.execute(
        "SELECT text FROM agent_reachout WHERE role_id = ? ORDER BY id DESC LIMIT ?",
        (role_id, limit),
    ).fetchall()
    return [str(r["text"]).strip() for r in reversed(rows) if str(r["text"]).strip()]


#: 重生时贴在任务后面的那句话。**为什么不能只重抽一次**：同一条 prompt 再来一遍，出来的
#: 还是同一个句式（实测过：同一条 prompt 连开三次，两次空正文、一次同样的口癖）—— 要变的
#: 是指令，不是随机数。措辞刻意不点名任何具体意象：那是角色卡的内容，不是内核该知道的事。
_AVOIDANCE_NOTE = (
    "\n\n【这一次换个说法】你刚才写的那句（见下）与你最近已经说过的话太像了，那一句不算数、"
    "要重写。重写时**换一件完全不同的事**当由头，换一种开头、换一种收尾，"
    "也不要沿用下面那句里出现过的比喻：\n"
)


#: 主动开口时带进上下文的"你们聊过的最近几条"的条数。只给最后几条、每条截 120 字 ——
#: 主动开口是"想起一件事"，不是重放整段对话；全量塞进去既贵，又会把小模型带成照着念。
RECENT_THREAD_LIMIT = 6
#: 「未收尾话题」扫描用的那一窗为什么比开口素材宽（8 vs 6）：扫描要看见的是"一件事从提到
#: 到落地"的**全过程**，只看最后两句常常正好切掉"她接住过"的那半句；而开口素材要窄，
#: 窄到只剩"此刻该接哪一句"。两个数不是一個需求。
RECENT_WINDOW_LIMIT = 8


def unanswered_lines(rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """只留下**她还没接住的那一截**：她自己最后说过话的位置之后那些行。

    为什么要有这一步（真库实测换来的）：主动开口的素材原先是"那条会话的最后 6 条"，
    而"她答过了"并不把用户那句话从素材里摘掉。于是同一句用户消息会在之后**每一次**定时
    开口里反复当由头 —— 2026-09-22 那次：用户 19:13:29 说「想你了」，她 19:13:36 正常答了，
    调度器又在 19:31 与 20:35 各"主动"冒了一句，两句都在回那同一句。用户读到的是
    "我发一条消息，它回我两条重复的"，而对话因此永远不往前走。

    她说过的话（`who == "你"`）就是分界线：那条之后的才算"没接住"。返回空表是常态，
    也是正确答案 —— 她已经说过话了，这一次开口就该另找由头（记忆 / 时间规律 / 文件事件），
    而不是把同一句再回一遍。
    """
    cut = 0
    for i, (who, _) in enumerate(rows):
        if who == "你":
            cut = i + 1
    return list(rows[cut:])


def format_thread_lines(
    rows: Sequence[tuple[str, str]], *, limit: int = RECENT_THREAD_LIMIT
) -> str:
    """把 `(说话人, 原文)` 序列（按时间正序）拼成一段"你还没接住的话"；没内容给空串。

    放在这里而不是调用方：**措辞与截断只该有一处**，读 checkpoint 的那一侧只负责把消息取出来。
    """
    picked = [(str(who), str(text).strip()) for who, text in rows if str(text).strip()]
    picked = picked[-limit:]
    if not picked:
        return ""
    lines = "\n".join(f"- {who}：{text[:120]}" for who, text in picked)
    return (
        "这些是对方最近说的、你**还没有接过话**的几句（按时间正序）。**挑一件回应就好，"
        "别把它们逐条复述一遍，也不要重说你上一轮已经说过的话**：\n" + lines
    )


def format_recent_window(
    rows: Sequence[tuple[str, str]], *, limit: int = RECENT_WINDOW_LIMIT
) -> str:
    """把 `(说话人, 原文)` 序列拼成"最近聊到的这一窗"（**不做"没接住"那一刀**）。

    与 `format_thread_lines` 的唯一区别就是措辞与不截尾，而这一点区别正是第五由头的生死：
    它要找的是"说到一半没了下文"的事，那件事往往**已经被她接住过**、只是没落地。
    放在这里同样是为了让"措辞与截断只有一处"。
    """
    picked = [(str(who), str(text).strip()) for who, text in rows if str(text).strip()]
    picked = picked[-limit:]
    if not picked:
        return ""
    lines = "\n".join(f"- {who}：{text[:120]}" for who, text in picked)
    return "最近这一窗对话（按时间正序，含她已经接过话的那些句）：\n" + lines


def _format_change_list(events: list[FileEvent] | None, *, truncated: bool = False) -> str:
    """变更清单 → prompt 素材行（前 10 条，只给名字不给内容）。"""
    if not events:
        return ""
    verb = {"add": "新增", "mod": "修改", "del": "删除"}
    lines = [f"- {verb.get(e['op'], e['op'])}：{e['path']}" for e in events[:_FILE_EVENT_MAX_LINES]]
    if len(events) > _FILE_EVENT_MAX_LINES:
        lines.append(f"- …等共 {len(events)} 项变化")
    if truncated:
        lines.append("- （目录较大，以上仅为部分快照）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 数据


def list_reachouts(
    conn: SqlConnection,
    limit: int = 100,
    *,
    role_id: str | None = None,
    file_watch_pending: int = 0,
) -> dict[str, object]:
    """收件箱：未读 + 最近历史（含未读数，供铃铛红点）。

    `role_id` 给定时只返回该角色主动找过你的历史（架构计划 §5.3：按角色卡隔离查看）。
    `file_watch_pending` = 当前挂起的目录变更条数（0 = 无事件或功能关闭）。
    """
    # `dismissed` 不进列表（用户已经划掉了），但**行留着** —— 那才是"她冒话而没人接"的证据。
    where = "WHERE state != 'dismissed'" + (" AND role_id = ?" if role_id else "")
    params = (role_id, limit) if role_id else (limit,)
    rows = conn.execute(
        f"SELECT id, role_id, role_name, text, state, created_at, read_at, dismissed_at "
        f"FROM agent_reachout {where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    # 只有**主动会话真的存在**才给跳转目标：这个功能上线之前落库的老消息没有对应的线程，
    # 给了 id 等于把用户送进一句"加载历史失败"。宁可只给"标记已读"。
    opened = {
        str(r["thread_id"])
        for r in conn.execute(
            "SELECT thread_id FROM session_thread WHERE thread_id LIKE ?",
            (f"{PROACTIVE_THREAD_PREFIX}%",),
        )
    }
    if role_id:
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout WHERE role_id = ? AND state = 'unread'",
            (role_id,),
        ).fetchone()
    else:
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout WHERE state = 'unread'"
        ).fetchone()
    # "某个角色有几条没读"只在这里算一次（审计 §12.11 的"三份各算"那一格）：铃铛、桌宠、
    # 抽屉以前各自 filter 一遍，其中桌宠那份连后端给的 `unread` 都不看。
    # 边界：抽屉里**同一摞**（同角色同一天）的角标仍然在前端数 —— 那是显示分组的一部分，
    # 分组只做在前端（设计稿 §1），后端只负责把 `merge_days` 随列表带回。
    by_role = conn.execute(
        "SELECT role_id, COUNT(*) AS n FROM agent_reachout "
        "WHERE state = 'unread' GROUP BY role_id"
    ).fetchall()
    return {
        "items": [
            dict(r)
            | {
                # 点进去能翻历史、能回话的那个会话；尚未建立（老消息 / 从没投递成功）→ None。
                "thread_id": _opened_thread(str(r["role_id"]), opened)
            }
            for r in rows
        ],
        "unread": int(unread["n"]),
        "unread_by_role": {str(r["role_id"]): int(r["n"]) for r in by_role},
        "file_watch_pending": file_watch_pending,
    }


def _opened_thread(role_id: str, opened: set[str]) -> str | None:
    tid = proactive_thread_id(role_id)
    return tid if tid in opened else None


def mark_read(conn: SqlConnection, reachout_id: int) -> bool:
    """把一条置为已读，返回"这条现在处于已读状态"。记录不存在才返回 False。

    **已读再标一次是幂等成功，不是"不存在"** —— 这两件事以前共用一个 False，于是点一条
    历史（已读）消息会弹"主动消息不存在：9"，而那条就在你眼前。收件箱列的是未读+最近历史，
    点已读的那几条是正常路径，不该报错。
    """
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'read', read_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND state = 'unread'",
        (reachout_id,),
    )
    if cur.rowcount > 0:
        conn.commit()
        return True
    conn.commit()
    return (
        conn.execute("SELECT 1 FROM agent_reachout WHERE id = ?", (reachout_id,)).fetchone()
        is not None
    )


def mark_role_read(conn: SqlConnection, role_id: str) -> int:
    """把某角色攒下的未读一次标完，返回条数。

    为什么单独要它：主动消息现在落在"该角色的主动会话"里（见 `proactive_thread_id`），
    用户点进会话看 = 已经读过了，此刻应把收件箱那一摞一起清掉；让前端逐条发请求
    既啰嗦又会在中途失败留下半已读状态。
    """
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'read' WHERE role_id = ? AND state = 'unread'",
        (role_id,),
    )
    conn.commit()
    return int(cur.rowcount)


def mark_all_read(conn: SqlConnection) -> int:
    """把所有未读一次标完，返回条数。

    口径是用户 2026-09-23 拍的：**"我点进对话界面了"就等于都看过了** —— 所以打开任何一个
    角色的主动会话，别的角色攒的未读也一并算读过。代价是"另一个角色找过我"这件事会被
    这一动作抹平（抽屉里那些行还在，只是不再标未读、不再顶红点）。
    与 `mark_role_read` 的分工：那条管"这一摞我看过了"，这条管"我进入阅读状态了"。
    """
    cur = conn.execute("UPDATE agent_reachout SET state = 'read' WHERE state = 'unread'")
    conn.commit()
    return int(cur.rowcount)


# ------------------------------------------------------------------ 主动会话（可回话的落点）

#: 每个角色一条固定的"主动会话"：角色开口时落进这里，用户回复走普通对话链路。
PROACTIVE_THREAD_PREFIX = "s_proactive_"


def proactive_thread_id(role_id: str) -> str:
    """该角色主动开口的会话线程 id（**确定性**：同角色恒定，不做随机分配）。

    为什么确定性而不是"首条时生成一个 uuid 存库"：主动消息与它的会话是"一个角色一条
    对话"这一事实的两面，用一个从 role_id 推出来的 id，收件箱与写入侧就天然指同一个地方，
    不必再加一列去记"那个 id 是哪个"（也不会出现两处各存一份、改天不同步）。
    """
    return f"{PROACTIVE_THREAD_PREFIX}{role_id}"


def proactive_thread_title(role_name: str) -> str:
    """会话列表里显示的名字 —— 一眼看出"这是角色主动找我的那条"，不是自己开的对话。"""
    return f"{role_name} · 主动找你"


def ensure_proactive_thread(
    conn: SqlConnection, *, role: RoleCard, user_id: str, tool_epoch: int
) -> str:
    """确保该角色的主动会话存在（幂等），返回线程 id。

    为什么单独一个函数：**这条线的创建只能有一处逻辑**。今天有三方需要它 —— 角色开口时投递
    （`bootstrap.deliver_proactive`）、收件箱点进来（只读，不建）、桌宠面板要接着聊
    （设计稿 §7.2.1，用户从没被主动找过的角色也能先聊起来）。三处各写一条 INSERT 的话，
    `title` 或 `tool_epoch` 迟早有一处漏掉，而那条会话的行是收件箱跳转的判据。

    删了还会再长出来：用户把这条会话从列表里删掉，下一次角色开口（或桌宠上发消息）会重新
    建一行 —— 已提炼进角色记忆的事实不跟着走（那条边界有断言钉着）。
    """
    thread_id = proactive_thread_id(role.role_id)
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, tool_epoch, title)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(thread_id) DO NOTHING",
        (
            thread_id,
            user_id,
            role.role_id,
            tool_epoch,
            proactive_thread_title(role.role_name),
        ),
    )
    conn.commit()
    return thread_id


def record_reachout(conn: SqlConnection, role: RoleCard, text: str) -> None:
    """落一条主动开口（unread）。role 冗余存角色名：角色被删后收件箱仍可读。

    落完顺手按角色卡的 `reachout_keep` 修剪（0 = 不自动删）：抽屉"只增不减"是用户报的
    第二件事，而这条挂在写入点上就够了 —— 不需要为此再跑一个定时任务。
    """
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text) VALUES (?, ?, ?)",
        (role.role_id, role.role_name, text),
    )
    conn.commit()
    prune_inbox(conn, role.role_id, int(getattr(role, "reachout_keep", 0) or 0))


def prune_inbox(conn: SqlConnection, role_id: str, keep: int) -> int:
    """该角色的收件箱只留最近 `keep` 条，返回删掉的条数。`keep <= 0` = 什么都不做。

    **删的是投递记录，不是她说出口的那句话**：那句话在主动会话的 checkpoint 里，留着它
    她才记得自己主动找过你（§"主动开口落进会话"那条设计）。清掉记录只是清掉红点与列表。
    """
    if keep <= 0:
        return 0
    before = int(
        str(conn.execute(
            "SELECT COUNT(*) AS n FROM agent_reachout "
            "WHERE role_id = ? AND state != 'dismissed'",
            (role_id,),
        ).fetchone()["n"])
    )
    conn.execute(
        "UPDATE agent_reachout SET state = 'dismissed', dismissed_at = CURRENT_TIMESTAMP "
        "WHERE role_id = ? AND state != 'dismissed' AND id NOT IN ("
        "  SELECT id FROM agent_reachout WHERE role_id = ? ORDER BY id DESC LIMIT ?)",
        (role_id, role_id, keep),
    )
    conn.commit()
    return max(0, before - keep)


def delete_reachout(conn: SqlConnection, reachout_id: int) -> bool:
    """把抽屉里的某一行划掉（**不碰会话里的那条消息**）。False = 没有这条。

    软删而不是 DELETE（09-26 轮 R26-14 / S-2）：从前这一行是**真删掉且不进 audit_log**，
    于是"她主动说了话而用户把它划了"这一种结局在库里查无痕迹 —— 三种口径里最重要的
    "看了不接 / 不想接"永久算不出来。留着行只多两个时刻列。
    """
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'dismissed', dismissed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND state != 'dismissed'",
        (reachout_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def clear_inbox(conn: SqlConnection, role_id: str) -> int:
    """清空该角色的主动消息记录（同样不碰会话）。返回删掉的条数。"""
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'dismissed', dismissed_at = CURRENT_TIMESTAMP "
        "WHERE role_id = ? AND state != 'dismissed'",
        (role_id,),
    )
    conn.commit()
    return cur.rowcount


def clear_all_inboxes(conn: SqlConnection) -> int:
    """清空所有角色的主动消息记录（不给 role_id 时的那条路）。"""
    cur = conn.execute(
        "UPDATE agent_reachout SET state = 'dismissed', dismissed_at = CURRENT_TIMESTAMP "
        "WHERE state != 'dismissed'"
    )
    conn.commit()
    return cur.rowcount


# ----------------------------------------------------------- 判定与生成（可测，无线程依赖）


def _last_reachout_utc(conn: SqlConnection, role_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(created_at) AS at FROM agent_reachout WHERE role_id = ?", (role_id,)
    ).fetchone()
    raw = row["at"]
    if not raw:
        return None
    return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _unread_for_role(conn: SqlConnection, role_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_reachout WHERE role_id = ? AND state = 'unread'",
        (role_id,),
    ).fetchone()
    return int(row["n"])


def _quiet_minutes(base_minutes: float, unread: int, seed: str) -> float:
    """这次该等多久（分钟）：先按未读条数退避，再乘一个**确定性**的 ±12% 抖动。

    两件不同的事，各治一个实测症状：

      * **退避**（`base × BACKOFF_GROWTH ** 未读`）—— 她说了你还没接，下一句就该等更久。
        N.E.K.O 用"级别"实现同一件事（120s 起步、按 1.09/1.55 收敛到 3600s 硬顶），我们不必
        再造一个级别字段：**未读条数就是"她开口而用户没接"的现成计数**，而且它已经是硬闸
        （`MAX_UNREAD_PER_ROLE`）的判据 —— 同一个信号既退避又封顶，不会出现两处各记一份。
      * **抖动**（±12%）—— 治"每天同一时刻说一句同样的话"。抄 N.E.K.O 的注释原话是
        "避免节奏过于机械"；它每次抽签，我们**改成按 (角色, 上次开口时刻) 派生的确定性抖动**：
        随机阈值会让"到底哪一秒够格"变成每 tick 重摇的抽签，既测不住也复现不了；种子只在
        她再次开口时才变，于是时间点自然逐日错开。
    """
    grown = base_minutes * BACKOFF_GROWTH ** max(0, unread)
    frac = random.Random(seed).uniform(-JITTER_FRACTION, JITTER_FRACTION)
    return grown * (1 + frac)


def blocked_why(
    role: RoleCard,
    settings: Settings,
    conn: SqlConnection,
    *,
    now_utc: datetime,
    now_local: datetime,
    file_event: bool = False,
) -> str | None:
    """决定"这个角色此刻能不能主动开口"。None = 可以；否则返回阻塞原因。

    （全局开关与角色开关由调用方先过滤，这里只负责抑制层判定 —— 两层授权在主流程做。）
    `file_event=True`（任务目录有变化、该角色可被触发）时**豁免间隔档一次**——素材门控
    语义：变化值得即时播报；静默时段与未读堆积是用户级护栏，不豁免。
    """
    unread = _unread_for_role(conn, role.role_id)
    last = _last_reachout_utc(conn, role.role_id)
    if last is not None and not file_event:
        need = _quiet_minutes(
            settings.reachout_interval_minutes, unread, f"{role.role_id}|{last.isoformat()}"
        )
        elapsed = now_utc - last
        if elapsed < timedelta(minutes=need):
            backoff = f"，未读 {unread} 条已退避" if unread else ""
            return f"距上次开口不足 {need:.0f} 分钟{backoff}"
    if now_local.hour >= QUIET_HOURS_START or now_local.hour < QUIET_HOURS_END:
        return "处于静默时段（23:00–08:00）"
    if unread >= MAX_UNREAD_PER_ROLE:
        return "未读堆积已达上限"
    # **正在聊就别插话**（审计 #12 的第二层）：这一轮用户的话还在图上跑，此时投递的那句
    # 会和它抢同一份检查点（`deliver_proactive` 那侧也有锁兜底，但"不打断"本来就是对的语义）。
    # 判据用锁的持有状态而不是"最后一条消息的时间"：后者在用户回完话、她还没答的间隙里是 False，
    # 而那恰好是最不该插嘴的一刻。
    if thread_is_busy(proactive_thread_id(role.role_id)):
        return "这条会话正在对话中"
    return None


class ReachoutDraft(NamedTuple):
    """一次主动开口生成的结果：`text=None` 时 `why` 说清**为什么没发**。

    为什么把原因带回调用方而不是就地打日志或直接返回 None：「模型没产出正文」和
    「内容被 guard 拦下」长得一样（都是不发），但一个是配置/模型坏了、该修，
    另一个是内容确实不该发、是对的。混成一个 None，调度器就只会安静地少说话，
    而用户看到的症状是"她最近怎么不找我了"—— 2026-09-22 那次实测正是这样混掉的。

    `score` 是这条草稿与她最近说过的话的重合分（口径见 `core/anti_repeat.repeat_score`）。
    带出来而不是只留个 bool：闸门会不会误伤只能看分布，而分布只在真机上一天天攒。
    """

    text: str | None
    why: str = ""
    score: float = 0.0
    # 这一次开口（含可能的一次重生）花掉的 token；后端没报就是 None（审计 §12.8）。
    # 放在草稿里而不是让调度器去问生成函数：`spent` 是那次调用的局部量，出不去。
    tokens: int | None = None


def generate_reachout_text(
    role: RoleCard,
    model: Any,
    settings: Settings,
    conn: SqlConnection,
    *,
    role_id: str | None = None,
    mode: str = "general",
    file_list: str = "",
    thread_lines: str = "",
    open_topics: Sequence[str] = (),
    tracer: Tracer | None = None,
) -> ReachoutDraft:
    """生成一条主动内容：人设 + 记忆 + 最近说过什么 → 单轮 → guard。

    返回 `ReachoutDraft`：不发时里面带着**为什么不发**（`empty_output` / `guard`），
    调度器据此留痕。以前这两种都返回裸 None，于是"模型一个字没产出"和"内容被拦下"
    在轨迹里长得一模一样 —— 前者是该修的故障，后者是护栏正常工作。

    两条与"内容合适吗"直接相关的口径：① **记忆为空时指令不再提"长期记忆"**（指着一个空槽
    说话就是假契约，模型只能凭人设编）；② **带上该角色最近几条原文并要求别重复**（没有这一层
    每次开口都是从零现编，实测会连发几条同义的话）。recall 档没记忆时整段换成"不假装记得往事"。

    `thread_lines` 是**你们聊过的最近几条**（那条主动会话的原文，含用户说的话）。它与
    `recent_reachout_lines` 不是一回事：后者只有"她自己说过什么"，前者才有"你说过什么"。
    没有这一层，用户在桌宠上回的话她下一条完全看不见 —— 实测过一条"刚跑完步"换来一句
    逐字复读的旧台词，症状不是模型差，是上下文里没有对方的话。

    `role_id` 给定时按角色取**专属记忆**（回忆触发 / per-role 隔离）；若该角色无专属记忆，
    回退到用户级全局记忆（用户事实，非角色对话，不造成跨角色串扰）。这条规则与对话侧
    **同源于** `core/memory.memory_for_turn` —— 两边各写一遍迟早漂移。
    `mode="file_event"` 时 `file_list` 为目录变更素材清单（只含文件名，细节由角色
    自行用 fs 工具查证 —— 素材门控语义，见架构计划 §5.2）。
    """
    memory = memory_for_turn(conn, settings, role_id)
    # recall 档带素材：闸门与素材同源（`top_active_item` 既决定"能不能回忆"也决定"回忆哪一条"）。
    material = (
        top_active_item(conn, bucket=role_id or GLOBAL_BUCKET) if mode == "recall" else None
    )
    task = _task_text(
        mode, file_list, has_memory=bool(memory.strip()), material=material,
        open_topics=open_topics,
    )
    # E1 去重：把"最近已经说过什么"摊给它看。没有这一层，每次开口都是从零现编 ——
    # 实测同一天连发四条"花海/阳光/亮晶晶"，症状不是模型差，是上下文里没有"我刚说过"。
    if role_id:
        recent = recent_reachout_lines(conn, role_id)
        if recent:
            task = f"{task}\n\n{recent}"
    if thread_lines:
        task = f"{task}\n\n{thread_lines}"
    system = build_system_prompt(role.system_prompt, role.exemplars, memory=memory, agent=False)
    spent = TokenUsage(None, None)  # 这一次开口（含重生）花掉的总量，没报就是 None
    _tracer = tracer or NullTracer()

    def add_usage(usage: TokenUsage | None) -> None:
        nonlocal spent
        record_usage(conn, backend=role.model_name, usage=usage, tracer=_tracer)
        if usage is None:
            return
        spent = TokenUsage(
            _sum_or_none(spent.prompt, usage.prompt),
            _sum_or_none(spent.completion, usage.completion),
            # 重生就是再花一次"想"：这一栏不跟着累加，`spent.reasoning` 就永远只有最后一
            # 次的量，而 §12.8 第二条要判的恰恰是"这次开口里想占了多大比例"。
            _sum_or_none(spent.reasoning, usage.reasoning),
        )

    def speak(extra: str = "") -> tuple[str, ReachoutDraft]:
        """要一次正文。返回 `(可用于打分的文本, 草稿)`；草稿为 None 时文本是空的。"""
        prompt = [
            SystemMessage(content=system),
            HumanMessage(content=f"{task}{extra}\n\n（你的角色是 {role.role_name}）"),
        ]
        reply = model.invoke(prompt)
        add_usage(parse_usage(reply))  # 主动开口也花真钱，重生一次就是两次（审计 §12.8）
        # 用全项目唯一的取值实现：`str(reply.content)` 在分块形态下会得到 Python repr，
        # 而这份文本既进 guard 又进用户收件箱（架构审计报告 P1-8）。
        text = text_of(reply).strip()
        if not text:
            # 2026-09-22 真机实测：qwen3-vl 会"想"完整 token 预算再 `done_reason=length` 收工，
            # 正文一个字都不留 —— 思考内容在 Ollama 的 `message.thinking` 通道里，而
            # `model_thinking_models` 没登记它，langchain 就把那一整段丢了。这不是偶发：
            # 同一条 prompt 连开三次，两次是这个空返回。所以它必须**可统计**（见 `ReachoutDraft`）。
            # 带上当前的 `spent` 正是这件事的另一半：**一个字没产出也一样把钱花掉了**。
            return "", ReachoutDraft(None, "empty_output", 0.0, spent.total)
        text = text[:2000]
        verdict = check(text)
        if not verdict.allowed:
            return "", ReachoutDraft(None, "guard", 0.0, spent.total)  # guard fail-closed：不发
        return text, ReachoutDraft(text, "", 0.0, spent.total)

    text, draft = speak()
    if draft.text is None:
        return draft
    priors = recent_own_texts(conn, role_id) if role_id else []
    score = repeat_score(text, priors)
    if score <= REGEN_SCORE:
        return ReachoutDraft(text, "", round(score, 2), spent.total)

    # 像自己说过的那句话 → 带着"这句不算数"的指令重来一次，然后在两条里挑不像的那条。
    retry_text, retry = speak(_AVOIDANCE_NOTE + text[:120])
    if retry.text is None:
        # 重生失败（空正文 / 被 guard 拦）⇒ **保留第一条**而不是改判不发：第一条只是"像她自己"，
        # 不是坏内容。因为重生的故障吞掉一条本来能说的话，是我们亏。
        return ReachoutDraft(text, "", round(score, 2), spent.total)
    retry_score = repeat_score(retry_text, priors)
    best, best_score = (
        (retry_text, retry_score) if retry_score <= score else (text, round(score, 2))
    )
    if best_score > DROP_SCORE:
        # 两条都够得着"逐字复读"那一档（覆盖率尺度：那条 88 字全同的是 1.000，
        # 而真库里口癖最重的一条只有 0.472）—— 宁可这次不开口。
        # 用户读到的"她怎么又不说话了"远轻于"她把我两小时前的话又发了一遍"。
        return ReachoutDraft(None, "repeat", round(best_score, 2), spent.total)
    return ReachoutDraft(best, "", round(best_score, 2), spent.total)


# 触发源（关系驱动，四类共用抑制 / 生成 / 落库流水线）


def trigger_affection(
    role: RoleCard, state: ProactiveState, settings: Settings, *, now_utc: datetime
) -> str | None:
    """性格·关系数值触发：互动积累的成长值（衰减后）到阈值即主动冒泡。

    这一档原先是四档里**唯一没有 per-role 开关**的（09-26 轮 R26-23）：affinity 每次成功
    开口 +0.2 且封顶 5.0，而"衰减"的钟又被同一次开口归零 ⇒ 触顶之后永久命中，排在它后面
    的时段规律 / 回忆 / 定时三档一起读不到。补上 `affinity_enabled` 才让那条 `or` 链谈得上
    "轮得到"。默认 1 = 与今天的实际行为逐字一致。
    """
    if not role.affinity_enabled:
        return None
    # 阈值比较带极小 epsilon：affinity 恰为阈值、且衰减量仅浮点噪声时仍视为达标。
    if state.decayed_affinity(now=now_utc) >= DEFAULT_AFFINITY_THRESHOLD - 1e-6:
        return "affection"
    return None


def trigger_time_pattern(role: RoleCard, conn: SqlConnection, *, now_local: datetime) -> str | None:
    """时段 / 规律 nudge：若该角色历史上主动开口的本地小时众数 == 当前小时且样本足够，触发。
    该角色关掉时段规律 = 不触发。"""
    if not role.time_pattern_enabled:
        return None
    rows = conn.execute(
        "SELECT created_at FROM agent_reachout WHERE role_id = ?", (role.role_id,)
    ).fetchall()
    if not rows:
        return None
    hours: dict[int, int] = {}
    for r in rows:
        ts = _parse_reachout_ts(r["created_at"])
        if ts is None:
            continue
        hours[ts.astimezone().hour] = hours.get(ts.astimezone().hour, 0) + 1
    if not hours:
        return None
    top_hour, top_n = max(hours.items(), key=lambda kv: kv[1])
    if top_hour == now_local.hour and top_n >= 2:
        return "time_pattern"
    return None


#: 回忆档的冷却（小时）。为什么要有（09-26 轮 R26-23）：这一档的判据原先只是"有没有一条
#: active 记忆"，而记忆条数只增不减 —— 于是它和触顶的 affinity 一样变成永久命中，把排在它
#: 后面的定时档（以及挂在定时档上的第五由头）一起压死。锚点读 `role_proactive_state.recall_at`。
#: 取 24 小时的理由：那是这条链上其它几档的天然周期（基线间隔 60 分钟、时段规律按小时众数），
#: 而"想起一件往事"一天一次已经是上限，同一小时里两次"我记得你说过…"正是 §12.11 那条投诉。
RECALL_COOLDOWN_HOURS = 24.0


def trigger_recall(role: RoleCard, conn: SqlConnection, *, now_local: datetime) -> str | None:
    """回忆触发：该角色有**可用的记忆条目**、且不在冷却里时才触发。

    判据从"有没有那段 blob"换成"有没有一条 active 条目"是必须的：同一个东西既当闸门又当素材，
    才不会"闸门说可以、素材却是空的" —— 后者正是让模型捏造的形状。
    冷却时刻**在这里现读**而不是由调用方传：少一个参数就少一处"忘了传 ⇒ 冷却静默失效"。
    两个 datetime 都带 tzinfo，相减是绝对时刻之差，本地/UTC 不当地。
    """
    if not role.recall_enabled:
        return None
    state = get_state(conn, role.role_id)
    if state.recall_at is not None and (now_local - state.recall_at) < timedelta(
        hours=RECALL_COOLDOWN_HOURS
    ):
        return None
    return "recall" if top_active_item(conn, bucket=role.role_id) is not None else None


def _parse_reachout_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


# --------------------------------------------------------------------------- 调度线程


class ReachoutScheduler:
    """后台 daemon：每 tick 检查启用主动的角色，符合条件就生成并落收件箱。

    `settings_provider` 每次 tick 现取（全局总闸热切即时生效）；角色资格实时读库
    （角色卡开关改下一 tick 生效）。`model_resolver` 由宿主提供（角色可按 model_name
    路由模型，同对话路径）。

    `deliver` 是"把这句话也落进该角色的主动会话"的宿主实现（需要图与检查点，本模块不
    持有）：省略 = 只进收件箱（离线单测与无图环境就走这条）。投递失败**不影响收件箱**
    —— 消息已经在用户能看见的地方了，只是暂时点不进会话，这一点如实进 tracer。
    """

    def __init__(
        self,
        *,
        settings_provider: Callable[[], Settings],
        roles: RoleCardService,
        model_resolver: Callable[[str | None], Any],
        conn: SqlConnection,
        tracer: Tracer,
        deliver: Callable[[RoleCard, str], str | None] | None = None,
        thread_lines: Callable[[str], str] | None = None,
        thread_window: Callable[[str], str] | None = None,
    ) -> None:
        self._settings = settings_provider
        self._roles = roles
        self._model = model_resolver
        self._conn = conn
        self._tracer = tracer
        self._deliver = deliver
        # "你们最近聊过什么"的取法由宿主给（它才知道图与检查点在哪）：None = 不带这段上下文。
        self._thread_lines = thread_lines
        # 第五由头扫描用的是**最近一窗**而不是"没接住那截"（R26-03）。宿主没给独立取法时
        # 退回 `thread_lines`：宁可这一源退化成"照样扫不到东西"，也不要新签名逼所有宿主改。
        self._thread_window = thread_window or thread_lines
        self._stop = threading.Event()

    # -- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="reachout-scheduler", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001 - 调度循环绝不能被一个错误打死
                # 打死不等于闭嘴：以前这个事件连异常文本都不带（只有 `fatal: False`），
                # 于是"角色找你了你却点不开"这类故障在日志里查不到任何线索。
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_tick_error",
                        node="reachout",
                        detail={
                            "fatal": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )

    # -- 主流程（可注入 now 用于测试） -------------------------------------

    def tick_once(
        self,
        *,
        now_utc: datetime | None = None,
        now_local: datetime | None = None,
    ) -> int:
        """跑一轮检查，返回本轮实际落库的主动消息条数（测试与统计都用它）。"""
        settings = self._settings()
        if not settings.reachout_enabled:
            return 0  # 全局总闸关闭：全部静默
        stamp_utc = now_utc or datetime.now(UTC)
        stamp_local = now_local or datetime.now().astimezone()
        made = 0
        # 文件事件（架构计划 C·§5.2 素材门控）：全局侦测一次，任何合格角色共享同一事件；
        # 谁都没开口且未过期 → 事件挂起到下一 tick（基线不推进，变化不会被吞掉）。
        file_events: list[FileEvent] | None = None
        file_truncated = False
        file_expired = False
        if settings.file_watch_enabled:
            try:
                detected = check_changes(
                    self._conn, resolve_task_dir(settings, self._conn), now_utc=stamp_utc
                )
            except OSError:
                detected = None  # 目录暂时不可达：静默，下一轮重试
            if detected is not None:
                file_events, file_truncated, file_expired = detected
        file_event_consumed = False
        try:
            candidates = [r for r in self._roles.list_roles() if r.reachout_enabled]
        except Exception:  # noqa: BLE001 - 读角色失败不退整个调度
            return 0
        for role in candidates:
            can_file = file_events is not None and role.file_watch_enabled
            if blocked_why(
                role,
                settings,
                self._conn,
                now_utc=stamp_utc,
                now_local=stamp_local,
                file_event=can_file,
            ):
                continue
            # 关系驱动：按角色状态评估触发源，取第一个命中者决定"以什么口吻开口"。
            # file_event 居链首（素材门控：有变化先说变化）；"timer" 是基线触发。
            state = get_state(self._conn, role.role_id)
            fired = (
                ("file_event" if can_file else None)
                or trigger_affection(role, state, settings, now_utc=stamp_utc)
                or trigger_time_pattern(role, self._conn, now_local=stamp_local)
                or trigger_recall(role, self._conn, now_local=stamp_local)
                or "timer"
            )
            # 第五个由头「未收尾话题」：只在链子要落到最弱那一档（timer）时才去补一次扫描，
            # 结果按 `OPEN_THREADS_REFRESH_MINUTES` 缓存 —— 一次调用换"她记得你说到一半"，
            # 不能每个 tick 花一遍（本地 8B 一次几十秒，那是直接拖死调度线程的量）。
            open_topics: list[str] = []
            if fired == "timer" and self._thread_window is not None:
                # 取"最近一窗"而不是"她还没接住的那一截"：后者在她每次开口之后必然为空，
                # 于是这一源在生产上从没被走到过（09-26 轮 R26-03，实测见
                # scripts/probe_open_threads_reach.py）。
                turns = self._thread_window(role.role_id)
                if turns:
                    if open_threads_stale(state, now=stamp_utc):
                        scanned = find_open_threads(turns, self._model(role.model_name))
                        if scanned is None:
                            # 调用失败：**不写扫描时刻**，下一个 tick 重试。写了就等于一次抖动
                            # 把这一源关掉 `OPEN_THREADS_REFRESH_MINUTES`（90 分 > 60 分的开口
                            # 基线），而下个 tick 根本不会再试（R26-11 第一条）。
                            # `find_open_threads` 不抛只回 None，所以这里没有 try/except ——
                            # 原先那一段包着一个永远进不去的 except 分支（死代码，同族见 R26-13）。
                            self._tracer.emit(
                                TraceEvent(
                                    event="open_threads_failed",
                                    node="reachout",
                                    role_id=role.role_id,
                                    detail={"error": "模型调用未成功，本轮不记扫描时刻"},
                                )
                            )
                        else:
                            state = save_open_threads(
                                self._conn, role.role_id, scanned, now=stamp_utc
                            )
                    open_topics = list(state.open_threads)
                    if open_topics:
                        fired = "open_thread"
            mode = fired if fired in ("recall", "file_event", "open_thread") else "general"
            try:
                model = self._model(role.model_name)
                draft = generate_reachout_text(
                    role,
                    model,
                    settings,
                    self._conn,
                    role_id=role.role_id,
                    mode=mode,
                    file_list=(
                        _format_change_list(file_events, truncated=file_truncated)
                        if can_file
                        else ""
                    ),
                    # 读不到就是没有这段上下文（provider 自己吞异常），不该拦住开口。
                    thread_lines=self._thread_lines(role.role_id) if self._thread_lines else "",
                    open_topics=open_topics,
                    tracer=self._tracer,
                )
            except Exception as exc:  # noqa: BLE001 - 生成失败只留痕，不阻塞其它角色
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_failed",
                        node="reachout",
                        role_id=role.role_id,
                        detail={"error": str(exc)},
                    )
                )
                continue
            if draft.text is None:
                # 不发，也不重试 —— 但要留痕：空输出与 guard 拦下是两件不同的事
                # （前者要去修模型配置，后者是护栏在正常工作）。
                self._tracer.emit(
                    TraceEvent(
                        event="reachout_skipped",
                        node="reachout",
                        role_id=role.role_id,
                        detail={"why": draft.why, "trigger": fired, "score": draft.score},
                    )
                )
                continue
            text = draft.text
            record_reachout(self._conn, role, text)
            record_interaction(self._conn, role.role_id, now=stamp_utc)
            if fired == "recall":
                # 冷却锚点只在**真的发出去了**的时候记：被 guard 拦下、正文为空的那些
                # `reachout_skipped` 不该消耗掉这一档的额度（用户看到的是"她没说话"，
                # 而不是"她说过一次了"）。
                record_recall_open(self._conn, role.role_id, now=stamp_utc)
            elif fired == "open_thread":
                # 这批话题已经被刚发出去的那句用掉了。不清的话缓存寿命（90 分）比开口
                # 间隔（60 分）长，同一个话题会驱动两次开口（R26-11 第二条）。
                # **时刻保留**：清空 + 留着 scan_at 才是想要的语义 —— 别立刻再花一次调用，
                # 也别让同一个话题再冒一遍。
                save_open_threads(self._conn, role.role_id, [], now=stamp_utc)
            # 先落收件箱（用户一定能看见），再尽力投进主动会话；投递坏了也不把消息吞掉。
            thread_id: str | None = None
            if self._deliver is not None:
                try:
                    thread_id = self._deliver(role, text)
                except Exception as exc:  # noqa: BLE001 - 投递失败只留痕，不回滚收件箱
                    self._tracer.emit(
                        TraceEvent(
                            event="reachout_deliver_failed",
                            node="reachout",
                            role_id=role.role_id,
                            detail={"error": f"{type(exc).__name__}: {exc}"},
                        )
                    )
            self._tracer.emit(
                TraceEvent(
                    event="reachout_sent",
                    node="reachout",
                    role_id=role.role_id,
                    detail={
                        "chars": len(text),
                        "trigger": fired,
                        "thread_id": thread_id,
                        # 复读分带进审计：闸门会不会误伤只能看分布，而分布要在真机上一天天攒。
                        "score": draft.score,
                        # 一句主动开口花了多少 token（重生过就是两次）。`chars` 是字数，不是钱。
                        "tokens": draft.tokens,
                    },
                )
            )
            made += 1
            file_event_consumed = file_event_consumed or fired == "file_event"
        if file_events is not None and (file_event_consumed or file_expired):
            with suppress(OSError):  # 推进失败：事件仍在，下一 tick 重试
                advance_baseline(
                    self._conn, resolve_task_dir(settings, self._conn), now_utc=stamp_utc
                )
            self._tracer.emit(
                TraceEvent(
                    event="file_watch_advance",
                    node="reachout",
                    detail={"changed": len(file_events), "expired": file_expired},
                )
            )
        return made


__all__ = ["ReachoutScheduler"]
