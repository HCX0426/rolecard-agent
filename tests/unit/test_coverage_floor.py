"""分模块地板的判据（覆盖率体系第八刀）。

三条腿缺一不可：配置读得出、违规点得出名、**缺配置判红而不是空转**。
最后这条是这一族的老病（"搬一半 = 机制静默消失"），所以单列一条用例。
"""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "coverage_floor_under_test", str(ROOT / "scripts" / "coverage_floor.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_below_floor_is_named_with_its_exact_percent() -> None:
    mod = _load()
    entries = [
        ("src/x/good.py", 100, 93.74),
        ("src/x/weak.py", 75, 84.95),
        ("src/x/ok.py", 10, 85.0),  # 恰在线上：地板是"≥"，不是">"
    ]
    violations = mod.evaluate(entries, 85)
    assert len(violations) == 1, violations
    assert "weak.py" in violations[0] and "84.95" in violations[0]


def test_zero_statement_files_are_not_floor_violations() -> None:
    """空 `__init__.py` 不参与地板：它们恒报 100，数据里偶见 0.0 也不是地板要抓的对象。"""
    mod = _load()
    assert mod.evaluate([("src/pkg/__init__.py", 0, 0.0)], 85) == []


def test_floor_config_is_read_from_pyproject_text() -> None:
    mod = _load()
    assert mod.load_floor('[tool.rolecard]\ncoverage_floor = 85\n') == 85
    # 缺这一格 = None（调用方判红）；别处的表不算数
    assert mod.load_floor('[tool.mypy]\npackages = ["x"]\n') is None
    assert mod.load_floor('[tool.rolecard]\nother = 1\n') is None


def test_missing_floor_config_fails_loudly_instead_of_passing(tmp_path, capsys) -> None:
    """**删一处 → 红**：配置缺失时脚本必须红，而不是"没有地板就当全过"。"""
    mod = _load()
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n", encoding="utf-8")
    code = mod.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1 and "coverage_floor" in out and "空转" in out, (code, out)


def test_no_coverage_data_screams_and_passes(tmp_path, capsys) -> None:
    """没跑过覆盖率 ⇒ 大声跳过（exit 0 但打出 NO-DATA）：不知道 ≠ 通过，也不该判红。"""
    mod = _load()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.rolecard]\ncoverage_floor = 85\n", encoding="utf-8"
    )
    code = mod.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "NO-DATA" in out and "不代表通过" in out, (code, out)


def test_main_reports_every_file_below_the_floor(tmp_path, monkeypatch, capsys) -> None:
    """数据在场时按地板逐文件点名（真数据路径由两臂变异在真实 `.coverage` 上再验一次）。"""
    mod = _load()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.rolecard]\ncoverage_floor = 85\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        mod,
        "_report_entries",
        lambda _root: [("src/a/weak.py", 40, 76.5), ("src/a/fine.py", 40, 91.0)],
    )
    code = mod.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1 and "weak.py" in out and "76.5" in out, (code, out)
    assert "fine.py" not in out, out
