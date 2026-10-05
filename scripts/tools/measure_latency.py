"""量两个验收指标：**首字延迟 P95** 与 **完整回答延迟 P95**（`docs/需求与验收标准.md` 那张表里
一直挂着"待测"的两格）。

跑法（在仓库根；它会**自己起一个隔离实例**：库副本 + 自抢端口，不打用户的 :8000）：

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/measure_latency.py --n 20

口径为什么长这样，每一条都是被问过才定的：

* **延迟不配字数就是没数**：同一把尺子量"在吗"和"写三百字"会得到两个都对的结论。
  所以每条臂都报 首字 / 整轮 / 输出字数 三列，并且固定同一个问句（要求一百字左右）。
* **臂之间只换模型**：会话覆盖 `model_name` 是唯一被改的东西，`agent_mode` 一律钉成 `chat`
  —— 智能体档会掺进工具往返，那测的不是模型而是编排。
* **P95 需要 n≥20**：n=3 的"中位数"就是排序取中间，尾部完全看不见（`R26-15` 那组 n=3 的
  18.81 s[9.0~25.2] 就是例子：跨度比中位数有用得多）。
* **本地那臂要预热一次再开始计数**：第一次调用含模型加载，把它算进 P95 等于用一次冷启动
  去回答"日常要等多久"。预热那次单独报出来。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
sys.path.insert(0, str(ROOT / "scripts" / "forensics"))

THREAD = "s_proactive_elysia"
PROMPT = "解释一下为什么冬天白天比夏天短，说清楚原因，一百字左右。"
#: 默认两臂。验收标准原文要的就是这两臂（"本地 7B 与云端各测一遍"）。
#: `siliconflow-vl`（Qwen3-VL-30B）**不在默认里**，理由是一条实测：同一个问句在它那里
#: 一次 `call_model` 跑了 **364 秒**（09-26 22:30 那趟，服务端 `node_end` 的 latency_ms），
#: n=20 就是两小时 —— 不是不能测，是不该占默认路径；要看它的人显式
#: `--arms siliconflow-vl` 并自己认这个时长。
DEFAULT_ARMS = ("siliconflow", "qwen3-vl-8b")


def _free_port() -> int:
    with socket.socket() as p:
        p.bind(("127.0.0.1", 0))
        return int(p.getsockname()[1])


def _req(url: str, body: dict[str, Any] | None = None, method: str | None = None,
         timeout: float = 600.0):
    """`method` 默认**不传**：让 urllib 按"有没有 body"自己定（有 body = POST）。

    这里踩过一次坑，而且坑得浪费时间：上一版签名是 `method="GET"` 并把它显式传给
    `Request(...)`，于是 `POST /api/chat` 全部以 GET 发出，服务端老老实实回 404
    `{"detail":"Not Found"}` —— 我据此以为应用里有个"换模型后发不出去"的偶发 bug，
    重放、四组对照、查路由与重建，好几轮才在服务端日志里看见那行 `GET /api/chat`。
    教训不是"服务端日志要看"（那当然要看），是**别给一个方法名写默认值**：
    默认值与调用意图相反时，错的那一侧永远是最先被怀疑的那一侧。
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    kw: dict[str, Any] = {"data": data,
                          "headers": {"Content-Type": "application/json"} if data else {}}
    if method is not None:
        kw["method"] = method
    return urllib.request.Request(url, **kw)


def _open(url: str, body: dict[str, Any] | None = None, method: str | None = None,
          timeout: float = 600.0):
    """发出去并返回响应：`method` 不写就由 urllib 按有没有 body 决定（见 `_req` 那段教训）。"""
    return urllib.request.urlopen(  # noqa: S310
        _req(url, body, method), timeout=timeout)


def _json(url: str, body: dict[str, Any] | None = None, method: str | None = None) -> Any:
    with _open(url, body, method, timeout=60) as r:
        return json.loads(r.read())


def _one_turn(base: str, tid: str) -> tuple[float | None, float, int, str | None]:
    """跑一轮：返回（首字秒, 整轮秒, 投送的正文字数, 出错说明）。

    出错**不抛**：一次 404/超时该被记成"这一轮没量到"，而不是把整条臂 20 轮的样本一起带走
    （跑测期间见过一次 `POST /api/chat` 回 404 `{"detail":"Not Found"}`，四次重放都不再出现；
    与其当时当噪声跳过，不如把每一次失败都留在计数里，看它会不会成比例地回来）。
    """
    t0 = time.time()
    first: float | None = None
    chars = 0
    try:
        with _open(f"{base}/api/chat", {"thread_id": tid, "message": PROMPT}) as res:
            buf = b""
            while True:
                # 必须是 `read1`：`read(8192)` 会一直等到攒满 8KB 或流结束才返回，
                # 于是"首字"被量成"整轮"（第一版就是这样，两个数一模一样）。
                # 首字延迟要的是**第一帧到达的时刻**，只有 read1 兑现这个语义。
                chunk = res.read1(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", "replace").strip()
                    if not s.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(s[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    now = time.time() - t0
                    if ev.get("type") == "token":
                        first = now if first is None else first
                        chars += len(ev.get("text") or "")
                    elif ev.get("type") == "end":
                        return first, now, chars, None
        return first, time.time() - t0, chars, "流提前结束，没收到 end"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:80]
        return None, time.time() - t0, 0, f"HTTP {exc.code} {body}"
    except Exception as exc:  # noqa: BLE001 - 一次网络抖动不该吞掉整条臂
        return None, time.time() - t0, 0, f"{type(exc).__name__}: {str(exc)[:80]}"


def _pct(xs: list[float], q: float) -> float:
    """最近秩法（样本小，不插值编出一个不存在的精度）。"""
    s = sorted(xs)
    k = max(1, min(len(s), int(round(q * len(s)))))
    return s[k - 1]


def main() -> int:
    import scratch_db

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=20, help="每条臂跑几轮（P95 要 n>=20）")
    ap.add_argument("--arms", default=",".join(DEFAULT_ARMS), help="逗号分隔的模型名子串")
    args = ap.parse_args()

    copy = scratch_db.copy_of_live_db(ROOT / "build" / "latency_probe.db")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = ROOT / "build" / f"latency_server_{port}.log"
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "SQLITE_PATH": str(copy),
        "CHROMA_PATH": str(ROOT / "build" / "latency_chroma"),
        "UPLOAD_DIR": str(ROOT / "build" / "latency_uploads"),
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",  # 别顺手把探针说的话提进她的长期记忆
        "MODEL_PIN_ON_STARTUP": "0",  # 本地那臂要显式选，不该由启动去钉模型占显存
    }
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print(f"库副本={copy}  端口={port}  HEAD={head}  n={args.n}", flush=True)
    with log_path.open("wb") as log:
        srv = subprocess.Popen([VENV_PY, "scripts/run_api.py"], env=env, cwd=str(ROOT),  # noqa: S603
                               stdout=log, stderr=subprocess.STDOUT)
        rows: list[str] = []
        try:
            while True:
                if srv.poll() is not None:
                    print("后端起来又死了：\n" + log_path.read_text("utf-8", "replace")[-2000:])
                    return 1
                try:
                    if _open(f"{base}/api/health", timeout=2).status == 200:
                        break
                except (urllib.error.URLError, OSError):
                    time.sleep(0.3)

            catalog = _json(f"{base}/api/settings/models")
            # 形状：`providers[]` 是**供应商分组**（`id`/`label`/`style`…），后端名在每组自己的
            # `models[].name` 上。按 `providers[].name` 找会一个都取不到 —— 那正好是
            # "读不出来却悄悄当没测过"的形状，所以这里读不出就显式失败，不静默。
            known = [m["name"] for g in catalog.get("providers", [])
                     for m in (g.get("models") or []) if isinstance(m, dict) and m.get("name")]
            if not known:
                print("模型目录读不出预期形状：", json.dumps(catalog, ensure_ascii=False)[:400])
                return 1
            print(f"可选后端：{known}", flush=True)

            for frag in [a.strip() for a in args.arms.split(",") if a.strip()]:
                hit = next((n for n in known if frag.lower() in n.lower()), None)
                if not hit:
                    rows.append(f"{frag:22} —— 这台机器上没有这个后端，跳过（不是 0 秒，是没测）")
                    continue
                _open(f"{base}/api/session/{THREAD}",
                      {"model_name": hit, "agent_mode": "chat"}, "PATCH").read()
                *_, warm_err = _one_turn(base, THREAD)
                if warm_err:
                    print(f"  预热那轮就失败：{warm_err}（预热不计入样本，但失败要说）", flush=True)
                firsts: list[float] = []
                ends: list[float] = []
                lens: list[int] = []
                errs: list[str] = []
                for i in range(args.n):
                    f, e, c, err = _one_turn(base, THREAD)
                    if err:
                        errs.append(err)
                        print(f"  {hit} #{i + 1:2d} 失败：{err}", flush=True)
                        continue
                    if f is not None:
                        firsts.append(f)
                    ends.append(e)
                    lens.append(c)
                    print(f"  {hit} #{i + 1:2d} 首字 {f and round(f, 2)}s 整轮 {e:.2f}s {c} 字",
                          flush=True)
                if not firsts:
                    rows.append(f"{hit:22} 整轮都没投送出正文 —— 这条臂没量到，别当成快"
                                + (f"（失败 {len(errs)} 次）" if errs else ""))
                    continue
                rows.append(
                    f"{hit:22} 首字 P50 {_pct(firsts, 0.5):5.2f}s / P95 {_pct(firsts, 0.95):5.2f}s"
                    f"（max {max(firsts):.2f}）   整轮 P50 {_pct(ends, 0.5):5.2f}s /"
                    f" P95 {_pct(ends, 0.95):5.2f}s（max {max(ends):.2f}）   "
                    f"输出 {statistics.mean(lens):.0f}±{statistics.pstdev(lens):.0f} 字   "
                    f"样本 {len(ends)}/{args.n}"
                    + (f"，失败 {len(errs)} 次" if errs else ""))
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(srv.pid)],  # noqa: S603
                               check=False)
    print("\n=== 结果（同一把尺子：同一个问句、agent_mode=chat、n=" + str(args.n) + "）===")
    for r in rows:
        print("  " + r)
    print(f"\n出处：HEAD={head} 库={copy.name} 端口={port} 时间={time.strftime('%Y-%m-%d %H:%M')}")
    print(f"server pid={srv.pid} 已终止；日志 {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
