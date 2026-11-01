"""重算「台账删除那一族」的悬空编号名单 → `config/citation-prose-only.json`。

为什么要有这个文件：`audit citations` 那把尺子判"悬空"时，得有一份**可枚举**的豁免名单，
否则就得把 207 个号硬写进代码里（每次改注释都要手改两处，迟早漂开）。这份文件是
生成物：跑一次就知道当前还有哪些号悬空，改完注释重跑一次名单就短一截。

**它是"欠条"不是"通行证"**：名单里的号每个都还欠着一次"改写成那一号说的那件事"。
改完一处 → 重跑这个脚本 → 名单短一截 → 尺子的豁免面同步收窄。

跑法（在仓库根）：
    .venv\\Scripts\\python.exe scripts/tools/build_citation_prose_only.py
    .venv\\Scripts\\python.exe scripts/tools/build_citation_prose_only.py --check
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from consistency import checks_audit as ruler  # noqa: E402

OUT = ROOT / "config" / "citation-prose-only.json"


def dangling_numbers() -> list[str]:
    """当前所有"代码里引用、却无家可归"的编号（豁免名单自身不算）。"""
    index = ruler._audit_index()
    _numbers, homes, _per_doc = index.scanned_citations()
    found: set[str] = set()
    for path in ruler.iter_files(".py", ".ts", ".tsx", ".js"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            for match in ruler._SECTION_RE.finditer(line):
                if not homes.get(match.group(1)):
                    found.add(match.group(1))
            for num in ruler._PID_RE.findall(line):
                if num in ruler._CITATION_PROSE_ONLY:
                    continue
                if not homes.get(num):
                    found.add(num)
    return sorted(found)


def render() -> str:
    numbers = dangling_numbers()
    payload = {
        "这是什么": (
            "`audit citations` 那把尺子的**悬空豁免名单**（生成物，勿手改）。"
            "名单里的编号：代码注释仍在引用，但它的定义行随 2026-11-01 删档消失了 —— "
            "标的物被删，不是引用写错。"
        ),
        "怎么用": ("尺子读它豁免「号无家」那一半；「归因可疑」那半照查，新出现的悬空照红。"),
        "欠什么": (
            "每个号都欠一次改写：代码注释里的裸编号改写成**那一号说的那件事**"
            "（仓库纪律：不写裸编号）。改完一处重跑本脚本，名单就短一截。"
        ),
        "重跑": (".venv\\\\Scripts\\\\python.exe scripts/tools/build_citation_prose_only.py"),
        "numbers": numbers,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    body = render()
    if "--check" in args:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != body:
            print(
                "citation-prose-only.json 过期了：重跑 scripts/tools/build_citation_prose_only.py"
            )
            return 1
        print("citation-prose-only.json 与现状一致")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(body, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT).as_posix()}（{len(json.loads(body)['numbers'])} 个号）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
