"""npm 侧依赖漏洞扫描（2026-11-01 补上 —— 此前本仓只有 Python 一侧有尺子）。

**为什么补这一把**：`scripts/tools/audit_deps.py` 只扫 pip（pip-audit 问 OSV），
npm 侧**一把尺子都没有** —— 于是 minimatch 的 DoS（GHSA-g3hv-75wg-m249，10-30 披露）
在本仓躺了两天没人发现，直到 Dependabot 的月报把它推出来。Dependabot 是**月节奏**，
而"新披露"是**时刻**发生的： disclosures 与 CI 之间隔着一整月，窗口太长。

**机制与 pip 侧同款**（不是新发明）：
- 扫**当前锁文件**（`npm audit --package-lock-only --json`），问 npm 的 advisory 库；
- 对照 `config/dependency-audit-allowlist.json` 的同族豁免表：**新出现的告警 = 红**；
- 在册豁免带"上游还没给修复版本"这个前提，一旦前提消失（上游发了修好的版本），
  扫描**反过来报红** —— 豁免不是永久通行证（与 pip 侧同一个道理）；
- **fail-closed**：扫不成（断网/接口变更/npm 没装）退 2，门禁同样红 ——
  **扫不成不等于干净**（pip 侧那行注释原话，这条纪律对 npm 侧一样成立）。

**为什么走 npm registry 而不是 OSV**：npm 的 advisory 库（`/-/npm/v1/security/*`）已经
覆盖了 npm 生态的披露，且与 `npm audit` 同一份数据口径 —— 少一次转换，少一处漂移。
注意：**本机默认的腾讯云镜像不实现该端点**（实测 `[NOT_IMPLEMENTED]`），
所以本脚本强制走官方 registry；这也是"扫不成 ≠ 干净"的又一个形态。

跑法（在仓库根）：
    .venv\\Scripts\\python.exe scripts/tools/audit_deps_npm.py           # 人读摘要
    .venv\\Scripts\\python.exe scripts/tools/audit_deps_npm.py --json   # 机器读
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = ROOT / "config" / "dependency-audit-allowlist.json"

# Windows 控制台默认 GBK，而这份输出里有 🔴/🟡/⚪/✅ 这类 GBK 装不下的字符 —— 不重配编码，
# 判据读到的是问号，最坏是 print 那一句自己抛 UnicodeEncodeError 退出码 1（门禁
# `console encoding` 那条尺子看的 `reconfigure` 就是这个；同族事故 `R26-24`：产物全部落盘了，
# 收尾那句带对勾的 print 在 GBK 控制台上炸成"打包失败"）。
# 与 `scripts/tools/audit_deps.py` 同一条纪律、同一段写法 —— 两把尺子挨着，共一份理由。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

#: 要扫的 npm 工作区（各自带 package-lock.json）。shell 是 Electron 壳，
#: frontend 是控制台 —— 两边的 devDependency 树都进过真实漏洞（minimatch 就是）。
WORKSPACES = ("shell", "frontend")

#: 官方 registry（镜像不实现 audit 端点 —— 见模块 docstring）。
REGISTRY = "https://registry.npmjs.org"

#: npm audit 的退出码约定：0 = 没有告警，1 = 有告警，其余 = 它自己没跑成。
RC_CLEAN = 0
RC_FINDINGS = 1


def _npm() -> str:
    """npm 的可执行路径。Windows 上 `npm` 是 `npm.cmd`（subprocess 找它要显式给）。"""
    return "npm.cmd" if os.name == "nt" else "npm"


def scan(workspace: str) -> tuple[dict[str, Any] | None, str, int]:
    """扫一个工作区：`(payload, stderr, rc)`。payload 为 None = 扫不成。"""
    cwd = ROOT / workspace
    if not (cwd / "package-lock.json").exists():
        return None, f"{workspace}/package-lock.json 不存在", 2
    env = dict(os.environ)
    env["npm_config_registry"] = REGISTRY
    proc = subprocess.run(  # noqa: S603
        [
            _npm(),
            "audit",
            "--package-lock-only",
            "--json",
            "--registry",
            REGISTRY,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=600,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, f"npm audit 输出不是 JSON（{exc}）：{(proc.stderr or '')[:200]}", 2
    return payload, proc.stderr or "", proc.returncode


def flatten(payload: dict[str, Any], workspace: str) -> list[dict[str, Any]]:
    """把 npm audit 的输出拍平成 `{package, id, workspace, fix_versions}`。

    npm 的形状：`vulnerabilities` 是"包名 → {severity, via[], range, nodes[]}"，
    而 `via` 里的元素可能是字符串（指向另一个漏洞）或对象（这条漏洞本身）。
    """
    found: list[dict[str, Any]] = []
    for name, info in (payload.get("vulnerabilities") or {}).items():
        vias = info.get("via") or []
        for via in vias:
            if isinstance(via, str):
                continue  # 间接依赖：真正的告警在它指向的那个包上
            advisory_id = str(via.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            if not advisory_id:
                advisory_id = via.get("title") or name
            fix = ""
            for fix_item in via.get("range") and [] or []:
                fix = fix_item
            available = info.get("fixAvailable")
            if isinstance(available, dict):
                fix = str(available.get("version") or "")
            elif isinstance(available, bool):
                fix = "true" if available else ""
            found.append(
                {
                    "package": name,
                    "id": advisory_id,
                    "severity": via.get("severity") or info.get("severity") or "",
                    "workspace": workspace,
                    "fix_versions": fix,
                    "title": via.get("title") or "",
                }
            )
    return found


def load_allowlist(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读豁免表。返回 `(id → 条目, 表本身的问题)`。

    表里的条目是**按 (包, id) 认领**的（与 pip 侧同款）：npm 侧的 id 用 advisory
    尾号（`GHSA-…` / `CVE-…`），取不到就退回包名 —— 宁可宽认也不能把真漏洞放过去。
    """
    problems: list[str] = []
    if not path.exists():
        return {}, [f"豁免表不在：{path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, [f"豁免表不是合法 JSON：{exc}"]
    entries = data.get("豁免") or []
    if not isinstance(entries, list):
        return {}, ["豁免表的「豁免」不是数组"]
    table: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("豁免表里有一条不是对象")
            continue
        package = str(entry.get("package") or "")
        if not package:
            problems.append("豁免表里有一条没有 package")
            continue
        if not entry.get("理由"):
            problems.append(f"豁免表里 {package} 那条没有理由（空理由 = 门禁红）")
        aliases = entry.get("aliases") or []
        if not isinstance(aliases, list):
            problems.append(f"豁免表里 {package} 那条的 aliases 不是数组")
            continue
        for alias in [package, *aliases]:
            table[str(alias)] = entry
    return table, problems


def classify(
    findings: list[dict[str, Any]], table: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """红/豁免/前提失效三分（同 pip 侧 `classify` 的语义）。

    - **红**：不在表里的告警（新披露 = 门禁红）。
    - **豁免**：在表里，且**豁免表登记的上游修复版本为空** —— 前提成立（上游还没修）。
    - **前提失效**：在表里，但豁免表登记了上游修复版本 —— 豁免的前提没了，
      报红逼人回来处理（豁免是带复查条件的欠条，不是通行证）。

    **"上游有没有修"只认豁免表那一格**（人工登记），**不拿 npm 的 `fixAvailable` 推断** ——
    npm 给的是"**在你当前的版本约束下**能升到哪个版本"，不是"上游发布了哪个修复版"。
    minimatch 那次就是分水岭：npm 建议升 8.14.0（因为它被 ^3 的父声明钉着），
    而上游的修复版是 11.0.1。拿 npm 的建议当"上游已修"，会把一条成立的豁免误判成
    前提失效（实测踩过），反过来把豁免面收得比该有的更紧 —— 那是**误红**，不是严格。
    """
    red: list[dict[str, Any]] = []
    exempt: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for finding in findings:
        keys = [finding["id"], finding["package"], *finding.get("aliases", [])]
        entry = next((table[str(k)] for k in keys if str(k) in table), None)
        if entry is None:
            red.append(finding)
            continue
        if entry.get("上游修复版本"):
            # 表里登记了上游修复版本 ⇒ 前提没了 ⇒ 豁免不成立。
            stale.append(finding)
            continue
        exempt.append(finding)
    return {"red": red, "exempt": exempt, "stale": stale}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="npm 侧依赖漏洞扫描（对照在册豁免）")
    parser.add_argument("--json", action="store_true", help="输出机器读的 JSON")
    args = parser.parse_args(argv)

    entries, table_problems = load_allowlist(ALLOWLIST_PATH)
    findings: list[dict[str, Any]] = []
    scan_problems: list[str] = []
    for workspace in WORKSPACES:
        payload, stderr_text, rc = scan(workspace)
        if payload is None:
            scan_problems.append(f"{workspace}: 扫不成（退出码 {rc}）：{stderr_text[:200]}")
            continue
        findings.extend(flatten(payload, workspace))

    if scan_problems:
        # **扫不成不等于干净**（与 pip 侧同一条纪律）。
        message = "；".join(scan_problems)
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False))
        else:
            print(f"❌ npm 侧扫描跑不起来：{message}", file=sys.stderr)
            print(
                "   （扫不成不等于没漏洞：这一格该红。本机默认镜像不实现 audit 端点，"
                "脚本已强制走官方 registry —— 若仍失败，先看网络。）",
                file=sys.stderr,
            )
        return 2

    verdict = classify(findings, entries)
    blocked = bool(table_problems or verdict["red"] or verdict["stale"])

    if args.json:
        print(
            json.dumps(
                {
                    "workspaces": list(WORKSPACES),
                    "findings": findings,
                    "red": verdict["red"],
                    "exempt": verdict["exempt"],
                    "stale": verdict["stale"],
                    "table_problems": table_problems,
                    "blocked": blocked,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if blocked else 0

    total = len(findings)
    if not total:
        print(f"✅ npm 侧 {len(WORKSPACES)} 个工作区：0 条告警")
        return 0
    print(f"npm 侧 {len(WORKSPACES)} 个工作区：{total} 条告警")
    for label, items in (
        ("🔴 红（新披露，必须处理）", verdict["red"]),
        ("🟡 豁免前提失效（上游已给修复版本）", verdict["stale"]),
    ):
        if items:
            print(f"\n{label}（{len(items)}）")
            for item in items:
                fix = f"  修复版本：{item['fix_versions']}" if item["fix_versions"] else ""
                print(
                    f"  {item['package']} @ {item['workspace']} — {item['id']}"
                    f"（{item['severity']}）{fix}"
                )
    if verdict["exempt"]:
        print(f"\n⚪ 在册豁免（{len(verdict['exempt'])}）：上游还没有修复版本")
        for item in verdict["exempt"]:
            print(f"  {item['package']} @ {item['workspace']} — {item['id']}")
    for problem in table_problems:
        print(f"\n❌ 豁免表问题：{problem}")
    if blocked:
        print(
            "\n结论：红 —— 新披露的告警要处理（在册豁免的前提是「上游还没给修复版本」，"
            "那个前提消失时豁免自动失效）。",
            file=sys.stderr,
        )
        return 1
    print("\n结论：绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
