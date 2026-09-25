"""把"日常闲聊八轮"真的发进去，逐条打时间，最后用尺子量她 + 看记忆落了没有。

为什么在**库副本**上跑（`scripts/scratch_db.py`）：这几句"用户说的话"是编的，写进真库就是
给她植入假事实 —— persona 只解读真实观测，这是这项目的一条不变式。副本走的是同一套生产代码：
`create_app` → `/api/session` → `/api/chat`(SSE) → `/distill`，一行都不旁路，所以它测到的
就是真的会发生的形状（包括复读闸门、token 记账、自动提取）。

八轮的内容**故意固定**：只有输入不变，两次跑出来的数才有得比。要改就一起改这份说明。

云端那一格靠**角色路由**实现：把该角色复制一份、`model_name` 指向云端后端名 —— 这正是 US-8
的设计用途，不用动任何全局配置，也不用担心把默认后端改了会影响别的会话。

    .venv\\Scripts\\python.exe scripts\\persona_chat_sim.py                 # 两边都跑（本地很慢）
    .venv\\Scripts\\python.exe scripts\\persona_chat_sim.py --side cloud    # 只跑云端
    .venv\\Scripts\\python.exe scripts\\persona_chat_sim.py --role elysia --turns 4
    # 审计 §12.5 那一格（角色留本地、只把提取交云端）——两次跑对减就是 A/B：
    .venv\\Scripts\\python.exe scripts\\persona_chat_sim.py \\
        --role medical_archivist --model qwen3-vl-8b
    .venv\\Scripts\\python.exe scripts\\persona_chat_sim.py \\
        --role medical_archivist --model qwen3-vl-8b --extract-backend siliconflow \\
        --copy build/scratch/_chat_sim_cloud_extract.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import persona_meter as pm  # noqa: E402
import scratch_db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rolecard_agent.api.main import create_app  # noqa: E402

#: 一段日常：有长有短，带**能被记住**的具体事实，也带情绪转折。
#: 最后一条特意用"刚跑完步回来了"——§8.1 那条 88 字逐字复读就是在这句话上发生的。
TURNS = [
    "今天加班到十点才走，路上一个人都没有，突然有点饿但又懒得吃东西。",
    "早上去跑了三公里，跑完整个人清醒了，就是膝盖有点不太对劲，回头得少爬楼梯。",
    "周末把衣柜全翻了一遍，扔了七八件一年没穿的衣服，忽然觉得这种清理挺上瘾的。",
    "中午和同事吵架了，也不是什么大事，就是对方一句话把我整得一下午都没心情。",
    "晚上回家路上买了盆薄荷，放在窗台上，看着心情好一点。你说植物真的会被跟它说话养得更好吗。",
    "这周打算把体检报告整理一下，去年那个结石指标就说要复查，一直拖着没去。",
    "昨晚看了两集老剧，笑得停不下来，然后十二点才睡，今天早上闹钟响了四遍。",
    "刚跑完步回来了，出了一身汗，现在只想瘫着。你今天过得怎么样。",
]


def _cloud_twin(conn: sqlite3.Connection, role_id: str) -> str:
    """复制一份该角色、只改 `model_name` 指向云端后端，返回孪生 role_id。"""
    row = conn.execute("SELECT * FROM role_card WHERE role_id = ?", (role_id,)).fetchone()
    if row is None:
        raise SystemExit(f"库里没有角色 {role_id}")
    data = dict(row)
    backend = conn.execute(
        "SELECT name FROM model_backend WHERE provider_id != 'ollama' ORDER BY sort_order LIMIT 1"
    ).fetchone()
    if backend is None:
        raise SystemExit("库里没有云端后端行，云端那一格测不了")
    twin = f"{role_id}_cloud"
    data.update(role_id=twin, model_name=str(backend["name"]), is_builtin=0)
    cols = ",".join(data)
    conn.execute(
        f"INSERT OR REPLACE INTO role_card ({cols}) VALUES ({','.join('?' * len(data))})",
        list(data.values()),
    )
    conn.commit()
    return twin


def _run_thread(
    client: TestClient, label: str, role_id: str, turns: list[str]
) -> list[tuple[str, float, float]]:
    """逐轮走完整链路，**分别记首字延迟与总耗时**；返回每轮的 `(她说的话, 首字秒, 总秒)`。

    为什么要首字（TTFT）而不是只有总耗时：§12.7 要答的问题是"后台自动提取会不会跟下一轮
    抢显存"，那件事的表现形态是**响应变慢**，而本地 8B 的总耗时里大头是它在思考（实测一条
    问候 184~232 秒）。只有首字延迟能把"模型没空"和"这句话说得长"分开。
    """
    thread = client.post("/api/session", json={"role_id": role_id}).json()["thread_id"]
    print(f"\n===== {label}（thread {thread[:18]}）", flush=True)
    hers: list[tuple[str, float, float]] = []
    for msg in turns:
        started = time.time()
        first: float | None = None
        text = ""
        with client.stream(
            "POST", "/api/chat", json={"thread_id": thread, "message": msg}
        ) as res:
            if res.status_code != 200:
                print(f"  HTTP {res.status_code}", flush=True)
                continue
            buf = ""
            for chunk in res.iter_text():
                buf += chunk
                while (idx := buf.find("\n\n")) >= 0:
                    frame, buf = buf[:idx], buf[idx + 2 :]
                    for line in frame.split("\n"):
                        if not line.startswith("data: "):
                            continue
                        ev = json.loads(line[6:])
                        if ev["type"] == "token":
                            if first is None and ev["text"]:
                                first = time.time()
                            text += ev["text"]
                        elif ev["type"] == "message_replace":
                            text = ev["text"]
                        elif ev["type"] == "error":
                            print(f"  [错误] {ev['detail']}", flush=True)
        total = time.time() - started
        ttft = (first - started) if first is not None else float("nan")
        hers.append((text.strip(), ttft, total))
        print(f"  我：{msg}", flush=True)
        print(
            f"  她（首字 {ttft:.1f}s｜总 {total:.0f}s）：{text.strip()}"
            if first is not None
            else f"  她（{total:.0f}s，一个正文 token 都没出）",
            flush=True,
        )
    # 提取精华：§8.7 那格"记忆 0 条"的解药就是它，跑完对话顺手看能不能落到库里。
    client.post(f"/api/session/{thread}/distill", json={})
    return hers


def _report(
    db: Path, role_id: str, sides: dict[str, list[tuple[str, float, float]]],
    baseline: tuple[int, int],
) -> None:
    """每个臂单独量一遍（**别把两边的句子混在一栏里** —— 那等于量了一个不存在的角色）。"""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    items = conn.execute(
        "SELECT role_id, text FROM role_memory_item WHERE role_id IN (?, ?)",
        (role_id, f"{role_id}_cloud"),
    ).fetchall()
    print(f"\n对话后记忆条目：{len(items)} 条")
    for r in items[:10]:
        print(f"   [{r['role_id']}] {str(r['text'])[:66]}")
    for label, log in sides.items():
        hers = [t for t, _ttft, _total in log]
        kept = [t for t in hers if t]
        m = pm.measure([{"text": t} for t in kept])
        # 首字延迟单独一行，按轮序排：自动提取会不会跟下一轮抢显存，看的就是这一串里
        # 有没有哪一格突然翘起来（§12.7）。第 5 轮之后是提取该触发的位置。
        series = " ".join(f"{ttft:.1f}" if ttft == ttft else "—" for _t, ttft, _tot in log)
        print(f"\n{label}：首字延迟序列（秒，按轮）{series}")
        print(
            f"{label}：{m.get('样本数')} 句｜空正文 {len(hers) - len(kept)} 句"
            f"｜动作开头 {m.get('以动作括号开头')}｜正文去重 {m.get('正文首6字去重率')}"
            f"｜口癖「{m.get('口癖开头')}」×{m.get('口癖条数')}"
            f"｜长度 {m.get('长度范围')}（CV {m.get('长度变异系数')}）"
            f"｜重合最大 {m.get('重合度最大')}｜逐字复读 {m.get('判为逐字复读条数')}"
            f"｜型例比 {m.get('型例比')}｜词面复用 {m.get('词面复用均值')}"
        )
        if m.get("高频意象"):
            print(f"   反复用的字组：{m['高频意象']}")
    rows = conn.execute(
        "SELECT backend, calls, prompt_tokens, completion_tokens, unreported"
        " FROM token_usage_day WHERE day = ?",
        (pm.local_day(),),
    ).fetchall()
    print("\n本轮 token 账（已减掉副本从真库带过来的那几笔，所以它只算这次跑的）：")
    now_calls = sum(int(r["calls"] or 0) for r in rows)
    now_total = sum(int(r["prompt_tokens"] or 0) + int(r["completion_tokens"] or 0) for r in rows)
    if now_calls <= baseline[0]:
        print("   一条都没记下 —— 后端没回用量，或账没接上（查 `usage_record_failed`）")
    else:
        print(
            f"   {now_calls - baseline[0]} 次"
            f"｜输入+输出 {now_total - baseline[1]}"
            f"｜平均 {int((now_total - baseline[1]) / max(1, now_calls - baseline[0]))}"
            f"｜没报 {sum(int(r['unreported'] or 0) for r in rows)} 次"
        )
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="八轮日常的真机模拟（跑在库的副本上）")
    parser.add_argument("--role", default="elysia")
    parser.add_argument("--side", choices=("both", "cloud", "local"), default="both")
    parser.add_argument("--turns", type=int, default=len(TURNS), help="跑前几轮，默认全部 8 轮")
    parser.add_argument(
        "--copy",
        type=Path,
        default=scratch_db.SCRATCH_DIR / "_chat_sim.db",
        help="副本库落在哪（默认 build/scratch/ 下，整目录 gitignore）",
    )
    parser.add_argument(
        "--model",
        default="",
        help="副本里把该角色的 model_name 设成这个后端名。档案角色那一格必须是\"角色留在本地\""
             "（真库的默认后端是云端，不设这一项测的就不是路由决策后的形状）",
    )
    parser.add_argument(
        "--extract-backend",
        default="",
        help="MEMORY_EXTRACT_BACKEND：只把「提取精华/整理记忆」交给这个后端（审计 §12.5 那一格）",
    )
    parser.add_argument(
        "--sampling",
        default="",
        help="写进副本里该后端的采样惩罚，形如 repeat_penalty=1.2,frequency_penalty=0.1"
             "（不给 = 保持库里原样）。惩罚的**推荐档位**要用这把尺子量出来才写进界面文案",
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not 0 < args.turns <= len(TURNS):
        raise SystemExit(f"--turns 得在 1..{len(TURNS)}")

    scratch_db.copy_of_live_db(args.copy)
    if args.model:
        conn = sqlite3.connect(args.copy)
        changed = conn.execute(
            "UPDATE role_card SET model_name = ? WHERE role_id = ?", (args.model, args.role)
        ).rowcount
        conn.commit()
        conn.close()
        if not changed:
            raise SystemExit(f"副本里没有角色卡 {args.role!r}，--model 没处落")
    if args.extract_backend:
        os.environ["MEMORY_EXTRACT_BACKEND"] = args.extract_backend
    if args.sampling:
        _write_sampling(args.copy, args.sampling, target=args.model)
    print(
        f"\n配置：角色 {args.role}｜对话后端 {args.model or '（跟随卡片/默认）'}"
        f"｜提取后端 {args.extract_backend or '（未设 = 跟随会话）'}"
        f"｜采样惩罚 {args.sampling or '（未设 = 引擎默认）'}",
        flush=True,
    )
    # 孪生角色只在真要跑云端那一格时才建：`--side local` 的跑法不该凭空多插一行角色卡
    # （它还会因为"库里没有云端后端"直接退出，而那一格本次根本不跑）。
    twin = ""
    if args.side in ("both", "cloud"):
        conn = sqlite3.connect(args.copy)
        conn.row_factory = sqlite3.Row  # 下面要 dict(row)，默认工厂给的是裸 tuple
        twin = _cloud_twin(conn, args.role)
        conn.close()

    # trace 落到副本旁边：`node_end` 里那一行 `prompt_tokens` 正是这轮最值钱的证据
    # （实测这个单轮 prompt 就有 46k），但让它刷在控制台里会把"我/她"的对话冲得看不见。
    trace_path = args.copy.with_suffix(".trace.jsonl")
    os.environ["OBS_LOG_PATH"] = str(trace_path)
    day = pm.local_day()
    app = create_app(sqlite_path=args.copy)
    # 开跑前抄一份基线：副本是从真库 backup 来的，**今天真实跑过的那几笔也在表里**，
    # 不减掉就会把它们算成"本轮花的"（第一版就这么报错了数）。
    baseline = _token_baseline(args.copy, day)
    sides: dict[str, list[tuple[str, float, float]]] = {}
    with TestClient(app) as client:
        # 顺序有讲究：先云端后本地。本地 8B 一旦把模型钉进显存，云端那一格也会被它拖慢。
        if args.side in ("both", "cloud"):
            sides["云端"] = _run_thread(client, "云端", twin, TURNS[: args.turns])
        if args.side in ("both", "local"):
            sides["本地"] = _run_thread(client, "本地", args.role, TURNS[: args.turns])
    _report(args.copy, args.role, sides, baseline)
    print(f"trace（含每轮 prompt/completion token 与耗时）：{trace_path}")


_SAMPLING_COLS = ("repeat_penalty", "frequency_penalty", "presence_penalty")


def _write_sampling(db: Path, spec: str, *, target: str) -> None:
    """把 `repeat_penalty=1.2,frequency_penalty=0.1` 写进副本里那一行的采样惩罚。

    写的是**副本的 model_backend**，所以它走的是生产同一条读路径（`effective_settings`
    → `_init_model` 构造期传参），测到的就是界面上设成这个数之后真实的形状。
    列名只认那三个白名单值 —— 拼 SQL 的地方不接受来路不明的字符串，脚本也不例外。
    """
    fields: dict[str, float] = {}
    for part in spec.split(","):
        key, _, raw = part.partition("=")
        key = key.strip()
        if key not in _SAMPLING_COLS:
            raise SystemExit(f"未知的采样项 {key!r}，只认 {'/'.join(_SAMPLING_COLS)}")
        fields[key] = float(raw)
    if not fields:
        raise SystemExit("--sampling 没解析出任何一项")
    conn = sqlite3.connect(db)
    try:
        name = target or str(
            conn.execute("SELECT name FROM model_backend ORDER BY sort_order LIMIT 1").fetchone()[0]
        )
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(
            f"UPDATE model_backend SET {cols} WHERE name = ?", (*fields.values(), name)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise SystemExit(f"副本里没有后端 {name!r}，--sampling 没处落")
    finally:
        conn.close()


def _token_baseline(db: Path, day: str) -> tuple[int, int]:
    """开跑前"今天"已经记下的 (次数, token 总数)。"""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT SUM(calls) c, SUM(prompt_tokens + completion_tokens) t"
            " FROM token_usage_day WHERE day = ?",
            (day,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        return (0, 0)
    return (int(row[0]), int(row[1] or 0))


if __name__ == "__main__":
    main()
