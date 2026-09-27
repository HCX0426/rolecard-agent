"""数据源切换的端到端探针（M5）：本机实例 A 的界面登录本机实例 B，走一遍再切回来。

这就是 M8 那个"本机双实例"脚手架第一次真的用起来：两份 `DATA_ROOT`、两个自抢的
空闲端口、一个 headless Chrome。七步读数里第 ③ 步（错口令要有界面提示、不许卡死）
当场撞出过两个认证洞，台账 `R26-42` —— 所以这支探针值得留着反复跑。

跑法（仓库根）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/probe_data_source_switch.py
它需要 node + playwright-core + 本机 Chrome（缺任何一样就打印 SKIP 并算通过）。

红线照旧：自抢空闲端口、只杀自己 spawn 的 pid、两份数据根都在 build/ 下、不碰 :8000。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start(name: str, port: int, extra: dict[str, str]):
    root = ROOT / "build" / f"dss_{name}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "PYTHONIOENCODING": "utf-8",
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",
        "MODEL_PIN_ON_STARTUP": "0",
        "DATA_ROOT": str(root),
        **extra,
    }
    log = open(root / "api.log", "wb")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603
        [VENV_PY, "scripts/run_api.py"], env=env, cwd=str(ROOT),
        stdout=log, stderr=subprocess.STDOUT,
    )
    return proc, log, root


def wait_health(base: str, proc: subprocess.Popen) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"后端提前退出：{base}")
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as r:  # noqa: S310
                if r.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.5)
    raise RuntimeError(f"90s 内没起来：{base}")


def make_role(base: str, role_id: str, name: str, token: str | None) -> None:
    body = json.dumps(
        {"role_id": role_id, "role_name": name, "system_prompt": "e2e 用的卡"}
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        f"{base}/api/roles",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", **({"Authorization": token} if token else {})},
    )
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
        if r.status >= 400:
            raise RuntimeError(f"建卡失败 {role_id}: HTTP {r.status}")


def main() -> int:
    a_port, b_port = free_port(), free_port()
    a_base, b_base = f"http://127.0.0.1:{a_port}", f"http://127.0.0.1:{b_port}"
    token = "Basic " + base64.b64encode(b"u1:pw").decode()
    a, la, _ = start("a", a_port, {"AUTH_MODE": "off"})
    b, lb, _ = start(
        "b",
        b_port,
        {
            "AUTH_MODE": "on",
            "AUTH_CREDENTIALS": "u1:pw",
            "IDENTITY_USER_ID": "u1",
            "API_ALLOW_ORIGINS": a_base,
        },
    )
    print(f"A(本机)={a_base} pid={a.pid}  B(云端)={b_base} pid={b.pid}")
    try:
        wait_health(a_base, a)
        wait_health(b_base, b)
        make_role(a_base, "r_local", "本机专有卡", None)
        make_role(b_base, "r_cloud", "云端专有卡", token)
        r = subprocess.run(  # noqa: S603
            ["node", "scripts/ui_data_source_switch.js", a_base + "/", b_base],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=600,
        )
        print(r.stdout.strip())
        if r.stderr.strip():
            print("stderr:", r.stderr.strip()[-500:])
        return r.returncode
    finally:
        for proc, log in ((a, la), (b, lb)):
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False)
            log.close()
        print("两个后端都已 terminate（自己 spawn 的 pid）")


if __name__ == "__main__":
    sys.exit(main())
