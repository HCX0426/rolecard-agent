"""改动行覆盖率（diff-cover）：没被测试走到的改动行不许越过 push 线。

全局 fail_under 管平均、分模块地板管单文件 —— 它俩都看不见**这一批改动本身**：
把新逻辑写在一个覆盖 100% 的文件里，新增的分支一条没测，前两把尺子照样绿。
这条只问一件事：`origin/main` 以来动过的 src 行，有多少被执行过。

为什么基线是 origin/main（2026-10-07 落地时未推送栈已有 233 个提交）：
diff-cover 的语义本来就是"这堆要进 main 的改动，被验过没有" —— push 那一刻它是
唯一还算数的口径。基线随 push 前移，规则不用改。

几个纪律（与分模块地板同一族）：
* 阈值住 pyproject 的 ``[tool.rolecard] diff_coverage_floor`` —— 值是**量出来的**
  （落地当天 95.14%：2840 个可测改动行、138 行零星欠账散在十来个文件），不是拍的；
* **配置缺失判红**（搬一半 = 机制静默消失）；
* 没有 origin/main / 改动全在非 src / 覆盖率数据不在 ⇒ NO-DATA 大声跳过（退出 0
  但打印"不代表通过"）；
* 只统计落在 coverage 报告 ``executed_lines``/``missing_lines`` 里的行 —— 注释、空行、
  纯 def 装饰行不是可测行，不进分母（否则一条注释就能把比例搅乱）；
* 纯删除的 hunk 没有新侧行，不算（删代码不需要被测，删对了由别把尺子管）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tomllib

CONFIG_TABLE = "tool"
CONFIG_SECTION = "rolecard"
CONFIG_KEY = "diff_coverage_floor"
#: 改动行的基线：未推送栈的起点（push 后自动前移）。
BASE_REF = "origin/main"


def load_floor(pyproject_text: str) -> float | None:
    """从 pyproject 文本取阈值；没有这一格返回 None（调用方按"配置缺失"判红）。"""
    cfg = tomllib.loads(pyproject_text)
    section = cfg.get(CONFIG_TABLE, {}).get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return None
    value = section.get(CONFIG_KEY)
    return float(value) if isinstance(value, (int, float)) else None


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_changed_lines(diff_text: str) -> dict[str, set[int]]:
    """从 `git diff --unified=0` 文本提取每个文件的**新侧行号**（改动行）。

    纯删除（新侧计数 0）没有可测行；旧侧（`--- a/`）不算 —— 要测的是现在的代码。
    """
    out: dict[str, set[int]] = {}
    cur: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            cur = line[len("+++ b/") :]
            out.setdefault(cur, set())
            continue
        if line.startswith("--- a/"):
            cur = None
            continue
        hit = _HUNK.match(line)
        if hit and cur is not None:
            start = int(hit.group(1))
            count = int(hit.group(2) or "1")
            for n in range(start, start + count):
                out[cur].add(n)
    return out


def summarize(
    changed: dict[str, set[int]], report: dict
) -> tuple[int, int, dict[str, list[int]]]:
    """对齐覆盖数据，返回 (覆盖数, 未覆盖数, {文件: 未覆盖行号}).

    行号要与覆盖数据同一棵树 —— 覆盖率那步刚量完的 `.coverage` 就是当前树，天然对齐。
    """
    covered = 0
    uncovered = 0
    violations: dict[str, list[int]] = {}
    for fname, lines in changed.items():
        key = fname.replace("/", "\\")
        info = report["files"].get(key)
        if info is None:
            if not fname.endswith(".py"):
                # src/ 下的非 Python 资产（schema.sql 之类）不进 coverage 报告，是**例行**
                # 的（实测：改动清单里就有 schema.sql）—— 它们的"被测"由用例语义管，
                # 行号对不上 coverage 的执行清单，不进分母。
                continue
            # .py 文件却不在报告里 = 覆盖率数据与当前树错位（新加的文件没赶上那一趟）。
            # 无从判断哪些行可测 ⇒ **把改动行全记未覆盖（fail-closed）**：错位的绿比红贵，
            # 逼人重量一趟覆盖率，而不是把这批改动静默放过。
            uncovered += len(lines)
            violations[fname] = sorted(lines)
            continue
        executed = set(info.get("executed_lines") or [])
        missing = set(info.get("missing_lines") or [])
        hit = [n for n in lines if n in executed or n in missing]
        covered += sum(1 for n in hit if n in executed)
        miss = sorted(n for n in hit if n in missing)
        uncovered += len(miss)
        if miss:
            violations[fname] = miss
    return covered, uncovered, violations


def _git(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    # `-c core.quotepath=false`：`git diff` 的 `+++ b/<路径>` 头在 quotepath=true（Linux 默认）
    # 下对非 ASCII 路径**整体加引号+八进制转义** ⇒ `parse_changed_lines` 认不出那条头 ⇒
    # 这个文件的改动行**整个从分母里消失** ⇒ 改动行覆盖率的地板在它身上静默失效（ENGI-31 同根因
    # 的第 6 处，2026-10-09 晚盘全仓 23 个 git 调用点时盘出来的 —— 盘的时候本仓 src 下恰好没有
    # 非 ASCII 文件，所以今天是**潜伏**不是已发作：判据在这里是"改一行就少测一行"的静默方向）。
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _has_coverage_data(root: pathlib.Path) -> pathlib.Path | None:
    """跑一次 `coverage json` 出报告文件；数据不存在返回 None（大声跳过）。"""
    if not (root / "build" / ".coverage").exists():
        return None
    out_json = root / "build" / "diff_coverage_report.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(out_json)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"coverage json 失败（exit {proc.returncode}）：{(proc.stderr or '').strip()[-400:]}"
        )
    return out_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="改动行覆盖率（diff-cover）")
    parser.add_argument("--root", default=None, help="仓库根（默认按本文件位置推）")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root) if args.root else pathlib.Path(__file__).resolve().parents[1]

    pyproject = root / "pyproject.toml"
    floor = (
        load_floor(pyproject.read_text(encoding="utf-8")) if pyproject.exists() else None
    )
    if floor is None:
        print(
            "FAIL diff coverage :: pyproject 里没有 "
            f"[{CONFIG_TABLE}.{CONFIG_SECTION}] {CONFIG_KEY} —— 配置缺失 = 判据空转（判红）",
            flush=True,
        )
        return 1

    if _git(root, "rev-parse", "--verify", "--quiet", BASE_REF).returncode != 0:
        print(
            f"NO-DATA diff coverage :: 找不到 {BASE_REF}（没配远端的克隆）"
            " —— 跳过，不代表通过",
            flush=True,
        )
        return 0
    proc = _git(root, "diff", "--unified=0", "--no-color", BASE_REF, "--", "src/")
    if proc.returncode != 0:
        print(f"FAIL diff coverage :: git diff 失败：{(proc.stderr or '').strip()[:300]}")
        return 1
    changed = parse_changed_lines(proc.stdout)
    if not changed:
        print(
            f"NO-DATA diff coverage :: 相对 {BASE_REF} 没有 src 改动 —— 跳过，不代表通过",
            flush=True,
        )
        return 0

    try:
        report_path = _has_coverage_data(root)
    except RuntimeError as exc:
        print(f"FAIL diff coverage :: {exc}", flush=True)
        return 1
    if report_path is None:
        print(
            "NO-DATA diff coverage :: 没有 .coverage 数据（覆盖率那步没跑过）"
            " —— 跳过，不代表通过",
            flush=True,
        )
        return 0
    report = json.loads(report_path.read_text(encoding="utf-8"))

    covered, uncovered, violations = summarize(changed, report)
    total = covered + uncovered
    if total == 0:
        print(
            f"NO-DATA diff coverage :: {len(changed)} 个改动文件里没有可测行"
            " —— 跳过，不代表通过",
            flush=True,
        )
        return 0
    pct = 100.0 * covered / total
    if pct < floor:
        print(
            f"FAIL diff coverage :: {pct:.2f}% < {floor:g}%"
            f"（{BASE_REF} 以来 {total} 个可测改动行，未覆盖 {uncovered}）：",
            flush=True,
        )
        for fname, miss in sorted(violations.items(), key=lambda kv: -len(kv[1])):
            shown = ", ".join(str(n) for n in miss[:8])
            more = f" …+{len(miss) - 8}" if len(miss) > 8 else ""
            print(f"  {fname}  行 {shown}{more}", flush=True)
        print(
            "  修法：给这些改动补用例（别动阈值 —— 阈值是量出来的现状下限）",
            flush=True,
        )
        return 1
    print(
        f"OK diff coverage :: {pct:.2f}% ≥ {floor:g}%"
        f"（相对 {BASE_REF}：{total} 个可测改动行，未覆盖 {uncovered}）",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
