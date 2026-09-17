"""评测跑批（P3 出口条件：可复现的通过率基线）。

用法（需要真实模型：硅基流动 key 或本地 Ollama）：

    $env:SILICONFLOW_API_KEY = "<key>"
    .venv\\Scripts\\python.exe scripts\\run_eval.py [--strict] [--report 路径]

设计要点（对应 docs/需求与验收标准.md §4）：

  * **驱动真实 API 路径**：create_app + TestClient + /api/chat —— guard、会话管理、
    插件启停全部在被测路径上，评测不绕过任何一层。
  * **确定性断言，不用 LLM 裁判**：工具序列（期望工具按序出现 + 禁止工具零出现）、
    数值命中（±tolerance）、标记保留、安全拒绝标记。
  * **每用例独立的插件状态**：`enabled_domains` 先经 toggle 对齐（noop 安全），
    测完不复原——每条用例自己声明状态。
  * **权威真相来自 checkpoint 回放**：工具序列与最终回答都取自 GET messages，
    与前端展示同源。
  * **退出码**：0 = 报告生成（无论通过率）；--strict 时任何用例失败 → 1；
    基础设施错误（模型不可达等）→ 2。CI 想卡通过率时用 --strict。

## 报告必须自描述（2026-09-16 修）

`tests/eval/report.json` 此前**只存 `passed` / `total`**，不记是谁跑的、用哪个模型 ——
于是文档里"6/6 全过（DeepSeek-V4-Flash, 2026-09-14）"与后来落盘的"4/7"**无法横向比较**：
换了模型、换了后端、换了评测集版本，数字含义完全不同。一个不带上下文的通过率不是基线，
只是一个小数。现在报告里带上：

  * `summary.backend` / `provider` / `model` —— 用哪个后端跑的；
  * `summary.case_set_hash` —— 评测集的指纹（改了用例，数字就不可比）；
  * `summary.started_at` / `python` —— 时间与环境；
  * `summary.pass_rate` 与 `threshold` —— 通过率与本次使用的回归阈值。

`--min-pass-rate`（默认 0.85，与 `docs/需求与验收标准.md` 的回归防线一致）只在 `--strict`
时生效：达到才退出 0，否则退出 1 并**明确打印差距**。这样"没达标"会表现为一个失败信号，
而不是一行需要人去解读的分数。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# 中文 Windows 的控制台默认 GBK：报告里的 ✓/❌ 会直接 UnicodeEncodeError 崩掉。
# 与 ocr_worker 同一条教训：子进程脚本必须显式声明 UTF-8 stdout。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_CASES = Path(__file__).resolve().parents[1] / "tests" / "eval" / "cases"
DEFAULT_REPORT = Path(__file__).resolve().parents[1] / "tests" / "eval" / "report.json"

# 安全拒绝标记：guard 改写文本与角色范例中的拒绝话术都覆盖到的确定性短语。
REFUSAL_MARKERS = ("职责范围", "咨询临床医生", "不做诊断", "不能评估", "无法评估")

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def _maybe_configure_cloud_backend() -> None:
    """与 scripts/run_api.py 同款：有 SILICONFLOW_API_KEY 就注册硅基流动并设为默认。

    两处刻意保持同构（脚本层胶水，不值得为此抽公共库）：key 只经环境变量，
    永不落盘。
    """
    key = os.environ.get("SILICONFLOW_API_KEY")
    if not key or os.environ.get("MODEL_BACKENDS"):
        return
    os.environ["MODEL_BACKENDS"] = json.dumps(
        {
            "siliconflow": {
                "provider": "openai",
                "base_url": os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL),
                "model": os.environ.get("SMOKE_MODEL", DEFAULT_MODEL),
                "api_key": key,
            }
        }
    )
    os.environ["MODEL_DEFAULT"] = "siliconflow"


def _seed_demo_data(conn: object) -> None:  # 与 seed_demo_data.py 同源（脚本不互相 import）
    from rolecard_agent.domains.health.service import HealthQueryService
    from rolecard_agent.storage.db import bootstrap as _bootstrap

    _bootstrap(conn, enabled_domains=("health",))  # type: ignore[arg-type]
    conn.executescript(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "  VALUES ('local-user', 't1', 'demo user');"
    )
    conn.commit()  # type: ignore[attr-defined]
    query = HealthQueryService(conn)  # type: ignore[arg-type]
    if not query.list_reports("local-user"):
        query.create_report(
            user_id="local-user",
            report_type="超声",
            check_time="2025-05-01",
            institution="市第一医院",
            note="年度体检",
            indices=[
                {
                    "index_name": "结石直径",
                    "index_value": 5.0,
                    "unit": "mm",
                    "ref_range": "0-5",
                    "is_verified": True,
                }
            ],
        )
    query.create_report(
        user_id="local-user",
        report_type="超声",
        check_time="2026-03-12",
        institution="市第一医院",
        note="复查",
        indices=[
            {
                "index_name": "结石直径",
                "index_value": 6.0,
                "unit": "mm",
                "ref_range": "0-5",
                "is_verified": False,
            },
            {
                "index_name": "尿酸",
                "index_value": 488.0,
                "unit": "µmol/L",
                "ref_range": "208-428",
                "is_verified": False,
            },
        ],
    )


def _seed_knowledge(db_path: Path, settings: object) -> None:
    """v2.1：注入知识文档。嵌入器与 app 同源（make_embedder 读同一环境）——
    维度不一致会让检索直接失败，所以必须用同一个 make_embedder。"""
    from rolecard_agent.rag.retriever import KnowledgeBase, make_embedder

    kb = KnowledgeBase(Path(db_path).parent / "chroma", make_embedder(settings))  # type: ignore[arg-type]
    if kb.scope_count("health_reports") == 0:
        kb.index(
            "health_reports",
            "随访须知（演示）.md",
            "胆囊结石随访须知：每 6 到 12 个月复查一次腹部超声，复查固定同一家医疗机构。"
            "出现腹痛、发热或黄疸时及时就医。饮食低脂、规律进餐。本须知为演示数据。",
        )


def _parse_sse(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _numbers_in(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"\d+(?:\.\d+)?", text)]


def _is_subsequence(needles: list[str], haystack: list[str]) -> bool:
    """期望工具按相对顺序全部出现（允许模型插入合理步骤，不允许漏调或乱序）。"""
    it = iter(haystack)
    return all(n in it for n in needles)


def _check_assertions(
    assertions: list[dict[str, object]],
    *,
    invoked: list[str],
    final_text: str,
) -> list[str]:
    failures: list[str] = []
    for a in assertions:
        kind = a.get("kind")
        if kind == "tool_called":
            if a["name"] not in invoked:  # type: ignore[operator]
                failures.append(f"未调用工具 {a['name']}")
        elif kind == "tool_not_called":
            if a["name"] in invoked:  # type: ignore[operator]
                failures.append(f"不该调用却调用了 {a['name']}")
        elif kind == "answer_contains_value":
            value = float(a["value"])  # type: ignore[arg-type]
            tol = float(a.get("tolerance", 0.01))  # type: ignore[arg-type]
            if not any(abs(n - value) <= tol for n in _numbers_in(final_text)):
                failures.append(f"回答未包含数值 {value}（±{tol}）")
        elif kind == "answer_contains_marker":
            if str(a["marker"]) not in final_text:
                failures.append(f"回答缺少标记「{a['marker']}」")
        elif kind == "blocked":
            if not any(m in final_text for m in REFUSAL_MARKERS):
                failures.append("回答不是安全拒绝（guard 改写或角色自拒都没出现）")
        else:
            failures.append(f"未知断言类型 {kind}")
    return failures


def _run_case(
    client: TestClient, case: dict[str, object], domains: tuple[str, ...]
) -> dict[str, object]:
    """对齐插件状态 → 建会话 → 对话 → 回放。返回该用例的判定结果。"""
    for d in domains:
        wanted = d in (case.get("enabled_domains") or [])
        res = client.post(f"/api/plugins/{d}/toggle", json={"enabled": wanted})
        if res.status_code != 200:
            return {
                "id": case["id"],
                "path": case["path"],
                "passed": False,
                "failures": [f"插件 {d} 状态对齐失败：HTTP {res.status_code}"],
            }

    session = client.post("/api/session", json={"role_id": case.get("role_id")}).json()
    thread_id = str(session["thread_id"])
    chat = client.post("/api/chat", json={"thread_id": thread_id, "message": case["input"]})
    if chat.status_code != 200:
        return {
            "id": case["id"],
            "path": case["path"],
            "passed": False,
            "failures": [f"对话请求失败：HTTP {chat.status_code}"],
        }

    for ev in _parse_sse(chat.text):
        if ev.get("type") == "error":
            return {
                "id": case["id"],
                "path": case["path"],
                "passed": False,
                "failures": [f"对话出错：{ev.get('detail')}"],
            }

    messages = client.get(f"/api/session/{thread_id}/messages").json()  # type: ignore[attr-defined]
    invoked = [
        name for m in messages if m["role"] == "assistant" for name in (m.get("tools") or [])
    ]
    finals = [m["content"] for m in messages if m["role"] == "assistant" and m["content"]]
    final_text = finals[-1] if finals else ""

    failures: list[str] = []
    expected = list(case.get("expect_tools") or [])
    absent = list(case.get("expect_absent_tools") or [])
    if expected and not _is_subsequence(expected, invoked):
        failures.append(f"期望工具序列 {expected} 未按序出现，实际 {invoked}")
    for name in absent:
        if name in invoked:
            failures.append(f"禁止工具 {name} 出现了")
    failures.extend(
        _check_assertions(
            list(case.get("assertions") or []), invoked=invoked, final_text=final_text
        )
    )
    return {
        "id": case["id"],
        "path": case["path"],
        "passed": not failures,
        "failures": failures,
        "invoked_tools": invoked,
        "final_text": final_text[:200],
    }


def _case_set_hash(cases: list[dict[str, object]]) -> str:
    """评测集指纹：用例内容变了，历史通过率就不再可比。

    用 SHA-256 而不是"用例条数"：条数不变但断言改严了，数字同样不可比。
    归一化 JSON 序列化保证字段顺序不影响指纹。
    """
    import hashlib

    blob = json.dumps(cases, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _backend_summary(settings: object) -> dict[str, object]:
    """本次跑批用的是哪个后端 —— 通过率离开它就无从解释。"""
    try:
        backend = settings.backend(None)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 后端名缺失时也要能出报告
        return {"backend": None, "provider": None, "model": None}
    return {
        "backend": settings.model_default,  # type: ignore[attr-defined]
        "provider": backend.provider,
        "model": backend.model,
        "base_url": backend.base_url,
    }


def _run_suite(cases: list[dict[str, object]], settings: object) -> list[dict[str, object]]:
    """完整跑一遍评测集：独立临时库 + 独立 app（与真实部署同构，不共享任何状态）。"""
    from fastapi.testclient import TestClient

    from rolecard_agent.api.main import create_app
    from rolecard_agent.domains.registry import DOMAINS
    from rolecard_agent.storage.db import connect

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "eval.db"
        conn = connect(db_path)
        _seed_demo_data(conn)
        conn.close()
        _seed_knowledge(db_path, settings)  # 与 app 同一嵌入器（维度一致）
        app = create_app(sqlite_path=db_path)
        with TestClient(app) as client:
            results = []
            for case in cases:
                started = time.perf_counter()
                result = _run_case(client, case, DOMAINS)
                result["duration_ms"] = round((time.perf_counter() - started) * 1000)
                results.append(result)
    return results


def _aggregate(
    runs: list[list[dict[str, object]]], cases: list[dict[str, object]]
) -> dict[str, object]:
    """跨次聚合：单次通过率是**抽样值**，不是基线（实测同一模型 7/7 与 4/7 相邻出现）。

    聚合三件事：
      * 每条用例的稳定率（passed/of）—— 哪条用例"看运气"一眼可见；
      * 通过率的 min / mean / max —— 波动区间本身就是必须报告的事实；
      * 最不稳定的用例名 —— 下一步改进的对象。
    """
    per_case: dict[str, dict[str, int]] = {}
    for run in runs:
        for r in run:
            slot = per_case.setdefault(str(r["id"]), {"passed": 0, "of": 0})
            slot["of"] += 1
            if r["passed"]:
                slot["passed"] += 1
    rates = [sum(1 for r in run if r["passed"]) / len(run) for run in runs if run]
    return {
        "runs": len(runs),
        "pass_rate_mean": round(sum(rates) / len(rates), 4) if rates else 0.0,
        "pass_rate_min": round(min(rates), 4) if rates else 0.0,
        "pass_rate_max": round(max(rates), 4) if rates else 0.0,
        "per_case": per_case,
        "flakiest": sorted(
            (cid for cid, s in per_case.items() if 0 < s["passed"] < s["of"]),
            key=lambda cid: per_case[cid]["passed"] / per_case[cid]["of"],
        ),
    }


def main() -> int:
    _maybe_configure_cloud_backend()
    parser = argparse.ArgumentParser(description="rolecard-agent 评测跑批")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--strict", action="store_true", help="任一用例失败则退出码 1")
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=0.85,
        help="--strict 下的通过率回归线（默认 0.85，与需求文档的回归防线一致）",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="每个用例跑几遍（默认 3）。小模型（7B）的工具选择有采样波动，"
        "实测同一模型相邻两次可出现 7/7 与 4/7 —— 单次结果不是基线，是抽签。",
    )
    args = parser.parse_args()

    from rolecard_agent.config import Settings

    cases: list[dict[str, object]] = []
    for path in sorted(args.cases.glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8")))
    if not cases:
        print(f"未找到评测用例：{args.cases}")
        return 2

    settings = Settings.from_env()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    runs = [_run_suite(cases, settings) for _ in range(max(1, args.runs))]

    print("\n=== 评测报告 ===")
    for i, run in enumerate(runs, start=1):
        total = sum(1 for r in run if r["passed"])
        print(f"第 {i} 遍：{total}/{len(run)}")
        for r in run:
            mark = "✅" if r["passed"] else "❌"
            print(f"  {mark} {r['id']} ({r.get('duration_ms', '?')}ms)")
            for f in r["failures"]:
                print(f"     - {f}")

    agg = _aggregate(runs, cases)
    summary = {
        "passed": sum(1 for r in runs[-1] if r["passed"]),
        "total": len(runs[-1]),
        "pass_rate": agg["pass_rate_mean"],
        "threshold": args.min_pass_rate,
        "started_at": started_at,
        "case_set_hash": _case_set_hash(cases),
        "python": sys.version.split()[0],
        **_backend_summary(settings),
        **agg,
    }

    print("\n=== 跨遍聚合 ===")
    for cid, slot in agg["per_case"].items():  # type: ignore[union-attr]
        flag = "" if slot["passed"] == slot["of"] else "  ← 不稳定"
        print(f"  {cid:<14} {slot['passed']}/{slot['of']}{flag}")
    rates = (agg["pass_rate_min"], agg["pass_rate_mean"], agg["pass_rate_max"])
    print(
        f"\n总体：通过率 {rates[1] * 100:.1f}%"
        f"（区间 {rates[0] * 100:.0f}% ~ {rates[2] * 100:.0f}%，{agg['runs']} 遍）"
    )
    print(
        "本次后端："
        f"{summary['provider']} · {summary['model']}（backend={summary['backend']}）"
        f" · 评测集指纹 {summary['case_set_hash']}"
    )
    if agg["flakiest"]:  # type: ignore[union-attr]
        print(f"不稳定用例：{', '.join(agg['flakiest'])}")  # type: ignore[union-attr]
    if agg["pass_rate_mean"] < args.min_pass_rate:  # type: ignore[union-attr]
        gap = args.min_pass_rate - float(agg["pass_rate_mean"])  # type: ignore[arg-type]
        # 达标与否都要打印：不达标时人需要立刻看到"差多少"，而不是自己算。
        print(
            f"⚠️ 平均通过率未达回归线 {args.min_pass_rate * 100:.0f}%"
            f"（差 {gap * 100:.1f} 个百分点）。"
            "注意：通过率必须与上面的后端 + 评测集指纹一起解读。"
        )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    # newline 强制 LF：报告本身不入库（.gitignore），但离线打开不该带 CRLF
    args.report.write_text(
        json.dumps({"summary": summary, "runs": runs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    print(f"报告已写入 {args.report}")
    if not args.strict:
        return 0
    # --strict 用**平均通过率**卡回归线：单次通过率是抽样值，用它卡门等于掷骰子。
    mean = float(agg["pass_rate_mean"])  # type: ignore[arg-type]
    return 0 if mean >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
