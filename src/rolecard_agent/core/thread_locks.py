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

import threading
from collections.abc import Iterator
from contextlib import contextmanager

#: 一个会话最多攒多少把锁：条目只增不减会漏内存，所以清掉没人持有的；见 `_lock_for`。
_MAX_TRACKED = 512

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock_for(thread_id: str) -> threading.Lock:
    with _guard:
        lock = _locks.get(thread_id)
        if lock is None:
            if len(_locks) >= _MAX_TRACKED:
                # 只回收"当前没被持有"的：正在跑的那一轮的锁绝不能抽走，否则两边各拿一把
                # 新锁，互斥就没了（这正是要修的那个 bug）。
                free = [tid for tid, held in _locks.items() if not held.locked()]
                for stale in free[: len(_locks) // 2]:
                    _locks.pop(stale, None)
            lock = _locks.setdefault(thread_id, threading.Lock())
        return lock


@contextmanager
def thread_write(thread_id: str) -> Iterator[bool]:
    """占住这个会话的写入。** yields** True = 拿到了；False = 没拿到（调用方自己决定等多久）。

    不直接抛：拿不到锁是正常情况（用户那一轮正在跑），调度侧的正确答案是"这次不插话"，
    而不是一句"内部错误"。
    """
    if not thread_id:
        # 没有线程 id（单测直接调节点、或内核装配阶段）⇒ 没有可串行的对象，照常跑。
        yield True
        return
    lock = _lock_for(thread_id)
    acquired = lock.acquire(timeout=_DEFAULT_WAIT)
    try:
        yield acquired
    finally:
        if acquired:
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


__all__ = [
    "clear_stop",
    "end_extraction",
    "release_thread",
    "request_stop",
    "stop_requested",
    "thread_is_busy",
    "thread_write",
    "try_extraction",
    "try_thread_write",
]
