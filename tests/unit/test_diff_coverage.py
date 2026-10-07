"""改动行覆盖率（diff-cover）的判据。

三条腿与分模块地板同族：配置读得出、diff 解析对得上、**缺配置判红**。
解析那条用的是真 `git diff --unified=0` 文本形状（含纯删除与新文件两种 hunk）。
"""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

DIFF_SAMPLE = """\
diff --git a/src/x/old.py b/src/x/old.py
--- a/src/x/old.py
+++ b/src/x/old.py
@@ -10,0 +11,3 @@
+新行11
+新行12
+新行13
@@ -40,3 +42,4 @@
 上下文不算改动
+新行44
 context
 context
@@ -60,2 +60,0 @@
-只删不增行A
-只删不增行B
diff --git a/src/x/new.py b/src/x/new.py
--- /dev/null
+++ b/src/x/new.py
@@ -0,0 +1,2 @@
+全新文件行1
+全新文件行2
"""


def _load():
    spec = importlib.util.spec_from_file_location(
        "diff_coverage_under_test", str(ROOT / "scripts" / "diff_coverage.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_takes_new_side_lines_only() -> None:
    """改动行 = 新侧行号；纯删除的 hunk 没有新侧行，不产生任何可测行。"""
    mod = _load()
    changed = mod.parse_changed_lines(DIFF_SAMPLE)
    # 第二个 hunk 的头是 `+42,4` —— --unified=0 下新侧四行全是改动行（42-45）
    assert changed["src/x/old.py"] == {11, 12, 13, 42, 43, 44, 45}, changed
    assert changed["src/x/new.py"] == {1, 2}, changed


def test_summarize_splits_executable_lines_against_report() -> None:
    """行号对齐覆盖报告：executed=覆盖、missing=未覆盖、两边都没有=非可执行（不进分母）。

    old.py 的改动行 44 故意两边都不在（注释级），它必须被排除 —— 否则一条注释就能
    把比例搅成假绿。
    """
    mod = _load()
    report = {
        "files": {
            "src\\x\\old.py": {"executed_lines": [11], "missing_lines": [12, 13]},
            "src\\x\\new.py": {"executed_lines": [1], "missing_lines": [2]},
        }
    }
    covered, uncovered, violations = mod.summarize(
        mod.parse_changed_lines(DIFF_SAMPLE), report
    )
    assert (covered, uncovered) == (2, 3), (covered, uncovered)
    assert violations["src/x/old.py"] == [12, 13]
    assert violations["src/x/new.py"] == [2]


def test_floor_config_is_read_and_absence_is_none() -> None:
    mod = _load()
    assert mod.load_floor("[tool.rolecard]\ndiff_coverage_floor = 95\n") == 95
    assert mod.load_floor("[tool.rolecard]\ncoverage_floor = 85\n") is None
    assert mod.load_floor("[tool.mypy]\n") is None


def test_missing_floor_config_fails_loudly(tmp_path, capsys) -> None:
    """**删一处 → 红**：阈值键被删时脚本必须红，而不是"没有阈值就当全过"。"""
    mod = _load()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.rolecard]\ncoverage_floor = 85\n", encoding="utf-8"
    )
    code = mod.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1 and "diff_coverage_floor" in out and "空转" in out, (code, out)


def test_no_origin_ref_or_no_data_screams_and_passes(tmp_path, capsys) -> None:
    """没有 origin/main（没配远端的克隆）⇒ NO-DATA 大声跳过：不知道 ≠ 通过，也不该判红。"""
    mod = _load()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.rolecard]\ndiff_coverage_floor = 95\n", encoding="utf-8"
    )
    # 非 git 目录：rev-parse 失败走 NO-DATA 分支
    code = mod.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "NO-DATA" in out and "不代表通过" in out, (code, out)
