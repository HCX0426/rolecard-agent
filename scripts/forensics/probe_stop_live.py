""""停止生成"实机端到端（#18 的验收）。全程走**库副本**，一行都不写真库。

量三件事，都是用户视角的：
  1. 对照 —— 同一条长请求不按时要跑多久；
  2. 停止 —— 按下 `/api/session/{tid}/stop` 之后 SSE 多久结束，以及**历史里那条 AI 消息
     是不是就是屏幕上那半截**（停在哪儿，历史到哪儿）；
  3. 下一句 —— 停完之后紧接着问一句短的，多久回。原始症状是"我按了停止，接下来那句特别慢"，
     本地 Ollama 串行推理时这条最诚实：如果它排在一句被取消的生成后面，这里就会看到几十秒。

跑法（先用副本库把 8100 那份后端起来）：

    SQLITE_PATH=build/scratch-stop.db RUN_API_PORT=8100 PYTHONPATH=src \\
        .venv/Scripts/python.exe scripts/probe_stop_live.py
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

# 结论行带「✅」，Windows 控制台默认 codepage 是 GBK：不重配编码，第一个 print 就抛
# UnicodeEncodeError（console_encoding 那条一致性检查抓的就是这个）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("STOP_PROBE_BASE", "http://127.0.0.1:8100")
LOCAL_BACKEND = os.environ.get("STOP_PROBE_MODEL", "qwen3-vl-8b")
LONG = "请写一篇 600 字左右的记叙文，主题：雨后在乐土散步，遇到一个等车的人。一次写完，不要反问。"
SHORT = "只回一个字：好"


def new_thread() -> str:
    """一条新会话，并把它的模型钉到本地那个（要的是"慢到来得及打断"）。"""
    created = httpx.post(
        f"{BASE}/api/session", json={"role_id": "elysia"}, timeout=60
    ).json()
    tid = str(created["thread_id"])
    httpx.patch(
        f"{BASE}/api/session/{tid}", json={"model_name": LOCAL_BACKEND}, timeout=60
    ).raise_for_status()
    return tid


def stream_chat(
    tid: str, text: str, *, stop_after: int | None = None
) -> tuple[float, float, str, float | None]:
    """跑一轮流式对话。

    返回 (首帧秒数, 整段秒数, 累计 token 文本, 按下停止的时刻)。`stop_after` 是"看到多少个
    字就按停止"—— 模拟的是"她说了开头我就不想听了"。
    """
    t0 = time.perf_counter()
    first = -1.0
    parts: list[str] = []
    stopped_at: float | None = None
    with httpx.stream(
        "POST", f"{BASE}/api/chat", json={"thread_id": tid, "message": text}, timeout=900
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            if first < 0:
                first = time.perf_counter() - t0
            ev = json.loads(line[5:].strip())
            if ev.get("type") == "token":
                parts.append(str(ev.get("text") or ""))
            elif ev.get("type") == "message_replace":
                parts = [str(ev.get("text") or "")]  # 已提交文本是真相，整条替换
            if stop_after is not None and stopped_at is None and sum(map(len, parts)) >= stop_after:
                httpx.post(f"{BASE}/api/session/{tid}/stop", timeout=30).raise_for_status()
                stopped_at = time.perf_counter() - t0
    return first, time.perf_counter() - t0, "".join(parts), stopped_at


def last_assistant_row(tid: str) -> dict:
    rows = httpx.get(f"{BASE}/api/session/{tid}/messages", params={"limit": 20}, timeout=60).json()
    for m in reversed(rows["messages"]):
        if m.get("role") == "assistant":
            return m
    return {}


def last_assistant(tid: str) -> str:
    return str(last_assistant_row(tid).get("content") or "")


def main() -> None:
    scratch = os.environ.get("SQLITE_PATH", "(没设！会写错库)")
    print(f"后端 {BASE} / 本地模型 {LOCAL_BACKEND} / SQLITE_PATH={scratch}")

    t1 = new_thread()
    f1, total1, text1, _ = stream_chat(t1, LONG)
    print(f"\n[对照] 不打断：首帧 {f1:.1f}s，整段 {total1:.1f}s，{len(text1)} 字")

    t2 = new_thread()
    f2, total2, streamed, stopped_at = stream_chat(t2, LONG, stop_after=40)
    committed = last_assistant(t2)
    assert stopped_at is not None
    print(f"[停止] 首帧 {f2:.1f}s，第 {stopped_at:.1f}s 按下停止，流在第 {total2:.1f}s 结束")
    print(f"       按下去到收手 {total2 - stopped_at:.2f}s"
          f"（不打断本来还要 {total1 - stopped_at:.0f}s）")
    print(f"[停止] 屏幕上 {len(streamed)} 字 / 历史里 {len(committed)} 字，"
          f"同一份 = {streamed.strip() == committed.strip()}")
    print(f"[停止] 历史结尾：…{committed[-30:]!r}")

    t3 = new_thread()
    f3, total3, text3, _ = stream_chat(t3, SHORT)
    print(f"[下一句] 首帧 {f3:.1f}s，整段 {total3:.1f}s，{len(text3)} 字"
          f" —— 这里若出现几十秒，就是停止没真的停（引擎还被那一轮占着）")

    # R26-13 尾：被截断的半句要**自带**"被叫停"的标记（随 checkpoint 落库）——
    # 刷新后的回放才标得出"没说完"，而不是靠页面 state。对照的两轮是说完了的，
    # 不许冤枉一句完整的话。
    row = last_assistant_row(t2)
    assert row.get("stopped") is True, f"半句没带 stopped 标记：{row}"
    for tid in (t1, t3):
        done = last_assistant_row(tid)
        assert done.get("stopped") is None, f"说完的轮被冤枉了：{done}"
    print("[标记] 截断的半句落库自带 stopped=true，对照两轮（说完了）没有 ✅")


if __name__ == "__main__":
    main()
