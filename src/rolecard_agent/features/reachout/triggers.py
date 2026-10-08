"""触发评估与开口生成（`R102-59` 从 `core/reachout.py` 拆出的四模块之一）。

本模块回答两个问题：**该不该由某个由头开口**（关系驱动四类触发源，以及「未收尾话题」的
扫描时效），以及**以那个由头的口吻说什么**（任务指令拼装 → 单轮模型调用 → guard → 去重
复测）。两者同住一个模块的理由：生成的口吻由触发档位（`mode`）直接决定，且它们都是无线程
依赖的纯函数 —— 调度器（`scheduler.py`）只按结果走流水线。

纯搬层，行为与拆分前逐字一致。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from langchain_core.messages import HumanMessage, SystemMessage

from rolecard_agent.base.identity import resolve_instance_identity
from rolecard_agent.base.observability import NullTracer, Tracer
from rolecard_agent.base.text import text_of
from rolecard_agent.config import Settings
from rolecard_agent.core.agent.guard import check
from rolecard_agent.core.agent.prompts import build_system_prompt
from rolecard_agent.core.common.anti_repeat import (
    BG_LIMIT,
    DROP_SCORE,
    REGEN_SCORE,
    repeat_score,
)
from rolecard_agent.core.common.usage import TokenUsage, parse_usage, record_usage
from rolecard_agent.core.file_watch import FileEvent
from rolecard_agent.core.memory import (
    GLOBAL_BUCKET,
    memory_for_turn,
    top_active_item,
)
from rolecard_agent.core.proactive_state import (
    DEFAULT_AFFINITY_THRESHOLD,
    ProactiveState,
    get_state,
)
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.storage.db import SqlConnection

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

# 文件事件触发（架构总览 §5）的口吻：目录变化是素材，严禁编造未见过的内容。
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
    body = f"{_TASK_PREFIX}{lead}{_REACHOUT_TASK_BODY}"
    # 手里有"你说到一半"的素材时，**哪一档开口都该用得上**：从前这份清单只在
    # `mode == "open_thread"` 那一档被读，而那一档要等整条链落到最弱的 timer 才够得着 ——
    # 于是 affection 开口时素材被整份丢掉（09-30 生产读数：`open_threads_at` 至今为 NULL，
    # 见台账 G3.8 追记）。这里只当**可选素材**给，不改开口节奏，也明令不许硬接。
    # recall / file_event 两档在上面就 return 了：它们的指令是"只说这一件"，
    # 再塞第二个话头就是让同一条消息干两件事。
    if open_topics:
        lines = "\n".join(f"- {t}" for t in open_topics)
        body += (
            "\n另外，下面这几件是对方提过、你们上次话尾还没收尾的事。"
            "**顺着说一句可以，但不顺就别硬接**，不确定的细节不要补、也不要逐条复述：\n"
            f"{lines}\n"
        )
    return body


def recent_reachout_lines(
    conn: SqlConnection, role_id: str, *, user_id: str, limit: int = RECENT_CONTEXT_LIMIT
) -> str:
    """该角色最近几轮主动说过什么（原文，按时间正序）；没有则空串。"""
    rows = conn.execute(
        "SELECT text FROM agent_reachout"
        " WHERE role_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
        (role_id, user_id, limit),
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


def recent_own_texts(
    conn: SqlConnection, role_id: str, *, user_id: str, limit: int = BG_LIMIT
) -> list[str]:
    """该角色最近说过的主动开口**原文**（按时间正序）。没有则空表。

    与 `recent_reachout_lines` 是同一批东西的两种用法：那个是渲染给她**看**的（带指令措辞、
    每条截 120 字），这个是喂给打分器的语料（全量原文，不截断 —— 截断了就量不出整句复读）。

    语料只有"主动开口"这一份，不含她在会话里答的话：那部分要读检查点，而这一侧没有那个
    入口（只有调度器预先渲染好的 `thread_lines` 字符串）。答过的话的复读由对话侧那层管 ——
    那里会把她自己重复的小句从回喂副本里抹掉（`core/agent/nodes.py:_scrub_own_repeats`）。
    """
    rows = conn.execute(
        "SELECT text FROM agent_reachout"
        " WHERE role_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
        (role_id, user_id, limit),
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


# 文件事件素材清单的最大行数（再多只报总数）。
_FILE_EVENT_MAX_LINES = 10


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
    自行用 fs 工具查证 —— 素材门控语义，见架构总览 §5）。
    """
    # 后台这条链没有「这次请求」可问：按这台实例的主人读（§4.1 的实例级身份）。
    owner = resolve_instance_identity(settings)
    memory = memory_for_turn(conn, settings, role_id, user_id=owner)
    # recall 档带素材：闸门与素材同源（`top_active_item` 既决定"能不能回忆"也决定"回忆哪一条"）。
    material = (
        top_active_item(conn, user_id=owner, bucket=role_id or GLOBAL_BUCKET)
        if mode == "recall"
        else None
    )
    task = _task_text(
        mode, file_list, has_memory=bool(memory.strip()), material=material,
        open_topics=open_topics,
    )
    # E1 去重：把"最近已经说过什么"摊给它看。没有这一层，每次开口都是从零现编 ——
    # 实测同一天连发四条"花海/阳光/亮晶晶"，症状不是模型差，是上下文里没有"我刚说过"。
    if role_id:
        recent = recent_reachout_lines(conn, role_id, user_id=resolve_instance_identity(settings))
        if recent:
            task = f"{task}\n\n{recent}"
    if thread_lines:
        task = f"{task}\n\n{thread_lines}"
    system = build_system_prompt(role.system_prompt, role.exemplars, memory=memory, agent=False)
    spent = TokenUsage(None, None)  # 这一次开口（含重生）花掉的总量，没报就是 None
    _tracer = tracer or NullTracer()

    def add_usage(usage: TokenUsage | None) -> None:
        nonlocal spent
        # 记的是**实际接话那台**：角色没声明后端时落全局默认，声明的那台被删过也落默认
        # （`resolve_role_model` 正是这么降级的）。记成 `role.model_name` 会让这两种情况
        # 在按后端分组的用量页上变成 NULL。
        record_usage(
            conn,
            backend=settings.backend_name(role.model_name),
            usage=usage,
            user_id=owner,
            tracer=_tracer,
        )
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
    priors = (
        recent_own_texts(conn, role_id, user_id=resolve_instance_identity(settings))
        if role_id
        else []
    )
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


def trigger_time_pattern(
    role: RoleCard, conn: SqlConnection, *, user_id: str, now_local: datetime
) -> str | None:
    """时段 / 规律 nudge：若该角色历史上主动开口的本地小时众数 == 当前小时且样本足够，触发。
    该角色关掉时段规律 = 不触发。"""
    if not role.time_pattern_enabled:
        return None
    rows = conn.execute(
        "SELECT created_at FROM agent_reachout WHERE role_id = ? AND user_id = ?",
        (role.role_id, user_id),
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


def trigger_recall(
    role: RoleCard, conn: SqlConnection, *, user_id: str, now_local: datetime
) -> str | None:
    """回忆触发：该角色有**可用的记忆条目**、且不在冷却里时才触发。

    判据从"有没有那段 blob"换成"有没有一条 active 条目"是必须的：同一个东西既当闸门又当素材，
    才不会"闸门说可以、素材却是空的" —— 后者正是让模型捏造的形状。
    冷却时刻**在这里现读**而不是由调用方传：少一个参数就少一处"忘了传 ⇒ 冷却静默失效"。
    两个 datetime 都带 tzinfo，相减是绝对时刻之差，本地/UTC 不当地。
    """
    if not role.recall_enabled:
        return None
    state = get_state(conn, role.role_id, user_id=user_id)
    if state.recall_at is not None and (now_local - state.recall_at) < timedelta(
        hours=RECALL_COOLDOWN_HOURS
    ):
        return None
    return "recall" if top_active_item(
        conn, user_id=user_id, bucket=role.role_id
    ) is not None else None


def _parse_reachout_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)