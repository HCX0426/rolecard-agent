"""`coverage json` 的唯一调用实现（2026-10-30 壳与脚本审计 #9：coverage_floor 与
diff_coverage 逐字各写一份，全量档前后脚对同一份 ``build/.coverage`` 各跑一遍，
产出两份几乎同内容的报告 JSON——同一判据两处实现 + 一次白付的秒级调用）。

共用方式：两边都 ``from coverage_json import run_coverage_json``（脚本以
``python scripts/xxx.py`` 直跑，sys.path[0] 就是 scripts/）。各自的输出文件名不变
（coverage_floor_report.json / diff_coverage_report.json），下游读数不受影响。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def run_coverage_json(root: Path, out_json: Path) -> dict[str, Any]:
    """跑一次 ``coverage json`` 并返回解析后的报告；数据文件不存在抛 FileNotFoundError。

    失败（退出码非 0）原样带 stderr 尾巴抛 RuntimeError —— 与原先两份内联实现同一口径，
    只是实现只剩这一份。
    """
    data_file = root / "build" / ".coverage"
    if not data_file.exists():
        raise FileNotFoundError(str(data_file))
    out_json.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603
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
    return json.loads(out_json.read_text(encoding="utf-8"))
