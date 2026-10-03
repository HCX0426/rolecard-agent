"""取证包装层收成两档之后的判据（`R102-41`，10-03 批 25）。

这一层是"红跑不许没有现场"的那道守卫，所以两臂都要有牙：
命中在册签名的 chroma 偶发 ⇒ 重跑一次取证（**为取证不为转绿**）；
混进任何一条不在册的失败 ⇒ 立刻按原样红，一次都不许多跑。
后者是这条判据不能变成万能遮羞布的唯一保证（`R102-38` 那一族的教训是"看起来绿"比红贵）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(monkeypatch, tmp_path: pathlib.Path):
    for p in (str(ROOT), str(ROOT / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "pytest_with_evidence_under_test", str(ROOT / "scripts" / "pytest_with_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BUILD", tmp_path)
    return mod


FLAKE_LOG = (
    "FAILED tests/unit/test_rag.py::test_scope_isolation - "
    "chromadb.errors.InternalError: Error creating hnsw segment reader: Nothing found on disk\n"
    "1 failed, 1034 passed in 170.54s\n"
)
REAL_LOG = "FAILED tests/unit/test_x.py::y - AssertionError: 真的红了\n1 failed\n"


def test_the_two_lanes_differ_only_in_cov_and_stop_on_first_failure(monkeypatch, tmp_path) -> None:
    mod = _load(monkeypatch, tmp_path)
    fast, f1, f2 = mod.LANES["fast"]
    cov, c1, c2 = mod.LANES["coverage"]
    assert "-x" in fast and "--cov=rolecard_agent" not in fast, "快档该是裸跑 -x，不该量覆盖率"
    assert "--cov-fail-under=85" in cov and "-x" not in cov, "覆盖率档该量到底、不该一红就停"
    assert len({f1, f2, c1, c2}) == 4, f"两档日志撞名了：{f1} {f2} {c1} {c2}"


def test_unknown_lane_is_a_loud_failure_not_a_default(monkeypatch, tmp_path, capsys) -> None:
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "不存在的档"])
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_run", lambda cmd, extra=None: calls.append(cmd) or (0, ""))
    assert mod.main() == 2
    assert calls == [], f"档位都不认识还跑了 pytest：{calls}"
    assert "不存在的档" in capsys.readouterr().err


def test_a_non_signature_failure_is_never_retried(monkeypatch, tmp_path, capsys) -> None:
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        return 1, REAL_LOG if len(calls) == 1 else ""

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    assert len(calls) == 1, "不在册的失败被重跑了 —— 这层就变成遮羞布了"
    assert "不重跑" in capsys.readouterr().out


def test_flake_retries_for_evidence_and_keeps_both_logs(monkeypatch, tmp_path, capsys) -> None:
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        if len(calls) == 1:
            return 1, FLAKE_LOG
        assert "-x" not in cmd, "取证那一跑不该带着色 `-x`"
        assert "tests/unit/test_rag.py" in cmd, f"二跑该只跑红的那个文件：{cmd}"
        return 0, "1 passed\n"

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "FLAKY-RECORDED" in out and "机制仍未定位" in out, "放行时不许说成「已修」"
    assert (tmp_path / "gate-fast-run1.log").exists(), "首跑日志必须原样留着"
    assert (tmp_path / "gate-fast-run2-retry.log").exists()
    assert FLAKE_LOG in (tmp_path / "gate-fast-run1.log").read_text(encoding="utf-8")


def test_the_second_run_going_red_stays_red(monkeypatch, tmp_path, capsys) -> None:
    """反向臂：二跑仍红 ⇒ 退出非 0，且不写"未知放行"那句话。"""
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "coverage"])

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        return (1, FLAKE_LOG) if "--cov=rolecard_agent" in cmd else (1, FLAKE_LOG)

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "FLAKY-RECORDED" not in out and "真红" in out
