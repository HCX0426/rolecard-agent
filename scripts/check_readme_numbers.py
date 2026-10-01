"""收尾那一问：README 首屏那几个数，对得上**刚刚**落盘的读数吗？

为什么门禁里要单独有这一步（10-01，台账 `R28-73`）：比对本身写在 `check_consistency.py` 里，
而 `consistency` 那一步排在 `pytest(覆盖率)` 与 `前端 vitest` **之前** —— 于是那两个数在这趟里
刚被更新，比对却已经跑完了，用的还是上一趟的值（实测：覆盖率 91.89 → 91.92，README 当场"晚一趟"）。
把同一个判据在末尾再问一次，"同一趟看见"这句话才对**所有**四个键成立。

判据只有一份实现：这里不重抄规则，直接加载 `check_consistency` 并只调那一个函数 ——
所以它红了，就是 `consistency` 那一步也会红的那个原因，不会有第二套口径。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_spec = importlib.util.spec_from_file_location(
    "consistency_checks", str(ROOT / "scripts" / "check_consistency.py")
)
assert _spec and _spec.loader
checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checks)

checks.check_readme_headline_numbers()

fails = list(checks.fails)
warns = list(checks.warns)
if warns:
    print("提示（不进红）：")
    for item in warns:
        print("  " + item)
if fails:
    print("RESULT: README 首屏那组数与刚写入的读数对不上（把 README 改成读数里那个数，"
          "或跑一趟对应的档位让它重新量）")
    raise SystemExit(1)
print("RESULT: PASS（README 与门禁读数同趟一致）")
raise SystemExit(0)
