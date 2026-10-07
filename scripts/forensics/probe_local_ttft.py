"""本地 8B「首字 9→25 s 递增」到底是哪一侧（09-26 轮 R26-15 留下的猜测）。

两臂，量的是**同一段等待的不同切面**：

  · `--arm ollama`  直连 Ollama，同一个进程里把「加载 / 预填 / 思考解码 / 正文解码」
    四段拆开看（Ollama 在最后一个流式分片里自带 `load_duration`·`prompt_eval_duration`·
    `eval_count`·`eval_duration`，不需要我猜）。思考开/关 × 短/长提示 四个组合。
  · `--arm repo`    走本仓 `/api/chat`，记「第一个 thinking 帧」「第一个非空 token 帧」
    「整段结束」三个时刻，以及思考字符数 —— 用户手感的那个口径。

跑法（ollama 臂不需要后端；`--out` 默认 `build/scratch/ttft_probe.json`）：

    .venv/Scripts/python.exe scripts/probe_local_ttft.py --arm ollama
    .venv/Scripts/python.exe scripts/probe_local_ttft.py --arm repo --base http://127.0.0.1:8123

结果只写文件（`--out`），stdout 保持 ASCII：这台机器的控制台是 GBK，中文经它必乱。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# 直跑脚本时 src/ 不在 sys.path（pytest 由 pyproject 的 pythonpath 兜底，直跑没有）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
# 量的对象不在这里抄第二遍（单源那一族）：模型名已经现读 config，端点此前还留着自己的一份
# —— 抄一份旧值的探针，测的**不是用户实际在用的那一档**，而读数看着完全正常。
from rolecard_agent.config import DEFAULT_LOCAL_BACKEND, DEFAULT_LOCAL_BASE_URL  # noqa: E402

OLLAMA = DEFAULT_LOCAL_BASE_URL
MODEL = str(DEFAULT_LOCAL_BACKEND["model"])
NUM_CTX = 4096
SHORT = "用两三句话回答：你为什么喜欢下雨天？"
#: 约 1.4k tokens 的长提示，用来把「预填」与「思考解码」分开：两者都进首字，但只有后者随
#: 输出长度漂。内容是无意义重复，模型不会照着背。
LONG = SHORT + "\n".join(
    f"第{i}条备忘：今晚记得给窗台的栀子浇水，顺手把明早的闹钟往前调十分钟。" for i in range(28)
)


def _stream_ollama(*, think: str | None, prompt: str) -> dict[str, Any]:
    """一次直连流式调用。返回客户端看到的分段时间 + Ollama 自报的账。"""
    body: dict[str, Any] = {
        "model": MODEL,
        "stream": True,
        "options": {"num_ctx": NUM_CTX},
        "messages": [{"role": "user", "content": prompt}],
    }
    if think is not None:
        body["think"] = think == "true"
    t0 = time.perf_counter()
    first_think = first_content = None
    think_chars = content_chars = 0
    stats: dict[str, Any] = {}
    with httpx.stream("POST", f"{OLLAMA}/api/chat", json=body, timeout=900) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            msg = chunk.get("message") or {}
            if msg.get("thinking"):
                if first_think is None:
                    first_think = time.perf_counter() - t0
                think_chars += len(str(msg["thinking"]))
            if msg.get("content"):
                if first_content is None:
                    first_content = time.perf_counter() - t0
                content_chars += len(str(msg["content"]))
            if chunk.get("done"):
                stats = {k: chunk.get(k) for k in (
                    "total_duration", "load_duration", "prompt_eval_count",
                    "prompt_eval_duration", "eval_count", "eval_duration",
                )}
    return {
        "prompt_chars": len(prompt),
        "think_param": think,
        "wall_s": round(time.perf_counter() - t0, 2),
        "first_thinking_s": round(first_think, 2) if first_think is not None else None,
        "first_content_s": round(first_content, 2) if first_content is not None else None,
        "thinking_chars": think_chars,
        "content_chars": content_chars,
        "ollama": {
            k: (round(v / 1e9, 2) if isinstance(v, int | float) and k.endswith("duration") else v)
            for k, v in stats.items()
        },
    }


def _arm_ollama(reps: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # 顺序有意：先让第一次调用把模型装进显存，后面每一行的 load 才是"热"的。
    for prompt_name, prompt in (("short", SHORT), ("long", LONG)):
        for think in ("true", "false"):
            for i in range(reps):
                try:
                    row = _stream_ollama(think=think, prompt=prompt)
                except httpx.HTTPStatusError as exc:
                    # `think:false` 不是所有版本都接（不支持的模型会 400）——记下来别中断整轮。
                    row = {
                        "error": f"{exc.response.status_code} {exc.response.text[:200]}",
                        "think_param": think,
                    }
                row.update(arm="ollama", prompt=prompt_name, rep=i + 1)
                rows.append(row)
                print(json.dumps(row), flush=True)
    return rows


def _arm_repo(base: str, turns: int, role_id: str, model: str) -> list[dict[str, Any]]:
    """本仓链路：用户手感口径（第一个**非空 token** 才是她看见的字）。"""
    created = httpx.post(f"{base}/api/session", json={"role_id": role_id}, timeout=60)
    tid = str(created.json()["thread_id"])
    httpx.patch(
        f"{base}/api/session/{tid}", json={"model_name": model}, timeout=60
    ).raise_for_status()
    rows: list[dict[str, Any]] = []
    for i in range(turns):
        t0 = time.perf_counter()
        first_think = first_content = None
        think_chars = content_chars = 0
        with httpx.stream(
            "POST", f"{base}/api/chat", json={"thread_id": tid, "message": SHORT}, timeout=900
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip())
                etype = ev.get("type")
                if etype == "thinking" and ev.get("text"):
                    if first_think is None:
                        first_think = time.perf_counter() - t0
                    think_chars += len(str(ev["text"]))
                elif etype == "token" and ev.get("text"):
                    if first_content is None:
                        first_content = time.perf_counter() - t0
                    content_chars += len(str(ev["text"]))
                elif etype == "message_replace":
                    content_chars = len(str(ev.get("text") or ""))
        row = {
            "arm": "repo",
            "rep": i + 1,
            "wall_s": round(time.perf_counter() - t0, 2),
            "first_thinking_s": round(first_think, 2) if first_think is not None else None,
            "first_content_s": round(first_content, 2) if first_content is not None else None,
            "thinking_chars": think_chars,
            "content_chars": content_chars,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return rows


def _arm_window(reps: int) -> list[dict[str, Any]]:
    """同一份长输出，只在 `num_ctx` 上分档：量"越窗之后还跑不跑得快"。

    R26-29 留的那根 —— 本仓链路第一轮只有 22~29 tok/s 而直连 62~64，两个候选因（开局有
    主动开口在飞 / prompt+completion 越过 4096 窗）里后者是可单独量的，就用 `num_predict`
    把输出钉死，只挪窗口。
    """
    rows: list[dict[str, Any]] = []
    for num_ctx in (4096, 8192):
        # **每个窗口各自先叫一次**：Ollama 在加载时定死 num_ctx，换窗口不重发就等于把
        # "重新加载那 5~15 秒"算进下一臂的速率里（本机踩过，见 09-24 那条测量纪律）。
        httpx.post(
            f"{OLLAMA}/api/generate",
            json={"model": MODEL, "prompt": "只回一个字：好", "stream": False,
                  "options": {"num_ctx": num_ctx, "num_predict": 6}},
            timeout=300,
        ).raise_for_status()
        for i in range(reps):
            t0 = time.perf_counter()
            expanded = LONG + " 请把上面每一条备忘逐条扩写成三句话。"
            body = {
                "model": MODEL,
                "stream": True,
                "options": {"num_ctx": num_ctx, "num_predict": 3600},
                "messages": [{"role": "user", "content": expanded}],
            }
            eval_count = 0
            eval_duration_s = 0.0
            with httpx.stream("POST", f"{OLLAMA}/api/chat", json=body, timeout=900) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("done"):
                        eval_count = int(chunk.get("eval_count") or 0)
                        eval_duration_s = int(chunk.get("eval_duration") or 0) / 1e9
            wall = time.perf_counter() - t0
            ps = httpx.get(f"{OLLAMA}/api/ps", timeout=30).json()["models"]
            resident: dict[str, Any] = next(
                (m for m in ps if str(m.get("name", "")).startswith("qwen3-vl")), {}
            )
            row = {
                "arm": "window",
                "num_ctx": num_ctx,
                "rep": i + 1,
                "wall_s": round(wall, 2),
                "eval_count": eval_count,
                # 两口径都给：`tok_per_s` 含整段（含预填与 SSE 之前的排队），
                # `decode_tok_per_s` 只用 Ollama 自报的 eval_duration。
                "tok_per_s": round(eval_count / wall, 1) if wall > 0 and eval_count else None,
                "decode_tok_per_s": (
                    round(eval_count / eval_duration_s, 1)
                    if eval_duration_s and eval_count
                    else None
                ),
                "size_gb": round(int(resident.get("size") or 0) / 1e9, 2),
                "size_vram_gb": round(int(resident.get("size_vram") or 0) / 1e9, 2),
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
    return rows


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("arm", "prompt", "think_param"):
        values = sorted({str(r.get(key)) for r in rows if r.get(key) is not None})
        if len(values) > 1 or key == "arm":
            out[f"by_{key}"] = values
    for metric in ("first_content_s", "wall_s", "thinking_chars", "tok_per_s", "decode_tok_per_s"):
        vals = [float(r[metric]) for r in rows if isinstance(r.get(metric), int | float)]
        if vals:
            out[f"{metric}_median"] = round(statistics.median(vals), 2)
            out[f"{metric}_range"] = [round(min(vals), 2), round(max(vals), 2)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", choices=("ollama", "repo", "window"), default="ollama")
    ap.add_argument("--base", default="http://127.0.0.1:8123")
    ap.add_argument("--role", default="elysia")
    ap.add_argument("--model", default="qwen3-vl-8b")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--turns", type=int, default=5)
    ap.add_argument("--out", default="build/scratch/ttft_probe.json")
    args = ap.parse_args()

    if args.arm == "ollama":
        rows = _arm_ollama(args.reps)
    elif args.arm == "window":
        rows = _arm_window(args.reps)
    else:
        rows = _arm_repo(args.base, args.turns, args.role, args.model)
    payload = {"rows": rows, "summary": _summarize(rows)}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1).replace("\r\n", "\n"), encoding="utf-8"
    )
    print(json.dumps(payload["summary"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
