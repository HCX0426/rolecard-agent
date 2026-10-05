"""桌宠那一轮的回答，两种界面算法各落后多久。

臂 A = 桌宠：`POST /api/chat` 读 SSE，记下首字与整轮结束的时刻（用户在屏幕上看到第一个字的那一刻）。
臂 B = **修前**的对话界面：每 5.0 秒一次 `/messages?limit=1`，只比 `total`。
臂 C = **修后**的对话界面：每 0.8 秒一次 `/turn`（便宜，只查进程内登记），每 6 拍补一次贵读；
       读到"她不再生成"的那一拍立刻贵读一次，把真消息换进来。

报三个数：最早看见"她在打字"、最早看见她的第一个字、最早看见已经落地的那条消息。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
sys.path.insert(0, str(ROOT / "scripts" / "forensics"))
sys.path.insert(0, str(ROOT / "src"))

THREAD_ID = "s_proactive_elysia"
MESSAGE = "给我讲讲你今天想做的事，写长一点，两三百字，分几段。"

OLD_MS = 5.0  # 修前：只有一个 5 秒的贵读拍
TICK_S = 0.8  # 修后：心跳
EVERY_TICKS = 7  # 修后：每 7 拍（= 5.6 秒）付一次贵读


def _free_port() -> int:
    with socket.socket() as p:
        p.bind(("127.0.0.1", 0))
        return int(p.getsockname()[1])


def _http(url: str, body: dict | None = None, timeout: float = 600.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


def _get_json(url: str) -> dict:
    with _http(url, timeout=30) as res:
        return json.loads(res.read())


def _read_events(url: str, body: dict) -> tuple[float | None, float | None, int]:
    first = end = None
    chars = 0
    with _http(url, body) as res:
        buf = b""
        while True:
            chunk = res.read1(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                try:
                    ev = json.loads(text[5:].strip())
                except json.JSONDecodeError:
                    continue
                now = time.perf_counter()
                if ev.get("type") == "token":
                    first = first or now
                    chars += len(ev.get("text") or "")
                elif ev.get("type") == "end":
                    return first, now, chars
    return first, end, chars


def _sim_old(base: str, stop: threading.Event, log: list[tuple[float, str, int]]) -> None:
    """修前：只有贵读，只认 `total`。"""
    while not stop.wait(OLD_MS):
        page = _get_json(f"{base}/api/session/{THREAD_ID}/messages?limit=1")
        log.append((time.perf_counter(), "total", int(page["total"])))


def _sim_new(base: str, stop: threading.Event, log: list[tuple[float, str, int]]) -> None:
    """修后：心跳问便宜的，落地那一拍立刻贵读，其余每 7 拍贵读一次。"""
    ticks = 0
    mirroring = False
    while not stop.wait(TICK_S):
        now = time.perf_counter()
        if ticks >= EVERY_TICKS:
            page = _get_json(f"{base}/api/session/{THREAD_ID}/messages?limit=1")
            ticks = 0
            log.append((now, "total", int(page["total"])))
            inf = page.get("inflight")
            was = mirroring
            mirroring = inf is not None
            if inf:
                log.append((now, "mirror", len(str(inf.get("text")))))
            elif was:
                page = _get_json(f"{base}/api/session/{THREAD_ID}/messages?limit=1")
                log.append((time.perf_counter(), "total", int(page["total"])))
            continue
        cheap = _get_json(f"{base}/api/session/{THREAD_ID}/turn")
        ticks += 1
        inf = cheap.get("inflight")
        if inf is not None:
            if not mirroring:
                log.append((now, "typing", 0))
            mirroring = True
            if inf.get("text"):
                log.append((now, "mirror", len(str(inf.get("text")))))
        elif mirroring:
            mirroring = False
            page = _get_json(f"{base}/api/session/{THREAD_ID}/messages?limit=1")
            log.append((time.perf_counter(), "total", int(page["total"])))


def main() -> int:
    import scratch_db

    copy = scratch_db.copy_of_live_db(ROOT / "build" / "sync_probe.db")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = ROOT / "build" / f"sync_probe_server_{port}.log"
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "SQLITE_PATH": str(copy),
        "CHROMA_PATH": str(ROOT / "build" / "sync_probe_chroma"),
        "UPLOAD_DIR": str(ROOT / "build" / "sync_probe_uploads"),
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",
        "MODEL_PIN_ON_STARTUP": "0",
    }
    print(f"源库副本={copy}\n端口={port}", flush=True)
    with log_path.open("wb") as log:
        srv = subprocess.Popen(  # noqa: S603
            [VENV_PY, "scripts/run_api.py"], env=env, cwd=str(ROOT),
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            while True:
                if srv.poll() is not None:
                    print("后端起来又死了：\n" + log_path.read_text("utf-8", "replace")[-2000:])
                    return 1
                try:
                    if _http(f"{base}/api/health", timeout=2).status == 200:
                        break
                except (urllib.error.URLError, OSError):
                    time.sleep(0.3)

            base_total = _get_json(f"{base}/api/session/{THREAD_ID}/messages?limit=1")["total"]
            print(f"轮前 total={base_total}\n", flush=True)

            stop = threading.Event()
            old: list[tuple[float, str, int]] = []
            new: list[tuple[float, str, int]] = []
            ta = threading.Thread(target=_sim_old, args=(base, stop, old), daemon=True)
            tb = threading.Thread(target=_sim_new, args=(base, stop, new), daemon=True)
            ta.start()
            tb.start()
            time.sleep(0.5)

            sent_at = time.perf_counter()
            first, end_at, chars = _read_events(
                f"{base}/api/chat", {"thread_id": THREAD_ID, "message": MESSAGE})
            time.sleep(7.0)
            stop.set()
            ta.join(timeout=3)
            tb.join(timeout=3)
            if first is None:
                print("没拿到首字，读数作废")
                return 1
            print(f"臂 A：投送 {chars} 字，首字在 +{first - sent_at:.2f}s\n")

            def first_seen(
                log: list[tuple[float, str, int]], kind: str, *, min_v: int = 0
            ) -> float | None:
                return next((ts for ts, k, v in log if k == kind and v > min_v), None)

            # 判据必须是 base+2：他自己那句也算一格，只看"多过 base"会把它当成她落地
            old_total = first_seen(old, "total", min_v=int(base_total) + 1)
            new_type = first_seen(new, "typing", min_v=-1)
            new_mirror = first_seen(new, "mirror")
            new_total = first_seen(new, "total", min_v=int(base_total) + 1)
            print(f"{'':22}{'最早看见她在打字':16}{'最早看见她的字':16}最早看见落地的那条")
            def cell(ts: float | None) -> str:
                return f"+{ts - sent_at:5.2f}s" if ts else "读不到"
            print(f"  修前（只有 5 秒贵读）{cell(None):16}{cell(None):16}{cell(old_total)}")
            print(f"  修后（0.8 秒心跳）  {cell(new_type):16}{cell(new_mirror):16}"
                  f"{cell(new_total)}")
            print()
            if old_total:
                print(f"  落地那一条：修前 +{old_total - sent_at:.2f}s → 修后 "
                      f"{(new_total or old_total) - sent_at:.2f}s"
                      f"　（提前 {old_total - (new_total or old_total):.2f}s）")
            if new_mirror:
                print(f"  他从桌宠看到第一个字起，界面最早出现她的字：修后 "
                      f"{new_mirror - first:.2f}s（修前是 {(old_total or first) - first:.2f}s）")
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(  # noqa: S603
                    ["taskkill", "/F", "/T", "/PID", str(srv.pid)], check=False)
            print(f"\nserver pid={srv.pid} 已终止；日志 {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
