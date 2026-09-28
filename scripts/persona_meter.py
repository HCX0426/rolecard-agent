"""活人感的客观度量（只读，不跑模型）：把"像不像人"折成几个能算的数。

为什么存在：用户报"角色对话没活人感"，这类反馈最容易死在"感觉变好了/还是那样"上。
设计稿 §8.1 先把症状量成了数（最近 6 条回复 6/6 以（动作）开头、字数挤在 68–90、
开头 12 字有两条逐字相同），这个脚本就是把那三条变成一条命令，**改前改后各跑一次**。

用法：

    .venv\\Scripts\\python.exe scripts\\persona_meter.py            # 按角色打印（只读库）
    .venv\\Scripts\\python.exe scripts\\persona_meter.py --json     # 机器可读，便于 diff
    .venv\\Scripts\\python.exe scripts\\persona_meter.py --limit 30 # 每角色看最近 30 条
    .venv\\Scripts\\python.exe scripts\\persona_meter.py --api http://127.0.0.1:8000  # 加量回复侧

指标（与设计稿 §8.3 一一对应）：

  * **首 6 字去重率 / 正文首 6 字去重率** —— 开场复读的直接信号。1.00 = 每条开头都不一样；
    越低越像模板。两列都要看：只看前者会把口癖量没（见 `_body`），真实数据里就是这么漏的。
    重复最多的那个正文开头会单独印成一行「口癖」。
  * **长度标准差 + 变异系数 CV**（附均值与范围）—— 挤在窄带里就是"每句都一个长度"，那是机器味。
    **判单调要看 CV 而不是 σ**：σ 是绝对量，均值一变它就跟着变（实测：把回复从 161–202 字压到
    74–95 字，σ 从 12.0 掉到 6.1，读起来像"更单调"，其实只是句子变短了）。
  * **以（动作）开头的比例** —— 我们那条角色卡最显眼的模板。
  * **与最近 5 条的 4-gram Dice**（均值 / 最大 / ≥0.85 的条数）—— 逐字复读的量化。
    阈值 0.85 与 n-gram 的做法抄自 N.E.K.O 的 `anti_repeat`（它用 Dice≥0.85 判逐字复读）。
  * **记忆条数** —— 原料在不在场：0 条=她没有任何关于你的事实可用，只能凭人设编。
  * **型例比 / 词面复用 / 高频意象** —— "语义空转"那一族（设计稿 §8.1 的症状：句式指标全绿，
    读原话却来回是乐土/月光/那几样）。前两个是数，第三个直接写出**她卡在哪些字组上**。
    两个脾气要知道：① 词面用**字符二元组**近似"词"（不引分词器），所以像「乐的」「土的」
    这种跨词边界的碎片也会进来 —— 一句句不同的真语料里共同的意象才会浮上来，
    同一句复制 N 遍的假语料里反而是碎片赢；② TTR 随样本变长自然下降，
    **只在同一个 `--limit` 下横比**，别拿 20 条的数去比 5 条的数。
  * 回复侧多一列**「用户条数」** —— 为 0 表示那条会话里你根本没说过话，她是在自言自语。
  * **今日 token（按后端）** —— 路由改云端之后，"活人感"每一步都花真钱（审计 §12.8）：
    一句回答一次调用，而 §12.1 那道复读闸门太像时还会**重生一次**。`没报` 那一列不为 0
    说明账本在漏（后端没回用量），**不等于**今天很省。

设计约束：

  * **只读**：以 `mode=ro` 打开 sqlite，跑它不动任何数据，后端正在跑也没关系（WAL 允许并发读）。
  * **报告必须自描述**（`run_eval.py` 那条教训）：输出带库路径、样本窗口与取样时间，
    否则两次数字放一起根本不知道是不是同一件事。
  * **默认不碰模型、不碰网络**：这是尺子，不是评测，一条命令就该在离线时也能跑。
  * **回复侧要显式要**（`--api`）：她"实际说出口的那句"不在 `agent_reachout` 里，只在检查点
    的消息里，得问在跑的后端。所以默认只量主动开口，给了 `--api` 才加一张表 —— 离线尺子的
    默认路径不能依赖服务在跑，但也不能因此量错了东西（见 `reply_side`）。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rolecard_agent.core.reachout import proactive_thread_id  # noqa: E402
from rolecard_agent.core.usage import daily_usage, local_day, usage_days  # noqa: E402

FIRST_CHARS = 6  # 开场复读看前几个字
NGRAM = 4  # 复读判定的 n-gram 长度
RECENT_WINDOW = 5  # 每条只与它**前面**几条比：复读是抄历史，不是被抄
DICE_REWRITE = 0.85  # ≥ 这个值按"逐字复读"计
#: 一个意象要出现在多少比例的消息里才单独报出来（「高频意象」那一栏的呈现门槛）。
#: 它是**呈现**门槛不是判据：这把尺子不拦任何东西，只决定哪些词值得写在脸上给人看。
IMAGERY_SHARE = 0.6


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


#: 开头连续的动作括号：`（指尖轻点裙摆，忽然歪头笑出声）哎呀…` → 正文是 `哎呀…`
_LEADING_BRACKETS = re.compile(r"^(?:\s*[（(][^（）()]*[）)])+")


def _body(text: str) -> str:
    """剥掉开头的（动作）括号，只看她真正**说出口**的那句从哪几个字开始。

    为什么单独量这个：第一次跑出来的真实数据里 8/8 条正文都以「哎呀」开头，而
    「首 6 字去重率」报了好看的 0.86 —— 因为前 6 个字全落在动作括号里，括号每次换个
    道具（指尖/裙摆/发梢）就够了。也就是说**只看原文开头会把最响的那个口癖量没**。
    """
    return _LEADING_BRACKETS.sub("", str(text or "").strip(), count=1)


def _ngrams(text: str, n: int = NGRAM) -> set[str]:
    return {text[i : i + n] for i in range(len(text) - n + 1)} if len(text) >= n else set()


#: 词面的最小长度：两个汉字。一个字的"的/了/呢"谁都是高频，报了没有信息量。
_LEX_N = 2


def _lexigrams(text: str) -> list[str]:
    """汉字/字母数字串里的二元组 —— **近似"词"**，不引分词器。

    为什么用字符二元组而不是 jieba：这把尺子的定位是"零依赖、随时能跑"（它读的是只读库，
    装在一个已经跑着 Ollama 与 Electron 的机器上）。多一个依赖就多一个装不上的理由。
    """
    out: list[str] = []
    for run in re.findall(r"[\w一-鿿]+", str(text or "")):
        out.extend(run[i : i + _LEX_N] for i in range(len(run) - _LEX_N + 1))
    return out


def lexical_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """"语义空转"那一族：句式全绿但来回就是那几个意象，Dice 0.85 那把抓不到（设计稿 §8.1）。

    三个数，各自量一件不同的事：

    * **型例比 TTR** = 不同的二元组 / 全部二元组。越低越"词汇翻来覆去"。
      它随样本变长自然下降，所以只在**同一个 `--limit`** 下横比 —— 这条限制写在这里，
      是因为一个不注明分母的可比性会被下一个人当成趋势。
    * **词面复用均值** = 每条里"她在更早的句子里用过的二元组"占多少（0~1）。
      这一列**故意不复用**生产闸门那个 `anti_repeat.repeat_score`：那个是 4-gram 覆盖率，
      量的还是"逐字"，而本节要抓的恰恰是"同一堆词换个说法"（§12.6 的原话：句式全绿、
      读起来却翻来覆去那几个意象）。两套量纲各管一段：4-gram 管复读，2-gram 管空转。
    * **高频意象** = 出现在 ≥`IMAGERY_SHARE` 条消息里的二元组，按条数排。这是给人**看**的那一个：
      数字说"空转"，它说"空转在哪些词上"。
    """
    texts = [str(r["text"]).strip() for r in rows]
    texts = [t for t in texts if _norm(t)]
    if not texts:
        return {"型例比": 0.0}
    grams = [_lexigrams(t) for t in texts]
    flat = [g for row in grams for g in row]
    per_doc = [set(row) for row in grams]
    doc_hits = Counter(g for row in per_doc for g in row)
    top = [
        (gram, count)
        for gram, count in doc_hits.most_common(40)
        if count >= max(2, ceil(len(texts) * IMAGERY_SHARE))
    ][:3]
    reused: list[float] = []
    for i in range(1, len(texts)):
        earlier: set[str] = set()
        for row in per_doc[:i]:
            earlier |= row
        mine = per_doc[i]
        if mine:
            reused.append(len(mine & earlier) / len(mine))
    return {
        "型例比": round(len(set(flat)) / len(flat), 3) if flat else 0.0,
        "词面复用均值": round(statistics.mean(reused), 3) if reused else 0.0,
        "高频意象": "、".join(f"「{g}」{c}/{len(texts)}" for g, c in top),
    }


def _dice(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def measure(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按时间**正序**的一批文本 → 那几个数。没有样本就给 `样本数: 0`。

    收的是"任何能 `row["text"]` 取到文本的东西"：sqlite3.Row 或字典都行 —— 主动开口那侧来自
    库，回复那侧来自 API（见 `--api`），两套数据共用同一把尺子才有可比性。
    """
    raw = [str(r["text"]).strip() for r in rows]
    kept = [(t, _norm(t)) for t in raw if _norm(t)]
    n = len(kept)
    if n == 0:
        return {"样本数": 0}

    raw_open = [flat[:FIRST_CHARS] for _, flat in kept]
    openings = [_norm(_body(t))[:FIRST_CHARS] for t, _ in kept]
    tic, tic_count = Counter(openings).most_common(1)[0]
    lengths = [float(len(flat)) for _, flat in kept]
    action_first = sum(1 for t, _ in kept if t.startswith("（")) / n

    grams = [_ngrams(flat) for _, flat in kept]
    scores = [
        max((_dice(g, grams[j]) for j in range(max(0, i - RECENT_WINDOW), i)), default=0.0)
        for i, g in enumerate(grams)
    ]
    scored = scores[1:]  # 第一条没有"前面"可比，别把 0.0 混进均值里拉低

    return {
        "样本数": n,
        "首6字去重率": round(len(set(raw_open)) / n, 3),
        "正文首6字去重率": round(len(set(openings)) / n, 3),
        "口癖开头": tic,
        "口癖条数": tic_count,
        **lexical_metrics(rows),
        "长度均值": round(statistics.mean(lengths), 1),
        "长度标准差": round(statistics.pstdev(lengths), 1) if n > 1 else 0.0,
        # 变异系数 σ/均值。**判"每句一样长"只能用这个，不能用绝对 σ**：2026-09-22 加了一条
        # 长度纪律后，回复从 161–202 字压到 74–95 字，绝对 σ 反而从 12.0 掉到 6.1 —— 看着像
        # "更单调"，其实只是均值腰斩把 σ 一起带下来了。两次的相对离散度几乎一样（13% vs 8%）。
        "长度变异系数": round(statistics.pstdev(lengths) / statistics.mean(lengths), 3)
        if n > 1 and statistics.mean(lengths)
        else 0.0,
        "长度范围": f"{int(min(lengths))}-{int(max(lengths))}",
        "以动作括号开头": f"{action_first:.0%}",
        "重合度均值": round(statistics.mean(scored), 3) if scored else 0.0,
        "重合度最大": round(max(scored), 3) if scored else 0.0,
        "判为逐字复读条数": sum(1 for s in scored if s >= DICE_REWRITE),
    }


def report(db: Path, limit: int) -> dict[str, Any]:
    if not db.exists():
        raise SystemExit(f"库不存在：{db}")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        roles = conn.execute("SELECT role_id, role_name FROM role_card ORDER BY role_id").fetchall()
        per_role: dict[str, Any] = {}
        for role in roles:
            rid = str(role["role_id"])
            rows = conn.execute(
                "SELECT text FROM agent_reachout WHERE role_id = ? ORDER BY id DESC LIMIT ?",
                (rid, limit),
            ).fetchall()
            memory = conn.execute(
                "SELECT COUNT(*) AS c FROM role_memory_item WHERE role_id = ?", (rid,)
            ).fetchone()
            per_role[rid] = {
                "名称": str(role["role_name"] or rid),
                **measure(list(reversed(rows))),  # 正序：判"抄前面的"要用时间序
                "记忆条数": int(memory["c"] or 0),
            }
        return {
            "库": str(db),
            "取样时间": datetime.now(UTC).isoformat(timespec="seconds"),
            "每角色窗口": limit,
            "角色": per_role,
            # 钱的事也在这把尺子上（审计 §12.8）：路由改云端后，"活人感"每一步都花真 token，
            # 而重生一次就是两次调用。不读库就报不出这个数，所以它跟着同一份只读连接走。
            "token": _token_report(conn),
        }
    finally:
        conn.close()


def _token_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """今日各后端的用量 + 最近几天的总量。老库里可能还没这张表 ⇒ 给个说明而不是崩。"""
    try:
        return {"今日": daily_usage(conn), "最近": usage_days(conn, limit=7)}
    except sqlite3.OperationalError:  # 没有 token_usage_day = 这份库比记账功能还老
        return {"说明": f"这份库还没有 token 账表（{local_day()} 起记账）"}


def reply_side(api: str, role_ids: Sequence[str], limit: int) -> dict[str, Any]:
    """回复侧：那条主动会话里**她说的**最近几条，用同一把尺子量。

    为什么必须单独量这个：最难看的一次复读发生在回复侧（用户说"刚跑完步"，她回了一句与更早
    某条主动开口逐字相同的话），而**回复不在 `agent_reachout` 里**。只量主动开口就会报出
    "逐字复读 0 条"这种好看但放错地方的数字 —— 尺子量错了东西，比没有尺子更糟。
    这一侧要读 checkpoint 回放，所以需要后端在跑；读不到只记一条 error，不整体失败。
    """
    out: dict[str, Any] = {}
    from rolecard_agent.config import Settings
    from rolecard_agent.core.identity import resolve_instance_identity

    owner = resolve_instance_identity(Settings.from_env())
    for rid in role_ids:
        url = (
            f"{api.rstrip('/')}/api/session/"
            f"{proactive_thread_id(rid, user_id=owner)}/messages?limit={limit}"
        )
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - 只连本机配置里的地址
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # 还没有过主动会话（从来没主动开过口）——那是"没有样本"，不是量不到。
                # 混成 error 会让人以为尺子坏了，从而跳过真正需要看的角色。
                out[rid] = {"样本数": 0, "说明": "还没有过主动会话"}
                continue
            out[rid] = {"error": f"HTTP {exc.code}"}
            continue
        except Exception as exc:  # noqa: BLE001 - 一个会话读不到不该让整把尺子失灵
            out[rid] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        msgs = data.get("messages") or []
        hers = [{"text": m.get("content") or ""} for m in msgs if m.get("role") == "assistant"]
        out[rid] = {
            **measure(hers),
            "用户条数": sum(1 for m in msgs if m.get("role") == "user"),
        }
    return out


#: 两张表共用的度量列（口径必须完全一致，否则主动侧和回复侧的数字没法比）。
#: 「原文去重」和「正文去重」分成两列是**故意的**：前者是量错的那把尺子，留着才看得出
#: 口癖藏在动作括号后面 —— 只报后者，下一个人没法知道为什么要多这么一列。
_METRIC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("样本", "样本数"),
    ("原文去重", "首6字去重率"),
    ("正文去重", "正文首6字去重率"),
    ("长度σ", "长度标准差"),
    ("长度CV", "长度变异系数"),
    ("长度范围", "长度范围"),
    ("动作开头", "以动作括号开头"),
    ("重合均", "重合度均值"),
    ("重合最大", "重合度最大"),
    ("复读条", "判为逐字复读条数"),
    ("型例比", "型例比"),
    ("词面复用", "词面复用均值"),
)

COLUMNS = (("角色", "rid"), *_METRIC_COLUMNS, ("记忆", "记忆条数"))

#: token 账那一栏。「没报」单独一列是重点：一次调用后端没回用量时，
#: 总数里就是没有它 —— 那一列不为 0 就说明账本在漏，而不是"今天很省"。
TOKEN_COLUMNS = (
    ("后端", "backend"),
    ("调用", "calls"),
    ("输入", "prompt"),
    ("输出", "completion"),
    # 「其中想」是输出的**子集**，不是第四段账单 —— 单列它才能回答"这些钱里有多少
    # 是她想出来的"（审计 §12.8 第二条；实测一条"在吗"回 616 个输出 token、其中 590 是想）。
    ("其中想", "reasoning"),
    ("合计", "total"),
    ("没报", "unreported"),
)

#: 回复侧没有"记忆条数"，但有"用户条数"——同一把尺子，最后一列换掉。
REPLY_COLUMNS = (("角色", "rid"), *_METRIC_COLUMNS, ("用户条数", "用户条数"))


def _print_token(token: Mapping[str, Any]) -> None:
    """今天各后端花了多少 token（审计 §12.8）。按后端分：云端是钱，本地是显存与时间。"""
    if token.get("说明"):
        print(f"\ntoken 账：{token['说明']}")
        return
    today = list(token.get("今日") or [])
    if not today:
        print("\ntoken 账：今天还没有一次调用被记下")
        return
    print("\n今日 token（按后端）")
    print("  ".join(name.ljust(10) for name, _ in TOKEN_COLUMNS))
    for row in today:
        cells = [str(row[key]) for _, key in TOKEN_COLUMNS]
        print("  ".join(c.ljust(10) for c in cells))
    days = list(token.get("最近") or [])
    if len(days) > 1:
        trend = "  ".join(f"{d['day'][5:]}:{d['total']}" for d in days[:7])
        print(f"最近几天总量：{trend}")


def _print_table(
    title: str, rows: Mapping[str, Mapping[str, Any]], columns: Sequence[tuple[str, str]]
) -> None:
    print(f"\n{title}")
    print("  ".join(name.ljust(10) for name, _ in columns))
    for rid, row in rows.items():
        if row.get("error"):
            print(f"{rid[:10]:<10}  读不到：{row['error']}")
            continue
        if not row.get("样本数"):
            # 没样本也要说清"为什么没得看"：主动侧看记忆条数，回复侧看是不是压根没开过口。
            hints = [str(row["说明"])] if row.get("说明") else []
            if "记忆条数" in row:
                hints.append(f"记忆 {row['记忆条数']} 条")
            tail = f"｜{'｜'.join(hints)}" if hints else ""
            print(f"{rid[:10]:<10}  （没有样本{tail}）")
            continue
        cells = []
        for _, key in columns:
            value = rid if key == "rid" else row[key]
            cells.append(f"{value:.2f}" if isinstance(value, float) else str(value))
        print("  ".join(c.ljust(10) for c in cells))
    # 口癖与"反复念叨的意象"单独一行：值是变长中文，塞进表格里会把整张表挤歪，
    # 而它们恰恰是最该被看见的两条 —— 尤其后者，它说的是**她卡在哪些词上**，不是又一个分数。
    for rid, row in rows.items():
        who = str(row.get("名称") or rid)
        if row.get("口癖条数", 0) > 1:
            print(f"{who[:10]:<10} 的口癖：正文开头 {row['口癖条数']} 次「{row['口癖开头']}」")
        if row.get("高频意象"):
            print(f"{who[:10]:<10} 反复用的意象：{row['高频意象']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="活人感的客观度量（只读）")
    parser.add_argument(
        "--db", type=Path, default=None, help="sqlite 路径，默认取配置里的 SQLITE_PATH"
    )
    parser.add_argument("--limit", type=int, default=20, help="每角色看最近多少条")
    parser.add_argument(
        "--api",
        default="",
        help="给了就连后端量回复侧，例如 http://127.0.0.1:8000（读的是 checkpoint 回放）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于改前改后 diff）")
    args = parser.parse_args()

    # 中文 Windows 的 GBK 代码页装不下这些字，先把 stdout 钉成 UTF-8（这台机器的老坑）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 默认看哪份库：**必须与 `persona_ab` / `persona_chat_sim` 同一个取法**（09-26 轮
    # R26-17 / S-7）。从前这里走 `Settings.from_env().sqlite_path` —— 那是**仓库开发态**
    # 那一份，而 09-24 之后真数据在安装目录下、开发态那份被隔离成空快照。结果就是：
    # 这把"改前/改后"的尺子与被量的那两条 A/B 臂看的不是同一个世界，读数没法对。
    # 判定逻辑因此收口到 `scratch_db.resolve_live_db()` 一处（env 覆盖 > 两根里内容较新者），
    # 空库永远当不上源那条规矩也只写一遍。
    if args.db:
        db = Path(args.db)
    else:
        sys.path.insert(0, str(ROOT / "scripts"))
        import scratch_db  # noqa: PLC0415

        db = scratch_db.resolve_live_db()
    if not db.is_absolute():
        db = (ROOT / db).resolve()
    data = report(db, args.limit)
    if args.api:
        data["回复侧"] = reply_side(args.api, list(data["角色"]), args.limit)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return

    print(f"库：{data['库']}｜取样：{data['取样时间']}｜每角色最近 {data['每角色窗口']} 条")
    _print_table("主动开口侧（来自 agent_reachout）", data["角色"], COLUMNS)
    if "回复侧" in data:
        _print_table("回复侧（来自那条主动会话的 checkpoint 回放）", data["回复侧"], REPLY_COLUMNS)
    _print_token(data.get("token") or {})
    print(
        "\n读法：两列去重率→1.00 越好（正文那列才是口癖所在，只报原文那列会量错东西）；"
        "长度看 **CV**（σ/均值）而不是绝对 σ —— 均值一变 σ 就跟着变（把回复从 161–202 压到"
        "74–95 那次，σ 反而从 12.0 掉到 6.1，看着像退步）；重合最大≥0.85 按逐字复读计；"
        "记忆 0 条 = 她没有任何关于你的事实可用；"
        "回复侧「用户条数」为 0 = 她那句根本没接住你说过的任何事；"
        "token 那栏的「没报」= 后端没回用量，**不是**这次不要钱。"
    )


if __name__ == "__main__":
    main()
