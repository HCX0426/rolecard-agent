"""活人感的 A/B：同一模型、同一历史、同一温度，只差**一个**改动，用尺子量差多少。

为什么要有它（审计 §12.9）：本项目反复出现"改完感觉好了"的时刻，而唯一能让这种话作废的
办法就是把两个臂放在**同一轮实验**里跑。设计稿 §8.9/§8.12 那两张表就是这么来的。

跑在**库的副本**上（`scripts/scratch_db.py`）：主动开口那组会真的调模型、会进 guard 也会记
token 账 —— 那些笔账不该混进真库的"今天花了多少"。

    .venv\\Scripts\\python.exe scripts\\persona_ab.py                    # 开口臂：范例三种条件
    .venv\\Scripts\\python.exe scripts\\persona_ab.py --group chat       # 对话臂：深度注入开/关
    .venv\\Scripts\\python.exe scripts\\persona_ab.py --role elysia --n 6

`--n` 默认 5：**样本小到个位数的 A/B 只能看方向，不能定阈值**。这条写在输出里也写在下面，
因为第一次跑就有人想把 0.13 与 0.14 当成差别（它们不是 —— 见 §8.12 的读数）。

刻意不复用生产配置的地方只有一处：范例的"异构"那份文本是**实验输入**，不是产品文案，
它住在下面这个常量里就是要让人一眼看出"这是喂进去比对的，不是她该有的样子"。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_PROXY", "*")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import persona_meter as pm  # noqa: E402
import scratch_db  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from rolecard_agent.base.identity import resolve_instance_identity  # noqa: E402
from rolecard_agent.base.text import text_of  # noqa: E402
from rolecard_agent.config import Settings  # noqa: E402
from rolecard_agent.core import runtime_settings  # noqa: E402
from rolecard_agent.core.anti_repeat import repeat_score  # noqa: E402
from rolecard_agent.core.graph import build_model  # noqa: E402
from rolecard_agent.core.model_settings import ModelSettingsService  # noqa: E402
from rolecard_agent.core.prompts import (  # noqa: E402
    DEPTH_INJECT_FROM_END,
    VOICE_DEPTH_PROMPT,
    build_system_prompt,
)
from rolecard_agent.core.reachout import generate_reachout_text  # noqa: E402
from rolecard_agent.roles.models import RoleCard  # noqa: E402
from rolecard_agent.roles.service import RoleCardService  # noqa: E402

#: 实验输入，不是产品文案（见模块 docstring）。三条在由头/句式/长度上互相岔开，
#: 且刻意避开她现有口癖里那几个词 —— 否则量的就不是"范例同型"这一件事。
HETERO_EXEMPLARS: list[dict[str, str]] = [
    {"user": "你还在吗", "assistant": "在。刚把今天的风记到本子上，你要说的事我先听着。"},
    {
        "user": "给我讲点别的",
        "assistant": (
            "讲个小的：窗台那只猫今天没来，我等了整个下午，"
            "结果发现自己惦记的不过是它踩奶时那点儿动静——就这样，没什么道理。"
        ),
    },
    {"user": "今天不太开心", "assistant": "那就别开心了，先喝口水。我陪你坐一会儿。"},
]

#: 对话臂共用的几条"用户话"：短、日常、带一点情绪压力（那正是模板最容易回来的地方）。
SEED_TURNS = ["我回来了", "刚跑完步", "今天好累", "你在干嘛"]
PROBE_TURN = "你会不会觉得我很烦"


def _effective(conn: sqlite3.Connection) -> Settings:
    """与生产同一条配置链：env → DB 后端行 → 运行环境覆盖。

    少并任何一层，量的就不是提示词而是脚本本身（审计 §12.10 那次"num_ctx 掉回默认
    ⇒ 三次里两次空正文"的假故障，就是这么造出来的）。
    """
    return runtime_settings.apply_overrides(
        ModelSettingsService(conn).effective_settings(
            Settings.from_env(), user_id=resolve_instance_identity(Settings.from_env())
        ),
        runtime_settings.load_overrides(conn),
    )


def _arms(
    conn: sqlite3.Connection, settings: Settings, role_id: str, n: int, backend: str | None
) -> dict[str, Any]:
    """开口那组：范例 现状 / 异构 / 无范例，各跑 n 次，走**生产生成路径**。

    走 `generate_reachout_text` 而不是自己拼一次 invoke，为的是把 guard、去重指令与
    §12.1 那道复读闸门一起带进来 —— 要量的是"她实际会发出去的那句"，不是模型的裸输出。

    注意：`elysia` 的三条范例已在 2026-09-23 被删（设计稿 §8.12 的实测结论），所以
    "现状范例"与"无范例"两臂现在**会同源**——那不是 bug，是那一次实验留下的状态。
    要再比"有 vs 无"，拿一张还有范例的卡（`medical_archivist`）或先把 §8.12 那段贴回去。
    """
    role = RoleCardService(conn).scoped(resolve_instance_identity(settings)).get(role_id)
    # 后端可由 --backend 指定：**同一轮实验必须同一个后端**，否则量的就是"云端 vs 本地"
    # 而不是"这一处改动"（§8.12 那次的读数是云端，本地 8B 在同样三臂上动作开头一律 100%）。
    model = build_model(settings, backend or role.model_name, role.temperature)
    out: dict[str, Any] = {}
    variants: dict[str, Any] = {
        "现状范例": role.exemplars or [],
        "异构范例": HETERO_EXEMPLARS,
        "无范例": [],
    }
    for label, exemplars in variants.items():
        card = RoleCard(
            role_id=role.role_id,
            role_name=role.role_name,
            system_prompt=role.system_prompt,
            exemplars=exemplars,
            model_name=role.model_name,
            temperature=role.temperature,
        )
        texts, scores = [], []
        for _ in range(n):
            draft = generate_reachout_text(
                card, model, settings, conn, role_id=role_id, mode="general"
            )
            if draft.text is None:
                print(f"  [{label}] 这一条没发出去：why={draft.why}")
                continue
            texts.append(draft.text)
            scores.append(draft.score)
        out[label] = {"texts": texts, "gate_scores": scores}
    return out


def _chat_arms(
    conn: sqlite3.Connection,
    settings: Settings,
    role_id: str,
    n: int,
    backend: str | None,
) -> dict[str, Any]:
    """对话那组：同一份历史（她自己真实说过的话），只差那条深度注入。

    历史用她**真说过的**几条：复读的压力必须在场，否则两臂都没东西可抄，A/B 就是空的。
    """
    role = RoleCardService(conn).scoped(resolve_instance_identity(settings)).get(role_id)
    model = build_model(settings, backend or role.model_name, role.temperature)
    hers = [
        str(r["text"]).strip()
        for r in conn.execute(
            "SELECT text FROM agent_reachout WHERE role_id = ? ORDER BY id DESC LIMIT 4", (role_id,)
        ).fetchall()
    ]
    system = build_system_prompt(role.system_prompt, role.exemplars, memory="", agent=False)
    out: dict[str, Any] = {}
    for label, inject in (("不注入（旧形态）", False), ("注入深度指令", True)):
        turns: list[Any] = [SystemMessage(content=system)]
        for i, text in enumerate(hers):
            turns.append(HumanMessage(content=SEED_TURNS[i % len(SEED_TURNS)]))
            turns.append(AIMessage(content=text))
        turns.append(HumanMessage(content=PROBE_TURN))
        if inject:
            turns.insert(max(1, len(turns) - DEPTH_INJECT_FROM_END), SystemMessage(
                content=VOICE_DEPTH_PROMPT
            ))
        out[label] = {"texts": [text_of(model.invoke(turns)).strip() for _ in range(n)]}
    return out


def _report(
    groups: dict[str, Any], priors: list[str], conn: sqlite3.Connection, day: str,
    baseline: tuple[int, int],
) -> None:
    """每个臂一行的读数 + 逐条原文。列的选择与 `persona_meter` 完全一致（同一把尺子）。

    token 只报**整轮一个数**：账本记的是"哪一天哪个后端"，没有"哪一臂"这一维 ——
    硬凑一个每臂成本，就是在编一个没有依据的分配。
    """
    print(
        f"\n{'臂':<16} 样本  动作开头  正文去重  长度范围   型例比 词面复用"
        " 重合最大 复读条  与旧开口重合"
    )
    for label, arm in groups.items():
        texts = [t for t in arm["texts"] if t]
        if not texts:
            print(f"{label:<16} 0 条（一次都没发出去）")
            continue
        m = pm.measure([{"text": t} for t in texts])
        toward = sum(repeat_score(t, priors) for t in texts) / len(texts)
        print(
            f"{label:<16} {m['样本数']:>3}   {m['以动作括号开头']:>6}   "
            f"{m['正文首6字去重率']:>7}   {m['长度范围']:>8}  {m['型例比']:>6}  "
            f"{m['词面复用均值']:>6}   {m['重合度最大']:>6}    {m['判为逐字复读条数']:>3}"
            f"      {toward:>6.2f}"
        )
        if m.get("高频意象"):
            print(f"{'':16}   反复用的字组：{m['高频意象']}")
        for t in texts:
            print(f"      · {t.replace(chr(10), ' ')[:104]}")
    print(f"本轮花费：{_tokens_this_run(baseline, _token_totals(conn, day))} token")


def _token_totals(conn: sqlite3.Connection, day: str) -> tuple[int, int]:
    """今天（副本上）已经记下的 (次数, token 总数)。"""
    row = conn.execute(
        "SELECT SUM(calls) c, SUM(prompt_tokens + completion_tokens) t FROM token_usage_day"
        " WHERE day = ?",
        (day,),
    ).fetchone()
    return int(row["c"] or 0), int(row["t"] or 0)


def _tokens_this_run(baseline: tuple[int, int], now: tuple[int, int]) -> str:
    """本轮花掉的 token = 现在的账 − 开跑前的账。

    为什么要减基线：副本是从真库 backup 来的，**里面已经带着今天真实跑过的那几笔**
    （第一版就直接把整张表当"本轮"报，6 次生成报成了 7 次 —— 数字看着对，其实是别人的账）。
    """
    calls = now[0] - baseline[0]
    total = now[1] - baseline[1]
    return f"{total}（{calls} 次 ≈{total // max(1, calls)}）"


def main() -> None:
    parser = argparse.ArgumentParser(description="活人感 A/B（跑在库的副本上）")
    parser.add_argument("--role", default="elysia", help="角色 id，默认 elysia")
    parser.add_argument("--n", type=int, default=5, help="每个臂跑几次，默认 5")
    parser.add_argument(
        "--group", choices=("openings", "chat", "both"), default="openings", help="跑哪一组臂"
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="强制这一轮所有臂都走某个后端名（默认跟角色卡的路由）。同一轮必须同后端："
        "否则量的是「云端 vs 本地」而不是改动本身。",
    )
    parser.add_argument(
        "--copy",
        type=Path,
        default=scratch_db.SCRATCH_DIR / "_persona_ab.db",
        help="副本库落在哪（默认 build/scratch/ 下，整目录 gitignore）",
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    scratch_db.copy_of_live_db(args.copy)
    conn = sqlite3.connect(args.copy)
    conn.row_factory = sqlite3.Row
    settings = _effective(conn)
    chosen = args.backend
    if chosen is None:
        card = conn.execute(
            "SELECT model_name FROM role_card WHERE role_id = ?", (args.role,)
        ).fetchone()
        chosen = str(card["model_name"]) if card and card["model_name"] else None
    row = (
        conn.execute(
            "SELECT b.name, p.provider, b.model FROM model_backend b"
            " JOIN model_provider p ON p.id = b.provider_id WHERE b.name = ?",
            (chosen,),
        ).fetchone()
        if chosen
        else None
    )
    line = f"副本 {args.copy.name}｜角色 {args.role}｜每臂 {args.n} 次"
    if row is not None:
        line += f"｜后端 {row['provider']}/{row['model']}（{row['name']}）"
    print(line)
    print("样本小到个位数只能看方向：**别拿它定阈值**。")
    priors = [
        str(r["text"]) for r in conn.execute(
            "SELECT text FROM agent_reachout WHERE role_id = ? ORDER BY id", (args.role,)
        ).fetchall()
    ]
    day = pm.local_day()
    # 开跑前先把今天已有的账抄下来：副本里那些笔是真库跑出来的，不是本轮的。
    baseline = _token_totals(conn, day)
    if args.group in ("openings", "both"):
        _report(
            _arms(conn, settings, args.role, args.n, args.backend), priors, conn, day, baseline
        )
    if args.group in ("chat", "both"):
        _report(
            _chat_arms(conn, settings, args.role, args.n, args.backend),
            priors,
            conn,
            day,
            baseline,
        )
    conn.close()


if __name__ == "__main__":
    main()
