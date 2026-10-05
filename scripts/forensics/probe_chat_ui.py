"""对话页拆分的真机交互探针驱动：自起后端 + 库副本 + headless Chrome。

四条红线按 rolecard-live-scratch-probe 的规矩守着：
不打 :8000、端口现取、写实验只落在副本库上、只 terminate 自己 spawn 的那个 pid。

跑法（仓库根）：
    PYTHONPATH=src PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/probe_chat_ui.py
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
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

    workdir = Path(tempfile.mkdtemp(prefix="chat_ui_probe_"))
    copy = workdir / "app.db"
    # 先问"哪份是真库"：copy_of_live_db 会往 stderr 打一行"源库 = 谁"，那行要留在读数里
    scratch_db.copy_of_live_db(copy)

    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "PYTHONIOENCODING": "utf-8",
        "SQLITE_PATH": str(copy),
        "CHROMA_PATH": str(workdir / "chroma"),
        "UPLOAD_DIR": str(workdir / "uploads"),
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",  # 探针不许顺手改她的记忆
        "MODEL_PIN_ON_STARTUP": "0",  # 不占本机显存，也不排队等本地推理
    }
    log = workdir / "backend.log"
    with log.open("wb") as fh:
        server = subprocess.Popen(  # noqa: S603
            [sys.executable, str(ROOT / "scripts" / "run_api.py")],
            cwd=str(ROOT),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    rc = 1
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if server.poll() is not None:
                tail = log.read_text("utf-8", "replace")[-2000:]
                print(f"后端起不来（退出码 {server.returncode}），日志尾部：\n{tail}")
                return 1
            with contextlib.suppress(Exception):
                if urllib.request.urlopen(f"{base}/api/health", timeout=3).status == 200:  # noqa: S310
                    break
            time.sleep(1.0)
        else:
            print("后端 90s 内没就绪")
            return 1

        proc = subprocess.run(  # noqa: S603
            ["node", str(ROOT / "scripts" / "js" / "probe_chat_ui.js"), base],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(ROOT),
            encoding="utf-8",
            errors="replace",
        )
        print((proc.stdout or "") + (proc.stderr or ""))
        rc = proc.returncode
    finally:
        server.terminate()
        with contextlib.suppress(Exception):
            server.wait(timeout=20)
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        ).stdout.strip()
        print(f"\n（读数出处：库副本 {copy} / 端口 {port} / HEAD {head}）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
