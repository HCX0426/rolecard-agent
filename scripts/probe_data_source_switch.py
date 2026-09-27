"""数据源切换 + 上行同步的端到端探针（M5 与 M7）：本机实例 A 的界面登录本机实例 B，
走一遍、把 A 那份推过去、再切回来。

这就是 M8 那个"本机双实例"脚手架：两份 `DATA_ROOT`、两个自抢的空闲端口、一个 headless
Chrome。十四步读数里第 ③ 步（错口令要有界面提示、不许卡死）当场撞出过两个认证洞，
台账 `R26-42`；上行那五步（⑨~⑬）是"人真的点过四屏"与"东西真的落在对面"的分界 ——
最后两步不在浏览器里查，而是拿对面的清单核（浏览器只能证明屏幕上有"上行完成"四个字）。

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

# 读数行里有「⑬⑭」这类 GBK 装不下的带圈数字：不重配编码，最后一句 print 会抛
# UnicodeEncodeError 退场（`R26-24` 那一族症状，门禁的 console encoding 一条就是为它写的）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


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


def make_role(
    base: str, role_id: str, name: str, token: str | None, *, prompt: str = "e2e 用的卡"
) -> None:
    body = json.dumps(
        {"role_id": role_id, "role_name": name, "system_prompt": prompt}
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


MEMORY_TEXT = "用户在这台机器上写过一条只有本机有的事实。"


def add_memory(base: str, text: str) -> None:
    """给本机那份加一条事实（走真路由 `POST /api/settings/memory/item`）。

    上行那一步要**故意取消勾选"记忆"**，所以这一类得先有东西，否则"没勾就不推"
    测的是一句空话。
    """
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        f"{base}/api/settings/memory/item",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
        if r.status >= 400:
            raise RuntimeError(f"加记忆失败: HTTP {r.status}")


def get_json(url: str, token: str | None = None) -> object:
    req = urllib.request.Request(  # noqa: S310
        url, headers={"Authorization": token} if token else {}
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


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
            # 云端那台按设计**只存盘、不开口**（`R26-45` 之后定的部署形态：会不会说话是
            # 实例的属性，不是浏览器的状态）。探针带着它跑，这条形态才是被真跑过的，
            # 而不是只写在文档里。
            "REACHOUT_ENABLED": "0",
            "API_ALLOW_ORIGINS": a_base,
        },
    )
    print(f"A(本机)={a_base} pid={a.pid}  B(云端)={b_base} pid={b.pid}")
    try:
        wait_health(a_base, a)
        wait_health(b_base, b)
        # 一张**两边都有、内容被各自改过**的卡（造那条真冲突），加一张**只有本机有**的卡
        # （M5 那一步"整份数据集换了"要靠它来证 —— 共用 role_id 的那张两边都看得见）。
        make_role(a_base, "r_only_local", "只有本机的卡", None)
        make_role(a_base, "r_local", "本机专有卡", None, prompt="本机写的那份人设")
        make_role(b_base, "r_cloud", "云端专有卡", token)
        # 同一张卡在对面被改过一遍 ⇒ 计划里必须有**一条真冲突**，
        # 否则第三屏（逐条裁决）在这支探针里永远走不到，只剩 jsdom 那几条绿。
        make_role(b_base, "r_local", "本机专有卡", token, prompt="云端把这张卡改过了一次")
        add_memory(a_base, MEMORY_TEXT)
        r = subprocess.run(  # noqa: S603
            ["node", "scripts/ui_data_source_switch.js", a_base + "/", b_base],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=600,
        )
        print(r.stdout.strip())
        if r.stderr.strip():
            print("stderr:", r.stderr.strip()[-500:])
        if r.returncode != 0:
            return r.returncode
        # 浏览器那十二步只证明"人点到了完成屏"。推过去的东西**到底落在哪儿**要在对面查：
        # 卡该在（勾了），那条记忆不该在（在预检屏被取消了勾选）。
        roles = json.dumps(get_json(f"{b_base}/api/roles", token), ensure_ascii=False)
        pushed = "本机写的那份人设" in roles
        print(f"{'PASS' if pushed else 'FAIL'}"
              "  ⑭ 裁决「保留本机这份」真的写进了对面（人设换成本机那份）")
        inv = json.dumps(get_json(f"{b_base}/api/sync/inventory", token), ensure_ascii=False)
        quiet = MEMORY_TEXT not in inv
        print(f"{'PASS' if quiet else 'FAIL'}"
              "  ⑮ 没勾的「记忆」真的没碰对面")
        return 0 if (pushed and quiet) else 1
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
