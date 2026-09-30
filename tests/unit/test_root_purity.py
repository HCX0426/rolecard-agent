"""`scripts/check_root_purity.py` 的判据用例。

钉的是 09-30 那次实测的形状（`R28-49`）：数据根从仓库切到安装目录之后，旧根里的测试痕迹
跟着升级成了生产数据 —— 5 行 `ingestion_task`（2 行占位哈希 + 3 行跨根）与 8 条夹具向量。
用例里那些字面值**就是从真库里读出来的**，不是编的。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_SPEC = importlib.util.spec_from_file_location(
    "check_root_purity",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "check_root_purity.py",
)
assert _SPEC and _SPEC.loader
purity = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = purity  # dataclass 要能按 __module__ 找回这个模块，否则 collection 就炸
_SPEC.loader.exec_module(purity)

ROOT = pathlib.Path("C:/Users/hcx/AppData/Local/rolecard-agent")


def test_placeholder_hashes_are_seed_leftovers() -> None:
    rows = [("ing_b3a8db624471", "u1", "", "deadbeef"), ("ing_a80f3904e398", "u1", "", "cafe")]
    rep = purity.classify_ingestion(rows, ROOT)
    assert len(rep.red) == 2
    assert all("占位值" in line for line in rep.red)


def test_cross_root_ledger_row_is_red_even_when_the_name_is_ordinary(
    tmp_path: pathlib.Path,
) -> None:
    """仓库旧根里躺着的那三行：路径是绝对路径、文件**在**，但不在这根下。"""
    old_root = tmp_path / "repo-data" / "uploads"
    old_root.mkdir(parents=True)
    (old_root / "47ba2914_report.txt").write_text("血常规报告", encoding="utf-8")
    rows = [
        ("ing_5a3563c26c57", "local-user", str(old_root / "47ba2914_report.txt"), "f2" + "0" * 62)
    ]
    rep = purity.classify_ingestion(rows, ROOT)
    assert any("另一个数据根" in line or "测试夹具" in line for line in rep.red), rep.red


def test_ledger_row_naming_an_absent_original_is_red() -> None:
    gone = ROOT / "uploads" / "eac53824_冒烟报告.txt"
    rows = [("ing_gone", "local-user", str(gone), "5d" + "0" * 62)]
    rep = purity.classify_ingestion(rows, ROOT)
    assert len(rep.red) == 1  # 名字本身也是已知夹具；只报一次，不重复刷
    assert "冒烟报告.txt" in rep.red[0]


def test_a_row_whose_original_lives_in_this_root_passes(tmp_path: pathlib.Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    real = uploads / "7cd0e2a1_我的报告.txt"
    real.write_text("真实内容", encoding="utf-8")
    rows = [("ing_real", "local-user", str(real), "9a" + "1" * 62)]
    rep = purity.classify_ingestion(rows, tmp_path)
    assert rep.red == []
    assert rep.warn == []


def test_chroma_fixtures_are_flagged_by_source_name() -> None:
    sources = ["report.txt", "verify.png", "复查图片.png", "审计.pdf", "01-基本设定与人设.md"]
    rep = purity.classify_chroma(sources, "health_reports")
    flagged = [line for line in rep.red]
    assert len(flagged) == 4, flagged
    assert not any("基本设定" in line for line in flagged)


def test_fixture_wording_in_user_text_warns_but_does_not_block() -> None:
    """启发式那一半只 warn —— 与门禁"未知不拦"同一条纪律（这些串也可能是用户自己写的）。"""

    class _FakeConn:
        def execute(self, _sql: str) -> _FakeConn:
            self._rows = [("空腹血糖 6.1 mmol/L",), ("自己的真话",)]
            return self

        def fetchall(self) -> list[tuple[str]]:
            return self._rows

    rep = purity.classify_text_columns(_FakeConn(), [("role_memory_item", "text", "长期记忆")])  # type: ignore[arg-type]
    assert rep.red == []
    assert len(rep.warn) == 1
    assert "长期记忆" in rep.warn[0]
