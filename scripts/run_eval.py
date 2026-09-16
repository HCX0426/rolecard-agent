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


def main() -> int:
    _maybe_configure_cloud_backend()
    parser = argparse.ArgumentParser(description="rolecard-agent 评测跑批")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--strict", action="store_true", help="任一用例失败则退出码 1")
    args = parser.parse_args()

    from fastapi.testclient import TestClient

    from rolecard_agent.api.main import create_app
    from rolecard_agent.config import Settings
    from rolecard_agent.domains.registry import DOMAINS
    from rolecard_agent.storage.db import connect

    cases: list[dict[str, object]] = []
    for path in sorted(args.cases.glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8")))
    if not cases:
        print(f"未找到评测用例：{args.cases}")
        return 2

    settings = Settings.from_env()
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

    by_path: dict[str, list[dict[str, object]]] = {}
    for r in results:
        by_path.setdefault(str(r["path"]), []).append(r)

    print("\n=== 评测报告 ===")
    total_pass = sum(1 for r in results if r["passed"])
    for path, items in by_path.items():
        passed = sum(1 for r in items if r["passed"])
        avg_ms = sum(int(r.get("duration_ms", 0)) for r in items) // len(items)
        print(f"{path:<14} {passed}/{len(items)}  平均 {avg_ms}ms")
        for r in items:
            mark = "✅" if r["passed"] else "❌"
            print(f"  {mark} {r['id']} ({r.get('duration_ms', '?')}ms)")
            for f in r["failures"]:
                print(f"     - {f}")
    print(f"\n总体：{total_pass}/{len(results)}")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    # newline 强制 LF：报告本身不入库（.gitignore），但离线打开不该带 CRLF
    args.report.write_text(
        json.dumps(
            {"results": results, "passed": total_pass, "total": len(results)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"报告已写入 {args.report}")
    return 1 if args.strict and total_pass < len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
