#!/usr/bin/env python
"""R26-09 的判定：主动开口这一路，「记忆注入」和「未收尾话题扫描」到底重不重复。

台账里那条说的是：同一件事（"用户提过、还没落地"）有三条路各判一遍 —— `memory_distill`
抽取 → `memory_for_turn` 免费注入主动 prompt，而 `open_threads` **又花一次模型调用**判一遍，
两路同源于同一个"未接住窗口"。要砍掉第五由头，前提得是"记忆那一路已经把它带上了"。
这句话不能靠读代码认定 —— 读代码只能看出**形状**像重复，看不出**内容**重不重复。

所以本探针量的是内容：对每个角色，把两条路各自交给开口 prompt 的那段文字并排打出来，
再看扫描挖出的每一条话题**是否已经在那段记忆里**。

用法（只读真库，一切写都落在临时副本上；会打**真**云端模型，每个有待扫窗口的角色一次）：

    .venv\\Scripts\\python.exe scripts/probe_memory_vs_scan.py
    .venv\\Scripts\\python.exe scripts/probe_memory_vs_scan.py --db build/scratch-r2609.db
        # 指定副本当源：`persona_chat_sim.py` 造出来的那份会话副本要用这个入口才接得上

判据口径（写清楚，免得下次换人重数）：

* **"已带上" = 话题与记忆文本共享一个 ≥4 字的连续片段**。用子串而不是"语义相近"，
  因为语义判断本身又要一次模型调用 —— 那正是本探针要审的对象，不能拿它当尺子。
* 扫描返回 `None` = 这次调用没成功（不当"没有话题"，也不计入重复率）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "forensics"))

# Windows 控制台默认 GBK：本探针打「｜」「▸」这类字符（`R26-24` 那一族，
# `check_consistency.py` 的 `console encoding` 盯着这条）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import scratch_db  # noqa: E402

from rolecard_agent.api.main import create_app  # noqa: E402
from rolecard_agent.core.memory import GLOBAL_BUCKET, memory_for_turn  # noqa: E402
from rolecard_agent.core.proactive.open_threads import find_open_threads  # noqa: E402
from rolecard_agent.features.reachout import OPEN_THREADS_REFRESH_MINUTES  # noqa: E402
from rolecard_agent.storage.db import bootstrap, connect  # noqa: E402


def shared_run(text: str, topic: str, *, min_len: int = 4) -> str:
    """两段文字共享的最长连续片段（够长才算"同一件事已经被带上"）。"""
    best = ""
    for start in range(len(topic)):
        for end in range(start + min_len, len(topic) + 1):
            piece = topic[start:end]
            if len(piece) <= len(best) and piece not in text:
                continue
            if len(piece) > len(best) and piece in text:
                best = piece
    return best


def _counts(conn: Any, bucket: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM role_memory_item WHERE role_id = ? AND invalidated_at IS NULL",
        (bucket,),
    ).fetchone()
    return int(row[0])


def main() -> int:
    # `--db` 指一份现成的副本（比如 `persona_chat_sim.py` 造出来的那份），省一次拷贝；
    # 不给就按老规矩从真库取一份临时副本 —— 真库永远只读。
    explicit = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--db" else None
    if explicit is not None and not explicit.exists():
        print(f"--db 指的副本不存在：{explicit}", file=sys.stderr)
        return 2
    if explicit is None:
        live = scratch_db.resolve_live_db()
        copy = Path(tempfile.mkdtemp(prefix="mem_vs_scan_")) / "app.db"
        scratch_db.copy_of_live_db(copy)
        print(f"源库（只读）：{live}\n副本：{copy}")
    else:
        copy = explicit
        print(f"用现成副本：{copy.resolve()}")
    conn = connect(copy)
    bootstrap(conn)  # 副本也要升到当前形状（补列器），否则读不到新列
    print(f"记忆池现状：全局 {_counts(conn, GLOBAL_BUCKET)} 条活跃条目")

    app = create_app(sqlite_path=copy)
    rt = app.state.ctx.runtime
    scanned = 0
    carried = 0
    total_topics = 0
    # 这台实例的主人（M2b 之后记忆与角色卡都按归属读；探针跑在真库副本上，主人就是本机那份）
    owner = rt.identity
    for role in rt.assembly.roles.scoped(owner).list_roles():
        rid = str(role.role_id)
        name = str(role.role_name or rid)
        mem = memory_for_turn(conn, rt.effective, rid, user_id=owner)
        window = rt.proactive_recent_window(rid)
        items = _counts(conn, rid)
        print(f"\n{'─' * 66}\n{name}（{rid}）｜该角色专属记忆 {items} 条｜主动会话 "
              f"{'有' if window else '无'}内容")
        print(f"  记忆注入给开口 prompt 的文本：{mem!r}")
        if not window:
            print("  扫描：没有可扫的窗口（这条线程还没聊过）⇒ 不参与对减")
            continue
        topics = find_open_threads(window, rt.resolve_role_model(role.model_name))
        if topics is None:
            print("  扫描：这次调用没成功（记 None，不当'没有话题'）")
            continue
        scanned += 1
        print(f"  扫描挖出的未收尾话题：{topics}")
        for topic in topics:
            total_topics += 1
            run = shared_run(mem, topic) if mem else ""
            if len(run) >= 4:
                carried += 1
                print(f"    ▸ 「{topic}」记忆里已有（共享片段 {run!r}）⇒ 这一条确实重复")
            else:
                print(f"    ▸ 「{topic}」记忆里**没有** ⇒ 只有扫描这条路会把它带上")
        print(f"  被扫的窗口原文：{window[:300]}")

    print(f"\n{'=' * 66}\n判定")
    print(f"  有窗口可扫的角色：{scanned} 个；扫出话题 {total_topics} 条；"
          f"其中记忆那一路已经带上的：{carried} 条")
    if total_topics == 0:
        print("  ⇒ 样本为 0：今天没有任何一条未收尾话题可用来证伪，**不能据此下结论**。")
        print("    要出结论得先攒出真实会话（这条探针跑一次只要几个云端调用）。")
        return 0
    if carried == total_topics:
        print("  ⇒ 全部重复：记忆注入已经带上每一条，第五由头今天是可以砍的那一个。")
    else:
        print(f"  ⇒ 不重复：{total_topics - carried}/{total_topics} 条只有扫描带得上，"
              "而记忆池的填充是**事后**的（对话里说完→下一轮才提炼），"
              "开口要的恰恰是'此刻还没落地'那一件。")
    print("  代价对照：扫描 = 每个刷新周期一次模型调用"
          f"（缓存 {OPEN_THREADS_REFRESH_MINUTES} 分钟）；记忆注入 = 免费搭车。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
