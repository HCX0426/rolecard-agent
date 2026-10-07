"""分模块地板：每个源文件不许低于 pyproject 定的下限（覆盖率体系的后半）。

全局 ``fail_under`` 管的是**平均**，平均高不等于没有文件在烂：全局 90 时一个 60 分的
文件只要其余都够高就照绿 —— 而"低覆盖文件恰好全是失败路径"正是那批补了七轮的现场。
地板管的是**单文件不许掉到现状之下**：下限取当前全仓最低值（量出来的，不是拍的），
此后新文件、新分支都得自己过线。

执行位置在门禁覆盖率那一步**之后**（数据是它刚写的 ``.coverage``）。几个纪律：

* 下限住在 pyproject 的 ``[tool.rolecard] coverage_floor``（唯一出处）。**刻意不放**
  ``[tool.coverage.report]``：coverage.py 对自家表里不认识的键每个用例都报
  CoverageWarning（实测确认），等于给每次测试挂一条噪音；
* **配置缺失 = 机制空转，判红** —— 删掉那一行是这一族最安静的死法（"搬一半"的老病）；
* 数据文件不存在时**大声跳过**（exit 0 但打出 NO-DATA 标记）：没跑过覆盖率不是缺陷
  （fresh clone / ``--only`` 单跑本步），但必须说出来，不能静默；
* 只看 ``num_statements > 0`` 的文件：空 ``__init__.py`` 报 100%，不构成地板问题。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tomllib

#: pyproject 里下限的出处（表名 + 键名，与其它工具的表互不干扰）。
CONFIG_TABLE = "tool"
CONFIG_SECTION = "rolecard"
CONFIG_KEY = "coverage_floor"


def load_floor(pyproject_text: str) -> float | None:
    """从 pyproject 文本取下限；没有这一格返回 None（调用方按"配置缺失"判红）。"""
    cfg = tomllib.loads(pyproject_text)
    section = cfg.get(CONFIG_TABLE, {}).get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return None
    value = section.get(CONFIG_KEY)
    return float(value) if isinstance(value, (int, float)) else None


def evaluate(
    entries: list[tuple[str, int, float]], floor: float
) -> list[str]:
    """逐文件比对，返回违规描述（空表 = 全过）。

    ``entries`` = (文件, 语句数, 精确百分比)。0 语句文件不参与（它们恒报 100，
    即便数据里出现 0.0 也不是地板要抓的对象）。
    """
    return [
        f"{name}  {pct:.2f}% < {floor:g}%（{stmts} 语句）"
        for name, stmts, pct in entries
        if stmts > 0 and pct < floor
    ]


def _report_entries(root: pathlib.Path) -> list[tuple[str, int, float]] | None:
    """把 ``.coverage`` 数据变成 entries；数据不存在返回 None（调用方大声跳过）。"""
    data_file = root / ".coverage"
    if not data_file.exists():
        return None
    out_json = root / "build" / "coverage_floor_report.json"
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
    report = json.loads(out_json.read_text(encoding="utf-8"))
    return [
        (
            name,
            int(info["summary"]["num_statements"]),
            float(info["summary"]["percent_covered"]),
        )
        for name, info in report["files"].items()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分模块覆盖率地板")
    parser.add_argument("--root", default=None, help="仓库根（默认按本文件位置推）")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root) if args.root else pathlib.Path(__file__).resolve().parents[1]

    pyproject = root / "pyproject.toml"
    floor = (
        load_floor(pyproject.read_text(encoding="utf-8"))
        if pyproject.exists()
        else None
    )
    if floor is None:
        print(
            "FAIL coverage floor :: pyproject 里没有 "
            f"[{CONFIG_TABLE}.{CONFIG_SECTION}] {CONFIG_KEY} —— 配置缺失 = 地板空转（判红）",
            flush=True,
        )
        return 1

    try:
        entries = _report_entries(root)
    except RuntimeError as exc:
        print(f"FAIL coverage floor :: {exc}", flush=True)
        return 1
    if entries is None:
        print(
            "NO-DATA coverage floor :: 没有 .coverage 数据（覆盖率那步没跑过）"
            " —— 跳过，不代表通过",
            flush=True,
        )
        return 0

    violations = evaluate(entries, floor)
    if violations:
        print(
            f"FAIL coverage floor :: {len(violations)} 个文件低于地板 {floor:g}%：",
            flush=True,
        )
        for line in sorted(violations):
            print(f"  {line}", flush=True)
        print(
            "  修法：给这些文件补失败路径用例（别动地板 —— 地板是量出来的现状下限）",
            flush=True,
        )
        return 1
    measured = sum(1 for _, stmts, _ in entries if stmts > 0)
    print(
        f"OK coverage floor :: {measured} 个有语句的文件全部 ≥ {floor:g}%",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
