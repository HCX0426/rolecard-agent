"""JSON 驱动的账本编辑器（规格文件走 argv[1]）。

用法：python scripts/tools/ledger_apply.py <spec.json> [--probe]

规格文件形状：{"edits": [{"id", "old", "new", "must_contain": [...]}]}
纪律：锚点 count==1 才写；new 不许含换行（表格行不许劈开）；must_contain 逐条到位；
写完独立结构自检（ENGI 表行数 / 编号连续 / 每行分割计数 5），不过就整体还原。

为什么从 build/ 挪进来：它此前躺在 gitignore 的临时目录里，一次"删临时文档"把它连带删掉
而 git 救不回来（未入库）。入库后与其它 scripts 同受管理，并被 `console encoding` 那条尺子
看住 —— 本文件会 print 中文（✓/✗/✅），Windows GBK 控制台装不下，故先重配 stdout/stderr
（`docs/开发流程.md` 的编码纪律，与仓库其它入口脚本同一形状）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# 必须在任何 print 之前：默认编码可能是 GBK，而下面每条输出都含中文。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

LED = Path("docs/代码审查快照（2026-10-04）.md")
spec_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if spec_path is None or not spec_path.exists():
    print("用法：ledger_apply.py <spec.json> [--probe]")
    sys.exit(2)
SPEC = json.loads(spec_path.read_text(encoding="utf-8"))["edits"]

before = LED.read_text(encoding="utf-8")
text = before

if "--probe" in sys.argv:
    missed = 0
    for e in SPEC:
        n = text.count(e["old"])
        missed += 0 if n == 1 else 1
        print(f"count={n} {'✓' if n == 1 else '✗'}  [{e['id']}] {e['old'][:64]}")
        if n != 1:
            stem = e["old"][:16]
            near = [ln for ln in text.splitlines() if stem in ln]
            for ln in near[:1]:
                idx = ln.find(stem)
                print("      实际附近:", repr(ln[max(0, idx - 8):idx + 96]))
    print("锚点全对，可以写盘" if not missed else f"有 {missed} 个锚不合格")
    sys.exit(1 if missed else 0)

for e in SPEC:
    n = text.count(e["old"])
    if n != 1:
        print(f"[{e['id']}] 锚点 {n} 次，整笔不写盘")
        sys.exit(1)
    if "\n" in e["new"]:
        print(f"[{e['id']}] 新文本含换行，会劈开表格行，不写盘")
        sys.exit(1)
    text = text.replace(e["old"], e["new"])
    for token in e["must_contain"]:
        if token not in text:
            print(f"[{e['id']}] 写完仍缺关键句 {token!r}，不写盘")
            sys.exit(1)

LED.write_text(text, encoding="utf-8", newline="\n")

lines = LED.read_text(encoding="utf-8").splitlines()
rows = [ln for ln in lines if ln.startswith("| ENGI-")]
nums: list[int] = []
for ln in rows:
    first = re.split(r"(?<!\\)\|", ln.strip("|"))[0].strip()
    d = "".join(ch for ch in first.split("（")[0] if ch.isdigit())
    if d:
        nums.append(int(d))
widths = {len(re.split(r"(?<!\\)\|", ln)) for ln in rows}
bad: list[str] = []
if nums != list(range(15, 15 + len(nums))):
    bad.append(f"编号不连续：{nums}")
if widths != {5}:
    bad.append(f"格数不齐（新文本里混进了裸竖线）：{widths}")
if len(rows) != 18:
    bad.append(f"表行数 {len(rows)}（应 18）")
if bad:
    LED.write_text(before, encoding="utf-8", newline="\n")
    print("自检不过，已还原：", bad)
    sys.exit(1)
print(f"✅ 落账完成：{len(rows)} 行（{nums[0]}→{nums[-1]}）、每行分割计数 5、"
      f"文件 {len(lines)} 行（原 {len(before.splitlines())}）")
