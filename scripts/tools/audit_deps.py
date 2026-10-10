"""依赖漏洞扫描（2026-10-04 快照「CI 无 pip-audit/密钥扫描」那一格）。

跑法：

    .venv\\Scripts\\python.exe scripts/tools/audit_deps.py            # 人读摘要
    .venv\\Scripts\\python.exe scripts/tools/audit_deps.py --json     # 机器读

**为什么这不是 pytest 里的一条用例**：本仓的铁律是测试全部离线运行（README 首屏那一格自己
就写着"全部离线"），而 OSV 查询要出网 —— 把它写成用例会造出一个"断网就跑不绿的测试"。
所以它是**一条可单独跑的命令**（外加 CI 的一步，见下）。

**为什么这一格还没变成 CI 的红线**：快照那一行自己写了条件 —— "pip-audit 进门禁
（**锁文件落地后基线稳定**）"。现在 `requirements-rag.txt` 是 `chromadb>=1.5.9` 这种无上界
写法，同一份豁免表可能因为上游发了新版而失效或复活，直接进 CI 就会造出一道**没有人能让它
变绿**的红（要等锁文件那条先落地，之后再接）。这一刀先交"随时可跑、已在真实环境量过"的那一半。

豁免表 `dependency-audit-allowlist.json` 的语义**不是"忽略"**，这是整条命令的全部设计约束：

  * 不在表上的告警 → **红**（新 CVE 必须被人看见一次）；
  * 在表上、但上游**已经给出修复版本** → 也**红**：每条豁免的前提是"没得升"，
    前提没了这张欠条就该销 —— 否则它就退化成一张永久通行证，而永久通行证的下场是没人再读；
  * 在表上、这次没扫出来 → **warn**：环境本来就会变（包升上去了），但一张没人复查的表会烂掉，
    所以不许静默；
  * 表上任何一条**空理由 / 空入库日期** → **红**（与 `requirements 分类` 那条尺子同一口径）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = ROOT / "config" / "dependency-audit-allowlist.json"

# Windows 控制台默认 GBK，而这份输出里有 ❌/⚠️ 这类 GBK 装不下的字符 —— 不重配编码，
# 判据读到的是问号，最坏是 print 那一句自己抛 UnicodeEncodeError 退出码 1（门禁
# `console encoding` 那条尺子看的 `reconfigure` 就是这个；同族事故 `R26-24`：产物全部落盘了，
# 收尾那句带对勾的 print 在 GBK 控制台上炸成"打包失败"）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

#: pip-audit 的退出码约定：0 = 没有告警，1 = 有告警，其余 = 它自己没跑成。
RC_CLEAN = 0
RC_FOUND = 1


def load_allowlist(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读豁免表，返回 `{id: 条目}` 与**表自身**的问题清单（空理由、缺字段、重复 id）。"""
    problems: list[str] = []
    if not path.exists():
        return {}, [f"豁免表不见了：{path.name}（扫描本身要出网，这张表是它唯一的判据）"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, [f"豁免表读不出来：{exc}"]
    entries: dict[str, dict[str, Any]] = {}
    for item in data.get("豁免") or []:
        vid = str(item.get("id") or "").strip()
        if not vid:
            problems.append("有一条豁免没写 id（表里的每一条都得能被点名）")
            continue
        if vid in entries:
            problems.append(f"豁免 id 重复：{vid}")
        for field in ("理由", "入库"):
            if not str(item.get(field) or "").strip():
                problems.append(f"豁免 {vid} 的「{field}」是空的（空理由不算理由）")
        entries[vid] = item
    return entries, problems


def flatten(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """pip-audit 的 JSON 拍平成 `{package, id, fix_versions, aliases}`，按 (包, id) 去重。

    去重是必需的：实测本机环境里同一条 `PYSEC-2026-311` 出现了两次（两个 dist 记录指向同一个
    版本），不去重就会数出"5 条告警"这种比现实更吓人的数。
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for dep in payload.get("dependencies") or []:
        name = str(dep.get("name") or "?")
        for vuln in dep.get("vulns") or []:
            vid = str(vuln.get("id") or "?")
            key = (name, vid)
            if key in seen:
                continue
            seen[key] = {
                "package": name,
                "version": str(dep.get("version") or "?"),
                "id": vid,
                "fix_versions": [str(v) for v in (vuln.get("fix_versions") or [])],
                "aliases": [str(a) for a in (vuln.get("aliases") or [])],
            }
    return sorted(seen.values(), key=lambda row: (row["package"], row["id"]))


def classify(
    findings: list[dict[str, Any]], entries: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """把扫出来的告警分三类：`red`（要拦）、`exempt`（在册且前提仍成立）、`stale`（欠条该查）。"""
    red: list[dict[str, Any]] = []
    exempt: list[dict[str, Any]] = []
    for row in findings:
        entry = entries.get(row["id"])
        if entry is None:
            red.append({**row, "why": "不在豁免表上"})
            continue
        if row["fix_versions"]:
            # 豁免的前提是"上游没得升"。前提没了，这条就不再是豁免，而是一个待办的升级。
            red.append({**row, "why": f"豁免前提没了（上游已出修复版本 {row['fix_versions']}）"})
        else:
            exempt.append(row)
    hit_ids = {row["id"] for row in findings}
    stale = [dict(entry, id=vid) for vid, entry in sorted(entries.items()) if vid not in hit_ids]
    return {"red": red, "exempt": exempt, "stale": stale}


def run_pip_audit() -> tuple[dict[str, Any] | None, str, int]:
    """跑 pip-audit（扫**当前环境**——那才是真正在跑的依赖树，含传递依赖）。

    `-X utf8` 不是可选项：这些 requirements 文件里有中文，Windows 上 pip 的需求文件解析器按
    locale（GBK）解码会在**读文件**那一步就抛 UnicodeDecodeError（本机实测）。Linux runner
    没有这个问题，但同一条命令在两台机器上都得能跑 —— 否则"本地量到的基线"与 CI 不是同一个。
    """
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "pip_audit",
        "--progress-spinner",
        "off",
        "-f",
        "json",
    ]
    # **挂死必须变成一次干净的 2，不是 traceback，更不是无限等**（2026-10-09）：这一步与
    # 密钥扫描同形状地没有超时，OSV 抽风时它吊着整条 CI job 直到 25 分钟被杀 —— 而 GitHub
    # 对 cancelled job **不上传日志**，现场直接没了（run 37804463056 就是这么死的，只能倒推）。
    # 「扫不成不等于干净」这条判据早就写在文件头，缺的只是把"问不到"也归进扫不成的形状。
    # 600s：本机实测全环境审计 54s（180 个包逐个问 OSV），十倍余量给 OSV 慢，但绝不无限。
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        # rc 用 pip-audit 自己的约定里"它自己没跑成"那一类（非 0/1），文案接管解释。
        return None, "pip-audit 600s 没答完（OSV 不可达/慢），按扫不成处理", 124
    if proc.returncode not in (RC_CLEAN, RC_FOUND):
        return None, (proc.stderr or proc.stdout).strip(), proc.returncode
    try:
        return json.loads(proc.stdout or "{}"), proc.stderr or "", proc.returncode
    except json.JSONDecodeError as exc:
        return None, f"输出的 JSON 读不出来：{exc}", proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="依赖漏洞扫描（对照在册豁免）")
    parser.add_argument("--json", action="store_true", help="输出机器读的 JSON")
    args = parser.parse_args(argv)

    entries, table_problems = load_allowlist(ALLOWLIST_PATH)
    payload, stderr_text, rc = run_pip_audit()
    if payload is None:
        # **扫不成不等于干净**：没装 pip-audit、断网、OSV 挂了，都长得很像"这次没问题"。
        message = f"扫描跑不起来（pip-audit 退出码 {rc}）：{stderr_text[:300]}"
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False))
        else:
            print(f"❌ {message}", file=sys.stderr)
            print(
                "   （扫不成不等于没漏洞：CI 上这一步该红；本地跑先装 pip-audit。）",
                file=sys.stderr,
            )
        return 2

    findings = flatten(payload)
    verdict = classify(findings, entries)
    blocked = table_problems or verdict["red"]

    if args.json:
        print(
            json.dumps(
                {
                    "scanned": len(payload.get("dependencies") or []),
                    "findings": findings,
                    "red": verdict["red"],
                    "exempt": verdict["exempt"],
                    "stale": verdict["stale"],
                    "table_problems": table_problems,
                    "blocked": bool(blocked),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if blocked else 0

    print(
        f"依赖漏洞扫描：{len(payload.get('dependencies') or [])} 个包，"
        f"{len(findings)} 条去重后的告警（在册豁免 {len(verdict['exempt'])} 条）"
    )
    for row in verdict["red"]:
        print(f"  ❌ {row['package']} {row['version']} {row['id']} —— {row['why']}")
        if row["aliases"]:
            print(f"      别名：{', '.join(row['aliases'])}")
    for row in verdict["stale"]:
        print(f"  ⚠️  豁免 {row['id']} 这次没扫出来（包升级了？该复查这张表是不是还该带着它）")
    for problem in table_problems:
        print(f"  ❌ 豁免表：{problem}")
    if blocked:
        print("结论：红。新告警必须被人看见一次；在册豁免的前提（上游没得升）没了就该销。")
        return 1
    print("结论：绿。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
