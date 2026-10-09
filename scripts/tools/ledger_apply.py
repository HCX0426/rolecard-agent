"""JSON 驱动的账本编辑器（规格文件走 argv[1]）。

用法：python scripts/tools/ledger_apply.py <spec.json> [--probe]

规格文件形状：{"edits": [{"id", "old", "new", "must_contain": [...]}]}
纪律：锚点 count==1 才写；new 含换行须显式 multi_line（默认禁止，防把表格行劈成两半）；
must_contain 逐条到位；写完独立结构自检（ENGI 表行数只增不减 / 编号连续 / 各行格数一致），
不过就整体还原。**锚点必须包含目标行的结尾**（锚在行中间会把新行内容塞进行内、挤成多格 ——
2026-10-10 实测踩过，自检"格数不齐"当场拦下）。

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
    # 含换行的新文本只在**显式声明**时才允许（`multi_line: true`）：它用来整块插入新行
    # （如新增一条 ENGI 记录），而默认禁止是防"顺手把表格行劈成两半"那个老坑。
    if "\n" in e["new"] and not e.get("multi_line"):
        print(f"[{e['id']}] 新文本含换行，会劈开表格行；确实要插入整块就写 multi_line: true")
        sys.exit(1)
    if e.get("multi_line") and e["new"].strip("\n").splitlines() and any(
        ln.strip() and not ln.lstrip().startswith(("|", "#", ">", "-", "*")) and "：" not in ln
        for ln in e["new"].splitlines()
    ):
        # 多行块的每一行要么是表格行/标题/引用，要么是纯散文段落 —— 不允许出现"半截表格行"
        # （以 `|` 开头却不以 `|` 结尾）那种最容易劈坏文件的形状。
        half = [
            ln for ln in e["new"].splitlines()
            if ln.lstrip().startswith("|") and not ln.rstrip().endswith("|")
        ]
        if half:
            print(f"[{e['id']}] 多行块里有半截表格行（以 | 开头却不以 | 结尾），不写盘")
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
widths = {len(re.split(r"(?<!\\)\|", ln.strip("|"))) for ln in rows}
bad: list[str] = []
# 编号必须连续（从第一个 ENGI 行起）：这条是"编号是身份"的机器化那一半。
if nums and nums != list(range(nums[0], nums[0] + len(nums))):
    bad.append(f"编号不连续：{nums}")
# 格数必须**全体一致**（不写死具体几格：表格形状本身可能演进）。基准取改动前的第一条 ENGI 行。
if len(widths) != 1:
    bad.append(f"格数不齐（新文本里混进了裸竖线）：{widths}")
# 行数只许**增加**（落账是追加，不是改写历史）。
was = len([ln for ln in before.splitlines() if ln.startswith("| ENGI-")])
if len(rows) < was:
    bad.append(f"ENGI 表行数从 {was} 减到 {len(rows)}（落账只该追加）")
if bad:
    LED.write_text(before, encoding="utf-8", newline="\n")
    print("自检不过，已还原：", bad)
    sys.exit(1)
print(f"✅ 落账完成：{was} → {len(rows)} 行（编号 {nums[0]}→{nums[-1]}）、"
      f"每行 {widths.pop()} 格、文件 {len(lines)} 行（原 {len(before.splitlines())}）")
