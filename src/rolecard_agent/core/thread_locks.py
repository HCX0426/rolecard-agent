"""同一个会话线程的写入必须排队（审计 §12 的 #12：主动开口会吞掉用户的一条消息）。

症状是用户报的："我在'主动找我'那条会话里回话时，正好触发了她的主动开口，
我发的一条消息就没了。"

成因不是模型，也不是调度策略，是**两条路同时写同一份检查点**：

  * 用户那一轮：`core/turn.run_turn` → `graph.stream(...)`，LangGraph 在每个超步写一次检查点；
  * 她主动那句：`core/bootstrap.deliver_proactive` → `graph.update_state(...)`，调度线程里跑。

`update_state` 是"读最新检查点 → 追加 → 写回"。如果它读到的正是这一轮**开始之前**的那个父节点，
它写出来的分支就把这一轮已经追加进去的用户消息盖掉了（谁后写谁赢）。用户看到的就是一条
凭空消失的消息 —— 而检查点里那条分支还在，所以界面上翻不到、日志里也说不清。

这里给每个 `thread_id` 一把进程内的锁，两条路都拿它。为什么不用数据库事务解决：检查点写入
跨多次 `sqlite` 语句（`checkpoints` + `writes` + blob），LangGraph 的 `update_state` 与
`stream` 各自管自己的事务，把它们并进同一个隔离级别要改的是依赖库；而"同一会话同一时刻只有一
个写者"本来就是这条链路的语义（一个人不会同时和你聊两轮）。

锁是**进程内**的：这台机器上后端只有一个进程（分发形态 A：随包后端、只绑 127.0.0.1），
所以够用；如果哪天变成多进程，这条要换成库级的乐观并发检查，别假装它跨进程。
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

#: 锁表条目的告警阈值。**只告警，不回收**（2026-10-04 审查快照的锁表竞态条目）：旧实现会在超限时
# 回收"当前没被持有"的锁，而"取出锁对象→acquire"之间没有任何引用计数 —— 另一线程恰在
# 这个间隙把锁 pop 掉，第三个线程就会为同一 thread_id setdefault 出**新锁**，两把锁同时
# 写同一检查点，互斥瓦解（后写盖先写，与 docstring 记的"吞消息"同型）。每把锁 ~48 字节，
# 一万个会话不到 1MB：宁可无界增长到告警，也不换互斥性。哪天真要省内存，做引用计数
# （refs==0 且 !locked() 才可回收），不要按 locked() 现场判断。
_MAX_TRACKED = 65536

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()
_warned_full = False


def _lock_for(thread_id: str) -> threading.Lock:
    global _warned_full
    with _guard:
        lock = _locks.get(thread_id)
        if lock is None:
            if len(_locks) >= _MAX_TRACKED and not _warned_full:
                # 只响一次：这是"锁表大得反常"的信号（一万+ 个 thread_id），不是常规路径。
                _warned_full = True
                print(
                    f"[thread-locks] 锁表达到 {_MAX_TRACKED} 条且不再回收"
                    "（2026-10-04 审查快照：按 locked() 回收有互斥竞态）"
                    " —— 检查是不是 thread_id 在无界生成",
                    file=sys.stderr,
                    flush=True,
                )
            lock = _locks.setdefault(thread_id, threading.Lock())
        return lock


class ThreadBusy(RuntimeError):
    """这个会话等不到写锁了（别人正持有超过 `thread_write` 的等待上限）。

    为什么要有这个类型而不返回布尔（09-28 轮 `R28-02`/`R28-03`）：`with thread_write(tid):`
    这种写法**没法不带上分支**，所以六处写检查点的地方里有五处直接把 yield 的布尔丢了 ——
    拿不到锁时 `update_state` 照样无互斥执行，正是本模块 docstring 记的那个"吞消息"形状。
    接口能误用而调用方会误用，那就改接口：**要么拿到锁，要么在写之前炸**，
    不给"忘了判断"留任何一条路。想自己决定等多久、拿不到就跳过的，用 `try_thread_write`。
    """

    def __init__(self, thread_id: str, *, waited: float) -> None:
        super().__init__(f"会话 {thread_id} 等写锁等了 {waited:.0f} 秒还没轮到")
        self.thread_id = thread_id
        self.waited = waited


@contextmanager
def thread_write(thread_id: str, *, timeout: float | None = None) -> Iterator[None]:
    """占住这个会话的写入；等不到就抛 `ThreadBusy`（**不会**带着没锁的状态往下走）。

    不直接抛 HTTPException：这一层不认识 FastAPI。路由侧由 `api/main.py` 注册的处理器
    统一翻成 409 + 一句人话 —— 六个写检查点的口子共用同一个出口，而不是各写各的 try。

    `timeout=None` 用 `_DEFAULT_WAIT`（比 model_timeout 略长，保证"排队"不是"丢掉一轮"）；
    批量清理那种希望快点失败的，自己传一个短的。
    """
    if not thread_id:
        # 没有线程 id（单测直接调节点、或内核装配阶段）⇒ 没有可串行的对象，照常跑。
        yield
        return
    wait = _DEFAULT_WAIT if timeout is None else timeout
    lock = _lock_for(thread_id)
    if not lock.acquire(timeout=wait):
        raise ThreadBusy(thread_id, waited=wait)
    try:
        yield
    finally:
        lock.release()


#: 对话那一轮等锁的默认上限：比 `model_timeout`(120s) 略长，保证"排队"不是"丢掉一轮"。
_DEFAULT_WAIT = 150


def try_thread_write(thread_id: str, *, timeout: float = 0.0) -> bool:
    """非阻塞地问一句"现在能写这个会话吗"，能就**占住**（配合 `release_thread` 用）。

    给调度侧用：它不该为了投递一句话阻塞几十秒（那会拖住别的角色的开口时机）。
    """
    if not thread_id:
        return True
    return _lock_for(thread_id).acquire(timeout=timeout)


def release_thread(thread_id: str) -> None:
    """`try_thread_write` 的另一半。没持有过就是 RuntimeError —— 调用点都成对写死。"""
    if not thread_id:
        return
    lock = _locks.get(thread_id)
    if lock is not None and lock.locked():
        lock.release()


def thread_is_busy(thread_id: str) -> bool:
    """这个会话此刻有没有写者在跑（给"别打断正在进行的对话"那条判断用）。"""
    if not thread_id:
        return False
    with _guard:
        lock = _locks.get(thread_id)
    return bool(lock and lock.locked())


# ---------------------------------------------------------------- 提取的"在飞"标记
#
# 故意**不是** `thread_write` 的那把锁：后台提取要跑 10–120 秒，如果它去抢会话写入锁，
# 用户下一句话就得排队等它（`run_turn` 的等锁上限 150s > `model_timeout` 120s ⇒ 本地那次
# 基本就是等满）。§12.7 实测过"自动提取不拖慢下一轮"，靠的正是它不进这把锁 ——
# 所以去重提取需要的是另一个只问"有没有同类在跑"的标记，双方都不阻塞对方。

_extracting: set[str] = set()


def try_extraction(thread_id: str) -> bool:
    """没有同会话的提取在跑就占上它（True）；已经有一次在跑就 False。

    一次提取 = 一次真模型调用，而游标只在**调用结束**时才推进：不挡的话，用户在提取跑的
    那几十秒里每发一句都会另起一次看到同一个窗口的提取（2026-09-24 副本实测八轮跑了 4 次，
    每次都把 prompt 里的【已有条目】抄一点回来）。挡住就是了 —— 兜底下一轮还会再问。
    """
    if not thread_id:
        return True
    with _guard:
        if thread_id in _extracting:
            return False
        _extracting.add(thread_id)
        return True


def end_extraction(thread_id: str) -> None:
    """`try_extraction` 的另一半。没占过就静默（它只是一枚标记，不该制造新的失败模式）。"""
    if not thread_id:
        return
    with _guard:
        _extracting.discard(thread_id)


# ---------------------------------------------------------------- 停止生成的取消信号
#
# 与上面两样都不是一回事：锁管"谁先写这份检查点"，提取标记管"有没有同类在跑"，这里管
# **"这一轮还要不要继续"**。生命周期也不同 —— 它不由下令的人释放，而是在**下一轮开始时**
# 清掉（上一句的"停"绝不能顺延到下一句）。
#
# 为什么需要单独一枚旗子而不是"把 SSE 连接关掉就完事"（2026-09-24 实测推翻的假设，
# 审计 §12.12②）：`await run_in_executor(next, gen)` 被取消**不会**中断线程池里已经在跑的
# 那次 `next()` —— 节点继续跑到提交，模型继续吐字。所以取消必须走到**分块边界**上，
# 由正在消费流的那一层自己收手。

#: 有序字典当集合用：Python 的 dict 保持插入序，于是"最旧的先扔"做得到。
#: 从前这里是 `set` + 超上限 `.clear()`，而 clear 会把**所有**在飞的停一把抹掉 ——
#: 包括上一微秒刚按下的那一次。09-26 轮 R26-13 记的就是这个。
_stopped: dict[str, None] = {}


def request_stop(thread_id: str) -> None:
    """给这个会话下"停"的手势。

    从前它返回 `bool`（"这次是不是新增的"）并在文档里承诺"调用方据此决定要不要留痕"，
    而两处调用方（`sessions.py:486` 与 `turn.py:305`）都把返回值丢掉 —— 一个没人兑现的
    承诺比没有承诺更容易骗到下一个人（R26-13）。签名照实收成 None。
    """
    if not thread_id:
        return
    with _guard:
        if len(_stopped) > _MAX_TRACKED:
            # 停过又再没开过口的会话会一直留一枚旗子。忘了它是安全的（最坏是某一轮没被
            # 取消），而攒成无界集合是不安全的 —— 宁可忘，不可长。但"忘"按**最旧的先扔**，
            # 不是一把清光。
            for stale in list(_stopped)[: _MAX_TRACKED // 2]:
                _stopped.pop(stale, None)
        _stopped[thread_id] = None


def stop_requested(thread_id: str) -> bool:
    """这一轮该不该收手。消费循环每个边界问一次 —— 它只是一次集合查找。"""
    if not thread_id:
        return False
    with _guard:
        return thread_id in _stopped


def clear_stop(thread_id: str) -> None:
    """一轮**开始**时调用：把上一句留下的"停"擦掉。"""
    if not thread_id:
        return
    with _guard:
        _stopped.pop(thread_id, None)


# ---------------------------------------------------------------- 正在生成的那一句
#
# 为什么需要它（2026-09-26 用户报"桌宠发的回答，对话界面同步得有些慢"，实测拆解）：
# LangGraph 每个**超步**才写一次检查点，而助手整句要等 `call_model` 返回才算一条消息 ——
# 副本库上自起后端量到的一轮（419 字、云端档）：他那句 0.21 秒就可读，她的整句 10.49 秒
# 才进检查点，界面那个 5 秒网格把它推到 15.0 秒。也就是说 12.1 秒的落后里，
# **7.6 秒是"第二个读者全程读不到她正在说"**，改轮询频率治不到它。
#
# 存的是**已经过守卫投送出去**的那段增量（与桌宠屏幕上已有的字严格一致），不是
# `StreamingGuard.buffer`：尾巴那 WINDOW 个字符是还没过窗口检查的，提前让另一个读者看见
# 就等于绕过了 fail-closed 的纪律。
#
# 键存在 = 这一轮在飞（哪怕还没有字），所以"她在打字"这个状态本身也是可读的。

_inflight: dict[str, str] = {}


def inflight_begin(thread_id: str) -> None:
    """一轮开跑。与 `inflight_end` 成对，配对由 `run_turn` 的 `finally` 保证。"""
    if not thread_id:
        return
    with _guard:
        _inflight[thread_id] = ""


def inflight_append(thread_id: str, delta: str) -> None:
    """投送了一段正文增量。没在飞就静默（宿主可以不开这一路）。"""
    if not thread_id or not delta:
        return
    with _guard:
        if thread_id in _inflight:
            _inflight[thread_id] += delta


def inflight_replace(thread_id: str, text: str) -> None:
    """权威文本整条替换（与客户端的 `MessageReplace` 同一个契约）。"""
    if not thread_id:
        return
    with _guard:
        if thread_id in _inflight:
            _inflight[thread_id] = text


def inflight_end(thread_id: str) -> None:
    """一轮收尾：无论成功、失败、被停、还是没人要了，这一段都不该再被别人看见。"""
    if not thread_id:
        return
    with _guard:
        _inflight.pop(thread_id, None)


def inflight_text(thread_id: str) -> str | None:
    """此刻正在生成的那段字（已投送部分）。`None` = 这一轮没在飞。"""
    if not thread_id:
        return None
    with _guard:
        return _inflight.get(thread_id)


__all__ = [
    "ThreadBusy",
    "clear_stop",
    "end_extraction",
    "inflight_append",
    "inflight_begin",
    "inflight_end",
    "inflight_replace",
    "inflight_text",
    "release_thread",
    "request_stop",
    "stop_requested",
    "thread_is_busy",
    "thread_write",
    "try_extraction",
    "try_thread_write",
]
