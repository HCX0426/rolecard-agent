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
    assert "-x" in fast and "--cov" not in fast, "快档该是裸跑 -x，不该量覆盖率"
    # 覆盖率档只说"量"（`--cov`）：**量什么范围、多少算过全在 pyproject 的 [tool.coverage.*]**。
    # 判据因此从"命令里有没有那串字面量"改成"命令里不许再有那些字面量" —— 谁把它们抄回
    # 命令行，就会再造出第二个事实面（CI/夜间臂/人肉各抄一份，漏抄的安静地量出另一个数）。
    assert "--cov" in cov and "-x" not in cov, "覆盖率档该量到底、不该一红就停"
    for banned in ("--cov-fail-under", "--cov=", "--cov-branch"):
        assert banned not in cov, f"覆盖率口径又回到命令行了（{banned}）：它住在 pyproject"
    assert len({f1, f2, c1, c2}) == 4, f"两档日志撞名了：{f1} {f2} {c1} {c2}"


def test_the_coverage_config_lives_in_pyproject(monkeypatch, tmp_path) -> None:
    """口径搬进配置之后，**配置里那三件必须在**：少了任何一件，读数就悄悄换范围。

    这一条不是给 pyproject 上保险，是给"搬了个家"这件事本身上保险：搬家时最容易
    只搬一半（比如只搬 `source` 忘了 `fail_under`，命令行删了、配置里没有 ⇒ 阈值静默消失，
    覆盖率再差也不会红 —— 那正是"看起来绿比红贵"那一族）。
    """
    import tomllib

    mod = _load(monkeypatch, tmp_path)
    assert "--cov" in mod.LANES["coverage"][0], "前提：命令只说量"
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    run = cfg["tool"]["coverage"]["run"]
    report = cfg["tool"]["coverage"]["report"]
    assert run["branch"] is True, "branch 关掉就没有【失败路径只走过一边】的信号"
    assert run["source"] == ["src/rolecard_agent"], "范围换了，历史读数就不可比"
    # 阈值活在这里一处（第七刀 85→90）。这条断言的作用是逼下一个人**来这里改**，
    # 而不是在别处加第二份配置 —— "搬了个家"是否搬全，就看改完这里红不红。
    assert report["fail_under"] == 90, "阈值不能住在别处"
    # 反向臂：命令行里不许留任何一份副本（上面那条判"在不在配置里"，这条判"有没有第二份"）
    joined = " ".join(mod.LANES["coverage"][0])
    assert "--cov-fail-under" not in joined and "rolecard_agent" not in joined


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


#: fixture/收集期炸掉的形状（2026-10-09 Windows 臂真发生）：短摘要打的是 `ERROR …`，
#: 而且**原因带在册签名**。旧正则只认 `FAILED` ⇒ 一条都摘不出来 ⇒ 明明该重跑取证，
#: 却走了「不在在册签名里，不重跑」那条分支按原样红。
ERROR_LOG = (
    "ERROR tests/unit/test_api_edges.py::test_upload_stays_pending - "
    "chromadb.errors.InternalError: Query error: Database error: "
    "error returned from database: (code: 5) database is locked\n"
    "354 passed, 1 error in 233.83s\n"
)


def test_an_error_at_setup_line_is_retried_like_a_failed_one(
    monkeypatch, tmp_path, capsys
) -> None:
    """chroma 这个偶发**最常以 ERROR 出现**（fixture 期就炸），重跑通道不能只接 `FAILED`。"""
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        if len(calls) == 1:
            return 1, ERROR_LOG
        assert "tests/unit/test_api_edges.py" in cmd, f"二跑该只跑红的那个文件：{cmd}"
        return 0, "1 passed\n"

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 0
    assert "FLAKY-RECORDED" in capsys.readouterr().out


#: 混合形状：一条在册 + 一条**真红**。旧实现问的是"整份日志里出现过签名吗"（`any`），
#: 于是这一发会整批进重跑；而重跑只跑失败的那几个文件 —— 一个"只有全套语境下才成立"的
#: 真红（跨用例污染正是这种）可以二跑绿、被记成 FLAKY-RECORDED 放行。
#: 文件头部第 1 条判据写的是「**全部**失败都带签名」，实现却更松 —— 这就是那条遮羞布。
MIXED_LOG = (
    "FAILED tests/unit/test_rag.py::test_scope_isolation - "
    "chromadb.errors.InternalError: Nothing found on disk\n"
    "FAILED tests/unit/test_x.py::y - AssertionError: 真的红了\n"
    "2 failed, 900 passed\n"
)


def test_a_mixed_batch_retries_nothing_even_with_one_flake(
    monkeypatch, tmp_path, capsys
) -> None:
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        return 1, MIXED_LOG if len(calls) == 1 else ""

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    assert len(calls) == 1, "混进真红的一批被整批重跑了：签名判据从「全部」退成了「任一」"
    out = capsys.readouterr().out
    assert "不重跑" in out
    # 不在册的那几条要**点名**，否则下一个人只能自己去 900 行里找是哪条挡了重跑
    assert "tests/unit/test_x.py" in out, out[-400:]


def test_an_unreasoned_summary_line_is_not_treated_as_a_flake(
    monkeypatch, tmp_path, capsys
) -> None:
    """空原因 = 不知道它为什么红 ⇒ 按不在册算（fail-closed），不许换来一次重跑放行。"""
    mod = _load(monkeypatch, tmp_path)
    bare = "FAILED tests/unit/test_z.py::w\n1 failed\n"
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        return 1, bare if len(calls) == 1 else ""

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    assert len(calls) == 1, "没原因文本的一条红被当成 chroma 偶发重跑了"


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


def test_a_fresh_checkout_without_build_still_gets_its_evidence(
    monkeypatch, tmp_path, capsys
) -> None:
    """`build/` 是 gitignore 的 —— 全新检出里**没有**这个目录，取证日志不许因此崩。

    2026-10-09 Windows 臂实测：首跑撞上在册 chroma 偶发，本该走"重跑取证"通道，结果
    `run1.write_text` 先炸在 `FileNotFoundError: …\\build\\gate-fast-run1.log` 上 —— 取证层
    自己成了新的红，重跑根本没发生。门禁 `--ci` 照不出它纯属顺序运气（静态组的读数那一步
    先把 build/ mkdir 了），这一条臂只跑本脚本就撞上了。上面的既有用例都拿 `tmp_path`
    当 BUILD —— 那是 pytest 建好的目录，**永远照不出"目录不存在"这一格**，所以这里刻意
    指到一个不存在的子目录。
    """
    mod = _load(monkeypatch, tmp_path)
    fresh = tmp_path / "checkout-without-build"  # 刻意不建：模拟全新检出
    assert not fresh.exists()
    monkeypatch.setattr(mod, "BUILD", fresh)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])

    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        return (1, FLAKE_LOG) if len(calls) == 1 else (0, "1 passed\n")

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 0  # 从前这一行之前就先抛 FileNotFoundError
    assert (fresh / "gate-fast-run1.log").exists(), "首跑日志必须在（目录是被这层自己建的）"
    assert (fresh / "gate-fast-run2-retry.log").exists()
    assert "FLAKY-RECORDED" in capsys.readouterr().out


def test_the_second_run_going_red_stays_red(monkeypatch, tmp_path, capsys) -> None:
    """反向臂：二跑仍红 ⇒ 退出非 0，且不写"未知放行"那句话。"""
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "coverage"])

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        return (1, FLAKE_LOG) if "--cov" in cmd else (1, FLAKE_LOG)

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "FLAKY-RECORDED" not in out and "真红" in out
