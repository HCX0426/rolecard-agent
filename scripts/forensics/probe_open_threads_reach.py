""""未收尾话题"的**可达性**验收（09-26 轮 R26-03 规定的那一步能证伪的动作）。

与 `probe_open_threads_live.py` 的分工不同，而且这个区别就是要害：

* 那份探针把**手写的对话文本**直喂 `_PROMPT`，量的是"模型会不会挖话题 / 守不守格式"；
* 本探针一句对话都不手写，素材来自生产那条链 —— `Runtime.proactive_recent_lines`
  （读检查点 → 按"最后说话的是谁"切 `unanswered_lines` / `unreplied_lines` → 对应的格式化器），
  触发源排序与抑制层全部走真实代码（`ReachoutScheduler.tick_once`）。量的是
  "**这一次扫描到底会不会发生**"。

昨天"已验收 5/5"与真库 `open_threads_at` 至今为 NULL 能同时成立，就是因为只做过前者。

模型侧是记录型替身：只记下"被喂了什么"，回 `NONE` ⇒ 不写话题、不调云端、不花钱。
实验全部跑在**真库副本**上（`scratch_db.copy_of_live_db`），真库只读。

跑法：

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/probe_open_threads_reach.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
# 副本上不需要把 8B 钉进显存（那会真打一次 Ollama）。
os.environ["MODEL_PIN_ON_STARTUP"] = "false"

# Windows 控制台默认 GBK：本探针打出对勾/叉号 emoji，不重配编码会在最后一行抛
# UnicodeEncodeError（`R26-24` 那一族，`check_consistency.py` 的 `console encoding` 盯着）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "forensics"))

import scratch_db  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from rolecard_agent.api.main import create_app  # noqa: E402
from rolecard_agent.base.observability import TraceEvent  # noqa: E402
from rolecard_agent.core.graph import build_graph_config  # noqa: E402
from rolecard_agent.core.proactive_state import (  # noqa: E402
    AFFINITY_DECAY_PER_DAY,
    get_state,
)
from rolecard_agent.features.reachout import (  # noqa: E402
    OPEN_THREADS_REFRESH_MINUTES,
    proactive_thread_id,
    trigger_affection,
)

COPY = ROOT / "build" / "scratch-open-threads.db"
#: 扫描只认"最近那几轮"，把 now 往后推这么多天还等不到 timer 可达就当作等不到。
SWEEP_DAYS = 120
#: `_PROMPT` 独有的格式行，用来把"话题扫描"从替身收到的调用里认出来。
SCAN_MARK = "OPEN <"


class StubModel:
    """记录型模型替身：记下每次被喂的 prompt，回**空正文**。

    空正文让 `generate_reachout_text` 走 `text=None` 那一支 ⇒ 这一轮不落库，但
    `reachout_skipped` 的 detail 里带着 `trigger`，那正是本探针要读的"命中的是哪一档"。
    扫描侧永远返回 `NONE` 等价物（空 = 不写话题），一次云端调用也不会发出去。
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, text: Any, *_a: Any, **_k: Any) -> AIMessage:
        self.prompts.append(text if isinstance(text, str) else str(text))
        return AIMessage(content="")

    def bind(self, *_a: Any, **_k: Any) -> StubModel:
        return self

    def with_structured_output(self, *_a: Any, **_k: Any) -> StubModel:
        return self


class Recorder:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        self.events.append(event)


def _head(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def _scans(stub: StubModel) -> int:
    return sum(1 for p in stub.prompts if SCAN_MARK in p)


def _one(rt: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = rt.conn.execute(sql, params).fetchone()
    return list(row).pop() if row is not None else None


def _report_state(rt: Any, roles: list[Any], now: datetime) -> None:
    print(f"{'role':16}{'affinity':>10}{'衰减后':>10}{'扫描时刻':>20}{'缓存话题':>10}{'未读':>6}")
    for role in roles:
        st = get_state(rt.conn, role.role_id, user_id=rt.identity)
        unread = _one(
            rt,
            "SELECT COUNT(*) FROM agent_reachout WHERE role_id=? AND state='unread'",
            (role.role_id,),
        )
        scanned = str(st.open_threads_scan_at or "NULL")[:19]
        print(
            f"{role.role_id:16}{st.affinity:10.2f}"
            f"{st.decayed_affinity(now=now):10.2f}{scanned:>20}"
            f"{len(st.open_threads):10}{unread:6}"
        )
    print(
        f"\n扫描缓存寿命 = {OPEN_THREADS_REFRESH_MINUTES} 分钟；"
        f"开口基线间隔 = {rt.effective.reachout_interval_minutes:.0f} 分钟；"
        f"affinity 日衰减 = {AFFINITY_DECAY_PER_DAY:.0%}/天"
    )


def _report_input_chain(rt: Any, roles: list[Any], stub: StubModel) -> None:
    for role in roles:
        cp_rows = _one(
            rt,
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id=?",
            (proactive_thread_id(role.role_id, user_id=rt.identity),),
        )
        text = rt.proactive_recent_lines(role.role_id)
        print(
            f"[{role.role_id}] 检查点 {cp_rows} 行 → "
            f"proactive_recent_lines = {len(text)} 字符"
            f"{'（空 ⇒ find_open_threads 直接 return []，一次调用都不发）' if not text else ''}"
        )
        if text:
            print("   " + text.replace("\n", "\n   ")[:500])
    print(f"\n此刻替身累计扫描调用 = {_scans(stub)} 次")


def _report_delivery_effect(rt: Any, roles: list[Any]) -> None:
    """因果演示：先让**用户**在主动会话里留一句没被接住的话，再投递一句，看窗口怎么关上的。

    写入用 `graph.update_state`（只碰检查点这一处事实面，绕开轮次编排），读取用生产的
    `proactive_recent_lines` —— 也就是说"空"与"非空"两次都是生产那条读法给的答复。
    """
    for role in roles:
        tid = proactive_thread_id(role.role_id, user_id=rt.identity)
        cfg = build_graph_config(tid, rt.effective)
        rt.state["graph"].update_state(cfg, {"messages": [HumanMessage(content="我下周要体检")]})
        opened = rt.proactive_recent_lines(role.role_id)
        delivered = rt.deliver_proactive(role, "（探针投的一句，用来观测输入链）")
        closed = rt.proactive_recent_lines(role.role_id)
        print(f"[{role.role_id}] 线程 {delivered or tid}")
        print(f"   用户留了一句没收尾的话 → 读法给 {len(opened)} 字符"
              f"{'（非空，扫描会被调用）' if opened else '（仍然为空 ⇒ 判据另有其因）'}")
        if opened:
            print("   " + opened.replace("\n", "\n   ")[:300])
        print(f"   她主动开口那一句落进同一线程 → 读法给 {len(closed)} 字符"
              + ("  ← 窗口被她自己那句话关上了" if opened and not closed else ""))


def _first_free_day(role: Any, st: Any, settings: Any, base_local: datetime) -> int | None:
    """把 now 逐日往后推，看 `trigger_affection` 第一次**不**命中是哪天（= timer 可达的第一天）。"""
    for d in range(SWEEP_DAYS + 1):
        probe_local = base_local + timedelta(days=d)
        if trigger_affection(role, st, settings, now_utc=probe_local.astimezone(UTC)) is None:
            return d
    return None


def _scheduler(rt: Any) -> Any:
    """生产接线的那台调度器：由**宿主**在 `create_app` 里注册到运行时（后台任务宿主注册）。

    断言而不是回落一个手拼的调度器：探针要量的是生产那条链，注册没接上就该立刻知道，
    而不是拿一份自己拼的接线量出个好看的数。
    """
    scheduler = rt.background_task("reachout")
    assert scheduler is not None, "宿主没注册主动开口调度器：这条链在生产里是断的"
    return scheduler


def _run_tick(rt: Any, stub: StubModel, rec: Recorder, roles: list[Any], label: str,
              when_utc: datetime, when_local: datetime) -> None:
    for role in roles:
        rt.conn.execute("UPDATE agent_reachout SET state='read' WHERE role_id=?", (role.role_id,))
    before = _scans(stub)
    made = _scheduler(rt).tick_once(now_utc=when_utc, now_local=when_local)
    traces = [
        f"{e.event}:{e.detail.get('trigger') or e.detail.get('why') or e.detail.get('mode')}"
        for e in rec.events
        if e.event in ("reachout_skipped", "reachout_sent", "open_threads_failed")
    ][-4:]
    print(f"[{label}] 落库 {made} 条｜留痕 {traces}｜扫描调用 {before} → {_scans(stub)}")


def _force_timer_experiment(
    rt: Any, stub: StubModel, rec: Recorder, roles: list[Any], now: datetime
) -> None:
    """把 affection 档压下去（只动副本），留一句没接住的话，看 `fired` 是否终于走到 timer。

    这一格量的是**修法值不值**：如果 R26-03 的修法（给 affection 加开关/衰减、把扫描窗口
    从"没接住那截"改成"最近一窗"）落地，扫描实际会拿到什么素材、又是被哪一档抢先。
    """
    from rolecard_agent.core.proactive_state import save_state  # 局部导入：只有这格要写状态

    rt.conn.execute("DELETE FROM agent_reachout")  # 去掉间隔/未读两道抑制，让触发链裸露
    for role in roles:
        st = get_state(rt.conn, role.role_id, user_id=rt.identity)
        st.affinity = 0.0
        st.last_interaction_utc = now
        st.open_threads_scan_at = None  # 当作从没扫过
        save_state(rt.conn, st, user_id=rt.identity)
        cfg = build_graph_config(
            proactive_thread_id(role.role_id, user_id=rt.identity), rt.effective
        )
        rt.state["graph"].update_state(
            cfg, {"messages": [HumanMessage(content="我下周要体检，结果出来跟你说")]}
        )
        before = _scans(stub)
        made = _scheduler(rt).tick_once(now_utc=now, now_local=now.astimezone())
        fired = [
            e.detail.get("trigger")
            for e in rec.events
            if e.event in ("reachout_skipped", "reachout_sent")
        ][-1:]
        print(f"[{role.role_id}] affinity=0 + 一句未接住的话 → tick：落库 {made} 条、"
              f"命中档位 {fired}、扫描调用 {before} → {_scans(stub)}")
    scans = [p for p in stub.prompts if SCAN_MARK in p]
    if not scans:
        print("\n⇒ 扫描一次都没发生：`fired` 仍被排在 timer 之前的档位吃掉（见上一行命中档位）。")
    for i, p in enumerate(scans):
        print(f"\n--- 扫描第 {i + 1} 次拿到的**生产素材**（{len(p)} 字符，逐字）---\n{p}")


def _clear_all_shadows(
    rt: Any, stub: StubModel, rec: Recorder, roles: list[Any], now: datetime
) -> None:
    """把排在 timer 之前的**四档**全部让位（只动副本），问最后一个问题：链尾到底通不通。

    `trigger_recall` 没有冷却，判据只是"该角色有没有一条 active 记忆" —— 也就是说只要
    记忆在长，这一档就永久压在 timer 头上。这是 R26-03 原来没点出的第二道遮蔽。
    """
    rt.conn.execute(
        "UPDATE role_card SET recall_enabled=0, time_pattern_enabled=0, file_watch_enabled=0"
    )
    rt.conn.commit()
    before = _scans(stub)
    made = _scheduler(rt).tick_once(now_utc=now, now_local=now.astimezone())
    fired = [
        e.detail.get("trigger")
        for e in rec.events
        if e.event in ("reachout_skipped", "reachout_sent")
    ][-1:]
    print(f"四档全关 + affinity=0 + 一句未接住的话 → tick：落库 {made} 条、命中档位 {fired}、"
          f"扫描调用 {before} → {_scans(stub)}")
    for i, p in enumerate(p for p in stub.prompts[before:] if SCAN_MARK in p):
        print(f"\n--- 扫描第 {i + 1} 次拿到的**生产素材**（{len(p)} 字符，逐字）---\n{p}")


def main() -> None:
    now = datetime.now(UTC)
    _head("准备：真库（只读）→ 副本（实验写这里）")
    src = scratch_db.resolve_live_db()
    scratch_db.copy_of_live_db(COPY, src)
    print(f"源库 = {src}\n副本 = {COPY}")

    stub, rec = StubModel(), Recorder()
    app = create_app(sqlite_path=COPY, model=stub, tracer=rec)  # type: ignore[arg-type]
    rt = app.state.ctx.runtime
    settings = rt.effective
    roles = [r for r in rt.roles.list_roles() if r.reachout_enabled]
    print(f"全局总闸 reachout_enabled = {settings.reachout_enabled}；"
          f"可主动开口的角色 = {[r.role_id for r in roles]}")

    _head("① 关系状态读数（R26-03 那三重锁死里能被直接看到的部分）")
    _report_state(rt, roles, now)

    _head("② 生产输入链实际给到的素材（不手写一句对话）")
    _report_input_chain(rt, roles, stub)

    _head("③ 投递一句之后再读同一处 —— 「常态为空」的机制")
    _report_delivery_effect(rt, roles)

    _head("④ 触发源排序：fired == \"timer\" 那一天到底来不来")
    base_local = now.astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    free_days: dict[str, int | None] = {}
    for role in roles:
        st = get_state(rt.conn, role.role_id, user_id=rt.identity)
        free_days[role.role_id] = _first_free_day(role, st, settings, base_local)
        d = free_days[role.role_id]
        verdict = f"{d} 天之后" if d is not None else f"{SWEEP_DAYS} 天内都不让位"
        print(f"[{role.role_id}] affinity={st.affinity:.2f} → affection 档让位于 {verdict}")
    print("读法：上面那个天数的前提是**此后一次都没开口** —— 每次成功开口都会 `record_interaction`"
          "\n      把 affinity 再 +0.2、同时把衰减的钟（`last_interaction_utc`）归零。"
          "而开口本身又被 affection 档驱动着。")
    reachable = [d for d in free_days.values() if d is not None]
    first_free = min(reachable) if reachable else None

    _head("⑤ 真实调度器各跑一次 tick：现在，与 timer 第一次可达的那天")
    # 生产接线：调度器由宿主在 create_app 里注册，读法就是 Runtime 上那几个门面
    # （`proactive_recent_lines` / `proactive_recent_window` / `deliver_proactive`）。
    # 这里**不 start**：只取同一台对象手动跑 tick，不让后台循环抢跑。
    _scheduler(rt)
    _run_tick(rt, stub, rec, roles, "现在", now, now.astimezone())
    later_local = base_local + timedelta(days=first_free if first_free is not None else SWEEP_DAYS)
    _run_tick(
        rt, stub, rec, roles,
        f"timer 第一次可达（+{first_free if first_free is not None else SWEEP_DAYS} 天）",
        later_local.astimezone(UTC),
        later_local,
    )

    _head("⑥ 强行把 timer 档让出来（只改副本）：如果触发条件修好，它拿到的素材长什么样")
    _force_timer_experiment(rt, stub, rec, roles, now)

    _head("⑦ 连排在 timer 之前的另外三档也一起关掉 —— 链尾到底通不通")
    _clear_all_shadows(rt, stub, rec, roles, now)

    _head("结论读数")
    print(f"替身累计收到的话题扫描调用 = {_scans(stub)} 次"
          f"（>0 才说明这一源在生产上可达；0 = 这一源从来没被走到）")
    print(f"替身累计被调用 {len(stub.prompts)} 次，其中话题扫描 {_scans(stub)} 次"
          "（其余是开口正文那几次，走的是同一份替身）")


if __name__ == "__main__":
    main()
