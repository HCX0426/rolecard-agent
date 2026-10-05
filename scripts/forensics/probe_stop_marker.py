"""R26-13 尾（被叫停的半句落库带标记）的真链路驱动：自起后端 + 库副本 + probe_stop_live。

按 rolecard-live-scratch-probe 的四条红线：不打 :8000、端口现取、只写副本库、
只 terminate 自己 spawn 的进程。跑法（仓库根）：

    PYTHONPATH=src PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/probe_stop_marker.py
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "forensics"))
sys.path.insert(0, str(ROOT / "src"))

import scratch_db  # noqa: E402


def main() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    copy = (ROOT / "build" / "scratch-stop.db").resolve()
    if not copy.exists() or os.environ.get("REFRESH_COPY"):
        print(scratch_db.copy_of_live_db(copy), file=sys.stderr)

    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "PYTHONIOENCODING": "utf-8",
        "SQLITE_PATH": str(copy),
        "CHROMA_PATH": str(ROOT / "build" / "scratch-stop-chroma"),
        "UPLOAD_DIR": str(ROOT / "build" / "scratch-stop-uploads"),
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",  # 探针的对话不许进她的长期记忆
        "MODEL_PIN_ON_STARTUP": "0",  # 不占本机显存
    }
    base = f"http://127.0.0.1:{port}"
    log = ROOT / "build" / "scratch-stop-backend.log"
    with log.open("wb") as fh:
        server = subprocess.Popen(  # noqa: S603
            [sys.executable, str(ROOT / "scripts" / "run_api.py")],
            cwd=str(ROOT),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    rc = 1
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if server.poll() is not None:
                tail = log.read_text("utf-8", "replace")[-2000:]
                print(f"后端起不来（{server.returncode}）：\n{tail}")
                return 1
            with contextlib.suppress(Exception):
                if urllib.request.urlopen(f"{base}/api/health", timeout=3).status == 200:  # noqa: S310
                    break
            time.sleep(1.0)
        else:
            print("后端 90s 内没就绪")
            return 1

        model = os.environ.get("STOP_PROBE_MODEL", "siliconflow")
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(ROOT / "scripts" / "forensics" / "probe_stop_live.py")],
            capture_output=True,
            text=True,
            timeout=900,
            cwd=str(ROOT),
            env={**env, "STOP_PROBE_BASE": base, "STOP_PROBE_MODEL": model},
            encoding="utf-8",
            errors="replace",
        )
        print((proc.stdout or "") + (proc.stderr or ""))
        rc = proc.returncode
    finally:
        server.terminate()
        with contextlib.suppress(Exception):
            server.wait(timeout=20)
        model = os.environ.get("STOP_PROBE_MODEL", "siliconflow")
        print(f"\n（读数出处：库副本 {copy} / 端口 {port} / 模型 {model}）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
