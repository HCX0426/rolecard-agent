"""桌宠那一轮 vs 对话界面：旧写法（只比 total）与新写法（读 inflight）的落后差多少。

臂 A = 桌宠：POST /api/chat 读 SSE，记下首字与整轮结束的时刻。
臂 B = 对话界面那个探针：每 0.25s 打 `/messages?limit=1`，把 `total` 与 `inflight.text`
       的变化各记一格（0.25s 是细网格，为的是量准"服务端真的读得到"的时刻；
       界面上是 5 秒一拍发现、之后 0.8 秒一拍跟踪，最后按这两个数换算）。

对照的是同一个后端、同一轮生成：`total` 那一格代表旧界面最早能看见的时候，
`inflight` 那一格代表新界面最早能看见的时候。
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

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

UI_DISCOVER_S = 5.0  # ChatPage 发现"她在说"的那一拍（SESSION_SYNC_MS）
UI_TRACK_S = 0.8  # 发现之后跟字的拍子（MIRROR_SYNC_MS）
THREAD_ID = "s_proactive_elysia"
MESSAGE = "给我讲讲你今天想做的事，写长一点，两三百字，分几段。"


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


def _read_events(url: str, body: dict) -> tuple[float | None, float | None, int]:
    """跑一轮 SSE：返回（首字时刻，轮末时刻，投送出去的字数）。"""
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
    print(f"源库副本={copy}\n端口={port} 线程={THREAD_ID}", flush=True)
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

            probe_url = f"{base}/api/session/{THREAD_ID}/messages?limit=1"
            base_total = int(json.loads(_http(probe_url).read())["total"])
            print(f"轮前 total={base_total}\n", flush=True)

            stop = threading.Event()
            seen: list[tuple[float, int, str | None]] = []

            def poller() -> None:
                while not stop.wait(0.25):
                    try:
                        page = json.loads(_http(probe_url).read())
                        inf = page.get("inflight")
                        seen.append((
                            time.perf_counter(), int(page["total"]),
                            None if inf is None else str(inf.get("text")),
                        ))
                    except Exception as exc:  # noqa: BLE001
                        print("poll err", exc, flush=True)

            th = threading.Thread(target=poller, daemon=True)
            th.start()
            time.sleep(0.3)

            sent_at = time.perf_counter()
            first, end, chars = _read_events(
                f"{base}/api/chat", {"thread_id": THREAD_ID, "message": MESSAGE})
            time.sleep(2.0)
            stop.set()
            th.join(timeout=3)

            print(f"臂 A：投送 {chars} 字，事件里首字在 +{(first or sent_at) - sent_at:.2f}s")
            print("--- 臂 B 的变化格 ---")
            last: tuple[int, str | None] = (base_total, None)
            for ts, total, text in seen:
                if (total, text) != last:
                    state = "在飞（还没进检查点）" if text is not None else "已提交"
                    print(f"  t=+{ts - sent_at:6.2f}s total={total} {state} "
                          f"字数={0 if text is None else len(text)}")
                    last = (total, text)

            inflight_at = next((ts for ts, _, t in seen if t is not None), None)
            inflight_chars_at = next((ts for ts, _, t in seen if t), None)
            # 她那一句进检查点 = total 比"轮前 + 他那句"还多一格（+1 是他自己那句）
            committed_at = next((ts for ts, t, _ in seen if t >= base_total + 2), None)
            if first is None or committed_at is None:
                print("有一项没量到，读数作废")
                return 1

            def on_grid(ts: float, step: float) -> float:
                """`ts` 落在界面上那一拍的哪一格（向上取整）。"""
                n = int((ts - sent_at) // step) + 1
                return sent_at + n * step

            old_ui = on_grid(committed_at, UI_DISCOVER_S)
            new_ui = old_ui
            if inflight_at and inflight_chars_at:
                # 界面先按 5 秒那一拍发现"她在说"，之后每 0.8 秒跟一次字
                disc = on_grid(inflight_at, UI_DISCOVER_S)
                if disc >= inflight_chars_at:
                    new_ui = disc
                else:
                    k = int((inflight_chars_at - disc) // UI_TRACK_S) + 1
                    new_ui = disc + k * UI_TRACK_S
            print("--- 读数（都相对「按下发送」）---")
            print(f"  首字出现在桌宠              +{first - sent_at:6.2f}s")
            print(f"  服务端读得到「她在打字」      "
                  f"{f'+{inflight_at - sent_at:6.2f}s' if inflight_at else '没读到 —— 修法没生效'}")
            print(f"  服务端读得到她的第一个字      "
                  f"{f'+{inflight_chars_at - sent_at:6.2f}s' if inflight_chars_at else '没读到'}")
            print(f"  她那整句进检查点（旧的上限）  +{committed_at - sent_at:6.2f}s")
            print(f"  旧界面最早看见她那句          +{old_ui - sent_at:6.2f}s"
                  f"　从首字算落后 {old_ui - first:.2f}s")
            print(f"  新界面最早看见她在说的那半句  +{new_ui - sent_at:6.2f}s"
                  f"　从首字算落后 {new_ui - first:.2f}s")
            print(f"  提前了 {old_ui - new_ui:.2f}s；之后每 ≤{UI_TRACK_S}s 跟着涨，"
                  f"直到整句落地换成真消息")
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
