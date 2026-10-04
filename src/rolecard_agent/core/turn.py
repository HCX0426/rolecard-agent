"""一轮对话的编排（内核侧）：跑图 → 产出**结构化轮次事件**，不知道 HTTP、不知道 SSE。

为什么从 `api/chat.py` 拆出来（架构审计报告 §0 / §7）：原先"一轮怎么编排"和"怎么把它
帧成 Server-Sent Event"缠在同一个函数里，于是里程碑 D 的桌宠壳要么重写这~150 行、要么
被迫继续吃 SSE —— 而事件流只是**其中一种**投送方式。拆完之后：

    内核 `run_turn()` → `Iterator[TurnEvent]`  ← 桌宠壳 / 评测脚本 / 任何宿主直接消费
    HTTP 壳 `api/chat.py` → 帧成 SSE + 线程池桥   ← 浏览器要的格式

两个不变式（都在下面 `TurnEvent` 与 `run_turn` 的注释里），它们比"代码放哪儿"更重要：

1. **流式审核怎么活下来**：`call_model` 的 guard 审的是**完整**回复，而流式意味着 token 先于
   该检查存在 —— 但"生成了"不等于"显示了"。这里只投送**已过审的前缀**：每个块喂给累加器，
   对累计文本跑 `guard.check()`，只有(a)检查仍通过且(b)该文本越过了尾部回扣窗口
   （`WINDOW` ≥ 最长触发式，所以半个触发式永远不会显示）才发出。累计文本一触规则就**停止
   投送**，权威文本（与 `call_model` 写进 checkpoint 的那份同一个 `BLOCKED_RESPONSE`）
   以 `MessageReplace` 事件到达。残余风险如实写明而非藏起：触发式的前半截可能短暂出现，
   由 replace 事件纠正。
2. **权威文本从哪儿来**：`stream_mode="messages"` 给的是模型的原始块（guard 之前），
   `stream_mode="updates"` 给的是节点**已提交**的内容（guard 之后）。每轮模型结束时对账：
   相等 → 把窗口扣住的尾巴作为最终 token 放出；不等（guard 改写、或角色缺失兜底句）→
   `MessageReplace`。客户端永远把已提交文本当真相。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, ClassVar

from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError

from rolecard_agent.core.graph import MODEL_NODE, TOOLS_NODE
from rolecard_agent.core.guard import check
from rolecard_agent.core.nodes import EmptyModelStream, TurnStopped, VisionNotSupported
from rolecard_agent.core.observability import TraceEvent, Tracer, scrub_endpoints
from rolecard_agent.core.text import text_of
from rolecard_agent.core.thread_locks import (
    ThreadBusy,
    clear_stop,
    inflight_append,
    inflight_begin,
    inflight_end,
    inflight_replace,
    release_thread,
    request_stop,
    stop_requested,
    try_thread_write,
)
from rolecard_agent.core.usage import TokenUsage, usage_from_metadata

# 回扣不投送的字符数。必须 >= 最长触发式（最宽约 21 字：主语 + 8 填充 + 能愿动词 + 8 填充
# + 动作动词），32 留足余量又察觉不到渲染延迟。
WINDOW = 32

# 供应商对"把图片发给不支持视觉的模型"的报错形态各家不一（siliconflow 20041 / OpenAI
# 兼容 / Ollama 文案都不同），但都围绕 "VLM / vision / image / text-only" 这几个词。
# 命中即翻译成可操作提示，而不是笼统的"模型调用失败"——用户据此知道是"含图会话切到了
# 纯文本模型"，该切回视觉模型或新开不含图的会话（用户 2026-09-19 报的 bug）。
_VISION_MISMATCH_SIGNALS = (
    "not a vlm",
    "vision language model",
    "text-only prompt",
    "does not support image",
    "does not support vision",
    "unsupported image",
    "image not supported",
    "input does not contain any image",
)

VISION_MISMATCH_DETAIL = (
    "当前模型不支持图片识别（视觉）。这条对话里含有图片，请切换到支持视觉的模型"
    "（例如本地 qwen3-vl）后再问；或新开一条不含图片的会话来使用当前模型。"
)

_GENERIC_MODEL_FAILURE = "模型调用失败，请稍后重试或换一种问法。"

#: 模型一个字都没吐时该说的话：与通用的"换一种问法"刻意不同（`R28-06`）。
EMPTY_STREAM_DETAIL = "模型这一轮没有返回任何内容。重试一次，或换一个模型 —— 不是你的问法问题。"


def model_error_detail(exc: Exception) -> str:
    """把模型调用异常映射成给用户的可读提示。纯函数，便于脱机测试。"""
    # 调用前拦截（P1-2）与"供应商 400 后才认出来"是**同一个条件**，所以必须是同一句话；
    # 差别只在轨迹里（`vision_blocked_pre_call` 只有前者才会有）。
    if isinstance(exc, VisionNotSupported):
        return VISION_MISMATCH_DETAIL
    if isinstance(exc, EmptyModelStream):
        # 空响应要单独立一句（R28-06）：那句通用的"换一种问法"会把人推向**重复问**，
        # 而这一次根本不是问法的问题 —— 后端一个字都没吐，换问法照样空。
        return EMPTY_STREAM_DETAIL
    text = str(exc).lower()
    if any(sig in text for sig in _VISION_MISMATCH_SIGNALS):
        return VISION_MISMATCH_DETAIL
    return _GENERIC_MODEL_FAILURE


# ---------------------------------------------------------------- 轮次事件
#
# 每个事件类自带 `sse_type`（投送给浏览器时的那个 wire 名）。它是**词表的唯一声明处**：
# HTTP 壳据此帧化，桌宠壳据此映射成原生事件，而 `tests/unit/test_frontend_contract.py`
# 拿它去比对 `frontend/src/lib/stream.ts` 的 switch 分支 —— 前后端事件名漂移曾是一类
# 静默故障（谁改名谁自己知道，浏览器就只是"少一类渲染"）。


@dataclass(frozen=True, slots=True)
class Start:
    """一轮开始。`role` 是角色摘要（id/名字/模型），客户端据此点亮头像。"""

    sse_type: ClassVar[str] = "start"
    role: dict[str, str]


@dataclass(frozen=True, slots=True)
class Thinking:
    """思考模型的推理增量。与正文**分流**：UI 收进折叠面板，不混进回答。"""

    sse_type: ClassVar[str] = "thinking"
    text: str


@dataclass(frozen=True, slots=True)
class Token:
    """一段已过审、可以显示的正文增量。"""

    sse_type: ClassVar[str] = "token"
    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型这一轮提交的工具调用（在工具真正执行之前投送，UI 才能显示"进行中"）。"""

    sse_type: ClassVar[str] = "tool_call"
    name: str | None
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行结果。内容按内核的失败三分类已是用户可读文本，不含内部细节。"""

    sse_type: ClassVar[str] = "tool_result"
    name: str | None
    content: str


@dataclass(frozen=True, slots=True)
class MessageReplace:
    """权威文本：已提交内容与已投送内容不一致时（guard 改写 / 兜底句 / 非流式后端），
    客户端用它**整条替换**当前气泡。"""

    sse_type: ClassVar[str] = "message_replace"
    text: str


@dataclass(frozen=True, slots=True)
class ContextTrimmed:
    """历史预算挤掉了旧消息。只承载"发生了什么、多少条"，不含任何对话内容；
    每**用户轮次**最多一条（见 `run_turn` 的 trim_reported）。"""

    sse_type: ClassVar[str] = "context_trimmed"
    dropped: int
    kept: int


@dataclass(frozen=True, slots=True)
class Error:
    """这一轮以失败结束。`detail` 是给用户的一句人话；原因只进日志（见 tracer 调用点）。"""

    sse_type: ClassVar[str] = "error"
    detail: str


@dataclass(frozen=True, slots=True)
class End:
    """流收尾。无论成功失败都会发 —— 客户端靠它结束"生成中"态。

    `stopped=True` 说清"这一轮是用户叫停的"：不是失败（不发 Error），但界面该把气泡标成
    已停止而不是"生成完了"。中途停时历史里已经有那半截（`call_model` 提交的就是它），
    所以这里只是补一个来源说明。
    """

    sse_type: ClassVar[str] = "end"
    stopped: bool = False


TurnEvent = (
    Start
    | Thinking
    | Token
    | ToolCall
    | ToolResult
    | MessageReplace
    | ContextTrimmed
    | Error
    | End
)

#: 词表（测试与文档用它，不手抄字符串）。
EVENT_TYPES: tuple[str, ...] = (
    Start.sse_type,
    Thinking.sse_type,
    Token.sse_type,
    ToolCall.sse_type,
    ToolResult.sse_type,
    MessageReplace.sse_type,
    ContextTrimmed.sse_type,
    Error.sse_type,
    End.sse_type,
)


class StreamingGuard:
    """单轮模型输出的增量式 fail-closed 审核。

    与客户端的契约：文本只通过本守卫允许的事件到达，所以完整的违规式永远不会上屏；
    权威文本始终由模型节点的已提交更新给出，客户端把它当最终真相。
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.emitted = 0
        self.blocked = False

    def feed(self, text: str) -> str:
        """累加一个块；返回可投送的增量（无事可发时返回 ""）。

        每块都对**整个** buffer 重跑 `check()`，这不是省事的近似而是正确的增量语义：
        一个式子只有**完整**出现在文本里才可检，而 buffer 里出现完整式子恰好就是全文检查
        会抓到的那个东西。
        """
        self.buffer += text
        if self.blocked:
            return ""
        verdict = check(self.buffer)
        if not verdict.allowed:
            self.blocked = True
            return ""
        safe = max(0, len(self.buffer) - WINDOW)
        if safe <= self.emitted:
            return ""
        delta = self.buffer[self.emitted : safe]
        self.emitted = safe
        return delta

    def flush_tail(self) -> str:
        """放出被窗口扣住的部分。只在该轮已提交文本过了全文守卫后调用一次
        （那才是让整个 buffer 都安全、包括尾巴的依据）。"""
        if self.blocked:
            return ""
        delta = self.buffer[self.emitted :]
        self.emitted = len(self.buffer)
        return delta

    def reset(self) -> None:
        """下一轮模型调用从干净累加器开始。"""
        self.buffer = ""
        self.emitted = 0
        self.blocked = False


def run_turn(
    graph: Any,
    *,
    graph_input: dict[str, Any],
    config: dict[str, Any],
    role_summary: dict[str, str],
    tracer: Tracer | None = None,
    usage_recorder: Callable[[TokenUsage | None], None] | None = None,
    after_turn: Callable[[], None] | None = None,
) -> Iterator[TurnEvent]:
    """把一个用户轮次跑过内核图，产出结构化事件流（同步，宿主无关）。

    **整轮占住这个会话的写入锁**（`core/thread_locks.py`，审计 #12）：调度线程的主动投递
    走的是 `graph.update_state`，它读的可能是这一轮开始**之前**的检查点，两边分叉同一个父节点
    时后写的会盖掉先写的 —— 用户报的"我发的一条消息被吞了"就是这么来的。

    等锁超过 `_TURN_LOCK_WAIT` 时**明确拒绝**而不是无锁照跑（2026-10-04 审查快照、用户
    拍板）：旧语义"宁可罕见地分叉，也不拒掉用户这句话"在本地 8B 长轮（>150s 常见）+
    双窗口同发的现实下，就是消息被静默覆盖 —— 而且发生时没有任何信号。现在短等待后给
    一句人话的 `Error` 帧（SSE 已 200，状态码改不了；前端照 error 帧显示），用户重发即可
    —— edit/delete 走 409、chat 走 error 帧，同一把锁同一套兜底哲学。
    """
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
    # 上一句的"停"绝不能顺延到这一句：旗子是在这一轮的开始处清，不是在上一轮的结束处清 ——
    # 结束处清会赶上"用户立刻发了下一句"那种接力，两边抢同一个键。
    # 但"开始处"必须在**拿到写锁之后**（09-26 轮 R26-02）：上一轮断连时那记 `request_stop`
    # 是它自己 `finally` 里补的，而它跑在 `release_thread` **之前** —— 也就是说锁还在它手里。
    # 先清后抢的话，新一轮可能在自己的清理之后才看见上一轮补下的旗子，于是白吃一个"停"、
    # 整轮什么都不产出（`nodes.py` 看见旗子就直接 `TurnStopped`）。抢到锁才清，
    # 顺序就被互斥保证了：上一轮的收尾一定在锁里做完，它的旗子一定落在清理之前。
    held = try_thread_write(thread_id, timeout=_TURN_LOCK_WAIT)
    if held:
        # R28-01：上面那段注释说的"抢到锁才清"，代码原先是**无条件**清的 —— 等锁超时时
        # 上一轮还在飞，这一清就把它对用户那个「停止」按钮的承诺抹掉了（症状：按了停止
        # 她还在说，而那一轮的生成继续在线程池里烧）。没抢到锁就不清：旗子仍归在飞那轮。
        clear_stop(thread_id)
    if not held:
        # 拿不到锁 = 上一轮还在飞：明确拒绝，绝不无锁分叉（2026-10-04 审查快照、用户拍板，
        # 翻掉旧 docstring"宁可罕见地分叉"的取舍）。SSE 已 200 状态码改不了，409 的语义
        # 走 error 帧表达 —— detail 与 HTTP 409 的 ThreadBusy 文案同源，前端照 error 帧显示。
        if tracer is not None:
            tracer.emit(
                TraceEvent(
                    event="thread_lock_timeout",
                    node="run_turn",
                    thread_id=thread_id,
                    detail={"waited_s": _TURN_LOCK_WAIT, "action": "rejected"},
                )
            )
        yield Error(detail=str(ThreadBusy(thread_id, waited=_TURN_LOCK_WAIT)))
        return
    # `ended[0]` 由正文在发出 `End` 的那一刻立起来。它留着的唯一用途是分辨
    # **"这一轮自己跑完了"** 与 **"没人要它了"**（客户端关页面 / 断连）：
    # 后者要顺手把这轮叫停 —— 不然生成会继续在线程池里跑到天荒地老（#18 的另一半：
    # 用户以为"关掉窗口就停了"，实测那边还在烧）。
    ended = [False]
    # 在飞登记挂在**这里**而不是各个投送点：`_iter_turn` 有三处发正文（增量、过审尾巴、
    # 整条替换），漏一处就是"那个来源的字在另一个界面上永远不出现"。收成一个收口之后，
    # 任何将来新增的投送点都自动被登记 —— 登记的内容严格等于投送出去的内容，所以那条
    # fail-closed 的窗口纪律（`WINDOW` 个字符不提前给第二个读者看）一格不差地照用。
    inflight_begin(thread_id)
    try:
        for event in _iter_turn(
            graph,
            graph_input=graph_input,
            config=config,
            role_summary=role_summary,
            tracer=tracer,
            usage_recorder=usage_recorder,
            ended=ended,
        ):
            if isinstance(event, Token):
                inflight_append(thread_id, event.text)
            elif isinstance(event, MessageReplace):
                inflight_replace(thread_id, event.text)
            yield event
    finally:
        # 先清登记再叫停、再放锁：另一个界面读的要是这一轮已经收手的字，
        # 就只剩"检查点里那句已提交的"这一条真相可看。
        inflight_end(thread_id)
        if not ended[0]:
            request_stop(thread_id)
        # 走到这里 held 恒为 True（拿不到锁的那条路在上面已经 yield Error 返回），
        # 只放自己持有的这把。
        release_thread(thread_id)
        # 轮后钩子（自动记忆提取）从投送层挪到这里 —— **单一收尾点**（2026-10-04 审查
        # 快照的收尾不对称条目）：从前它挂在 async 生成器的正常完成路径上，客户端断线
        # 抛 GeneratorExit 时被跳过，断线轮次的提取静默缺失；而锁与停止旗的收尾都在
        # 这里、断线时照跑。现在三件事同一层：无论正常结束、失败还是断线，钩子都会执行。
        # 检查点此刻已提交（stream 已走完），提取看到的是完整一轮。
        if after_turn is not None:
            # 钩子**同步**交还调用方（宿主自己决定怎么跑：后台池、线程、原地）。内核
            # 不再自己起线程 —— 提取宿主给一个池，提取宿主就用固定几个线程的连接，
            # ThreadLocalConnection 的槽从此有界（fire-and-forget 线程的每轮一格
            # 正是连接泄漏的根，2026-10-04 审查快照的连接泄漏条目）。
            after_turn()


#: 一轮等锁的上限（秒）：短等待。拿不到就明确拒绝（error 帧，409 的 SSE 形态），
#: 不再"等满 150s 后无锁照跑"—— 那在本地 8B 长轮 + 双窗口下就是消息静默覆盖。
_TURN_LOCK_WAIT = 5


def _iter_turn(
    graph: Any,
    *,
    graph_input: dict[str, Any],
    config: dict[str, Any],
    role_summary: dict[str, str],
    tracer: Tracer | None = None,
    usage_recorder: Callable[[TokenUsage | None], None] | None = None,
    ended: list[bool] | None = None,
) -> Iterator[TurnEvent]:
    """`run_turn` 的正文：一轮事件流本身（锁与"没人要了就叫停"在外层那半边，见上）。

    同步实现是**必须的**而不是选择：项目的检查点是同步 `SqliteSaver`，其 async 对应实现会抛
    `NotImplementedError`。需要异步投送（SSE 不占事件循环）的宿主用 `api/chat.py` 的线程池桥。

    工具轮的事件序：`Start → [Thinking]* → ToolCall → ToolResult → (Token)* → End`
    直答轮的事件序：`Start → [Thinking]* → (Token)* → End`

    `Thinking` 只在模型是"按名单启用思考"的推理模型时出现（见 config 的 MODEL_THINKING_MODELS）。
    `MessageReplace` 在已提交文本与已投送文本分叉时出现（guard 改写、角色缺失兜底、或该模型
    根本没发任何增量块）。
    """
    yield Start(role=role_summary)
    guard = StreamingGuard()
    # 这一轮是不是用户叫停的（两种：模型调用没开始就停 = 异常；开始后被停 = 旗子还立着）。
    stopped_early = False
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
    # 一次用户轮次里 `call_model` 可能跑多次（工具循环）。裁剪只报**第一次**：那一轮代表
    # "这一问开始时模型能看到多少历史"，是用户需要知道的那个事实；后面几次的数值是工具
    # 消息把窗口挤得更满的结果，重复上报只会变成噪音。
    trim_reported = False
    think_emitted = False
    # 每次模型调用的真用量，键是 (节点, 步)。分块带的是累计值，所以"后到的覆盖先到的"
    # 才是那一次调用的数（见 `_from_message_chunk`）。
    usage_seen: dict[Any, TokenUsage | None] = {}
    try:
        for mode, payload in graph.stream(
            graph_input, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                yield from _from_message_chunk(payload, guard=guard, usage_seen=usage_seen)
                continue
            for node, update in (payload or {}).items():
                if node == MODEL_NODE:
                    messages = (update or {}).get("messages") or []
                    if not messages:
                        continue
                    # 历史被上下文预算裁剪过 → 如实告诉客户端（审查报告 H3 的界面部分）。
                    # 事件只承载"发生了什么、多少条"，不含任何对话内容。
                    dropped = (update or {}).get("context_trimmed") or 0
                    if dropped and not trim_reported:
                        trim_reported = True
                        yield ContextTrimmed(
                            dropped=dropped, kept=(update or {}).get("context_kept") or 0
                        )
                    committed = messages[-1]
                    # 兜底路径：模型没有走增量流（评测脚本 / 非流式后端）时，思考内容会
                    # 完整地落在 committed 上 —— 此时一次性发出，并以 think_emitted
                    # 防止与流式增量重复。
                    committed_think = (getattr(committed, "additional_kwargs", None) or {}).get(
                        "reasoning_content"
                    )
                    if committed_think and not think_emitted:
                        think_emitted = True
                        yield Thinking(text=str(committed_think))
                    for call in getattr(committed, "tool_calls", None) or []:
                        yield ToolCall(name=call.get("name"), args=call.get("args") or {})
                    text = text_of(committed)
                    if guard.blocked or text != guard.buffer:
                        # 已提交文本与已投送文本分叉：客户端用权威（安全）文本整条替换
                        # 进行中的气泡。
                        yield MessageReplace(text=text)
                    else:
                        tail = guard.flush_tail()
                        if tail:
                            yield Token(text=tail)
                    guard.reset()
                elif node == TOOLS_NODE:
                    yield from _from_tool_update(update)
    except TurnStopped:
        # 用户按了停止，而这一轮的模型调用**还没开始**（多半是停在工具跑着的那段路上）。
        # 不发 Error：那不是失败；也不会有新消息进历史：节点根本没跑完一次生成。
        # 仍然往下走用量结账 —— 一次用户轮次里可能已经跑过几轮工具调用，那些 token 是真花了。
        stopped_early = True
    except GraphRecursionError:
        # 工具循环撞上步数上限（core/graph.build_graph_config 设的 recursion_limit）。
        # 这不是"模型调用失败"——模型一直在正常回话，是它陷入了重复调用，所以必须
        # 说清"发生了什么、怎么绕开"，否则用户只会反复重试同一个问法。
        limit = config.get("recursion_limit")
        _emit_error_trace(
            tracer,
            f"GraphRecursionError: 超过步数上限 {limit}",
            {"node": MODEL_NODE, "reason": "recursion_limit"},
        )
        yield Error(
            detail=(
                f"这一轮的工具调用超过了 {limit} 步上限，已自动停止"
                "（通常是模型陷入了重复调用）。换个问法，或把任务拆小一点再试。"
            )
        )
    except Exception as exc:  # noqa: BLE001 - 客户端拿一句人话，日志拿真实原因
        # 带上消息体（审查报告 E1）：只记异常类型名等于回答不了"这次为什么失败" ——
        # 超时、连接被拒、模型不存在在日志里长得一模一样。异常文本不承载报告原文，
        # 且 URL 会被脱敏，因此脱敏纪律不受影响。
        _emit_error_trace(
            tracer,
            f"{type(exc).__name__}: {scrub_endpoints(str(exc))}"[:300],
            {"node": MODEL_NODE},
        )
        yield Error(detail=model_error_detail(exc))
    # 用量在 End 之前结账：这一轮可能跑了多次模型调用（工具循环），逐次留痕并交给宿主记账。
    # 放在这里是唯一能看到真值的地方 —— `call_model` 拿到的已经是被逐块相加污染过的合并值。
    ordered = sorted(usage_seen.items(), key=lambda kv: (kv[0][1] is None, kv[0][1] or 0))
    for (node, step), usage in ordered:
        if tracer is not None:
            tracer.emit(
                TraceEvent(
                    event="llm_usage",
                    node=str(node),
                    tokens=usage.total if usage is not None else None,
                    detail={
                        "step": step,
                        "prompt_tokens": usage.prompt if usage is not None else None,
                        "completion_tokens": usage.completion if usage is not None else None,
                        # completion 的子集，单独一列才看得出"这条回复有多少在想"。
                        "reasoning_tokens": usage.reasoning if usage is not None else None,
                    },
                )
            )
        if usage_recorder is not None:
            usage_recorder(usage)
    # 中途收手时旗子还立着（节点自己看见它才停的手）；没开始就停是上面那条 TurnStopped。
    if ended is not None:
        ended[0] = True
    yield End(stopped=stopped_early or stop_requested(thread_id))


def _from_message_chunk(
    payload: Any,
    *,
    guard: StreamingGuard,
    usage_seen: dict[Any, TokenUsage | None] | None = None,
) -> Iterator[TurnEvent]:
    """`messages` 模式的一个原始块 → 思考事件 + 已过审的正文增量（并顺手记一次用量）。

    用量为什么在这里取而不是在 `call_model` 里取（审计 §12.8/#8）：
    **供应商在每一个流式分块里都回一份"累计到目前为止"的 usage**，而 langchain 合并
    `AIMessageChunk` 时是把这些逐块**相加**的 —— 于是 `call_model` 拿到的那个合并值是
    `真值 × 分块数`（实测一条"在吗"：非流式 26 token，流式合并后 272,607）。
    只有这里看得见单个分块，所以真值只能在这儿取：**按 (节点, 步) 取最后一次出现的累计值**
    —— 第一次不行，第一块的 `output_tokens` 恒为 0。
    """
    chunk, meta = payload
    if usage_seen is not None and isinstance(chunk, AIMessageChunk) and isinstance(meta, dict):
        usage = usage_from_metadata(getattr(chunk, "usage_metadata", None))
        # 键用 (langgraph_node, langgraph_step)：一次用户轮次里 `call_model` 会因
        # 工具循环跑多次，这两样正好把每次调用分开。`run_id` 在 langgraph 的 messages
        # 元数据里**不存在**（实测只有 checkpoint_ns / node / step / triggers）。
        key = (meta.get("langgraph_node"), meta.get("langgraph_step"))
        # 没带用量的块也要把这次调用登记上（值为 None）：`calls` 少记一次，账上的
        # "今天 0 token"就又同时意味着"没花钱"和"报了账但没数"这两种完全不同的事。
        if usage is not None or key not in usage_seen:
            usage_seen[key] = usage
    # 思考模型的推理增量（langchain-ollama：reasoning=True 时思考进
    # additional_kwargs['reasoning_content']）。与正文分流：思考走 thinking 事件、进折叠
    # 面板，不混进回答正文。qwen3-vl / 云端 DeepSeek 这类思考模型这里是关键分流 ——
    # 否则思考 token 会被当成回答的字数报给用户（审计 §12.10 那次误判的一半成因）。
    think_delta = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
    if isinstance(chunk, AIMessageChunk) and think_delta:
        yield Thinking(text=str(think_delta))
    if not isinstance(chunk, AIMessageChunk) or not chunk.content:
        return
    delta = guard.feed(text_of(chunk))
    if delta:
        yield Token(text=delta)


def _from_tool_update(update: Any) -> Iterator[TurnEvent]:
    for message in (update or {}).get("messages") or []:
        if isinstance(message, ToolMessage):
            yield ToolResult(name=message.name, content=text_of(message))


def _emit_error_trace(
    tracer: Tracer | None, error: str, detail: dict[str, Any]
) -> None:
    if tracer is not None:
        tracer.emit(TraceEvent(event="chat_error", error=error, detail=detail))


#: 宿主侧只需要的一个签名：跑一轮拿事件流（桌宠壳据此不必 import 任何 HTTP 件）。
TurnRunner = Callable[..., Iterator[TurnEvent]]
